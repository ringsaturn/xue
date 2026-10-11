"""Placing the WRF mass grid in Lambert conformal space and sampling it
onto a regular latitude/longitude grid.

The target grid is the rectangle *inscribed* in the domain — the innermost
longitude of each side column and latitude of each side row — rather than
the footprint the HRRR path extends outwards to: a 500 m nest is small and
drawn alone, and a target cell outside the domain would be an extrapolated
edge value rendered as data. Every target center therefore lies inside the
mass grid and the bilinear blend needs no clamping.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ..reproject import LambertConformal
from .reader import WrfError, WrfGrid

WRF_SPHERE_RADIUS = 6_370_000.0
#: Largest distance, beyond the stored coordinate's own float32 resolution,
#: between a mass point as this placement computes it and the XLAT/XLONG
#: WRF wrote for it. One ulp of a float32 longitude near 140°E is 1.5e-5°,
#: about 1.4 m on the ground, so that resolution is allowed on top of this
#: bound; a real placement error (a wrong radius, a wrong center) is tens
#: of metres and up.
PLACEMENT_TOLERANCE_M = 1.0


@dataclass(frozen=True)
class MassGrid:
    """The mass grid in projected metres: ``x0``/``y0`` is the south-west
    mass point, rows run north."""

    projection: LambertConformal
    nx: int
    ny: int
    x0: float
    y0: float
    dx: float
    dy: float

    def forward(self, longitude: np.ndarray, latitude: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Fractional ``(column, row)`` of points given in degrees."""
        p = self.projection
        rho = p.scale / np.tan(np.pi / 4.0 + np.radians(latitude) / 2.0) ** p.cone_constant
        theta = p.cone_constant * np.radians(longitude - p.central_meridian)
        x = rho * np.sin(theta)
        y = p.origin_radius - rho * np.cos(theta)
        return (x - self.x0) / self.dx, (y - self.y0) / self.dy

    def inverse(self, column: np.ndarray, row: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Longitude and latitude in degrees of fractional mass indices."""
        p = self.projection
        x = self.x0 + column * self.dx
        dy = p.origin_radius - (self.y0 + row * self.dy)
        rho = np.copysign(np.sqrt(x * x + dy * dy), p.cone_constant)
        theta = np.arctan2(x, dy)
        latitude = 2.0 * np.arctan((p.scale / rho) ** (1.0 / p.cone_constant)) - np.pi / 2.0
        return p.central_meridian + np.degrees(theta / p.cone_constant), np.degrees(latitude)


def place_grid(grid: WrfGrid, xlat: np.ndarray, xlong: np.ndarray) -> MassGrid:
    """The mass grid located from the header's center, held to the
    latitude/longitude the model wrote for every mass point."""
    projection = LambertConformal(
        radius=WRF_SPHERE_RADIUS,
        standard_parallel_1=grid.truelat1,
        standard_parallel_2=grid.truelat2,
        latitude_of_origin=grid.moad_cen_lat,
        central_meridian=grid.stand_lon,
    )
    # CEN_LAT/CEN_LON is the center of the staggered grid, which is also the
    # center of the mass grid: mass point i sits at (i - (nx-1)/2)·DX from it.
    xc, yc = projection.forward(grid.cen_lon, grid.cen_lat)
    mass = MassGrid(
        projection=projection,
        nx=grid.nx,
        ny=grid.ny,
        x0=xc - (grid.nx - 1) / 2.0 * grid.dx,
        y0=yc - (grid.ny - 1) / 2.0 * grid.dy,
        dx=grid.dx,
        dy=grid.dy,
    )
    columns, rows = np.meshgrid(np.arange(grid.nx, dtype=np.float64), np.arange(grid.ny, dtype=np.float64))
    longitude, latitude = mass.inverse(columns, rows)
    metres_per_degree = WRF_SPHERE_RADIUS * math.pi / 180.0
    east_scale = metres_per_degree * np.cos(np.radians(xlat))
    north = np.abs(latitude - xlat) * metres_per_degree
    east = np.abs(longitude - xlong) * east_scale
    ulp_north = np.spacing(xlat.astype(np.float32)).astype(np.float64) * metres_per_degree
    ulp_east = np.spacing(xlong.astype(np.float32)).astype(np.float64) * east_scale
    excess = float(max(np.max(north - ulp_north), np.max(east - ulp_east)))
    if excess > PLACEMENT_TOLERANCE_M:
        raise WrfError(
            f"{grid.domain}: the Lambert conformal placement misses XLAT/XLONG by up to "
            f"{float(np.max(np.hypot(north, east))):.2f} m; the header's projection is not the one the model ran on"
        )
    return mass


@dataclass(frozen=True)
class Sampler:
    """Bilinear sampling of mass-grid planes at a regular grid's cell
    centers. Latitudes ascend (south to north) like the mass grid's rows,
    so no plane is flipped on the way through."""

    width: int
    height: int
    longitudes: np.ndarray
    latitudes: np.ndarray
    step: float
    column: np.ndarray
    row: np.ndarray
    fx: np.ndarray
    fy: np.ndarray

    def take(self, plane: np.ndarray) -> np.ndarray:
        column, row = self.column, self.row
        south = plane[row, column] * (1.0 - self.fx) + plane[row, column + 1] * self.fx
        north = plane[row + 1, column] * (1.0 - self.fx) + plane[row + 1, column + 1] * self.fx
        return south * (1.0 - self.fy) + north * self.fy


def inscribed_rectangle(mass: MassGrid) -> tuple[float, float, float, float]:
    """``(west, south, east, north)``: the innermost longitude of the west
    and east columns and latitude of the south and north rows of mass
    points, so the rectangle lies wholly inside the domain."""
    rows = np.arange(mass.ny, dtype=np.float64)
    columns = np.arange(mass.nx, dtype=np.float64)
    west_lon, _ = mass.inverse(np.zeros_like(rows), rows)
    east_lon, _ = mass.inverse(np.full_like(rows, mass.nx - 1), rows)
    _, south_lat = mass.inverse(columns, np.zeros_like(columns))
    _, north_lat = mass.inverse(columns, np.full_like(columns, mass.ny - 1))
    return float(west_lon.max()), float(south_lat.max()), float(east_lon.min()), float(north_lat.min())


def _strictly_inside(low: float, high: float, step: float) -> np.ndarray:
    """The multiples of ``step`` strictly between ``low`` and ``high``."""
    first = math.floor(round(low / step, 9)) + 1
    last = math.ceil(round(high / step, 9)) - 1
    if last < first:
        raise WrfError(f"no {step}° cell center lies inside [{low:.4f}, {high:.4f}]")
    return np.round(np.arange(first, last + 1, dtype=np.float64) * step, 9)


def build_sampler(mass: MassGrid, step: float) -> Sampler:
    west, south, east, north = inscribed_rectangle(mass)
    longitudes = _strictly_inside(west, east, step)
    latitudes = _strictly_inside(south, north, step)
    lon_grid, lat_grid = np.meshgrid(longitudes, latitudes)
    c, r = mass.forward(lon_grid, lat_grid)
    # Inside by construction; the clip only absorbs the last-bit rounding of
    # a center that lies on the boundary of the inscribed rectangle.
    c = np.clip(c, 0.0, mass.nx - 1.0)
    r = np.clip(r, 0.0, mass.ny - 1.0)
    column = np.minimum(np.floor(c), mass.nx - 2.0).astype(np.int64)
    row = np.minimum(np.floor(r), mass.ny - 2.0).astype(np.int64)
    return Sampler(
        width=len(longitudes),
        height=len(latitudes),
        longitudes=longitudes,
        latitudes=latitudes,
        step=step,
        column=column,
        row=row,
        fx=c - column,
        fy=r - row,
    )

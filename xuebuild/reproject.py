"""The projection arithmetic of a model published on its own map projection,
and the true latitudes of a regular Gaussian grid.

A model computed on a Lambert conformal conic keeps its grid: the bundle's
``grid.projection`` block declares it (docs/format.md, "Projected Sources") and
every plane stays as the model wrote it. What the encoder does with the
projection is place the published cells (:class:`ProjectedGrid`), cut a
geographic box out of them, and turn the model's grid-relative winds to east and
north (:func:`wind_rotation`).

Every operation here is repeated by the native encoder
(``rust/xue/src/projection.rs``), in the same order and on the same doubles, and
the two are held byte-identical. Per-point trigonometry goes through the C
library's ``sin``/``cos``/``tan``/``pow``, which is what Rust's ``f64`` methods
call too, rather than NumPy's vectorised approximations; only exact IEEE
multiplications, additions, divisions and square roots run elementwise on a
whole plane.
"""

from __future__ import annotations

import functools
import math
import re
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from .errors import ConversionError

LAMBERT_CONFORMAL_KIND = "lambert-conformal"
"""``grid.projection.kind`` of :class:`LambertConformal`."""


@dataclass(frozen=True)
class LambertConformal:
    """A Lambert conformal conic projection on a sphere, as GRIB2 grid
    definition template 3.30 declares it and as GDAL reports it back
    (``Lambert Conic Conformal (2SP)`` with a spherical datum).

    Snyder (1987) §15, spherical case. With two equal standard parallels the
    cone constant is ``sin`` of the parallel, which the two-parallel formula
    cannot express (0/0); both forms are written out so the constants are
    bit-identical between the two encoders."""

    radius: float
    """Sphere radius in metres — 6371229 for every NCEP product."""
    standard_parallel_1: float
    standard_parallel_2: float
    latitude_of_origin: float
    central_meridian: float

    @property
    def cone_constant(self) -> float:
        phi_1 = math.radians(self.standard_parallel_1)
        phi_2 = math.radians(self.standard_parallel_2)
        if phi_1 == phi_2:
            return math.sin(phi_1)
        return math.log(math.cos(phi_1) / math.cos(phi_2)) / math.log(
            math.tan(math.pi / 4.0 + phi_2 / 2.0) / math.tan(math.pi / 4.0 + phi_1 / 2.0)
        )

    @property
    def scale(self) -> float:
        """``R·F`` — the radius of the parallel at latitude ``φ`` is
        ``scale / tan(π/4 + φ/2)^n``."""
        n = self.cone_constant
        phi_1 = math.radians(self.standard_parallel_1)
        return self.radius * (math.cos(phi_1) * math.tan(math.pi / 4.0 + phi_1 / 2.0) ** n / n)

    def parallel_radius(self, latitude: float) -> float:
        """Projected distance from the apex of the cone to the parallel at
        ``latitude`` (degrees)."""
        return self.scale / math.tan(math.pi / 4.0 + math.radians(latitude) / 2.0) ** self.cone_constant

    @property
    def origin_radius(self) -> float:
        return self.parallel_radius(self.latitude_of_origin)

    def meridian_angle(self, longitude: float) -> float:
        """The angle, in radians, the meridian at ``longitude`` makes with
        the central meridian on the cone."""
        return self.cone_constant * math.radians(longitude - self.central_meridian)

    def forward(self, longitude: float, latitude: float) -> tuple[float, float]:
        """Projected ``(x, y)`` in metres of one point."""
        rho = self.parallel_radius(latitude)
        theta = self.meridian_angle(longitude)
        return rho * math.sin(theta), self.origin_radius - rho * math.cos(theta)

    def forward_wrapped(self, longitude: float, latitude: float) -> tuple[float, float]:
        """:meth:`forward` for a longitude spelled any way a caller may spell
        it. Without the wrap, 262.5 and -97.5 would open different angles on
        the cone and one of them would land off a grid that holds the point."""
        delta = (longitude - self.central_meridian + 180.0) % 360.0 - 180.0
        return self.forward(self.central_meridian + delta, latitude)

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        """Longitude and latitude, in degrees, of one projected point."""
        n = self.cone_constant
        dy = self.origin_radius - y
        # sqrt, not hypot: Python's hypot is its own algorithm, not libm's,
        # and the native encoder must land on the same double.
        rho = math.copysign(math.sqrt(x * x + dy * dy), n)
        # Both arguments carry the cone's sign: on a southern cone (n < 0) the
        # radius is negative, and the angle would land half a turn away.
        sign = math.copysign(1.0, n)
        theta = math.atan2(sign * x, sign * dy)
        latitude = 2.0 * math.atan((self.scale / rho) ** (1.0 / n)) - math.pi / 2.0
        return self.central_meridian + math.degrees(theta / n), math.degrees(latitude)

    def wkt(self) -> str:
        """This projection as WKT2, the ``crs_wkt`` of a store's ``crs`` array
        (docs/zarr-profile.md, "Coordinates"). Spelled here rather than taken
        from GDAL, whose text differs between releases: the array document is
        part of a store's bytes. Numbers print as their shortest round trip."""
        degree = 'ANGLEUNIT["degree",0.0174532925199433]'
        metre = 'LENGTHUNIT["metre",1]'
        return (
            'PROJCRS["Lambert Conic Conformal on a sphere",'
            'BASEGEOGCRS["Sphere",'
            f'DATUM["Sphere",ELLIPSOID["Sphere",{self.radius!r},0,{metre}]],'
            f'PRIMEM["Greenwich",0,{degree}]],'
            'CONVERSION["Lambert Conic Conformal (2SP)",'
            'METHOD["Lambert Conic Conformal (2SP)",ID["EPSG",9802]],'
            f'PARAMETER["Latitude of false origin",{self.latitude_of_origin!r},{degree}],'
            f'PARAMETER["Longitude of false origin",{self.central_meridian!r},{degree}],'
            f'PARAMETER["Latitude of 1st standard parallel",{self.standard_parallel_1!r},{degree}],'
            f'PARAMETER["Latitude of 2nd standard parallel",{self.standard_parallel_2!r},{degree}],'
            f'PARAMETER["Easting at false origin",0,{metre}],'
            f'PARAMETER["Northing at false origin",0,{metre}]],'
            'CS[Cartesian,2],'
            f'AXIS["easting",east,ORDER[1],{metre}],'
            f'AXIS["northing",north,ORDER[2],{metre}]]'
        )

    def cf_attributes(self) -> dict[str, Any]:
        """The CF grid mapping a store's ``crs`` array carries, in the order
        the array document writes it."""
        return {
            "grid_mapping_name": "lambert_conformal_conic",
            "latitude_of_projection_origin": self.latitude_of_origin,
            "longitude_of_central_meridian": self.central_meridian,
            "standard_parallel": [self.standard_parallel_1, self.standard_parallel_2],
            "earth_radius": self.radius,
            "crs_wkt": self.wkt(),
        }


@dataclass(frozen=True)
class ProjectedGrid:
    """A published grid in projected space, in the north-to-south row order
    GDAL reads it in: the ``grid.projection`` block of a bundle's metadata.
    ``x0``/``y0`` are the *center* of the first (north-west) cell; cell
    (``row``, ``column``) is centered at ``x0 + column * dx``, ``y0 + row * dy``,
    so ``dx`` is positive and ``dy`` negative."""

    projection: LambertConformal
    width: int
    height: int
    x0: float
    y0: float
    dx: float
    dy: float

    def decimated(self) -> ProjectedGrid:
        """Every second row and column from the first: the first cell stays
        where it is."""
        return replace(
            self,
            width=(self.width + 1) // 2,
            height=(self.height + 1) // 2,
            dx=self.dx * 2,
            dy=self.dy * 2,
        )

    def window(self, row: int, column: int, height: int, width: int) -> ProjectedGrid:
        """The ``height`` x ``width`` cells from (``row``, ``column``)."""
        return replace(
            self, width=width, height=height, x0=self.x0 + column * self.dx, y0=self.y0 + row * self.dy
        )

    def metadata(self) -> dict[str, Any]:
        """The ``grid.projection`` block."""
        projection = self.projection
        return {
            "kind": LAMBERT_CONFORMAL_KIND,
            "radius": projection.radius,
            "standardParallel1": projection.standard_parallel_1,
            "standardParallel2": projection.standard_parallel_2,
            "latitudeOfOrigin": projection.latitude_of_origin,
            "centralMeridian": projection.central_meridian,
            "x0": self.x0,
            "y0": self.y0,
            "dx": self.dx,
            "dy": self.dy,
        }

    @classmethod
    def from_metadata(cls, block: dict[str, Any], width: int, height: int) -> ProjectedGrid:
        """The grid a validated ``grid.projection`` block declares."""
        return cls(
            projection=LambertConformal(
                radius=float(block["radius"]),
                standard_parallel_1=float(block["standardParallel1"]),
                standard_parallel_2=float(block["standardParallel2"]),
                latitude_of_origin=float(block["latitudeOfOrigin"]),
                central_meridian=float(block["centralMeridian"]),
            ),
            width=width,
            height=height,
            x0=float(block["x0"]),
            y0=float(block["y0"]),
            dx=float(block["dx"]),
            dy=float(block["dy"]),
        )

    def footprint(self) -> tuple[float, float, float, float]:
        """``(west, south, east, north)`` in degrees: the extremes every
        boundary cell center reaches. On a conic grid the longitude extremes
        sit on the northern corners and the latitude extremes along the
        edges, so the whole boundary is walked rather than the corners
        alone."""
        west = south = math.inf
        east = north = -math.inf
        last_column = self.width - 1
        last_row = self.height - 1
        cells = [(column, row) for column in range(self.width) for row in {0, last_row}]
        cells += [(column, row) for column in {0, last_column} for row in range(1, last_row)]
        for column, row in cells:
            longitude, latitude = self.projection.inverse(self.x0 + column * self.dx, self.y0 + row * self.dy)
            west = min(west, longitude)
            east = max(east, longitude)
            south = min(south, latitude)
            north = max(north, latitude)
        return west, south, east, north


@functools.lru_cache(maxsize=8)
def wind_rotation(grid: ProjectedGrid) -> tuple[np.ndarray, np.ndarray]:
    """``(sin α, cos α)`` of every cell of ``grid``, flat in row-major order,
    where ``α = n (λ − λ0)`` is the angle from the grid's y axis to true north.

    Read off the cell's coordinates rather than its longitude, so no libm call
    runs per cell: with ``x = x0 + column dx``, ``y = y0 + row dy`` and
    ``ρ = sqrt(x² + (ρ0 − y)²)``, ``sin α = x / ρ`` and ``cos α = (ρ0 − y) / ρ``.
    Only ``ρ0`` goes through the C library, once. ``ρ`` takes the cone's sign, as
    in :meth:`LambertConformal.inverse`, which on a northern cone is no change at
    all. Computed once per grid and shared by every wind plane of a build."""
    projection = grid.projection
    apex = projection.origin_radius - (grid.y0 + np.arange(grid.height, dtype=np.float64) * grid.dy)
    x = grid.x0 + np.arange(grid.width, dtype=np.float64) * grid.dx
    rho = math.copysign(1.0, projection.cone_constant) * np.sqrt(np.add.outer(apex * apex, x * x))
    return (x[np.newaxis, :] / rho).ravel(), (apex[:, np.newaxis] / rho).ravel()


def rotate_winds(grid: ProjectedGrid, u: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One grid-relative wind pair turned to east and north, on the flat planes
    ``grid`` describes: ``u_e = cos α u + sin α v``, ``v_e = −sin α u + cos α v``."""
    sine, cosine = wind_rotation(grid)
    return cosine * u + sine * v, -sine * u + cosine * v


def gaussian_latitude(n: int, row: int) -> float:
    """The latitude in degrees of row ``row`` of the regular Gaussian grid with
    ``n`` latitudes per hemisphere, counted from the north: a root of the
    Legendre polynomial of order ``2n``, by the iteration of docs/zarr-profile.md,
    "Coordinates". Operation for operation the same as
    ``rust/xue/src/projection.rs``: a library root finder differs by up to
    1.8e-12° near the poles, which would move a store's ``latitude`` bytes."""
    order = 2 * n
    degree = float(order)
    x = math.cos(math.pi * (row + 0.75) / (degree + 0.5))
    for _ in range(100):
        previous, current = 1.0, x
        for k in range(2, order + 1):
            previous, current = current, ((2.0 * k - 1.0) * x * current - (k - 1.0) * previous) / k
        derivative = degree * (x * current - previous) / (x * x - 1.0)
        step = current / derivative
        x -= step
        if abs(step) < 1e-15:
            break
    return math.degrees(math.asin(x))


@functools.lru_cache(maxsize=4)
def gaussian_latitudes(n: int) -> tuple[float, ...]:
    """Every latitude of the Gaussian grid with ``n`` latitudes per hemisphere,
    north to south. Every bundle of every tier of a run asks for it, so it is
    kept."""
    return tuple(gaussian_latitude(n, row) for row in range(2 * n))


def nearest_gaussian_row(n: int, latitude: float) -> int:
    """The row of the Gaussian grid a latitude is nearest to: the inverse of
    the iteration's first guess, ``cos(π (row + 0.75) / (2n + 0.5))``, which
    lies within a few hundredths of a row of the root."""
    return math.floor((90.0 - latitude) * (2.0 * n + 0.5) / 180.0 - 0.25)


# `PARAMETER["Latitude of false origin",38.5,` — GDAL's WKT2 spelling of each
# Lambert conformal parameter, as `gdalinfo -json` prints it and as the gdal
# crate's `SpatialRef::to_wkt` returns it.
_WKT_PARAMETER = re.compile(r'PARAMETER\["([^"]+)",\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)')
_WKT_SPHERE = re.compile(r'ELLIPSOID\["[^"]*",\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?),\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)')
_LAMBERT_METHOD = "Lambert Conic Conformal (2SP)"


def lambert_conformal_from_wkt(wkt: str) -> LambertConformal | None:
    """The projection a GDAL WKT string describes, or None for a geographic
    (regular latitude/longitude) coordinate system. Anything projected that
    is not a spherical Lambert conformal conic is an error: the format
    declares one projection."""
    if not wkt or wkt.lstrip().startswith(("GEOGCRS", "GEOGCS")):
        return None
    if _LAMBERT_METHOD not in wkt:
        raise ConversionError(f"unsupported map projection (only {_LAMBERT_METHOD} on a sphere is): {wkt[:80]}")
    sphere = _WKT_SPHERE.search(wkt)
    if sphere is None or float(sphere.group(2)) != 0.0:
        raise ConversionError("Lambert conformal grids are supported on a sphere only")
    parameters = {name: float(value) for name, value in _WKT_PARAMETER.findall(wkt)}
    try:
        projection = LambertConformal(
            radius=float(sphere.group(1)),
            standard_parallel_1=parameters["Latitude of 1st standard parallel"],
            standard_parallel_2=parameters["Latitude of 2nd standard parallel"],
            latitude_of_origin=parameters["Latitude of false origin"],
            central_meridian=parameters["Longitude of false origin"],
        )
    except KeyError as exc:
        raise ConversionError(f"Lambert conformal WKT lacks {exc}") from exc
    if parameters.get("Easting at false origin", 0.0) or parameters.get("Northing at false origin", 0.0):
        raise ConversionError("Lambert conformal grids with a false origin are unsupported")
    return projection

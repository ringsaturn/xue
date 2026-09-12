"""Regridding a projected source onto the regular latitude/longitude grid
the format describes.

The container knows one grid: rows of equal latitude, columns of equal
longitude (docs/format.md, "Grid Layout"). A model computed on a map
projection — HRRR on a Lambert conformal conic, 3 km — does not arrive on
one, so the encoder resamples every plane onto a regular grid before
anything else looks at it: the plane is bilinearly sampled at each target
cell center, whose projected position is found with the projection's own
forward formulas. Every step past extraction then runs exactly as it does
for a source that was regular to begin with, and the bundle carries an
ordinary ``grid`` block.

The target grid is the source's footprint: the extremes of longitude and
latitude its boundary cells reach, snapped outwards to the declared step.
A conic domain is a trapezoid on that rectangle, so the rectangle has
corners the source never covered. They are not gaps and not a reserved
code — the format has neither — but the nearest source cell continued
outwards (the sampling coordinate is clamped to the source grid), so the
plane is complete, the edge never meets a discontinuity a filter or a
contour would draw, and the extension compresses to almost nothing. A
renderer that knows the source's projection clips to its footprint; one
that does not draws the extension, which is the price of a format without
a bitmap.

Every operation here is repeated by the native encoder
(``rust/xue/src/encode/reproject.rs``), in the same order and on the same
doubles, and the two are held byte-identical. The per-row and per-column
trigonometry goes through the C library's ``sin``/``cos``/``tan``/``pow`` —
which is what Rust's ``f64`` methods call too — rather than NumPy's
vectorised approximations; only exact IEEE multiplications, subtractions
and divisions are done elementwise on the whole plane.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import numpy as np

from .errors import ConversionError


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

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        """Longitude and latitude, in degrees, of one projected point."""
        n = self.cone_constant
        dy = self.origin_radius - y
        # sqrt, not hypot: Python's hypot is its own algorithm, not libm's,
        # and the native encoder must land on the same double.
        rho = math.copysign(math.sqrt(x * x + dy * dy), n)
        theta = math.atan2(x, dy)
        latitude = 2.0 * math.atan((self.scale / rho) ** (1.0 / n)) - math.pi / 2.0
        return self.central_meridian + math.degrees(theta / n), math.degrees(latitude)


@dataclass(frozen=True)
class ProjectedGrid:
    """A source grid in projected space: what GDAL's geotransform says about
    a Lambert conformal GRIB, in the north-to-south row order GDAL reads it
    in. ``x0``/``y0`` are the *center* of the first (north-west) cell."""

    projection: LambertConformal
    width: int
    height: int
    x0: float
    y0: float
    dx: float
    dy: float
    """Both positive: columns run east, rows run south."""

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
        cells = [(column, row) for column in range(self.width) for row in (0, last_row)]
        cells += [(column, row) for column in (0, last_column) for row in range(1, last_row)]
        for column, row in cells:
            longitude, latitude = self.projection.inverse(self.x0 + column * self.dx, self.y0 - row * self.dy)
            west = min(west, longitude)
            east = max(east, longitude)
            south = min(south, latitude)
            north = max(north, latitude)
        return west, south, east, north


@dataclass(frozen=True)
class Regrid:
    """What a source declares: that its planes are projected and must be
    resampled onto a regular grid of ``step`` degrees. The grid's extent is
    the source's own footprint, so a cropped fixture of the same product
    lands on a proportionally smaller grid."""

    step: float


# `PARAMETER["Latitude of false origin",38.5,` — GDAL's WKT2 spelling of each
# Lambert conformal parameter, as `gdalinfo -json` prints it and as the gdal
# crate's `SpatialRef::to_wkt` returns it.
_WKT_PARAMETER = re.compile(r'PARAMETER\["([^"]+)",\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)')
_WKT_SPHERE = re.compile(r'ELLIPSOID\["[^"]*",\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?),\s*(-?[0-9.]+(?:[eE][-+]?[0-9]+)?)')
_LAMBERT_METHOD = "Lambert Conic Conformal (2SP)"


def lambert_conformal_from_wkt(wkt: str) -> LambertConformal | None:
    """The projection a GDAL WKT string describes, or None for a geographic
    (regular latitude/longitude) coordinate system. Anything projected that
    is not a spherical Lambert conformal conic is an error: the resampler
    knows one projection."""
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


@dataclass(frozen=True)
class Resampler:
    """Bilinear sampling of every source plane at the target grid's cell
    centers. The sampling coordinates are computed once per build and
    shared by every plane; ``take`` is the per-plane step."""

    source: ProjectedGrid
    width: int
    height: int
    first_longitude: float
    first_latitude: float
    step: float
    column: np.ndarray
    """Index of the source column west of each target cell, ``(height, width)``."""
    row: np.ndarray
    """Index of the source row north of each target cell."""
    fx: np.ndarray
    """Fraction of the way east from ``column`` to the next, in ``[0, 1]``."""
    fy: np.ndarray
    """Fraction of the way south from ``row`` to the next."""

    @property
    def source_shape(self) -> tuple[int, int]:
        return self.source.height, self.source.width

    def take(self, plane: np.ndarray) -> np.ndarray:
        """One source plane, ``(source.height, source.width)``, resampled to
        ``(height, width)``. The four corner weights are applied in a fixed
        order — the two rows blended east-west first, then north-south —
        which the native encoder repeats."""
        if plane.shape != self.source_shape:
            raise ConversionError(f"resampler expected a {self.source_shape} plane, got {plane.shape}")
        column, row = self.column, self.row
        top = plane[row, column] * (1.0 - self.fx) + plane[row, column + 1] * self.fx
        bottom = plane[row + 1, column] * (1.0 - self.fx) + plane[row + 1, column + 1] * self.fx
        return top * (1.0 - self.fy) + bottom * self.fy


def build_resampler(source: ProjectedGrid, regrid: Regrid) -> Resampler:
    """The regular grid a projected source lands on, and how to sample it.

    The extent is the footprint snapped outwards to whole steps, so the
    grid's origin is a multiple of the step (the way the 0.25° grids' are)
    and every source cell center lies inside it. The trigonometry is one
    pass per row (the parallel's radius) and one per column (the meridian's
    angle); the plane-sized arrays are products and differences of those."""
    if source.width < 2 or source.height < 2:
        raise ConversionError("a projected grid needs at least two rows and two columns to resample")
    step = regrid.step
    west, south, east, north = source.footprint()
    first_column = math.floor(west / step)
    last_column = math.ceil(east / step)
    first_row = math.ceil(north / step)
    last_row = math.floor(south / step)
    width = last_column - first_column + 1
    height = first_row - last_row + 1
    first_longitude = round(first_column * step, 10)
    first_latitude = round(first_row * step, 10)
    if width * step > 360.0:
        raise ConversionError("a projected source wider than the globe cannot be regridded")

    projection = source.projection
    origin_radius = projection.origin_radius
    radius = np.array(
        [projection.parallel_radius(first_latitude - j * step) for j in range(height)], dtype=np.float64
    )
    angles = [projection.meridian_angle(first_longitude + i * step) for i in range(width)]
    sin_theta = np.array([math.sin(theta) for theta in angles], dtype=np.float64)
    cos_theta = np.array([math.cos(theta) for theta in angles], dtype=np.float64)
    x = np.outer(radius, sin_theta)
    y = origin_radius - np.outer(radius, cos_theta)
    c = (x - source.x0) / source.dx
    r = (source.y0 - y) / source.dy
    # Clamp into the source grid: a cell beyond its edge samples the edge.
    c = np.clip(c, 0.0, float(source.width - 1))
    r = np.clip(r, 0.0, float(source.height - 1))
    column = np.minimum(np.floor(c), float(source.width - 2)).astype(np.int64)
    row = np.minimum(np.floor(r), float(source.height - 2)).astype(np.int64)
    return Resampler(
        source=source,
        width=width,
        height=height,
        first_longitude=first_longitude,
        first_latitude=first_latitude,
        step=step,
        column=column,
        row=row,
        fx=c - column,
        fy=r - row,
    )

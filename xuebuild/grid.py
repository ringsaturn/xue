"""The grid one run's planes are published on (mirrored in ``encode/grid.rs``).

A file's grid is read off ``gdalinfo`` (:func:`grid_info`), snapped to the
round step and origin GDAL's derivation misses by a hair, rolled to the
-180-first layout, thinned where a source publishes coarser than it arrives,
resampled where it arrives on a map projection, and cropped to a showcase
window (:func:`crop_grid`). The two encoders must agree on every number of
it to the bit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from .errors import ConversionError
from .gdal import dataset_info
from .reproject import ProjectedGrid, Resampler, build_resampler, lambert_conformal_from_wkt
from .sources import Downsample, SourceSpec


@dataclass(frozen=True)
class CropWindow:
    """A rectangular window cut out of every extracted plane, indexed in the
    -180-first layout the column roll produces.

    ``column_start`` may run past the source's last column: a window crossing
    the antimeridian continues from column 0, so columns are always taken
    modulo ``source_width``. Rows never wrap — the grid ends at the poles."""

    source_width: int
    source_height: int
    row_start: int
    column_start: int
    width: int
    height: int

    def take(self, plane: np.ndarray) -> np.ndarray:
        """The window of one (source_height, source_width) plane."""
        rows = plane[self.row_start : self.row_start + self.height]
        end = self.column_start + self.width
        if end <= self.source_width:
            return rows[:, self.column_start : end]
        return rows[:, np.arange(self.column_start, end) % self.source_width]


@dataclass(frozen=True)
class BlockReduction:
    """The thinning a source's :class:`~xuebuild.sources.Downsample` asks for,
    fixed to one file's grid: every ``factor`` x ``factor`` block of the
    ``(source_height, source_width)`` plane becomes its maximum. Carried on
    the grid the way a :class:`~xuebuild.reproject.Resampler` is, and like it
    never serialized — the bundle's grid is the thinned one."""

    factor: int
    source_width: int
    source_height: int

    @property
    def source_shape(self) -> tuple[int, int]:
        return self.source_height, self.source_width

    def take(self, plane: np.ndarray) -> np.ndarray:
        """The block maximum of one ``(source_height, source_width)`` plane.
        A NaN anywhere in a block is the block's value, as with ``max``
        everywhere in this pipeline, so a broken record still fails the
        completeness check downstream instead of vanishing into a maximum."""
        if plane.shape != self.source_shape:
            raise ConversionError(f"plane is {plane.shape}, expected {self.source_shape}")
        k = self.factor
        return plane.reshape(self.source_height // k, k, self.source_width // k, k).max(axis=(1, 3))


@dataclass(frozen=True)
class GridInfo:
    width: int
    height: int
    first_longitude: float
    first_latitude: float
    longitude_step: float
    latitude_step: float
    column_roll: int = 0
    """Columns every extracted plane is rolled right by, so grids GDAL leaves
    starting at Greenwich (the sflux Gaussian grid) come out in the same
    -180-first layout as every other source. 0 for grids GDAL already rotates."""
    crop: CropWindow | None = None
    """Regional window applied after the roll (showcase cases). When set,
    every field above describes the *cropped* planes, and the window carries
    the source dimensions gdal_translate actually extracts — or, on a
    projected source, the regular grid the resampler produces. Never
    serialized: the cropped origin and extent already say where the data is."""
    resample: Resampler | None = None
    """Set for a source on a map projection (HRRR): the extracted planes are
    the projected grid, and every field above describes the regular grid
    they are resampled onto (:mod:`xuebuild.reproject`), before any crop.
    Never serialized either — the bundle's grid is the regular one."""
    downsample: BlockReduction | None = None
    """Set for a source published coarser than it arrives (MRMS): the
    extracted planes are the source grid, and every field above describes
    the thinned grid their block maxima land on, before any crop. Never
    serialized either."""

    @property
    def wraps(self) -> bool:
        return abs(self.width * self.longitude_step - 360.0) < 1e-6

    @property
    def source_shape(self) -> tuple[int, int]:
        """(height, width) of the plane gdal_translate extracts."""
        if self.resample is not None:
            return self.resample.source_shape
        if self.downsample is not None:
            return self.downsample.source_shape
        if self.crop is None:
            return self.height, self.width
        return self.crop.source_height, self.crop.source_width

    def decimated(self) -> GridInfo:
        """The grid produced by keeping every second row and column (rows and
        columns 0, 2, 4, ...). For the 721-row production grid the last kept
        row still lands exactly on the south pole, and 720 columns at a
        doubled step still cover the full 360 degrees."""
        return GridInfo(
            width=(self.width + 1) // 2,
            height=(self.height + 1) // 2,
            first_longitude=self.first_longitude,
            first_latitude=self.first_latitude,
            longitude_step=self.longitude_step * 2,
            latitude_step=self.latitude_step * 2,
        )

    def metadata(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "layout": "row-major",
            "rowOrder": "north-to-south",
            "columnOrder": "west-to-east",
            "firstLongitude": self.first_longitude,
            "firstLatitude": self.first_latitude,
            "longitudeStep": self.longitude_step,
            "latitudeStep": self.latitude_step,
            "wrapLongitude": self.wraps,
        }


def grid_info(path: Path, source: SourceSpec | None = None) -> GridInfo:
    """The grid one file's planes are published on. A regular
    latitude/longitude file describes it outright; a file on a map
    projection describes the projected grid, and the published one is the
    regular grid ``source.regrid`` asks for over its footprint — a source
    that declares no regrid must not be projected, and one that does must
    be, so a file of the wrong kind is refused rather than misread."""
    info = dataset_info(path, description=f"inspect grid of {path}")
    width, height = (int(value) for value in info["size"])
    transform = info["geoTransform"]
    if transform[2] or transform[4]:
        raise ConversionError(f"rotated grids are unsupported: {path}")
    projection = lambert_conformal_from_wkt(str(info.get("coordinateSystem", {}).get("wkt", "")))
    regrid = source.regrid if source is not None else None
    if projection is not None:
        if regrid is None:
            raise ConversionError(f"{path} is on a map projection, which this source does not declare")
        if transform[1] <= 0 or transform[5] >= 0:
            raise ConversionError(f"grid must run west-to-east and north-to-south: {path}")
        # GDAL places a projected grid by projecting the record's first
        # point itself, and two GDAL releases land that double a few
        # nanometres apart (seen between the wheel's 3.13 and Ubuntu's
        # system GDAL). The resampler's every coordinate descends from these
        # four numbers, and the two encoders must read the same grid, so
        # they are taken to the millimetre — far below anything a 3 km grid
        # means, far above the noise. The native encoder rounds the same
        # way (grid.rs `round3`).
        x0, dx, y0, dy = (round(float(transform[index]), 3) for index in (0, 1, 3, 5))
        resampler = build_resampler(
            ProjectedGrid(
                projection=projection,
                width=width,
                height=height,
                x0=x0 + dx / 2.0,
                y0=y0 + dy / 2.0,
                dx=dx,
                dy=-dy,
            ),
            regrid,
        )
        return GridInfo(
            width=resampler.width,
            height=resampler.height,
            first_longitude=resampler.first_longitude,
            first_latitude=resampler.first_latitude,
            longitude_step=resampler.step,
            latitude_step=-resampler.step,
            resample=resampler,
        )
    if regrid is not None:
        raise ConversionError(f"{path} is a regular grid, but this source declares a projected one")
    lon_step, lat_step = float(transform[1]), float(transform[5])
    if lon_step <= 0 or lat_step >= 0:
        raise ConversionError(f"grid must run west-to-east and north-to-south: {path}")
    grid = normalize_longitudes(
        snap_regional_steps(
            snap_global_longitudes(
                GridInfo(
                    width=width,
                    height=height,
                    first_longitude=float(transform[0]) + lon_step / 2,
                    first_latitude=float(transform[3]) + lat_step / 2,
                    longitude_step=lon_step,
                    latitude_step=lat_step,
                )
            )
        )
    )
    if source is not None and source.downsample is not None:
        grid = downsample_grid(grid, source.downsample, path)
    return grid


def downsample_grid(grid: GridInfo, downsample: Downsample, path: Path) -> GridInfo:
    """The grid a source's block reduction publishes: one cell per
    ``factor`` x ``factor`` block, centered on the block — half a source
    step in from the source's first center per extra cell — at ``factor``
    times the step. The source must divide into whole blocks, and a global
    or rolled grid is not thinned (nothing publishes one that way)."""
    k = downsample.factor
    if k < 1:
        raise ConversionError(f"downsample factor must be positive, not {k}")
    if grid.width % k or grid.height % k:
        raise ConversionError(
            f"{path} is {grid.width}x{grid.height}, which does not divide into {k}x{k} blocks"
        )
    if grid.wraps or grid.column_roll:
        raise ConversionError(f"{path} is a global grid, which this source does not thin")
    return GridInfo(
        width=grid.width // k,
        height=grid.height // k,
        # Rounded to ten decimals the way a crop's origin is: the sum of two
        # decimals is not always the double nearest their decimal sum.
        first_longitude=round(grid.first_longitude + grid.longitude_step * (k - 1) / 2, 10),
        first_latitude=round(grid.first_latitude + grid.latitude_step * (k - 1) / 2, 10),
        longitude_step=grid.longitude_step * k,
        latitude_step=grid.latitude_step * k,
        downsample=BlockReduction(factor=k, source_width=grid.width, source_height=grid.height),
    )


# How far short of (or past) a full circle a grid's columns may fall, as a
# fraction of one cell, and still be the global grid they clearly are.
_GLOBAL_SPAN_TOLERANCE_CELLS = 1e-3


def snap_global_longitudes(grid: GridInfo) -> GridInfo:
    """Give a global grid the exact step and origin its column count implies.

    GDAL derives a GRIB grid's longitude step from the first and last
    longitudes rather than from the increment the record also carries, so
    an encoder that rounds the last longitude is enough to put a grid a hair
    off the globe: WAVEWATCH III writes the 0.25° grid's last column as
    359.750016°, which GDAL turns into a 0.2500000111° step whose 1440
    columns span 360.000016° — not a wrapping grid by :attr:`GridInfo.wraps`,
    and a different ``longitudeStep`` from the pgrb2 record of the same
    cycle on the very same grid. A grid whose columns span 360° to within a
    thousandth of a cell is the global grid, and is described as one: the
    step becomes ``360 / width`` and the first center is placed on that
    step's grid (rounding half up, as everywhere in this pipeline) when it
    lies within the same tolerance of it. An exact grid (every other source)
    passes through unchanged — the same numbers come back out — and a
    regional grid is left alone. The native encoder applies the identical
    rule (``grid.rs``): the two must agree to the bit."""
    span = grid.width * grid.longitude_step
    if abs(span - 360.0) > grid.longitude_step * _GLOBAL_SPAN_TOLERANCE_CELLS:
        return grid
    step = 360.0 / grid.width
    first = math.floor(grid.first_longitude / step + 0.5) * step
    if abs(first - grid.first_longitude) > step * _GLOBAL_SPAN_TOLERANCE_CELLS:
        first = grid.first_longitude
    return replace(grid, longitude_step=step, first_longitude=first)


# The steps a regional grid is snapped to, as (unit, divisor) in the order
# they are tried: whole thousandths of a degree, then whole arc-minutes
# (EFAS's 1/60°, which no decimal spells: the double GDAL derives for it
# needs seventeen digits, more than some `gdalinfo` releases print).
_REGIONAL_STEP_UNITS = ((1e-3, 1000.0), (1.0 / 60.0, 60.0))


def snap_regional_steps(grid: GridInfo) -> GridInfo:
    """Give a regional grid the round step it was clearly published on.

    The same GDAL habit that puts a global grid a hair off the globe puts a
    regional one a hair off its step: the step is derived from the first
    and last coordinates, and MRMS writes the last longitude of its 0.01°
    grid two millionths of a degree short, which GDAL turns into a
    0.0099999997° step (and a crop of it, whose last coordinate the GRIB
    writer rounds again, into 0.0099999937°). A grid whose cells, at the
    nearest whole thousandth of a degree or, failing that, the nearest
    whole arc-minute (EFAS's 1/60°), span its extent to within a thousandth
    of a cell is on that step, on both axes independently, and its first
    center is placed on the half-step grid (rounding half up) when it lies
    within the same tolerance of it — a grid whose edges are on whole steps
    has its centers half a step in. A step that is near neither — the sflux
    Gaussian grid, the radar mosaic's power-of-two tiles — passes through
    unchanged, as does a global grid, which :func:`snap_global_longitudes`
    has already described. The native encoder applies the identical rule
    (``grid.rs``): the two must agree to the bit."""
    if grid.wraps:
        return grid
    longitude_step, first_longitude = snap_axis(grid.longitude_step, grid.first_longitude, grid.width)
    latitude_step, first_latitude = snap_axis(grid.latitude_step, grid.first_latitude, grid.height)
    return replace(
        grid,
        longitude_step=longitude_step,
        first_longitude=first_longitude,
        latitude_step=latitude_step,
        first_latitude=first_latitude,
    )


def snap_axis(step: float, first: float, count: int) -> tuple[float, float]:
    """One axis of :func:`snap_regional_steps`: ``(step, first)`` snapped,
    or as given. The snapped values are whole units divided out at the end
    — ``30 / 1000`` is the double nearest 0.03 where ``30 * 0.001`` is not
    — so a bundle says ``0.03``, not ``0.030000000000000002``."""
    for unit, divisor in _REGIONAL_STEP_UNITS:
        units = math.floor(step / unit + 0.5)
        rounded = units / divisor
        if units == 0 or abs(count * step - count * rounded) > abs(step) * _GLOBAL_SPAN_TOLERANCE_CELLS:
            continue
        # The first *center* sits on the half-step grid: a grid whose edges
        # are on whole steps (MRMS: 130.00 W) has its centers half a step in.
        half_steps = math.floor(first / (rounded / 2.0) + 0.5)
        snapped_first = (half_steps * units) / (2.0 * divisor)
        if abs(snapped_first - first) > abs(rounded) * _GLOBAL_SPAN_TOLERANCE_CELLS:
            snapped_first = first
        return rounded, snapped_first
    return step, first


def normalize_longitudes(grid: GridInfo) -> GridInfo:
    """Roll grids that start at Greenwich to the -180-first layout.

    GDAL's GRIB driver rotates global regular lat/lon grids to start at -180
    but leaves the sflux Gaussian grid starting at longitude 0; downstream
    (shaders, posters, particles) assumes one layout, so the columns whose
    centers lie at or past 180 degrees are moved to the front of every
    extracted plane (``GridInfo.column_roll``)."""
    # A wrapping grid GDAL already rotated starts within half a wrap of -180;
    # the sflux grid's first cell center computes to exactly 0.0, so the test
    # must be "starts near -180", not "non-positive".
    if not grid.wraps or grid.first_longitude < -90.0:
        return grid
    pivot = math.ceil((180.0 - grid.first_longitude) / grid.longitude_step - 1e-9)
    roll = grid.width - pivot
    if roll <= 0 or roll >= grid.width:
        return grid
    return replace(
        grid,
        first_longitude=grid.first_longitude + pivot * grid.longitude_step - 360.0,
        column_roll=roll,
    )


def crop_grid(grid: GridInfo, bbox: tuple[float, float, float, float]) -> GridInfo:
    """Restrict a grid to the smallest whole-cell window covering ``bbox``.

    ``bbox`` is ``(west, south, east, north)`` in degrees. The window keeps
    the cell whose center sits at or before each lower edge and the one at or
    after each upper edge, so the cropped planes always cover the requested
    box outright. On a wrapping source grid ``west > east`` crosses the
    antimeridian; the returned origin is renormalized into [-180, 180) and the
    window, not the metadata, carries the wrap.
    """
    if grid.crop is not None:
        raise ConversionError("a grid can only be cropped once")
    west, south, east, north = (float(value) for value in bbox)
    if not -90.0 <= south < north <= 90.0:
        raise ConversionError(f"bbox latitudes must satisfy -90 <= south < north <= 90: {bbox}")
    if not (-360.0 <= west <= 360.0 and -360.0 <= east <= 360.0):
        raise ConversionError(f"bbox longitudes must be within [-360, 360]: {bbox}")
    if east < west and not grid.wraps:
        raise ConversionError("only a wrapping grid can be cropped across the antimeridian")
    span = (east - west) % 360.0
    if span == 0.0:
        if east < west or not grid.wraps:
            raise ConversionError(f"bbox must have a positive longitude span: {bbox}")
        span = 360.0

    # Degrees east of the grid's origin. A wrapping grid has every longitude
    # on some column. A regional grid is taken in the copy of the world it
    # lies in: a satellite disk over the Pacific runs from 80.7 to 200.7,
    # and a box spelled -170 is at 190 on it, while a box a few degrees west
    # of the origin is west of it, not most of the way around the world.
    offset = (west - grid.first_longitude) % 360.0
    if not grid.wraps and offset > (grid.width - 1) * grid.longitude_step + 1e-9:
        offset -= 360.0
    column_start = math.floor(offset / grid.longitude_step + 1e-9)
    column_end = math.ceil((offset + span) / grid.longitude_step - 1e-9)
    if grid.wraps:
        width = min(column_end - column_start + 1, grid.width)
        if width >= grid.width:
            column_start, width = 0, grid.width
        column_start %= grid.width
    else:
        column_start = max(column_start, 0)
        column_end = min(column_end, grid.width - 1)
        width = column_end - column_start + 1
    if width <= 0:
        raise ConversionError(f"bbox does not overlap the source grid: {bbox}")

    row_start = max(math.floor((north - grid.first_latitude) / grid.latitude_step + 1e-9), 0)
    row_end = min(math.ceil((south - grid.first_latitude) / grid.latitude_step - 1e-9), grid.height - 1)
    height = row_end - row_start + 1
    if height <= 0:
        raise ConversionError(f"bbox does not overlap the source grid: {bbox}")

    first_longitude = grid.first_longitude + column_start * grid.longitude_step
    if width < grid.width:
        first_longitude = (first_longitude + 180.0) % 360.0 - 180.0
    return GridInfo(
        width=width,
        height=height,
        first_longitude=round(first_longitude, 10),
        first_latitude=round(grid.first_latitude + row_start * grid.latitude_step, 10),
        longitude_step=grid.longitude_step,
        latitude_step=grid.latitude_step,
        column_roll=grid.column_roll,
        crop=CropWindow(
            source_width=grid.width,
            source_height=grid.height,
            row_start=row_start,
            column_start=column_start,
            width=width,
            height=height,
        ),
        resample=grid.resample,
        downsample=grid.downsample,
    )

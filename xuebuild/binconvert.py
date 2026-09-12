"""Build Xue v1 bundles from gridded input.

Each variable is packaged into its own single-variable ``.xue`` file so the
frontend can download exactly the fields it needs. Extraction runs one
``gdalinfo`` and one multi-band ``gdal_translate`` per input file, in parallel
across files.

A forecast source arrives as one GRIB2 file per forecast hour; an observation
source (:mod:`xue.observation`) as one NetCDF file whose bands are the time
axis. Everything past frame discovery — crop, quantize, temporal grouping,
container write, manifest — is the same for both.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import zlib
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import binformat, grib2, temporal, zstdcli
from .errors import ConversionError, DownloadError
from .gdal import (
    discover_inputs,
    dataset_info,
    inspect_grib,
    inspect_grib_multi,
    normalize_unit,
    raster_expression,
    require_command,
    run_command,
)
from .manifest import (
    REQUIRED_BIN_BUNDLE_VARIABLES,
    build_bin_manifest,
    build_latest_pointer,
    iso_z,
    write_bin_manifest,
    write_latest_pointer,
)
from .model import GRIB_PLANE_SOURCE, PlaneSource, SourceFrame
from .observation import inspect_observation
from .quantize import PRESSURE_VARIABLE_IDS, PROFILES, PrecipitationCodebook, TemperatureCodebook
from .sources import SourceSpec, source_spec
from .variables import (
    ISOBARIC_LEVELS_HPA,
    STANDARD_GRAVITY,
    SURFACE_TEMPERATURE_IDS,
    WAVE_VECTOR_COMPONENT_IDS,
    isobaric_variable,
    variable_spec,
)
from .videoconvert import build_debug_playlist, encode_variable_video

LOG = logging.getLogger(__name__)

METADATA_SCHEMA_VERSION = 3
"""Bundle metadata schema this encoder writes: every variable descriptor
carries its GRIB2 parameter identity (docs/format.md). Earlier versions
remain readable; nothing new is written at them."""

# Scalar variables ship one single-variable bundle each (with a poster, and
# for the surface fields a video companion); which scalars a source publishes
# is the source's business (sources.py bundle_scalar_ids — sflux adds dswrf).
# A vector field ships its two components together in one two-variable bundle
# for the frontend's magnitude shader and particle layer: the 10 m wind, the
# wind on each isobaric surface, the water vapour flux the converter derives
# on each surface from the specific humidity and the wind there, and the
# wave vector it derives from the significant wave height and the primary
# wave direction.
WIND_COMPONENT_IDS = ("ugrd10m", "vgrd10m")
WIND_BUNDLE_ID = "wind10m"
WAVE_BUNDLE_ID = "wave"
VECTOR_BUNDLES: dict[str, tuple[str, str]] = {
    WIND_BUNDLE_ID: WIND_COMPONENT_IDS,
    **{f"wind{level}": (f"ugrd{level}", f"vgrd{level}") for level in ISOBARIC_LEVELS_HPA},
    **{f"qflux{level}": (f"uqflx{level}", f"vqflx{level}") for level in ISOBARIC_LEVELS_HPA},
    WAVE_BUNDLE_ID: WAVE_VECTOR_COMPONENT_IDS,
}

# The vector bundles the converter derives rather than reads, and what from:
# a vapour flux pair from the specific humidity and both wind components on
# its surface, the wave vector from the wave height and direction. A wind
# pair is its own two components and is not here.
DERIVED_VECTORS: dict[str, tuple[str, ...]] = {
    **{f"qflux{level}": (f"spfh{level}", f"ugrd{level}", f"vgrd{level}") for level in ISOBARIC_LEVELS_HPA},
    WAVE_BUNDLE_ID: ("htsgw", "dirpw"),
}


# Scalar bundles the converter derives rather than reads: the equivalent
# potential temperature on each isobaric surface, from the temperature and
# the specific humidity there. Like a vapour flux bundle, listing one in a
# source's scalars publishes it only when every input is fetched, and an
# input that serves only the derivation is released once it is done.
DERIVED_SCALARS: dict[str, tuple[str, ...]] = {
    f"thetae{level}": (f"tmp{level}", f"spfh{level}") for level in ISOBARIC_LEVELS_HPA
}


def theta_e_level(bundle_id: str) -> int | None:
    """The isobaric surface of a ``thetae<level>`` bundle, or None."""
    if bundle_id in DERIVED_SCALARS:
        return int(bundle_id[len("thetae") :])
    return None


def vapour_flux_level(bundle_id: str) -> int | None:
    """The isobaric surface of a ``qflux<level>`` bundle, or None."""
    if bundle_id.startswith("qflux") and bundle_id in VECTOR_BUNDLES:
        return int(bundle_id[len("qflux") :])
    return None


def vector_input_ids(bundle_id: str) -> tuple[str, ...]:
    """The source inputs one vector bundle is built from: a wind pair is its
    own two components; a vapour flux pair is derived from the specific
    humidity and both wind components on the same surface; the wave vector
    from the significant wave height and the primary wave direction."""
    return DERIVED_VECTORS.get(bundle_id, VECTOR_BUNDLES[bundle_id])


# The scalars that also get an H.264 companion: the surface fields the video
# path was built for. It is an opt-in path (`?use_h264=true`), so the
# upper-air fills ship bundles and posters only rather than cost the
# scheduled build an ffmpeg pass per level.
VIDEO_VARIABLE_IDS = frozenset({"tmp2m", "prate", "dswrf", "cref"})
# Precipitation and radar reflectivity move with weather systems, so temporal
# differencing makes them larger, not smaller: their chunks stack the codes
# RAW. Every linear-codebook field is smooth enough to chain against the
# previous frame inside its chunk.
RAW_VARIABLE_IDS = {"prate", "cref"}
# The pressure family (sea level pressure, pressure-level geopotential
# heights) ships bundles only: the frontend draws it as contour lines, which
# needs the exact codes and never the H.264 companion's chroma-subsampled
# approximation, and a poster would paint a filled field the view does not
# show. Everything else about them is an ordinary linear scalar bundle.
PRESSURE_BUNDLE_IDS = frozenset(PRESSURE_VARIABLE_IDS)

# gdal_translate decodes GRIB packing on the CPU: one worker per core.
_EXTRACT_WORKERS = min(16, os.cpu_count() or 4)
# gdalinfo inspection is a ~1 s subprocess per file; oversubscribe mildly.
_INSPECT_WORKERS = min(32, 2 * (os.cpu_count() or 4))
# Bundles compressing/verifying concurrently; the shared zstd pool bounds the
# real CPU load, this only caps how many bundles' raw payloads are alive.
_BUNDLE_WRITERS = 4


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
    the source dimensions gdal_translate actually extracts. Never serialized:
    the cropped origin and extent already say where the data is."""

    @property
    def wraps(self) -> bool:
        return abs(self.width * self.longitude_step - 360.0) < 1e-6

    @property
    def source_shape(self) -> tuple[int, int]:
        """(height, width) of the plane gdal_translate extracts."""
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


@dataclass
class PlaneStats:
    variable_id: str
    lead_seconds: int
    max_abs_error: float
    clamped_points: int
    overflow_points: int


# Every source names its precipitation input differently (GFS prate, ECMWF
# tp, sflux prate_ave), so a bundle's inputs are resolved off the source's own
# input list rather than hard-coded per model.
PRECIPITATION_INPUT_IDS = ("prate", "tp", "prate_ave")


def bundle_input_ids(source: SourceSpec, bundle_id: str) -> tuple[str, ...]:
    """The source input variables one published bundle is built from."""
    if bundle_id in VECTOR_BUNDLES:
        return vector_input_ids(bundle_id)
    if bundle_id in DERIVED_SCALARS:
        return DERIVED_SCALARS[bundle_id]
    if bundle_id == "prate":
        return (next(vid for vid in source.input_variable_ids if vid in PRECIPITATION_INPUT_IDS),)
    return (bundle_id,)


def series_lead_seconds(frames: dict[str, SourceFrame]) -> int:
    """The lead time one file's frames all share, in seconds from the run."""
    return next(iter(frames.values())).lead_seconds


def published_bundle_ids(source: SourceSpec) -> tuple[str, ...]:
    """Every bundle a source can publish, in manifest order: its scalars,
    then each listed vector bundle whose inputs the source fetches. A
    derived scalar counts the same way — listed, it ships only when its
    inputs are."""
    scalars = tuple(
        bundle_id
        for bundle_id in source.bundle_scalar_ids
        if all(variable_id in source.input_variable_ids for variable_id in bundle_input_ids(source, bundle_id))
    )
    vectors = tuple(
        bundle_id
        for bundle_id in source.bundle_vector_ids
        if all(variable_id in source.input_variable_ids for variable_id in vector_input_ids(bundle_id))
    )
    return scalars + vectors


def _grid_info(path: Path) -> GridInfo:
    info = dataset_info(path, description=f"inspect grid of {path}")
    width, height = (int(value) for value in info["size"])
    transform = info["geoTransform"]
    if transform[2] or transform[4]:
        raise ConversionError(f"rotated grids are unsupported: {path}")
    lon_step, lat_step = float(transform[1]), float(transform[5])
    if lon_step <= 0 or lat_step >= 0:
        raise ConversionError(f"grid must run west-to-east and north-to-south: {path}")
    return _normalize_longitudes(
        _snap_global_longitudes(
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


# How far short of (or past) a full circle a grid's columns may fall, as a
# fraction of one cell, and still be the global grid they clearly are.
_GLOBAL_SPAN_TOLERANCE_CELLS = 1e-3


def _snap_global_longitudes(grid: GridInfo) -> GridInfo:
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


def _normalize_longitudes(grid: GridInfo) -> GridInfo:
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

    offset = (west - grid.first_longitude) % 360.0 if grid.wraps else west - grid.first_longitude
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
    )


def _convert_units(frame: SourceFrame, values: np.ndarray) -> np.ndarray:
    isobaric = isobaric_variable(frame.variable_id)
    if frame.variable_id in SURFACE_TEMPERATURE_IDS or (isobaric is not None and isobaric[0] == "tmp"):
        unit = normalize_unit(frame.unit)
        if unit == "K":
            values -= 273.15
        elif unit == "F":
            values = (values - 32.0) * 5.0 / 9.0
    elif frame.variable_id == "prate":
        values *= 3600.0
    elif frame.variable_id == "tp":
        # ECMWF run-total precipitation accumulation, metres -> mm; the rate
        # derivation (de-accumulation) happens later against the previous frame.
        values *= 1000.0
    elif frame.variable_id == "prmsl":
        # GRIB2 carries mean sea level pressure in pascals; the codebook
        # quantizes hectopascals.
        values /= 100.0
    elif isobaric is not None and isobaric[0] == "spfh":
        # GRIB2 carries specific humidity as a mass ratio (kg/kg); the
        # codebook quantizes g/kg.
        values *= 1000.0
    elif frame.variable_id == "vis":
        # GRIB2 carries visibility in metres; the codebook quantizes km.
        values /= 1000.0
    elif frame.variable_id == "icec":
        # GRIB2 carries sea ice cover as a 0–1 proportion; the codebook
        # quantizes percent.
        values *= 100.0
    elif frame.variable_id == "dirpw":
        # A direction in degrees true: a record can carry 360, which is the
        # codebook's 0 — reduce it there so the wrap never clamps.
        values = np.mod(values, 360.0)
    # Wind components, geopotential heights, relative humidity, cloud cover,
    # CAPE, vertical velocity, ice thickness and the wave height and period
    # are already in their output units.
    return values


def derive_vapour_flux(
    values: dict[str, np.ndarray], bundle_id: str
) -> tuple[np.ndarray, np.ndarray]:
    """The water vapour flux components on one isobaric surface, q·V/g in
    g·cm⁻¹·hPa⁻¹·s⁻¹, from the specific humidity already in g/kg and the wind
    in m/s there. One multiplication then one division per component, in this
    order, in float64: the native encoder does exactly the same, and the two
    are held byte-identical."""
    specific_humidity_id, u_id, v_id = vector_input_ids(bundle_id)
    q = values[specific_humidity_id]
    return q * values[u_id] / STANDARD_GRAVITY, q * values[v_id] / STANDARD_GRAVITY


def derive_wave_vector(values: dict[str, np.ndarray], bundle_id: str) -> tuple[np.ndarray, np.ndarray]:
    """The wave vector: the significant wave height, already in metres, laid
    along the direction the waves travel, as an eastward and a northward
    component. The primary direction is degrees true the waves come *from*
    (the meteorological convention the wind uses), so the components are
    the wind's ``(-h sin θ, -h cos θ)`` and a reader's ``atan2(-u, -v)``
    gives the direction back. Land is 0 m from 0°, so (0, 0). The
    operations run in this order, in float64 — degrees to radians by one
    multiplication, sine and cosine, the negated height times each — and
    the native encoder repeats them, which is what keeps the two
    byte-identical on a field neither reads from a record."""
    height_id, direction_id = vector_input_ids(bundle_id)
    radians = values[direction_id] * (math.pi / 180.0)
    height = -values[height_id]
    return height * np.sin(radians), height * np.cos(radians)


def derive_vector(values: dict[str, np.ndarray], bundle_id: str) -> tuple[np.ndarray, np.ndarray]:
    """The two components of one derived vector bundle, from the planes of
    its inputs."""
    if bundle_id == WAVE_BUNDLE_ID:
        return derive_wave_vector(values, bundle_id)
    return derive_vapour_flux(values, bundle_id)


# The smallest specific humidity the derivation sees, in kg/kg: a dry
# stratospheric cell can carry zero, whose vapour pressure has no logarithm.
_THETA_E_MINIMUM_Q = 1e-7


def derive_theta_e(values: dict[str, np.ndarray], bundle_id: str) -> np.ndarray:
    """The equivalent potential temperature on one isobaric surface, in K,
    from the temperature already in °C and the specific humidity already in
    g/kg there: Bolton (1980) eq. 43 with its own lifting-condensation-level
    temperature (eq. 15) and the dew point inverted from its eq. 10. The
    operations below run in this exact order, in float64, and the native
    encoder reproduces them one by one — that is what keeps the two
    byte-identical on a field neither reads from a record."""
    temperature_id, humidity_id = DERIVED_SCALARS[bundle_id]
    p = float(theta_e_level(bundle_id))
    t = values[temperature_id] + 273.15
    q = np.maximum(values[humidity_id] / 1000.0, _THETA_E_MINIMUM_Q)
    r = q / (1.0 - q)
    e = p * r / (0.622 + r)
    ln_e = np.log(e / 6.112)
    dew_point = 243.5 * ln_e / (17.67 - ln_e) + 273.15
    t_lcl = 1.0 / (1.0 / (dew_point - 56.0) + np.log(t / dew_point) / 800.0) + 56.0
    theta = t * (1000.0 / p) ** (0.2854 * (1.0 - 0.28 * r))
    r_g = r * 1000.0
    return theta * np.exp((3.376 / t_lcl - 0.00254) * r_g * (1.0 + 0.00081 * r_g))


def deaccumulate_precipitation(
    current_mm: np.ndarray,
    previous_mm: np.ndarray | None,
    step_hours: int,
) -> np.ndarray:
    """Mean precipitation rate (mm/h) over the step ending at the current
    frame, from run-total accumulations in mm. The first frame has no
    preceding interval, so its rate is zero; packing noise can make the
    accumulation dip slightly, so negative differences clamp to zero."""
    if previous_mm is None:
        return np.zeros_like(current_mm)
    return np.maximum(current_mm - previous_mm, 0.0) / step_hours


def average_window_start(hour: int, window_hours: int) -> int:
    """First hour of the averaging window whose interval ends at ``hour``.

    GFS interval averages reset every ``window_hours``: f001–f006 average
    from hour 0, f007–f012 from hour 6, and so on."""
    if hour <= 0:
        raise ConversionError("averaged precipitation has no analysis frame")
    return window_hours * ((hour - 1) // window_hours)


def deaverage_precipitation(
    current_average: np.ndarray,
    hour: int,
    previous_average: np.ndarray | None,
    previous_hour: int | None,
    window_hours: int,
) -> np.ndarray:
    """Mean rate (mm/h) over the step ending at ``hour``, from GFS
    window-cumulative average rates (kg/m^2 s, sflux ``PRATE ave``).

    Each frame is scaled to mm accumulated since its window start and
    differenced against the previous frame of the same window; the window's
    first frame (no ``previous_average``) differences against zero. The rate
    divisor is the interval the difference spans, so a mixed-step axis (three
    hours between frames past f120) needs no external step argument."""
    window_start = average_window_start(hour, window_hours)
    accumulated_mm = current_average * 3600.0 * (hour - window_start)
    previous_mm = np.zeros_like(accumulated_mm)
    interval_start = window_start
    if previous_average is not None and previous_hour is not None:
        if not window_start < previous_hour < hour:
            raise ConversionError("previous averaged frame is outside the current averaging window")
        previous_mm = previous_average * 3600.0 * (previous_hour - window_start)
        interval_start = previous_hour
    return deaccumulate_precipitation(accumulated_mm, previous_mm, hour - interval_start)


def _extract_plane(frame: SourceFrame, grid: GridInfo, work: Path) -> np.ndarray:
    """Extract one GRIB band as a float64 plane in physical units. The
    converter itself extracts whole files via :func:`_extract_planes`; this
    single-band form is kept for the bench scripts."""
    return _extract_planes({frame.variable_id: frame}, grid, work)[frame.variable_id]


def _extract_planes(
    frames: dict[str, SourceFrame],
    grid: GridInfo,
    work: Path,
    plane_source: PlaneSource = GRIB_PLANE_SOURCE,
) -> dict[str, np.ndarray]:
    """Extract every requested band of one file in a single gdal_translate."""
    order = list(frames)
    source = frames[order[0]].path
    hour = frames[order[0]].lead_seconds
    # Named by the band set's hash rather than the ids joined: a GFS frame
    # now carries over thirty of them, past a filesystem's 255-byte name.
    band_set = zlib.crc32("-".join(order).encode("ascii")) & 0xFFFFFFFF
    raw = work / f"planes.f{hour:03d}.{os.getpid()}.{band_set:08x}.bin"
    command = [require_command("gdal_translate"), "-q"]
    if plane_source.unscale:
        command.append("-unscale")
    for variable_id in order:
        command += ["-b", str(frames[variable_id].band)]
    command += ["-of", "ENVI", "-ot", "Float64", "-co", "INTERLEAVE=BSQ", str(source), str(raw)]
    run_command(command, description=f"extract {', '.join(order)} f{hour:03d}")
    values = np.fromfile(raw, dtype="<f8")
    source_height, source_width = grid.source_shape
    plane_size = source_width * source_height
    if values.size != plane_size * len(order):
        raise ConversionError(f"extracted plane size mismatch for {source}")
    planes: dict[str, np.ndarray] = {}
    for index, variable_id in enumerate(order):
        plane = values[index * plane_size : (index + 1) * plane_size].copy().reshape(source_height, source_width)
        if grid.column_roll:
            plane = np.roll(plane, grid.column_roll, axis=1)
        if grid.crop is not None:
            plane = np.ascontiguousarray(grid.crop.take(plane))
        plane = plane_source.apply_fill(plane.ravel())
        plane = _fill_missing(variable_id, plane)
        if not np.isfinite(plane).all():
            raise ConversionError(f"Xue v1 requires complete planes, found non-finite values in {source}")
        planes[variable_id] = _convert_units(frames[variable_id], plane)
    return planes


def _fill_missing(variable_id: str, plane: np.ndarray) -> np.ndarray:
    """Map the points a record does not cover to the bottom of the
    variable's codebook — a value, not a gap (docs/format.md). Which values
    mark them is the variable's own to declare (``VariableSpec.fill_values``:
    GDAL's 9999 for a GRIB2 bitmap); a variable that covers its grid declares
    none and passes through untouched. Runs before unit conversion, on the
    raw record values."""
    spec = variable_spec(variable_id)
    if not spec.fill_values:
        return plane
    return PlaneSource(fill_values=spec.fill_values, fill_replacement=float(spec.value_range[0])).apply_fill(plane)


def _prepare_frames(paths: list[Path], variable_id: str) -> list[SourceFrame]:
    frames = sorted((inspect_grib(path, variable_id) for path in paths), key=lambda frame: frame.lead_seconds)
    _check_frames(frames, variable_id)
    return frames


def _check_frames(frames: list[SourceFrame], variable_id: str) -> None:
    leads = [frame.lead_seconds for frame in frames]
    if len(set(leads)) != len(leads):
        raise ConversionError(f"duplicate lead times for {variable_id}")
    if len({frame.run_time for frame in frames}) != 1:
        raise ConversionError(f"input files contain multiple GFS run times for {variable_id}")


def _check_reference_frames(
    fast_frames: dict[str, SourceFrame],
    reference_frames: dict[str, SourceFrame],
    variable_ids: tuple[str, ...],
) -> None:
    """Raise if the GRIB2 header index disagrees with gdalinfo on the
    per-run reference file. The header index locates the bands every
    gdal_translate extraction reads, so a mismatch must never pass silently;
    the raise drops the whole run into the gdalinfo fallback path."""
    for variable_id in variable_ids:
        fast = fast_frames.get(variable_id)
        reference = reference_frames.get(variable_id)
        if (fast is None) or (reference is None):
            if fast is not reference:
                raise ConversionError(
                    f"GRIB2 header index and gdalinfo disagree on the presence of {variable_id}"
                )
            continue
        if (
            fast.band != reference.band
            or fast.run_time != reference.run_time
            or fast.valid_time != reference.valid_time
            or fast.lead_seconds != reference.lead_seconds
            or raster_expression(variable_id, fast.unit) != raster_expression(variable_id, reference.unit)
        ):
            raise ConversionError(
                f"GRIB2 header index disagrees with gdalinfo for {variable_id} in {fast.path}: "
                f"{fast} != {reference}"
            )


def _prepare_frames_all(
    paths: list[Path],
    variable_ids: tuple[str, ...],
    optional_at_analysis: tuple[str, ...] = (),
    reference_frames: dict[str, SourceFrame] | None = None,
) -> list[dict[str, SourceFrame]]:
    """Inspect every file once for all variables, in parallel across files.

    Variables in ``optional_at_analysis`` may be absent from the f000 file
    only (sflux carries no PRATE record at analysis time). Inspection uses
    the GRIB2 header index; the first file is cross-checked against
    ``reference_frames`` (a real gdalinfo pass) and a run whose files the
    header index cannot parse falls back to gdalinfo inspection."""
    with ThreadPoolExecutor(max_workers=_INSPECT_WORKERS) as executor:
        try:
            per_file = list(
                executor.map(
                    lambda path: grib2.inspect_grib_fast(path, variable_ids, optional_ids=optional_at_analysis),
                    paths,
                )
            )
            if reference_frames is not None:
                _check_reference_frames(per_file[0], reference_frames, variable_ids)
        except ConversionError as exc:
            LOG.warning("GRIB2 header index unavailable (%s); falling back to gdalinfo inspection", exc)
            per_file = list(
                executor.map(
                    lambda path: inspect_grib_multi(path, variable_ids, optional_ids=optional_at_analysis), paths
                )
            )
    for frames in per_file:
        leads = {frame.lead_seconds for frame in frames.values()}
        if len(leads) != 1:
            raise ConversionError(f"variables disagree on the lead time in {frames[variable_ids[0]].path}")
    per_file.sort(key=lambda frames: frames[variable_ids[0]].lead_seconds)
    for variable_id in variable_ids:
        for frames in per_file:
            if variable_id in frames:
                continue
            lead = frames[variable_ids[0]].lead_seconds
            if variable_id not in optional_at_analysis or lead != 0:
                raise ConversionError(
                    f"missing {variable_id} record at forecast hour {lead // binformat.HOUR_SECONDS}"
                )
        _check_frames([frames[variable_id] for frames in per_file if variable_id in frames], variable_id)
    return per_file


def _linear_stats(values: np.ndarray, codes: np.ndarray, codebook: TemperatureCodebook) -> tuple[float, int]:
    """Quantization error and clamp count for any linear-codebook plane."""
    decoded = codebook.decode(codes)
    in_range = (values >= codebook.minimum) & (values <= codebook.maximum)
    clamped = int(np.count_nonzero(~in_range))
    max_error = float(np.abs(decoded[in_range] - values[in_range]).max()) if in_range.any() else 0.0
    return max_error, clamped


def axis_unit_seconds(lead_seconds: Sequence[int]) -> int:
    """The coarsest unit that expresses every frame's lead time exactly.

    An hour for every forecast source, and whatever the file carries for an
    observation series — six minutes for the radar mosaic. Capping at an hour
    keeps a whole-hour axis indexed by its forecast hours, exactly as before
    (docs/format.md)."""
    return math.gcd(binformat.HOUR_SECONDS, *lead_seconds)


def lead_hours(offset: int, unit_seconds: int) -> int:
    """``offset`` as a whole number of hours, rounded up — how far a run
    reaches, for the manifest's coarse ``forecastHours``."""
    return -(-offset * unit_seconds // binformat.HOUR_SECONDS)


def _time_metadata(offsets: list[int], unit_seconds: int) -> dict[str, Any]:
    """The metadata ``time`` block: offsets on a declared unit, uniform ones
    declaring a ``frameStep`` and the rest listing their offsets outright
    (docs/format.md). A forecast run lists them because its source changes
    cadence partway; an observation series lists them wherever a publication
    was missed."""
    steps = {after - before for before, after in zip(offsets, offsets[1:])}
    block: dict[str, Any] = {
        "unitSeconds": unit_seconds,
        "firstFrameOffset": offsets[0],
        "frameCount": len(offsets),
    }
    if len(steps) <= 1:
        block["frameStep"] = steps.pop() if steps else 1
    else:
        block["frameOffsets"] = list(offsets)
    return block


def _variable_metadata(variable_id: str, numeric_id: int, source: SourceSpec, profile: str) -> dict[str, Any]:
    """One schema v3 variable descriptor: what the field is (GRIB2 parameter
    and fixed surface), what its values mean, and how they are quantized.

    ``numeric_id`` is the file-local ``variableId`` handle that ties this
    descriptor to the index (docs/format.md): the variable's 1-based position
    in the bundle's variable list, assigned by :func:`build_metadata`."""
    spec = variable_spec(variable_id)
    parameter = spec.parameter_metadata()
    if variable_id == "prate" and (source.accumulated_precipitation or source.averaged_precipitation):
        # The published rate is the mean over the step, derived from the
        # source's run-total accumulation (ECMWF) or window average (sflux) —
        # a statistic over the interval, not the instantaneous field GFS
        # pgrb2 carries under the same parameter.
        parameter["typeOfStatisticalProcessing"] = 0
    return {
        "numericId": numeric_id,
        "id": variable_id,
        "label": spec.label,
        "unit": spec.output_unit,
        "parameter": parameter,
        "quantization": PROFILES[profile][variable_id].metadata(),
    }


def build_metadata(
    run_time: datetime,
    offsets: list[int],
    grid: GridInfo,
    profile: str,
    variable_ids: tuple[str, ...] = ("tmp2m", "prate"),
    *,
    source: SourceSpec | None = None,
    unit_seconds: int = binformat.HOUR_SECONDS,
) -> dict[str, Any]:
    resolved = source or source_spec("gfs")
    return {
        "schemaVersion": METADATA_SCHEMA_VERSION,
        "model": resolved.manifest_model,
        "product": resolved.product,
        "runTime": iso_z(run_time),
        "profile": profile,
        "time": _time_metadata(offsets, unit_seconds),
        "grid": grid.metadata(),
        # variableId is a file-local handle: 1..n by position in the bundle's
        # variable list, which is the same list that fixes chunk order in
        # _bundle_chunks. Nothing outside one file reads these numbers — a
        # variable's identity is its GRIB2 parameter block.
        "variables": [
            _variable_metadata(variable_id, numeric_id, resolved, profile)
            for numeric_id, variable_id in enumerate(variable_ids, start=1)
        ],
    }


def encode_poster(codes: np.ndarray, grid: GridInfo) -> tuple[bytes, GridInfo]:
    """Encode one quantized plane as a small first-frame poster.

    The plane is decimated 2x in both axes (row/column 0, 2, 4, ... — for the
    721-row grid the last row still lands exactly on the south pole), rows are
    delta-filtered against the previous row (PNG "Up", uint8 wraparound), and
    the result is zlib-deflated so the browser can inflate it with the native
    ``DecompressionStream("deflate")`` — no WASM on the poster path.
    """
    plane = codes.reshape(grid.height, grid.width)[::2, ::2]
    poster_grid = grid.decimated()
    filtered = plane.copy()
    filtered[1:] -= plane[:-1]
    return zlib.compress(filtered.tobytes(), 9), poster_grid


def decode_poster(payload: bytes, width: int, height: int) -> np.ndarray:
    """Reference decoder for :func:`encode_poster`, used by tests."""
    filtered = np.frombuffer(zlib.decompress(payload), dtype=np.uint8).reshape(height, width).copy()
    return np.cumsum(filtered, axis=0, dtype=np.uint8)


def _quantize_file(
    frames: dict[str, SourceFrame],
    grid: GridInfo,
    work: Path,
    codebooks: dict[str, TemperatureCodebook | PrecipitationCodebook],
    previous_precipitation: tuple[int, Future] | None = None,
    average_window_hours: int = 6,
    own_precipitation: Future | None = None,
    plane_source: PlaneSource = GRIB_PLANE_SOURCE,
    derived_vector_ids: tuple[str, ...] = (),
    drop_ids: frozenset[str] = frozenset(),
    derived_scalar_ids: tuple[str, ...] = (),
) -> tuple[int, dict[str, np.ndarray], list[PlaneStats]]:
    """Extract and quantize every variable of one file; runs on a worker thread.

    ``derived_vector_ids`` names the vapour flux bundles and
    ``derived_scalar_ids`` the equivalent potential temperatures to derive
    from the planes just extracted, and ``drop_ids`` the inputs that served
    only such a derivation and are not themselves published — they are
    released here rather than quantized and carried through the whole run.

    Derived-precipitation sources (ECMWF run-total tp, sflux window-averaged
    prate_ave) difference against the previous file's raw plane. Instead of
    re-extracting that file, workers share planes through futures: each worker
    publishes its own converted raw plane into ``own_precipitation`` and reads
    the predecessor's from ``previous_precipitation`` (its hour and future).
    The pool runs files in submission (hour) order, so the awaited predecessor
    is always already running or done. Shared planes are never mutated.
    """
    lead = next(iter(frames.values())).lead_seconds
    # The precipitation derivations below are GRIB-only, and every GRIB record
    # is a whole hour out, so they can work in hours.
    hour = lead // binformat.HOUR_SECONDS
    try:
        values = _extract_planes(frames, grid, work, plane_source)
    except BaseException as exc:
        # Unblock the successor waiting on this worker's plane.
        if own_precipitation is not None:
            own_precipitation.set_exception(exc)
        raise
    raw_precipitation_id = next((vid for vid in ("tp", "prate_ave") if vid in values), None)
    if own_precipitation is not None:
        own_precipitation.set_result(values[raw_precipitation_id])
    previous_plane: np.ndarray | None = None
    previous_hour: int | None = None
    if previous_precipitation is not None:
        previous_hour, previous_future = previous_precipitation
        previous_plane = previous_future.result()
    if raw_precipitation_id == "tp":
        # ECMWF: replace the run-total accumulation (already mm) with the
        # mean rate over the step that ends at this frame (mm/h). The step is
        # the actual distance to the previous frame — six hours past the
        # 144-hour cadence change, three before it.
        step = hour - previous_hour if previous_hour is not None else 1
        values["prate"] = deaccumulate_precipitation(values.pop("tp"), previous_plane, step)
    elif raw_precipitation_id == "prate_ave":
        # sflux: PRATE is the window-cumulative mean rate (kg/m^2 s); derive
        # the per-step rate against the previous frame of the same averaging
        # window (the window's first frame differences against zero).
        values["prate"] = deaverage_precipitation(
            values.pop("prate_ave"),
            hour,
            previous_plane,
            previous_hour,
            average_window_hours,
        )
    for bundle_id in derived_vector_ids:
        u_id, v_id = VECTOR_BUNDLES[bundle_id]
        values[u_id], values[v_id] = derive_vector(values, bundle_id)
    for bundle_id in derived_scalar_ids:
        values[bundle_id] = derive_theta_e(values, bundle_id)
    for variable_id in drop_ids:
        values.pop(variable_id, None)
    codes: dict[str, np.ndarray] = {}
    stats: list[PlaneStats] = []
    for variable_id, plane_values in values.items():
        codebook = codebooks[variable_id]
        plane_codes = codebook.quantize(plane_values)
        if isinstance(codebook, TemperatureCodebook):
            max_error, clamped = _linear_stats(plane_values, plane_codes, codebook)
            stats.append(PlaneStats(variable_id, hour, max_error, clamped, 0))
        else:
            overflow = int(np.count_nonzero(plane_codes == codebook.overflow_code))
            stats.append(PlaneStats(variable_id, hour, 0.0, 0, overflow))
        codes[variable_id] = plane_codes
    return lead, codes, stats


def _bundle_tile(tile: tuple[int, int], grid: GridInfo, *, half: bool) -> tuple[int, int]:
    """The tile size one bundle is cut with.

    A half-resolution variant halves the source tile, so tile number n covers
    the same ground in both tiers and a viewport keeps its tile rectangle
    across a tier switch. Either size is then clamped to the grid, because the
    format requires ``1 <= tile <= grid`` so that a single-tile file states its
    grid size exactly — and a regional crop is routinely smaller than the
    source's tile (a six-degree showcase window is 24 x 24 cells against the
    0.25-degree grid's 48 x 52 tile). Clamping makes such a file one tile,
    which is the right answer: there is nothing left to subdivide.
    """
    if half:
        tile = ((tile[0] + 1) // 2, (tile[1] + 1) // 2)
    return min(tile[0], grid.width), min(tile[1], grid.height)


def _bundle_chunks(
    variable_ids: tuple[str, ...],
    offsets: list[int],
    codes: dict[int, dict[str, np.ndarray]],
    tiles: binformat.TileGeometry,
) -> tuple[
    list[binformat.VariableEntry],
    list[binformat.GroupEntry],
    list[tuple[binformat.ChunkEntry, np.ndarray]],
]:
    """The v2 index tables and uncompressed chunks of one bundle.

    Every variable of a bundle shares the file's one axis and therefore its
    temporal groups, and the chunks come back in the physical order the spec
    fixes — group, then tile row-major, then variable. That order is what
    makes the two components of the wind bundle adjacent within a tile, so a
    single range request still covers a wind frame, while a viewport's tile
    row and a cell's series each stay one narrow span.

    ``variableId`` is file-local and assigned here exactly the way
    :func:`build_metadata` assigns it — 1..n by position in ``variable_ids``
    — so the index and the metadata agree by construction, and the ascending
    id order the spec fixes is the bundle's own variable order.
    """
    numeric_ids = {variable_id: index for index, variable_id in enumerate(variable_ids, start=1)}
    predictors = {
        numeric_ids[variable_id]: (
            binformat.PREDICTOR_RAW
            if variable_id in RAW_VARIABLE_IDS
            else binformat.PREDICTOR_PREVIOUS
        )
        for variable_id in variable_ids
    }
    planes = {
        offset: {numeric_ids[variable_id]: codes[offset][variable_id] for variable_id in variable_ids}
        for offset in offsets
    }
    return temporal.build_chunks(offsets, planes, tiles, predictors)


def _decimate_codes(codes: np.ndarray, grid: GridInfo) -> np.ndarray:
    """Half-resolution copy of one quantized plane (rows/columns 0, 2, 4, ...),
    matching the poster decimation so every tier shares the same sample sites."""
    return np.ascontiguousarray(codes.reshape(grid.height, grid.width)[::2, ::2]).ravel()


def _playback_bandwidth(byte_length: int, frame_count: int, *, fps: float = 12.0) -> int:
    """HLS STREAM-INF style bandwidth hint: average bits per second needed to
    keep up with the 12 fps playback rate while downloading the whole tier."""
    return max(1, round(byte_length * 8 * fps / max(1, frame_count)))


def _write_variable_bundle(
    variable_id: str,
    output: Path,
    metadata: dict[str, Any],
    tile: tuple[int, int],
    tables: tuple[
        list[binformat.VariableEntry],
        list[binformat.GroupEntry],
        list[tuple[binformat.ChunkEntry, np.ndarray]],
    ],
    zstd_level: int,
    compressor: ThreadPoolExecutor,
) -> dict[str, Any]:
    """Compress, write, and read-back-verify one bundle. ``compressor`` is
    shared between concurrently written bundles so the total zstd load stays
    bounded by one machine-sized pool."""
    variables, groups, raw_chunks = tables
    LOG.info("compressing %d %s chunks at zstd level %d", len(raw_chunks), variable_id, zstd_level)
    compressed = list(
        compressor.map(lambda chunk: zstdcli.compress(chunk[1].tobytes(), level=zstd_level), raw_chunks)
    )
    chunks = [
        binformat.ChunkPayload(entry=replace(entry, compressed_length=len(payload)), payload=payload)
        for (entry, _stored), payload in zip(raw_chunks, compressed)
    ]
    binformat.write_bundle_v2(
        output,
        metadata,
        tile_width=tile[0],
        tile_height=tile[1],
        variables=variables,
        groups=groups,
        chunks=chunks,
    )
    LOG.info("wrote %s (%.2f MB)", output, output.stat().st_size / 1e6)

    # Read the complete file back and reconstruct every chunk before
    # publishing stats.
    bundle = binformat.read_bundle(output)
    bundle.verify_all(executor=compressor)
    LOG.info("verified %d %s chunks by full read-back decode", len(bundle.chunks), variable_id)

    bundle_bytes = output.read_bytes()
    return {
        "variable": variable_id,
        "output": str(output),
        "byteLength": len(bundle_bytes),
        "crc32": f"{zlib.crc32(bundle_bytes) & 0xFFFFFFFF:08x}",
    }


def _bundle_manifest_entry(
    bundle: dict[str, Any],
    manifest_dir: Path,
    video_report: dict[str, Any] | None,
    poster_report: dict[str, Any] | None,
    variant_reports: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    entry = {
        "variable": bundle["variable"],
        "path": Path(bundle["output"]).relative_to(manifest_dir).as_posix(),
        "byteLength": bundle["byteLength"],
        "crc32": bundle["crc32"],
    }
    if variant_reports:
        # Resolution ladder: STREAM-INF style alternate renditions of the
        # same variable; the top-level path stays the canonical full-res tier.
        entry["variants"] = [
            {
                "path": Path(variant["output"]).relative_to(manifest_dir).as_posix(),
                "width": variant["width"],
                "height": variant["height"],
                "byteLength": variant["byteLength"],
                "crc32": variant["crc32"],
                "bandwidth": variant["bandwidth"],
            }
            for variant in variant_reports
        ]
    if poster_report is not None:
        entry["poster"] = {
            "path": Path(poster_report["path"]).relative_to(manifest_dir).as_posix(),
            "width": poster_report["width"],
            "height": poster_report["height"],
            "byteLength": poster_report["byteLength"],
            "crc32": poster_report["crc32"],
            "metadataJson": poster_report["metadataJson"],
        }
    if video_report is not None:
        entry["video"] = {
            "streamPath": Path(video_report["streamPath"]).relative_to(manifest_dir).as_posix(),
            "indexPath": Path(video_report["indexPath"]).relative_to(manifest_dir).as_posix(),
            "byteLength": video_report["byteLength"],
            "crc32": video_report["crc32"],
            "codec": video_report["codec"],
            "width": video_report["width"],
            "height": video_report["height"],
            "gop": video_report["gop"],
            "frameCount": video_report["frameCount"],
            "metadataJson": video_report["metadataJson"],
        }
    return entry


def convert_bin(
    input_path: Path | Sequence[Path],
    output_dir: Path,
    *,
    profile: str = "quality",
    work_root: Path | None = None,
    zstd_level: int = zstdcli.DEFAULT_LEVEL,
    require_complete: bool = False,
    expected_hours: int = 120,
    manifest_path: Path | None = None,
    latest_path: Path | None = None,
    run_id: str | None = None,
    force: bool = False,
    skip_video: bool = False,
    skip_variants: bool = False,
    model: str = "gfs",
    bbox: tuple[float, float, float, float] | None = None,
    bundle_ids: tuple[str, ...] | None = None,
    last_hour: int | None = None,
) -> dict[str, Any]:
    """Convert a GRIB run into per-variable Xue bundles.

    Writes ``<output_dir>/<variable>.xue`` for every scalar variable plus the
    two-variable ``wind10m.xue`` bundle when the input
    files carry the 10 m wind components, per-variable posters,
    half-resolution ``.half.xue`` variants, the optional per-variable
    video artifacts and their debug playlists, and returns build
    statistics. When ``latest_path`` and ``run_id`` are given, also
    (re)writes the mutable ``latest.json`` live pointer aimed at the freshly
    written manifest.

    ``bbox`` (west, south, east, north, degrees) crops every plane to a
    region, and ``bundle_ids`` restricts which bundles are built — the two
    knobs the historical showcase cases use to ship a small slice of a past
    run. A restricted build's manifest carries only the bundles it was asked
    for, so it is not required to hold the core tmp2m and prate pair.
    ``last_hour`` trims an observation source's series to a leading window;
    a forecast run is already exactly the frames that were fetched.
    """
    if profile not in PROFILES:
        raise ConversionError(f"unknown profile: {profile}")
    source = source_spec(model)
    if bundle_ids is not None:
        unsupported = [bundle_id for bundle_id in bundle_ids if bundle_id not in published_bundle_ids(source)]
        if unsupported or not bundle_ids:
            raise ConversionError(
                f"{source.manifest_model} publishes {list(published_bundle_ids(source))}, not {list(bundle_ids)}"
            )
    zstd_version = zstdcli.zstd_version()
    codebooks = PROFILES[profile]

    if source.observation:
        # An observation source is one local file holding the whole series,
        # one band per time (xue/observation.py). There are no records to
        # match, no wind pair, and no published cadence to validate the axis
        # against — the file's own times are the axis, gaps included.
        if not isinstance(input_path, Path):
            raise ConversionError(f"a {source.manifest_model} build takes exactly one NetCDF file")
        if require_complete:
            raise ConversionError(f"{source.manifest_model} has no complete run to require")
        series = inspect_observation(input_path, source)
        # ``last_hour`` trims the series to a leading window of the file, and
        # the frame it stops on must exist — a case's declared range is never
        # silently shortened.
        per_file = series.frames
        if last_hour is not None:
            cutoff = last_hour * binformat.HOUR_SECONDS
            per_file = [frames for frames in per_file if series_lead_seconds(frames) <= cutoff]
            if not per_file or series_lead_seconds(per_file[-1]) != cutoff:
                raise ConversionError(
                    f"{input_path} has no frame exactly at hour {last_hour}; its series ends at "
                    f"hour {series_lead_seconds(series.frames[-1]) / binformat.HOUR_SECONDS:g}"
                )
        variable_ids = source.input_variable_ids
        available_vector_ids: tuple[str, ...] = ()
        available_derived_ids: tuple[str, ...] = ()
        drop_ids: frozenset[str] = frozenset()
        grid_path = series.dataset
        plane_source = series.plane_source
    else:
        paths = discover_inputs(input_path)
        # One real gdalinfo pass over the first file: it probes which vector
        # bundles can be built (their inputs are optional, so runs fetched
        # before the wind components joined the download set, and the cropped
        # test fixtures, still build cleanly) and serves as the per-run
        # cross-check reference for the GRIB2 header index used on every file.
        # A restricted build inspects only the inputs it asked for: a case that
        # ships temperature alone must not fail on a file that carries no
        # precipitation record, because it never downloaded one.
        inspect_ids = source.input_variable_ids
        if bundle_ids is not None:
            needed = {input_id for bundle_id in bundle_ids for input_id in bundle_input_ids(source, bundle_id)}
            inspect_ids = tuple(variable_id for variable_id in inspect_ids if variable_id in needed)
        requested_vector_ids = tuple(
            bundle_id
            for bundle_id in published_bundle_ids(source)
            if bundle_id in VECTOR_BUNDLES and (bundle_ids is None or bundle_id in bundle_ids)
        )
        requested_derived_ids = tuple(
            bundle_id
            for bundle_id in published_bundle_ids(source)
            if bundle_id in DERIVED_SCALARS and (bundle_ids is None or bundle_id in bundle_ids)
        )
        # An input that only feeds a derivation — a vector bundle, a derived
        # scalar — may be absent; one that is also a published scalar may not.
        derivation_only_ids = tuple(
            dict.fromkeys(
                variable_id
                for bundle_id in requested_vector_ids + requested_derived_ids
                for variable_id in bundle_input_ids(source, bundle_id)
                if variable_id not in source.bundle_scalar_ids
            )
        )
        reference_frames = inspect_grib_multi(
            paths[0],
            inspect_ids,
            optional_ids=source.optional_at_analysis + derivation_only_ids,
        )
        available_vector_ids = tuple(
            bundle_id
            for bundle_id in requested_vector_ids
            if all(variable_id in reference_frames for variable_id in vector_input_ids(bundle_id))
        )
        available_derived_ids = tuple(
            bundle_id
            for bundle_id in requested_derived_ids
            if all(variable_id in reference_frames for variable_id in DERIVED_SCALARS[bundle_id])
        )
        for bundle_id in requested_vector_ids + requested_derived_ids:
            if bundle_id not in available_vector_ids + available_derived_ids:
                LOG.warning(
                    "building without the %s bundle, %s are not all in %s",
                    bundle_id,
                    ", ".join(bundle_input_ids(source, bundle_id)),
                    paths[0],
                )

        # The variables read from the GRIB inputs: every scalar input (ECMWF
        # carries the accumulated tp instead of a rate and sflux the
        # window-averaged prate_ave, which _quantize_file de-accumulates /
        # de-averages into prate), plus the inputs of each vector bundle that
        # can be built.
        input_scalar_ids = tuple(
            variable_id for variable_id in inspect_ids if variable_id not in derivation_only_ids
        )
        vector_read_ids = tuple(
            dict.fromkeys(
                variable_id
                for bundle_id in available_vector_ids + available_derived_ids
                for variable_id in bundle_input_ids(source, bundle_id)
                if variable_id not in input_scalar_ids
            )
        )
        # The inputs that only serve a derivation (spfh850 under the vapour
        # flux and the equivalent potential temperature) are released once it
        # is done, rather than quantized and held for the whole run.
        drop_ids = frozenset(vector_read_ids) - {
            variable_id for bundle_id in available_vector_ids for variable_id in VECTOR_BUNDLES[bundle_id]
        }
        # The first variable is the run's reference: every file is keyed by its
        # forecast hour, so it must be one no file can lack. Stable-sorting the
        # analysis-optional inputs (sflux prate_ave) to the back is enough unless
        # nothing else was asked for.
        variable_ids = tuple(
            sorted(
                input_scalar_ids + vector_read_ids,
                key=lambda variable_id: variable_id in source.optional_at_analysis,
            )
        )
        if not variable_ids or variable_ids[0] in source.optional_at_analysis:
            raise ConversionError(
                f"a {source.manifest_model} build needs at least one variable present in every file, "
                f"including the analysis; {list(variable_ids)} is not enough"
            )
        per_file = _prepare_frames_all(paths, variable_ids, source.optional_at_analysis, reference_frames)
        grid_path = paths[0]
        plane_source = GRIB_PLANE_SOURCE

    # The bundle's time axis: the coarsest unit that expresses every frame
    # exactly, and each frame's offset in it. An hour for every forecast
    # source, so their offsets are their forecast hours as before.
    lead_seconds = [frames[variable_ids[0]].lead_seconds for frames in per_file]
    unit_seconds = axis_unit_seconds(lead_seconds)
    offsets = [seconds // unit_seconds for seconds in lead_seconds]
    if offsets[-1] >= binformat.NO_DEPENDENCY:
        raise ConversionError(
            f"the axis needs {offsets[-1]} steps of {unit_seconds} s, past the u16 frame offset range"
        )
    if not source.observation:
        # The input hours must be a contiguous run of the source's published
        # axis (hourly to f120, three-hourly beyond, on GFS), so no frame is
        # missing and every step matches the cadence the source publishes.
        hours = offsets
        try:
            axis = source.forecast_hours(hours[-1])
        except DownloadError as exc:
            raise ConversionError(str(exc)) from exc
        if hours != [hour for hour in axis if hour >= hours[0]]:
            raise ConversionError(f"forecast hours must be a contiguous run of the {source.manifest_model} axis")
        if require_complete:
            try:
                expected_axis = source.forecast_hours(expected_hours)
            except DownloadError as exc:
                raise ConversionError(str(exc)) from exc
            if hours != expected_axis:
                raise ConversionError(
                    f"complete build requires forecast hours 0 through {expected_hours} on the "
                    f"{source.manifest_model} axis"
                )
    run_time = per_file[0][variable_ids[0]].run_time.astimezone(UTC)

    grid = _grid_info(grid_path)
    if require_complete and (grid.width, grid.height) != source.production_grid:
        raise ConversionError(
            f"production build requires a {source.production_grid[0]}x{source.production_grid[1]} grid"
        )
    if bbox is not None:
        grid = crop_grid(grid, bbox)
        LOG.info(
            "cropped to %dx%d from %.4f,%.4f (%s)",
            grid.width,
            grid.height,
            grid.first_longitude,
            grid.first_latitude,
            ",".join(f"{value:g}" for value in bbox),
        )

    stats: list[PlaneStats] = []
    codes_by_offset: dict[int, dict[str, np.ndarray]] = {}

    work_parent = work_root or output_dir
    work_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="xue-", dir=work_parent) as temporary:
        work = Path(temporary)
        LOG.info("extracting and quantizing %d files with %d workers", len(per_file), _EXTRACT_WORKERS)
        # Precipitation derivation predecessors, shared between workers as
        # futures (see _quantize_file): ECMWF tp differences against the
        # previous frame unconditionally; sflux prate_ave only against the
        # previous frame of the same averaging window (the window's first
        # frame differences against zero).
        raw_precipitation_id = next((vid for vid in ("tp", "prate_ave") if vid in variable_ids), None)

        def sharing_plan():
            """Yield (frames, previous (hour, future) | None, own future | None)
            per file. A future is created only when the next file will
            difference against this file's raw plane; no reference is kept
            here, so each shared plane is freed once its consumer finishes."""
            previous_future: Future | None = None
            for index, frames in enumerate(per_file):
                frame = frames.get(raw_precipitation_id) if raw_precipitation_id else None
                previous: tuple[int, Future] | None = None
                if previous_future is not None and frame is not None:
                    previous = (
                        per_file[index - 1][raw_precipitation_id].lead_seconds // binformat.HOUR_SECONDS,
                        previous_future,
                    )
                own: Future | None = None
                if frame is not None and index + 1 < len(per_file):
                    successor = per_file[index + 1].get(raw_precipitation_id)
                    if successor is not None and (
                        raw_precipitation_id == "tp"
                        or average_window_start(
                            successor.lead_seconds // binformat.HOUR_SECONDS, source.average_window_hours
                        )
                        < frame.lead_seconds // binformat.HOUR_SECONDS
                    ):
                        own = Future()
                previous_future = own
                yield frames, previous, own

        derived_vector_ids = tuple(bundle_id for bundle_id in available_vector_ids if bundle_id in DERIVED_VECTORS)
        # Derived after the vapour flux and before the derivation-only inputs
        # are dropped: both read spfh850.
        derived_scalar_ids = available_derived_ids
        with ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS) as executor:
            results = executor.map(
                lambda item: _quantize_file(
                    item[0],
                    grid,
                    work,
                    codebooks,
                    item[1],
                    source.average_window_hours,
                    item[2],
                    plane_source,
                    derived_vector_ids,
                    drop_ids,
                    derived_scalar_ids,
                ),
                sharing_plan(),
            )
            for lead, codes, file_stats in results:
                codes_by_offset[lead // unit_seconds] = codes
                stats.extend(file_stats)
    LOG.info("quantized %d planes", sum(len(codes) for codes in codes_by_offset.values()))

    # The encoded (bundle) variables — the raw tp / prate_ave inputs have
    # already been derived into prate by this point, and a derived scalar
    # whose inputs the run lacked is left out like a vector bundle is.
    scalar_variable_ids = tuple(
        variable_id
        for variable_id in source.bundle_scalar_ids
        if variable_id not in DERIVED_SCALARS or variable_id in available_derived_ids
    )
    if bundle_ids is not None:
        scalar_variable_ids = tuple(
            variable_id for variable_id in scalar_variable_ids if variable_id in bundle_ids
        )
    encoded_variable_ids = scalar_variable_ids + tuple(
        variable_id for bundle_id in available_vector_ids for variable_id in VECTOR_BUNDLES[bundle_id]
    )
    # Scalars that also ship a poster — every published scalar but the
    # contour-drawn pressure family — and, among those, the surface fields
    # that also get a video companion when ffmpeg is around.
    companion_variable_ids = tuple(
        variable_id for variable_id in scalar_variable_ids if variable_id not in PRESSURE_BUNDLE_IDS
    )
    video_variable_ids = tuple(
        variable_id for variable_id in companion_variable_ids if variable_id in VIDEO_VARIABLE_IDS
    )

    # Per-variable time axes. On derived-precipitation sources (ECMWF
    # accumulations, sflux window averages) the rate has no data for the
    # analysis frame — its interval would precede the run — so the prate
    # series starts at the first real step and every prate artifact (bundle,
    # variant, poster, video) carries its own shorter axis. All other
    # variables keep the full run axis.
    variable_offsets: dict[str, list[int]] = {variable_id: offsets for variable_id in encoded_variable_ids}
    if (
        "prate" in encoded_variable_ids
        and (source.accumulated_precipitation or source.averaged_precipitation)
        and len(offsets) > 1
    ):
        variable_offsets["prate"] = offsets[1:]

    # Optional per-variable WebCodecs video artifacts.
    # Best-effort: a missing ffmpeg or an encode failure just skips that
    # variable's artifact, Xue remains the universal fallback.
    video_reports: dict[str, dict[str, Any]] = {}
    if not skip_video:
        for variable_id in video_variable_ids:
            try:
                video_artifact = encode_variable_video(
                    codes_by_offset, variable_offsets[variable_id], variable_id, width=grid.width, height=grid.height
                )
            except ConversionError as exc:
                LOG.warning("skipping %s video artifact: %s", variable_id, exc)
                continue
            output_dir.mkdir(parents=True, exist_ok=True)
            stream_path = output_dir / f"{variable_id}.h264"
            index_path = output_dir / f"{variable_id}.h264.index.json"
            stream_path.write_bytes(video_artifact.stream_bytes)
            index_path.write_text(json.dumps(video_artifact.index) + "\n", encoding="utf-8")
            playlist_path = output_dir / f"{variable_id}.h264.m3u8"
            playlist_path.write_text(
                build_debug_playlist(video_artifact.index["frames"], stream_path.name),
                encoding="utf-8",
            )
            # Same shape as the metadata embedded in the .xue, scoped to this
            # variable: the frontend needs grid/time/quantization to configure
            # the WebGL layer and palette regardless of which decode path it uses.
            video_metadata = build_metadata(run_time, variable_offsets[variable_id], grid, profile, (variable_id,), source=source, unit_seconds=unit_seconds)
            video_reports[variable_id] = {
                "variable": variable_id,
                "streamPath": str(stream_path),
                "indexPath": str(index_path),
                "byteLength": len(video_artifact.stream_bytes),
                "crc32": f"{zlib.crc32(video_artifact.stream_bytes) & 0xFFFFFFFF:08x}",
                "codec": video_artifact.codec_string,
                "width": grid.width,
                "height": grid.height,
                "gop": video_artifact.index["gop"],
                "frameCount": video_artifact.index["frameCount"],
                "metadataJson": json.dumps(video_metadata),
                "playlistPath": str(playlist_path),
            }
            LOG.info("wrote %s (%.2f MB)", stream_path, len(video_artifact.stream_bytes) / 1e6)

    # First-frame posters: one tiny artifact per variable so a variable switch
    # can paint immediately while the real stream loads.
    output_dir.mkdir(parents=True, exist_ok=True)
    poster_reports: dict[str, dict[str, Any]] = {}
    for variable_id in companion_variable_ids:
        payload, poster_grid = encode_poster(codes_by_offset[variable_offsets[variable_id][0]][variable_id], grid)
        poster_path = output_dir / f"{variable_id}.poster.bin"
        poster_path.write_bytes(payload)
        poster_reports[variable_id] = {
            "path": str(poster_path),
            "width": poster_grid.width,
            "height": poster_grid.height,
            "byteLength": len(payload),
            "crc32": f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}",
            "metadataJson": json.dumps(build_metadata(run_time, variable_offsets[variable_id], poster_grid, profile, (variable_id,), source=source, unit_seconds=unit_seconds)),
        }
        LOG.info("wrote %s (%.1f KB)", poster_path, len(payload) / 1e3)

    # Full-resolution canonical bundles (one per scalar variable plus one per
    # two-variable vector bundle) and the half-resolution ladder
    # (decimated from the already-quantized codes exactly like the posters,
    # same temporal structure; the half grid is embedded in the variant
    # bundle's own metadata, the manifest carries the tier list).
    #
    # All bundles are written concurrently: compression funnels through one
    # shared machine-sized zstd pool, while the writer pool lets one bundle's
    # serial tail (container write, read-back verify) overlap another's
    # compression. Each job materializes its raw payloads itself, so at most
    # _BUNDLE_WRITERS bundles' payloads are alive at once.
    half_grid = grid.decimated()
    # Iterate the codes actually present per hour: derived prate has no
    # analysis-frame plane on ECMWF/sflux.
    half_codes_by_offset = {
        offset: {
            variable_id: _decimate_codes(codes, grid)
            for variable_id, codes in codes_by_offset[offset].items()
        }
        for offset in offsets
    } if not skip_variants else {}

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as compressor, ThreadPoolExecutor(
        max_workers=_BUNDLE_WRITERS
    ) as writers:

        def submit_bundle(
            bundle_id: str,
            suffix: str,
            bundle_grid: GridInfo,
            codes: dict[int, dict[str, np.ndarray]],
        ) -> Future:
            bundle_variable_ids = VECTOR_BUNDLES.get(bundle_id, (bundle_id,))
            bundle_offsets = variable_offsets[bundle_variable_ids[0]]
            metadata = build_metadata(
                run_time,
                bundle_offsets,
                bundle_grid,
                profile,
                bundle_variable_ids,
                source=source,
                unit_seconds=unit_seconds,
            )

            tile = _bundle_tile(source.tile, bundle_grid, half=bool(suffix))
            tiles = binformat.TileGeometry(bundle_grid.width, bundle_grid.height, *tile)

            def job() -> dict[str, Any]:
                tables = _bundle_chunks(bundle_variable_ids, bundle_offsets, codes, tiles)
                report = _write_variable_bundle(
                    bundle_id,
                    output_dir / f"{bundle_id}{suffix}.xue",
                    metadata,
                    tile,
                    tables,
                    zstd_level,
                    compressor,
                )
                if suffix:
                    report["width"] = bundle_grid.width
                    report["height"] = bundle_grid.height
                    report["bandwidth"] = _playback_bandwidth(report["byteLength"], len(bundle_offsets))
                return report

            return writers.submit(job)

        # Submit largest first so the vector bundles' long compression starts
        # at once; reports keep the scalars-then-vectors manifest order
        # regardless.
        submit_order = available_vector_ids + scalar_variable_ids
        report_order = scalar_variable_ids + available_vector_ids
        full_futures = {bundle_id: submit_bundle(bundle_id, "", grid, codes_by_offset) for bundle_id in submit_order}
        half_futures = (
            {bundle_id: submit_bundle(bundle_id, ".half", half_grid, half_codes_by_offset) for bundle_id in submit_order}
            if not skip_variants
            else {}
        )
        bundle_reports = [full_futures[bundle_id].result() for bundle_id in report_order]
        variant_reports: dict[str, list[dict[str, Any]]] = {
            bundle_id: [half_futures[bundle_id].result()] for bundle_id in report_order if bundle_id in half_futures
        }

    report = {
        "outputDir": str(output_dir),
        "model": source.manifest_model,
        "grid": grid.metadata(),
        "profile": profile,
        "zstdLevel": zstd_level,
        "zstdVersion": ".".join(map(str, zstd_version)),
        "bundles": bundle_reports,
        "variants": [variant for variants in variant_reports.values() for variant in variants],
        "posters": list(poster_reports.values()),
        "videos": list(video_reports.values()),
        "byteLength": sum(bundle["byteLength"] for bundle in bundle_reports),
        "temperatureMaxAbsError": max((item.max_abs_error for item in stats if item.variable_id == "tmp2m"), default=0.0),
        "temperatureClampedPoints": sum(item.clamped_points for item in stats if item.variable_id == "tmp2m"),
        "precipitationOverflowPoints": sum(item.overflow_points for item in stats if item.variable_id == "prate"),
    }
    if WIND_BUNDLE_ID in available_vector_ids:
        report["windMaxAbsError"] = max(
            (item.max_abs_error for item in stats if item.variable_id in WIND_COMPONENT_IDS), default=0.0
        )
        report["windClampedPoints"] = sum(
            item.clamped_points for item in stats if item.variable_id in WIND_COMPONENT_IDS
        )
    # Quantization acceptance runs over the *encoded* variables (prate is the
    # de-accumulated output on ECMWF; the raw tp input has no codebook).
    for variable_id in encoded_variable_ids:
        codebook = codebooks[variable_id]
        if not isinstance(codebook, TemperatureCodebook):
            continue
        worst = max((item.max_abs_error for item in stats if item.variable_id == variable_id), default=0.0)
        if worst > 0.5001 * codebook.step:
            raise ConversionError(f"{variable_id} quantization error exceeds half a step")

    if manifest_path is not None:
        # The core tmp2m/prate pair is what a complete forecast run must
        # publish. A restricted build ships only the bundles it was asked
        # for, and a source that publishes neither (the radar archive) can
        # never satisfy the rule at all.
        require_core = bundle_ids is None and all(
            variable_id in published_bundle_ids(source) for variable_id in REQUIRED_BIN_BUNDLE_VARIABLES
        )
        payload = build_bin_manifest(
            run_time,
            bundles=[
                _bundle_manifest_entry(
                    bundle,
                    manifest_path.parent,
                    video_reports.get(bundle["variable"]),
                    poster_reports.get(bundle["variable"]),
                    variant_reports.get(bundle["variable"]),
                )
                for bundle in bundle_reports
            ],
            expected_hours=lead_hours(offsets[-1], unit_seconds) if len(offsets) > 1 else expected_hours,
            model=source.manifest_model,
            product=source.product,
            require_core_variables=require_core,
        )
        write_bin_manifest(
            manifest_path,
            payload,
            force=force,
            expected_hours=lead_hours(offsets[-1], unit_seconds) if len(offsets) > 1 else expected_hours,
            require_core_variables=require_core,
        )
        LOG.info("wrote manifest %s", manifest_path)
        if latest_path is not None and run_id is not None:
            manifest_bytes = manifest_path.read_bytes()
            pointer = build_latest_pointer(
                run_id,
                run_time,
                manifest_path=manifest_path.relative_to(latest_path.parent).as_posix(),
                manifest_crc32=f"{zlib.crc32(manifest_bytes) & 0xFFFFFFFF:08x}",
                model=source.manifest_model,
                product=source.product,
            )
            write_latest_pointer(latest_path, pointer)
            LOG.info("wrote live pointer %s -> run %s", latest_path, run_id)
    return report


def verify_bin(path: Path) -> dict[str, Any]:
    """Structurally validate a bundle and decode every payload."""
    bundle = binformat.read_bundle(path)
    bundle.verify_all()
    report = {
        "path": str(path),
        "byteLength": len(bundle.data),
        "containerVersion": bundle.container_version,
        "variables": sorted(bundle.variable_ids.values()),
        "frameCount": bundle.frame_count,
        "grid": f"{bundle.width}x{bundle.height}",
        "runTime": bundle.metadata.get("runTime"),
        "profile": bundle.metadata.get("profile"),
    }
    # A payload is a whole plane in v1 and a chunk — one tile of one temporal
    # group — in v2, so the count that describes a file depends on which.
    if bundle.container_version == binformat.VERSION_V2:
        report["chunks"] = len(bundle.chunks)
        report["groups"] = len(bundle.groups)
        report["tiles"] = f"{bundle.tiles.tile_width}x{bundle.tiles.tile_height}"
        report["tileCount"] = bundle.tiles.count
    else:
        report["planes"] = len(bundle.entries)
    return report

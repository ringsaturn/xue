"""Build Xue v1 bundles from GFS GRIB2 input.

Each variable is packaged into its own single-variable ``.xue`` file so the
frontend can download exactly the fields it needs. Extraction runs one
``gdalinfo`` and one multi-band ``gdal_translate`` per input file, in parallel
across files.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import binformat, grib2, temporal, zstdcli
from .errors import ConversionError
from .gdal import (
    discover_inputs,
    inspect_grib,
    inspect_grib_multi,
    normalize_unit,
    raster_expression,
    require_command,
    run_command,
)
from .manifest import build_bin_manifest, build_latest_pointer, iso_z, write_bin_manifest, write_latest_pointer
from .model import GribFrame
from .quantize import PROFILES, PrecipitationCodebook, TemperatureCodebook
from .sources import source_spec
from .videoconvert import build_debug_playlist, encode_variable_video

LOG = logging.getLogger(__name__)

VARIABLE_NUMERIC_IDS = {"tmp2m": 1, "prate": 2, "ugrd10m": 3, "vgrd10m": 4, "dswrf": 5}
VARIABLE_LABELS = {
    "tmp2m": "2 meter temperature",
    "prate": "Precipitation rate",
    "ugrd10m": "10 meter U wind component",
    "vgrd10m": "10 meter V wind component",
    "dswrf": "Downward shortwave radiation flux",
}
VARIABLE_UNITS = {"tmp2m": "°C", "prate": "mm/h", "ugrd10m": "m/s", "vgrd10m": "m/s", "dswrf": "W/m²"}

# Scalar variables ship one single-variable bundle each (with poster + video
# artifacts); which scalars a source publishes is the source's business
# (sources.py bundle_scalar_ids — sflux adds dswrf). The two wind components
# ship together in one two-variable wind10m bundle for the GPU particle
# layer.
WIND_COMPONENT_IDS = ("ugrd10m", "vgrd10m")
WIND_BUNDLE_ID = "wind10m"
# Linear-codebook fields are smooth enough for the six-frame ANCHOR groups;
# precipitation stays independent RAW planes.
GROUPED_VARIABLE_IDS = {"tmp2m", "ugrd10m", "vgrd10m", "dswrf"}

# gdal_translate decodes GRIB packing on the CPU: one worker per core.
_EXTRACT_WORKERS = min(16, os.cpu_count() or 4)
# gdalinfo inspection is a ~1 s subprocess per file; oversubscribe mildly.
_INSPECT_WORKERS = min(32, 2 * (os.cpu_count() or 4))
# Bundles compressing/verifying concurrently; the shared zstd pool bounds the
# real CPU load, this only caps how many bundles' raw payloads are alive.
_BUNDLE_WRITERS = 4


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

    @property
    def wraps(self) -> bool:
        return abs(self.width * self.longitude_step - 360.0) < 1e-6

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
    forecast_hour: int
    max_abs_error: float
    clamped_points: int
    overflow_points: int


def _grid_info(path: Path) -> GridInfo:
    result = run_command([require_command("gdalinfo"), "-json", str(path)], description=f"inspect grid of {path}")
    info = json.loads(result.stdout)
    width, height = (int(value) for value in info["size"])
    transform = info["geoTransform"]
    if transform[2] or transform[4]:
        raise ConversionError(f"rotated grids are unsupported: {path}")
    lon_step, lat_step = float(transform[1]), float(transform[5])
    if lon_step <= 0 or lat_step >= 0:
        raise ConversionError(f"grid must run west-to-east and north-to-south: {path}")
    return _normalize_longitudes(
        GridInfo(
            width=width,
            height=height,
            first_longitude=float(transform[0]) + lon_step / 2,
            first_latitude=float(transform[3]) + lat_step / 2,
            longitude_step=lon_step,
            latitude_step=lat_step,
        )
    )


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


def _convert_units(frame: GribFrame, values: np.ndarray) -> np.ndarray:
    if frame.variable_id == "tmp2m":
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
    # Wind components are already m/s.
    return values


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
    step_hours: int,
) -> np.ndarray:
    """Mean rate (mm/h) over the step ending at ``hour``, from GFS
    window-cumulative average rates (kg/m^2 s, sflux ``PRATE ave``).

    Each frame is scaled to mm accumulated since its window start and
    differenced against the previous frame of the same window; the window's
    first frame (no ``previous_average``) differences against zero."""
    window_start = average_window_start(hour, window_hours)
    accumulated_mm = current_average * 3600.0 * (hour - window_start)
    previous_mm = np.zeros_like(accumulated_mm)
    if previous_average is not None and previous_hour is not None:
        if not window_start < previous_hour <= hour:
            raise ConversionError("previous averaged frame is outside the current averaging window")
        previous_mm = previous_average * 3600.0 * (previous_hour - window_start)
    return deaccumulate_precipitation(accumulated_mm, previous_mm, step_hours)


def _extract_plane(frame: GribFrame, grid: GridInfo, work: Path) -> np.ndarray:
    """Extract one GRIB band as a float64 plane in physical units. The
    converter itself extracts whole files via :func:`_extract_planes`; this
    single-band form is kept for the bench scripts."""
    return _extract_planes({frame.variable_id: frame}, grid, work)[frame.variable_id]


def _extract_planes(frames: dict[str, GribFrame], grid: GridInfo, work: Path) -> dict[str, np.ndarray]:
    """Extract every requested band of one file in a single gdal_translate."""
    order = list(frames)
    source = frames[order[0]].path
    hour = frames[order[0]].forecast_hour
    raw = work / f"planes.f{hour:03d}.{os.getpid()}.{'-'.join(order)}.bin"
    command = [require_command("gdal_translate"), "-q"]
    for variable_id in order:
        command += ["-b", str(frames[variable_id].band)]
    command += ["-of", "ENVI", "-ot", "Float64", "-co", "INTERLEAVE=BSQ", str(source), str(raw)]
    run_command(command, description=f"extract {', '.join(order)} f{hour:03d}")
    values = np.fromfile(raw, dtype="<f8")
    plane_size = grid.width * grid.height
    if values.size != plane_size * len(order):
        raise ConversionError(f"extracted plane size mismatch for {source}")
    planes: dict[str, np.ndarray] = {}
    for index, variable_id in enumerate(order):
        plane = values[index * plane_size : (index + 1) * plane_size].copy()
        if grid.column_roll:
            plane = np.roll(plane.reshape(grid.height, grid.width), grid.column_roll, axis=1).ravel()
        if not np.isfinite(plane).all():
            raise ConversionError(f"Xue v1 requires complete planes, found non-finite values in {source}")
        planes[variable_id] = _convert_units(frames[variable_id], plane)
    return planes


def _prepare_frames(paths: list[Path], variable_id: str) -> list[GribFrame]:
    frames = sorted((inspect_grib(path, variable_id) for path in paths), key=lambda frame: frame.forecast_hour)
    _check_frames(frames, variable_id)
    return frames


def _check_frames(frames: list[GribFrame], variable_id: str) -> None:
    hours = [frame.forecast_hour for frame in frames]
    if len(set(hours)) != len(hours):
        raise ConversionError(f"duplicate forecast hours for {variable_id}")
    if len({frame.run_time for frame in frames}) != 1:
        raise ConversionError(f"input files contain multiple GFS run times for {variable_id}")


def _check_reference_frames(
    fast_frames: dict[str, GribFrame],
    reference_frames: dict[str, GribFrame],
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
            or fast.forecast_hour != reference.forecast_hour
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
    reference_frames: dict[str, GribFrame] | None = None,
) -> list[dict[str, GribFrame]]:
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
        hours = {frame.forecast_hour for frame in frames.values()}
        if len(hours) != 1:
            raise ConversionError(f"variables disagree on the forecast hour in {frames[variable_ids[0]].path}")
    per_file.sort(key=lambda frames: frames[variable_ids[0]].forecast_hour)
    for variable_id in variable_ids:
        for frames in per_file:
            if variable_id in frames:
                continue
            hour = frames[variable_ids[0]].forecast_hour
            if variable_id not in optional_at_analysis or hour != 0:
                raise ConversionError(f"missing {variable_id} record at forecast hour {hour}")
        _check_frames([frames[variable_id] for frames in per_file if variable_id in frames], variable_id)
    return per_file


def _linear_stats(values: np.ndarray, codes: np.ndarray, codebook: TemperatureCodebook) -> tuple[float, int]:
    """Quantization error and clamp count for any linear-codebook plane."""
    decoded = codebook.decode(codes)
    in_range = (values >= codebook.minimum) & (values <= codebook.maximum)
    clamped = int(np.count_nonzero(~in_range))
    max_error = float(np.abs(decoded[in_range] - values[in_range]).max()) if in_range.any() else 0.0
    return max_error, clamped


def _entry(
    variable_id: str,
    predictor: int,
    forecast_hour: int,
    dependency_hour: int,
    group_id: int,
    plane: np.ndarray,
    payload_length: int,
) -> binformat.PlaneEntry:
    return binformat.PlaneEntry(
        variable_id=VARIABLE_NUMERIC_IDS[variable_id],
        predictor=predictor,
        compression=binformat.COMPRESSION_ZSTD,
        flags=binformat.FLAG_ZSTD_CHECKSUM,
        forecast_hour=forecast_hour,
        dependency_hour=dependency_hour,
        group_id=group_id,
        compressed_length=payload_length,
        data_offset=0,
        decoded_length=plane.size,
        crc32=binformat.crc32_plane(plane),
        minimum_code=int(plane.min()),
        maximum_code=int(plane.max()),
    )


def build_metadata(
    run_time: datetime,
    hours: list[int],
    grid: GridInfo,
    profile: str,
    variable_ids: tuple[str, ...] = ("tmp2m", "prate"),
    *,
    model: str = "GFS",
    product: str = "pgrb2.0p25",
) -> dict[str, Any]:
    codebooks = PROFILES[profile]
    step = hours[1] - hours[0] if len(hours) > 1 else 1
    return {
        "schemaVersion": 1,
        "model": model,
        "product": product,
        "runTime": iso_z(run_time),
        "profile": profile,
        "time": {"firstForecastHour": hours[0], "stepHours": step, "frameCount": len(hours)},
        "grid": grid.metadata(),
        "variables": [
            {
                "numericId": VARIABLE_NUMERIC_IDS[variable_id],
                "id": variable_id,
                "label": VARIABLE_LABELS[variable_id],
                "unit": VARIABLE_UNITS[variable_id],
                "quantization": codebooks[variable_id].metadata(),
            }
            for variable_id in variable_ids
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
    frames: dict[str, GribFrame],
    grid: GridInfo,
    work: Path,
    codebooks: dict[str, TemperatureCodebook | PrecipitationCodebook],
    previous_precipitation: tuple[int, Future] | None = None,
    step_hours: int = 1,
    average_window_hours: int = 6,
    own_precipitation: Future | None = None,
) -> tuple[int, dict[str, np.ndarray], list[PlaneStats]]:
    """Extract and quantize every variable of one file; runs on a worker thread.

    Derived-precipitation sources (ECMWF run-total tp, sflux window-averaged
    prate_ave) difference against the previous file's raw plane. Instead of
    re-extracting that file, workers share planes through futures: each worker
    publishes its own converted raw plane into ``own_precipitation`` and reads
    the predecessor's from ``previous_precipitation`` (its hour and future).
    The pool runs files in submission (hour) order, so the awaited predecessor
    is always already running or done. Shared planes are never mutated.
    """
    hour = next(iter(frames.values())).forecast_hour
    try:
        values = _extract_planes(frames, grid, work)
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
        # mean rate over the step that ends at this frame (mm/h).
        values["prate"] = deaccumulate_precipitation(values.pop("tp"), previous_plane, step_hours)
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
            step_hours,
        )
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
    return hour, codes, stats


def _variable_payloads(
    variable_id: str,
    hours: list[int],
    planes_by_hour: dict[int, np.ndarray],
) -> list[tuple[binformat.PlaneEntry, bytes]]:
    """Uncompressed plane payloads for one variable at one resolution.

    Linear-codebook fields (temperature and the wind components) use six-frame
    groups with a middle RAW anchor and ANCHOR residuals; precipitation stays
    independent RAW planes with groupId mirroring forecastHour. The same
    scheme applies to every resolution tier, so full
    and half bundles stay structurally alike.
    """
    payloads: list[tuple[binformat.PlaneEntry, bytes]] = []
    if variable_id in GROUPED_VARIABLE_IDS:
        for group_id, group in enumerate(temporal.group_forecast_hours(hours)):
            anchor = temporal.anchor_hour(group)
            anchor_plane = planes_by_hour[anchor]
            for hour in [anchor, *[hour for hour in group if hour != anchor]]:
                plane = planes_by_hour[hour]
                if hour == anchor:
                    payloads.append(
                        (_entry(variable_id, binformat.PREDICTOR_RAW, hour, binformat.NO_DEPENDENCY, group_id, plane, 0), plane.tobytes())
                    )
                else:
                    residual = temporal.encode_residual(plane, anchor_plane)
                    payloads.append(
                        (_entry(variable_id, binformat.PREDICTOR_ANCHOR, hour, anchor, group_id, plane, 0), residual.tobytes())
                    )
    else:
        for hour in hours:
            plane = planes_by_hour[hour]
            payloads.append(
                (_entry(variable_id, binformat.PREDICTOR_RAW, hour, binformat.NO_DEPENDENCY, hour, plane, 0), plane.tobytes())
            )
    return payloads


def _wind_bundle_payloads(
    hours: list[int],
    planes_by_hour: dict[int, dict[str, np.ndarray]],
) -> list[tuple[binformat.PlaneEntry, bytes]]:
    """Payloads of the two-variable wind bundle, physically interleaved per
    temporal group (u group, then the same v group) so streaming a wind frame
    touches two adjacent byte spans."""
    per_component = {
        variable_id: _variable_payloads(
            variable_id, hours, {hour: planes_by_hour[hour][variable_id] for hour in hours}
        )
        for variable_id in WIND_COMPONENT_IDS
    }
    payloads: list[tuple[binformat.PlaneEntry, bytes]] = []
    cursor = 0
    for group in temporal.group_forecast_hours(hours):
        for variable_id in WIND_COMPONENT_IDS:
            payloads.extend(per_component[variable_id][cursor : cursor + len(group)])
        cursor += len(group)
    return payloads


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
    raw_payloads: list[tuple[binformat.PlaneEntry, bytes]],
    zstd_level: int,
    compressor: ThreadPoolExecutor,
) -> dict[str, Any]:
    """Compress, write, and read-back-verify one bundle. ``compressor`` is
    shared between concurrently written bundles so the total zstd load stays
    bounded by one machine-sized pool."""
    LOG.info("compressing %d %s payloads at zstd level %d", len(raw_payloads), variable_id, zstd_level)
    compressed = list(
        compressor.map(lambda payload: zstdcli.compress(payload[1], level=zstd_level), raw_payloads)
    )
    planes_out = [
        binformat.PlanePayload(entry=replace(entry, compressed_length=len(payload)), payload=payload)
        for (entry, _raw), payload in zip(raw_payloads, compressed)
    ]
    binformat.write_bundle(output, metadata, planes_out)
    LOG.info("wrote %s (%.2f MB)", output, output.stat().st_size / 1e6)

    # Read the complete file back and decode every plane before publishing stats.
    bundle = binformat.read_bundle(output)
    bundle.verify_all(executor=compressor)
    LOG.info("verified %d %s planes by full read-back decode", len(bundle.entries), variable_id)

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
    input_path: Path,
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
    """
    if profile not in PROFILES:
        raise ConversionError(f"unknown profile: {profile}")
    source = source_spec(model)
    step = source.step_hours
    zstd_version = zstdcli.zstd_version()
    paths = discover_inputs(input_path)
    codebooks = PROFILES[profile]

    # One real gdalinfo pass over the first file: it probes wind availability
    # (wind is optional so runs fetched before the wind components joined the
    # download set, and the cropped test fixtures, still build cleanly) and
    # serves as the per-run cross-check reference for the GRIB2 header index
    # used on every file.
    reference_frames = inspect_grib_multi(
        paths[0],
        source.input_variable_ids,
        optional_ids=source.optional_at_analysis + WIND_COMPONENT_IDS,
    )
    wind_available = all(variable_id in reference_frames for variable_id in WIND_COMPONENT_IDS)
    if not wind_available:
        LOG.warning("building without the wind10m bundle, 10 m wind components are not in %s", paths[0])

    # The variables read from the GRIB inputs; ECMWF carries the accumulated
    # tp instead of a rate and sflux the window-averaged prate_ave, which
    # _quantize_file de-accumulates / de-averages into prate.
    input_scalar_ids = tuple(
        variable_id for variable_id in source.input_variable_ids if variable_id not in WIND_COMPONENT_IDS
    )
    variable_ids = input_scalar_ids + (WIND_COMPONENT_IDS if wind_available else ())
    per_file = _prepare_frames_all(paths, variable_ids, source.optional_at_analysis, reference_frames)
    hours = [frames["tmp2m"].forecast_hour for frames in per_file]
    if len(hours) > 1 and hours != list(range(hours[0], hours[0] + step * len(hours), step)):
        raise ConversionError(f"forecast hours must be a complete sequence at a {step}-hour step")
    if require_complete and hours != list(range(0, expected_hours + 1, step)):
        raise ConversionError(f"complete build requires forecast hours 0 through {expected_hours} every {step} hours")
    run_time = per_file[0]["tmp2m"].run_time.astimezone(UTC)

    grid = _grid_info(paths[0])
    if require_complete and (grid.width, grid.height) != source.production_grid:
        raise ConversionError(
            f"production build requires a {source.production_grid[0]}x{source.production_grid[1]} grid"
        )

    stats: list[PlaneStats] = []
    codes_by_hour: dict[int, dict[str, np.ndarray]] = {}

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
                    previous = (per_file[index - 1][raw_precipitation_id].forecast_hour, previous_future)
                own: Future | None = None
                if frame is not None and index + 1 < len(per_file):
                    successor = per_file[index + 1].get(raw_precipitation_id)
                    if successor is not None and (
                        raw_precipitation_id == "tp"
                        or average_window_start(successor.forecast_hour, source.average_window_hours)
                        < frame.forecast_hour
                    ):
                        own = Future()
                previous_future = own
                yield frames, previous, own

        with ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS) as executor:
            results = executor.map(
                lambda item: _quantize_file(
                    item[0], grid, work, codebooks, item[1], step, source.average_window_hours, item[2]
                ),
                sharing_plan(),
            )
            for hour, codes, file_stats in results:
                codes_by_hour[hour] = codes
                stats.extend(file_stats)
    LOG.info("quantized %d planes", sum(len(codes) for codes in codes_by_hour.values()))

    # The encoded (bundle) variables — the raw tp / prate_ave inputs have
    # already been derived into prate by this point.
    scalar_variable_ids = source.bundle_scalar_ids
    encoded_variable_ids = scalar_variable_ids + (WIND_COMPONENT_IDS if wind_available else ())

    # Per-variable time axes. On derived-precipitation sources (ECMWF
    # accumulations, sflux window averages) the rate has no data for the
    # analysis frame — its interval would precede the run — so the prate
    # series starts at the first real step and every prate artifact (bundle,
    # variant, poster, video) carries its own shorter axis. All other
    # variables keep the full run axis.
    variable_hours: dict[str, list[int]] = {variable_id: hours for variable_id in encoded_variable_ids}
    if (source.accumulated_precipitation or source.averaged_precipitation) and len(hours) > 1:
        variable_hours["prate"] = hours[1:]

    # Optional per-variable WebCodecs video artifacts.
    # Best-effort: a missing ffmpeg or an encode failure just skips that
    # variable's artifact, Xue remains the universal fallback.
    video_reports: dict[str, dict[str, Any]] = {}
    if not skip_video:
        for variable_id in scalar_variable_ids:
            try:
                video_artifact = encode_variable_video(
                    codes_by_hour, variable_hours[variable_id], variable_id, width=grid.width, height=grid.height
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
            video_metadata = build_metadata(run_time, variable_hours[variable_id], grid, profile, (variable_id,), model=source.manifest_model, product=source.product)
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
    for variable_id in scalar_variable_ids:
        payload, poster_grid = encode_poster(codes_by_hour[variable_hours[variable_id][0]][variable_id], grid)
        poster_path = output_dir / f"{variable_id}.poster.bin"
        poster_path.write_bytes(payload)
        poster_reports[variable_id] = {
            "path": str(poster_path),
            "width": poster_grid.width,
            "height": poster_grid.height,
            "byteLength": len(payload),
            "crc32": f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}",
            "metadataJson": json.dumps(build_metadata(run_time, variable_hours[variable_id], poster_grid, profile, (variable_id,), model=source.manifest_model, product=source.product)),
        }
        LOG.info("wrote %s (%.1f KB)", poster_path, len(payload) / 1e3)

    # Full-resolution canonical bundles (one per scalar variable plus the
    # combined two-variable wind bundle) and the half-resolution ladder
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
    half_codes_by_hour = {
        hour: {
            variable_id: _decimate_codes(codes, grid)
            for variable_id, codes in codes_by_hour[hour].items()
        }
        for hour in hours
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
            wind = bundle_id == WIND_BUNDLE_ID
            bundle_hours = hours if wind else variable_hours[bundle_id]
            metadata = build_metadata(
                run_time,
                bundle_hours,
                bundle_grid,
                profile,
                WIND_COMPONENT_IDS if wind else (bundle_id,),
                model=source.manifest_model,
                product=source.product,
            )

            def job() -> dict[str, Any]:
                if wind:
                    payloads = _wind_bundle_payloads(bundle_hours, codes)
                else:
                    payloads = _variable_payloads(
                        bundle_id, bundle_hours, {hour: codes[hour][bundle_id] for hour in bundle_hours}
                    )
                report = _write_variable_bundle(
                    bundle_id, output_dir / f"{bundle_id}{suffix}.xue", metadata, payloads, zstd_level, compressor
                )
                if suffix:
                    report["width"] = bundle_grid.width
                    report["height"] = bundle_grid.height
                    report["bandwidth"] = _playback_bandwidth(report["byteLength"], len(bundle_hours))
                return report

            return writers.submit(job)

        # Submit largest first so the wind bundle's long compression starts at
        # once; reports keep the scalars-then-wind order regardless.
        submit_order = ((WIND_BUNDLE_ID,) if wind_available else ()) + scalar_variable_ids
        report_order = scalar_variable_ids + ((WIND_BUNDLE_ID,) if wind_available else ())
        full_futures = {bundle_id: submit_bundle(bundle_id, "", grid, codes_by_hour) for bundle_id in submit_order}
        half_futures = (
            {bundle_id: submit_bundle(bundle_id, ".half", half_grid, half_codes_by_hour) for bundle_id in submit_order}
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
    if wind_available:
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
            expected_hours=hours[-1] if len(hours) > 1 else expected_hours,
            model=source.manifest_model,
            product=source.product,
        )
        write_bin_manifest(manifest_path, payload, force=force, expected_hours=hours[-1] if len(hours) > 1 else expected_hours)
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
    """Structurally validate a bundle and decode every plane."""
    bundle = binformat.read_bundle(path)
    bundle.verify_all()
    return {
        "path": str(path),
        "byteLength": len(bundle.data),
        "planes": len(bundle.entries),
        "variables": sorted(bundle.variable_ids.values()),
        "frameCount": bundle.frame_count,
        "grid": f"{bundle.width}x{bundle.height}",
        "runTime": bundle.metadata.get("runTime"),
        "profile": bundle.metadata.get("profile"),
    }

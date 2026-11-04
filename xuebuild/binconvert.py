"""Convert gridded input into a run's bundles (mirrored in ``encode/convert.rs``).

:func:`convert_bin` runs the build in stages: discover the frames and the
bundles they can build (:mod:`xuebuild.discover`), read every grid family's
grid (:mod:`xuebuild.grid`), extract and quantize every file in parallel —
one multi-band ``gdal_translate`` per input file, the derivations of
:mod:`xuebuild.derive` on the planes it returns — cut the resolution ladder
(:mod:`xuebuild.ladder`), write every bundle's arrays into the run's shared
Zarr stores (and, asked for, its ``.xue`` container), and report the build
and its manifest (:mod:`xuebuild.report`).

A forecast source arrives as one GRIB2 file per forecast hour; an observation
or series-file source (:mod:`xuebuild.observation`) as NetCDF series whose
bands are the time axis. Everything past frame discovery is the same for both.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from . import binformat, temporal, zarrstore, zstdcli
from .bundles import (
    DERIVED_SCALARS,
    DERIVED_VECTORS,
    PRECIPITATION_RATE_BUNDLE_ID,
    VECTOR_BUNDLES,
    bundle_variable_ids,
    cat_level,
)
from .common import crc32_hex
from .derive import (
    average_window_start,
    deaccumulate_precipitation,
    deaverage_precipitation,
    derive_cat_plane,
    derive_scalar,
    derive_vector,
    interval_rate,
    precipitation_mode,
    soil_temperature_unit,
)
from .discover import (
    Discovered,
    analysis_optional_ids,
    bundle_grid_family,
    bundle_input_ids,
    discover_records,
    discover_series,
    published_bundle_ids,
    series_lead_seconds,
    store_dimensions,
)
from .errors import ConversionError, DownloadError
from . import rules
from .gdal import (
    require_command,
    run_command,
)
from .grid import GridInfo, crop_grid, grid_info
from .ladder import build_ladder, bundle_tile, playback_bandwidth, variant_tier
from .metadata import axis_unit_seconds, build_metadata
from .model import GRIB_PLANE_SOURCE, PlaneSource, SourceFrame
from .poster import encode_poster
from .quantize import PROFILES, PrecipitationCodebook, TemperatureCodebook
from .report import build_report, delivered_byte_length, write_manifest
from .sources import SourceSpec, family_frame_path, source_spec
from .variables import VARIABLES, variable_spec

LOG = logging.getLogger(__name__)


def _predictor(variable_id: str) -> int:
    """The container predictor the variable's ``predictor`` rule names:
    chunks chain against the previous frame, or stack the codes raw."""
    return binformat.PREDICTOR_RAW if variable_spec(variable_id).predictor == "raw" else binformat.PREDICTOR_PREVIOUS


# gdal_translate decodes GRIB packing on the CPU: one worker per core.
_EXTRACT_WORKERS = min(16, os.cpu_count() or 4)
# Bundles compressing/verifying concurrently; the shared zstd pool bounds the
# real CPU load, this only caps how many bundles' raw payloads are alive.
_BUNDLE_WRITERS = 4


@dataclass
class PlaneStats:
    variable_id: str
    lead_seconds: int
    max_abs_error: float
    clamped_points: int
    overflow_points: int


def _precipitation_plan(source: SourceSpec, variable_ids: tuple[str, ...]) -> tuple[str, str] | None:
    """The raw precipitation input this build reads and the mode that turns
    it into the rate, or None when the rate is read as it is or the input
    is not among ``variable_ids`` (a build that asked for other bundles)."""
    mode = precipitation_mode(source)
    if mode is None:
        return None
    input_id = next(iter(bundle_input_ids(source, PRECIPITATION_RATE_BUNDLE_ID)), None)
    if input_id is None or input_id not in variable_ids:
        return None
    return input_id, mode


def _convert_units(frame: SourceFrame, values: np.ndarray) -> np.ndarray:
    """One plane taken to its published unit by the variable's conversion
    rule (:func:`xuebuild.rules.convert`). A soil temperature's unit is
    read through :func:`soil_temperature_unit` first: GDAL labels the
    plane Celsius whether or not it converted it."""
    spec = variable_spec(frame.variable_id)
    unit = frame.unit
    if spec.conversion.rule == "soil-temperature":
        unit = soil_temperature_unit(rules.temperature_unit(unit), values)
    return rules.convert(spec.conversion, unit, values)


def _plane_source_for(plane_source: PlaneSource | Mapping[str, PlaneSource], variable_id: str) -> PlaneSource:
    """One file's plane source (GRIB: the same for every record) or an
    observation window's per-variable ones."""
    if isinstance(plane_source, PlaneSource):
        return plane_source
    return plane_source[variable_id]


def _grid_for(grid: GridInfo | Mapping[str, GridInfo], variable_id: str) -> GridInfo:
    """One variable's grid: the run's single grid, or — on a source with a
    grid family — the grid of the family the variable is read from."""
    if isinstance(grid, GridInfo):
        return grid
    return grid[variable_id]


def _extract_planes(
    frames: dict[str, SourceFrame],
    grid: GridInfo | Mapping[str, GridInfo],
    work: Path,
    plane_source: PlaneSource | Mapping[str, PlaneSource] = GRIB_PLANE_SOURCE,
    uncropped_ids: frozenset[str] = frozenset(),
    uncropped: dict[str, np.ndarray] | None = None,
) -> dict[str, np.ndarray]:
    """Extract every requested band of one frame: a single gdal_translate
    per file the frame's variables live in — one for a GRIB record set,
    one per variable for a satellite window whose series are one file
    each, one per grid family for a source with more than one grid
    (mirrored in encode/convert.rs).

    ``grid`` is the published grid, or one per variable where the frame's
    families are on grids of their own; a file holds one family's records,
    so the plane size is read off the grid of whatever the file carries.

    Each of ``uncropped_ids`` is also put in ``uncropped`` as it was before
    the crop window was cut, unit-converted the same way — the whole plane a
    derivation reading neighbours needs (:func:`derive_cat_plane`)."""
    order = list(frames)
    hour = frames[order[0]].lead_seconds
    values_by_id: dict[str, np.ndarray] = {}
    # The files in first-seen order, each with the variables it holds.
    by_file: dict[Path, list[str]] = {}
    for variable_id in order:
        by_file.setdefault(frames[variable_id].path, []).append(variable_id)
    for source, file_order in by_file.items():
        source_height, source_width = _grid_for(grid, file_order[0]).source_shape
        plane_size = source_width * source_height
        # Named by the band set's hash rather than the ids joined: a GFS frame
        # carries over thirty of them, past a filesystem's 255-byte name.
        band_set = zlib.crc32("-".join(file_order).encode("ascii")) & 0xFFFFFFFF
        raw = work / f"planes.f{hour:03d}.{os.getpid()}.{band_set:08x}.bin"
        command = [require_command("gdal_translate"), "-q"]
        if _plane_source_for(plane_source, file_order[0]).unscale:
            command.append("-unscale")
        for variable_id in file_order:
            command += ["-b", str(frames[variable_id].band)]
        command += ["-of", "ENVI", "-ot", "Float64", "-co", "INTERLEAVE=BSQ", str(source), str(raw)]
        run_command(command, description=f"extract {', '.join(file_order)} f{hour:03d}")
        values = np.fromfile(raw, dtype="<f8")
        if values.size != plane_size * len(file_order):
            raise ConversionError(f"extracted plane size mismatch for {source}")
        for index, variable_id in enumerate(file_order):
            values_by_id[variable_id] = values[index * plane_size : (index + 1) * plane_size].copy()
    planes: dict[str, np.ndarray] = {}
    for variable_id in order:
        variable_grid = _grid_for(grid, variable_id)
        plane = values_by_id[variable_id].reshape(*variable_grid.source_shape)
        if variable_grid.column_roll:
            plane = np.roll(plane, variable_grid.column_roll, axis=1)
        # Missing data becomes a value before the plane is resampled, so a
        # fill never blends into its neighbours; then the projected plane
        # lands on the regular grid, and only then is a regional window cut.
        plane = _plane_source_for(plane_source, variable_id).apply_fill(plane)
        plane = _fill_missing(variable_id, plane)
        if variable_grid.resample is not None:
            plane = variable_grid.resample.take(plane)
        if variable_grid.downsample is not None:
            plane = variable_grid.downsample.take(plane)
        whole = plane
        if variable_grid.crop is not None:
            plane = np.ascontiguousarray(variable_grid.crop.take(plane))
        plane = plane.ravel()
        if not np.isfinite(plane).all():
            raise ConversionError(f"Xue v1 requires complete planes, found non-finite values in {source}")
        planes[variable_id] = _convert_units(frames[variable_id], plane)
        if uncropped is not None and variable_id in uncropped_ids:
            if variable_grid.crop is None:
                uncropped[variable_id] = planes[variable_id]
            else:
                uncropped[variable_id] = _convert_units(frames[variable_id], whole.ravel().copy())
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


def _linear_stats(values: np.ndarray, codes: np.ndarray, codebook: TemperatureCodebook) -> tuple[float, int]:
    """Quantization error and clamp count for any linear-codebook plane."""
    decoded = codebook.decode(codes)
    in_range = (values >= codebook.minimum) & (values <= codebook.maximum)
    clamped = int(np.count_nonzero(~in_range))
    max_error = float(np.abs(decoded[in_range] - values[in_range]).max()) if in_range.any() else 0.0
    return max_error, clamped


def _quantize_file(
    frames: dict[str, SourceFrame],
    grid: GridInfo | Mapping[str, GridInfo],
    work: Path,
    codebooks: dict[str, TemperatureCodebook | PrecipitationCodebook],
    previous_precipitation: tuple[int, Future | None] | None = None,
    average_window_hours: int = 6,
    own_precipitation: Future | None = None,
    plane_source: PlaneSource | Mapping[str, PlaneSource] = GRIB_PLANE_SOURCE,
    derived_vector_ids: tuple[str, ...] = (),
    drop_ids: frozenset[str] = frozenset(),
    derived_scalar_ids: tuple[str, ...] = (),
    cat_calibration: Mapping[int, tuple[float, float]] | None = None,
    precipitation: tuple[str, str] | None = None,
) -> tuple[int, dict[str, np.ndarray], list[PlaneStats]]:
    """Extract and quantize every variable of one file; runs on a worker thread.

    ``precipitation`` names the raw precipitation input and the mode that
    turns it into the rate (:func:`_precipitation_plan`); None reads the
    rate as it is.

    ``derived_vector_ids`` names the vapour flux bundles and
    ``derived_scalar_ids`` the equivalent potential temperatures to derive
    from the planes just extracted, and ``drop_ids`` the inputs that served
    only such a derivation and are not themselves published — they are
    released here rather than quantized and carried through the whole run.
    A clear-air turbulence bundle is derived on its inputs' uncropped planes
    with the source's ``cat_calibration`` (level → ln TI1 mean and standard
    deviation).

    Derived-precipitation sources (ECMWF run-total tp, sflux window-averaged
    prate_ave) difference against the previous file's raw plane. Instead of
    re-extracting that file, workers share planes through futures: each worker
    publishes its own converted raw plane into ``own_precipitation`` and reads
    the predecessor's from ``previous_precipitation`` (its hour and future).
    The pool runs files in submission (hour) order, so the awaited predecessor
    is always already running or done. Shared planes are never mutated. An
    interval total (Open-Meteo apcp) needs no predecessor plane at all — only
    the hour beside the future, which is then None.
    """
    lead = next(iter(frames.values())).lead_seconds
    # The precipitation derivations below are GRIB-only, and every GRIB record
    # is a whole hour out, so they can work in hours.
    hour = lead // binformat.HOUR_SECONDS
    cat_ids = tuple(bundle_id for bundle_id in derived_scalar_ids if cat_level(bundle_id) is not None)
    uncropped: dict[str, np.ndarray] = {}
    try:
        values = _extract_planes(
            frames,
            grid,
            work,
            plane_source,
            frozenset(variable_id for bundle_id in cat_ids for variable_id in DERIVED_SCALARS[bundle_id]),
            uncropped,
        )
    except BaseException as exc:
        # Unblock the successor waiting on this worker's plane.
        if own_precipitation is not None:
            own_precipitation.set_exception(exc)
        raise
    # The analysis frame of a source whose input is optional there carries
    # no raw plane, and derives nothing.
    raw_precipitation_id, mode = (None, None)
    if precipitation is not None and precipitation[0] in values:
        raw_precipitation_id, mode = precipitation
    if own_precipitation is not None:
        own_precipitation.set_result(values[raw_precipitation_id])
    previous_plane: np.ndarray | None = None
    previous_hour: int | None = None
    if previous_precipitation is not None:
        previous_hour, previous_future = previous_precipitation
        previous_plane = previous_future.result() if previous_future is not None else None
    if mode == "accumulated":
        # ECMWF: replace the run-total accumulation (already mm) with the
        # mean rate over the step that ends at this frame (mm/h). The step is
        # the actual distance to the previous frame — six hours past the
        # 144-hour cadence change, three before it.
        step = hour - previous_hour if previous_hour is not None else 1
        values[PRECIPITATION_RATE_BUNDLE_ID] = deaccumulate_precipitation(
            values.pop(raw_precipitation_id), previous_plane, step
        )
    elif mode == "interval":
        # Open-Meteo: the file already holds the total that fell over the
        # interval since the model's previous native output time, so the
        # rate is that total over the interval's length in hours — the
        # distance to the frame before this one on the axis.
        if previous_hour is None:
            raise ConversionError(f"the interval precipitation frame at hour {hour} names no interval")
        values[PRECIPITATION_RATE_BUNDLE_ID] = interval_rate(values.pop(raw_precipitation_id), hour - previous_hour)
    elif mode == "averaged":
        # sflux: PRATE is the window-cumulative mean rate (kg/m^2 s); derive
        # the per-step rate against the previous frame of the same averaging
        # window (the window's first frame differences against zero).
        values[PRECIPITATION_RATE_BUNDLE_ID] = deaverage_precipitation(
            values.pop(raw_precipitation_id),
            hour,
            previous_plane,
            previous_hour,
            average_window_hours,
        )
    for bundle_id in derived_vector_ids:
        u_id, v_id = VECTOR_BUNDLES[bundle_id]
        values[u_id], values[v_id] = derive_vector(values, bundle_id)
    for bundle_id in derived_scalar_ids:
        if bundle_id in cat_ids:
            values[bundle_id] = derive_cat_plane(
                uncropped, bundle_id, _grid_for(grid, DERIVED_SCALARS[bundle_id][0]), cat_calibration or {}
            )
        else:
            values[bundle_id] = derive_scalar(values, bundle_id)
    uncropped.clear()
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
    predictors = {numeric_ids[variable_id]: _predictor(variable_id) for variable_id in variable_ids}
    planes = {
        offset: {numeric_ids[variable_id]: codes[offset][variable_id] for variable_id in variable_ids}
        for offset in offsets
    }
    return temporal.build_chunks(offsets, planes, tiles, predictors)


def _store_source(
    variable_ids: tuple[str, ...],
    offsets: list[int],
    codes: dict[int, dict[str, np.ndarray]],
    metadata: dict[str, Any],
    tile: tuple[int, int],
) -> zarrstore.CodesSource:
    """One bundle's codes as the store writer reads them: each variable's
    planes in axis order, and its predictor by the rule `_bundle_chunks`
    applies to the container."""
    return zarrstore.CodesSource(
        metadata,
        {variable_id: [codes[offset][variable_id] for offset in offsets] for variable_id in variable_ids},
        {
            variable_id: "raw" if variable_spec(variable_id).predictor == "raw" else "previous"
            for variable_id in variable_ids
        },
        tile,
    )


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

    _verify_container(output)
    LOG.info("verified %s by full read-back decode", output)

    bundle_bytes = output.read_bytes()
    return {
        "variable": variable_id,
        "output": str(output),
        "byteLength": len(bundle_bytes),
        "crc32": crc32_hex(bundle_bytes),
    }


def _verify_container(path: Path) -> None:
    """Read a written container back through the wheel's decoder and decode
    every plane, each checked against the CRC-32 the writer recorded, before
    its stats are published."""
    import xue  # noqa: PLC0415  (the wheel is only needed when a container is written)

    bundle = xue.Bundle.open(path)
    for numeric_id in bundle.variable_ids:
        for offset in bundle.frame_offsets:
            bundle.decode(numeric_id, offset)
        bundle.clear_cache()


def _time_axis(
    per_file: list[dict[str, SourceFrame]],
    reference_id: str,
    source: SourceSpec,
    *,
    require_complete: bool,
    expected_hours: int,
) -> tuple[int, list[int], datetime]:
    """The run's axis — its unit, every frame's offset on it — and its run
    time, read off ``reference_id``'s frames and checked against the
    source's published axis."""
    # The bundle's time axis: the coarsest unit that expresses every frame
    # exactly, and each frame's offset in it. An hour for every forecast
    # source, so their offsets are their forecast hours as before.
    lead_seconds = [frames[reference_id].lead_seconds for frames in per_file]
    unit_seconds = axis_unit_seconds(lead_seconds)
    offsets = [seconds // unit_seconds for seconds in lead_seconds]
    if offsets[-1] >= binformat.NO_DEPENDENCY:
        raise ConversionError(
            f"the axis needs {offsets[-1]} steps of {unit_seconds} s, past the u16 frame offset range"
        )
    run_time = per_file[0][reference_id].run_time.astimezone(UTC)
    if not source.observation:
        # The input hours must be a contiguous run of the source's published
        # axis (hourly to f120, three-hourly beyond, on GFS), so no frame is
        # missing and every step matches the cadence the source publishes.
        # The run's own cycle decides whether a longer axis applies.
        hours = offsets
        try:
            axis = source.forecast_hours(hours[-1], cycle=run_time.hour)
        except DownloadError as exc:
            raise ConversionError(str(exc)) from exc
        if hours != [hour for hour in axis if hour >= hours[0]]:
            raise ConversionError(f"forecast hours must be a contiguous run of the {source.manifest_model} axis")
        if require_complete:
            try:
                expected_axis = source.forecast_hours(expected_hours, cycle=run_time.hour)
            except DownloadError as exc:
                raise ConversionError(str(exc)) from exc
            if hours != expected_axis:
                raise ConversionError(
                    f"complete build requires forecast hours 0 through {expected_hours} on the "
                    f"{source.manifest_model} axis"
                )
    return unit_seconds, offsets, run_time


def _family_grids(
    source: SourceSpec,
    variable_ids: tuple[str, ...],
    variable_families: Mapping[str, str | None],
    grid_path: Path,
    *,
    require_complete: bool,
    bbox: tuple[float, float, float, float] | None,
) -> dict[str | None, GridInfo]:
    # One GridInfo per family in play, each read from that family's own file
    # and checked against that family's production grid. The families are in
    # input order, so the primary file's — the run's grid in the build report
    # — comes first wherever this build reads it at all.
    family_order = list(
        dict.fromkeys(variable_families.get(variable_id) for variable_id in variable_ids)
    )
    grids: dict[str | None, GridInfo] = {}
    for family in family_order:
        family_grid = grid_info(family_frame_path(grid_path, family), source)
        if require_complete:
            production_grid, _ = source.family_grid(family)
            if (family_grid.width, family_grid.height) != production_grid:
                where = f" for the {family} family" if family is not None else ""
                raise ConversionError(
                    f"production build requires a {production_grid[0]}x{production_grid[1]} grid{where}"
                )
        if bbox is not None:
            family_grid = crop_grid(family_grid, bbox)
            LOG.info(
                "cropped %s to %dx%d from %.4f,%.4f (%s)",
                family or "the run",
                family_grid.width,
                family_grid.height,
                family_grid.first_longitude,
                family_grid.first_latitude,
                ",".join(f"{value:g}" for value in bbox),
            )
        grids[family] = family_grid
    return grids


def _sharing_plan(
    per_file: list[dict[str, SourceFrame]],
    raw_precipitation_id: str | None,
    source: SourceSpec,
    run_time: datetime,
):
    """Yield (frames, previous (hour, future | None) | None, own future
    | None) per file. A future is created only when the next file will
    difference against this file's raw plane; no reference is kept
    here, so each shared plane is freed once its consumer finishes."""
    previous_future: Future | None = None
    for index, frames in enumerate(per_file):
        frame = frames.get(raw_precipitation_id) if raw_precipitation_id else None
        previous: tuple[int, Future | None] | None = None
        own: Future | None = None
        if frame is not None and source.interval_precipitation:
            # The interval this total covers is the distance to the
            # frame before it on the axis being built. A series that
            # starts at a step of its own — the rate's, whose analysis
            # frame does not exist — takes the distance from the
            # previous step of the source's published axis instead,
            # which is the interval the model itself accumulated over;
            # and the first step of an axis that itself starts past
            # the analysis (``first_hour``) accumulated from the run
            # time, hour 0. Only a total at the analysis names no
            # interval at all.
            hour = frame.lead_seconds // binformat.HOUR_SECONDS
            if index:
                interval_start = series_lead_seconds(per_file[index - 1]) // binformat.HOUR_SECONDS
            else:
                axis = source.forecast_hours(hour, cycle=run_time.hour)
                if len(axis) >= 2:
                    interval_start = axis[-2]
                elif hour > 0:
                    interval_start = 0
                else:
                    raise ConversionError(
                        f"the interval precipitation frame at hour {hour} names no interval"
                    )
            previous = (interval_start, None)
        elif previous_future is not None and frame is not None:
            previous = (
                per_file[index - 1][raw_precipitation_id].lead_seconds // binformat.HOUR_SECONDS,
                previous_future,
            )
        if frame is not None and not source.interval_precipitation and index + 1 < len(per_file):
            successor = per_file[index + 1].get(raw_precipitation_id)
            if successor is not None and (
                precipitation_mode(source) == "accumulated"
                or average_window_start(
                    successor.lead_seconds // binformat.HOUR_SECONDS, source.average_window_hours
                )
                < frame.lead_seconds // binformat.HOUR_SECONDS
            ):
                own = Future()
        previous_future = own
        yield frames, previous, own


def _quantize_run(
    discovered: Discovered,
    read_grids: dict[str, GridInfo],
    work_parent: Path,
    codebooks: dict[str, TemperatureCodebook | PrecipitationCodebook],
    source: SourceSpec,
    *,
    unit_seconds: int,
    run_time: datetime,
) -> tuple[dict[int, dict[str, np.ndarray]], list[PlaneStats]]:
    """Extract and quantize every file of the run in parallel
    (:func:`_quantize_file`): the codes of every variable by frame offset,
    and each plane's quantization statistics."""
    per_file = discovered.per_file
    variable_ids = discovered.variable_ids
    plane_source = discovered.plane_source
    drop_ids = discovered.drop_ids
    available_vector_ids = discovered.available_vector_ids
    available_derived_ids = discovered.available_derived_ids
    stats: list[PlaneStats] = []
    codes_by_offset: dict[int, dict[str, np.ndarray]] = {}

    work_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="xue-", dir=work_parent) as temporary:
        work = Path(temporary)
        LOG.info("extracting and quantizing %d files with %d workers", len(per_file), _EXTRACT_WORKERS)
        # Precipitation derivation predecessors, shared between workers as
        # futures (see _quantize_file): ECMWF tp differences against the
        # previous frame unconditionally; sflux prate_ave only against the
        # previous frame of the same averaging window (the window's first
        # frame differences against zero). An Open-Meteo apcp total needs no
        # predecessor plane, only the length of the interval it covers.
        precipitation = _precipitation_plan(source, variable_ids)
        raw_precipitation_id = precipitation[0] if precipitation is not None else None

        derived_vector_ids = tuple(bundle_id for bundle_id in available_vector_ids if bundle_id in DERIVED_VECTORS)
        # Derived after the vapour flux and before the derivation-only inputs
        # are dropped: both read spfh850.
        derived_scalar_ids = available_derived_ids
        cat_calibration = {level: (mean, std) for level, mean, std in source.cat_calibration}
        with ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS) as executor:
            results = executor.map(
                lambda item: _quantize_file(
                    item[0],
                    read_grids,
                    work,
                    codebooks,
                    item[1],
                    source.average_window_hours,
                    item[2],
                    plane_source,
                    derived_vector_ids,
                    drop_ids,
                    derived_scalar_ids,
                    cat_calibration,
                    precipitation,
                ),
                _sharing_plan(per_file, raw_precipitation_id, source, run_time),
            )
            for lead, codes, file_stats in results:
                codes_by_offset[lead // unit_seconds] = codes
                stats.extend(file_stats)
    LOG.info("quantized %d planes", sum(len(codes) for codes in codes_by_offset.values()))
    return codes_by_offset, stats


def _variable_offsets(
    source: SourceSpec,
    offsets: list[int],
    scalar_variable_ids: tuple[str, ...],
    encoded_variable_ids: tuple[str, ...],
) -> dict[str, list[int]]:
    # Per-variable time axes. A variable with no data for the analysis frame
    # starts at the first real step, and every artifact of it (bundle,
    # variant, poster) carries its own shorter axis: the rate on a
    # derived-precipitation source (ECMWF accumulations, sflux window
    # averages), whose interval would precede the run, and any scalar whose
    # record the source lists as optional at the analysis (ECMWF's gust, an
    # interval maximum with an empty interval there). All other variables
    # keep the full run axis.
    variable_offsets: dict[str, list[int]] = {variable_id: offsets for variable_id in encoded_variable_ids}
    if len(offsets) > 1 and offsets[0] == 0:
        # Only a run that starts at the analysis has an analysis frame to be
        # missing from: a build whose axis already starts at the first step
        # (a job for the rate alone on a source whose precipitation input has
        # no analysis file) carries every frame it read.
        for variable_id in analysis_optional_ids(source, scalar_variable_ids):
            variable_offsets[variable_id] = offsets[1:]
    # A static field does not vary in time: its one frame is the first, and
    # its bundle carries just that offset (docs/format.md — a one-frame axis
    # is a uniform axis with frameStep 1). The fetch reads it only from the
    # analysis and the inspector accepts it nowhere else.
    for variable_id in encoded_variable_ids:
        if VARIABLES[variable_id].static:
            variable_offsets[variable_id] = [offsets[0]]
    return variable_offsets


def _write_posters(
    output_dir: Path,
    companion_variable_ids: tuple[str, ...],
    codes_by_offset: dict[int, dict[str, np.ndarray]],
    variable_offsets: dict[str, list[int]],
    grid_for: Callable[[str], GridInfo],
    *,
    run_time: datetime,
    profile: str,
    source: SourceSpec,
    unit_seconds: int,
    producer_versions: Mapping[str, tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    # First-frame posters: one tiny artifact per variable so a variable switch
    # can paint immediately while the real stream loads.
    output_dir.mkdir(parents=True, exist_ok=True)
    poster_reports: dict[str, dict[str, Any]] = {}
    for variable_id in companion_variable_ids:
        payload, poster_grid = encode_poster(
            codes_by_offset[variable_offsets[variable_id][0]][variable_id], grid_for(variable_id)
        )
        poster_path = output_dir / f"{variable_id}.poster.bin"
        poster_path.write_bytes(payload)
        poster_reports[variable_id] = {
            "path": str(poster_path),
            "width": poster_grid.width,
            "height": poster_grid.height,
            "byteLength": len(payload),
            "crc32": crc32_hex(payload),
            "metadataJson": json.dumps(build_metadata(run_time, variable_offsets[variable_id], poster_grid, profile, (variable_id,), source=source, unit_seconds=unit_seconds, producers=producer_versions)),
        }
        LOG.info("wrote %s (%.1f KB)", poster_path, len(payload) / 1e3)
    return poster_reports


def _open_shared_stores(output_dir: Path, variant_factors: tuple[int, ...], *, zarr: bool, partial: bool) -> dict[int, Path]:
    # The shared stores this build writes into, by factor. A whole build owns
    # them and starts them empty; a fanned-out job shares them with the run's
    # other jobs (on one machine, or merged afterwards), so it only drops the
    # group document, which its arrays make stale until assembly.
    shared_stores: dict[int, Path] = {}
    if zarr:
        for factor in (1, *variant_factors):
            store = output_dir / zarrstore.shared_store_name(variant_tier(factor) if factor > 1 else None)
            if not partial and store.exists():
                shutil.rmtree(store)
            (store / "zarr.json").unlink(missing_ok=True)
            shared_stores[factor] = store
    return shared_stores


def _write_bundles(
    *,
    source: SourceSpec,
    run_time: datetime,
    profile: str,
    unit_seconds: int,
    producer_versions: Mapping[str, tuple[str, str]],
    output_dir: Path,
    zstd_level: int,
    container: bool,
    zarr: bool,
    series: bool,
    shared_stores: dict[int, Path],
    grids: Mapping[str | None, GridInfo],
    variant_grids: Mapping[int, Mapping[str | None, GridInfo]],
    variable_families: Mapping[str, str | None],
    variable_offsets: dict[str, list[int]],
    codes_by_offset: dict[int, dict[str, np.ndarray]],
    variant_codes_by_offset: dict[int, dict[int, dict[str, np.ndarray]]],
    variant_factors: tuple[int, ...],
    submit_order: tuple[str, ...],
    report_order: tuple[str, ...],
) -> tuple[
    list[dict[str, Any]],
    dict[str, list[dict[str, Any]]],
    dict[tuple[int, str], zarrstore.BundleWrite],
]:
    """Write every bundle of every tier, concurrently: the full tier's
    reports in ``report_order``, each bundle's rung reports in ascending
    factor order, and the store write of each (tier, bundle)."""
    store_writes: dict[tuple[int, str], zarrstore.BundleWrite] = {}

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as compressor, ThreadPoolExecutor(
        max_workers=_BUNDLE_WRITERS
    ) as writers:

        def submit_bundle(
            bundle_id: str,
            factor: int,
            codes: dict[int, dict[str, np.ndarray]],
        ) -> Future:
            # ``factor`` 1 is the full tier, bare-named; a rung takes its
            # tier's suffix and the tile the rung's grid is cut with.
            suffix = f".{variant_tier(factor)}" if factor > 1 else ""
            bundle_variables = bundle_variable_ids(bundle_id)
            bundle_offsets = variable_offsets[bundle_variables[0]]
            # The bundle is written on its own family's grid, with that
            # family's tile: one grid for every source but CFSv2, whose
            # pressure-level bundles are 1° where its surface ones are the
            # T126 Gaussian grid.
            family = variable_families.get(bundle_variables[0])
            bundle_grid = grids[family] if factor == 1 else variant_grids[factor][family]
            family_tile = source.family_grid(family)[1]
            metadata = build_metadata(
                run_time,
                bundle_offsets,
                bundle_grid,
                profile,
                bundle_variables,
                source=source,
                unit_seconds=unit_seconds,
                producers=producer_versions,
            )

            tile = bundle_tile(family_tile, bundle_grid, factor=factor)
            tiles = binformat.TileGeometry(bundle_grid.width, bundle_grid.height, *tile)

            def job() -> dict[str, Any]:
                if container:
                    tables = _bundle_chunks(bundle_variables, bundle_offsets, codes, tiles)
                    report = _write_variable_bundle(
                        bundle_id,
                        output_dir / f"{bundle_id}{suffix}.xue",
                        metadata,
                        tile,
                        tables,
                        zstd_level,
                        compressor,
                    )
                    del tables
                else:
                    report = {"variable": bundle_id, "container": False}
                if zarr:
                    # Written from the codes in memory: a bundle's arrays are a
                    # function of the metadata, the codes, the tile, the
                    # predictors and its dimension names alone
                    # (`zarrstore.write_bundle_arrays`), so they never wait on
                    # a container, and where one is written too the two carry
                    # the same codes by construction. The group document comes
                    # after every bundle of the tier.
                    store_source = _store_source(bundle_variables, bundle_offsets, codes, metadata, tile)
                    export = zarrstore.write_bundle_arrays(
                        store_source,
                        shared_stores[factor],
                        bundle_id,
                        store_dimensions(source, bundle_id),
                        executor=compressor,
                    )
                    LOG.info("wrote %s into %s (%.2f MB)", bundle_id, export.path, export.byte_length / 1e6)
                    store_writes[(factor, bundle_id)] = export
                    report["zarr"] = export.to_dict()
                    if series and factor == 1 and bundle_id in source.series_bundle_ids:
                        # The series companion is a full-resolution artifact:
                        # a pinned point reads the canonical bundle, never a
                        # tier, so no variant ships one. Its codec chain
                        # carries the bundle's own predictor (`xue.delta`).
                        companion = zarrstore.write_store(
                            store_source,
                            output_dir / f"{bundle_id}.series.zarr",
                            delta=True,
                            series=True,
                            executor=compressor,
                        )
                        LOG.info("wrote %s (%.2f MB)", companion.path, companion.byte_length / 1e6)
                        report["series"] = companion.to_dict()
                    del store_source
                if factor > 1:
                    report["width"] = bundle_grid.width
                    report["height"] = bundle_grid.height
                    # A tier's playback hint is the delivery it ships: the
                    # container where one is written, the store otherwise.
                    report["bandwidth"] = playback_bandwidth(delivered_byte_length(report), len(bundle_offsets))
                return report

            return writers.submit(job)

        full_futures = {bundle_id: submit_bundle(bundle_id, 1, codes_by_offset) for bundle_id in submit_order}
        variant_futures = {
            factor: {
                bundle_id: submit_bundle(bundle_id, factor, variant_codes_by_offset[factor])
                for bundle_id in submit_order
            }
            for factor in variant_factors
        }
        bundle_reports = [full_futures[bundle_id].result() for bundle_id in report_order]
        variant_reports: dict[str, list[dict[str, Any]]] = {
            bundle_id: [variant_futures[factor][bundle_id].result() for factor in variant_factors]
            for bundle_id in report_order
            if variant_factors
        }
    return bundle_reports, variant_reports, store_writes


def _finalize_stores(
    shared_stores: dict[int, Path],
    store_writes: dict[tuple[int, str], zarrstore.BundleWrite],
    report_order: tuple[str, ...],
    bundle_reports: list[dict[str, Any]],
    variant_reports: dict[str, list[dict[str, Any]]],
    variant_factors: tuple[int, ...],
    *,
    partial: bool,
) -> None:
    # Each tier's group document, last: the part over this build's bundles in
    # manifest order, then — unless a fanned-out job's — the group document
    # from it. Every entry naming the store carries its CRC-32.
    for factor, store in shared_stores.items():
        writes = [store_writes[(factor, bundle_id)] for bundle_id in report_order]
        _part, crc32 = zarrstore.write_part(store, writes)
        if not partial:
            crc32 = zarrstore.finalize_store(store, list(report_order), remove_parts=True)
        stamped = (
            list(bundle_reports)
            if factor == 1
            else [variant_reports[bundle_id][variant_factors.index(factor)] for bundle_id in report_order]
        )
        for report in stamped:
            report["zarr"]["crc32"] = crc32


def _check_quantization(
    stats: Sequence[PlaneStats],
    encoded_variable_ids: tuple[str, ...],
    codebooks: dict[str, TemperatureCodebook | PrecipitationCodebook],
) -> None:
    # Quantization acceptance runs over the *encoded* variables (prate is the
    # de-accumulated output on ECMWF; the raw tp input has no codebook).
    for variable_id in encoded_variable_ids:
        codebook = codebooks[variable_id]
        if not isinstance(codebook, TemperatureCodebook):
            continue
        worst = max((item.max_abs_error for item in stats if item.variable_id == variable_id), default=0.0)
        if worst > 0.5001 * codebook.step:
            raise ConversionError(f"{variable_id} quantization error exceeds half a step")


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
    skip_variants: bool = False,
    model: str = "gfs",
    bbox: tuple[float, float, float, float] | None = None,
    bundle_ids: tuple[str, ...] | None = None,
    last_hour: int | None = None,
    zarr: bool = True,
    container: bool = False,
    series: bool = True,
    partial: bool = False,
) -> dict[str, Any]:
    """Convert a GRIB run (or a series file) into shared Zarr stores.

    Writes every bundle's arrays into the run's shared store
    ``<output_dir>/bundles.zarr`` — every scalar variable, plus the
    two-variable ``wind10m`` bundle when the input files carry the 10 m wind
    components, and so on — and each rung of the source's ladder into
    ``bundles.half.zarr`` (and on the satellite disks ``bundles.quarter.zarr``
    too, ``SourceSpec.variant_factors``), the series store
    (``<bundle>.series.zarr``) of the source's point-read bundles,
    per-variable posters, and returns build statistics. The
    ``.xue`` container of each is written only with ``container=True``. When ``latest_path`` and ``run_id`` are given, also
    (re)writes the mutable ``latest.json`` live pointer aimed at the freshly
    written manifest.

    ``bbox`` (west, south, east, north, degrees) crops every plane to a
    region, and ``bundle_ids`` restricts which bundles are built — the two
    knobs the historical showcase cases use to ship a small slice of a past
    run. A restricted build's manifest carries only the bundles it was asked
    for, so it is not required to hold the core tmp2m and prate pair.
    ``last_hour`` trims an observation source's series to a leading window;
    a forecast run is already exactly the frames that were fetched.
    ``zarr`` writes a Zarr v3 store for every bundle and variant
    (`xuebuild.zarrstore`, docs/zarr-profile.md) straight from the quantized
    codes and names it in the manifest. ``container`` writes the ``.xue``
    as well, from the same codes, so the two agree by construction and the
    bundle's bytes do not depend on whether a store was asked for. Without
    the container no ``.xue`` is written at all: the manifest names the
    store alone, so a run publishes one delivery. A build needs one of the
    two.

    ``partial`` is one job of a fanned-out publish (``build-bin --bundles``):
    each shared store gets a partial group document
    (``zarr.part.<first bundle>.json``) instead of its ``zarr.json``, the
    entries carry that part's CRC-32, and ``assemble-run`` writes the group
    documents. Otherwise the group documents are written here, last, and
    the parts removed.
    """
    if profile not in PROFILES:
        raise ConversionError(f"unknown profile: {profile}")
    if not container and not zarr:
        raise ConversionError("a build without the container needs the store: pass zarr=True")
    source = source_spec(model)
    if bundle_ids is not None:
        unsupported = [bundle_id for bundle_id in bundle_ids if bundle_id not in published_bundle_ids(source)]
        if unsupported or not bundle_ids:
            raise ConversionError(
                f"{source.manifest_model} publishes {list(published_bundle_ids(source))}, not {list(bundle_ids)}"
            )
    zstd_version = zstdcli.zstd_version()
    codebooks = PROFILES[profile]

    if source.series_file:
        discovered = discover_series(
            input_path, source, bundle_ids, require_complete=require_complete, last_hour=last_hour
        )
    else:
        discovered = discover_records(input_path, source, bundle_ids)
    variable_ids = discovered.variable_ids
    per_file = discovered.per_file
    available_vector_ids = discovered.available_vector_ids
    available_derived_ids = discovered.available_derived_ids
    available_composite_ids = discovered.available_composite_ids
    available_volume_ids = discovered.available_volume_ids
    producer_versions = discovered.producer_versions
    variable_families = discovered.variable_families

    unit_seconds, offsets, run_time = _time_axis(
        per_file, variable_ids[0], source, require_complete=require_complete, expected_hours=expected_hours
    )

    # Every published variable's grid family: for a variable the converter
    # derives (the vapour flux pair, the rate) the family its bundle's
    # inputs are read from, for one it reads its own. None throughout on
    # every source but CFSv2, so there is one family and one grid as there
    # always was.
    for bundle_id in published_bundle_ids(source):
        family = bundle_grid_family(source, bundle_id)
        for variable_id in bundle_variable_ids(bundle_id):
            variable_families[variable_id] = family
    grids = _family_grids(
        source,
        variable_ids,
        variable_families,
        discovered.grid_path,
        require_complete=require_complete,
        bbox=bbox,
    )
    # The run-level grid — the build report's, and the one every source with
    # a single grid uses throughout.
    grid = next(iter(grids.values()))

    def grid_for(variable_id: str) -> GridInfo:
        """The grid one published variable's planes are on."""
        return grids[variable_families.get(variable_id)]

    read_grids = {variable_id: grid_for(variable_id) for variable_id in variable_ids}

    codes_by_offset, stats = _quantize_run(
        discovered,
        read_grids,
        work_root or output_dir,
        codebooks,
        source,
        unit_seconds=unit_seconds,
        run_time=run_time,
    )

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
        variable_id
        for bundle_id in available_vector_ids + available_composite_ids + available_volume_ids
        for variable_id in bundle_variable_ids(bundle_id)
    )
    # Scalars that also ship a poster — every published scalar whose
    # companions name one, which leaves out the contour-drawn pressure
    # family.
    companion_variable_ids = tuple(
        variable_id for variable_id in scalar_variable_ids if "poster" in variable_spec(variable_id).companions
    )

    variable_offsets = _variable_offsets(source, offsets, scalar_variable_ids, encoded_variable_ids)

    poster_reports = _write_posters(
        output_dir,
        companion_variable_ids,
        codes_by_offset,
        variable_offsets,
        grid_for,
        run_time=run_time,
        profile=profile,
        source=source,
        unit_seconds=unit_seconds,
        producer_versions=producer_versions,
    )

    # Full-resolution canonical bundles (one per scalar variable plus one per
    # two-variable vector bundle) and the resolution ladder: one rendition
    # per factor of the source's `variant_factors` (the half alone for most
    # sources; half and quarter on the satellite disks), each
    # decimated from the already-quantized full codes exactly like the
    # posters, same temporal structure; the rung's grid is embedded in the
    # variant bundle's own metadata, the manifest carries the tier list in
    # ascending factor order.
    #
    # All bundles are written concurrently: compression funnels through one
    # shared machine-sized zstd pool, while the writer pool lets one bundle's
    # serial tail (container write, read-back verify) overlap another's
    # compression. Each job materializes its raw payloads itself, so at most
    # _BUNDLE_WRITERS bundles' payloads are alive at once.
    variant_factors = source.variant_factors if not skip_variants else ()
    variant_grids, variant_codes_by_offset = build_ladder(
        variant_factors, grids, codes_by_offset, offsets, grid_for, profile
    )
    shared_stores = _open_shared_stores(output_dir, variant_factors, zarr=zarr, partial=partial)

    # Submit largest first — the full tier, then each rung of the
    # ladder in turn — so the volume, composite and vector bundles' long
    # compression starts at once; reports keep the
    # scalars-vectors-composites-volumes manifest order and, within a
    # bundle, the ladder's ascending factor order regardless.
    submit_order = available_volume_ids + available_composite_ids + available_vector_ids + scalar_variable_ids
    report_order = scalar_variable_ids + available_vector_ids + available_composite_ids + available_volume_ids
    bundle_reports, variant_reports, store_writes = _write_bundles(
        source=source,
        run_time=run_time,
        profile=profile,
        unit_seconds=unit_seconds,
        producer_versions=producer_versions,
        output_dir=output_dir,
        zstd_level=zstd_level,
        container=container,
        zarr=zarr,
        series=series,
        shared_stores=shared_stores,
        grids=grids,
        variant_grids=variant_grids,
        variable_families=variable_families,
        variable_offsets=variable_offsets,
        codes_by_offset=codes_by_offset,
        variant_codes_by_offset=variant_codes_by_offset,
        variant_factors=variant_factors,
        submit_order=submit_order,
        report_order=report_order,
    )
    _finalize_stores(
        shared_stores, store_writes, report_order, bundle_reports, variant_reports, variant_factors, partial=partial
    )

    report = build_report(
        output_dir=output_dir,
        source=source,
        grid=grid,
        profile=profile,
        zstd_level=zstd_level,
        zstd_version=zstd_version,
        bundle_reports=bundle_reports,
        variant_reports=variant_reports,
        poster_reports=poster_reports,
        stats=stats,
        available_vector_ids=available_vector_ids,
    )
    _check_quantization(stats, encoded_variable_ids, codebooks)

    if manifest_path is not None:
        write_manifest(
            manifest_path,
            run_time=run_time,
            source=source,
            bundle_reports=bundle_reports,
            poster_reports=poster_reports,
            variant_reports=variant_reports,
            grids=grids,
            offsets=offsets,
            unit_seconds=unit_seconds,
            expected_hours=expected_hours,
            bundle_ids=bundle_ids,
            force=force,
            latest_path=latest_path,
            run_id=run_id,
        )
    return report

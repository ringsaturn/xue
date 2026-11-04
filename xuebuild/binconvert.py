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
import os
import shutil
import tempfile
import zlib
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC
from pathlib import Path
from typing import Any

import numpy as np

from . import binformat, temporal, zarrstore, zstdcli
from .bundles import (  # noqa: F401  (the bundle table's names, re-exported for its readers)
    BUNDLES,
    CAT_SHEAR_LAYERS,
    COMPOSITE_BUNDLES,
    DERIVED_SCALARS,
    DERIVED_VECTORS,
    PRECIPITATION_RATE_BUNDLE_ID,
    SWELL_BUNDLE_ID,
    VECTOR_BUNDLES,
    VOLUME_BUNDLES,
    WAVE_BUNDLE_ID,
    WIND_100M_BUNDLE_ID,
    WIND_100M_COMPONENT_IDS,
    WIND_80M_BUNDLE_ID,
    WIND_80M_COMPONENT_IDS,
    WIND_BUNDLE_ID,
    WIND_COMPONENT_IDS,
    WIND_WAVE_BUNDLE_ID,
    bundle_variable_ids,
    cat_level,
    derivation,
    is_derived_scalar,
    theta_e_level,
    vapour_flux_level,
    vector_input_ids,
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
    analysis_optional_ids,
    bundle_grid_family,
    bundle_input_ids,
    discover_frames,
    grid_family_of,
    prepare_frames_all,
    published_bundle_ids,
    series_lead_seconds,
    store_dimensions,
)
from .errors import ConversionError, DownloadError
from . import rules
from .gdal import (
    inspect_grib_multi,
    require_command,
    run_command,
)
from .grid import GridInfo, crop_grid, grid_info
from .manifest import (
    build_bin_manifest,
    write_bin_manifest,
    write_pointer_to_manifest,
)
from .metadata import axis_unit_seconds, build_metadata, lead_hours
from .model import GRIB_PLANE_SOURCE, PlaneSource, SourceFrame
from .observation import inspect_observation
from .poster import encode_poster
from .quantize import PROFILES, PrecipitationCodebook, TemperatureCodebook
from .sources import SourceSpec, family_frame_path, source_spec
from .variables import VARIABLES, variable_spec

LOG = logging.getLogger(__name__)


def _predictor(variable_id: str) -> int:
    """The container predictor the variable's ``predictor`` rule names:
    chunks chain against the previous frame, or stack the codes raw."""
    return binformat.PREDICTOR_RAW if variable_spec(variable_id).predictor == "raw" else binformat.PREDICTOR_PREVIOUS

# The resolution ladder's tier names by decimation factor: the suffix of
# the rendition's files (`<bundle>.half.xue`, the shared `bundles.half.zarr`) and its
# STAC `xue:tier`. A source's `SourceSpec.variant_factors` picks its rungs
# (the half alone for most grids, the half and the quarter on the
# satellite disks and the 0.1° IFS grids); the eighth is named because a
# run published with it stays readable. Any other factor is a registry bug,
# not a runtime case.
VARIANT_TIERS: dict[int, str] = {2: "half", 4: "quarter", 8: "eighth"}

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


def variant_tier(factor: int) -> str:
    """The tier name of the rendition decimated by ``factor``: ``half``,
    ``quarter`` or ``eighth`` (:data:`VARIANT_TIERS`). A factor the ladder
    has no name for is a registry bug, and raised as one."""
    try:
        return VARIANT_TIERS[factor]
    except KeyError:
        raise ValueError(f"no resolution tier is decimated by {factor}") from None


def _variant_grid(grid: GridInfo, factor: int) -> GridInfo:
    """The grid of the rendition decimated by ``factor``: :meth:`GridInfo.decimated`
    applied log2(factor) times, so 3000 rows go 1500, 750, 375 and the
    721-row production grid 361, 181, 91 (a ceiling at every halving)."""
    for _ in range(factor.bit_length() - 1):
        grid = grid.decimated()
    return grid


def _bundle_tile(tile: tuple[int, int], grid: GridInfo, *, factor: int) -> tuple[int, int]:
    """The tile size one bundle is cut with: the source tile divided by the
    rendition's decimation ``factor`` (1 for the full tier), rounded up.

    A reduced-resolution variant divides the source tile by its factor, so
    tile number n covers the same ground in every tier and a viewport keeps
    its tile rectangle across a tier switch; the ceiling at each rung is the
    ceiling of the rung before halved (``ceil(ceil(n / 2) / 2) == ceil(n /
    4)``), so the quarter's tile is the half's halved. Either size is then
    clamped to the grid, because the format requires ``1 <= tile <= grid``
    so that a single-tile file states its grid size exactly — and a regional
    crop is routinely smaller than the source's tile (a six-degree showcase
    window is 24 x 24 cells against the 0.25-degree grid's 48 x 52 tile).
    Clamping makes such a file one tile, which is the right answer: there is
    nothing left to subdivide.
    """
    tile = ((tile[0] + factor - 1) // factor, (tile[1] + factor - 1) // factor)
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
    predictors = {numeric_ids[variable_id]: _predictor(variable_id) for variable_id in variable_ids}
    planes = {
        offset: {numeric_ids[variable_id]: codes[offset][variable_id] for variable_id in variable_ids}
        for offset in offsets
    }
    return temporal.build_chunks(offsets, planes, tiles, predictors)


def _decimate_codes(codes: np.ndarray, grid: GridInfo, factor: int = 2) -> np.ndarray:
    """Reduced-resolution copy of one quantized plane: every ``factor``-th
    row and column from the origin (rows/columns 0, 2, 4, ... at the half),
    matching the poster decimation so every tier shares the same sample
    sites. Decimating by 4 is decimating by 2 twice, so a rung is always
    taken from the full plane and never from the rung before."""
    return np.ascontiguousarray(codes.reshape(grid.height, grid.width)[::factor, ::factor]).ravel()


def _decimate_peak_codes(codes: np.ndarray, grid: GridInfo, factor: int, nodata_code: int) -> np.ndarray:
    """Reduced-resolution copy of one quantized plane that keeps peaks: at
    each site :func:`_decimate_codes` keeps (rows and columns 0, ``factor``,
    2 ``factor``, ...), the largest code in the window of ``factor // 2``
    rows and columns either side of it, clipped at the plane's edges. The
    windows overlap by a row and a column, so every cell of the full plane
    reaches a site, and a peak moves at most half a reduced cell. A
    linear codebook's codes rise with the value; ``nodata_code`` counts
    below every code and is the site's only when its whole window is
    nodata."""
    half = factor // 2
    plane = codes.reshape(grid.height, grid.width).astype(np.int16)
    plane[plane == nodata_code] = -1
    padded = np.pad(plane, half, constant_values=-1)
    peak = np.full(((grid.height + factor - 1) // factor, (grid.width + factor - 1) // factor), -1, dtype=np.int16)
    for row in range(2 * half + 1):
        for column in range(2 * half + 1):
            np.maximum(peak, padded[row : row + grid.height : factor, column : column + grid.width : factor], out=peak)
    peak[peak < 0] = nodata_code
    return np.ascontiguousarray(peak.astype(np.uint8)).ravel()


def _decimate_variant_codes(variable_id: str, codes: np.ndarray, grid: GridInfo, factor: int, profile: str) -> np.ndarray:
    """One plane's reduced tier by the variable's ``decimation`` rule: the
    peak-keeping decimation (:func:`_decimate_peak_codes`) for a variable
    whose full tier is already the peak of the source pixels and whose
    one-cell hot spots must survive the half tier — the 3.9 µm window,
    published to show fires — or the site's own code."""
    if variable_spec(variable_id).decimation == "peak":
        return _decimate_peak_codes(codes, grid, factor, PROFILES[profile][variable_id].nodata_code)
    return _decimate_codes(codes, grid, factor)


def _playback_bandwidth(byte_length: int, frame_count: int, *, fps: float = 12.0) -> int:
    """HLS STREAM-INF style bandwidth hint: average bits per second needed to
    keep up with the 12 fps playback rate while downloading the whole tier."""
    return max(1, round(byte_length * 8 * fps / max(1, frame_count)))


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


def _bundle_manifest_entry(
    bundle: dict[str, Any],
    manifest_dir: Path,
    poster_report: dict[str, Any] | None,
    variant_reports: list[dict[str, Any]] | None = None,
    *,
    grid: GridInfo,
) -> dict[str, Any]:
    entry = {
        "variable": bundle["variable"],
        # The bundle's full grid, the same block the store's
        # ``attributes.xue.grid`` carries, so the STAC Item states the
        # authoritative grid instead of un-decimating a poster.
        "grid": grid.metadata(),
        **_container_fields(bundle, manifest_dir),
    }
    if variant_reports:
        # Resolution ladder: STREAM-INF style alternate renditions of the
        # same variable; the top-level entry stays the canonical full-res tier.
        entry["variants"] = [_variant_manifest_entry(variant, manifest_dir) for variant in variant_reports]
    if poster_report is not None:
        entry["poster"] = {
            "path": Path(poster_report["path"]).relative_to(manifest_dir).as_posix(),
            "width": poster_report["width"],
            "height": poster_report["height"],
            "byteLength": poster_report["byteLength"],
            "crc32": poster_report["crc32"],
            "metadataJson": poster_report["metadataJson"],
        }
    if "zarr" in bundle:
        entry["zarr"] = _zarr_descriptor(bundle["zarr"], manifest_dir)
    if "series" in bundle:
        entry["series"] = _zarr_descriptor(bundle["series"], manifest_dir)
    return entry


def _variant_manifest_entry(variant: dict[str, Any], manifest_dir: Path) -> dict[str, Any]:
    """One tier's manifest entry, in the key order the native encoder writes
    (the manifests are compared byte for byte): the container's `path` first
    and its `byteLength` / `crc32` after the grid, or none of the three on a
    tier built without a container."""
    container = _container_fields(variant, manifest_dir)
    return {
        **({"path": container["path"]} if container else {}),
        "width": variant["width"],
        "height": variant["height"],
        **({"byteLength": container["byteLength"], "crc32": container["crc32"]} if container else {}),
        "bandwidth": variant["bandwidth"],
        **({"zarr": _zarr_descriptor(variant["zarr"], manifest_dir)} if "zarr" in variant else {}),
    }


def _container_fields(report: dict[str, Any], manifest_dir: Path) -> dict[str, Any]:
    """The manifest's ``path`` / ``byteLength`` / ``crc32`` unit for a bundle
    or variant report — or nothing, for a report built without a container
    (``container: False``), whose store is the one delivery."""
    if not report.get("container", True):
        return {}
    return {
        "path": Path(report["output"]).relative_to(manifest_dir).as_posix(),
        "byteLength": report["byteLength"],
        "crc32": report["crc32"],
    }


def _delivered_byte_length(report: dict[str, Any]) -> int:
    """What a bundle or tier report weighs as delivered: its container where
    one was written, its store otherwise."""
    if report.get("container", True):
        return int(report["byteLength"])
    return int(report["zarr"]["byteLength"])


def _zarr_descriptor(report: dict[str, Any], manifest_dir: Path) -> dict[str, Any]:
    """The manifest's ``zarr`` descriptor from a bundle report's ``zarr``
    block (the store's absolute path, its size and the CRC-32 of its root
    document, as `zarrstore.write_bundle_arrays` reports them)."""
    return {
        "path": Path(report["path"]).relative_to(manifest_dir).as_posix(),
        "byteLength": report["byteLength"],
        "crc32": report["crc32"],
    }


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
        # A series-file source is one NetCDF file holding a whole variable's
        # series, one band per time (xue/observation.py): the CMA mosaic's
        # local archive file, the window the JMA fetch wrote, a satellite
        # window's channels, or the forecast run om2nc resampled off the
        # Open-Meteo bucket. There are no records to match. An observation's
        # own times are the axis, gaps included, with no published cadence to
        # validate against; a forecast's are its lead times and are validated
        # below like any cycle's. (The MRMS observation is one GRIB per
        # frame and takes the record path below, re-keyed onto its window's
        # axis.) A run directory holds one such file per variable — or one
        # file for its one variable (observation.series_files). Only the
        # variables the requested bundles carry are read: a satellite
        # window's unpublished channels feed its composite in the fetch
        # stage and are never quantized here.
        if not isinstance(input_path, Path):
            raise ConversionError(f"a {source.manifest_model} build takes one NetCDF series, or the directory holding them")
        if require_complete and not source.fetched:
            raise ConversionError(f"{source.manifest_model} has no complete run to require")
        requested_bundle_ids = tuple(
            bundle_id for bundle_id in published_bundle_ids(source) if bundle_ids is None or bundle_id in bundle_ids
        )
        # What the series must carry: a composite's components, which the
        # fetch stage produced and wrote as series of their own, and every
        # other bundle's *inputs* — the question the GRIB path asks too, and
        # the one that matters on a forecast series, where `prate` is
        # derived from an `apcp` series (`interval_precipitation`). Then the
        # analysis-optional inputs sort to the back, so the first variable
        # is one no frame of the run can lack and can key the axis; the GRIB
        # path sorts its own the same way.
        series_variable_ids = tuple(
            sorted(
                dict.fromkeys(
                    variable_id
                    for bundle_id in requested_bundle_ids
                    for variable_id in (
                        bundle_variable_ids(bundle_id)
                        if bundle_id in COMPOSITE_BUNDLES
                        else bundle_input_ids(source, bundle_id)
                    )
                ),
                key=lambda variable_id: variable_id in source.optional_at_analysis,
            )
        )
        series = inspect_observation(input_path, source, series_variable_ids)
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
        variable_ids = series_variable_ids
        # A vector bundle ships when the series carries every input it is
        # built from — the same rule the GRIB path applies to a run's first
        # file, asked of the files the fetch wrote.
        available_vector_ids = tuple(
            bundle_id
            for bundle_id in requested_bundle_ids
            if bundle_id in VECTOR_BUNDLES
            and all(variable_id in series.datasets for variable_id in vector_input_ids(bundle_id))
        )
        available_derived_ids: tuple[str, ...] = ()
        available_composite_ids = tuple(bundle_id for bundle_id in requested_bundle_ids if bundle_id in COMPOSITE_BUNDLES)
        available_volume_ids = tuple(bundle_id for bundle_id in requested_bundle_ids if bundle_id in VOLUME_BUNDLES)
        drop_ids: frozenset[str] = frozenset()
        grid_path = series.dataset
        plane_source: PlaneSource | dict[str, PlaneSource] = series.plane_sources
        producer_versions = series.producers
        # No series-file source has a grid family: one run, one grid.
        variable_families: dict[str, str | None] = {}
    else:
        paths = discover_frames(input_path, source)
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
        # Which grid family each input is read from — None for every input of
        # every source but CFSv2, whose pressure-level inputs sit in a frame
        # file of their own on a grid of their own (sources.CompanionFile).
        input_families = {
            variable_id: grid_family_of(source, variable_id) for variable_id in source.input_variable_ids
        }
        reference_frames: dict[str, SourceFrame] = {}
        for family in dict.fromkeys(input_families[variable_id] for variable_id in inspect_ids):
            reference_frames.update(
                inspect_grib_multi(
                    family_frame_path(paths[0], family),
                    tuple(
                        variable_id for variable_id in inspect_ids if input_families[variable_id] == family
                    ),
                    optional_ids=source.optional_at_analysis + derivation_only_ids,
                )
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
        # A volume's levels are every one of them an input the run must
        # carry (none is optional), so a listed volume is built from what
        # the first file was found to hold.
        available_volume_ids = tuple(
            bundle_id
            for bundle_id in published_bundle_ids(source)
            if bundle_id in VOLUME_BUNDLES
            and (bundle_ids is None or bundle_id in bundle_ids)
            and all(variable_id in reference_frames for variable_id in VOLUME_BUNDLES[bundle_id])
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
                key=lambda variable_id: (
                    VARIABLES[variable_id].static,
                    variable_id in source.optional_at_analysis,
                ),
            )
        )
        if not variable_ids or variable_ids[0] in source.optional_at_analysis or VARIABLES[variable_ids[0]].static:
            raise ConversionError(
                f"a {source.manifest_model} build needs at least one variable present in every file, "
                f"including the analysis; {list(variable_ids)} is not enough"
            )
        per_file = prepare_frames_all(
            paths,
            variable_ids,
            source.optional_at_analysis,
            reference_frames,
            source.cadence_seconds,
            families=input_families,
            static_ids=tuple(variable_id for variable_id in variable_ids if VARIABLES[variable_id].static),
        )
        available_composite_ids = ()
        grid_path = paths[0]
        plane_source = GRIB_PLANE_SOURCE
        producer_versions = {}
        variable_families = dict(input_families)

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
    run_time = per_file[0][variable_ids[0]].run_time.astimezone(UTC)
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

    # Every published variable's grid family: for a variable the converter
    # derives (the vapour flux pair, the rate) the family its bundle's
    # inputs are read from, for one it reads its own. None throughout on
    # every source but CFSv2, so there is one family and one grid as there
    # always was.
    for bundle_id in published_bundle_ids(source):
        family = bundle_grid_family(source, bundle_id)
        for variable_id in bundle_variable_ids(bundle_id):
            variable_families[variable_id] = family
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
    # The run-level grid — the build report's, and the one every source with
    # a single grid uses throughout.
    grid = grids[family_order[0]]

    def grid_for(variable_id: str) -> GridInfo:
        """The grid one published variable's planes are on."""
        return grids[variable_families.get(variable_id)]

    read_grids = {variable_id: grid_for(variable_id) for variable_id in variable_ids}

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
        # frame differences against zero). An Open-Meteo apcp total needs no
        # predecessor plane, only the length of the interval it covers.
        precipitation = _precipitation_plan(source, variable_ids)
        raw_precipitation_id = precipitation[0] if precipitation is not None else None

        def sharing_plan():
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
    # One rung per factor per family: a family's rung is its own grid
    # decimated, so a bundle's tier keeps the tile geometry of its own grid.
    variant_grids = {
        factor: {family: _variant_grid(family_grid, factor) for family, family_grid in grids.items()}
        for factor in variant_factors
    }
    # Iterate the codes actually present per hour: derived prate has no
    # analysis-frame plane on ECMWF/sflux.
    variant_codes_by_offset = {
        factor: {
            offset: {
                variable_id: _decimate_variant_codes(variable_id, codes, grid_for(variable_id), factor, profile)
                for variable_id, codes in codes_by_offset[offset].items()
            }
            for offset in offsets
        }
        for factor in variant_factors
    }

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

            tile = _bundle_tile(family_tile, bundle_grid, factor=factor)
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
                    report["bandwidth"] = _playback_bandwidth(_delivered_byte_length(report), len(bundle_offsets))
                return report

            return writers.submit(job)

        # Submit largest first — the full tier, then each rung of the
        # ladder in turn — so the volume, composite and vector bundles' long
        # compression starts at once; reports keep the
        # scalars-vectors-composites-volumes manifest order and, within a
        # bundle, the ladder's ascending factor order regardless.
        submit_order = available_volume_ids + available_composite_ids + available_vector_ids + scalar_variable_ids
        report_order = scalar_variable_ids + available_vector_ids + available_composite_ids + available_volume_ids
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
        # Always empty: the report keeps the key the native encoder's
        # report still writes.
        "videos": [],
        "byteLength": sum(_delivered_byte_length(bundle) for bundle in bundle_reports),
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
        # The source's core bundles (sources.py core_bundle_ids: the
        # tmp2m/prate pair on a forecast, the reflectivity on a radar
        # mosaic) are what a complete run must publish. A restricted build
        # ships only the bundles it was asked for.
        require_core = bundle_ids is None
        payload = build_bin_manifest(
            run_time,
            bundles=[
                _bundle_manifest_entry(
                    bundle,
                    manifest_path.parent,
                    poster_reports.get(bundle["variable"]),
                    variant_reports.get(bundle["variable"]),
                    grid=grids[bundle_grid_family(source, bundle["variable"])],
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
            write_pointer_to_manifest(
                latest_path,
                manifest_path,
                run_id=run_id,
                run_time=run_time,
                model=source.manifest_model,
                product=source.product,
            )
            LOG.info("wrote live pointer %s -> run %s", latest_path, run_id)
    return report

"""Build the indicators product: find each source's new run, compute its
line, append it to the month's file, rewrite the season files and, last,
the index (``docs/indicators.md`` §5).

The output directory is the product root (``indicators/``) as the previous
build left it, or as the workflow pulled it from the bucket: ``index.json``,
the month files the new lines go into, and the season files. A month file
that is in the index must be here byte for byte before a line is appended
to it, since the index's offsets into it are permanent.

Sources fail alone: a source whose pointer, manifest, store or weights
cannot be read reports its error in ``sources[]`` and the rest go on. A run
already in the index is skipped; ``force`` recomputes it and must arrive at
the same bytes, or the build fails that source (a line is never rewritten).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..common import crc32_hex, iso_z, write_bytes_atomic
from ..errors import XueError
from ..sources import source_spec
from . import calendar, season, weights as weights_module
from .features import Series, daily_features, frame_steps, round2, summarize
from .regions import REGION_IDS, regions_index, region
from .schema import (
    PRODUCT,
    RANGES,
    SCHEMA_VERSION,
    IndicatorsError,
    encode_json,
    validate_index,
    validate_line,
    validate_season,
)
from .store import Fetch, RunRef, StoreArray, http_fetch, open_array, open_run, read_values, resolve_latest

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://dataset.ringsaturn.me/xue/"
INDEX_NAME = "index.json"
CONTEXT_PATHS = ("context/oni.json",)


@dataclass(frozen=True)
class SourceEntry:
    grid: str
    cadence: str


SOURCES: dict[str, SourceEntry] = {
    "gfs": SourceEntry("0p25", "00, 06, 12, 18 UTC"),
    "ecmwf": SourceEntry("0p25", "00, 06, 12, 18 UTC"),
    "aifs": SourceEntry("0p25", "00, 06, 12, 18 UTC"),
    "ifshres": SourceEntry("0p1", "00, 12 UTC"),
}
"""The sources the product reads, in the order it lists them, with the
weights grid each one's stores are on."""

TEMPERATURE_BUNDLE = "tmp2m"
PRECIPITATION_BUNDLE = "prate"
UNITS = {TEMPERATURE_BUNDLE: "°C", PRECIPITATION_BUNDLE: "mm/h"}

NOTE = (
    "Crop-area-weighted daily weather quantities computed from open model data and open crop maps. "
    "Not an official product of any agency."
)
ATTRIBUTION: tuple[dict[str, str], ...] = (
    {"name": "NOAA NCEP", "data": "GFS forecasts", "license": "Public domain (U.S. Government work)"},
    {"name": "ECMWF", "data": "IFS and AIFS open data", "license": "CC BY 4.0"},
    {"name": "Open-Meteo", "data": "ECMWF IFS HRES as forwarded by Open-Meteo", "license": "CC BY 4.0"},
    {
        "name": "IFPRI",
        "data": "SPAM 2020 v2r2 soybean physical area, doi:10.7910/DVN/SWPENT",
        "license": "CC BY 4.0",
    },
    {"name": "Natural Earth", "data": "admin-1 states and provinces", "license": "Public domain"},
    {"name": "NOAA CPC", "data": "Oceanic Nino Index", "license": "Public domain (U.S. Government work)"},
)
FEATURES: tuple[dict[str, str], ...] = (
    {"id": "t2m_mean", "unit": "°C", "definition": "mean over the day's frames of the area-weighted 2 m temperature"},
    {"id": "t2m_max", "unit": "°C", "definition": "area-weighted mean of each cell's highest 2 m temperature among the day's frames"},
    {"id": "t2m_min", "unit": "°C", "definition": "area-weighted mean of each cell's lowest 2 m temperature among the day's frames"},
    {"id": "precip", "unit": "mm", "definition": "area-weighted precipitation total over the local day"},
    {"id": "dry_frac", "unit": "1", "definition": "crop-area fraction whose daily precipitation is below 1 mm"},
    {"id": "hot30_frac", "unit": "1", "definition": "crop-area fraction whose daily highest 2 m temperature is above 30 °C"},
    {"id": "hot35_frac", "unit": "1", "definition": "crop-area fraction whose daily highest 2 m temperature is above 35 °C"},
    {"id": "gdd", "unit": "°C d", "definition": "growing degree days, max(0, min(t2m_mean, 30) - 10)"},
    {"id": "complete", "unit": "boolean", "definition": "every frame the source's axis places in the day is present"},
)
assert tuple(feature["id"] for feature in FEATURES) == (*RANGES, "complete")


# -- local state ---------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndicatorsError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise IndicatorsError(f"{path} is not a JSON object")
    return payload


def load_index(output_dir: Path) -> dict[str, Any] | None:
    path = output_dir / INDEX_NAME
    if not path.exists():
        return None
    payload = _read_json(path)
    validate_index(payload)
    return payload


def month_path(source: str, run_time: datetime) -> str:
    return f"features/{source}/{run_time:%Y-%m}.jsonl"


def _indexed_runs(index: Mapping[str, Any] | None, source: str) -> dict[str, tuple[str, int, int]]:
    """``run → (path, offset, length)`` of a source's indexed lines."""
    found: dict[str, tuple[str, int, int]] = {}
    for entry in (index or {}).get("files", []):
        if entry["path"].startswith(f"features/{source}/"):
            for run in entry["runs"]:
                found[run["run"]] = (entry["path"], run["offset"], run["length"])
    return found


# -- one line ------------------------------------------------------------------

def _expected_offsets(run: RunRef, temperature: StoreArray) -> list[int] | None:
    """The axis the source publishes for this run, in seconds, so a missing
    frame shows; None (take the axis as given) when the manifest's horizon
    is not on the source's schedule."""
    last_hour = run.forecast_hours or temperature.offsets_seconds[-1] // 3600
    try:
        return [hour * 3600 for hour in source_spec(run.source).forecast_hours(last_hour)]
    except XueError:
        return None


def compute_line(
    run: RunRef,
    grid_weights: weights_module.Weights,
    fetch: Fetch = http_fetch,
    *,
    threads: int | None = None,
) -> dict[str, Any]:
    """One run's line (docs/indicators.md §4.1), validated."""
    arrays: dict[str, StoreArray] = {}
    for bundle_id in (TEMPERATURE_BUNDLE, PRECIPITATION_BUNDLE):
        array = open_array(run, bundle_id, fetch)
        if array.unit != UNITS[bundle_id]:
            raise IndicatorsError(f"{run.source} {bundle_id} is in {array.unit!r}, expected {UNITS[bundle_id]!r}")
        if array.run_time != run.run_time:
            raise IndicatorsError(f"{run.source} {bundle_id} store is of another run")
        weights_module.check_grid(grid_weights, array.grid)
        arrays[bundle_id] = array
    region_ids = [region_id for region_id in REGION_IDS if region_id in grid_weights.regions]
    if not region_ids:
        raise IndicatorsError(f"weights {grid_weights.grid} name none of the product's regions")
    windows = {region_id: (grid_weights.regions[region_id].rows, grid_weights.regions[region_id].cols) for region_id in region_ids}
    options = {} if threads is None else {"threads": threads}
    values = {bundle_id: read_values(array, windows, fetch, **options) for bundle_id, array in arrays.items()}
    temperature, precipitation = arrays[TEMPERATURE_BUNDLE], arrays[PRECIPITATION_BUNDLE]
    expected = _expected_offsets(run, temperature)
    regions: dict[str, Any] = {}
    for region_id in region_ids:
        days = daily_features(
            Series(run.run_time, temperature.offsets_seconds, values[TEMPERATURE_BUNDLE][region_id]),
            Series(run.run_time, precipitation.offsets_seconds, values[PRECIPITATION_BUNDLE][region_id]),
            grid_weights.regions[region_id].weights,
            region(region_id).utc_offset_hours,
            expected,
        )
        regions[region_id] = {"days": days, "summary": summarize(days)}
    line = {
        "schemaVersion": SCHEMA_VERSION,
        "source": run.source,
        "run": run.run,
        "runTime": iso_z(run.run_time),
        "manifest": {"path": run.manifest_path, "crc32": run.manifest_crc32},
        "weights": {"version": grid_weights.version, "grid": grid_weights.grid, "crc32": grid_weights.crc32},
        "calendar": {"version": calendar.VERSION},
        "horizon_days": round2(temperature.offsets_seconds[-1] / 86400),
        "frame_step_hours": frame_steps(temperature.offsets_seconds),
        "regions": regions,
    }
    validate_line(line, REGION_IDS)
    return line


def encode_line(line: Mapping[str, Any]) -> bytes:
    """One JSONL line: the object and its newline."""
    return encode_json(dict(line)) + b"\n"


# -- the build -----------------------------------------------------------------

def parse_round(value: str) -> str | None:
    """``now`` (the live pointers) → None, else a ``YYYYMMDDHH`` run."""
    if value == "now":
        return None
    try:
        datetime.strptime(value, "%Y%m%d%H")
    except ValueError as exc:
        raise IndicatorsError(f"--round must be now or a run, YYYYMMDDHH: {value!r}") from exc
    return value


def _file_entry(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "byteLength": len(data), "crc32": crc32_hex(data)}


def _month_entry(path: str, data: bytes, runs: list[dict[str, Any]]) -> dict[str, Any]:
    return {**_file_entry(path, data), "runs": runs}


def _local_month(output_dir: Path, entry: Mapping[str, Any] | None, path: str) -> tuple[bytes, list[dict[str, Any]]]:
    """A month file as it stands, held to its index entry."""
    local = output_dir / path
    if entry is None:
        if local.exists():
            raise IndicatorsError(f"{path} exists but the index does not list it; pull the live index first")
        return b"", []
    try:
        data = local.read_bytes()
    except OSError as exc:
        raise IndicatorsError(f"{path} is in the index but not here; pull it before appending: {exc}") from exc
    if len(data) != entry["byteLength"] or crc32_hex(data) != entry["crc32"]:
        raise IndicatorsError(f"{path} here is not the file the index describes; pull it again")
    return data, [dict(run) for run in entry["runs"]]


def _load_weights(weights_dir: Path, grid: str, cache: dict[str, Any]) -> weights_module.Weights:
    if grid not in cache:
        try:
            loaded = weights_module.load(weights_dir / f"{grid}.zarr")
        except IndicatorsError as exc:
            cache[grid] = exc
        else:
            if loaded.grid != grid:
                cache[grid] = IndicatorsError(f"{weights_dir / f'{grid}.zarr'} is the {loaded.grid} grid")
            else:
                cache[grid] = loaded
    value = cache[grid]
    if isinstance(value, Exception):
        raise value
    return value


def _gfs_lines(output_dir: Path, files: Mapping[str, bytes]) -> list[dict[str, Any]]:
    """Every GFS line here: the month files on disk, with the ones this
    build is writing taken from memory."""
    paths = {str(path.relative_to(output_dir)) for path in (output_dir / "features" / season.SEASON_SOURCE).glob("*.jsonl")}
    paths |= {path for path in files if path.startswith(f"features/{season.SEASON_SOURCE}/")}
    lines = []
    for path in sorted(paths):
        data = files[path] if path in files else (output_dir / path).read_bytes()
        lines.extend(json.loads(text) for text in data.decode("ascii").splitlines() if text)
    return lines


def _previous_seasons(output_dir: Path) -> dict[season.SeasonKey, dict[str, Any]]:
    found = {}
    for path in sorted((output_dir / "season").glob("*.json")):
        payload = _read_json(path)
        validate_season(payload)
        found[(payload["region"], payload["season"])] = payload
    return found


def build(
    output_dir: Path,
    *,
    sources: Sequence[str] = tuple(SOURCES),
    round_run: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    weights_dir: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
    fetch: Fetch = http_fetch,
    source_grids: Mapping[str, str] | None = None,
    threads: int | None = None,
) -> dict[str, Any]:
    """Run one build round. Returns the report printed by the CLI."""
    unknown = [source for source in sources if source not in SOURCES]
    if unknown:
        raise IndicatorsError(f"unknown indicators sources {unknown}; choose from {', '.join(SOURCES)}")
    weights_dir = weights_dir if weights_dir is not None else output_dir / "weights"
    grids = {source: entry.grid for source, entry in SOURCES.items()}
    grids.update(source_grids or {})
    index = load_index(output_dir)
    previous_files = {entry["path"]: entry for entry in (index or {}).get("files", [])}
    previous_sources = {entry["id"]: entry for entry in (index or {}).get("sources", [])}

    weights_cache: dict[str, Any] = {}
    months: dict[str, tuple[bytes, list[dict[str, Any]]]] = {}
    statuses: dict[str, dict[str, Any]] = {}
    report_sources: list[dict[str, Any]] = []
    weights_seen: dict[str, weights_module.Weights] = {}
    gfs_changed = False

    for source in sources:
        status: dict[str, Any] = {"id": source}
        try:
            run = resolve_latest(source, base_url, fetch) if round_run is None else open_run(source, round_run, base_url, fetch)
            status["run"] = run.run
            indexed = _indexed_runs(index, source)
            if run.run in indexed and not force:
                status["status"] = "skipped"
            else:
                grid_weights = _load_weights(weights_dir, grids[source], weights_cache)
                weights_seen[grid_weights.grid] = grid_weights
                data = encode_line(compute_line(run, grid_weights, fetch, threads=threads))
                path = month_path(source, run.run_time)
                if path not in months:
                    months[path] = _local_month(output_dir, previous_files.get(path), path)
                content, runs = months[path]
                if run.run in indexed:
                    indexed_path, offset, length = indexed[run.run]
                    existing = months[path][0] if indexed_path == path else (output_dir / indexed_path).read_bytes()
                    if existing[offset : offset + length] != data:
                        raise IndicatorsError(f"{source} {run.run} recomputed to different bytes than its indexed line")
                    status.update(status="reproduced", path=indexed_path, offset=offset, length=length)
                else:
                    runs.append({"run": run.run, "offset": len(content), "length": len(data)})
                    months[path] = (content + data, runs)
                    status.update(status="added", path=path, offset=len(content), length=len(data))
                gfs_changed |= source == season.SEASON_SOURCE
            statuses[source] = {"ok": True, "error": None}
        except XueError as exc:
            log.warning("indicators %s: %s", source, exc)
            status.update(status="failed", error=str(exc))
            statuses[source] = {"ok": False, "error": str(exc)}
        report_sources.append(status)

    # The weights block names the version the lines of this build used; a
    # build that loaded none keeps the index's.
    versions = {loaded.version for loaded in weights_seen.values()}
    if len(versions) > 1:
        raise IndicatorsError(f"the weights files disagree on their version: {sorted(versions)}")
    if versions:
        any_weights = next(iter(weights_seen.values()))
        weights_block = {
            "version": any_weights.version,
            "spam": any_weights.spam,
            "files": [{"grid": grid, "path": f"weights/{grid}.zarr"} for grid in sorted({grids[source] for source in SOURCES})],
        }
    elif index is not None:
        weights_block = index["weights"]
    else:
        weights_block = {"version": "none", "spam": "none", "files": []}

    month_bytes = {path: content for path, (content, _runs) in months.items()}
    season_files: dict[str, bytes] = {}
    if gfs_changed:
        built = season.build_seasons(
            _gfs_lines(output_dir, month_bytes), _previous_seasons(output_dir), weights_version=weights_block["version"]
        )
        for (region_id, season_id), payload in built.items():
            validate_season(payload)
            season_files[season.season_path(region_id, season_id)] = encode_json(payload)

    files: dict[str, dict[str, Any]] = {path: dict(entry) for path, entry in previous_files.items()}
    for path, (content, runs) in months.items():
        if runs:
            files[path] = _month_entry(path, content, runs)
    for path, data in season_files.items():
        files[path] = _file_entry(path, data)
    for path in CONTEXT_PATHS:
        local = output_dir / path
        if local.exists():
            files[path] = _file_entry(path, local.read_bytes())

    source_entries = []
    for source in SOURCES:
        if source not in statuses and source not in previous_sources:
            continue
        runs = sorted(_indexed_runs({"files": list(files.values())}, source))
        state = statuses.get(source) or {key: previous_sources[source][key] for key in ("ok", "error")}
        source_entries.append(
            {
                "id": source,
                "grid": grids[source],
                "cadence": SOURCES[source].cadence,
                "latestRun": runs[-1] if runs else None,
                **state,
            }
        )
    new_index = {
        "schemaVersion": SCHEMA_VERSION,
        "product": PRODUCT,
        "note": NOTE,
        "attribution": [dict(entry) for entry in ATTRIBUTION],
        "regions": regions_index(),
        "features": [dict(entry) for entry in FEATURES],
        "calendar": calendar.calendar_index(),
        "weights": weights_block,
        "sources": source_entries,
        "files": [files[path] for path in sorted(files)],
    }
    validate_index(new_index)
    index_bytes = encode_json(new_index)
    index_path = output_dir / INDEX_NAME
    index_changed = not index_path.exists() or index_path.read_bytes() != index_bytes

    written: list[str] = []
    if not dry_run:
        # Payload before the object that names it: month files, then the
        # season files, then the index, the product's one entry point.
        for path, (content, runs) in sorted(months.items()):
            local = output_dir / path
            if runs and (not local.exists() or local.read_bytes() != content):
                write_bytes_atomic(local, content)
                written.append(path)
        for path, data in sorted(season_files.items()):
            local = output_dir / path
            if not local.exists() or local.read_bytes() != data:
                write_bytes_atomic(local, data)
                written.append(path)
        if index_changed:
            write_bytes_atomic(index_path, index_bytes)
            written.append(INDEX_NAME)
    return {
        "product": PRODUCT,
        "built": iso_z(datetime.now(UTC)),
        "round": round_run or "now",
        "dryRun": dry_run,
        "sources": report_sources,
        "season": sorted(season_files),
        "index": {"changed": index_changed, "byteLength": len(index_bytes), "crc32": crc32_hex(index_bytes)},
        "written": written,
    }

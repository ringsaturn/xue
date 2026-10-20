"""Building a case's ``river/`` directory (``docs/river.md``) from a work
directory: the case's ``case.json`` there names the frames, the gauges and
the flood extents, and a river definition
(``showcase/river-cases/<id>.json``) picks which of them to publish.

A network case writes the reaches as GeoParquet in Hilbert order, one
network store per track (the analysis window, and each forecast cycle
named), the gauges' observed series as ``gauges.jsonl`` and the index; a
grid case, whose field is the showcase case's own, writes the gauges and
the extents alone. The index is written last and read back through
:func:`xuebuild.river.schema.read_index`.
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from ..common import crc32_hex, iso_z
from ..pointproduct import encode_json
from . import geoparquet
from .network import ROW_GROUP_ROWS, geometry_descriptor, write_network_store
from .schema import GAUGES_FILENAME, INDEX_FILENAME, KINDS, SLUG, RiverIndexError, read_index

LOG = logging.getLogger(__name__)

GEOMETRY_FILENAME = "reaches.parquet"

_DEFINITION_KEYS = {"id", "kind", "work", "output", "model", "tracks", "gauges", "extent", "credits", "disclaimer"}


class RiverBuildError(RiverIndexError):
    """A river definition or work directory that cannot be built."""


@dataclass(frozen=True)
class Track:
    id: str
    kind: str
    product: str
    cycle: datetime | None


@dataclass(frozen=True)
class Definition:
    id: str
    kind: str
    work: Path
    output: Path
    model: str
    tracks: tuple[Track, ...]
    gauges: tuple[str, ...]
    extent: tuple[str, ...]
    credits: tuple[str, ...] | None
    disclaimer: str | None


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RiverBuildError(f"{label} must be an ISO 8601 UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError as exc:
        raise RiverBuildError(f"{label} is not a valid timestamp") from exc


def load_definition(path: Path, *, work: Path | None = None, output: Path | None = None) -> Definition:
    """A river definition, ``work`` and ``output`` resolved against the
    working directory unless given."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RiverBuildError(f"cannot read river definition {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) - _DEFINITION_KEYS:
        raise RiverBuildError(f"{path}: a river definition is an object of {sorted(_DEFINITION_KEYS)}")
    case_id = payload.get("id")
    if not isinstance(case_id, str) or not SLUG.match(case_id):
        raise RiverBuildError(f"{path}: id must be a slug")
    kind = payload.get("kind")
    if kind not in KINDS:
        raise RiverBuildError(f"{case_id}: kind must be one of {KINDS}")
    for key in ("work", "output"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise RiverBuildError(f"{case_id}: {key} must name a directory")
    destination = Path(output or payload["output"])
    if destination.name != "river":
        raise RiverBuildError(f"{case_id}: output must be a case's river/ directory, not {destination}")
    tracks: list[Track] = []
    raw_tracks = payload.get("tracks", [])
    if (kind == "network") != bool(raw_tracks):
        raise RiverBuildError(f"{case_id}: a network case names its tracks, a grid case none")
    for position, item in enumerate(raw_tracks):
        where = f"{case_id}: tracks[{position}]"
        if not isinstance(item, dict) or set(item) - {"id", "kind", "product", "cycle"}:
            raise RiverBuildError(f"{where} must be {{id, kind, product, cycle?}}")
        if not isinstance(item.get("id"), str) or not SLUG.match(item["id"]):
            raise RiverBuildError(f"{where}.id must be a slug")
        if item.get("kind") not in ("analysis", "forecast"):
            raise RiverBuildError(f"{where}.kind must be analysis or forecast")
        if not isinstance(item.get("product"), str) or not item["product"]:
            raise RiverBuildError(f"{where}.product must be a string")
        cycle = None
        if item["kind"] == "forecast":
            cycle = _parse_time(item.get("cycle"), f"{where}.cycle")
        elif "cycle" in item:
            raise RiverBuildError(f"{where}: an analysis track names no cycle")
        tracks.append(Track(item["id"], item["kind"], item["product"], cycle))
    if len({track.id for track in tracks}) != len(tracks):
        raise RiverBuildError(f"{case_id}: track ids must be unique")
    model = payload.get("model", "")
    if kind == "network" and (not isinstance(model, str) or not model):
        raise RiverBuildError(f"{case_id}: a network case names its model")
    gauges = payload.get("gauges", [])
    extent = payload.get("extent", [])
    for key, value in (("gauges", gauges), ("extent", extent)):
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            raise RiverBuildError(f"{case_id}: {key} must be a list of strings")
    credits = payload.get("credits")
    if credits is not None and (not isinstance(credits, list) or any(not isinstance(item, str) for item in credits)):
        raise RiverBuildError(f"{case_id}: credits must be a list of strings")
    disclaimer = payload.get("disclaimer")
    if disclaimer is not None and (not isinstance(disclaimer, str) or not disclaimer.strip()):
        raise RiverBuildError(f"{case_id}: disclaimer must be a string")
    return Definition(
        id=case_id,
        kind=kind,
        work=Path(work or payload["work"]),
        output=destination,
        model=model,
        tracks=tuple(tracks),
        gauges=tuple(gauges),
        extent=tuple(extent),
        credits=tuple(credits) if credits is not None else None,
        disclaimer=disclaimer,
    )


# -- gauges --------------------------------------------------------------------


def read_series(path: Path) -> list[tuple[str, float | None]]:
    """A ``time,value`` CSV: ISO UTC times, an empty value for a gap."""
    rows: list[tuple[str, float | None]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or reader.fieldnames[:2] != ["time", "value"]:
            raise RiverBuildError(f"{path} must have the columns time,value")
        for row in reader:
            moment = iso_z(_parse_time(row["time"], f"{path} time"))
            text = (row["value"] or "").strip()
            rows.append((moment, float(text) if text else None))
    return rows


def series_line(gauge_id: str, discharge: list[tuple[str, float | None]], stage: list[tuple[str, float | None]] | None = None, stage_unit: str | None = None) -> dict[str, Any]:
    """One ``gauges.jsonl`` object: the union of the discharge and stage
    times, ``null`` where one of them has no reading."""
    flows = dict(discharge)
    levels = dict(stage or [])
    times = sorted(set(flows) | set(levels))
    line: dict[str, Any] = {"id": gauge_id, "time": times, "value": [flows.get(moment) for moment in times]}
    if stage is not None:
        line["stage"] = [levels.get(moment) for moment in times]
        line["stageUnit"] = stage_unit
    return line


def write_lines(path: Path, lines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Write one JSON object per line and return each id's byte span (the
    object alone, without its newline)."""
    spans: dict[str, dict[str, Any]] = {}
    data = bytearray()
    for line in lines:
        encoded = encode_json(line)
        spans[line["id"]] = {"path": path.name, "offset": len(data), "length": len(encoded)}
        data += encoded + b"\n"
    path.write_bytes(bytes(data))
    return spans


def _note(qualifiers: str) -> str:
    codes = set(qualifiers.replace(" ", "").split(","))
    if "e" in codes:
        return "USGS, estimated"
    if "P" in codes:
        return "USGS, provisional"
    if "A" in codes:
        return "USGS, approved"
    return "USGS"


def _network_gauge(entry: dict[str, Any], work: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    discharge = read_series(work / entry["series"])
    stage = read_series(work / entry["stage"]) if entry.get("stage") else None
    line = series_line(entry["id"], discharge, stage, entry.get("stage_unit") if stage is not None else None)
    peaks = []
    peak = (entry.get("obs_notes") or {}).get("discharge_peak")
    if peak:
        peaks.append({"time": peak["time"], "value": round(float(peak["value"]), 1), "note": _note(peak.get("qualifiers", ""))})
    gauge = {
        "id": entry["id"],
        "name": entry["name"],
        "lon": round(float(entry["lon"]), 6),
        "lat": round(float(entry["lat"]), 6),
        "source": entry["source"],
        "unit": entry.get("value_unit", "m3/s"),
        "peaks": peaks,
    }
    return gauge, [line]


def _grid_gauge(entry: dict[str, Any], work: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not entry.get("series"):
        raise RiverBuildError(f"gauge {entry['id']} has no observed series to carry")
    lines = [series_line(entry["id"], read_series(work / entry["series"]))]
    outflow = (entry.get("series_extra") or {}).get("outflow")
    if outflow:
        lines.append(series_line(f"{entry['id']}.outflow", read_series(work / outflow)))
    peaks = []
    for report in entry.get("reported", []):
        peak: dict[str, Any] = {}
        if report.get("time"):
            peak["time"] = report["time"]
        peak["value"] = report["value"]
        if report.get("unit") and report["unit"] != "m3/s":
            peak["unit"] = report["unit"]
        source = report.get("source", "")
        # The source's citation up to its first comma: the agency and the
        # report, not the URL and the caveat, which are in the credits.
        peak["note"] = f"{report['what']} ({source.split(',')[0]})" if source else report["what"]
        peaks.append(peak)
    gauge = {
        "id": entry["id"],
        "name": entry["name"],
        "lon": round(float(entry["lon"]), 6),
        "lat": round(float(entry["lat"]), 6),
        "source": entry["source"],
        "unit": "m3/s",
    }
    if "cell_lon" in entry and "cell_lat" in entry:
        # The model cell the gauge was snapped to: the point to sample the
        # case's field at, since a gauge's own position may fall a cell off
        # the river on a 1.5 km grid.
        gauge["sample"] = [round(float(entry["cell_lon"]), 6), round(float(entry["cell_lat"]), 6)]
    gauge["peaks"] = peaks
    return gauge, lines


# -- building ------------------------------------------------------------------


def _frames(case: dict[str, Any], track: Track) -> list[dict[str, Any]]:
    if track.kind == "analysis":
        frames = [frame for frame in case["frames"] if frame["track"] == "analysis"]
    else:
        frames = [
            frame
            for frame in case["frames"]
            if frame["track"] == "forecast" and frame.get("cycle") and _parse_time(frame["cycle"], "cycle") == track.cycle
        ]
    if not frames:
        raise RiverBuildError(f"track {track.id}: the work directory has no frames for it")
    return sorted(frames, key=lambda frame: _parse_time(frame["valid"], "valid"))


def build(definition: Definition) -> dict[str, Any]:
    """Write the definition's ``river/`` directory and return its index."""
    work = definition.work
    case_path = work / "case.json"
    try:
        case = json.loads(case_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RiverBuildError(f"cannot read {case_path}: {exc}") from exc
    if case.get("kind") != definition.kind:
        raise RiverBuildError(f"{definition.id}: the work directory is a {case.get('kind')} case, not {definition.kind}")
    output = definition.output
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    index: dict[str, Any] = {"version": 1, "kind": definition.kind}
    row_of: dict[int, int] = {}
    if definition.kind == "network":
        reaches = geoparquet.read_reaches(work / case["features"]["file"])
        if np.any(np.diff(reaches.feature_id) <= 0):
            raise RiverBuildError(f"{definition.id}: the work geometry must be in ascending feature_id")
        permutation = geoparquet.hilbert_permutation(reaches)
        sorted_reaches = reaches.take(permutation)
        parquet = geoparquet.write_reaches(output / GEOMETRY_FILENAME, sorted_reaches, row_group_rows=ROW_GROUP_ROWS)
        LOG.info(
            "%s: %d reaches, %d bytes, footer %d, row groups %s",
            parquet.path.name, parquet.rows, parquet.byte_length, parquet.footer_bytes, parquet.row_group_bytes,
        )
        geometry = geometry_descriptor(parquet.path, ROW_GROUP_ROWS, len(sorted_reaches))
        index["geometry"] = geometry
        row_of = {int(feature): row for row, feature in enumerate(sorted_reaches.feature_id)}
        baseflow = np.load(work / case["baseflow"]["file"]).astype(np.float32)
        if baseflow.shape != (len(reaches),):
            raise RiverBuildError(f"{definition.id}: baseflow must hold one value per reach")
        baseflow = baseflow[permutation]
        index["tracks"] = []
        for track in definition.tracks:
            frames = _frames(case, track)
            values = np.stack([np.load(work / frame["file"]).astype(np.float32) for frame in frames])
            if values.shape[1] != len(reaches):
                raise RiverBuildError(f"track {track.id}: a frame does not hold one value per reach")
            valid = [_parse_time(frame["valid"], "valid") for frame in frames]
            run_time = track.cycle or valid[0]
            store = write_network_store(
                output / f"{track.id}.zarr",
                model=definition.model,
                product=track.product,
                run_time=run_time,
                valid_times=valid,
                values=values[:, permutation],
                feature_ids=sorted_reaches.feature_id,
                baseflow=baseflow,
                geometry=geometry,
            )
            LOG.info("%s: %d frames, %d bytes (shard %d)", store.path.name, store.frame_count, store.byte_length, store.shard_bytes)
            index["tracks"].append(
                {
                    "id": track.id,
                    "kind": track.kind,
                    "runTime": iso_z(run_time),
                    "store": {"path": store.path.name, "byteLength": store.byte_length, "crc32": store.crc32},
                }
            )

    known = {entry["id"]: entry for entry in case.get("gauges", [])}
    gauges: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for gauge_id in definition.gauges:
        if gauge_id not in known:
            raise RiverBuildError(f"{definition.id}: the work directory has no gauge {gauge_id}")
        entry = known[gauge_id]
        if definition.kind == "network":
            gauge, own = _network_gauge(entry, work)
            if int(entry["feature_id"]) not in row_of:
                raise RiverBuildError(f"gauge {gauge_id}: reach {entry['feature_id']} is not in the network")
            gauge = {**{key: gauge[key] for key in ("id", "name", "lon", "lat", "source", "unit")}, "feature": row_of[int(entry["feature_id"])], "peaks": gauge["peaks"]}
        else:
            gauge, own = _grid_gauge(entry, work)
        gauges.append(gauge)
        lines.extend(own)
    if gauges:
        spans = write_lines(output / GAUGES_FILENAME, lines)
        for gauge in gauges:
            gauge["series"] = spans[gauge["id"]]
            if f"{gauge['id']}.outflow" in spans:
                gauge["outflow"] = spans[f"{gauge['id']}.outflow"]
    index["gauges"] = [
        {key: gauge[key] for key in ("id", "name", "lon", "lat", "source", "unit", "feature", "sample", "series", "outflow", "peaks") if key in gauge}
        for gauge in gauges
    ]

    extents = {entry["path"]: entry for entry in case.get("extent", [])}
    index["extent"] = []
    for name in definition.extent:
        if name not in extents:
            raise RiverBuildError(f"{definition.id}: the work directory has no extent {name}")
        entry = extents[name]
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work / name, target)
        data = target.read_bytes()
        index["extent"].append(
            {"path": name, "byteLength": len(data), "crc32": crc32_hex(data), "at": entry["at"], "credit": entry["credit"]}
        )

    index["credits"] = list(definition.credits if definition.credits is not None else case.get("credits", []))
    disclaimer = definition.disclaimer or case.get("disclaimer")
    if disclaimer:
        index["disclaimer"] = disclaimer
    index_path = output / INDEX_FILENAME
    index_path.write_bytes(encode_json(index))
    read_index(index_path)
    LOG.info("wrote %s (%d bytes)", index_path, index_path.stat().st_size)
    return index

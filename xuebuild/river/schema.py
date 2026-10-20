"""A case's ``river/index.json`` (``docs/river.md``), version 1: the
validator every writer and reader goes through.

:func:`validate_index` checks the document's shape; :func:`read_index`
reads one from disk, validates it and checks every file it names against
what is beside it — a plain file by length and CRC-32 of its bytes, a
network store by its byte length, the CRC-32 of its group ``zarr.json``
and the group's own block, a gauge's series by the line its byte span
cuts out of ``gauges.jsonl``. Nothing here imports the showcase: the
showcase imports this.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..common import crc32_hex
from ..errors import XueError

INDEX_VERSION = 1
INDEX_FILENAME = "index.json"
GAUGES_FILENAME = "gauges.jsonl"
KINDS = ("network", "grid")
TRACK_KINDS = ("analysis", "forecast")
CRC32 = re.compile(r"^[0-9a-f]{8}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
GAUGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
UNIT = re.compile(r"^[A-Za-z0-9/^.·°%-]+$")

_INDEX_KEYS = {"version", "kind", "geometry", "tracks", "gauges", "extent", "credits", "disclaimer"}
_GAUGE_KEYS = {"id", "name", "lon", "lat", "source", "unit", "feature", "sample", "series", "outflow", "peaks"}
_PEAK_KEYS = {"time", "value", "unit", "note"}
_EXTENT_KEYS = {"path", "byteLength", "crc32", "at", "credit"}
_LINE_KEYS = {"id", "time", "value", "stage", "stageUnit"}


class RiverIndexError(XueError):
    """A river index, or a file it names, that is not usable."""


def _fail(message: str) -> None:
    raise RiverIndexError(message)


def check_path(value: object, label: str) -> str:
    """A path relative to the index: no leading ``/``, no ``..``, no scheme,
    no backslash, no empty component."""
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or ":" in value:
        _fail(f"{label} must be a relative path")
    parts = PurePosixPath(value).parts
    if any(part in ("..", ".", "") for part in parts) or value.endswith("/"):
        _fail(f"{label} must not climb out of the index's directory")
    return value


def _time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail(f"{label} must be an ISO 8601 UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        _fail(f"{label} is not a valid timestamp")
    raise AssertionError


def _number(value: object, label: str, low: float = -math.inf, high: float = math.inf) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        _fail(f"{label} must be a finite number in [{low}, {high}]")
    return float(value)


def _count(value: object, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{label} must be an integer of at least {minimum}")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"{label} must be a non-empty string")
    return value


def _file(value: object, label: str, keys: set[str] = frozenset({"path", "byteLength", "crc32"})) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail(f"{label} must be {{{', '.join(sorted(keys))}}}")
    check_path(value["path"], f"{label}.path")
    _count(value["byteLength"], f"{label}.byteLength", 1)
    if not isinstance(value["crc32"], str) or not CRC32.match(value["crc32"]):
        _fail(f"{label}.crc32 must be 8 lowercase hex characters")
    return value


def _span(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"path", "offset", "length"}:
        _fail(f"{label} must be {{path, offset, length}}")
    if value["path"] != GAUGES_FILENAME:
        _fail(f"{label}.path must be {GAUGES_FILENAME}")
    _count(value["offset"], f"{label}.offset")
    _count(value["length"], f"{label}.length", 2)
    return value


def validate_index(payload: object) -> dict[str, Any]:
    """The document's shape, without touching the disk."""
    if not isinstance(payload, dict) or set(payload) - _INDEX_KEYS:
        _fail(f"a river index is an object of {sorted(_INDEX_KEYS)}")
    if payload.get("version") != INDEX_VERSION:
        _fail(f"river index version must be {INDEX_VERSION}")
    kind = payload.get("kind")
    if kind not in KINDS:
        _fail(f"kind must be one of {KINDS}")
    network = kind == "network"
    if network != ("geometry" in payload) or network != ("tracks" in payload):
        _fail("geometry and tracks are present on a network index and absent on a grid one")

    feature_count = None
    if network:
        geometry = payload["geometry"]
        keys = {"path", "byteLength", "crc32", "featureCount", "rowGroupRows"}
        if not isinstance(geometry, dict) or set(geometry) != keys:
            _fail(f"geometry must be {{{', '.join(sorted(keys))}}}")
        _file({key: geometry[key] for key in ("path", "byteLength", "crc32")}, "geometry")
        if not geometry["path"].endswith(".parquet"):
            _fail("geometry.path must name a .parquet file")
        feature_count = _count(geometry["featureCount"], "geometry.featureCount", 1)
        _count(geometry["rowGroupRows"], "geometry.rowGroupRows", 1)
        tracks = payload["tracks"]
        if not isinstance(tracks, list) or not tracks:
            _fail("tracks must be a non-empty list")
        seen: set[str] = set()
        for position, track in enumerate(tracks):
            where = f"tracks[{position}]"
            if not isinstance(track, dict) or set(track) != {"id", "kind", "runTime", "store"}:
                _fail(f"{where} must be {{id, kind, runTime, store}}")
            if not isinstance(track["id"], str) or not SLUG.match(track["id"]) or track["id"] in seen:
                _fail(f"{where}.id must be a unique slug")
            seen.add(track["id"])
            if track["kind"] not in TRACK_KINDS:
                _fail(f"{where}.kind must be one of {TRACK_KINDS}")
            _time(track["runTime"], f"{where}.runTime")
            _file(track["store"], f"{where}.store")
            if not track["store"]["path"].endswith(".zarr"):
                _fail(f"{where}.store.path must name a .zarr store")

    gauges = payload.get("gauges", [])
    if not isinstance(gauges, list):
        _fail("gauges must be a list")
    seen = set()
    for position, gauge in enumerate(gauges):
        where = f"gauges[{position}]"
        if not isinstance(gauge, dict) or set(gauge) - _GAUGE_KEYS:
            _fail(f"{where} has a key outside {sorted(_GAUGE_KEYS)}")
        gauge_id = gauge.get("id")
        if not isinstance(gauge_id, str) or not GAUGE_ID.match(gauge_id) or gauge_id in seen:
            _fail(f"{where}.id must be a unique identifier")
        seen.add(gauge_id)
        where = f"gauge {gauge_id}"
        for key in ("name", "source"):
            _text(gauge.get(key), f"{where}.{key}")
        if not isinstance(gauge.get("unit"), str) or not UNIT.match(gauge["unit"]):
            _fail(f"{where}.unit must be a unit symbol")
        _number(gauge.get("lon"), f"{where}.lon", -180, 180)
        _number(gauge.get("lat"), f"{where}.lat", -90, 90)
        if network:
            feature = _count(gauge.get("feature"), f"{where}.feature")
            if feature >= feature_count:
                _fail(f"{where}.feature is past the geometry's last row")
        elif "feature" in gauge:
            _fail(f"{where}: feature belongs to a network index only")
        if "sample" in gauge:
            sample = gauge["sample"]
            if not isinstance(sample, list) or len(sample) != 2:
                _fail(f"{where}.sample must be [lon, lat]")
            _number(sample[0], f"{where}.sample[0]", -180, 180)
            _number(sample[1], f"{where}.sample[1]", -90, 90)
        _span(gauge.get("series"), f"{where}.series")
        if "outflow" in gauge:
            _span(gauge["outflow"], f"{where}.outflow")
        peaks = gauge.get("peaks", [])
        if not isinstance(peaks, list):
            _fail(f"{where}.peaks must be a list")
        for index, peak in enumerate(peaks):
            label = f"{where}.peaks[{index}]"
            if not isinstance(peak, dict) or set(peak) - _PEAK_KEYS or "value" not in peak:
                _fail(f"{label} must be {{time?, value, unit?, note?}}")
            if "time" in peak:
                _time(peak["time"], f"{label}.time")
            _number(peak["value"], f"{label}.value")
            if "unit" in peak and (not isinstance(peak["unit"], str) or not UNIT.match(peak["unit"])):
                _fail(f"{label}.unit must be a unit symbol")
            if "note" in peak:
                _text(peak["note"], f"{label}.note")

    extent = payload.get("extent", [])
    if not isinstance(extent, list):
        _fail("extent must be a list")
    for position, item in enumerate(extent):
        where = f"extent[{position}]"
        if not isinstance(item, dict) or set(item) != _EXTENT_KEYS:
            _fail(f"{where} must be {{{', '.join(sorted(_EXTENT_KEYS))}}}")
        _file({key: item[key] for key in ("path", "byteLength", "crc32")}, where)
        if not item["path"].endswith(".geojson"):
            _fail(f"{where}.path must name a .geojson file")
        _time(item["at"], f"{where}.at")
        _text(item["credit"], f"{where}.credit")

    credits = payload.get("credits", [])
    if not isinstance(credits, list) or any(not isinstance(item, str) or not item.strip() for item in credits):
        _fail("credits must be a list of non-empty strings")
    if "disclaimer" in payload:
        _text(payload["disclaimer"], "disclaimer")
    if not gauges and not network and not extent:
        _fail("a grid index carries gauges or extents, or it says nothing")
    return payload


def _check_plain(directory: Path, descriptor: dict[str, Any], label: str) -> bytes:
    path = directory / descriptor["path"]
    if not path.is_file():
        _fail(f"{label} names {descriptor['path']}, which is not there")
    data = path.read_bytes()
    if len(data) != descriptor["byteLength"] or crc32_hex(data) != descriptor["crc32"]:
        _fail(f"{label}: {descriptor['path']} is not the file the index describes (length or CRC-32)")
    return data


def _check_store(directory: Path, track: dict[str, Any], geometry: dict[str, Any]) -> None:
    from .network import NetworkStoreError, read_group  # the store module imports nothing of this one

    store = directory / track["store"]["path"]
    label = f"track {track['id']}"
    if not (store / "zarr.json").is_file():
        _fail(f"{label} names {track['store']['path']}, which is not a store")
    root = (store / "zarr.json").read_bytes()
    length = sum(path.stat().st_size for path in store.rglob("*") if path.is_file())
    if crc32_hex(root) != track["store"]["crc32"] or length != track["store"]["byteLength"]:
        _fail(f"{label}: {track['store']['path']} is not the store the index describes (length or CRC-32)")
    try:
        block = read_group(store)
    except NetworkStoreError as exc:
        raise RiverIndexError(f"{label}: {exc}") from exc
    # The store names its GeoParquet relative to the directory holding it;
    # the index, relative to itself. Both must land on the same file.
    own = {key: value for key, value in block["geometry"].items() if key != "path"}
    if own != {key: value for key, value in geometry.items() if key != "path"} or (
        (store.parent / str(block["geometry"]["path"])).resolve() != (directory / geometry["path"]).resolve()
    ):
        _fail(f"{label}: the store's geometry is not the index's")
    if _time(block["runTime"], f"{label} runTime") != _time(track["runTime"], f"{label}.runTime"):
        _fail(f"{label}: the store's runTime is not the track's")


def _line(data: bytes, span: dict[str, Any], label: str) -> dict[str, Any]:
    end = span["offset"] + span["length"]
    if end > len(data):
        _fail(f"{label} runs past the end of {GAUGES_FILENAME}")
    if span["offset"] and data[span["offset"] - 1 : span["offset"]] != b"\n":
        _fail(f"{label} does not start a line")
    if data[end : end + 1] != b"\n":
        _fail(f"{label} does not end where its line does")
    try:
        line = json.loads(data[span["offset"] : end])
    except (json.JSONDecodeError, UnicodeDecodeError):
        _fail(f"{label} is not one JSON object")
    validate_line(line, label)
    return line


def validate_line(line: object, label: str) -> dict[str, Any]:
    """One line of ``gauges.jsonl``: an id and equally long time and value
    arrays, optionally a stage array of the same length and its unit."""
    if not isinstance(line, dict) or set(line) - _LINE_KEYS:
        _fail(f"{label} must be {{id, time, value, stage?, stageUnit?}}")
    if not isinstance(line.get("id"), str) or not GAUGE_ID.match(line["id"]):
        _fail(f"{label}.id must be an identifier")
    times, values = line.get("time"), line.get("value")
    if not isinstance(times, list) or not isinstance(values, list) or not times or len(times) != len(values):
        _fail(f"{label}: time and value must be non-empty lists of one length")
    previous = None
    for index, moment in enumerate(times):
        parsed = _time(moment, f"{label}.time[{index}]")
        if previous is not None and parsed <= previous:
            _fail(f"{label}: times must be strictly increasing")
        previous = parsed
    for key in ("value", "stage"):
        if key not in line:
            continue
        series = line[key]
        if not isinstance(series, list) or len(series) != len(times):
            _fail(f"{label}.{key} must hold one entry per time")
        for item in series:
            if item is not None:
                _number(item, f"{label}.{key}[]")
    if ("stage" in line) != ("stageUnit" in line):
        _fail(f"{label}: stage and stageUnit come together")
    if "stageUnit" in line and (not isinstance(line["stageUnit"], str) or not UNIT.match(line["stageUnit"])):
        _fail(f"{label}.stageUnit must be a unit symbol")
    return line


def read_index(path: Path) -> dict[str, Any]:
    """Read, validate and verify a river index against the files beside it."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RiverIndexError(f"cannot read river index {path}: {exc}") from exc
    index = validate_index(payload)
    directory = Path(path).parent
    if index["kind"] == "network":
        geometry = index["geometry"]
        _check_plain(directory, geometry, "geometry")
        for track in index["tracks"]:
            _check_store(directory, track, geometry)
    if index.get("gauges"):
        jsonl = directory / GAUGES_FILENAME
        if not jsonl.is_file():
            _fail(f"the gauges name {GAUGES_FILENAME}, which is not there")
        data = jsonl.read_bytes()
        for gauge in index["gauges"]:
            line = _line(data, gauge["series"], f"gauge {gauge['id']}.series")
            if line["id"] != gauge["id"]:
                _fail(f"gauge {gauge['id']}.series cuts out the line of {line['id']}")
            if "outflow" in gauge:
                line = _line(data, gauge["outflow"], f"gauge {gauge['id']}.outflow")
                if line["id"] != f"{gauge['id']}.outflow":
                    _fail(f"gauge {gauge['id']}.outflow cuts out the line of {line['id']}")
    for item in index.get("extent", []):
        _check_plain(directory, item, f"extent {item['path']}")
    return index

"""The ``tc`` product, schema v1: construction and validation of the three
files — the mutable pointer ``latest-tc.json``, an issue's ``index.json``
and one ``<storm>.json`` per system — in the posture ``manifest.py``
takes for the raster runs: written *and* read through the validator.
``docs/tc.md`` is the normative description.

Admission is structural (the principle of the self-describing variables):
an agency or model id the registry does not know is not an error, so a
centre can be added to the product before every reader learns its name.
What is checked is shape — id patterns, monotone leads, coordinate ranges,
non-negative radii, the fixed-point arrays' lengths.
"""

from __future__ import annotations

import json
import math
import os
import re
import zlib
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..errors import TcProductError
from .track import MISSING, QUADRANTS, RADII_THRESHOLDS

SCHEMA_VERSION = 1
POINTER_FILENAME = "latest-tc.json"
INDEX_FILENAME = "index.json"
LEVELS = ("A", "B", "C")

ATCF_ID = re.compile(r"^[A-Z]{2}\d{2}\d{4}$")
SYNTHETIC_ID = re.compile(r"^x-[a-z]{2}-\d{10}-\d+$")
SOURCE_KEY = re.compile(r"^[a-z][a-z0-9]*$")
ALIAS = re.compile(r"^[A-Za-z0-9:._-]+$")
CRC32 = re.compile(r"^[0-9a-f]{8}$")
ISSUE = re.compile(r"^\d{10}$")


def issue_directory(issue: datetime) -> str:
    """``tc.2026091206``: one directory per aggregation hour."""
    return f"tc.{issue.astimezone(UTC).strftime('%Y%m%d%H')}"


def parse_issue(value: str) -> datetime:
    if not ISSUE.match(value):
        raise TcProductError("issue must be a UTC hour, YYYYMMDDHH")
    try:
        return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as exc:
        raise TcProductError(f"issue is not a valid hour: {value}") from exc


def _time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise TcProductError(f"{label} must be an ISO 8601 UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise TcProductError(f"{label} is not a valid timestamp") from exc


def _number(value: object, label: str, *, minimum: float | None = None, maximum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TcProductError(f"{label} must be a number")
    if math.isnan(value):
        raise TcProductError(f"{label} must not be NaN")
    if minimum is not None and value < minimum:
        raise TcProductError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise TcProductError(f"{label} must be at most {maximum}")


def _optional_number(value: object, label: str, **bounds: float | None) -> None:
    if value is not None:
        _number(value, label, **bounds)


def _storm_id_ok(value: object) -> bool:
    return isinstance(value, str) and bool(ATCF_ID.match(value) or SYNTHETIC_ID.match(value))


def validate_point(point: object, label: str, *, forecast: bool) -> None:
    if not isinstance(point, dict):
        raise TcProductError(f"{label} must be an object")
    _time(point.get("time"), f"{label}.time")
    if forecast:
        lead = point.get("lead")
        if isinstance(lead, bool) or not isinstance(lead, int) or lead < 0:
            raise TcProductError(f"{label}.lead must be a non-negative integer (seconds)")
    _number(point.get("lat"), f"{label}.lat", minimum=-90, maximum=90)
    _number(point.get("lon"), f"{label}.lon", minimum=-180, maximum=180)
    _optional_number(point.get("vmax"), f"{label}.vmax", minimum=0, maximum=150)
    _optional_number(point.get("pmin"), f"{label}.pmin", minimum=800, maximum=1100)
    _optional_number(point.get("rmw"), f"{label}.rmw", minimum=0)
    _optional_number(point.get("gust"), f"{label}.gust", minimum=0, maximum=200)
    _optional_number(point.get("cone"), f"{label}.cone", minimum=0)
    cls = point.get("class")
    if cls is not None and not isinstance(cls, str):
        raise TcProductError(f"{label}.class must be a string or null")
    radii = point.get("radii")
    if radii is None:
        return
    if not isinstance(radii, dict):
        raise TcProductError(f"{label}.radii must be an object or null")
    for threshold, quadrants in radii.items():
        if threshold not in RADII_THRESHOLDS:
            raise TcProductError(f"{label}.radii has an unknown threshold {threshold!r}")
        if not isinstance(quadrants, list) or len(quadrants) != len(QUADRANTS):
            raise TcProductError(f"{label}.radii[{threshold}] must list four quadrants (ne, se, sw, nw)")
        for quadrant, value in zip(QUADRANTS, quadrants):
            _optional_number(value, f"{label}.radii[{threshold}].{quadrant}", minimum=0)


def validate_forecast(forecast: object, label: str) -> None:
    if not isinstance(forecast, dict):
        raise TcProductError(f"{label} must be an object")
    _time(forecast.get("issued"), f"{label}.issued")
    base = _time(forecast.get("base"), f"{label}.base")
    run = forecast.get("run")
    if run is not None and (not isinstance(run, str) or not ISSUE.match(run)):
        raise TcProductError(f"{label}.run must be a YYYYMMDDHH cycle id or absent")
    points = forecast.get("points")
    if not isinstance(points, list):
        raise TcProductError(f"{label}.points must be a list")
    previous_lead = -1
    for index, point in enumerate(points):
        validate_point(point, f"{label}.points[{index}]", forecast=True)
        lead = point["lead"]
        if lead <= previous_lead:
            raise TcProductError(f"{label}.points must have strictly increasing leads")
        if int((_time(point["time"], "time") - base).total_seconds()) != lead:
            raise TcProductError(f"{label}.points[{index}] time does not equal base + lead")
        previous_lead = lead


def validate_track(track: object, label: str) -> None:
    if not isinstance(track, dict):
        raise TcProductError(f"{label} must be an object")
    source = track.get("source")
    if source is not None and (not isinstance(source, str) or not SOURCE_KEY.match(source)):
        raise TcProductError(f"{label}.source must be a source id or null")
    if not isinstance(track.get("provisional"), bool):
        raise TcProductError(f"{label}.provisional must be a boolean")
    points = track.get("points")
    if not isinstance(points, list):
        raise TcProductError(f"{label}.points must be a list")
    previous: datetime | None = None
    for index, point in enumerate(points):
        validate_point(point, f"{label}.points[{index}]", forecast=False)
        time = _time(point["time"], "time")
        if previous is not None and time <= previous:
            raise TcProductError(f"{label}.points must be in strictly increasing time")
        previous = time


def validate_ensemble(ensemble: object, label: str) -> None:
    if not isinstance(ensemble, dict):
        raise TcProductError(f"{label} must be an object")
    _time(ensemble.get("issued"), f"{label}.issued")
    _time(ensemble.get("base"), f"{label}.base")
    run = ensemble.get("run")
    if run is not None and (not isinstance(run, str) or not ISSUE.match(run)):
        raise TcProductError(f"{label}.run must be a YYYYMMDDHH cycle id or absent")
    leads = ensemble.get("leads")
    if not isinstance(leads, list) or any(isinstance(l, bool) or not isinstance(l, int) or l < 0 for l in leads):
        raise TcProductError(f"{label}.leads must be a list of non-negative integers (seconds)")
    if any(b <= a for a, b in pairwise(leads)):
        raise TcProductError(f"{label}.leads must be strictly increasing")
    members = ensemble.get("members")
    if not isinstance(members, list) or any(isinstance(m, bool) or not isinstance(m, int) for m in members):
        raise TcProductError(f"{label}.members must be a list of integer member ids")
    if len(set(members)) != len(members):
        raise TcProductError(f"{label}.members must be unique")
    expected = len(leads) * len(members)
    bounds = {"lat": (-9000, 9000), "lon": (-18000, 18000), "vmax": (0, 1500), "pmin": (8000, 11000)}
    for key, (low, high) in bounds.items():
        values = ensemble.get(key)
        if not isinstance(values, list) or len(values) != expected:
            raise TcProductError(f"{label}.{key} must hold members × leads = {expected} integers")
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TcProductError(f"{label}.{key} must be integers")
            if value != MISSING and not low <= value <= high:
                raise TcProductError(f"{label}.{key} has a value out of range: {value}")
    mean = ensemble.get("mean")
    if mean is not None:
        validate_forecast(mean, f"{label}.mean")


def validate_aliases(aliases: object, label: str) -> None:
    if not isinstance(aliases, dict):
        raise TcProductError(f"{label} must be an object")
    for alias, window in aliases.items():
        if not ALIAS.match(alias):
            raise TcProductError(f"{label} has a malformed alias {alias!r}")
        if not isinstance(window, dict):
            raise TcProductError(f"{label}[{alias}] must be an object")
        start = _time(window.get("from"), f"{label}[{alias}].from")
        end = _time(window.get("to"), f"{label}[{alias}].to")
        if end < start:
            raise TcProductError(f"{label}[{alias}] ends before it starts")


def validate_sources(sources: object, label: str) -> None:
    if not isinstance(sources, list):
        raise TcProductError(f"{label} must be a list")
    seen: set[str] = set()
    for index, status in enumerate(sources):
        if not isinstance(status, dict):
            raise TcProductError(f"{label}[{index}] must be an object")
        source_id = status.get("id")
        if not isinstance(source_id, str) or not SOURCE_KEY.match(source_id):
            raise TcProductError(f"{label}[{index}].id must be a source id")
        if source_id in seen:
            raise TcProductError(f"{label} lists {source_id} twice")
        seen.add(source_id)
        if not isinstance(status.get("ok"), bool):
            raise TcProductError(f"{label}[{index}].ok must be a boolean")
        if not status["ok"] and not isinstance(status.get("error"), str):
            raise TcProductError(f"{label}[{index}] failed without an error message")
        if "fetched" in status:
            _time(status["fetched"], f"{label}[{index}].fetched")


def _validate_keyed(section: object, label: str, validator: Any) -> None:
    if not isinstance(section, dict):
        raise TcProductError(f"{label} must be an object")
    for key, value in section.items():
        if not SOURCE_KEY.match(key):
            raise TcProductError(f"{label} has a malformed key {key!r}")
        validator(value, f"{label}.{key}")


def validate_storm(payload: object) -> None:
    if not isinstance(payload, dict):
        raise TcProductError("storm must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise TcProductError(f"storm schemaVersion must be {SCHEMA_VERSION}")
    if not _storm_id_ok(payload.get("id")):
        raise TcProductError("storm id must be an ATCF id or a synthetic x-<basin>-<hour>-<n> id")
    if payload.get("level") not in LEVELS:
        raise TcProductError("storm level must be A, B or C")
    if payload["level"] == "A" and not ATCF_ID.match(payload["id"]):
        raise TcProductError("an A-level storm is identified by its ATCF id")
    basin = payload.get("basin")
    if not isinstance(basin, str) or len(basin) != 2 or not basin.isalpha() or not basin.isupper():
        raise TcProductError("storm basin must be a two-letter ATCF basin")
    for key in ("name", "sid", "intl"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            raise TcProductError(f"storm {key} must be a string or null")
    validate_aliases(payload.get("aliases"), "storm.aliases")
    _validate_keyed(payload.get("best"), "storm.best", validate_track)
    _validate_keyed(payload.get("agencies"), "storm.agencies", validate_forecast)

    def model(value: object, label: str) -> None:
        if isinstance(value, dict) and "leads" in value:
            validate_ensemble(value, label)
        else:
            validate_forecast(value, label)

    _validate_keyed(payload.get("models"), "storm.models", model)
    impact = payload.get("impact")
    if not isinstance(impact, dict):
        raise TcProductError("storm.impact must be an object")
    alert = payload.get("alert")
    if alert is not None:
        if not isinstance(alert, dict):
            raise TcProductError("storm.alert must be an object or null")
        _time(alert.get("time"), "storm.alert.time")
        line = alert.get("line")
        if line is not None:
            if not isinstance(line, list) or len(line) != 2:
                raise TcProductError("storm.alert.line must be two [lat, lon] ends")
            for end in line:
                if not isinstance(end, list) or len(end) != 2:
                    raise TcProductError("storm.alert.line ends must be [lat, lon]")
                _number(end[0], "storm.alert.line lat", minimum=-90, maximum=90)
                _number(end[1], "storm.alert.line lon", minimum=-180, maximum=180)
        _optional_number(alert.get("halfWidth"), "storm.alert.halfWidth", minimum=0)
    validate_sources(payload.get("sources"), "storm.sources")


def validate_index(payload: object) -> None:
    if not isinstance(payload, dict):
        raise TcProductError("index must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise TcProductError(f"index schemaVersion must be {SCHEMA_VERSION}")
    _time(payload.get("issued"), "index.issued")
    _time(payload.get("generated"), "index.generated")
    storms = payload.get("storms")
    if not isinstance(storms, list):
        raise TcProductError("index.storms must be a list")
    ids: set[str] = set()
    paths: set[str] = set()
    for position, entry in enumerate(storms):
        label = f"index.storms[{position}]"
        if not isinstance(entry, dict):
            raise TcProductError(f"{label} must be an object")
        storm_id = entry.get("id")
        if not _storm_id_ok(storm_id):
            raise TcProductError(f"{label}.id is malformed")
        if storm_id in ids:
            raise TcProductError(f"index lists {storm_id} twice")
        ids.add(storm_id)
        if entry.get("level") not in LEVELS:
            raise TcProductError(f"{label}.level must be A, B or C")
        basin = entry.get("basin")
        if not isinstance(basin, str) or len(basin) != 2:
            raise TcProductError(f"{label}.basin must be a two-letter basin")
        name = entry.get("name")
        if name is not None and not isinstance(name, str):
            raise TcProductError(f"{label}.name must be a string or null")
        path = entry.get("path")
        if not isinstance(path, str) or path.startswith(("/", "http:", "https:")) or "/" in path or path in paths:
            raise TcProductError(f"{label}.path must be a unique file name beside the index")
        paths.add(path)
        byte_length = entry.get("byteLength")
        if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length <= 0:
            raise TcProductError(f"{label}.byteLength must be a positive integer")
        if not isinstance(entry.get("crc32"), str) or not CRC32.match(entry["crc32"]):
            raise TcProductError(f"{label}.crc32 must be 8 lowercase hex characters")
        validate_aliases(entry.get("aliases"), f"{label}.aliases")
        last = entry.get("position")
        if last is not None:
            if not isinstance(last, dict):
                raise TcProductError(f"{label}.position must be an object or null")
            _time(last.get("time"), f"{label}.position.time")
            _number(last.get("lat"), f"{label}.position.lat", minimum=-90, maximum=90)
            _number(last.get("lon"), f"{label}.position.lon", minimum=-180, maximum=180)
        _optional_number(entry.get("vmax"), f"{label}.vmax", minimum=0)
        _optional_number(entry.get("pmin"), f"{label}.pmin", minimum=800, maximum=1100)
        for key in ("agencies", "models", "best"):
            values = entry.get(key)
            if not isinstance(values, list) or any(not isinstance(v, str) or not SOURCE_KEY.match(v) for v in values):
                raise TcProductError(f"{label}.{key} must list source ids")
    crosswalk = payload.get("crosswalk")
    if not isinstance(crosswalk, dict):
        raise TcProductError("index.crosswalk must be an object")
    for old, new in crosswalk.items():
        if not _storm_id_ok(old) or not _storm_id_ok(new) or old == new:
            raise TcProductError(f"index.crosswalk entry {old!r} → {new!r} is malformed")
    validate_sources(payload.get("sources"), "index.sources")


def build_pointer(issue: datetime, index_path: str, index_bytes: bytes) -> dict[str, Any]:
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "product": "tc",
        "issued": issue.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "path": index_path,
        "byteLength": len(index_bytes),
        "crc32": crc32_hex(index_bytes),
    }
    validate_pointer(payload)
    return payload


def validate_pointer(payload: object) -> None:
    if not isinstance(payload, dict):
        raise TcProductError("tc pointer must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise TcProductError(f"tc pointer schemaVersion must be {SCHEMA_VERSION}")
    if payload.get("product") != "tc":
        raise TcProductError("tc pointer product must be 'tc'")
    issued = _time(payload.get("issued"), "pointer.issued")
    path = payload.get("path")
    if (
        not isinstance(path, str)
        or not path.endswith("/" + INDEX_FILENAME)
        or path.startswith(("/", "http:", "https:"))
        or ".." in Path(path).parts
    ):
        raise TcProductError("tc pointer path must be a relative <issue directory>/index.json path")
    if Path(path).parts[0] != issue_directory(issued):
        raise TcProductError("tc pointer path does not name the issued hour's directory")
    byte_length = payload.get("byteLength")
    if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length <= 0:
        raise TcProductError("tc pointer byteLength must be a positive integer")
    if not isinstance(payload.get("crc32"), str) or not CRC32.match(payload["crc32"]):
        raise TcProductError("tc pointer crc32 must be 8 lowercase hex characters")


def crc32_hex(payload: bytes) -> str:
    return f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"


def encode_json(payload: dict[str, Any]) -> bytes:
    """The one serialisation — compact separators, keys as built, ASCII
    escaped — so a file's CRC32 is a function of its content alone."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def read_index(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TcProductError(f"cannot read tc index {path}: {exc}") from exc
    validate_index(payload)
    return payload

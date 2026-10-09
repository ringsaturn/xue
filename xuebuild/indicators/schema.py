"""What the indicators product writes, and the checks it passes before it
is written (``docs/indicators.md``).

Every object is serialised by :func:`encode_json` (the point products'
serialisation: compact, ASCII, keys as built), so a file's CRC-32 is a
function of its content. A feature line, a season file and the index are
each validated here on write; a line that fails is dropped whole and its
source reports the error, without stopping the others.

The product publishes physical quantities only. :func:`check_vocabulary`
holds every key and every string value to that: none of the words in
:data:`FORBIDDEN_WORDS` appears as a word anywhere in what is written.
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Any

from ..common import crc32_hex
from ..errors import XueError
from ..pointproduct import encode_json

__all__ = [
    "SCHEMA_VERSION",
    "FORBIDDEN_WORDS",
    "IndicatorsError",
    "check_vocabulary",
    "crc32_hex",
    "encode_json",
    "validate_context",
    "validate_index",
    "validate_line",
    "validate_season",
]

SCHEMA_VERSION = 1
PRODUCT = "indicators"


class IndicatorsError(XueError):
    """An indicators input or output that breaks docs/indicators.md."""


# -- vocabulary ----------------------------------------------------------------

FORBIDDEN_WORDS: tuple[str, ...] = (
    "signal",
    "alert",
    "warning",
    "watch",
    "bull",
    "bullish",
    "bear",
    "bearish",
    "long",
    "short",
    "price",
    "contract",
    "ticker",
    "trade",
    "trading",
    "hedge",
    "position",
    "yield",
)
"""Words that never appear in a key or a string value of the product."""
_FORBIDDEN = re.compile(
    r"^(?:" + "|".join(FORBIDDEN_WORDS) + r")(?:s|es|ed|ing)?$"
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def _words(text: str) -> list[str]:
    """``hot35_days`` → hot35, days; ``byteLength`` → byte, length."""
    return [word.lower() for part in _SPLIT.split(text) for word in _CAMEL.split(part) if word]


def forbidden_in(text: str) -> list[str]:
    return [word for word in _words(text) if _FORBIDDEN.match(word)]


QUOTED_KEYS = frozenset({"notice"})
"""Keys whose value is an upstream's own sentence, quoted as its terms of
use require (an attribution ``notice``); the vocabulary rule is for the
product's words, so these values are not checked."""


def check_vocabulary(payload: object, where: str = "$") -> None:
    """Raise if a forbidden word is a word of any key or string value."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            found = forbidden_in(str(key))
            if found:
                raise IndicatorsError(f"{where}: key {key!r} uses {found[0]!r}")
            if key in QUOTED_KEYS and isinstance(value, str):
                continue
            check_vocabulary(value, f"{where}.{key}")
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            check_vocabulary(value, f"{where}[{index}]")
    elif isinstance(payload, str):
        found = forbidden_in(payload)
        if found:
            raise IndicatorsError(f"{where}: {payload!r} uses {found[0]!r}")


# -- primitives ----------------------------------------------------------------

RUN = re.compile(r"^\d{10}$")
CRC32 = re.compile(r"^[0-9a-f]{8}$")
SOURCE_ID = re.compile(r"^[a-z][a-z0-9]*$")
REGION_ID = re.compile(r"^[a-z]{2}-[a-z]{2,3}$")
SEASON_ID = re.compile(r"^\d{4}(?:-\d{2})?$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RELATIVE_PATH = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")

#: The value ranges a daily object is held to (docs/indicators.md §4).
RANGES: dict[str, tuple[float, float]] = {
    "t2m_mean": (-60.0, 60.0),
    "t2m_max": (-60.0, 60.0),
    "t2m_min": (-60.0, 60.0),
    "precip": (0.0, 500.0),
    "dry_frac": (0.0, 1.0),
    "hot30_frac": (0.0, 1.0),
    "hot35_frac": (0.0, 1.0),
    "gdd": (0.0, 20.0),
}
DAY_FIELDS: tuple[str, ...] = ("date", *RANGES, "complete")
SUMMARY_FIELDS: tuple[str, ...] = ("days", "precip_sum", "gdd_sum", "hot35_days")
SEASON_TOTALS: tuple[str, ...] = (
    "days_counted",
    "days_partial",
    "precip_total",
    "gdd_total",
    "hot35_days",
    "cdd_current",
    "cdd_max",
    "days_counted_window",
    "precip_window",
    "hot35_days_window",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IndicatorsError(message)


def _number(value: object, label: str, minimum: float | None = None, maximum: float | None = None) -> float:
    _require(
        not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value),
        f"{label} must be a finite number",
    )
    assert isinstance(value, (int, float))
    _require(minimum is None or value >= minimum, f"{label} must be at least {minimum}")
    _require(maximum is None or value <= maximum, f"{label} must be at most {maximum}")
    return float(value)


def _integer(value: object, label: str, minimum: int = 0) -> int:
    _require(not isinstance(value, bool) and isinstance(value, int) and value >= minimum, f"{label} must be an integer >= {minimum}")
    assert isinstance(value, int)
    return value


def _string(value: object, label: str, pattern: re.Pattern[str] | None = None) -> str:
    _require(isinstance(value, str) and bool(value), f"{label} must be a non-empty string")
    assert isinstance(value, str)
    _require(pattern is None or bool(pattern.match(value)), f"{label} is malformed: {value!r}")
    return value


def _date(value: object, label: str) -> date:
    text = _string(value, label, DATE)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise IndicatorsError(f"{label} is not a date: {text}") from exc


def _object(value: object, label: str, keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    _require(isinstance(value, dict), f"{label} must be an object")
    assert isinstance(value, dict)
    if keys is not None:
        _require(tuple(value) == keys, f"{label} must carry exactly {', '.join(keys)} in that order")
    return value


def _list(value: object, label: str) -> list[Any]:
    _require(isinstance(value, list), f"{label} must be a list")
    assert isinstance(value, list)
    return value


def _check_day(day: object, label: str, extra: tuple[str, ...] = ()) -> date:
    entry = _object(day, label, DAY_FIELDS[:1] + extra + DAY_FIELDS[1:])
    moment = _date(entry["date"], f"{label}.date")
    for field, (low, high) in RANGES.items():
        _number(entry[field], f"{label}.{field}", low, high)
    _require(entry["t2m_min"] <= entry["t2m_mean"] <= entry["t2m_max"], f"{label}: t2m_min <= t2m_mean <= t2m_max")
    _require(entry["hot35_frac"] <= entry["hot30_frac"], f"{label}: hot35_frac must not exceed hot30_frac")
    _require(isinstance(entry["complete"], bool), f"{label}.complete must be a boolean")
    return moment


def _check_days(days: object, label: str, extra: tuple[str, ...] = ()) -> None:
    previous: date | None = None
    for index, day in enumerate(_list(days, label)):
        moment = _check_day(day, f"{label}[{index}]", extra)
        _require(previous is None or moment > previous, f"{label} must be in ascending date order without repeats")
        previous = moment


# -- the feature line ----------------------------------------------------------

LINE_KEYS = (
    "schemaVersion",
    "source",
    "run",
    "runTime",
    "manifest",
    "weights",
    "calendar",
    "horizon_days",
    "frame_step_hours",
    "regions",
)


def validate_line(line: object, region_ids: tuple[str, ...]) -> None:
    """One run's line (docs/indicators.md §4.1)."""
    entry = _object(line, "line", LINE_KEYS)
    _require(entry["schemaVersion"] == SCHEMA_VERSION, f"line schemaVersion must be {SCHEMA_VERSION}")
    _string(entry["source"], "line.source", SOURCE_ID)
    run = _string(entry["run"], "line.run", RUN)
    run_time = _string(entry["runTime"], "line.runTime")
    _require(run_time == f"{run[:4]}-{run[4:6]}-{run[6:8]}T{run[8:10]}:00:00Z", "line.runTime must be the run's hour")
    manifest = _object(entry["manifest"], "line.manifest", ("path", "crc32"))
    _string(manifest["path"], "line.manifest.path", RELATIVE_PATH)
    _require(manifest["crc32"] is None or bool(CRC32.match(str(manifest["crc32"]))), "line.manifest.crc32 is malformed")
    weights = _object(entry["weights"], "line.weights", ("version", "grid", "crc32"))
    _string(weights["version"], "line.weights.version")
    _string(weights["grid"], "line.weights.grid")
    _string(weights["crc32"], "line.weights.crc32", CRC32)
    calendar = _object(entry["calendar"], "line.calendar", ("version",))
    _string(calendar["version"], "line.calendar.version")
    _number(entry["horizon_days"], "line.horizon_days", 0.0, 400.0)
    steps = _list(entry["frame_step_hours"], "line.frame_step_hours")
    _require(bool(steps), "line.frame_step_hours must not be empty")
    for index, step in enumerate(steps):
        pair = _list(step, f"line.frame_step_hours[{index}]")
        _require(len(pair) == 2, "line.frame_step_hours entries are [through_hour, step_hours] pairs")
        _integer(pair[0], f"line.frame_step_hours[{index}][0]")
        _integer(pair[1], f"line.frame_step_hours[{index}][1]", 1)
    regions = _object(entry["regions"], "line.regions")
    _require(bool(regions), "line.regions must not be empty")
    order = [region_id for region_id in region_ids if region_id in regions]
    _require(list(regions) == order, "line.regions must be known regions in table order")
    for region_id, block in regions.items():
        label = f"line.regions.{region_id}"
        body = _object(block, label, ("days", "summary"))
        _check_days(body["days"], f"{label}.days")
        summary = _object(body["summary"], f"{label}.summary", SUMMARY_FIELDS)
        _integer(summary["days"], f"{label}.summary.days")
        _number(summary["precip_sum"], f"{label}.summary.precip_sum", 0.0)
        _number(summary["gdd_sum"], f"{label}.summary.gdd_sum", 0.0)
        _number(summary["hot35_days"], f"{label}.summary.hot35_days", 0.0)
    check_vocabulary(entry)


# -- the season file -----------------------------------------------------------

SEASON_KEYS = (
    "schemaVersion",
    "region",
    "season",
    "source",
    "calendar",
    "weights",
    "windows",
    "throughRun",
    *SEASON_TOTALS,
    "daily",
)
SEASON_DAY_EXTRA = ("run", "lead_hours", "partial", "flowering")


def validate_season(payload: object) -> None:
    """One region's in-season state (docs/indicators.md §4.2)."""
    entry = _object(payload, "season", SEASON_KEYS)
    _require(entry["schemaVersion"] == SCHEMA_VERSION, f"season schemaVersion must be {SCHEMA_VERSION}")
    _string(entry["region"], "season.region", REGION_ID)
    _string(entry["season"], "season.season", SEASON_ID)
    _string(entry["source"], "season.source", SOURCE_ID)
    _string(_object(entry["calendar"], "season.calendar", ("version",))["version"], "season.calendar.version")
    _object(entry["weights"], "season.weights", ("version",))
    windows = _object(entry["windows"], "season.windows", ("season", "flowering"))
    for name, window in windows.items():
        pair = _list(window, f"season.windows.{name}")
        _require(len(pair) == 2 and all(isinstance(item, str) for item in pair), f"season.windows.{name} must be [start, end]")
    _string(entry["throughRun"], "season.throughRun", RUN)
    for field in ("days_counted", "days_partial", "cdd_current", "cdd_max", "days_counted_window"):
        _integer(entry[field], f"season.{field}")
    for field in ("precip_total", "gdd_total", "hot35_days", "precip_window", "hot35_days_window"):
        _number(entry[field], f"season.{field}", 0.0)
    _require(entry["cdd_current"] <= entry["cdd_max"], "season.cdd_current must not exceed cdd_max")
    daily = _list(entry["daily"], "season.daily")
    _require(len(daily) == entry["days_counted"], "season.days_counted must be the length of daily")
    _check_days(daily, "season.daily", SEASON_DAY_EXTRA)
    for index, day in enumerate(daily):
        label = f"season.daily[{index}]"
        _string(day["run"], f"{label}.run", RUN)
        _integer(day["lead_hours"], f"{label}.lead_hours")
        _require(isinstance(day["partial"], bool) and isinstance(day["flowering"], bool), f"{label} flags must be booleans")
    check_vocabulary(entry)


# -- context -------------------------------------------------------------------

def validate_context(payload: object) -> None:
    """``context/oni.json``."""
    entry = _object(payload, "context", ("schemaVersion", "id", "name", "unit", "source", "fetches", "rows"))
    _require(entry["schemaVersion"] == SCHEMA_VERSION, f"context schemaVersion must be {SCHEMA_VERSION}")
    _string(entry["id"], "context.id", SOURCE_ID)
    source = _object(entry["source"], "context.source", ("name", "url", "license"))
    for key in source:
        _string(source[key], f"context.source.{key}")
    fetches = _list(entry["fetches"], "context.fetches")
    _require(bool(fetches), "context.fetches must not be empty")
    for index, fetch in enumerate(fetches):
        item = _object(fetch, f"context.fetches[{index}]", ("fetched", "rows", "crc32"))
        _string(item["fetched"], f"context.fetches[{index}].fetched")
        _integer(item["rows"], f"context.fetches[{index}].rows", 1)
        _string(item["crc32"], f"context.fetches[{index}].crc32", CRC32)
    rows = _list(entry["rows"], "context.rows")
    _require(len(rows) == fetches[-1]["rows"], "context.rows must be the newest fetch's rows")
    for index, row in enumerate(rows):
        item = _object(row, f"context.rows[{index}]", ("season", "year", "total", "anomaly"))
        _string(item["season"], f"context.rows[{index}].season", re.compile(r"^[A-Z]{3}$"))
        _integer(item["year"], f"context.rows[{index}].year", 1850)
        _number(item["total"], f"context.rows[{index}].total", -10.0, 40.0)
        _number(item["anomaly"], f"context.rows[{index}].anomaly", -10.0, 10.0)
    check_vocabulary(entry)


# -- the index -----------------------------------------------------------------

INDEX_KEYS = (
    "schemaVersion",
    "product",
    "note",
    "attribution",
    "regions",
    "features",
    "calendar",
    "weights",
    "sources",
    "files",
)


ATTRIBUTION_KEYS = (
    ("name", "data", "license"),
    ("name", "data", "license", "citation"),
    ("name", "data", "license", "citation", "notice"),
)
"""An attribution entry's keys: ``citation`` (the form the upstream asks
for) and ``notice`` (a sentence it requires on adaptations) are optional."""


def validate_index(payload: object) -> None:
    """``indicators/index.json`` (docs/indicators.md §5)."""
    entry = _object(payload, "index", INDEX_KEYS)
    _require(entry["schemaVersion"] == SCHEMA_VERSION, f"index schemaVersion must be {SCHEMA_VERSION}")
    _require(entry["product"] == PRODUCT, f"index product must be {PRODUCT!r}")
    _string(entry["note"], "index.note")
    for index, item in enumerate(_list(entry["attribution"], "index.attribution")):
        attribution = _object(item, f"index.attribution[{index}]")
        _require(
            tuple(attribution) in ATTRIBUTION_KEYS,
            f"index.attribution[{index}] must carry name, data, license, then optionally citation and notice",
        )
        for key in attribution:
            _string(attribution[key], f"index.attribution[{index}].{key}")
    region_ids = []
    for index, item in enumerate(_list(entry["regions"], "index.regions")):
        region = _object(item, f"index.regions[{index}]")
        region_ids.append(_string(region.get("id"), f"index.regions[{index}].id", REGION_ID))
    _require(len(set(region_ids)) == len(region_ids), "index.regions ids must be unique")
    for index, item in enumerate(_list(entry["features"], "index.features")):
        feature = _object(item, f"index.features[{index}]", ("id", "unit", "definition"))
        for key in feature:
            _string(feature[key], f"index.features[{index}].{key}")
    calendar = _object(entry["calendar"], "index.calendar", ("version", "source", "regions"))
    _string(calendar["version"], "index.calendar.version")
    weights = _object(entry["weights"], "index.weights", ("version", "spam", "files"))
    _string(weights["version"], "index.weights.version")
    for index, item in enumerate(_list(weights["files"], "index.weights.files")):
        _object(item, f"index.weights.files[{index}]", ("grid", "path"))
    seen_sources: set[str] = set()
    for index, item in enumerate(_list(entry["sources"], "index.sources")):
        label = f"index.sources[{index}]"
        status = _object(item, label, ("id", "grid", "cadence", "latestRun", "ok", "error"))
        source_id = _string(status["id"], f"{label}.id", SOURCE_ID)
        _require(source_id not in seen_sources, f"index.sources lists {source_id} twice")
        seen_sources.add(source_id)
        _require(status["latestRun"] is None or bool(RUN.match(str(status["latestRun"]))), f"{label}.latestRun is malformed")
        _require(isinstance(status["ok"], bool), f"{label}.ok must be a boolean")
        _require(status["ok"] == (status["error"] is None), f"{label}.error must be set exactly when not ok")
    previous = ""
    for index, item in enumerate(_list(entry["files"], "index.files")):
        label = f"index.files[{index}]"
        file_entry = _object(item, label)
        path = _string(file_entry.get("path"), f"{label}.path", RELATIVE_PATH)
        _require(path > previous, "index.files must be sorted by path without repeats")
        previous = path
        _integer(file_entry.get("byteLength"), f"{label}.byteLength", 1)
        _string(file_entry.get("crc32"), f"{label}.crc32", CRC32)
        if path.startswith("features/"):
            _require(tuple(file_entry) == ("path", "byteLength", "crc32", "runs"), f"{label} must carry path, byteLength, crc32, runs")
            end = 0
            for slot, run in enumerate(_list(file_entry["runs"], f"{label}.runs")):
                span = _object(run, f"{label}.runs[{slot}]", ("run", "offset", "length"))
                _string(span["run"], f"{label}.runs[{slot}].run", RUN)
                _require(span["offset"] == end, f"{label}.runs must tile the file from offset 0")
                end = span["offset"] + _integer(span["length"], f"{label}.runs[{slot}].length", 2)
            _require(end == file_entry["byteLength"], f"{label}.runs must cover the whole file")
        else:
            _require(tuple(file_entry) == ("path", "byteLength", "crc32"), f"{label} must carry path, byteLength, crc32")
    check_vocabulary(entry)

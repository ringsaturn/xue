"""The ``sounding`` product, schema v1: construction and validation of the
three files — the mutable pointer ``latest-sounding.json``, an issue's
``index.json`` and the ``soundings.jsonl`` beside it — in the posture
``xuebuild/tc/schema.py`` takes for the tracks and ``manifest.py`` for the
raster runs: written *and* read through the validator. ``docs/sounding.md``
is the normative description.

Admission is structural. A station identifier the reader has never seen,
a radiosonde type code it does not know, a gateway id that is not one of
today's two: none of those is an error, because the gateways are a
transitional arrangement and the station set changes weekly. What is
checked is shape — the id pattern, coordinate ranges, arrays of one
length, pressure strictly descending, timestamps in UTC, CRC32s and
relative paths — and the one rule the soundings file adds: the stations'
byte spans are in order, do not overlap, and tile the file exactly.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import SoundingProductError
from ..pointproduct import (  # noqa: F401 — re-exported: the sounding modules import them from here
    CRC32,
    archive_directory,
    flat_directory,
    crc32_hex,
    encode_json,
    iso_z,
    pointer_payload,
    pointer_shape_error,
    check_number,
    check_pointer,
    check_sources,
    check_time,
    parse_issue_hour,
    read_index_file,
    write_bytes_atomic,
)
from .bufr import MISSING, VALUE_BOUNDS

SCHEMA_VERSION = 1
PRODUCT = "sounding"
POINTER_FILENAME = "latest-sounding.json"
INDEX_FILENAME = "index.json"
SOUNDINGS_FILENAME = "soundings.jsonl"

STATION_ID = re.compile(r"^\d+-\d+-\d+-[0-9A-Za-z_]+$")
"""A WIGOS identifier written out: series, issuer, issue number and the
local identifier. Digits, hyphens and (rarely) letters — a file name that
needs no escaping in a URL."""

WMO_NUMBER = re.compile(r"^\d{5}$")
SOURCE_KEY = re.compile(r"^[a-z][a-z0-9]*(?:[.:-][a-z0-9]+)*$")
"""A gateway id (``jp-jma-gts-to-wis2``) or a native WIS2 topic source
(``wis2:jp-jma``), admitted on shape alone."""

ISSUE = re.compile(r"^\d{10}$")

LEVEL_ARRAYS = ("p", "z", "t", "td", "wd", "ws", "sig")

_BOUNDS = VALUE_BOUNDS
"""The fixed-point ranges a level array's values stay inside, ``MISSING``
aside. The parser owns the table (``bufr.VALUE_BOUNDS``) and enforces it
as it writes, so the two cannot drift: anything the parser emits is
something this validator admits."""


def issue_directory(issue: datetime) -> str:
    """``sounding/2026/09/14/sounding.2026091402``: one directory per
    aggregation hour, filed under its year, month and day."""
    return archive_directory(PRODUCT, issue)


def pointer_directories(issue: datetime) -> tuple[str, ...]:
    """Where a pointer may name the issue: the archive tree, or the flat
    ``sounding.<hour>`` directory issues were published in before it."""
    return (issue_directory(issue), flat_directory(PRODUCT, issue))


def parse_issue(value: str) -> datetime:
    return parse_issue_hour(SoundingProductError, value)


def _time(value: object, label: str) -> datetime:
    return check_time(SoundingProductError, value, label)


def _number(value: object, label: str, *, minimum: float | None = None, maximum: float | None = None) -> None:
    check_number(SoundingProductError, value, label, minimum=minimum, maximum=maximum)


def _optional_number(value: object, label: str, **bounds: float | None) -> None:
    if value is not None:
        _number(value, label, **bounds)


def _optional_string(value: object, label: str, pattern: re.Pattern[str] | None = None) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise SoundingProductError(f"{label} must be a string or null")
    if pattern is not None and not pattern.match(value):
        raise SoundingProductError(f"{label} is malformed")


def _station_id_ok(value: object) -> bool:
    return isinstance(value, str) and bool(STATION_ID.match(value))


def validate_position(payload: dict[str, Any], label: str) -> None:
    _number(payload.get("lat"), f"{label}.lat", minimum=-90, maximum=90)
    _number(payload.get("lon"), f"{label}.lon", minimum=-180, maximum=180)
    _optional_number(payload.get("elev"), f"{label}.elev", minimum=-500, maximum=9000)


def validate_levels(payload: dict[str, Any], label: str) -> None:
    """The seven parallel arrays: one length, in range, pressure strictly
    descending and always present (a level with no pressure has no place
    on the axis and is never written). ``reported`` is what the bulletin
    held before thinning and is never below ``n``."""
    count = payload.get("n")
    if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise SoundingProductError(f"{label}.n must be a positive integer")
    reported = payload.get("reported")
    if isinstance(reported, bool) or not isinstance(reported, int) or reported < count:
        raise SoundingProductError(
            f"{label}.reported must be an integer at least {label}.n: the levels the bulletin held before thinning"
        )
    for key in LEVEL_ARRAYS:
        values = payload.get(key)
        if not isinstance(values, list) or len(values) != count:
            raise SoundingProductError(f"{label}.{key} must hold n = {count} integers")
        low, high = _BOUNDS[key]
        for value in values:
            if isinstance(value, bool) or not isinstance(value, int):
                raise SoundingProductError(f"{label}.{key} must be integers")
            if value != MISSING and not low <= value <= high:
                raise SoundingProductError(f"{label}.{key} has a value out of range: {value}")
    pressures = payload["p"]
    if any(value == MISSING for value in pressures):
        raise SoundingProductError(f"{label}.p must not be missing on any level")
    if any(later >= earlier for earlier, later in zip(pressures, pressures[1:])):
        raise SoundingProductError(f"{label}.p must be strictly descending")


_HEIGHT_MIN, _HEIGHT_MAX = _BOUNDS["z"]
"""A derived height is a published ``z``, interpolated or picked out, so
it lives in ``z``'s range and not a narrower one: stations below sea level
report negative geopotential heights, and a bulletin that flags a
tropopause at one is reporting a bad height, not breaking the schema."""


def validate_derived(payload: object, label: str) -> None:
    if not isinstance(payload, dict):
        raise SoundingProductError(f"{label} must be an object")
    _optional_number(payload.get("freezingLevel"), f"{label}.freezingLevel", minimum=_HEIGHT_MIN, maximum=_HEIGHT_MAX)
    _optional_number(payload.get("pw"), f"{label}.pw", minimum=0, maximum=300)
    _optional_number(payload.get("lapse850_500"), f"{label}.lapse850_500", minimum=-30, maximum=30)
    _optional_number(payload.get("tropopause"), f"{label}.tropopause", minimum=_HEIGHT_MIN, maximum=_HEIGHT_MAX)


def validate_sounding(payload: object, label: str) -> None:
    if not isinstance(payload, dict):
        raise SoundingProductError(f"{label} must be an object")
    _time(payload.get("time"), f"{label}.time")
    launched = payload.get("launched")
    if launched is not None:
        _time(launched, f"{label}.launched")
    _optional_string(payload.get("bulletin"), f"{label}.bulletin")
    _optional_string(payload.get("gateway"), f"{label}.gateway", SOURCE_KEY)
    _time(payload.get("arrived"), f"{label}.arrived")
    sonde = payload.get("sondeType")
    if sonde is not None and (isinstance(sonde, bool) or not isinstance(sonde, int) or sonde < 0):
        raise SoundingProductError(f"{label}.sondeType must be a BUFR code table value or null")
    validate_levels(payload, label)
    validate_derived(payload.get("derived"), f"{label}.derived")


def validate_headline(payload: object, label: str) -> None:
    if not isinstance(payload, dict):
        raise SoundingProductError(f"{label} must be an object")
    _optional_number(payload.get("t500"), f"{label}.t500", minimum=-120, maximum=60)
    _optional_number(payload.get("td500"), f"{label}.td500", minimum=-150, maximum=60)
    _optional_number(payload.get("freezingLevel"), f"{label}.freezingLevel", minimum=_HEIGHT_MIN, maximum=_HEIGHT_MAX)
    _optional_number(payload.get("pw"), f"{label}.pw", minimum=0, maximum=300)
    levels = payload.get("levels")
    if isinstance(levels, bool) or not isinstance(levels, int) or levels <= 0:
        raise SoundingProductError(f"{label}.levels must be a positive integer")


def validate_sources(sources: object, label: str) -> None:
    def extra(status: dict[str, Any], where: str) -> None:
        if status.get("fetched") is not None:
            _time(status["fetched"], f"{where}.fetched")
        if status.get("watermark") is not None:
            _time(status["watermark"], f"{where}.watermark")

    check_sources(SoundingProductError, sources, label, SOURCE_KEY, extra)


def validate_station_line(payload: object, label: str = "station") -> None:
    """One line of ``soundings.jsonl``: a station with its whole window of
    nominal times. It carries no ``schemaVersion`` and no ``sources`` — the
    index it hangs off has both, and repeating them per station would cost
    more than the stations do."""
    if not isinstance(payload, dict):
        raise SoundingProductError(f"{label} must be an object")
    if not _station_id_ok(payload.get("id")):
        raise SoundingProductError(f"{label}.id must be a WIGOS identifier, series-issuer-issue-local")
    _optional_string(payload.get("wmo"), f"{label}.wmo", WMO_NUMBER)
    _optional_string(payload.get("name"), f"{label}.name")
    validate_position(payload, label)
    soundings = payload.get("soundings")
    if not isinstance(soundings, list) or not soundings:
        raise SoundingProductError(f"{label}.soundings must be a non-empty list")
    previous: datetime | None = None
    for position, sounding in enumerate(soundings):
        inner = f"{label}.soundings[{position}]"
        validate_sounding(sounding, inner)
        time = _time(sounding["time"], f"{inner}.time")
        if previous is not None and time >= previous:
            raise SoundingProductError(f"{label}.soundings must be in strictly decreasing time, newest first")
        previous = time


def validate_index(payload: object) -> None:
    if not isinstance(payload, dict):
        raise SoundingProductError("index must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise SoundingProductError(f"index schemaVersion must be {SCHEMA_VERSION}")
    _time(payload.get("issued"), "index.issued")
    _time(payload.get("generated"), "index.generated")
    watermark = payload.get("watermark")
    if not isinstance(watermark, dict):
        raise SoundingProductError("index.watermark must be an object")
    for source_id, moment in watermark.items():
        if not isinstance(source_id, str) or not SOURCE_KEY.match(source_id):
            raise SoundingProductError(f"index.watermark has a malformed source id {source_id!r}")
        if moment is not None:
            _time(moment, f"index.watermark[{source_id}]")
    soundings = payload.get("soundings")
    if not isinstance(soundings, dict):
        raise SoundingProductError("index.soundings must be an object")
    if soundings.get("path") != SOUNDINGS_FILENAME:
        raise SoundingProductError(f"index.soundings.path must be {SOUNDINGS_FILENAME!r}, the file beside the index")
    soundings_bytes = soundings.get("byteLength")
    if isinstance(soundings_bytes, bool) or not isinstance(soundings_bytes, int) or soundings_bytes < 0:
        raise SoundingProductError("index.soundings.byteLength must be a non-negative integer")
    if not isinstance(soundings.get("crc32"), str) or not CRC32.match(soundings["crc32"]):
        raise SoundingProductError("index.soundings.crc32 must be 8 lowercase hex characters")
    stations = payload.get("stations")
    if not isinstance(stations, list):
        raise SoundingProductError("index.stations must be a list")
    ids: set[str] = set()
    previous: str | None = None
    end = 0
    """Where the previous station's line ended, newline included: the
    spans are in order, do not overlap and tile the file exactly."""
    for position, entry in enumerate(stations):
        label = f"index.stations[{position}]"
        if not isinstance(entry, dict):
            raise SoundingProductError(f"{label} must be an object")
        station_id = entry.get("id")
        if not _station_id_ok(station_id):
            raise SoundingProductError(f"{label}.id is malformed")
        assert isinstance(station_id, str)
        if station_id in ids:
            raise SoundingProductError(f"index lists {station_id} twice")
        if previous is not None and station_id <= previous:
            raise SoundingProductError("index.stations must be sorted by id")
        ids.add(station_id)
        previous = station_id
        _optional_string(entry.get("wmo"), f"{label}.wmo", WMO_NUMBER)
        _optional_string(entry.get("name"), f"{label}.name")
        validate_position(entry, label)
        offset = entry.get("offset")
        length = entry.get("length")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise SoundingProductError(f"{label}.offset must be a non-negative integer")
        if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            raise SoundingProductError(f"{label}.length must be a positive integer")
        if offset != end:
            raise SoundingProductError(
                f"{label}.offset must continue the previous station's span: expected {end}, got {offset}"
            )
        if offset + length > soundings_bytes:
            raise SoundingProductError(f"{label} spans past the end of {SOUNDINGS_FILENAME}")
        end = offset + length + 1  # the newline the span excludes
        latest = _time(entry.get("latest"), f"{label}.latest")
        times = entry.get("times")
        if not isinstance(times, list) or not times:
            raise SoundingProductError(f"{label}.times must be a non-empty list")
        parsed = [_time(value, f"{label}.times[{index}]") for index, value in enumerate(times)]
        if any(later >= earlier for earlier, later in zip(parsed, parsed[1:])):
            raise SoundingProductError(f"{label}.times must be in strictly decreasing time, newest first")
        if parsed[0] != latest:
            raise SoundingProductError(f"{label}.latest must equal the first of {label}.times")
        validate_headline(entry.get("headline"), f"{label}.headline")
    if end != soundings_bytes:
        raise SoundingProductError(
            f"index.stations must span {SOUNDINGS_FILENAME} exactly: the spans end at {end} of {soundings_bytes}"
        )
    validate_sources(payload.get("sources"), "index.sources")


def build_pointer(issue: datetime, index_path: str, index_bytes: bytes) -> dict[str, Any]:
    payload = pointer_payload(PRODUCT, issue, index_path, index_bytes)
    validate_pointer(payload)
    return payload


def validate_pointer(payload: object) -> None:
    check_pointer(SoundingProductError, payload, PRODUCT, pointer_directories, "sounding pointer path does not name the issued hour's directory")


def read_index(path: Path) -> dict[str, Any]:
    return read_index_file(SoundingProductError, path, PRODUCT, validate_index)

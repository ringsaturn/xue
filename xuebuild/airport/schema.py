"""The ``airport`` product, schema v1: construction and validation of the
three kinds of file — the mutable pointer ``latest-airport.json``, a
round's ``index.json`` and the ``history.jsonl`` beside it — in the
posture ``manifest.py`` takes for the raster runs and ``tc/schema.py`` for
the storm tracks: written *and* read through the validator.
``docs/airport.md`` is the normative description.

Admission is structural. A station id, a sky cover or a weather string the
reader has never seen is not an error; what is checked is shape — the row
length, the ranges, the ISO-8601 UTC times, the CRC32 pattern — and the
one rule the history file adds: the rows' byte spans are in order and lie
inside the file the index measured, so a reader can slice one station out
of it by range and parse the slice on its own.
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import AirportProductError
from ..pointproduct import (  # noqa: F401 — re-exported: the airport modules import them from here
    CRC32,
    crc32_hex,
    encode_json,
    pointer_payload,
    pointer_shape_error,
    write_bytes_atomic,
)

SCHEMA_VERSION = 1
POINTER_FILENAME = "latest-airport.json"
INDEX_FILENAME = "index.json"
HISTORY_FILENAME = "history.jsonl"
"""One line per station, in the round directory beside the index: a
reader with an index row slices one station out by byte range, and a
reader without one streams the file."""

ROUND_MINUTES = 10
"""The publishing cadence: a round is a UTC minute that is a multiple of
this, so a schedule that drifts by a minute still names one round."""

HISTORY_HOURS = 24
"""How much of each station's past the history file carries."""

LATITUDE_RANGE = (-90.0, 90.0)
LONGITUDE_RANGE = (-180.0, 180.0)
TEMPERATURE_RANGE = (-100.0, 70.0)
WIND_DIRECTION_RANGE = (0, 360)
WIND_SPEED_RANGE = (0.0, 150.0)
PRESSURE_RANGE = (800.0, 1100.0)
ELEVATION_RANGE = (-500.0, 9000.0)
"""The ranges the contract admits. The validators refuse a value outside
them and the parsers write null instead of one: the service occasionally
codes an unknown as a number (``-99.99`` for a position it does not have),
and one such cell must not cost a round."""

CATEGORIES = ("VFR", "MVFR", "IFR", "LIFR")
METAR_TYPES = ("METAR", "SPECI")
CHANGES = ("FM", "BECMG", "TEMPO", "PROB")
STATION_ROW = (
    "icao",
    "lat",
    "lon",
    "elev",
    "obsTime",
    "t",
    "td",
    "wd",
    "ws",
    "gust",
    "vis",
    "qnh",
    "category",
    "tafPresent",
    "offset",
    "length",
)
"""The index's compact row, in order. A reader indexes it positionally;
the last two are the byte span of the station's line in the history file,
the JSON object alone without its newline."""

ROUND = re.compile(r"^\d{12}$")
ICAO = re.compile(r"^[A-Z0-9]{2,4}$")
SOURCE_KEY = re.compile(r"^[a-z][a-z0-9-]*$")
COVER = re.compile(r"^[A-Z]{3,5}$")
"""A sky cover as the service spells it: the three-letter amounts, and
``CAVOK`` where a forecast uses it."""


def parse_round(value: str) -> datetime:
    """``202609161430`` → the round's UTC minute. A minute that is not a
    multiple of :data:`ROUND_MINUTES` names no round."""
    if not ROUND.match(value):
        raise AirportProductError("round must be a UTC minute, YYYYMMDDHHMM")
    try:
        moment = datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError as exc:
        raise AirportProductError(f"round is not a valid minute: {value}") from exc
    if moment.minute % ROUND_MINUTES:
        raise AirportProductError(f"round minute must be a multiple of {ROUND_MINUTES}: {value}")
    return moment


def floor_round(moment: datetime) -> datetime:
    """The round a moment falls in: the minute floored to the cadence.
    The publisher's clock is never exactly on it (GitHub's ten-minute cron
    drifts by a minute or three), and the round name must not drift with
    it."""
    moment = moment.astimezone(UTC)
    return moment.replace(minute=(moment.minute // ROUND_MINUTES) * ROUND_MINUTES, second=0, microsecond=0)


def round_name(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M")


def round_directory(moment: datetime) -> str:
    """``airport.202609161430``: one immutable directory per round, the
    index and the history file in it."""
    return f"airport.{round_name(moment)}"


# --- primitives --------------------------------------------------------


def _time(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AirportProductError(f"{label} must be an ISO 8601 UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise AirportProductError(f"{label} is not a valid timestamp") from exc


Bounds = tuple[float | None, float | None]


def _number(value: object, label: str, bounds: Bounds | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AirportProductError(f"{label} must be a number")
    if math.isnan(value):
        raise AirportProductError(f"{label} must not be NaN")
    minimum, maximum = bounds or (None, None)
    if minimum is not None and value < minimum:
        raise AirportProductError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise AirportProductError(f"{label} must be at most {maximum}")


def _optional_number(value: object, label: str, bounds: Bounds | None = None) -> None:
    if value is not None:
        _number(value, label, bounds)


def _optional_string(value: object, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise AirportProductError(f"{label} must be a string or null")


def _integer(value: object, label: str, bounds: Bounds | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AirportProductError(f"{label} must be an integer")
    minimum, maximum = bounds or (None, None)
    if minimum is not None and value < minimum:
        raise AirportProductError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise AirportProductError(f"{label} must be at most {maximum}")


def _optional_integer(value: object, label: str, bounds: Bounds | None = None) -> None:
    if value is not None:
        _integer(value, label, bounds)


def _category(value: object, label: str) -> None:
    if value is not None and value not in CATEGORIES:
        raise AirportProductError(f"{label} must be one of {', '.join(CATEGORIES)} or null")


def _cloud(value: object, label: str) -> None:
    if not isinstance(value, list):
        raise AirportProductError(f"{label} must be a list of [cover, base] layers")
    for position, layer in enumerate(value):
        where = f"{label}[{position}]"
        if not isinstance(layer, list) or len(layer) != 2:
            raise AirportProductError(f"{where} must be [cover, base]")
        cover = layer[0]
        if not isinstance(cover, str) or not COVER.match(cover):
            raise AirportProductError(f"{where} cover must be a sky cover code")
        _optional_number(layer[1], f"{where} base", (0, None))


def validate_sources(sources: object, label: str) -> None:
    if not isinstance(sources, list):
        raise AirportProductError(f"{label} must be a list")
    seen: set[str] = set()
    for index, status in enumerate(sources):
        where = f"{label}[{index}]"
        if not isinstance(status, dict):
            raise AirportProductError(f"{where} must be an object")
        source_id = status.get("id")
        if not isinstance(source_id, str) or not SOURCE_KEY.match(source_id):
            raise AirportProductError(f"{where}.id must be a source id")
        if source_id in seen:
            raise AirportProductError(f"{label} lists {source_id} twice")
        seen.add(source_id)
        if not isinstance(status.get("ok"), bool):
            raise AirportProductError(f"{where}.ok must be a boolean")
        if not status["ok"] and not isinstance(status.get("error"), str):
            raise AirportProductError(f"{where} failed without an error message")
        if "fetched" in status:
            _time(status["fetched"], f"{where}.fetched")
        if "reports" in status:
            _integer(status["reports"], f"{where}.reports", (0, None))


# --- the files ---------------------------------------------------------


def validate_metar(payload: object, label: str) -> datetime:
    if not isinstance(payload, dict):
        raise AirportProductError(f"{label} must be an object")
    time = _time(payload.get("time"), f"{label}.time")
    if not isinstance(payload.get("raw"), str) or not payload["raw"]:
        raise AirportProductError(f"{label}.raw must be the report verbatim")
    _optional_number(payload.get("t"), f"{label}.t", TEMPERATURE_RANGE)
    _optional_number(payload.get("td"), f"{label}.td", TEMPERATURE_RANGE)
    _optional_integer(payload.get("wd"), f"{label}.wd", WIND_DIRECTION_RANGE)
    _optional_number(payload.get("ws"), f"{label}.ws", WIND_SPEED_RANGE)
    _optional_number(payload.get("gust"), f"{label}.gust", WIND_SPEED_RANGE)
    _optional_integer(payload.get("vis"), f"{label}.vis", (0, None))
    _optional_number(payload.get("qnh"), f"{label}.qnh", PRESSURE_RANGE)
    _optional_number(payload.get("slp"), f"{label}.slp", PRESSURE_RANGE)
    _optional_string(payload.get("wx"), f"{label}.wx")
    _cloud(payload.get("cloud"), f"{label}.cloud")
    _category(payload.get("category"), f"{label}.category")
    if not isinstance(payload.get("auto"), bool):
        raise AirportProductError(f"{label}.auto must be a boolean")
    if payload.get("type") not in METAR_TYPES:
        raise AirportProductError(f"{label}.type must be one of {', '.join(METAR_TYPES)}")
    return time


def validate_taf(payload: object, label: str) -> None:
    if not isinstance(payload, dict):
        raise AirportProductError(f"{label} must be an object or null")
    _time(payload.get("issued"), f"{label}.issued")
    start = _time(payload.get("from"), f"{label}.from")
    end = _time(payload.get("to"), f"{label}.to")
    if end < start:
        raise AirportProductError(f"{label} ends before it starts")
    if not isinstance(payload.get("raw"), str) or not payload["raw"]:
        raise AirportProductError(f"{label}.raw must be the forecast verbatim")
    if not isinstance(payload.get("amended"), bool):
        raise AirportProductError(f"{label}.amended must be a boolean")
    periods = payload.get("periods")
    if not isinstance(periods, list):
        raise AirportProductError(f"{label}.periods must be a list")
    for position, period in enumerate(periods):
        where = f"{label}.periods[{position}]"
        if not isinstance(period, dict):
            raise AirportProductError(f"{where} must be an object")
        period_start = _time(period.get("from"), f"{where}.from")
        period_end = _time(period.get("to"), f"{where}.to")
        if period_end < period_start:
            raise AirportProductError(f"{where} ends before it starts")
        change = period.get("change")
        if change is not None and change not in CHANGES:
            raise AirportProductError(f"{where}.change must be one of {', '.join(CHANGES)} or null")
        _optional_integer(period.get("prob"), f"{where}.prob", (0, 100))
        _optional_integer(period.get("wd"), f"{where}.wd", WIND_DIRECTION_RANGE)
        _optional_number(period.get("ws"), f"{where}.ws", WIND_SPEED_RANGE)
        _optional_number(period.get("gust"), f"{where}.gust", WIND_SPEED_RANGE)
        _optional_integer(period.get("vis"), f"{where}.vis", (0, None))
        _optional_string(period.get("wx"), f"{where}.wx")
        _cloud(period.get("cloud"), f"{where}.cloud")


def validate_history_station(payload: object, label: str) -> None:
    """One line of ``history.jsonl``: a station with its whole window of
    observations and its current forecast."""
    if not isinstance(payload, dict):
        raise AirportProductError(f"{label} must be an object")
    icao = payload.get("icao")
    if not isinstance(icao, str) or not ICAO.match(icao):
        raise AirportProductError(f"{label}.icao is not an ICAO station id")
    _optional_string(payload.get("name"), f"{label}.name")
    _number(payload.get("lat"), f"{label}.lat", LATITUDE_RANGE)
    _number(payload.get("lon"), f"{label}.lon", LONGITUDE_RANGE)
    _optional_number(payload.get("elev"), f"{label}.elev", ELEVATION_RANGE)
    _optional_string(payload.get("iata"), f"{label}.iata")
    _optional_string(payload.get("wmo"), f"{label}.wmo")
    metars = payload.get("metars")
    if not isinstance(metars, list) or not metars:
        raise AirportProductError(f"{label}.metars must be a non-empty list")
    previous: datetime | None = None
    for position, metar in enumerate(metars):
        time = validate_metar(metar, f"{label}.metars[{position}]")
        if previous is not None and time >= previous:
            raise AirportProductError(f"{label}.metars must be newest first, strictly decreasing")
        previous = time
    if payload.get("taf") is not None:
        validate_taf(payload["taf"], f"{label}.taf")


def validate_index(payload: object) -> None:
    if not isinstance(payload, dict):
        raise AirportProductError("index must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise AirportProductError(f"index schemaVersion must be {SCHEMA_VERSION}")
    issued = _time(payload.get("issued"), "index.issued")
    if issued.second or issued.microsecond or issued.minute % ROUND_MINUTES:
        raise AirportProductError(f"index.issued must be a round: a UTC minute divisible by {ROUND_MINUTES}")
    _time(payload.get("generated"), "index.generated")
    history = payload.get("history")
    if not isinstance(history, dict):
        raise AirportProductError("index.history must be an object")
    if history.get("path") != HISTORY_FILENAME:
        raise AirportProductError(f"index.history.path must be {HISTORY_FILENAME!r}, the file beside the index")
    history_bytes = history.get("byteLength")
    if isinstance(history_bytes, bool) or not isinstance(history_bytes, int) or history_bytes < 0:
        raise AirportProductError("index.history.byteLength must be a non-negative integer")
    if not isinstance(history.get("crc32"), str) or not CRC32.match(history["crc32"]):
        raise AirportProductError("index.history.crc32 must be 8 lowercase hex characters")
    stations = payload.get("stations")
    if not isinstance(stations, list):
        raise AirportProductError("index.stations must be a list")
    previous: str | None = None
    end = 0
    for position, row in enumerate(stations):
        label = f"index.stations[{position}]"
        if not isinstance(row, list) or len(row) != len(STATION_ROW):
            raise AirportProductError(f"{label} must be a row of {len(STATION_ROW)} values ({', '.join(STATION_ROW)})")
        icao, lat, lon, elev, obs_time, t, td, wd, ws, gust, vis, qnh, category, taf_present, offset, length = row
        if not isinstance(icao, str) or not ICAO.match(icao):
            raise AirportProductError(f"{label}.icao is not an ICAO station id")
        if previous is not None and icao <= previous:
            raise AirportProductError("index.stations must be sorted by icao and unique")
        previous = icao
        _number(lat, f"{label}.lat", LATITUDE_RANGE)
        _number(lon, f"{label}.lon", LONGITUDE_RANGE)
        _optional_number(elev, f"{label}.elev", ELEVATION_RANGE)
        _time(obs_time, f"{label}.obsTime")
        _optional_number(t, f"{label}.t", TEMPERATURE_RANGE)
        _optional_number(td, f"{label}.td", TEMPERATURE_RANGE)
        _optional_integer(wd, f"{label}.wd", WIND_DIRECTION_RANGE)
        _optional_number(ws, f"{label}.ws", WIND_SPEED_RANGE)
        _optional_number(gust, f"{label}.gust", WIND_SPEED_RANGE)
        _optional_integer(vis, f"{label}.vis", (0, None))
        _optional_number(qnh, f"{label}.qnh", PRESSURE_RANGE)
        _category(category, f"{label}.category")
        if taf_present not in (0, 1) or isinstance(taf_present, bool):
            raise AirportProductError(f"{label}.tafPresent must be 0 or 1")
        # The span is what makes one station one range request: in row
        # order, inside the file the index measured, and the object alone,
        # so the slice parses on its own.
        _integer(offset, f"{label}.offset", (0, None))
        _integer(length, f"{label}.length", (1, None))
        if offset < end:
            raise AirportProductError(f"{label}.offset must not precede the previous station's span")
        if offset + length > history_bytes:
            raise AirportProductError(f"{label} spans past the end of {HISTORY_FILENAME}")
        end = offset + length
    validate_sources(payload.get("sources"), "index.sources")


def build_pointer(issued: datetime, index_path: str, index_bytes: bytes) -> dict[str, Any]:
    payload = pointer_payload("airport", issued, index_path, index_bytes)
    validate_pointer(payload)
    return payload


def validate_pointer(payload: object) -> None:
    error = pointer_shape_error(payload, "airport")
    if error is not None:
        raise AirportProductError(error)
    assert isinstance(payload, dict)
    issued = _time(payload.get("issued"), "pointer.issued")
    if Path(payload["path"]).parts[0] != round_directory(issued):
        raise AirportProductError("airport pointer path does not name the issued round's directory")


def read_index(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AirportProductError(f"cannot read airport index {path}: {exc}") from exc
    validate_index(payload)
    return payload

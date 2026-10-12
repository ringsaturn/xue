"""The ``fire`` product, schema v1: construction and validation of the
three kinds of file — the mutable pointer ``latest-fire.json``, a round's
``index.json`` and the ``<sensor>.jsonl`` files beside it — written *and*
read through the validator, the posture ``synop/schema.py`` takes.
``docs/fire.md`` is the normative description.

Admission is structural. A sensor the reader has never seen is not an
error; what is checked is shape — the detection fields' encodings and
ranges, the ISO-8601 UTC times, the CRC32 pattern — and the rule the
sensor files add: one sensor's ``hours[]`` rows address consecutive lines
of its file, so any span of hours is one range request.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import FireProductError
from ..pointproduct import (  # noqa: F401 — re-exported: the fire modules import them from here
    CRC32,
    check_number,
    check_pointer,
    check_sources,
    check_time,
    crc32_hex,
    encode_json,
    iso_z,
    pointer_payload,
    read_index_file,
    write_bytes_atomic,
)

SCHEMA_VERSION = 1
PRODUCT = "fire"
POINTER_FILENAME = "latest-fire.json"
INDEX_FILENAME = "index.json"
SENSOR_FILE_SUFFIX = ".jsonl"

WINDOW_HOURS = 168
"""How much of the past a round's sensor files carry: seven days, enough
to watch a fire's spread across several overpasses a day."""

LATITUDE_RANGE = (-900000, 900000)
LONGITUDE_RANGE = (-1800000, 1800000)
"""Positions in the sensor files are integers of 1e-4 degrees."""
BT_RANGE = (2000, 6000)
"""Brightness temperature in 0.1 K. VIIRS I4 saturates near 367 K and
MODIS band 21 near 500 K; the bounds keep a unit error out, not a hot fire."""
FRP_RANGE = (0, None)
SECONDS_RANGE = (0, 3540)
CONFIDENCE = frozenset("lnh")
DAY_NIGHT = frozenset("DN")
RESOLUTION_RANGE = (1, 100000)

ROW = ("sensor", "hour", "count", "frpMax", "offset", "length")
"""An ``hours[]`` row: the sensor's position in ``sensors[]``, the UTC hour,
its detections, their largest FRP in MW, and the byte span of its line."""

ROUND = re.compile(r"^\d{12}$")
SENSOR = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
SOURCE_KEY = re.compile(r"^[a-z][a-z0-9-]*$")


def parse_round(value: str) -> datetime:
    """``202610120500`` → the round's UTC hour. A minute other than 00
    names no round."""
    if not ROUND.match(value):
        raise FireProductError("round must be a UTC minute, YYYYMMDDHHMM")
    try:
        moment = datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError as exc:
        raise FireProductError(f"round is not a valid minute: {value}") from exc
    if moment.minute:
        raise FireProductError(f"round must be a whole hour: {value}")
    return moment


def floor_round(moment: datetime) -> datetime:
    """The round a moment falls in: its UTC hour."""
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def round_name(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M")


def round_directory(moment: datetime) -> str:
    """``fire.202610120500``: one immutable directory per round, flat under
    the data root like the synop rounds. A round carries the whole window
    and lives six hours, so there is no archive to file by day."""
    return f"fire.{round_name(moment)}"


def sensor_filename(sensor: str) -> str:
    return f"{sensor}{SENSOR_FILE_SUFFIX}"


# --- primitives --------------------------------------------------------


Bounds = tuple[float | None, float | None]


def _time(value: object, label: str) -> datetime:
    return check_time(FireProductError, value, label)


def _integer(value: object, label: str, bounds: Bounds | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FireProductError(f"{label} must be an integer")
    minimum, maximum = bounds or (None, None)
    if minimum is not None and value < minimum:
        raise FireProductError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise FireProductError(f"{label} must be at most {maximum}")


def _crc_file(value: object, label: str, path: str) -> int:
    if not isinstance(value, dict):
        raise FireProductError(f"{label} must be an object")
    if value.get("path") != path:
        raise FireProductError(f"{label}.path must be {path!r}, the file beside the index")
    byte_length = value.get("byteLength")
    if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 0:
        raise FireProductError(f"{label}.byteLength must be a non-negative integer")
    if not isinstance(value.get("crc32"), str) or not CRC32.match(value["crc32"]):
        raise FireProductError(f"{label}.crc32 must be 8 lowercase hex characters")
    return byte_length


def validate_sources(sources: object, label: str) -> None:
    def extra(status: dict[str, Any], where: str) -> None:
        if "fetched" in status:
            _time(status["fetched"], f"{where}.fetched")
        for key in ("reports", "rejected"):
            if key in status:
                _integer(status[key], f"{where}.{key}", (0, None))

    check_sources(FireProductError, sources, label, SOURCE_KEY, extra)


# --- the files ---------------------------------------------------------


def validate_sensor(payload: object, label: str) -> int:
    """One entry of the index's ``sensors``; returns its file's length."""
    if not isinstance(payload, dict):
        raise FireProductError(f"{label} must be an object")
    sensor = payload.get("id")
    if not isinstance(sensor, str) or not SENSOR.match(sensor):
        raise FireProductError(f"{label}.id is not a sensor id")
    for key in ("name", "attribution", "license", "url"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise FireProductError(f"{label}.{key} must be a non-empty string")
    _integer(payload.get("resolution"), f"{label}.resolution", RESOLUTION_RANGE)
    _time(payload.get("latest"), f"{label}.latest")
    _integer(payload.get("count"), f"{label}.count", (1, None))
    return _crc_file(payload.get("file"), f"{label}.file", sensor_filename(sensor))


def validate_hour_line(payload: object, label: str) -> None:
    """One line of a sensor file: an hour's detections, column by column,
    sorted and unique on (``t``, ``lat``, ``lon``)."""
    if not isinstance(payload, dict):
        raise FireProductError(f"{label} must be an object")
    sensor = payload.get("sensor")
    if not isinstance(sensor, str) or not SENSOR.match(sensor):
        raise FireProductError(f"{label}.sensor is not a sensor id")
    hour = payload.get("hour")
    _integer(hour, f"{label}.hour", (0, None))
    if hour % 3600:
        raise FireProductError(f"{label}.hour must be a whole UTC hour")
    count = payload.get("n")
    _integer(count, f"{label}.n", (1, None))
    columns = {key: payload.get(key) for key in ("t", "lat", "lon", "frp", "bt")}
    for key, values in columns.items():
        if not isinstance(values, list) or len(values) != count:
            raise FireProductError(f"{label}.{key} must be a list of n values")
    for key, allowed in (("conf", CONFIDENCE), ("dn", DAY_NIGHT)):
        value = payload.get(key)
        if not isinstance(value, str) or len(value) != count or not set(value) <= allowed:
            raise FireProductError(f"{label}.{key} must be n characters of {''.join(sorted(allowed))}")
    bounds = {"t": SECONDS_RANGE, "lat": LATITUDE_RANGE, "lon": LONGITUDE_RANGE, "frp": FRP_RANGE, "bt": BT_RANGE}
    previous: tuple[int, int, int] | None = None
    for position in range(count):
        for key, values in columns.items():
            _integer(values[position], f"{label}.{key}[{position}]", bounds[key])
        if columns["t"][position] % 60:
            raise FireProductError(f"{label}.t[{position}] must be a whole minute")
        key = (columns["t"][position], columns["lat"][position], columns["lon"][position])
        if previous is not None and key <= previous:
            raise FireProductError(f"{label} detections must be sorted by (t, lat, lon) and unique")
        previous = key


def validate_index(payload: object) -> None:
    if not isinstance(payload, dict):
        raise FireProductError("index must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise FireProductError(f"index schemaVersion must be {SCHEMA_VERSION}")
    issued = _time(payload.get("issued"), "index.issued")
    if issued.second or issued.microsecond or issued.minute:
        raise FireProductError("index.issued must be a round: a whole UTC hour")
    _time(payload.get("generated"), "index.generated")
    window = payload.get("window")
    if not isinstance(window, dict):
        raise FireProductError("index.window must be an object")
    start = _time(window.get("start"), "index.window.start")
    end = _time(window.get("end"), "index.window.end")
    if start >= end:
        raise FireProductError("index.window.start must precede its end")

    sensors = payload.get("sensors")
    if not isinstance(sensors, list):
        raise FireProductError("index.sensors must be a list")
    lengths: list[int] = []
    seen: set[str] = set()
    for position, sensor in enumerate(sensors):
        lengths.append(validate_sensor(sensor, f"index.sensors[{position}]"))
        if sensor["id"] in seen:
            raise FireProductError(f"index.sensors lists {sensor['id']} twice")
        seen.add(sensor["id"])

    hours = payload.get("hours")
    if not isinstance(hours, list):
        raise FireProductError("index.hours must be a list")
    previous: tuple[int, datetime] | None = None
    ends = [0] * len(sensors)
    counts = [0] * len(sensors)
    for position, row in enumerate(hours):
        label = f"index.hours[{position}]"
        if not isinstance(row, list) or len(row) != len(ROW):
            raise FireProductError(f"{label} must be a row of {len(ROW)} values ({', '.join(ROW)})")
        sensor, hour_value, count, frp_max, offset, length = row
        _integer(sensor, f"{label}.sensor", (0, len(sensors) - 1))
        hour = _time(hour_value, f"{label}.hour")
        if hour.minute or hour.second or hour.microsecond:
            raise FireProductError(f"{label}.hour must be a whole UTC hour")
        if hour < start.replace(minute=0, second=0, microsecond=0):
            raise FireProductError(f"{label}.hour lies before the window")
        if previous is not None and (sensor, hour) <= previous:
            raise FireProductError("index.hours must be sorted by sensor, then hour, and unique")
        previous = (sensor, hour)
        _integer(count, f"{label}.count", (1, None))
        check_number(FireProductError, frp_max, f"{label}.frpMax", minimum=0)
        # One sensor's lines are back to back, each followed by its
        # newline, so a span of hours is one contiguous range.
        _integer(offset, f"{label}.offset", (0, None))
        _integer(length, f"{label}.length", (1, None))
        if offset != ends[sensor]:
            raise FireProductError(f"{label}.offset must follow the previous hour's line of its sensor")
        ends[sensor] = offset + length + 1
        counts[sensor] += count
    for position, sensor in enumerate(sensors):
        if ends[position] != lengths[position]:
            raise FireProductError(f"index.hours do not span {sensor_filename(sensor['id'])} exactly")
        if counts[position] != sensor["count"]:
            raise FireProductError(f"index.sensors[{position}].count is not the sum of its hours")
    validate_sources(payload.get("sources"), "index.sources")


def build_pointer(issued: datetime, index_path: str, index_bytes: bytes) -> dict[str, Any]:
    payload = pointer_payload(PRODUCT, issued, index_path, index_bytes)
    validate_pointer(payload)
    return payload


def validate_pointer(payload: object) -> None:
    check_pointer(
        FireProductError,
        payload,
        PRODUCT,
        lambda issued: (round_directory(issued),),
        "fire pointer path does not name the issued round's directory",
    )


def read_index(path: Path) -> dict[str, Any]:
    return read_index_file(FireProductError, path, PRODUCT, validate_index)

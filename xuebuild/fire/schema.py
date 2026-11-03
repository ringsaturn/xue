"""The ``fire`` product, schema v1: construction and validation of the
three kinds of file — the mutable pointer ``latest-fire.json``, a round's
``index.json`` and the ``<sensor>.jsonl`` files beside it — written *and*
read through the validator. ``docs/fire.md`` is the normative description.

The files' shapes, encodings and ranges are
``docs/schemas/fire-{index,line}.schema.json``, which the tests hold every
golden to. What is checked here is what a schema cannot say: one sensor's
``hours[]`` rows address consecutive lines of its file, so any span of
hours is one range request; the counts add up; every column of a line is
``n`` long and its detections sorted and unique. The ranges below are the
reader's (``firms.py``), which drops a row outside them.
"""

from __future__ import annotations

import re
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..errors import FireProductError
from ..pointproduct import (  # noqa: F401 — re-exported: the fire modules import them from here
    RoundProduct,
    check_sources,
    crc32_hex,
    encode_json,
    iso_z,
    read_index_file,
    shaped,
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

ROW = ("sensor", "hour", "count", "frpMax", "offset", "length")
"""An ``hours[]`` row: the sensor's position in ``sensors[]``, the UTC hour,
its detections, their largest FRP in MW, and the byte span of its line."""

SOURCE_KEY = re.compile(r"^[a-z][a-z0-9-]*$")
COLUMNS = ("t", "lat", "lon", "frp", "bt", "conf", "dn")


ROUNDS = RoundProduct(PRODUCT, FireProductError, cadence_minutes=60, archived=False)
parse_round = ROUNDS.parse_round
floor_round = ROUNDS.floor_round
round_name = ROUNDS.round_name
round_directory = ROUNDS.round_directory
raw_directory = ROUNDS.raw_directory
pointer_directories = ROUNDS.pointer_directories
local_round_directory = ROUNDS.local_round_directory
build_pointer = ROUNDS.build_pointer
validate_pointer = ROUNDS.validate_pointer


def sensor_filename(sensor: str) -> str:
    return f"{sensor}{SENSOR_FILE_SUFFIX}"


# --- the files ---------------------------------------------------------


def validate_hour_line(payload: dict[str, Any], label: str) -> None:
    """One line of a sensor file: every column ``n`` long, the detections
    sorted and unique on (``t``, ``lat``, ``lon``)."""
    with shaped(FireProductError, label):
        _hour_line(payload, label)


def _hour_line(payload: dict[str, Any], label: str) -> None:
    count = payload["n"]
    for key in COLUMNS:
        if len(payload[key]) != count:
            raise FireProductError(f"{label}.{key} must be n long")
    keys = list(zip(payload["t"], payload["lat"], payload["lon"], strict=True))
    if any(later <= earlier for earlier, later in pairwise(keys)):
        raise FireProductError(f"{label} detections must be sorted by (t, lat, lon) and unique")


def validate_index(payload: dict[str, Any]) -> None:
    with shaped(FireProductError, "index"):
        _index(payload)


def _index(payload: dict[str, Any]) -> None:
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise FireProductError(f"index schemaVersion must be {SCHEMA_VERSION}")
    start = ROUNDS.time(payload["window"]["start"], "index.window.start")
    if start >= ROUNDS.time(payload["window"]["end"], "index.window.end"):
        raise FireProductError("index.window.start must precede its end")

    sensors = payload["sensors"]
    seen: set[str] = set()
    for position, sensor in enumerate(sensors):
        if sensor["file"]["path"] != sensor_filename(sensor["id"]):
            raise FireProductError(f"index.sensors[{position}].file.path must be {sensor_filename(sensor['id'])!r}")
        if sensor["id"] in seen:
            raise FireProductError(f"index.sensors lists {sensor['id']} twice")
        seen.add(sensor["id"])

    first_hour = start.replace(minute=0, second=0, microsecond=0)
    previous: tuple[int, datetime] | None = None
    ends = [0] * len(sensors)
    counts = [0] * len(sensors)
    for position, row in enumerate(payload["hours"]):
        label = f"index.hours[{position}]"
        sensor, hour_value, count, _, offset, length = row[: len(ROW)]
        if sensor >= len(sensors):
            raise FireProductError(f"{label}.sensor names no sensor")
        hour = ROUNDS.time(hour_value, f"{label}.hour")
        if hour < first_hour:
            raise FireProductError(f"{label}.hour lies before the window")
        if previous is not None and (sensor, hour) <= previous:
            raise FireProductError("index.hours must be sorted by sensor, then hour, and unique")
        previous = (sensor, hour)
        # One sensor's lines are back to back, each followed by its
        # newline, so a span of hours is one contiguous range.
        if offset != ends[sensor]:
            raise FireProductError(f"{label}.offset must follow the previous hour's line of its sensor")
        ends[sensor] = offset + length + 1
        counts[sensor] += count
    for position, sensor in enumerate(sensors):
        if ends[position] != sensor["file"]["byteLength"]:
            raise FireProductError(f"index.hours do not span {sensor_filename(sensor['id'])} exactly")
        if counts[position] != sensor["count"]:
            raise FireProductError(f"index.sensors[{position}].count is not the sum of its hours")
    check_sources(FireProductError, payload["sources"], "index.sources", SOURCE_KEY, lambda status, where: None)


def read_index(path: Path) -> dict[str, Any]:
    return read_index_file(FireProductError, path, PRODUCT, validate_index)

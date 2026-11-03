"""The ``aq`` product, schema v1: construction and validation of the four
kinds of file — the mutable pointer ``latest-aq.json``, a round's
``index.json``, and per network the ``<network>.jsonl`` and the
``<network>.stations.json`` beside it — written *and* read through the
validator. ``docs/aq.md`` is the normative description.

Admission is structural. A network, a station or an element key the reader
has never seen is not an error. The shapes are
``docs/schemas/aq-{index,stations,line}.schema.json``; checked here is what
a schema cannot say — the row length the index's own ``elements`` list
implies, the ranges of the elements v1 defines in a row, and the rule the
stations file adds: each row's byte span lies, in order, inside its
network's ``.jsonl``, so a reader slices one station out by range and
parses the slice on its own.
"""

from __future__ import annotations

import re
from itertools import pairwise
from pathlib import Path
from typing import Any

from ..errors import XueError
from ..pointproduct import (  # noqa: F401 — re-exported: the aq modules import them from here
    RoundProduct,
    archive_round_directory,
    check_sources,
    crc32_hex,
    encode_json,
    existing_round_directory,
    flat_round_directory,
    iso_z,
    read_index_file,
    round_directories,
    shaped,
    write_bytes_atomic,
)


class AqProductError(XueError):
    """An ``aq`` file that does not meet ``docs/aq.md``, or a build that
    cannot proceed."""


SCHEMA_VERSION = 1
PRODUCT = "aq"
POINTER_FILENAME = "latest-aq.json"
INDEX_FILENAME = "index.json"
NETWORK_FILE_SUFFIX = ".jsonl"
STATIONS_FILE_SUFFIX = ".stations.json"

ROUND_MINUTES = 5
"""A round is a UTC minute that is a multiple of this. The schedule runs
twice an hour; the finer grid only keeps a late-starting run in a round of
its own name."""

HISTORY_HOURS = 24
"""How much of each station's past its network file carries."""

ELEMENT_BOUNDS: dict[str, tuple[float, float]] = {
    "pm25": (-5.0, 10000.0),
    "pm10": (-5.0, 10000.0),
    "o3": (-5.0, 1000.0),
    "no2": (-5.0, 2000.0),
    "so2": (-5.0, 5000.0),
    "co": (-0.5, 100.0),
}
"""The elements v1 defines, in the order the stations files lay them out,
with the range each stays inside. The floors are below zero because an
analyser near zero drifts, and AirNow publishes its small negative hours
as measured; the adapters write null outside the range rather than lose
the round."""

ELEMENTS = tuple(ELEMENT_BOUNDS)
ELEMENT_DECIMALS = {"pm25": 1, "pm10": 1, "o3": 1, "no2": 1, "so2": 1, "co": 2}

GAS_REFERENCE = {"temperature": 293, "pressure": 101325}
"""The reference conditions, kelvin and pascals, a network that reports
gases in µg/m³ states them at, published as its ``gasReference``. Hong
Kong's EPD adjusts every gas measurement to 293 K and 101.325 kPa (its Air
Quality Objectives' note)."""

MOLAR_VOLUME = round(8.314462618 * GAS_REFERENCE["temperature"] / GAS_REFERENCE["pressure"] * 1000, 2)
"""Litres per mole of an ideal gas at :data:`GAS_REFERENCE`, 24.04: what
the mass concentrations are turned back into mixing ratios with."""

MOLAR_MASS = {"o3": 48.00, "no2": 46.01, "so2": 64.07, "co": 28.01}

ELEMENT_TABLE: dict[str, dict[str, Any]] = {
    "pm25": {"unit": "µg/m³", "cell": "mean", "period": "network"},
    "pm10": {"unit": "µg/m³", "cell": "mean", "period": "network"},
    "o3": {"unit": "ppb", "cell": "mean", "period": "network"},
    "no2": {"unit": "ppb", "cell": "mean", "period": "network"},
    "so2": {"unit": "ppb", "cell": "mean", "period": "network"},
    "co": {"unit": "ppm", "cell": "mean", "period": "network"},
}
"""The index's ``elementTable``: every element a mean over the network's
``avgPeriod``, ending at its time."""

ROW_HEAD = ("id", "lat", "lon", "elev", "rank", "name", "agency", "obsTime")
ROW_TAIL = ("offset", "length")
"""A stations-file row is ``ROW_HEAD``, then the current value of each of
the file's ``elements`` in that order, then ``ROW_TAIL`` — the byte span of
the station's line in its network's ``.jsonl``, the JSON object alone."""

ELEVATION_RANGE = (-500.0, 9000.0)

STATION = re.compile(r"^([a-z][a-z0-9]*(?:-[a-z0-9]+)*):[0-9A-Za-z_.-]+$")
ELEMENT = re.compile(r"^[a-z][a-z0-9]*$")
SOURCE_KEY = re.compile(r"^[a-z][a-z0-9-]*$")


ROUNDS = RoundProduct(PRODUCT, AqProductError, cadence_minutes=ROUND_MINUTES)
parse_round = ROUNDS.parse_round
floor_round = ROUNDS.floor_round
round_name = ROUNDS.round_name
round_directory = ROUNDS.round_directory
raw_directory = ROUNDS.raw_directory
pointer_directories = ROUNDS.pointer_directories
local_round_directory = ROUNDS.local_round_directory
build_pointer = ROUNDS.build_pointer
validate_pointer = ROUNDS.validate_pointer


def network_filename(network: str) -> str:
    return f"{network}{NETWORK_FILE_SUFFIX}"


def stations_filename(network: str) -> str:
    return f"{network}{STATIONS_FILE_SUFFIX}"


def to_mixing_ratio(key: str, micrograms: float) -> float:
    """A gas's µg/m³ → ppb (``co``: ppm) at :data:`GAS_REFERENCE`."""
    ppb = micrograms * MOLAR_VOLUME / MOLAR_MASS[key]
    return ppb / 1000.0 if key == "co" else ppb


def bounded(key: str, value: float | None) -> float | None:
    """A value at the element's precision, or null outside its range."""
    if value is None:
        return None
    minimum, maximum = ELEMENT_BOUNDS[key]
    if value != value or value < minimum or value > maximum:
        return None
    rounded = round(value, ELEMENT_DECIMALS[key])
    return 0.0 if rounded == 0 else rounded  # no -0.0 in the files


# --- the files ---------------------------------------------------------
#
# The shapes are docs/schemas/aq-{index,stations,line}.schema.json; the
# tests hold every golden to them. What follows is what a schema cannot
# say, and a file without the schema's shape is refused through `shaped`.


def columns(elements: list[str]) -> list[str]:
    """A stations file's ``columns``: its rows' layout by name. A v1 file
    keeps the positional layout readers from before the field rely on."""
    return [*ROW_HEAD, *elements, *ROW_TAIL]


def _unique(values: list[str], label: str) -> None:
    if len(set(values)) != len(values):
        raise AqProductError(f"{label} lists a key twice")


def validate_index(payload: dict[str, Any]) -> None:
    with shaped(AqProductError, "index"):
        _index(payload)


def _index(payload: dict[str, Any]) -> None:
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise AqProductError(f"index schemaVersion must be {SCHEMA_VERSION}")
    _unique([network["id"] for network in payload["networks"]], "index.networks")
    for position, network in enumerate(payload["networks"]):
        for key, name in (("file", network_filename(network["id"])), ("stations", stations_filename(network["id"]))):
            if network[key]["path"] != name:
                raise AqProductError(f"index.networks[{position}].{key}.path must be {name!r}")
    elements = payload["elements"]
    _unique(elements, "index.elements")
    table = payload.get("elementTable")
    if table is not None and list(table) != elements:
        raise AqProductError("index.elementTable must have one entry per element, in the elements' order")
    check_sources(AqProductError, payload["sources"], "index.sources", SOURCE_KEY, lambda status, where: None)


def validate_stations_file(payload: dict[str, Any], network: dict[str, Any], elements: list[str], issued: str | None = None) -> None:
    """A ``<network>.stations.json`` against the index entry that names it:
    the index's ``network``, ``issued`` and ``elements`` repeated exactly,
    ``columns`` the v1 layout, ``count`` rows sorted by id, every id the
    network's, every element of §2 in its range, and every span inside the
    network's ``.jsonl`` in row order."""
    label = stations_filename(network["id"])
    with shaped(AqProductError, label):
        _stations_file(payload, network, elements, issued, label)


def _stations_file(payload: dict[str, Any], network: dict[str, Any], elements: list[str], issued: str | None, label: str) -> None:
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise AqProductError(f"{label} schemaVersion must be {SCHEMA_VERSION}")
    if payload["network"] != network["id"]:
        raise AqProductError(f"{label}.network must be {network['id']!r}")
    if issued is not None and payload["issued"] != issued:
        raise AqProductError(f"{label}.issued is not the index's")
    if payload["elements"] != list(elements):
        raise AqProductError(f"{label}.elements must equal the index's elements")
    layout = columns(list(elements))
    if "columns" in payload and payload["columns"] != layout:
        raise AqProductError(f"{label}.columns must be {', '.join(layout)}")
    rows = payload["stations"]
    if len(rows) != network["stations"]["count"]:
        raise AqProductError(f"{label} has {len(rows)} rows but the index counts {network['stations']['count']}")
    file_length = network["file"]["byteLength"]
    previous: str | None = None
    end = 0
    for position, row in enumerate(rows):
        where = f"{label}.stations[{position}]"
        if len(row) != len(layout):
            raise AqProductError(f"{where} must be a row of {len(layout)} values ({', '.join(layout)})")
        station = row[0]
        if STATION.match(station).group(1) != network["id"]:
            raise AqProductError(f"{where}.id does not belong to network {network['id']}")
        if previous is not None and station <= previous:
            raise AqProductError(f"{label}.stations must be sorted by id and unique")
        previous = station
        for key, value in zip(elements, row[len(ROW_HEAD) : -len(ROW_TAIL)], strict=True):
            bounds = ELEMENT_BOUNDS.get(key)
            if value is not None and bounds is not None and not bounds[0] <= value <= bounds[1]:
                raise AqProductError(f"{where}.{key} is outside {bounds[0]}..{bounds[1]}")
        # The span is what makes one station one range request: in row
        # order, inside the file the index measured, and the object alone,
        # so the slice parses on its own.
        offset, length = row[-len(ROW_TAIL) :]
        if offset < end:
            raise AqProductError(f"{where}.offset must not precede the previous station's span")
        if offset + length > file_length:
            raise AqProductError(f"{where} spans past the end of {network_filename(network['id'])}")
        end = offset + length


def validate_station_line(payload: dict[str, Any], label: str) -> None:
    """One line of a network file: ``time`` strictly increasing, and every
    array under ``obs`` an element's and as long as ``time``."""
    with shaped(AqProductError, label):
        times = payload["time"]
        if any(later <= earlier for earlier, later in pairwise(times)):
            raise AqProductError(f"{label}.time must be strictly increasing")
        for key, values in payload["obs"].items():
            if not ELEMENT.match(key):
                raise AqProductError(f"{label}.obs has a malformed element key {key!r}")
            if len(values) != len(times):
                raise AqProductError(f"{label}.obs.{key} must be as long as time")


def read_index(path: Path) -> dict[str, Any]:
    return read_index_file(AqProductError, path, PRODUCT, validate_index)

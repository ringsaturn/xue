"""The ``aq`` product, schema v1: construction and validation of the four
kinds of file — the mutable pointer ``latest-aq.json``, a round's
``index.json``, and per network the ``<network>.jsonl`` and the
``<network>.stations.json`` beside it — written *and* read through the
validator. ``docs/aq.md`` is the normative description.

Admission is structural. A network, a station or an element key the reader
has never seen is not an error; what is checked is shape — the row length
the index's own ``elements`` list implies, the ranges of the elements v1
defines, the ISO-8601 UTC times, the CRC32 pattern — and the rule the
stations file adds: each row's byte span lies, in order, inside its
network's ``.jsonl``, so a reader slices one station out by range and
parses the slice on its own.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import XueError
from ..pointproduct import (  # noqa: F401 — re-exported: the aq modules import them from here
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

MOLAR_VOLUME = 24.04
"""Litres per mole of an ideal gas at 20 °C and 1 atm, the reference the
mass concentrations of a network that reports gases in µg/m³ are turned
back into mixing ratios with."""

MOLAR_MASS = {"o3": 48.00, "no2": 46.01, "so2": 64.07, "co": 28.01}

ROW_HEAD = ("id", "lat", "lon", "elev", "rank", "name", "agency", "obsTime")
ROW_TAIL = ("offset", "length")
"""A stations-file row is ``ROW_HEAD``, then the current value of each of
the file's ``elements`` in that order, then ``ROW_TAIL`` — the byte span of
the station's line in its network's ``.jsonl``, the JSON object alone."""

RANKS = (0, 1, 2)
"""How prominent a station is, which a map thins by: 0 a principal or
background station, 1 a general station, 2 a roadside station."""

LATITUDE_RANGE = (-90.0, 90.0)
LONGITUDE_RANGE = (-180.0, 180.0)
ELEVATION_RANGE = (-500.0, 9000.0)
CADENCE_RANGE = (60, 86400)

ROUND = re.compile(r"^\d{12}$")
NETWORK = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
STATION = re.compile(r"^([a-z][a-z0-9]*(?:-[a-z0-9]+)*):[0-9A-Za-z_.-]+$")
ELEMENT = re.compile(r"^[a-z][a-z0-9]*$")
SOURCE_KEY = re.compile(r"^[a-z][a-z0-9-]*$")


def parse_round(value: str) -> datetime:
    """``202610311545`` → the round's UTC minute. A minute that is not a
    multiple of :data:`ROUND_MINUTES` names no round."""
    if not ROUND.match(value):
        raise AqProductError("round must be a UTC minute, YYYYMMDDHHMM")
    try:
        moment = datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError as exc:
        raise AqProductError(f"round is not a valid minute: {value}") from exc
    if moment.minute % ROUND_MINUTES:
        raise AqProductError(f"round minute must be a multiple of {ROUND_MINUTES}: {value}")
    return moment


def floor_round(moment: datetime) -> datetime:
    """The round a moment falls in: the minute floored to the cadence."""
    moment = moment.astimezone(UTC)
    return moment.replace(minute=(moment.minute // ROUND_MINUTES) * ROUND_MINUTES, second=0, microsecond=0)


def round_name(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M")


def round_directory(moment: datetime) -> str:
    """``aq.202610311545``: one immutable directory per round, flat under
    the data root. A round carries the whole 24-hour window and lives three
    hours, so there is no archive to file by day."""
    return f"aq.{round_name(moment)}"


def network_filename(network: str) -> str:
    return f"{network}{NETWORK_FILE_SUFFIX}"


def stations_filename(network: str) -> str:
    return f"{network}{STATIONS_FILE_SUFFIX}"


def to_mixing_ratio(key: str, micrograms: float) -> float:
    """A gas's µg/m³ → ppb (``co``: ppm) at the 20 °C, 1 atm reference."""
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


# --- primitives --------------------------------------------------------


Bounds = tuple[float | None, float | None]


def _time(value: object, label: str) -> datetime:
    return check_time(AqProductError, value, label)


def _number(value: object, label: str, bounds: Bounds | None = None) -> None:
    minimum, maximum = bounds or (None, None)
    check_number(AqProductError, value, label, minimum=minimum, maximum=maximum)


def _integer(value: object, label: str, bounds: Bounds | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AqProductError(f"{label} must be an integer")
    minimum, maximum = bounds or (None, None)
    if minimum is not None and value < minimum:
        raise AqProductError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise AqProductError(f"{label} must be at most {maximum}")


def _optional_number(value: object, label: str, bounds: Bounds | None = None) -> None:
    if value is not None:
        _number(value, label, bounds)


def _optional_string(value: object, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise AqProductError(f"{label} must be a string or null")


def _element_value(key: str, value: object, label: str) -> None:
    """One observed value: null, or a number inside the element's range.
    An element v1 does not define is only required to be a number."""
    if value is None:
        return
    _number(value, label, ELEMENT_BOUNDS.get(key))


def _crc_file(value: object, label: str, path: str) -> int:
    if not isinstance(value, dict):
        raise AqProductError(f"{label} must be an object")
    if value.get("path") != path:
        raise AqProductError(f"{label}.path must be {path!r}, the file beside the index")
    byte_length = value.get("byteLength")
    if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 0:
        raise AqProductError(f"{label}.byteLength must be a non-negative integer")
    if not isinstance(value.get("crc32"), str) or not CRC32.match(value["crc32"]):
        raise AqProductError(f"{label}.crc32 must be 8 lowercase hex characters")
    return byte_length


def _elements(value: object, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(key, str) and ELEMENT.match(key) for key in value)
        or len(set(value)) != len(value)
    ):
        raise AqProductError(f"{label} must be a list of unique element keys")
    return value


def validate_sources(sources: object, label: str) -> None:
    def extra(status: dict[str, Any], where: str) -> None:
        if "fetched" in status:
            _time(status["fetched"], f"{where}.fetched")
        for key in ("reports", "files", "missing", "unplaced"):
            if key in status:
                _integer(status[key], f"{where}.{key}", (0, None))

    check_sources(AqProductError, sources, label, SOURCE_KEY, extra)


# --- the files ---------------------------------------------------------


def validate_network(payload: object, label: str) -> None:
    """One entry of the index's ``networks``."""
    if not isinstance(payload, dict):
        raise AqProductError(f"{label} must be an object")
    network = payload.get("id")
    if not isinstance(network, str) or not NETWORK.match(network):
        raise AqProductError(f"{label}.id is not a network id")
    for key in ("name", "attribution", "license", "url"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise AqProductError(f"{label}.{key} must be a non-empty string")
    _integer(payload.get("cadence"), f"{label}.cadence", CADENCE_RANGE)
    if payload.get("avgPeriod") is not None:
        _integer(payload["avgPeriod"], f"{label}.avgPeriod", CADENCE_RANGE)
    if not isinstance(payload.get("preliminary"), bool):
        raise AqProductError(f"{label}.preliminary must be a boolean")
    if payload.get("latest") is not None:
        _time(payload["latest"], f"{label}.latest")
    _crc_file(payload.get("file"), f"{label}.file", network_filename(network))
    _crc_file(payload.get("stations"), f"{label}.stations", stations_filename(network))
    _integer(payload["stations"].get("count"), f"{label}.stations.count", (0, None))


def validate_index(payload: object) -> None:
    if not isinstance(payload, dict):
        raise AqProductError("index must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise AqProductError(f"index schemaVersion must be {SCHEMA_VERSION}")
    issued = _time(payload.get("issued"), "index.issued")
    if issued.second or issued.microsecond or issued.minute % ROUND_MINUTES:
        raise AqProductError(f"index.issued must be a round: a UTC minute divisible by {ROUND_MINUTES}")
    _time(payload.get("generated"), "index.generated")
    networks = payload.get("networks")
    if not isinstance(networks, list):
        raise AqProductError("index.networks must be a list")
    seen: set[str] = set()
    for position, network in enumerate(networks):
        validate_network(network, f"index.networks[{position}]")
        if network["id"] in seen:
            raise AqProductError(f"index.networks lists {network['id']} twice")
        seen.add(network["id"])
    _elements(payload.get("elements"), "index.elements")
    validate_sources(payload.get("sources"), "index.sources")


def validate_stations_file(payload: object, network: dict[str, Any], elements: list[str], issued: str | None = None) -> None:
    """A ``<network>.stations.json`` against the index entry that names it:
    the index's ``elements`` repeated exactly, ``count`` rows sorted by id,
    every id the network's, and every span inside the network's ``.jsonl``
    in row order."""
    label = stations_filename(network["id"])
    if not isinstance(payload, dict):
        raise AqProductError(f"{label} must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise AqProductError(f"{label} schemaVersion must be {SCHEMA_VERSION}")
    if payload.get("network") != network["id"]:
        raise AqProductError(f"{label}.network must be {network['id']!r}")
    stamp = _time(payload.get("issued"), f"{label}.issued")
    if issued is not None and stamp != _time(issued, "index.issued"):
        raise AqProductError(f"{label}.issued is not the index's")
    if _elements(payload.get("elements"), f"{label}.elements") != list(elements):
        raise AqProductError(f"{label}.elements must equal the index's elements")
    width = len(ROW_HEAD) + len(elements) + len(ROW_TAIL)
    rows = payload.get("stations")
    if not isinstance(rows, list):
        raise AqProductError(f"{label}.stations must be a list")
    if len(rows) != network["stations"]["count"]:
        raise AqProductError(f"{label} has {len(rows)} rows but the index counts {network['stations']['count']}")
    file_length = network["file"]["byteLength"]
    previous: str | None = None
    end = 0
    for position, row in enumerate(rows):
        where = f"{label}.stations[{position}]"
        if not isinstance(row, list) or len(row) != width:
            raise AqProductError(f"{where} must be a row of {width} values ({', '.join(ROW_HEAD)}, the elements, {', '.join(ROW_TAIL)})")
        station, lat, lon, elev, rank, name, agency, obs_time = row[: len(ROW_HEAD)]
        values = row[len(ROW_HEAD) : -len(ROW_TAIL)]
        offset, length = row[-len(ROW_TAIL) :]
        match = STATION.match(station) if isinstance(station, str) else None
        if match is None:
            raise AqProductError(f"{where}.id is not a <network>:<station> id")
        if match.group(1) != network["id"]:
            raise AqProductError(f"{where}.id does not belong to network {network['id']}")
        if previous is not None and station <= previous:
            raise AqProductError(f"{label}.stations must be sorted by id and unique")
        previous = station
        _number(lat, f"{where}.lat", LATITUDE_RANGE)
        _number(lon, f"{where}.lon", LONGITUDE_RANGE)
        _optional_number(elev, f"{where}.elev", ELEVATION_RANGE)
        if rank not in RANKS or isinstance(rank, bool):
            raise AqProductError(f"{where}.rank must be one of {RANKS}")
        _optional_string(name, f"{where}.name")
        _optional_string(agency, f"{where}.agency")
        _time(obs_time, f"{where}.obsTime")
        for key, value in zip(elements, values):
            _element_value(key, value, f"{where}.{key}")
        # The span is what makes one station one range request: in row
        # order, inside the file the index measured, and the object alone,
        # so the slice parses on its own.
        _integer(offset, f"{where}.offset", (0, None))
        _integer(length, f"{where}.length", (1, None))
        if offset < end:
            raise AqProductError(f"{where}.offset must not precede the previous station's span")
        if offset + length > file_length:
            raise AqProductError(f"{where} spans past the end of {network_filename(network['id'])}")
        end = offset + length


def validate_station_line(payload: object, label: str) -> None:
    """One line of a network file: a station and its window of hourly
    means, column by column. ``time`` is strictly increasing epoch seconds,
    each the end of its averaging hour; every array under ``obs`` is as
    long as ``time``."""
    if not isinstance(payload, dict):
        raise AqProductError(f"{label} must be an object")
    station = payload.get("id")
    if not isinstance(station, str) or not STATION.match(station):
        raise AqProductError(f"{label}.id is not a <network>:<station> id")
    _optional_string(payload.get("name"), f"{label}.name")
    names = payload.get("names")
    if names is not None:
        if not isinstance(names, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in names.items()):
            raise AqProductError(f"{label}.names must map language tags to strings, or be null")
    _number(payload.get("lat"), f"{label}.lat", LATITUDE_RANGE)
    _number(payload.get("lon"), f"{label}.lon", LONGITUDE_RANGE)
    _optional_number(payload.get("elev"), f"{label}.elev", ELEVATION_RANGE)
    if payload.get("rank") not in RANKS or isinstance(payload.get("rank"), bool):
        raise AqProductError(f"{label}.rank must be one of {RANKS}")
    _optional_string(payload.get("agency"), f"{label}.agency")
    times = payload.get("time")
    if not isinstance(times, list) or not times:
        raise AqProductError(f"{label}.time must be a non-empty list")
    previous: int | None = None
    for position, value in enumerate(times):
        _integer(value, f"{label}.time[{position}]", (0, None))
        if previous is not None and value <= previous:
            raise AqProductError(f"{label}.time must be strictly increasing")
        previous = value
    observations = payload.get("obs")
    if not isinstance(observations, dict):
        raise AqProductError(f"{label}.obs must be an object of element arrays")
    for key, values in observations.items():
        if not isinstance(key, str) or not ELEMENT.match(key):
            raise AqProductError(f"{label}.obs has a malformed element key {key!r}")
        if not isinstance(values, list) or len(values) != len(times):
            raise AqProductError(f"{label}.obs.{key} must be as long as time")
        for position, value in enumerate(values):
            _element_value(key, value, f"{label}.obs.{key}[{position}]")


def build_pointer(issued: datetime, index_path: str, index_bytes: bytes) -> dict[str, Any]:
    payload = pointer_payload(PRODUCT, issued, index_path, index_bytes)
    validate_pointer(payload)
    return payload


def validate_pointer(payload: object) -> None:
    check_pointer(
        AqProductError,
        payload,
        PRODUCT,
        lambda issued: (round_directory(issued),),
        "aq pointer path does not name the issued round's directory",
    )


def read_index(path: Path) -> dict[str, Any]:
    return read_index_file(AqProductError, path, PRODUCT, validate_index)

"""The ``synop`` product, index schema v2: construction and validation of
the four kinds of file — the mutable pointer ``latest-synop.json``, a
round's ``index.json``, and beside it one ``<network>.stations.json`` and
one ``<network>.jsonl`` per network — written *and* read through the
validator, the posture ``airport/schema.py`` takes. ``docs/synop.md`` is the
normative description.

An index of schema v1 (one ``stations`` table inside the index) is still
read: it is what the live round is at the switch, and the next build merges
onto its network files, which v1 and v2 share.

Admission is structural. A network, a station or an element key the reader
has never seen is not an error; what is checked is shape — the row length
the ``elements`` list implies, the ranges of the elements §2 defines, the
ISO-8601 UTC times, the CRC32 pattern — and the rule the network files
add: each row's byte span lies, in order, inside its network's ``.jsonl``,
so a reader slices one station out by range and parses the slice on its
own.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import SynopProductError
from ..pointproduct import (  # noqa: F401 — re-exported: the synop modules import them from here
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

SCHEMA_VERSION = 2
"""The index's and the stations files' schema. The pointer has its own,
``pointproduct.POINTER_SCHEMA_VERSION``, which stays 1."""
READABLE_SCHEMA_VERSIONS = (1, 2)
PRODUCT = "synop"
POINTER_FILENAME = "latest-synop.json"
INDEX_FILENAME = "index.json"
NETWORK_FILE_SUFFIX = ".jsonl"
STATIONS_FILE_SUFFIX = ".stations.json"

ROUND_MINUTES = 10
"""The publishing cadence: a round is a UTC minute that is a multiple of
this, so a schedule that drifts by a minute still names one round."""

HISTORY_HOURS = 24
"""How much of each station's past its network file carries."""

ELEMENT_BOUNDS: dict[str, tuple[float | None, float | None]] = {
    "t": (-100.0, 70.0),
    "td": (-100.0, 60.0),
    "rh": (0, 100),
    "p": (300.0, 1100.0),
    "slp": (800.0, 1100.0),
    "wd": (0, 360),
    "ws": (0.0, 150.0),
    "gust": (0.0, 150.0),
    "pr": (0.0, 1000.0),
    "pr1h": (0.0, 1000.0),
    "sun": (0.0, 60.0),
    "snow": (0, 2000),
    "vis": (0, None),
}
"""The elements §2 defines, in the order the index lays them out, with the
range each stays inside. ``p`` reaches down to 300 hPa because a summit
station reports its own pressure (Mt. Fuji's is about 640). The adapters
write null for a value outside its range rather than lose the round."""

ELEMENTS = tuple(ELEMENT_BOUNDS)
INTEGER_ELEMENTS = frozenset({"rh", "wd", "snow", "vis"})

ROW_HEAD = ("id", "lat", "lon", "elev", "rank", "name", "obsTime")
ROW_TAIL = ("offset", "length")
"""A stations-file row is ``ROW_HEAD``, then the current value of each of
the ``elements`` in that order, then ``ROW_TAIL`` — the byte span of the
station's line in its network's ``.jsonl``, the JSON object alone."""

ROW_HEAD_V1 = ("id", "network", "lat", "lon", "elev", "rank", "name", "obsTime")
"""A schema v1 index row's head: v2's with the network's position in
``networks`` as the second column."""

RANKS = (0, 1, 2)
"""How prominent a station is, which a map thins by: 0 a principal station
(a staffed office, a summit, a remote island), 1 a multi-element automatic
station, 2 a station that measures precipitation alone."""

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
    """``202610050010`` → the round's UTC minute. A minute that is not a
    multiple of :data:`ROUND_MINUTES` names no round."""
    if not ROUND.match(value):
        raise SynopProductError("round must be a UTC minute, YYYYMMDDHHMM")
    try:
        moment = datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError as exc:
        raise SynopProductError(f"round is not a valid minute: {value}") from exc
    if moment.minute % ROUND_MINUTES:
        raise SynopProductError(f"round minute must be a multiple of {ROUND_MINUTES}: {value}")
    return moment


def floor_round(moment: datetime) -> datetime:
    """The round a moment falls in: the minute floored to the cadence."""
    moment = moment.astimezone(UTC)
    return moment.replace(minute=(moment.minute // ROUND_MINUTES) * ROUND_MINUTES, second=0, microsecond=0)


def round_name(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M")


def round_directory(moment: datetime) -> str:
    """``synop.202610050010``: one immutable directory per round, flat
    under the data root like the airport's. A round carries the whole
    24-hour window and lives three hours, so there is no archive to file
    by day."""
    return f"synop.{round_name(moment)}"


def network_filename(network: str) -> str:
    return f"{network}{NETWORK_FILE_SUFFIX}"


def stations_filename(network: str) -> str:
    return f"{network}{STATIONS_FILE_SUFFIX}"


# --- primitives --------------------------------------------------------


Bounds = tuple[float | None, float | None]


def _time(value: object, label: str) -> datetime:
    return check_time(SynopProductError, value, label)


def _number(value: object, label: str, bounds: Bounds | None = None) -> None:
    minimum, maximum = bounds or (None, None)
    check_number(SynopProductError, value, label, minimum=minimum, maximum=maximum)


def _integer(value: object, label: str, bounds: Bounds | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SynopProductError(f"{label} must be an integer")
    minimum, maximum = bounds or (None, None)
    if minimum is not None and value < minimum:
        raise SynopProductError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise SynopProductError(f"{label} must be at most {maximum}")


def _optional_number(value: object, label: str, bounds: Bounds | None = None) -> None:
    if value is not None:
        _number(value, label, bounds)


def _optional_string(value: object, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise SynopProductError(f"{label} must be a string or null")


def _element_value(key: str, value: object, label: str) -> None:
    """One observed value: null, or a number inside the element's range
    (an integer where §2 says so). An element §2 does not define is only
    required to be a number."""
    if value is None:
        return
    if key in INTEGER_ELEMENTS:
        _integer(value, label, ELEMENT_BOUNDS[key])
    else:
        _number(value, label, ELEMENT_BOUNDS.get(key))


def _crc_file(value: object, label: str, path: str) -> int:
    if not isinstance(value, dict):
        raise SynopProductError(f"{label} must be an object")
    if value.get("path") != path:
        raise SynopProductError(f"{label}.path must be {path!r}, the file beside the index")
    byte_length = value.get("byteLength")
    if isinstance(byte_length, bool) or not isinstance(byte_length, int) or byte_length < 0:
        raise SynopProductError(f"{label}.byteLength must be a non-negative integer")
    if not isinstance(value.get("crc32"), str) or not CRC32.match(value["crc32"]):
        raise SynopProductError(f"{label}.crc32 must be 8 lowercase hex characters")
    return byte_length


def validate_sources(sources: object, label: str) -> None:
    def extra(status: dict[str, Any], where: str) -> None:
        if "fetched" in status:
            _time(status["fetched"], f"{where}.fetched")
        for key in ("reports", "snapshots", "missing", "files", "unplaced"):
            if key in status:
                _integer(status[key], f"{where}.{key}", (0, None))

    check_sources(SynopProductError, sources, label, SOURCE_KEY, extra)


# --- the files ---------------------------------------------------------


def validate_network(payload: object, label: str, version: int = SCHEMA_VERSION) -> tuple[int, dict[str, Any] | None]:
    """One entry of the index's ``networks``: returns its ``.jsonl``'s
    length and, in a v2 index, its ``stations`` block."""
    if not isinstance(payload, dict):
        raise SynopProductError(f"{label} must be an object")
    network = payload.get("id")
    if not isinstance(network, str) or not NETWORK.match(network):
        raise SynopProductError(f"{label}.id is not a network id")
    for key in ("name", "attribution", "license", "url"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            raise SynopProductError(f"{label}.{key} must be a non-empty string")
    _integer(payload.get("cadence"), f"{label}.cadence", CADENCE_RANGE)
    if payload.get("prPeriod") is not None:
        _integer(payload["prPeriod"], f"{label}.prPeriod", CADENCE_RANGE)
    if payload.get("latest") is not None:
        _time(payload["latest"], f"{label}.latest")
    length = _crc_file(payload.get("file"), f"{label}.file", network_filename(network))
    if version < 2:
        return length, None
    stations = payload.get("stations")
    _crc_file(stations, f"{label}.stations", stations_filename(network))
    assert isinstance(stations, dict)
    _integer(stations.get("count"), f"{label}.stations.count", (0, None))
    return length, stations


def validate_station_line(payload: object, label: str) -> None:
    """One line of a network file: a station and its window of
    observations, column by column. ``time`` is strictly increasing epoch
    seconds; every array under ``obs`` is as long as ``time``."""
    if not isinstance(payload, dict):
        raise SynopProductError(f"{label} must be an object")
    station = payload.get("id")
    if not isinstance(station, str) or not STATION.match(station):
        raise SynopProductError(f"{label}.id is not a <network>:<station> id")
    _optional_string(payload.get("name"), f"{label}.name")
    names = payload.get("names")
    if names is not None:
        if not isinstance(names, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in names.items()):
            raise SynopProductError(f"{label}.names must map language tags to strings, or be null")
    _number(payload.get("lat"), f"{label}.lat", LATITUDE_RANGE)
    _number(payload.get("lon"), f"{label}.lon", LONGITUDE_RANGE)
    _optional_number(payload.get("elev"), f"{label}.elev", ELEVATION_RANGE)
    _optional_string(payload.get("wmo"), f"{label}.wmo")
    if payload.get("rank") not in RANKS or isinstance(payload.get("rank"), bool):
        raise SynopProductError(f"{label}.rank must be one of {RANKS}")
    times = payload.get("time")
    if not isinstance(times, list) or not times:
        raise SynopProductError(f"{label}.time must be a non-empty list")
    previous: int | None = None
    for position, value in enumerate(times):
        _integer(value, f"{label}.time[{position}]", (0, None))
        if previous is not None and value <= previous:
            raise SynopProductError(f"{label}.time must be strictly increasing")
        previous = value
    observations = payload.get("obs")
    if not isinstance(observations, dict):
        raise SynopProductError(f"{label}.obs must be an object of element arrays")
    for key, values in observations.items():
        if not isinstance(key, str) or not ELEMENT.match(key):
            raise SynopProductError(f"{label}.obs has a malformed element key {key!r}")
        if not isinstance(values, list) or len(values) != len(times):
            raise SynopProductError(f"{label}.obs.{key} must be as long as time")
        for position, value in enumerate(values):
            _element_value(key, value, f"{label}.obs.{key}[{position}]")


def _validate_elements(elements: object, label: str) -> list[str]:
    if (
        not isinstance(elements, list)
        or not all(isinstance(key, str) and ELEMENT.match(key) for key in elements)
        or len(set(elements)) != len(elements)
    ):
        raise SynopProductError(f"{label} must be a list of unique element keys")
    return elements


def _validate_rows(
    rows: object,
    label: str,
    *,
    head: tuple[str, ...],
    elements: list[str],
    networks: list[str],
    lengths: list[int],
) -> None:
    """Station rows — a v1 index's (``head`` with a ``network`` column,
    ``networks`` every network of the index) or a v2 stations file's
    (``networks`` the file's one network): sorted by id and unique, each id
    prefixed by its network, each span in row order within its network and
    inside that network's ``.jsonl``."""
    if not isinstance(rows, list):
        raise SynopProductError(f"{label} must be a list")
    width = len(head) + len(elements) + len(ROW_TAIL)
    has_network = "network" in head
    previous: str | None = None
    ends = [0] * len(networks)
    for position, row in enumerate(rows):
        where = f"{label}[{position}]"
        if not isinstance(row, list) or len(row) != width:
            raise SynopProductError(
                f"{where} must be a row of {width} values ({', '.join(head)}, the elements, {', '.join(ROW_TAIL)})"
            )
        fields = dict(zip(head, row[: len(head)]))
        values = row[len(head) : -len(ROW_TAIL)]
        offset, length = row[-len(ROW_TAIL) :]
        station = fields["id"]
        match = STATION.match(station) if isinstance(station, str) else None
        if match is None:
            raise SynopProductError(f"{where}.id is not a <network>:<station> id")
        if previous is not None and station <= previous:
            raise SynopProductError(f"{label} must be sorted by id and unique")
        previous = station
        network = 0
        if has_network:
            network = fields["network"]
            _integer(network, f"{where}.network", (0, len(networks) - 1))
        if match.group(1) != networks[network]:
            raise SynopProductError(f"{where}.id does not belong to network {networks[network]}")
        _number(fields["lat"], f"{where}.lat", LATITUDE_RANGE)
        _number(fields["lon"], f"{where}.lon", LONGITUDE_RANGE)
        _optional_number(fields["elev"], f"{where}.elev", ELEVATION_RANGE)
        if fields["rank"] not in RANKS or isinstance(fields["rank"], bool):
            raise SynopProductError(f"{where}.rank must be one of {RANKS}")
        _optional_string(fields["name"], f"{where}.name")
        _time(fields["obsTime"], f"{where}.obsTime")
        for key, value in zip(elements, values):
            _element_value(key, value, f"{where}.{key}")
        # The span is what makes one station one range request: in row
        # order within its network, inside the file the index measured,
        # and the object alone, so the slice parses on its own.
        _integer(offset, f"{where}.offset", (0, None))
        _integer(length, f"{where}.length", (1, None))
        if offset < ends[network]:
            raise SynopProductError(f"{where}.offset must not precede the previous station's span in its network file")
        if offset + length > lengths[network]:
            raise SynopProductError(f"{where} spans past the end of {network_filename(networks[network])}")
        ends[network] = offset + length


def _round_time(value: object, label: str) -> datetime:
    issued = _time(value, label)
    if issued.second or issued.microsecond or issued.minute % ROUND_MINUTES:
        raise SynopProductError(f"{label} must be a round: a UTC minute divisible by {ROUND_MINUTES}")
    return issued


def validate_index(payload: object) -> None:
    """A round's index, schema v2 or v1. A v2 index is checked on its own;
    its stations files by :func:`validate_stations_file`."""
    if not isinstance(payload, dict):
        raise SynopProductError("index must be an object")
    version = payload.get("schemaVersion")
    if isinstance(version, bool) or version not in READABLE_SCHEMA_VERSIONS:
        raise SynopProductError(f"index schemaVersion must be one of {READABLE_SCHEMA_VERSIONS}")
    _round_time(payload.get("issued"), "index.issued")
    _time(payload.get("generated"), "index.generated")

    networks = payload.get("networks")
    if not isinstance(networks, list):
        raise SynopProductError("index.networks must be a list")
    lengths: list[int] = []
    seen: set[str] = set()
    for position, network in enumerate(networks):
        lengths.append(validate_network(network, f"index.networks[{position}]", version)[0])
        if network["id"] in seen:
            raise SynopProductError(f"index.networks lists {network['id']} twice")
        seen.add(network["id"])
    elements = _validate_elements(payload.get("elements"), "index.elements")

    if version == 1:
        _validate_rows(
            payload.get("stations"),
            "index.stations",
            head=ROW_HEAD_V1,
            elements=elements,
            networks=[network["id"] for network in networks],
            lengths=lengths,
        )
    elif "stations" in payload:
        raise SynopProductError("a v2 index carries no stations: each network's are in its stations file")
    validate_sources(payload.get("sources"), "index.sources")


def validate_stations_file(payload: object, index: dict[str, Any], network_id: str) -> None:
    """A ``<network>.stations.json`` against the v2 index that names it:
    the same round and elements, as many rows as the index counts, every
    span inside the network's ``.jsonl``."""
    label = stations_filename(network_id)
    if not isinstance(payload, dict):
        raise SynopProductError(f"{label} must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION or isinstance(payload.get("schemaVersion"), bool):
        raise SynopProductError(f"{label} schemaVersion must be {SCHEMA_VERSION}")
    if payload.get("network") != network_id:
        raise SynopProductError(f"{label}.network must be {network_id!r}")
    issued = _round_time(payload.get("issued"), f"{label}.issued")
    if iso_z(issued) != index.get("issued"):
        raise SynopProductError(f"{label}.issued is not the index's round")
    elements = _validate_elements(payload.get("elements"), f"{label}.elements")
    if elements != index.get("elements"):
        raise SynopProductError(f"{label}.elements differs from the index's")
    described = next((network for network in index.get("networks", []) if network.get("id") == network_id), None)
    if described is None or not isinstance(described.get("stations"), dict):
        raise SynopProductError(f"the index names no stations file for {network_id!r}")
    rows = payload.get("stations")
    _validate_rows(
        rows,
        f"{label}.stations",
        head=ROW_HEAD,
        elements=elements,
        networks=[network_id],
        lengths=[described["file"]["byteLength"]],
    )
    assert isinstance(rows, list)
    if len(rows) != described["stations"]["count"]:
        raise SynopProductError(f"{label} holds {len(rows)} stations; the index counts {described['stations']['count']}")


def build_pointer(issued: datetime, index_path: str, index_bytes: bytes) -> dict[str, Any]:
    payload = pointer_payload(PRODUCT, issued, index_path, index_bytes)
    validate_pointer(payload)
    return payload


def validate_pointer(payload: object) -> None:
    check_pointer(
        SynopProductError,
        payload,
        PRODUCT,
        lambda issued: (round_directory(issued),),
        "synop pointer path does not name the issued round's directory",
    )


def read_index(path: Path) -> dict[str, Any]:
    """A round's index, v2 or v1 (the live round at the switch)."""
    return read_index_file(SynopProductError, path, PRODUCT, validate_index)


def read_stations_file(directory: Path, index: dict[str, Any], network_id: str) -> dict[str, Any]:
    """One network's stations file of a v2 round in ``directory``, checked
    against its index: first the bytes the index describes, then the
    shape."""
    described = next((network for network in index.get("networks", []) if network.get("id") == network_id), None)
    if described is None or not isinstance(described.get("stations"), dict):
        raise SynopProductError(f"the index names no stations file for {network_id!r}")
    path = directory / described["stations"]["path"]
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise SynopProductError(f"cannot read {path}: {exc}") from exc
    if len(body) != described["stations"]["byteLength"] or crc32_hex(body) != described["stations"]["crc32"]:
        raise SynopProductError(f"{path} is not the file the index describes")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise SynopProductError(f"{path} is not JSON: {exc}") from exc
    validate_stations_file(payload, index, network_id)
    return payload

"""The ``alerts`` product, schema v1: construction and validation of the
three kinds of file — the mutable pointer ``latest-alerts.json``, a round's
``index.json`` and the ``<network>.jsonl`` files beside it — written *and*
read through the validator, the posture the other point products take.
``docs/alerts.md`` is the normative description.

Admission is structural. A network, an event or a native code the reader
has never seen is not an error. The shapes are
``docs/schemas/alerts-{index,line}.schema.json``; checked here is what a
schema cannot say — names, uniqueness, order, the boxes' and the
geometry's four decimals and closed rings, the ``ended[]`` rules — and the
rule the network files add: each row's byte span lies, in order, inside
the file of the network the row belongs to, so a reader slices one alert
out by range and parses the slice on its own.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..errors import XueError
from ..pointproduct import (  # noqa: F401 — re-exported: the alerts modules import them from here
    CRC32,
    RoundProduct,
    archive_round_directory,
    builder_block,
    check_sources,
    check_time,
    crc32_hex,
    encode_json,
    existing_round_directory,
    flat_round_directory,
    iso_z,
    read_index_file,
    round_directories,
    sha256_hex,
    shaped,
    write_bytes_atomic,
)


class AlertsProductError(XueError):
    """An alerts file, round or boundary set that is not what
    ``docs/alerts.md`` describes."""


SCHEMA_VERSION = 1
PRODUCT = "alerts"
POINTER_FILENAME = "latest-alerts.json"
INDEX_FILENAME = "index.json"
NETWORK_FILE_SUFFIX = ".jsonl"
AREAS_DIRECTORY = "alerts-areas"

ROUND_MINUTES = 5
"""The publishing cadence: a round is a UTC minute that is a multiple of
this, so a schedule that drifts by a minute still names one round."""

CATEGORIES = (
    "rain",
    "flood",
    "landslide",
    "wind",
    "thunderstorm",
    "snow-ice",
    "heat",
    "cold",
    "fog",
    "dust",
    "fire",
    "air",
    "coastal",
    "marine",
    "tsunami",
    "other",
)
"""The closed list every alert is filed under, in the order the index
lists it. A network's own event names map onto it through
``tables.json``; what maps nowhere is ``other``."""

LEVELS = (1, 2, 3, 4, 5)
"""1 statement or information, 2 advisory / yellow / 注意報, 3 warning /
orange / 警報, 4 danger / red / 危険警報, 5 JMA 特別警報 alone."""

ENDED_REASONS = ("expired", "replaced", "cancelled", "gone")
"""Why an alert left the snapshot, tested in this order: an ``ended[]``
entry is ``[id, network, reason]``, or ``[id, network, "replaced", by]``
naming the alert in force that replaced it."""

ROW_FIELDS = ("id", "network", "category", "level", "event", "sent", "onset", "expires", "west", "south", "east", "north", "offset", "length")
"""An index row, in order; the last two are the byte span of the alert's
line in its network's file, the JSON object alone."""

COORDINATE_DECIMALS = 4

ROUND = re.compile(r"^\d{12}$")
NETWORK = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
ALERT = re.compile(r"^([a-z][a-z0-9]*(?:-[a-z0-9]+)*):(\S+)$")
SOURCE_KEY = re.compile(r"^[a-z][a-z0-9-]*$")
LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


ROUNDS = RoundProduct(PRODUCT, AlertsProductError, cadence_minutes=ROUND_MINUTES)
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


def areas_filename(set_id: str, version: str) -> str:
    return f"{set_id}.{version}.pmtiles"


def areas_bbox_filename(set_id: str, version: str) -> str:
    return f"{set_id}.{version}.bbox.json"


ROUND_DEPTH = 5
"""How many directories deep this build writes a round under the data root
(``alerts/YYYY/MM/DD/alerts.<round>``): how far a path in its index climbs
to reach the root."""

ROUND_DEPTHS = (5, 1)
"""The depths a published round may sit at: ``alerts/YYYY/MM/DD/alerts.<round>``
or the flat ``alerts.<round>``."""


def areas_path(set_id: str, version: str, depth: int = ROUND_DEPTH) -> str:
    """The boundary set's path relative to a round's index: the sets sit
    in one directory under the data root and outlive the rounds."""
    return f"{'../' * depth}{AREAS_DIRECTORY}/{areas_filename(set_id, version)}"


def areas_bbox_path(set_id: str, version: str, depth: int = ROUND_DEPTH) -> str:
    return f"{'../' * depth}{AREAS_DIRECTORY}/{areas_bbox_filename(set_id, version)}"


# --- the files ---------------------------------------------------------
#
# The shapes are docs/schemas/alerts-{index,line}.schema.json; the tests
# hold every golden to them. What follows is what a schema cannot say, and
# a file without the schema's shape is refused through `shaped`.


def _unique(values: list[Any], label: str) -> None:
    if len(set(values)) != len(values):
        raise AlertsProductError(f"{label} lists a value twice")


def _coordinate(value: float, label: str) -> float:
    if round(value, COORDINATE_DECIMALS) != value:
        raise AlertsProductError(f"{label} must have at most {COORDINATE_DECIMALS} decimals")
    return float(value)


def validate_areas(payload: dict[str, Any] | None, label: str) -> None:
    """A network's boundary set: its files' paths climb from the round's
    directory (either layout) to ``alerts-areas/``, and its layers are
    unique."""
    if payload is None:
        return
    set_id, version = payload["set"], payload["version"]
    depth = next((depth for depth in ROUND_DEPTHS if payload["path"] == areas_path(set_id, version, depth)), None)
    if depth is None:
        raise AlertsProductError(f"{label}.path must be {areas_path(set_id, version)!r} from the round's directory")
    _unique(payload["layers"], f"{label}.layers")
    bbox = payload.get("bbox")
    if bbox is not None and bbox["path"] != areas_bbox_path(set_id, version, depth):
        raise AlertsProductError(f"{label}.bbox.path must be {areas_bbox_path(set_id, version, depth)!r}")


def validate_geometry(payload: dict[str, Any] | None, label: str) -> tuple[float, float, float, float] | None:
    """Null, or a MultiPolygon of closed rings with coordinates to four
    decimals; returns its bounding box."""
    if payload is None:
        return None
    west = south = 180.0
    east = north = -180.0
    for p, polygon in enumerate(payload["coordinates"]):
        for r, ring in enumerate(polygon):
            where = f"{label}.coordinates[{p}][{r}]"
            for lon_value, lat_value in ring:
                lon = _coordinate(lon_value, f"{where} longitude")
                lat = _coordinate(lat_value, f"{where} latitude")
                west, east = min(west, lon), max(east, lon)
                south, north = min(south, lat), max(north, lat)
            if ring[0] != ring[-1]:
                raise AlertsProductError(f"{where} is not closed")
    return west, south, east, north


def validate_alert_line(payload: dict[str, Any], label: str) -> tuple[float, float, float, float] | None:
    """One line of a network file: an alert in force. Returns the bounding
    box of its geometry, or None."""
    with shaped(AlertsProductError, label):
        return _alert_line(payload, label)


def _alert_line(payload: dict[str, Any], label: str) -> tuple[float, float, float, float] | None:
    network = ALERT.match(payload["id"]).group(1)
    if payload["network"] != network:
        raise AlertsProductError(f"{label}.network must be the id's network, {network!r}")
    # The schema admits a category a later mapping adds; this writer files
    # under §2's closed list only.
    if payload["category"] not in CATEGORIES:
        raise AlertsProductError(f"{label}.category must be one of {', '.join(CATEGORIES)}")
    for language in payload["texts"]:
        if not LANGUAGE.match(language):
            raise AlertsProductError(f"{label}.texts has a malformed language tag {language!r}")
    _unique(payload["areas"], f"{label}.areas")
    replaces = payload.get("replaces")
    if replaces is not None:
        _unique(replaces, f"{label}.replaces")
        if payload["id"] in replaces or any(ALERT.match(old).group(1) != network for old in replaces):
            raise AlertsProductError(f"{label}.replaces must name other alerts of the same network")
    return validate_geometry(payload.get("geometry"), f"{label}.geometry")


def validate_index(payload: dict[str, Any]) -> None:
    with shaped(AlertsProductError, "index"):
        _index(payload)


def _owner(alert: str, network: int, networks: list[dict[str, Any]], label: str) -> None:
    """The row's network is a position in ``networks``, and the id's."""
    if network >= len(networks):
        raise AlertsProductError(f"{label}.network names no network")
    if ALERT.match(alert).group(1) != networks[network]["id"]:
        raise AlertsProductError(f"{label}.id does not belong to network {networks[network]['id']}")


def _index(payload: dict[str, Any]) -> None:
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise AlertsProductError(f"index schemaVersion must be {SCHEMA_VERSION}")
    networks = payload["networks"]
    _unique([network["id"] for network in networks], "index.networks")
    for position, network in enumerate(networks):
        if network["file"]["path"] != network_filename(network["id"]):
            raise AlertsProductError(f"index.networks[{position}].file.path must be {network_filename(network['id'])!r}")
        validate_areas(network.get("areas"), f"index.networks[{position}].areas")
    categories = payload["categories"]
    _unique(categories, "index.categories")
    if "columns" in payload and payload["columns"] != list(ROW_FIELDS):
        raise AlertsProductError(f"index.columns must be {', '.join(ROW_FIELDS)}")

    previous: str | None = None
    ends = [0] * len(networks)
    current: set[str] = set()
    for position, row in enumerate(payload["alerts"]):
        label = f"index.alerts[{position}]"
        if len(row) != len(ROW_FIELDS):
            raise AlertsProductError(f"{label} must be a row of {len(ROW_FIELDS)} values ({', '.join(ROW_FIELDS)})")
        alert, network, category, _, _, _, _, _, west, south, east, north, offset, length = row
        if previous is not None and alert <= previous:
            raise AlertsProductError("index.alerts must be sorted by id and unique")
        previous = alert
        current.add(alert)
        _owner(alert, network, networks, label)
        if category not in categories:
            raise AlertsProductError(f"{label}.category is not in index.categories")
        box = (west, south, east, north)
        if any(value is not None for value in box):
            w, s, e, n = (_coordinate(value, f"{label}.{key}") for value, key in zip(box, ROW_FIELDS[8:12], strict=True))
            if w > e or s > n:
                raise AlertsProductError(f"{label} bounding box is inverted")
        # The span is what makes one alert one range request: in row order
        # within its network, inside the file the index measured, and the
        # object alone, so the slice parses on its own.
        if offset < ends[network]:
            raise AlertsProductError(f"{label}.offset must not precede the previous alert's span in its network file")
        if offset + length > networks[network]["file"]["byteLength"]:
            raise AlertsProductError(f"{label} spans past the end of {network_filename(networks[network]['id'])}")
        ends[network] = offset + length

    previous = None
    for position, row in enumerate(payload["ended"]):
        label = f"index.ended[{position}]"
        alert, network, reason = row[:3]
        if (len(row) == 4) != (reason == "replaced"):
            raise AlertsProductError(f"{label}: a replaced alert, and only one, names the alert that replaced it")
        if len(row) == 4 and row[3] not in current:
            raise AlertsProductError(f"{label}.by must be an alert in force")
        if previous is not None and alert <= previous:
            raise AlertsProductError("index.ended must be sorted by id and unique")
        previous = alert
        if alert in current:
            raise AlertsProductError(f"{label} names an alert still in force")
        _owner(alert, network, networks, label)
    check_sources(AlertsProductError, payload["sources"], "index.sources", SOURCE_KEY, lambda status, where: None)


def read_index(path: Path) -> dict[str, Any]:
    return read_index_file(AlertsProductError, path, PRODUCT, validate_index)

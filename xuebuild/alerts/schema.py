"""The ``alerts`` product, schema v1: construction and validation of the
three kinds of file — the mutable pointer ``latest-alerts.json``, a round's
``index.json`` and the ``<network>.jsonl`` files beside it — written *and*
read through the validator, the posture the other point products take.
``docs/alerts.md`` is the normative description.

Admission is structural. A network, an event or a native code the reader
has never seen is not an error; what is checked is shape — the closed
category list and the five levels, the ISO-8601 UTC times, the CRC32
pattern, the geometry's coordinates — and the rule the network files add:
each row's byte span lies, in order, inside the file of the network the
row belongs to, so a reader slices one alert out by range and parses the
slice on its own.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import XueError
from ..pointproduct import (  # noqa: F401 — re-exported: the alerts modules import them from here
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

ENDED_REASONS = ("expired", "cancelled", "gone")

ROW_FIELDS = ("id", "network", "category", "level", "event", "sent", "onset", "expires", "west", "south", "east", "north", "offset", "length")
"""An index row, in order; the last two are the byte span of the alert's
line in its network's file, the JSON object alone."""

TEXT_FIELDS = ("headline", "description", "instruction", "area")

LATITUDE_RANGE = (-90.0, 90.0)
LONGITUDE_RANGE = (-180.0, 180.0)
COORDINATE_DECIMALS = 4

ROUND = re.compile(r"^\d{12}$")
NETWORK = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
ALERT = re.compile(r"^([a-z][a-z0-9]*(?:-[a-z0-9]+)*):(\S+)$")
SOURCE_KEY = re.compile(r"^[a-z][a-z0-9-]*$")
AREA_SET = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
AREA_VERSION = re.compile(r"^\d{8}$")
LAYER = re.compile(r"^[a-z][a-z0-9_]*$")
LANGUAGE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def parse_round(value: str) -> datetime:
    """``202611010005`` → the round's UTC minute. A minute that is not a
    multiple of :data:`ROUND_MINUTES` names no round."""
    if not ROUND.match(value):
        raise AlertsProductError("round must be a UTC minute, YYYYMMDDHHMM")
    try:
        moment = datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError as exc:
        raise AlertsProductError(f"round is not a valid minute: {value}") from exc
    if moment.minute % ROUND_MINUTES:
        raise AlertsProductError(f"round minute must be a multiple of {ROUND_MINUTES}: {value}")
    return moment


def floor_round(moment: datetime) -> datetime:
    moment = moment.astimezone(UTC)
    return moment.replace(minute=(moment.minute // ROUND_MINUTES) * ROUND_MINUTES, second=0, microsecond=0)


def round_name(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M")


def round_directory(moment: datetime) -> str:
    """``alerts.202611010005``: one immutable directory per round, flat
    under the data root. A round is a snapshot that lives three hours, so
    there is no archive to file by day."""
    return f"alerts.{round_name(moment)}"


def network_filename(network: str) -> str:
    return f"{network}{NETWORK_FILE_SUFFIX}"


def areas_filename(set_id: str, version: str) -> str:
    return f"{set_id}.{version}.pmtiles"


def areas_path(set_id: str, version: str) -> str:
    """The boundary set's path relative to a round's index: the sets sit
    in one directory beside the rounds and outlive them."""
    return f"../{AREAS_DIRECTORY}/{areas_filename(set_id, version)}"


# --- primitives --------------------------------------------------------


def _time(value: object, label: str) -> datetime:
    return check_time(AlertsProductError, value, label)


def _optional_time(value: object, label: str) -> None:
    if value is not None:
        _time(value, label)


def _integer(value: object, label: str, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AlertsProductError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise AlertsProductError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise AlertsProductError(f"{label} must be at most {maximum}")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise AlertsProductError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> None:
    if value is not None and not isinstance(value, str):
        raise AlertsProductError(f"{label} must be a string or null")


def _coordinate(value: object, label: str, bounds: tuple[float, float]) -> float:
    check_number(AlertsProductError, value, label, minimum=bounds[0], maximum=bounds[1])
    assert isinstance(value, (int, float))
    if round(value, COORDINATE_DECIMALS) != value:
        raise AlertsProductError(f"{label} must have at most {COORDINATE_DECIMALS} decimals")
    return float(value)


def _crc_file(value: object, label: str, path: str) -> int:
    if not isinstance(value, dict):
        raise AlertsProductError(f"{label} must be an object")
    if value.get("path") != path:
        raise AlertsProductError(f"{label}.path must be {path!r}, the file beside the index")
    byte_length = _integer(value.get("byteLength"), f"{label}.byteLength", 0)
    if not isinstance(value.get("crc32"), str) or not CRC32.match(value["crc32"]):
        raise AlertsProductError(f"{label}.crc32 must be 8 lowercase hex characters")
    return byte_length


def validate_areas(payload: object, label: str) -> None:
    """A network's boundary set: null, or the PMTiles file its ``areas``
    codes resolve against."""
    if payload is None:
        return
    if not isinstance(payload, dict):
        raise AlertsProductError(f"{label} must be an object or null")
    set_id = payload.get("set")
    version = payload.get("version")
    if not isinstance(set_id, str) or not AREA_SET.match(set_id):
        raise AlertsProductError(f"{label}.set is not a boundary set id")
    if not isinstance(version, str) or not AREA_VERSION.match(version):
        raise AlertsProductError(f"{label}.version must be YYYYMMDD")
    if payload.get("path") != areas_path(set_id, version):
        raise AlertsProductError(f"{label}.path must be {areas_path(set_id, version)!r}")
    _integer(payload.get("byteLength"), f"{label}.byteLength", 1)
    if not isinstance(payload.get("crc32"), str) or not CRC32.match(payload["crc32"]):
        raise AlertsProductError(f"{label}.crc32 must be 8 lowercase hex characters")
    layers = payload.get("layers")
    if (
        not isinstance(layers, list)
        or not layers
        or not all(isinstance(layer, str) and LAYER.match(layer) for layer in layers)
        or len(set(layers)) != len(layers)
    ):
        raise AlertsProductError(f"{label}.layers must be a non-empty list of unique layer names")


def validate_geometry(payload: object, label: str) -> tuple[float, float, float, float] | None:
    """Null, or a GeoJSON MultiPolygon of closed rings with coordinates to
    four decimals; returns its bounding box."""
    if payload is None:
        return None
    if not isinstance(payload, dict) or payload.get("type") != "MultiPolygon":
        raise AlertsProductError(f"{label} must be a MultiPolygon or null")
    polygons = payload.get("coordinates")
    if not isinstance(polygons, list) or not polygons:
        raise AlertsProductError(f"{label}.coordinates must be a non-empty list of polygons")
    west = south = 180.0
    east = north = -180.0
    for p, polygon in enumerate(polygons):
        if not isinstance(polygon, list) or not polygon:
            raise AlertsProductError(f"{label}.coordinates[{p}] must be a non-empty list of rings")
        for r, ring in enumerate(polygon):
            where = f"{label}.coordinates[{p}][{r}]"
            if not isinstance(ring, list) or len(ring) < 4:
                raise AlertsProductError(f"{where} must be a ring of at least four positions")
            for point in ring:
                if not isinstance(point, list) or len(point) != 2:
                    raise AlertsProductError(f"{where} has a position that is not [lon, lat]")
                lon = _coordinate(point[0], f"{where} longitude", LONGITUDE_RANGE)
                lat = _coordinate(point[1], f"{where} latitude", LATITUDE_RANGE)
                west, east = min(west, lon), max(east, lon)
                south, north = min(south, lat), max(north, lat)
            if ring[0] != ring[-1]:
                raise AlertsProductError(f"{where} is not closed")
    return west, south, east, north


def validate_sources(sources: object, label: str) -> None:
    def extra(status: dict[str, Any], where: str) -> None:
        if "fetched" in status:
            _time(status["fetched"], f"{where}.fetched")
        for key in ("reports", "alerts", "skipped", "pages"):
            if key in status:
                _integer(status[key], f"{where}.{key}", 0)

    check_sources(AlertsProductError, sources, label, SOURCE_KEY, extra)


# --- the files ---------------------------------------------------------


def validate_network(payload: object, label: str) -> int:
    """One entry of the index's ``networks``; returns its file's length."""
    if not isinstance(payload, dict):
        raise AlertsProductError(f"{label} must be an object")
    network = payload.get("id")
    if not isinstance(network, str) or not NETWORK.match(network):
        raise AlertsProductError(f"{label}.id is not a network id")
    for key in ("name", "attribution", "license", "url"):
        _string(payload.get(key), f"{label}.{key}")
    _optional_time(payload.get("latest"), f"{label}.latest")
    validate_areas(payload.get("areas"), f"{label}.areas")
    return _crc_file(payload.get("file"), f"{label}.file", network_filename(network))


def validate_alert_line(payload: object, label: str) -> tuple[float, float, float, float] | None:
    """One line of a network file: an alert in force. Returns the bounding
    box of its geometry, or None."""
    if not isinstance(payload, dict):
        raise AlertsProductError(f"{label} must be an object")
    alert = payload.get("id")
    match = ALERT.match(alert) if isinstance(alert, str) else None
    if match is None:
        raise AlertsProductError(f"{label}.id is not a <network>:<upstream id> id")
    if payload.get("network") != match.group(1):
        raise AlertsProductError(f"{label}.network must be the id's network, {match.group(1)!r}")
    if payload.get("category") not in CATEGORIES:
        raise AlertsProductError(f"{label}.category must be one of {', '.join(CATEGORIES)}")
    if payload.get("level") not in LEVELS or isinstance(payload.get("level"), bool):
        raise AlertsProductError(f"{label}.level must be one of {LEVELS}")
    native = payload.get("native")
    if not isinstance(native, dict):
        raise AlertsProductError(f"{label}.native must be an object")
    _string(native.get("event"), f"{label}.native.event")
    _optional_string(native.get("code"), f"{label}.native.code")
    _optional_string(native.get("level"), f"{label}.native.level")
    _time(payload.get("sent"), f"{label}.sent")
    _optional_time(payload.get("onset"), f"{label}.onset")
    _optional_time(payload.get("expires"), f"{label}.expires")
    if payload.get("status") != "actual":
        raise AlertsProductError(f"{label}.status must be 'actual'")
    texts = payload.get("texts")
    if not isinstance(texts, dict):
        raise AlertsProductError(f"{label}.texts must be an object of languages")
    for language, text in texts.items():
        if not isinstance(language, str) or not LANGUAGE.match(language):
            raise AlertsProductError(f"{label}.texts has a malformed language tag {language!r}")
        if not isinstance(text, dict) or not all(isinstance(value, str) for value in text.values()):
            raise AlertsProductError(f"{label}.texts.{language} must map text fields to strings")
    areas = payload.get("areas")
    if (
        not isinstance(areas, list)
        or not all(isinstance(code, str) and code for code in areas)
        or len(set(areas)) != len(areas)
    ):
        raise AlertsProductError(f"{label}.areas must be a list of unique area codes")
    _optional_string(payload.get("url"), f"{label}.url")
    _optional_string(payload.get("sender"), f"{label}.sender")
    return validate_geometry(payload.get("geometry"), f"{label}.geometry")


def validate_index(payload: object) -> None:
    if not isinstance(payload, dict):
        raise AlertsProductError("index must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise AlertsProductError(f"index schemaVersion must be {SCHEMA_VERSION}")
    issued = _time(payload.get("issued"), "index.issued")
    if issued.second or issued.microsecond or issued.minute % ROUND_MINUTES:
        raise AlertsProductError(f"index.issued must be a round: a UTC minute divisible by {ROUND_MINUTES}")
    _time(payload.get("generated"), "index.generated")

    networks = payload.get("networks")
    if not isinstance(networks, list):
        raise AlertsProductError("index.networks must be a list")
    lengths: list[int] = []
    seen: set[str] = set()
    for position, network in enumerate(networks):
        lengths.append(validate_network(network, f"index.networks[{position}]"))
        if network["id"] in seen:
            raise AlertsProductError(f"index.networks lists {network['id']} twice")
        seen.add(network["id"])

    categories = payload.get("categories")
    if (
        not isinstance(categories, list)
        or not all(isinstance(key, str) and key for key in categories)
        or len(set(categories)) != len(categories)
    ):
        raise AlertsProductError("index.categories must be a list of unique category keys")

    rows = payload.get("alerts")
    if not isinstance(rows, list):
        raise AlertsProductError("index.alerts must be a list")
    previous: str | None = None
    ends = [0] * len(networks)
    current: set[str] = set()
    for position, row in enumerate(rows):
        label = f"index.alerts[{position}]"
        if not isinstance(row, list) or len(row) != len(ROW_FIELDS):
            raise AlertsProductError(f"{label} must be a row of {len(ROW_FIELDS)} values ({', '.join(ROW_FIELDS)})")
        alert, network, category, level, event, sent, onset, expires, west, south, east, north, offset, length = row
        match = ALERT.match(alert) if isinstance(alert, str) else None
        if match is None:
            raise AlertsProductError(f"{label}.id is not a <network>:<upstream id> id")
        if previous is not None and alert <= previous:
            raise AlertsProductError("index.alerts must be sorted by id and unique")
        previous = alert
        current.add(alert)
        _integer(network, f"{label}.network", 0, len(networks) - 1)
        if match.group(1) != networks[network]["id"]:
            raise AlertsProductError(f"{label}.id does not belong to network {networks[network]['id']}")
        if category not in categories:
            raise AlertsProductError(f"{label}.category is not in index.categories")
        if level not in LEVELS or isinstance(level, bool):
            raise AlertsProductError(f"{label}.level must be one of {LEVELS}")
        _string(event, f"{label}.event")
        _time(sent, f"{label}.sent")
        _optional_time(onset, f"{label}.onset")
        _optional_time(expires, f"{label}.expires")
        box = (west, south, east, north)
        if any(value is not None for value in box):
            w = _coordinate(west, f"{label}.west", LONGITUDE_RANGE)
            s = _coordinate(south, f"{label}.south", LATITUDE_RANGE)
            e = _coordinate(east, f"{label}.east", LONGITUDE_RANGE)
            n = _coordinate(north, f"{label}.north", LATITUDE_RANGE)
            if w > e or s > n:
                raise AlertsProductError(f"{label} bounding box is inverted")
        # The span is what makes one alert one range request: in row order
        # within its network, inside the file the index measured, and the
        # object alone, so the slice parses on its own.
        _integer(offset, f"{label}.offset", 0)
        _integer(length, f"{label}.length", 1)
        if offset < ends[network]:
            raise AlertsProductError(f"{label}.offset must not precede the previous alert's span in its network file")
        if offset + length > lengths[network]:
            raise AlertsProductError(f"{label} spans past the end of {network_filename(networks[network]['id'])}")
        ends[network] = offset + length

    ended = payload.get("ended")
    if not isinstance(ended, list):
        raise AlertsProductError("index.ended must be a list")
    previous = None
    for position, row in enumerate(ended):
        label = f"index.ended[{position}]"
        if not isinstance(row, list) or len(row) != 3:
            raise AlertsProductError(f"{label} must be [id, network, reason]")
        alert, network, reason = row
        match = ALERT.match(alert) if isinstance(alert, str) else None
        if match is None:
            raise AlertsProductError(f"{label}.id is not a <network>:<upstream id> id")
        if previous is not None and alert <= previous:
            raise AlertsProductError("index.ended must be sorted by id and unique")
        previous = alert
        if alert in current:
            raise AlertsProductError(f"{label} names an alert still in force")
        _integer(network, f"{label}.network", 0, len(networks) - 1)
        if match.group(1) != networks[network]["id"]:
            raise AlertsProductError(f"{label}.id does not belong to network {networks[network]['id']}")
        if reason not in ENDED_REASONS:
            raise AlertsProductError(f"{label}.reason must be one of {', '.join(ENDED_REASONS)}")
    validate_sources(payload.get("sources"), "index.sources")


def build_pointer(issued: datetime, index_path: str, index_bytes: bytes) -> dict[str, Any]:
    payload = pointer_payload(PRODUCT, issued, index_path, index_bytes)
    validate_pointer(payload)
    return payload


def validate_pointer(payload: object) -> None:
    check_pointer(
        AlertsProductError,
        payload,
        PRODUCT,
        lambda issued: (round_directory(issued),),
        "alerts pointer path does not name the issued round's directory",
    )


def read_index(path: Path) -> dict[str, Any]:
    return read_index_file(AlertsProductError, path, PRODUCT, validate_index)

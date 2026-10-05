"""The ``nexrad`` product's JSON, schema v1 (``docs/nexrad.md``): the
pointer ``latest-nexrad.json`` and a round's ``index.json`` (the window manifest), written and
read through the validator.

Admission is structural: an unknown site or source id is data. What is
checked is shape — the site table's rows, each round's products and their
chunk spans (in store order, non-overlapping, inside the shard the round
measured), the sweep times (strictly increasing per site, inside the
window) and the CRC32 pattern.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import NexradProductError
from ..pointproduct import (  # noqa: F401 — re-exported for the nexrad modules
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
PRODUCT = "nexrad"
PRODUCTS = ("n0b", "n0g")
POINTER_FILENAME = "latest-nexrad.json"
WINDOW_FILENAME = "index.json"
"""The window manifest, in the place a point product's index takes."""
ROUND_MINUTES = 5
WINDOW_SECONDS = 3 * 3600

ROUND = re.compile(r"^\d{12}$")
SITE = re.compile(r"^[A-Z0-9]{3}$")
ICAO = re.compile(r"^[A-Z0-9]{4}$")
ROUND_PATH = re.compile(r"^\.\./nexrad\.\d{12}/$")
WINDOW_STORE_PATH = "./"
"""A round's ``path`` when its stores are the window stores beside the
manifest (a case, ``docs/nexrad.md`` §1)."""
SOURCE_KEY = re.compile(r"^[a-z][a-z0-9-]*$")


def parse_round(value: str) -> datetime:
    if not ROUND.match(value):
        raise NexradProductError("round must be a UTC minute, YYYYMMDDHHMM")
    try:
        moment = datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError as exc:
        raise NexradProductError(f"round is not a valid minute: {value}") from exc
    if moment.minute % ROUND_MINUTES:
        raise NexradProductError(f"round minute must be a multiple of {ROUND_MINUTES}: {value}")
    return moment


def floor_round(moment: datetime) -> datetime:
    moment = moment.astimezone(UTC)
    return moment.replace(minute=(moment.minute // ROUND_MINUTES) * ROUND_MINUTES, second=0, microsecond=0)


def round_name(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%d%H%M")


def round_directory(moment: datetime) -> str:
    return f"nexrad.{round_name(moment)}"


def _integer(value: object, label: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NexradProductError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise NexradProductError(f"{label} must be at least {minimum}")
    return value


def _file(value: object, label: str) -> int:
    if not isinstance(value, dict):
        raise NexradProductError(f"{label} must be an object")
    length = _integer(value.get("byteLength"), f"{label}.byteLength", 1)
    if not isinstance(value.get("crc32"), str) or not CRC32.match(value["crc32"]):
        raise NexradProductError(f"{label}.crc32 must be 8 lowercase hex characters")
    return length


def validate_window(payload: object) -> None:
    if not isinstance(payload, dict):
        raise NexradProductError("window must be an object")
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise NexradProductError(f"window schemaVersion must be {SCHEMA_VERSION}")
    issued = check_time(NexradProductError, payload.get("issued"), "window.issued")
    check_time(NexradProductError, payload.get("generated"), "window.generated")
    window = _integer(payload.get("windowSeconds"), "window.windowSeconds", 1)

    sites = payload.get("sites")
    if not isinstance(sites, list):
        raise NexradProductError("window.sites must be a list")
    seen: set[str] = set()
    for position, row in enumerate(sites):
        label = f"window.sites[{position}]"
        if not isinstance(row, list) or len(row) != 5:
            raise NexradProductError(f"{label} must be [id, icao, latitude, longitude, height]")
        site, icao, latitude, longitude, height = row
        if not isinstance(site, str) or not SITE.match(site) or site in seen:
            raise NexradProductError(f"{label} id must be a unique three-character site id")
        seen.add(site)
        if not isinstance(icao, str) or not ICAO.match(icao):
            raise NexradProductError(f"{label} icao must be four characters")
        check_number(NexradProductError, latitude, f"{label} latitude", minimum=-90, maximum=90)
        check_number(NexradProductError, longitude, f"{label} longitude", minimum=-180, maximum=180)
        check_number(NexradProductError, height, f"{label} height", minimum=-500, maximum=9000)

    rounds = payload.get("rounds")
    if not isinstance(rounds, list):
        raise NexradProductError("window.rounds must be a list")
    previous: datetime | None = None
    latest: dict[tuple[str, int], int] = {}
    # A window store is one object per product, so every round names the
    # same group, shard and depth.
    stores: dict[str, tuple[object, object, int]] = {}
    in_window_store: bool | None = None
    floor = issued.timestamp() - window
    for position, entry in enumerate(rounds):
        label = f"window.rounds[{position}]"
        if not isinstance(entry, dict):
            raise NexradProductError(f"{label} must be an object")
        moment = check_time(NexradProductError, entry.get("round"), f"{label}.round")
        if moment.second or moment.minute % ROUND_MINUTES:
            raise NexradProductError(f"{label}.round must be a round minute")
        if previous is not None and moment <= previous:
            raise NexradProductError("window.rounds must be oldest first and unique")
        if moment > issued or moment.timestamp() <= floor:
            raise NexradProductError(f"{label}.round is outside the window")
        previous = moment
        path = entry.get("path")
        window_store = path == WINDOW_STORE_PATH
        if not window_store and (not isinstance(path, str) or not ROUND_PATH.match(path) or not path.rstrip("/").endswith(round_name(moment))):
            raise NexradProductError(f"{label}.path must name the round's directory, or be {WINDOW_STORE_PATH!r}")
        if in_window_store is not None and window_store != in_window_store:
            raise NexradProductError("window.rounds are all in round stores or all in window stores")
        in_window_store = window_store
        products = [key for key in entry if key in PRODUCTS]
        if not products:
            raise NexradProductError(f"{label} holds no product")
        for product in products:
            where = f"{label}.{product}"
            block = entry[product]
            if not isinstance(block, dict):
                raise NexradProductError(f"{where} must be an object")
            _file(block.get("group"), f"{where}.group")
            shard_length = _file(block.get("shard"), f"{where}.shard")
            depth: int | None = None
            if window_store:
                depth = _integer(block.get("depth"), f"{where}.depth", 1)
                store = (block["group"], block["shard"], depth)
                if stores.setdefault(product, store) != store:
                    raise NexradProductError(f"{where} must name the same window store as every other round")
            elif "depth" in block:
                raise NexradProductError(f"{where}.depth belongs to a window store")
            chunks = block.get("chunks")
            scans = block.get("scans")
            if not isinstance(chunks, list) or not chunks or not isinstance(scans, list) or len(scans) != len(chunks):
                raise NexradProductError(f"{where} needs a chunk and a scan row per site")
            end = 0
            for (chunk, scan) in zip(chunks, scans):
                if not isinstance(chunk, list) or len(chunk) != 4:
                    raise NexradProductError(f"{where}.chunks rows are [site, offset, length, sweeps]")
                site = _integer(chunk[0], f"{where} site", 0)
                if site >= len(sites):
                    raise NexradProductError(f"{where} names site {site} past the site table")
                offset = _integer(chunk[1], f"{where} offset", 0)
                length = _integer(chunk[2], f"{where} length", 1)
                sweeps = _integer(chunk[3], f"{where} sweeps", 1)
                if depth is not None and sweeps > depth:
                    raise NexradProductError(f"{where} site {site} has more sweeps than the store's depth")
                if offset < end or offset + length > shard_length:
                    raise NexradProductError(f"{where} chunk spans must be in order inside the shard")
                end = offset + length
                if not isinstance(scan, list) or len(scan) != 2 or scan[0] != site:
                    raise NexradProductError(f"{where}.scans rows are [site, [times…]] in chunk order")
                times = scan[1]
                if not isinstance(times, list) or len(times) != sweeps:
                    raise NexradProductError(f"{where} site {site} has {sweeps} sweeps but {len(times) if isinstance(times, list) else '?'} times")
                for time in times:
                    _integer(time, f"{where} scan time", 0)
                    if time > moment.timestamp():
                        raise NexradProductError(f"{where} site {site} has a sweep after its round")
                    key = (product, site)
                    if key in latest and time <= latest[key]:
                        raise NexradProductError(f"{where} site {site}'s sweeps must strictly follow the window's")
                    latest[key] = time
    check_sources(NexradProductError, payload.get("sources"), "window.sources", SOURCE_KEY, lambda _s, _w: None)


def build_pointer(issued: datetime, path: str, window_bytes: bytes) -> dict[str, Any]:
    payload = pointer_payload(PRODUCT, issued, path, window_bytes)
    validate_pointer(payload)
    return payload


def validate_pointer(payload: object) -> None:
    check_pointer(
        NexradProductError,
        payload,
        PRODUCT,
        lambda issued: (round_directory(issued),),
        "nexrad pointer path does not name the issued round's directory",
    )


def read_window(path: Path) -> dict[str, Any]:
    return read_index_file(NexradProductError, path, PRODUCT, validate_window)

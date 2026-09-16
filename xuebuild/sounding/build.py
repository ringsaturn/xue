"""``sounding-build``: the fetched gateway directories of one issue hour →
one immutable product directory and the live pointer. The only module
where the sources meet.

Every bulletin in the issue's raw directories is decoded, the station
hours the two gateways and the corrections deliver more than once are
deduplicated, and each station's file is rewritten with its newest four
nominal times — the new soundings merged onto what the previous issue
published for it, since an hour's fetch carries only what arrived in that
hour.

A station nothing new arrived for is *copied forward*: its file from the
previous issue's directory is written again byte for byte, so its CRC32,
and with it its ``?v=``, does not change and the bucket's ``sync`` skips
it. That is why the publish workflow pulls the live issue's directory,
not just its index, before building.

A gateway that failed to list is recorded in ``sources`` and the other
one publishes. The pointer is withheld only when neither gateway
contributed *and* there was no previous index to carry forward, since a
product with no stations in it would take an empty map live.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import SoundingProductError, XueError
from . import bufr, derive
from .bufr import BUFR_MAGIC
from .fetch import SOURCE_IDS, FetchResult, SourceStatus, issue_raw_directory, read_fetch_record
from .schema import (
    INDEX_FILENAME,
    POINTER_FILENAME,
    SCHEMA_VERSION,
    build_pointer,
    crc32_hex,
    encode_json,
    iso_z,
    issue_directory,
    read_index,
    validate_index,
    validate_station,
    write_bytes_atomic,
)

LOG = logging.getLogger(__name__)

KEEP_TIMES = 4
"""Nominal times a station file carries: four is two days of the 00Z /
12Z pair, which is the window the panel offers."""

STALE_HOURS = 48
"""A station whose newest sounding is older than this is not carried
forward any further; it has stopped reporting, or its identifier has
changed."""


def _sounding_json(sounding: bufr.Sounding) -> dict[str, Any]:
    levels: dict[str, Any] = {
        "n": sounding.n,
        "p": list(sounding.p),
        "z": list(sounding.z),
        "t": list(sounding.t),
        "td": list(sounding.td),
        "wd": list(sounding.wd),
        "ws": list(sounding.ws),
        "sig": list(sounding.sig),
    }
    payload: dict[str, Any] = {
        "time": iso_z(sounding.time),
        "launched": None if sounding.launched is None else iso_z(sounding.launched),
        "sondeType": sounding.sonde,
        "bulletin": sounding.bulletin,
        "gateway": sounding.gateway,
        "arrived": iso_z(sounding.arrived),
        **levels,
    }
    payload["derived"] = derive.derive(levels)
    return payload


def _better(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
    """The dedup rule of ``bufr.deduplicate``, applied to the serialised
    form so a carried-forward sounding competes with a new one on the same
    terms: more levels wins, and on a tie the later arrival — which is the
    correction."""
    return (candidate["n"], candidate["arrived"]) > (current["n"], current["arrived"])


def merge_soundings(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One sounding per nominal time, newest first, at most ``KEEP_TIMES``."""
    best: dict[str, dict[str, Any]] = {}
    for entry in entries:
        current = best.get(entry["time"])
        if current is None or _better(entry, current):
            best[entry["time"]] = entry
    ordered = sorted(best.values(), key=lambda entry: entry["time"], reverse=True)
    return ordered[:KEEP_TIMES]


def _is_bufr(path: Path) -> bool:
    """Whether the object is a BUFR message at all, by its magic. The
    gateways republish the GTS stream verbatim, so the ``I/U/S/`` subtree
    also carries plain-text NIL bulletins — a few dozen bytes saying a
    station had nothing to send. They were 7% of the objects the two
    gateways held when this was written."""
    try:
        with path.open("rb") as handle:
            return BUFR_MAGIC in handle.read(256)
    except OSError:
        return False


def _parse_gateway(
    directory: Path, gateway: str, files: list[str]
) -> tuple[list[bufr.Sounding], dict[str, int], str | None]:
    """Decode every bulletin the gateway's fetch left, counting what went
    wrong rather than raising: one corrupt message loses one bulletin.
    The first failure is returned too, so a gateway whose bulletins *all*
    fail — no ``bufr_dump`` on the machine, a bucket serving HTML — is
    reported as a failed source instead of an empty one."""
    from ..eccodescli import bufr_dump_json  # eccodes is only needed on this path

    soundings: list[bufr.Sounding] = []
    counts = {"bulletins": 0, "nil": 0, "unreadable": 0, "subsets": 0, "dropped": 0}
    error: str | None = None
    for name in sorted(files):
        path = directory / name
        parsed_name = bufr.parse_file_name(name)
        if parsed_name is None or not path.is_file():
            counts["unreadable"] += 1
            error = error or f"{name} is not a bulletin this build can read"
            continue
        if not _is_bufr(path):
            # A NIL bulletin: the station had nothing to report and the
            # GTS carries a line of text saying so. Normal traffic, and
            # not worth a bufr_dump.
            counts["nil"] += 1
            continue
        try:
            result = bufr.parse_bulletin(bufr_dump_json(path), name=parsed_name, gateway=gateway)
        except (XueError, TypeError, ValueError, OSError) as exc:
            LOG.warning("sounding %s: %s does not decode: %s", gateway, name, exc)
            counts["unreadable"] += 1
            error = error or f"{name}: {exc}"
            continue
        counts["bulletins"] += 1
        counts["subsets"] += result.subsets
        counts["dropped"] += result.dropped
        soundings.extend(result.soundings)
    return soundings, counts, error


def _station_payload(
    station_id: str,
    wmo: str | None,
    position: dict[str, Any],
    soundings: list[dict[str, Any]],
    statuses: list[SourceStatus],
) -> dict[str, Any]:
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "id": station_id,
        "wmo": wmo,
        "name": None,
        "lat": position["lat"],
        "lon": position["lon"],
        "elev": position.get("elev"),
        "soundings": soundings,
        "sources": [status.to_json() for status in statuses],
    }
    validate_station(payload)
    return payload


def _entry(
    station_id: str,
    payload: dict[str, Any],
    encoded: bytes,
) -> dict[str, Any]:
    newest = payload["soundings"][0]
    return {
        "id": station_id,
        "wmo": payload["wmo"],
        "name": payload["name"],
        "lat": payload["lat"],
        "lon": payload["lon"],
        "elev": payload["elev"],
        "path": f"{station_id}.json",
        "byteLength": len(encoded),
        "crc32": crc32_hex(encoded),
        "latest": newest["time"],
        "times": [sounding["time"] for sounding in payload["soundings"]],
        "headline": derive.headline(newest, newest["derived"]),
    }


def previous_directory(previous_index: dict[str, Any] | None, output_root: Path) -> Path | None:
    """Where the previous issue's station files are, when the build can
    see them. ``make live-sounding-index`` pulls the whole directory, so
    in the publish workflow it can."""
    if not previous_index:
        return None
    try:
        issued = datetime.fromisoformat(previous_index["issued"])
    except (KeyError, TypeError, ValueError):
        return None
    directory = output_root / issue_directory(issued)
    return directory if directory.is_dir() else None


def build_product(
    issue: datetime,
    raw_root: Path,
    output_root: Path,
    *,
    sources: tuple[str, ...] = SOURCE_IDS,
    previous_index: dict[str, Any] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the issue's fetched directories, write ``sounding.<issue>/``
    under ``output_root`` and, when anything contributed, the pointer."""
    now = now or datetime.now(UTC)
    raw_directory = issue_raw_directory(raw_root, issue)
    directory = output_root / issue_directory(issue)
    if (directory / INDEX_FILENAME).exists() and not force:
        raise SoundingProductError(
            f"{directory / INDEX_FILENAME} exists; pass --force to rebuild the issue"
        )

    statuses: list[SourceStatus] = []
    soundings: list[bufr.Sounding] = []
    watermark: dict[str, Any] = dict((previous_index or {}).get("watermark", {}))
    for gateway in sources:
        if gateway not in SOURCE_IDS:
            raise XueError(f"unknown sounding source {gateway!r}; choose from {', '.join(SOURCE_IDS)}")
        result: FetchResult | None = read_fetch_record(raw_directory, gateway)
        if result is None:
            statuses.append(SourceStatus(gateway, False, error="not fetched"))
            continue
        status = result.status
        if status.ok:
            found, counts, error = _parse_gateway(raw_directory / gateway, gateway, result.files)
            if counts["bulletins"] == 0 and counts["unreadable"] > 0:
                LOG.warning("sounding %s: no bulletin decoded: %s", gateway, error)
                status = SourceStatus(
                    gateway,
                    False,
                    fetched=status.fetched,
                    url=status.url,
                    # The watermark is deliberately not advanced: the
                    # objects were downloaded and could not be read, so the
                    # next hour lists them again rather than stepping past
                    # them for good.
                    watermark=None,
                    error=f"parse: {error}",
                    detail=status.detail | counts,
                )
            else:
                soundings.extend(found)
                status.detail.update(counts)
                status.detail["soundings"] = len(found)
        if status.watermark is not None:
            watermark[gateway] = iso_z(status.watermark)
        elif gateway not in watermark:
            watermark[gateway] = None
        statuses.append(status)

    fresh: dict[str, list[bufr.Sounding]] = {}
    for sounding in bufr.deduplicate(soundings):
        fresh.setdefault(sounding.key, []).append(sounding)

    previous_root = previous_directory(previous_index, output_root)
    previous_entries = {
        entry["id"]: entry for entry in (previous_index or {}).get("stations", []) if isinstance(entry, dict)
    }
    stale_before = issue - timedelta(hours=STALE_HOURS)

    entries: list[dict[str, Any]] = []
    copied = 0
    carried: set[str] = set()
    for key in sorted(fresh):
        group = fresh[key]
        group.sort(key=lambda sounding: sounding.time, reverse=True)
        newest = group[0]
        station_id = _published_id(group)
        # A station is one station by its WMO number, but the id it is
        # published under changes the day its centre starts filing a native
        # WIGOS identifier. Its history is looked for under both forms, and
        # both count as carried, so the hour does not publish the station
        # twice — once rewritten and once copied forward under its old id.
        known = {station_id} | {sounding.id for sounding in group}
        carried |= known
        previous_entry = next((previous_entries[name] for name in sorted(known) if name in previous_entries), None)
        history = _previous_soundings(previous_root, previous_entry)
        merged = merge_soundings([_sounding_json(sounding) for sounding in group] + history)
        if datetime.fromisoformat(merged[0]["time"]) < stale_before:
            continue
        wmo = next((sounding.wmo for sounding in group if sounding.wmo is not None), None)
        position = {"lat": newest.lat, "lon": newest.lon, "elev": newest.elev}
        payload = _station_payload(station_id, wmo, position, merged, statuses)
        encoded = encode_json(payload)
        write_bytes_atomic(directory / f"{station_id}.json", encoded)
        entries.append(_entry(station_id, payload, encoded))

    for station_id, entry in previous_entries.items():
        if station_id in carried or previous_root is None:
            continue
        if datetime.fromisoformat(entry["latest"]) < stale_before:
            continue
        source = previous_root / entry["path"]
        try:
            encoded = source.read_bytes()
        except OSError:
            LOG.warning("sounding: %s has no file in the previous issue; it drops out", station_id)
            continue
        if crc32_hex(encoded) != entry.get("crc32"):
            LOG.warning("sounding: %s does not match the previous index's CRC32; it drops out", station_id)
            continue
        write_bytes_atomic(directory / entry["path"], encoded)
        entries.append(dict(entry))
        copied += 1

    entries.sort(key=lambda entry: entry["id"])
    index = {
        "schemaVersion": SCHEMA_VERSION,
        "issued": iso_z(issue),
        "generated": iso_z(now),
        "watermark": watermark,
        "stations": entries,
        "sources": [status.to_json() for status in statuses],
    }
    validate_index(index)
    index_bytes = encode_json(index)
    write_bytes_atomic(directory / INDEX_FILENAME, index_bytes)

    contributed = any(status.ok for status in statuses) or copied > 0
    pointer_path: Path | None = None
    if contributed and entries:
        pointer = build_pointer(issue, f"{issue_directory(issue)}/{INDEX_FILENAME}", index_bytes)
        pointer_path = output_root / POINTER_FILENAME
        write_bytes_atomic(pointer_path, encode_json(pointer))
    else:
        LOG.warning("sounding: no gateway contributed and nothing carried forward; the pointer is not written")
    return {
        "issue": issue.strftime("%Y%m%d%H"),
        "directory": str(directory),
        "stations": len(entries),
        "fresh": len(entries) - copied,
        "copied": copied,
        "soundings": sum(len(group) for group in fresh.values()),
        "watermark": watermark,
        "sources": index["sources"],
        "pointer": None if pointer_path is None else str(pointer_path),
    }


def _published_id(group: list[bufr.Sounding]) -> str:
    """The station's published id: a native WIGOS identifier where any of
    the hour's soundings carries one, else the legacy WIGOS form of the
    WMO number (``docs/sounding.md`` §3)."""
    for sounding in group:
        if sounding.wigos is not None:
            return sounding.wigos
    return group[0].id


def _previous_soundings(
    previous_root: Path | None, entry: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """The station file the previous issue published, as a list of
    soundings to merge the new ones onto. Missing (only the index was
    pulled, or this is a first build) means the station starts again from
    what arrived this hour — the older nominal times are lost until they
    are refetched, which is why the workflow pulls the directory."""
    if previous_root is None or entry is None:
        return []
    path = previous_root / entry["path"]
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return []
    soundings = payload.get("soundings")
    if not isinstance(soundings, list):
        return []
    return [item for item in soundings if isinstance(item, dict) and "time" in item and "n" in item]


def load_previous_index(path: Path | None, output_root: Path) -> dict[str, Any] | None:
    """The previous hour's index: the one given, else the one the local
    pointer names, else nothing (a first build, or a fresh checkout)."""
    if path is not None:
        return read_index(path)
    pointer = output_root / POINTER_FILENAME
    if not pointer.exists():
        return None
    try:
        named = json.loads(pointer.read_text(encoding="utf-8")).get("path")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(named, str):
        return None
    index_path = output_root / named
    if not index_path.exists():
        return None
    try:
        return read_index(index_path)
    except SoundingProductError as exc:
        LOG.warning("sounding: ignoring the previous index at %s: %s", index_path, exc)
        return None


def previous_watermarks(previous_index: dict[str, Any] | None) -> dict[str, datetime | None]:
    """The per-gateway watermark the fetch starts from."""
    watermarks: dict[str, datetime | None] = {}
    for gateway, value in (previous_index or {}).get("watermark", {}).items():
        if isinstance(value, str):
            try:
                watermarks[gateway] = datetime.fromisoformat(value)
            except ValueError:
                watermarks[gateway] = None
        else:
            watermarks[gateway] = None
    return watermarks

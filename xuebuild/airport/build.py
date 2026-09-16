"""``airport-build``: one round's fetched files and the previous round's
shards → one immutable round directory, the shards that changed, and the
live pointer. The only module where the three sources meet.

The product is a rolling 24 hours of observations, and the AWC's cache
holds ninety minutes of them, so each round is a merge rather than a
snapshot: the previous round's shards are the history, this round's METARs
are laid over it keyed by (station, observation time) with the new report
winning, and anything older than :data:`~.schema.HISTORY_HOURS` before the
round falls off the end. A station that did not report this round keeps
its history untouched; a station that reported but is not in the station
table gets an entry with a null name and the report's own position.

Writing is content-addressed. Every shard is built, encoded and measured;
one whose CRC32 equals what the previous index recorded is *not* written —
the index simply names the file that is already there. A round in which
forty stations reported therefore costs forty shard objects, not five
thousand stations' worth of bytes (``docs/airport.md`` §1).

The pointer is withheld only when both the METARs and the TAFs failed, in
which case the previous round stays live: one of the two is enough to
publish, and the history is in the shards either way.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import AirportProductError, XueError
from ..pointproduct import iso_z
from .fetch import SOURCE_IDS, SourceStatus, read_fetch_record
from .metar import MetarReport, parse_metars
from .schema import (
    HISTORY_HOURS,
    INDEX_FILENAME,
    POINTER_FILENAME,
    SCHEMA_VERSION,
    SHARD_DIRECTORY,
    build_pointer,
    crc32_hex,
    encode_json,
    read_index,
    round_directory,
    round_name,
    shard_filename,
    shard_key,
    shard_path,
    validate_index,
    validate_shard,
    write_bytes_atomic,
)
from .stations import Station, parse_stations
from .taf import TafReport, parse_tafs

LOG = logging.getLogger(__name__)


def _published(status: SourceStatus) -> dict[str, Any]:
    """A source's status as the index publishes it: where the bytes came
    from, not where this build happened to put them."""
    return {key: value for key, value in status.to_json().items() if key != "file"}


def _station_payload(entry: dict[str, Any], table: Station | None) -> dict[str, Any]:
    """One station as a shard writes it. The station table wins over what
    a report carries — it does not change from report to report — and what
    the table does not know falls back to the report, then to what the
    previous round held."""
    payload = {
        "name": entry.get("name"),
        "lat": entry["lat"],
        "lon": entry["lon"],
        "elev": entry.get("elev"),
        "iata": entry.get("iata"),
        "wmo": entry.get("wmo"),
    }
    if table is not None:
        payload["lat"] = table.lat
        payload["lon"] = table.lon
        for key, value in (("name", table.name), ("elev", table.elev), ("iata", table.iata), ("wmo", table.wmo)):
            if value is not None:
                payload[key] = value
    payload["metars"] = entry["metars"]
    payload["taf"] = entry.get("taf")
    return payload


def _read_previous_shards(output_root: Path, previous_index: dict[str, Any] | None) -> tuple[dict[str, dict[str, Any]], dict[str, str], list[str]]:
    """The shards the previous index names, by shard key, with the CRC32 it
    recorded for each and the keys whose file could not be read.

    A shard is read from the one place the contract puts it — the shard
    directory beside the round directories — under the name the index's
    CRC32 makes: the index's ``path`` says the same thing relative to its
    own round directory."""
    payloads: dict[str, dict[str, Any]] = {}
    crcs: dict[str, str] = {}
    missing: list[str] = []
    if not previous_index:
        return payloads, crcs, missing
    for shard, entry in sorted(previous_index.get("shards", {}).items()):
        crc32 = entry["crc32"]
        path = output_root / SHARD_DIRECTORY / shard_filename(shard, crc32)
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("airport: shard %s is not readable (%s); its history starts again", shard, exc)
            missing.append(shard)
            continue
        stations = payload.get("stations")
        if not isinstance(stations, dict):
            LOG.warning("airport: shard %s is malformed; its history starts again", shard)
            missing.append(shard)
            continue
        payloads[shard] = payload
        crcs[shard] = crc32
    return payloads, crcs, missing


def _entries_from_previous(shards: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for payload in shards.values():
        for icao, station in payload["stations"].items():
            entries[icao] = {
                "name": station.get("name"),
                "lat": station.get("lat"),
                "lon": station.get("lon"),
                "elev": station.get("elev"),
                "iata": station.get("iata"),
                "wmo": station.get("wmo"),
                "reports": {report["time"]: report for report in station.get("metars", [])},
                "taf": station.get("taf"),
            }
    return entries


def _add_report(entries: dict[str, dict[str, Any]], report: MetarReport) -> None:
    entry = entries.get(report.icao)
    if entry is None:
        entry = {"name": None, "lat": report.lat, "lon": report.lon, "elev": report.elev, "iata": None, "wmo": None, "reports": {}, "taf": None}
        entries[report.icao] = entry
    else:
        # The newest report's own position: a station that moved, or one
        # the table does not know, is placed by what it just reported.
        entry["lat"] = report.lat
        entry["lon"] = report.lon
        if report.elev is not None:
            entry["elev"] = report.elev
    payload = report.to_json()
    entry["reports"][payload["time"]] = payload  # this round's report wins


def build_product(
    moment: datetime,
    raw_root: Path,
    output_root: Path,
    *,
    previous_index: dict[str, Any] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the round's fetched files and the previous round's shards,
    write ``airport.<round>/index.json``, the shards that changed and,
    unless both observation sources failed, the pointer."""
    now = now or datetime.now(UTC)
    directory = output_root / round_directory(moment)
    if (directory / INDEX_FILENAME).exists() and not force:
        raise AirportProductError(f"{directory / INDEX_FILENAME} exists; pass --force to rebuild the round")

    record = read_fetch_record(raw_root, moment)
    statuses: list[SourceStatus] = []
    reports: list[MetarReport] = []
    forecasts: dict[str, TafReport] = {}
    table: dict[str, Station] = {}
    metars_ok = False
    tafs_ok = False
    for source_id in SOURCE_IDS:
        status = record.status(source_id) if record is not None else None
        if status is None:
            statuses.append(SourceStatus(source_id, False, error="not fetched"))
            continue
        if status.ok and status.file:
            path = raw_root / status.file
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                if source_id == "awc-metars":
                    reports = parse_metars(text)
                    status.detail["reports"] = len(reports)
                    metars_ok = True
                elif source_id == "awc-tafs":
                    parsed = parse_tafs(text)
                    forecasts = {forecast.icao: forecast for forecast in parsed}
                    status.detail["reports"] = len(parsed)
                    tafs_ok = True
                else:
                    table = parse_stations(text)
                    status.detail["reports"] = len(table)
            except (XueError, OSError, ValueError) as exc:
                LOG.warning("airport %s: parse failed: %s", source_id, exc)
                status = SourceStatus(source_id, False, fetched=status.fetched, url=status.url, error=f"parse: {exc}")
        statuses.append(status)

    previous_shards, previous_crcs, missing = _read_previous_shards(output_root, previous_index)
    entries = _entries_from_previous(previous_shards)
    for report in reports:
        _add_report(entries, report)

    horizon = moment - timedelta(hours=HISTORY_HOURS)
    stations: dict[str, dict[str, Any]] = {}
    for icao in sorted(entries):
        entry = entries[icao]
        kept = [report for time, report in entry["reports"].items() if datetime.fromisoformat(time) >= horizon]
        if not kept:
            continue  # nothing observed in the window: the station leaves
        kept.sort(key=lambda report: report["time"], reverse=True)
        entry["metars"] = kept
        if tafs_ok:
            # The TAF cache is the whole current set, so an absent station
            # has no current forecast; when the fetch failed the previous
            # round's forecast is kept rather than silently dropped.
            forecast = forecasts.get(icao)
            entry["taf"] = forecast.to_json() if forecast is not None else None
        stations[icao] = _station_payload(entry, table.get(icao))

    shard_stations: dict[str, dict[str, Any]] = {}
    for icao, station in stations.items():
        shard_stations.setdefault(shard_key(icao), {})[icao] = station

    shards: dict[str, dict[str, Any]] = {}
    written = 0
    unchanged = 0
    for shard in sorted(shard_stations):
        payload = {"schemaVersion": SCHEMA_VERSION, "shard": shard, "stations": shard_stations[shard]}
        validate_shard(payload)
        body = encode_json(payload)
        crc32 = crc32_hex(body)
        path = output_root / SHARD_DIRECTORY / shard_filename(shard, crc32)
        if previous_crcs.get(shard) == crc32:
            unchanged += 1
        else:
            written += 1
        if not path.exists():
            write_bytes_atomic(path, body)
        shards[shard] = {"path": shard_path(shard, crc32), "byteLength": len(body), "crc32": crc32}

    rows = []
    for icao, station in stations.items():
        newest = station["metars"][0]
        rows.append(
            [
                icao,
                station["lat"],
                station["lon"],
                station["elev"],
                newest["time"],
                newest["t"],
                newest["td"],
                newest["wd"],
                newest["ws"],
                newest["gust"],
                newest["vis"],
                newest["qnh"],
                newest["category"],
                1 if station["taf"] else 0,
            ]
        )

    index = {
        "schemaVersion": SCHEMA_VERSION,
        "issued": iso_z(moment),
        "generated": iso_z(now),
        "shards": shards,
        "stations": rows,
        "sources": [_published(status) for status in statuses],
    }
    validate_index(index)
    index_bytes = encode_json(index)
    write_bytes_atomic(directory / INDEX_FILENAME, index_bytes)

    pointer_path: Path | None = None
    if metars_ok or tafs_ok:
        pointer = build_pointer(moment, f"{round_directory(moment)}/{INDEX_FILENAME}", index_bytes)
        pointer_path = output_root / POINTER_FILENAME
        write_bytes_atomic(pointer_path, encode_json(pointer))
    else:
        LOG.warning("airport: neither the METARs nor the TAFs arrived; the pointer is not written")
    return {
        "round": round_name(moment),
        "directory": str(directory),
        "stations": len(rows),
        "reports": sum(len(station["metars"]) for station in stations.values()),
        "tafs": sum(1 for station in stations.values() if station["taf"]),
        "shards": {"total": len(shards), "written": written, "unchanged": unchanged, "missing": missing},
        "sources": index["sources"],
        "pointer": None if pointer_path is None else str(pointer_path),
    }


def load_previous_index(path: Path | None, output_root: Path) -> dict[str, Any] | None:
    """The previous round's index, whose shards are this round's history:
    the one given, else the one the local pointer names, else nothing (a
    first build, or a fresh checkout — the history then starts from the
    ninety minutes the AWC cache holds)."""
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
    except AirportProductError as exc:
        LOG.warning("airport: ignoring the previous index at %s: %s", index_path, exc)
        return None

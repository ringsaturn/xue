"""``airport-build``: one round's fetched files and the previous round's
history → one immutable round directory and the live pointer. The only
module where the three sources meet.

The product is a rolling 24 hours of observations, and the AWC's cache
holds ninety minutes of them, so each round is a merge rather than a
snapshot: the previous round's ``history.jsonl`` is the history, this
round's METARs are laid over it keyed by (station, observation time) with
the new report winning, and anything older than
:data:`~.schema.HISTORY_HOURS` before the round falls off the end. A
station that did not report this round keeps its history untouched; a
station that reported but is not in the station table gets an entry with a
null name and the report's own position.

A round writes two files. ``history.jsonl`` is one line per station,
sorted by ICAO — the same compact JSON object the reader validates, then a
newline — and ``index.json`` carries every station's newest observation
plus the byte span of its line. So a browser reads one airport with one
range request against a file it can address by CRC32, and an analyst reads
the whole day by streaming one object rather than listing a thousand.

The pointer is withheld only when both the METARs and the TAFs failed, in
which case the previous round stays live: one of the two is enough to
publish, and the history carries over either way.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import AirportProductError, XueError
from ..pointproduct import iso_z
from ..stac import write_point_product_documents
from .fetch import SOURCE_IDS, SourceStatus, read_fetch_record
from .metar import MetarReport, parse_metars
from .schema import (
    HISTORY_FILENAME,
    HISTORY_HOURS,
    INDEX_FILENAME,
    POINTER_FILENAME,
    PRODUCT,
    SCHEMA_VERSION,
    build_pointer,
    crc32_hex,
    encode_json,
    read_index,
    round_directory,
    round_name,
    validate_history_station,
    validate_index,
    write_bytes_atomic,
)
from .stations import Station, parse_stations
from .taf import TafReport, parse_tafs

LOG = logging.getLogger(__name__)


def _published(status: SourceStatus) -> dict[str, Any]:
    """A source's status as the index publishes it: where the bytes came
    from, not where this build happened to put them."""
    return {key: value for key, value in status.to_json().items() if key != "file"}


def _station_payload(icao: str, entry: dict[str, Any], table: Station | None) -> dict[str, Any]:
    """One station as a line of the history file. The station table wins over what
    a report carries — it does not change from report to report — and what
    the table does not know falls back to the report, then to what the
    previous round held."""
    payload = {
        "icao": icao,
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


def _read_previous_history(output_root: Path, previous_index: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The previous round's stations, by ICAO id, read line by line out of
    the ``history.jsonl`` its index names.

    A history that cannot be read is not fatal: the round publishes what
    the AWC cache holds and every station's window starts again, which is
    the state of a first build anyway."""
    entries: dict[str, dict[str, Any]] = {}
    if not previous_index:
        return entries
    issued = datetime.fromisoformat(previous_index["issued"])
    path = output_root / round_directory(issued) / previous_index["history"]["path"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        LOG.warning("airport: %s is not readable (%s); the history starts again", path, exc)
        return entries
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            station = json.loads(line)
            icao = station["icao"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            LOG.warning("airport: %s line %d is not a station (%s); skipping it", path, number, exc)
            continue
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
    """Read the round's fetched files and the previous round's history,
    write ``airport.<round>/`` — the history file and the index over it —
    and, unless both observation sources failed, the pointer."""
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

    entries = _read_previous_history(output_root, previous_index)
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
        stations[icao] = _station_payload(icao, entry, table.get(icao))

    # One line per station, and the index's row for it carries the span of
    # the object alone — not the newline — so a reader's range request
    # slices out something that parses as JSON on its own.
    lines: list[bytes] = []
    rows = []
    offset = 0
    for icao, station in stations.items():
        validate_history_station(station, f"history[{icao}]")
        line = encode_json(station)
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
                offset,
                len(line),
            ]
        )
        lines.append(line)
        offset += len(line) + 1  # the newline
    history = b"\n".join(lines) + (b"\n" if lines else b"")
    write_bytes_atomic(directory / HISTORY_FILENAME, history)

    index = {
        "schemaVersion": SCHEMA_VERSION,
        "issued": iso_z(moment),
        "generated": iso_z(now),
        "history": {
            "path": HISTORY_FILENAME,
            "byteLength": len(history),
            "crc32": crc32_hex(history),
        },
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
    # The STAC face of the round just written (docs/stac.md §"Point
    # products"), a pure function of the index on disk. The Collection and
    # the live Item follow the pointer: a round nothing took live gets its
    # own Item and nothing else names it.
    stac_paths = write_point_product_documents(
        output_root,
        product=PRODUCT,
        index_path=directory / INDEX_FILENAME,
        live=pointer_path is not None,
    )
    return {
        "round": round_name(moment),
        "directory": str(directory),
        "stations": len(rows),
        "reports": sum(len(station["metars"]) for station in stations.values()),
        "tafs": sum(1 for station in stations.values() if station["taf"]),
        "history": {"byteLength": len(history), "stations": len(rows)},
        "sources": index["sources"],
        "pointer": None if pointer_path is None else str(pointer_path),
        "stac": stac_paths,
    }


def load_previous_index(path: Path | None, output_root: Path) -> dict[str, Any] | None:
    """The previous round's index, whose history file is this round's:
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

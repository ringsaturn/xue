"""The ``amedas`` network: the Japan Meteorological Agency's AMeDAS, about
1 300 stations reporting every ten minutes.

There is no published API. The agency's own map page reads three kinds of
JSON under ``https://www.jma.go.jp/bosai/amedas/``, and this adapter reads
the same ones: ``const/amedastable.json`` (the station table),
``data/latest_time.txt`` (the newest observation time, in JST) and
``data/map/<YYYYMMDDHHmm>00.json`` (every station at one ten-minute time,
in JST, about 250 KB; kept about seven days). Since the snapshots outlive a
round by a week, a round fetches every snapshot after the network's newest
published time rather than only the newest one, and a gap in the schedule
fills itself on the next run.

Every value arrives as ``[value, quality]``. The page's own rule
(``isNormalData``) is the one kept here: a value is published when its
quality code is 0 (normal) or 1 (quasi-normal) and it is not null; 2–4
(insufficient data), 5 (suspended), 6 (missing) and anything else become
null. Wind direction is a 16-point code, 0 being calm and 16 north.

Content of the agency's site is reusable under the Public Data License
v1.0 with the source credited (出典：気象庁ホームページ).
"""

from __future__ import annotations

import json
import logging
import urllib.error
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from ..errors import DownloadError, SynopProductError, XueError
from ..pointproduct import iso_z, write_bytes_atomic
from .http import get
from .schema import ELEMENT_BOUNDS, HISTORY_HOURS, ROUND_MINUTES, round_directory
from .station import NetworkRead, StationMeta

LOG = logging.getLogger(__name__)

NETWORK = "amedas"
MAP_SOURCE = "jma-amedas-map"
TABLE_SOURCE = "jma-amedas-table"

BASE_URL = "https://www.jma.go.jp/bosai/amedas"
TABLE_URL = f"{BASE_URL}/const/amedastable.json"
LATEST_URL = f"{BASE_URL}/data/latest_time.txt"
MAP_URL = f"{BASE_URL}/data/map"

TABLE_FILENAME = "amedas-stations.json"
TABLE_RECORD = "amedas-stations.fetch.json"
LATEST_FILENAME = "latest_time.txt"
SNAPSHOT_DIRECTORY = "map"

TABLE_MAX_AGE = timedelta(hours=24)
CADENCE = timedelta(minutes=10)
MAX_SNAPSHOTS = HISTORY_HOURS * 6
"""A round never asks for more than the window holds: a first build, or
one after a gap longer than a day, starts from the newest 24 hours."""

JST = timezone(timedelta(hours=9))
GOOD_QUALITY = (0, 1)
PRINCIPAL_TYPES = frozenset("ABDEFG")
"""Station types that are not plain AMeDAS (``C``): staffed offices,
special weather stations, and the one-off sites (Mt. Fuji is ``F``,
Minamitorishima ``E``). They rank 0."""


def network_directory(raw_root: Path, moment: datetime) -> Path:
    return raw_root / round_directory(moment) / NETWORK


def snapshot_stamp(moment: datetime) -> str:
    """The snapshot's file stem: the JST minute with ``00`` seconds."""
    return moment.astimezone(JST).strftime("%Y%m%d%H%M00")


def parse_stamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=JST).astimezone(UTC)


def parse_latest_time(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise SynopProductError(f"latest_time.txt is not a timestamp: {text[:40]!r}") from exc
    if moment.tzinfo is None:
        raise SynopProductError("latest_time.txt carries no offset")
    return moment.astimezone(UTC)


# --- fetch -------------------------------------------------------------


def fetch_table(raw_root: Path, *, force: bool = False, now: datetime | None = None) -> SourceStatus:
    """The station table, at most once a day, conditionally. A failure
    over a copy on disk stays ``ok`` with the error noted: a day-old table
    places the same stations."""
    now = now or datetime.now(UTC)
    destination = raw_root / TABLE_FILENAME
    record_path = raw_root / TABLE_RECORD
    record: dict[str, Any] = {}
    if record_path.exists():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = {}
    fetched = record.get("fetched")
    try:
        age = now - datetime.fromisoformat(fetched) if isinstance(fetched, str) else None
    except ValueError:
        age = None
    if not force and destination.exists() and age is not None and age < TABLE_MAX_AGE:
        return SourceStatus(
            TABLE_SOURCE, True, fetched=datetime.fromisoformat(str(fetched)), url=TABLE_URL, file=TABLE_FILENAME, detail={"cached": True}
        )
    modified_since = record.get("modified") if destination.exists() else None
    try:
        response = get(TABLE_URL, modified_since=modified_since if isinstance(modified_since, str) else None)
        if response.status == 404:
            raise DownloadError(f"{TABLE_URL} is gone (HTTP 404)")
    except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
        if destination.exists():
            LOG.warning("amedas: keeping the station table on disk: %s", exc)
            return SourceStatus(TABLE_SOURCE, True, fetched=now, url=TABLE_URL, file=TABLE_FILENAME, error=str(exc), detail={"cached": True})
        return SourceStatus(TABLE_SOURCE, False, fetched=now, url=TABLE_URL, error=str(exc))
    if response.body is not None:
        write_bytes_atomic(destination, response.body)
    write_bytes_atomic(
        record_path,
        json.dumps({"url": TABLE_URL, "fetched": iso_z(now), "modified": response.last_modified or modified_since}, indent=2).encode("utf-8"),
    )
    return SourceStatus(TABLE_SOURCE, True, fetched=now, url=TABLE_URL, file=TABLE_FILENAME, detail={"cached": response.body is None})


def snapshot_times(latest: datetime, since: datetime | None) -> list[datetime]:
    """The ten-minute times after ``since`` up to ``latest``, oldest
    first, never more than the window."""
    floor = latest - timedelta(hours=HISTORY_HOURS)
    start = since if since is not None and since > floor else floor
    times: list[datetime] = []
    moment = latest
    while moment > start and len(times) < MAX_SNAPSHOTS:
        times.append(moment)
        moment -= CADENCE
    return times[::-1]


def fetch(
    raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime | None = None
) -> list[SourceStatus]:
    """Fetch the round's snapshots into ``synop.<round>/amedas/`` and the
    station table into the raw root. Nothing after the round's own minute
    is asked for, so a rebuild reads what the round could have read."""
    now = now or datetime.now(UTC)
    directory = network_directory(raw_root, moment)
    relative = f"{round_directory(moment)}/{NETWORK}"
    table = fetch_table(raw_root, force=force, now=now)
    try:
        response = get(LATEST_URL)
        if response.body is None:
            raise DownloadError(f"{LATEST_URL}: HTTP {response.status}")
        latest = min(parse_latest_time(response.body.decode("utf-8")), moment)
        write_bytes_atomic(directory / LATEST_FILENAME, response.body)
    except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("amedas: latest_time.txt: %s", exc)
        return [SourceStatus(MAP_SOURCE, False, fetched=now, url=MAP_URL, error=str(exc)), table]
    latest = latest.replace(minute=(latest.minute // ROUND_MINUTES) * ROUND_MINUTES, second=0, microsecond=0)
    fetched = missing = 0
    error: str | None = None
    for time in snapshot_times(latest, since):
        stamp = snapshot_stamp(time)
        path = directory / SNAPSHOT_DIRECTORY / f"{stamp}.json"
        if path.exists() and not force:
            fetched += 1
            continue
        url = f"{MAP_URL}/{stamp}.json"
        try:
            response = get(url)
        except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
            error = str(exc)
            break
        if response.body is None:
            missing += 1  # past the agency's retention, or never written
            continue
        write_bytes_atomic(path, response.body)
        fetched += 1
    status = SourceStatus(
        MAP_SOURCE,
        error is None or fetched > 0,
        fetched=now,
        url=MAP_URL,
        file=relative,
        error=error,
        detail={"snapshots": fetched, "missing": missing},
    )
    return [status, table]


# --- read --------------------------------------------------------------


def _degrees(value: object) -> float:
    if not isinstance(value, list) or len(value) != 2:
        raise SynopProductError(f"position {value!r} is not [degrees, minutes]")
    degrees, minutes = value
    return round(float(degrees) + float(minutes) / 60.0, 4)


def parse_table(text: str) -> dict[str, StationMeta]:
    """``amedastable.json`` → stations by AMeDAS number. Rank 0 for the
    non-AMeDAS types, 2 for a station whose element mask has neither
    temperature nor wind (the precipitation-only gauges), else 1."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SynopProductError(f"amedastable.json is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise SynopProductError("amedastable.json is not a table of stations")
    stations: dict[str, StationMeta] = {}
    for code, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        try:
            lat = _degrees(entry["lat"])
            lon = _degrees(entry["lon"])
        except (KeyError, TypeError, ValueError, SynopProductError) as exc:
            LOG.warning("amedas: station %s has no usable position (%s); skipping it", code, exc)
            continue
        kind = entry.get("type")
        elements = entry.get("elems") if isinstance(entry.get("elems"), str) else ""
        if kind in PRINCIPAL_TYPES:
            rank = 0
        elif elements[:1] == "0" and elements[2:3] == "0":
            rank = 2
        else:
            rank = 1
        names = {key: entry[field] for key, field in (("ja", "kjName"), ("ja-Kana", "knName")) if isinstance(entry.get(field), str) and entry[field]}
        altitude = entry.get("alt")
        stations[code] = StationMeta(
            id=f"{NETWORK}:{code}",
            name=entry.get("enName") if isinstance(entry.get("enName"), str) and entry["enName"] else None,
            names=names or None,
            lat=lat,
            lon=lon,
            elev=float(altitude) if isinstance(altitude, (int, float)) and not isinstance(altitude, bool) else None,
            wmo=None,
            rank=rank,
        )
    return stations


def _good(cell: object) -> float | None:
    if not isinstance(cell, list) or len(cell) != 2:
        return None
    value, quality = cell
    if quality not in GOOD_QUALITY or isinstance(quality, bool):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _bounded(key: str, value: float | None) -> float | int | None:
    if value is None:
        return None
    minimum, maximum = ELEMENT_BOUNDS[key]
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        return None
    if key in ("rh", "wd", "snow", "vis"):
        return int(round(value))
    return round(value, 1)


def wind_direction(code: float | None) -> int | None:
    """16-point code → degrees true. 0 is calm, which has no direction."""
    if code is None or code <= 0 or code > 16 or code != int(code):
        return None
    return int(code * 22.5 + 0.5)


def convert(cell: dict[str, Any]) -> dict[str, float | int | None]:
    """One station's cells in one snapshot → the product's elements."""
    return {
        "t": _bounded("t", _good(cell.get("temp"))),
        "rh": _bounded("rh", _good(cell.get("humidity"))),
        "p": _bounded("p", _good(cell.get("pressure"))),
        "slp": _bounded("slp", _good(cell.get("normalPressure"))),
        "wd": wind_direction(_good(cell.get("windDirection"))),
        "ws": _bounded("ws", _good(cell.get("wind"))),
        "gust": None,
        "pr": _bounded("pr", _good(cell.get("precipitation10m"))),
        "pr1h": _bounded("pr1h", _good(cell.get("precipitation1h"))),
        "sun": _bounded("sun", _good(cell.get("sun10m"))),
        "snow": _bounded("snow", _good(cell.get("snow"))),
        "vis": _bounded("vis", _good(cell.get("visibility"))),
    }


def parse_snapshot(text: str) -> dict[str, dict[str, float | int | None]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SynopProductError(f"snapshot is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SynopProductError("snapshot is not an object of stations")
    return {code: convert(cell) for code, cell in payload.items() if isinstance(cell, dict)}


def read(raw_root: Path, statuses: dict[str, SourceStatus]) -> NetworkRead:
    """The fetched files → stations by product id and the observations
    to merge, ``(station id, epoch seconds, elements)``. A snapshot from a
    station the table does not place is counted and dropped: without a
    position it has nowhere to go."""
    table_status = statuses.get(TABLE_SOURCE)
    stations: dict[str, StationMeta] = {}
    by_code: dict[str, StationMeta] = {}
    if table_status is not None and table_status.ok and table_status.file:
        try:
            by_code = parse_table((raw_root / table_status.file).read_text(encoding="utf-8"))
        except (OSError, SynopProductError) as exc:
            LOG.warning("amedas: station table: %s", exc)
            statuses[TABLE_SOURCE] = SourceStatus(TABLE_SOURCE, False, fetched=table_status.fetched, url=TABLE_URL, error=f"parse: {exc}")
        else:
            table_status.detail["reports"] = len(by_code)
    stations = {meta.id: meta for meta in by_code.values()}

    map_status = statuses.get(MAP_SOURCE)
    observations: list[tuple[str, int, dict[str, float | int | None]]] = []
    if map_status is None or not map_status.ok or not map_status.file:
        return NetworkRead(stations, observations, False)
    unplaced: set[str] = set()
    directory = raw_root / map_status.file / SNAPSHOT_DIRECTORY
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            epoch = int(parse_stamp(path.stem).timestamp())
            snapshot = parse_snapshot(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, SynopProductError) as exc:
            LOG.warning("amedas: %s: %s; skipping it", path.name, exc)
            continue
        for code, elements in snapshot.items():
            meta = by_code.get(code)
            if meta is None:
                unplaced.add(code)
                continue
            observations.append((meta.id, epoch, elements))
    map_status.detail["reports"] = len(observations)
    if unplaced:
        LOG.warning("amedas: %d stations are not in the station table; their reports are dropped", len(unplaced))
    return NetworkRead(stations, observations, True)

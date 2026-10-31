"""The ``airnow`` network: the US EPA's AirNow, about 2 000 stations across
the United States, Canada and Mexico (and a handful of US diplomatic
posts) reporting hourly means.

AirNow's public file share ``https://files.airnowtech.org/airnow/`` keeps
one ``HourlyAQObs_<YYYYMMDDHH>.dat`` per hour under ``<YYYY>/<YYYYMMDD>/``
(the dated archive; the file is not under ``today/``): a quoted CSV with
a row per site carrying the site's position, elevation and agency beside
its hourly PM2.5, PM10, ozone, NO2, SO2 and CO. One file is an hour of the
whole network, about 1 MB. ``ValidDate`` and ``ValidTime`` are the *start*
of the averaging hour, in UTC, so a value's time here is that plus an hour.

AirNow posts an hour roughly 35 minutes after it ends and rewrites the
files of the previous three days every hour as late data arrive. A round
therefore reads the newest three hours again every time — the rewritten
ones replace what the previous round had — and any older hour after the
network's previous ``latest``, so a gap fills itself on the next run.

The data are preliminary and unvalidated; the AirNow Data Exchange
Guidelines ask that they be shown as such, that the reporting agency be
credited (``DataSource``, carried as the station's ``agency``) and that the
values not be altered — they are published here as AirNow states them.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..airport.fetch import SourceStatus
from ..errors import XueError
from ..pointproduct import write_bytes_atomic
from .http import get
from .schema import ELEVATION_RANGE, HISTORY_HOURS, STATION, AqProductError, bounded, round_directory
from .station import NetworkRead, Observation, StationMeta

LOG = logging.getLogger(__name__)

NETWORK = "airnow"
SOURCE = "airnow-hourly"
BASE_URL = "https://files.airnowtech.org/airnow"
FILE_PREFIX = "HourlyAQObs_"

RECENT_HOURS = 3
"""Hours read again every round because AirNow is still filling them in."""

COLUMNS: dict[str, tuple[str, str]] = {
    "pm25": ("PM25", "UG/M3"),
    "pm10": ("PM10", "UG/M3"),
    "o3": ("OZONE", "PPB"),
    "no2": ("NO2", "PPB"),
    "so2": ("SO2", "PPB"),
    "co": ("CO", "PPM"),
}
"""Element → (AirNow column, the unit its ``<column>_Unit`` must state).
AirNow already reports in the product's units; a value whose unit column
says anything else is not converted but dropped."""

REQUIRED = ("AQSID", "SiteName", "Latitude", "Longitude", "Elevation", "ValidDate", "ValidTime", "DataSource")


def network_directory(raw_root: Path, moment: datetime) -> Path:
    return raw_root / round_directory(moment) / NETWORK


def file_name(start: datetime) -> str:
    return f"{FILE_PREFIX}{start:%Y%m%d%H}.dat"


def file_url(start: datetime) -> str:
    return f"{BASE_URL}/{start:%Y}/{start:%Y%m%d}/{file_name(start)}"


def file_hours(moment: datetime, since: datetime | None) -> list[datetime]:
    """The start hours a round reads, oldest first: the newest hour that
    has ended by the round and the two before it always, and every older
    hour that ends after ``since`` and inside the window."""
    newest = moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    horizon = moment - timedelta(hours=HISTORY_HOURS)
    hours: list[datetime] = []
    start = newest
    while len(hours) < HISTORY_HOURS and start + timedelta(hours=1) >= horizon:
        end = start + timedelta(hours=1)
        if len(hours) < RECENT_HOURS or since is None or end > since:
            hours.append(start)
        else:
            break
        start -= timedelta(hours=1)
    return hours[::-1]


# --- fetch -------------------------------------------------------------


def fetch(
    raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime | None = None
) -> list[SourceStatus]:
    """Fetch the round's hourly files into ``aq.<round>/airnow/``. A file
    already there is kept unless ``force``: a rebuild reads the bytes the
    round was built from."""
    now = now or datetime.now(UTC)
    directory = network_directory(raw_root, moment)
    relative = f"{round_directory(moment)}/{NETWORK}"
    fetched = missing = 0
    error: str | None = None
    for start in file_hours(moment, since):
        path = directory / file_name(start)
        if path.exists() and not force:
            fetched += 1
            continue
        url = file_url(start)
        try:
            response = get(url)
        except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
            error = str(exc)
            break
        if response.body is None:
            missing += 1  # not posted yet
            continue
        write_bytes_atomic(path, response.body)
        fetched += 1
    if error is not None:
        LOG.warning("airnow: %s", error)
    return [
        SourceStatus(
            SOURCE,
            error is None or fetched > 0,
            fetched=now,
            url=BASE_URL,
            file=relative,
            error=error,
            detail={"files": fetched, "missing": missing},
        )
    ]


# --- read --------------------------------------------------------------


def _decode(data: bytes) -> str:
    """The files are UTF-8 in practice (Québec's accents arrive as two
    bytes); a file that is not is read as Latin-1 rather than lost."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _float(text: str | None) -> float | None:
    if text is None or not text.strip():
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value else None


def _elevation(text: str | None) -> float | None:
    """AirNow writes ``0.0`` for a site whose elevation it does not have —
    half the stations reporting in an hour, Baku's at −28 m among them — so
    an exact zero is unknown rather than sea level."""
    value = _float(text)
    if value is None or value == 0.0 or not ELEVATION_RANGE[0] <= value <= ELEVATION_RANGE[1]:
        return None
    return round(value, 1)


def _start(date: str, time: str) -> datetime:
    return datetime.strptime(f"{date.strip()} {time.strip()}", "%m/%d/%Y %H:%M").replace(tzinfo=UTC)


def parse_file(data: bytes) -> tuple[list[tuple[StationMeta, int, dict[str, float | None]]], int]:
    """One ``HourlyAQObs`` file → ``(station, epoch at the end of the hour,
    elements)`` per row, and how many rows had no usable position or id."""
    reader = csv.DictReader(io.StringIO(_decode(data), newline=""))
    missing = [column for column in REQUIRED if column not in (reader.fieldnames or ())]
    if missing:
        raise AqProductError(f"HourlyAQObs has no {', '.join(missing)} column")
    rows: list[tuple[StationMeta, int, dict[str, float | None]]] = []
    unplaced = 0
    for row in reader:
        code = (row.get("AQSID") or "").strip()
        lat = _float(row.get("Latitude"))
        lon = _float(row.get("Longitude"))
        station_id = f"{NETWORK}:{code}"
        if (
            not STATION.match(station_id)
            or lat is None
            or lon is None
            or (lat == 0.0 and lon == 0.0)
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
        ):
            unplaced += 1
            continue
        try:
            epoch = int((_start(row["ValidDate"], row["ValidTime"]) + timedelta(hours=1)).timestamp())
        except (ValueError, TypeError):
            unplaced += 1
            continue
        elements: dict[str, float | None] = {}
        for key, (column, unit) in COLUMNS.items():
            value = _float(row.get(column))
            if value is not None and (row.get(f"{column}_Unit") or "").strip().upper() != unit:
                value = None
            elements[key] = bounded(key, value)
        name = (row.get("SiteName") or "").strip() or None
        agency = (row.get("DataSource") or "").strip() or None
        meta = StationMeta(
            id=station_id,
            name=name,
            names=None,
            lat=round(lat, 4),
            lon=round(lon, 4),
            elev=_elevation(row.get("Elevation")),
            rank=1,
            agency=agency,
        )
        rows.append((meta, epoch, elements))
    return rows, unplaced


def read(raw_root: Path, statuses: dict[str, SourceStatus]) -> NetworkRead:
    """The fetched hours → stations by id and the observations to merge.
    Read oldest first, so a station's metadata is the newest file's."""
    status = statuses.get(SOURCE)
    stations: dict[str, StationMeta] = {}
    observations: list[Observation] = []
    if status is None or not status.ok or not status.file:
        return NetworkRead(stations, observations, False)
    directory = raw_root / status.file
    unplaced = reports = 0
    for path in sorted(directory.glob(f"{FILE_PREFIX}*.dat")) if directory.is_dir() else []:
        try:
            rows, dropped = parse_file(path.read_bytes())
        except (OSError, csv.Error, AqProductError) as exc:
            LOG.warning("airnow: %s: %s; skipping it", path.name, exc)
            continue
        unplaced += dropped
        # A row with no value is kept too: in a rewritten hour it says the
        # station's earlier values for that hour were withdrawn.
        for meta, epoch, elements in rows:
            stations[meta.id] = meta
            observations.append((meta.id, epoch, elements))
            reports += any(value is not None for value in elements.values())
    status.detail["reports"] = reports
    status.detail["unplaced"] = unplaced
    return NetworkRead(stations, observations, True)


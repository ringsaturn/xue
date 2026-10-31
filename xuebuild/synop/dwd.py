"""The ``dwd`` network: Deutscher Wetterdienst's national ten-minute SYNOP
reports, about 1 000 German stations — some 220 WMO stations and 800
national ones, of which about 470 measure precipitation alone.

``https://opendata.dwd.de/weather/weather_reports/synoptic/germany/`` holds
the last 48 hours as BUFR batches named
``Z__C_EDZW_<yyyymmddHHMMSS>_bda01,synop_bufr_GER_999999_999999__MW_<seq>.bin``,
about eight an hour. The sequence number cannot be predicted, so a round
lists the directory and fetches every batch stamped after the network's
newest published time (at most the 24-hour window, none after the round),
and the reading decodes them with eccodes' ``bufr_dump -j f``, the flat key
stream ``xuebuild.sounding.bufr`` also walks.

A batch mixes several templates and several observation times. Ten-minute
values arrive in half-hourly batches 20–35 minutes late; the batch on the
hour carries, besides, an hourly SYNOP template for the WMO stations and an
hourly national template whose precipitation is a one-hour total
(``timePeriod`` −60 min) — the network's ``pr1h``. One station at one time
is therefore spread over several subsets, in different batches and rounds:
the subsets read in a round are merged element by element, and the network
is ``partial`` so the build lays them over earlier rounds the same way.

Every value is taken by its BUFR key *and* the period or sensor height the
template states before it, never by its position in the template:

- ``airTemperature`` / ``dewpointTemperature`` at a sensor 1 m or more
  above ground (the 5 cm one is the grass minimum), K → °C;
- ``relativeHumidity``; ``nonCoordinatePressure`` and
  ``pressureReducedToMeanSeaLevel``, Pa → hPa;
- ``windSpeed`` / ``windDirection`` over −10 min; ``maximumWindGustSpeed``
  over the shortest period it is given for, up to an hour;
- ``totalPrecipitationOrTotalWaterEquivalent`` over −10 min (``pr``) and
  −60 min (``pr1h``); the one-minute series and the 3/6/12/24-hour totals
  are not elements;
- ``sunshineOverPeriodSpecified`` over −10 min, minutes;
- ``totalSnowDepth``, m → cm, its negative codes (−0.01 m "a little",
  −0.02 m "patchy") null;
- ``meteorologicalOpticalRange`` (the ten-minute MOR) for ``vis``, else
  the hourly template's ``horizontalVisibility``.

The BUFR carries no quality flags; a missing value is null and a value
outside the element's range is null.

Data: CC BY 4.0, credited "Source: Deutscher Wetterdienst".
"""

from __future__ import annotations

import logging
import math
import re
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from ..eccodescli import bufr_dump_json
from ..errors import DownloadError, XueError
from ..gdal import require_command
from ..pointproduct import write_bytes_atomic
from .http import get
from .schema import ELEMENT_BOUNDS, ELEMENTS, ELEVATION_RANGE, HISTORY_HOURS, STATION, round_directory
from .station import NetworkRead, StationMeta

LOG = logging.getLogger(__name__)

NETWORK = "dwd"
SOURCE = "dwd-synop-bufr"
BASE_URL = "https://opendata.dwd.de/weather/weather_reports/synoptic/germany/"

FILE_NAME = re.compile(r"^Z__C_EDZW_(?P<stamp>\d{14})_bda01,synop_bufr_GER_999999_999999__MW_(?P<sequence>\d+)\.bin$")
"""A batch: its stamp (UTC) and sequence number. The directory also keeps
``Z__C_EDZW_latest_…__MW_XXX.bin``, a copy of the newest batch, which the
pattern leaves out."""
HREF = re.compile(r'href="([^"?#]+)"')

INTEGERS = frozenset({"rh", "wd", "snow", "vis"})
TEN_MINUTES = -600
ONE_HOUR = -3600
MIN_SENSOR_HEIGHT = 1.0
"""Metres: an air temperature or dew point from a sensor lower than this
is the five-centimetre grass minimum, which the templates put beside the
two-metre screen value."""


def network_directory(raw_root: Path, moment: datetime) -> Path:
    return raw_root / round_directory(moment) / NETWORK


# --- fetch -------------------------------------------------------------


def parse_listing(html: str) -> list[tuple[datetime, str]]:
    """The directory page → its batches, ``(stamp, file name)``, oldest
    first."""
    batches: list[tuple[datetime, str]] = []
    for href in HREF.findall(html):
        name = urllib.parse.unquote(href.rsplit("/", 1)[-1])
        match = FILE_NAME.match(name)
        if match is None:
            continue
        stamp = datetime.strptime(match.group("stamp"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        batches.append((stamp, name))
    return sorted(set(batches))


def batches_to_fetch(batches: list[tuple[datetime, str]], moment: datetime, since: datetime | None) -> list[tuple[datetime, str]]:
    """The batches stamped after ``since`` (the network's newest published
    time; at most the window before the round) and not after the round.
    A batch holds nothing newer than its stamp, so everything newer than
    ``since`` is in these; values for older times that arrive late are in
    them too, because they arrive in later batches."""
    floor = moment - timedelta(hours=HISTORY_HOURS)
    start = since if since is not None and since > floor else floor
    return [(stamp, name) for stamp, name in batches if start < stamp <= moment]


def fetch(
    raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime | None = None
) -> list[SourceStatus]:
    """List the directory and fetch the round's batches into
    ``synop.<round>/dwd/`` under their own names."""
    now = now or datetime.now(UTC)
    directory = network_directory(raw_root, moment)
    relative = f"{round_directory(moment)}/{NETWORK}"
    try:
        response = get(BASE_URL)
        if response.body is None:
            raise DownloadError(f"{BASE_URL}: HTTP {response.status}")
        batches = parse_listing(response.body.decode("utf-8", errors="replace"))
    except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("dwd: listing: %s", exc)
        return [SourceStatus(SOURCE, False, fetched=now, url=BASE_URL, error=str(exc))]
    fetched = missing = 0
    error: str | None = None
    for _stamp, name in batches_to_fetch(batches, moment, since):
        path = directory / name
        if path.exists() and not force:
            fetched += 1
            continue
        try:
            response = get(BASE_URL + urllib.parse.quote(name))
        except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
            error = str(exc)
            break
        if response.body is None:
            missing += 1  # rotated out between the listing and the request
            continue
        write_bytes_atomic(path, response.body)
        fetched += 1
    directory.mkdir(parents=True, exist_ok=True)
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


def _split(entries: list[dict[str, Any]]) -> list[list[tuple[str, Any, str | None]]]:
    """``bufr_dump -j f``'s flat stream → one ``(key, value, units)`` list
    per subset. ``subsetNumber`` opens each subset of an uncompressed
    message; a compressed one carries every per-subset value as an array,
    which is unrolled."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("key") == "subsetNumber":
            if current:
                chunks.append(current)
            current = []
            continue
        current.append(entry)
    if current:
        chunks.append(current)
    subsets: list[list[tuple[str, Any, str | None]]] = []
    for chunk in chunks:
        count = max((len(entry["value"]) for entry in chunk if isinstance(entry.get("value"), list)), default=1)
        for position in range(count):
            walked: list[tuple[str, Any, str | None]] = []
            for entry in chunk:
                key = entry.get("key")
                if not isinstance(key, str):
                    continue
                value = entry.get("value")
                if isinstance(value, list):
                    value = value[position] if position < len(value) else None
                walked.append((key, value, entry.get("units")))
            subsets.append(walked)
    return subsets


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return float(value)


def _half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def bounded(key: str, value: float | None) -> float | int | None:
    """A value in the element's unit → the published one: inside the
    element's range or null, an integer where §2 says so (half up), else
    to a tenth."""
    if value is None:
        return None
    minimum, maximum = ELEMENT_BOUNDS[key]
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        return None
    if key in INTEGERS:
        return _half_up(value)
    return round(value, 1)


def _seconds(value: Any, units: str | None) -> int | None:
    number = _number(value)
    if number is None:
        return None
    scale = {"min": 60, "h": 3600, "s": 1}.get(units or "")
    return None if scale is None else int(number * scale)


@dataclass
class Subset:
    """What one subset says about one station at one time."""

    station: str
    """The DWD station identifier (``shortStationName`` or the WMO block
    and station number)."""
    wmo: str | None
    epoch: int | None
    name: str | None
    lat: float | None
    lon: float | None
    elev: float | None
    elements: dict[str, float | int | None] = field(default_factory=dict)
    priority: dict[str, int] = field(default_factory=dict)
    """Per element, how good this subset's value is when two subsets of
    one station and time both carry one: a gust over a shorter period, the
    ten-minute MOR over the observer's visibility. Equal priority: the
    later subset wins."""


HEADER_KEYS = frozenset(
    {
        "shortStationName",
        "blockNumber",
        "stationNumber",
        "stationOrSiteName",
        "year",
        "month",
        "day",
        "hour",
        "minute",
        "latitude",
        "longitude",
        "heightOfStationGroundAboveMeanSeaLevel",
    }
)


def _identity(header: dict[str, Any]) -> tuple[str, str | None] | None:
    """``(station id part, wmo)``: DWD's own station identifier — the
    ``Kennung`` of its station list — which the BUFR carries as
    ``shortStationName`` for a national station and as the WMO block and
    station number for a WMO one. A subset with neither names no station
    (the NIL reports of stations that sent nothing)."""
    block = _number(header.get("blockNumber"))
    number = _number(header.get("stationNumber"))
    wmo = f"{int(block):02d}{int(number):03d}" if block is not None and number is not None and 1 <= block <= 99 and 0 <= number <= 999 else None
    short = header.get("shortStationName")
    if isinstance(short, str) and short.strip():
        key = short.strip()
    elif wmo is not None:
        key = wmo
    else:
        return None
    if STATION.match(f"{NETWORK}:{key}") is None:
        return None
    return key, wmo


def _epoch(header: dict[str, Any]) -> int | None:
    parts = [_number(header.get(key)) for key in ("year", "month", "day", "hour", "minute")]
    if any(part is None for part in parts):
        return None
    try:
        moment = datetime(*(int(part) for part in parts), tzinfo=UTC)  # type: ignore[arg-type]
    except ValueError:
        return None
    return int(moment.timestamp())


def parse_subset(walked: list[tuple[str, Any, str | None]]) -> Subset | None:
    """One subset → what it reports, by key and the period or sensor
    height in force where the key appears (module docstring)."""
    header: dict[str, Any] = {}
    for key, value, _units in walked:
        if key in HEADER_KEYS and key not in header:
            header[key] = value
    identity = _identity(header)
    if identity is None:
        return None
    station, wmo = identity
    lat, lon = _number(header.get("latitude")), _number(header.get("longitude"))
    if lat is not None and not -90 <= lat <= 90 or lon is not None and not -180 <= lon <= 180:
        lat = lon = None
    elev = _number(header.get("heightOfStationGroundAboveMeanSeaLevel"))
    if elev is not None and not ELEVATION_RANGE[0] <= elev <= ELEVATION_RANGE[1]:
        elev = None
    name = header.get("stationOrSiteName")
    subset = Subset(
        station=station,
        wmo=wmo,
        epoch=_epoch(header),
        name=name.strip() if isinstance(name, str) and name.strip() else None,
        lat=None if lat is None else round(lat, 4),
        lon=None if lon is None else round(lon, 4),
        elev=elev,
    )

    def offer(element: str, value: float | int | None, priority: int = 0) -> None:
        if value is None:
            return
        if element in subset.elements and subset.priority[element] >= priority:
            return
        subset.elements[element] = value
        subset.priority[element] = priority

    period: int | None = None
    height: float | None = None
    wind_speed: float | None = None
    wind_direction: float | None = None
    for key, value, units in walked:
        number = _number(value)
        if key == "timePeriod":
            period = _seconds(value, units)
        elif key == "heightOfSensorAboveLocalGroundOrDeckOfMarinePlatform":
            height = number
        elif key == "airTemperature" and (height is None or height >= MIN_SENSOR_HEIGHT):
            offer("t", bounded("t", None if number is None else number - 273.15))
        elif key == "dewpointTemperature" and (height is None or height >= MIN_SENSOR_HEIGHT):
            offer("td", bounded("td", None if number is None else number - 273.15))
        elif key == "relativeHumidity":
            offer("rh", bounded("rh", number))
        elif key == "nonCoordinatePressure":
            offer("p", bounded("p", None if number is None else number / 100))
        elif key == "pressureReducedToMeanSeaLevel":
            offer("slp", bounded("slp", None if number is None else number / 100))
        elif key == "windSpeed" and period == TEN_MINUTES and wind_speed is None:
            wind_speed = number
        elif key == "windDirection" and period == TEN_MINUTES and wind_direction is None:
            wind_direction = number
        elif key == "maximumWindGustSpeed" and period is not None and ONE_HOUR <= period < 0:
            offer("gust", bounded("gust", number), priority=period)  # shorter (nearer zero) is better
        elif key == "totalPrecipitationOrTotalWaterEquivalent" and period == TEN_MINUTES:
            offer("pr", bounded("pr", number))
        elif key == "totalPrecipitationOrTotalWaterEquivalent" and period == ONE_HOUR:
            offer("pr1h", bounded("pr1h", number))
        elif key in ("sunshineOverPeriodSpecified", "totalSunshine") and period == TEN_MINUTES:
            offer("sun", bounded("sun", number))
        elif key == "totalSnowDepth":
            # −0.01 m and −0.02 m are codes ("a little", "patchy"), not depths.
            offer("snow", None if number is None or number < 0 else bounded("snow", number * 100))
        elif key == "meteorologicalOpticalRange":
            offer("vis", bounded("vis", number), priority=1)
        elif key == "horizontalVisibility":
            offer("vis", bounded("vis", number), priority=0)
    speed = bounded("ws", wind_speed)
    if speed is not None:
        offer("ws", speed)
        # BUFR 0 11 001 says calm with 0 and north with 360; the product
        # has no direction for calm.
        if speed > 0 and wind_direction is not None and 0 < wind_direction <= 360:
            offer("wd", _half_up(wind_direction))
    return subset


@dataclass
class _Station:
    meta: Subset
    """The newest subset that placed the station."""
    observations: dict[int, Subset] = field(default_factory=dict)


def _merge(into: Subset, subset: Subset) -> None:
    for element, value in subset.elements.items():
        priority = subset.priority[element]
        if element in into.elements and into.priority[element] > priority:
            continue
        into.elements[element] = value
        into.priority[element] = priority


def _rank(station: _Station) -> int:
    """0 for a WMO station, 2 for one that reported precipitation and
    nothing else this round, 1 otherwise."""
    if station.meta.wmo is not None:
        return 0
    reported = {element for subset in station.observations.values() for element in subset.elements}
    if reported and reported <= {"pr", "pr1h"}:
        return 2
    return 1


def read_batches(paths: list[Path]) -> tuple[dict[str, _Station], int]:
    """Batches, oldest first → stations by DWD id with their merged
    observations, and how many subsets named no station."""
    stations: dict[str, _Station] = {}
    unplaced = 0
    if paths:
        # Without eccodes nothing decodes: the network fails, rather than
        # every batch being skipped as if it were corrupt.
        require_command("bufr_dump")
    for path in paths:
        try:
            payload = bufr_dump_json(path)
        except XueError as exc:
            LOG.warning("dwd: %s does not decode (%s); skipping it", path.name, exc)
            continue
        entries = payload.get("messages") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            LOG.warning("dwd: %s decodes to no messages; skipping it", path.name)
            continue
        for walked in _split(entries):
            subset = parse_subset(walked)
            if subset is None:
                unplaced += 1
                continue
            station = stations.get(subset.station)
            if station is None:
                station = stations[subset.station] = _Station(subset)
            elif subset.lat is not None and subset.lon is not None:
                station.meta = subset
            if subset.epoch is None:
                continue
            existing = station.observations.get(subset.epoch)
            if existing is None:
                station.observations[subset.epoch] = existing = Subset(**{**subset.__dict__, "elements": {}, "priority": {}})
            _merge(existing, subset)
    return stations, unplaced


def read(raw_root: Path, statuses: dict[str, SourceStatus]) -> NetworkRead:
    status = statuses.get(SOURCE)
    if status is None or not status.ok or not status.file:
        return NetworkRead({}, [], False)
    directory = raw_root / status.file
    paths = sorted(directory.glob("*.bin"), key=lambda path: path.name) if directory.is_dir() else []
    stations, unplaced = read_batches(paths)
    metas: dict[str, StationMeta] = {}
    observations: list[tuple[str, int, dict[str, float | int | None]]] = []
    for key, station in stations.items():
        meta = station.meta
        if meta.lat is None or meta.lon is None:
            continue  # placed by no subset this round: the previous round's metadata stands
        station_id = f"{NETWORK}:{key}"
        metas[station_id] = StationMeta(
            id=station_id, name=meta.name, names=None, lat=meta.lat, lon=meta.lon, elev=meta.elev, wmo=meta.wmo, rank=_rank(station)
        )
    for key, station in sorted(stations.items()):
        for epoch, subset in sorted(station.observations.items()):
            if subset.elements:
                observations.append((f"{NETWORK}:{key}", epoch, {element: subset.elements.get(element) for element in ELEMENTS}))
    status.detail["reports"] = len(observations)
    status.detail["unplaced"] = unplaced
    return NetworkRead(metas, observations, True)

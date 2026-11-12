"""The ``hko`` network: the Hong Kong Observatory's regional weather, about
fifty automatic and manned stations every ten minutes, with its automatic
weather stations' hourly rainfall and its tide gauges' water level.

The Observatory publishes the latest reading of each element as one CSV
under ``https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/``
— air temperature, relative humidity, mean sea-level pressure, the ten-
minute wind and the ten-minute visibility — each row a time (HKT,
``YYYYMMDDHHMM``), a station *name* and the values. Two more feeds are read
beside them, each its own source: the past hour's rainfall at 36 stations
(a JSON document of the Observatory's open-data API, renewed every fifteen
minutes) and the latest water level of the tide gauges (one CSV, every
five minutes). Only the latest is served: a round that does not run loses
that time for good.

None of the files carries a station number or a position, so the stations
come from ``hko_stations.json`` beside this module, built from the
Observatory's station page (``hko.gov.hk/en/cis/stn.htm``, Chinese names
from its ``/tc/`` twin) — the code, English and Chinese name, position and
ground elevation of every current weather, buoy and wind station — and
the rain and tide gauges the two extra feeds name. A file's name is the
table's English name, or an alias the table lists for every file (``Chek
Lap Kok`` is the airport, ``HKA``) or for one kind of file only (``Tsing
Yi`` is the New Tsing Yi Station for temperature and humidity but the
Shell Oil Depot anemometer for wind). A name the table does not resolve is
dropped and counted as ``unplaced``.

Missing values are ``N/A`` or empty in the regional CSVs, ``M`` in the
rainfall (whole millimetres otherwise) and dashes in the tide file. Wind
directions are the eight compass points, ``Calm`` (no direction, speed 0)
or ``Variable``; speeds are km/h. Visibility reads ``18 km``. Tide heights
are metres above Hong Kong Chart Datum. There are no quality flags.

Data: DATA.GOV.HK terms of use, the Observatory credited.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import re
import urllib.error
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from ..errors import SynopProductError, XueError
from ..pointproduct import sha256_hex, write_bytes_atomic
from ..http import get
from .schema import ELEMENT_BOUNDS, ELEMENTS, raw_directory
from .station import NetworkRead, StationMeta

LOG = logging.getLogger(__name__)

NETWORK = "hko"
SOURCE = "hko-regional-weather"
RAINFALL_SOURCE = "hko-hourly-rainfall"
TIDE_SOURCE = "hko-tide"
BASE_URL = "https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather"
FILES = {
    "temperature": "latest_1min_temperature.csv",
    "humidity": "latest_1min_humidity.csv",
    "pressure": "latest_1min_pressure.csv",
    "wind": "latest_10min_wind.csv",
    "visibility": "latest_10min_visibility.csv",
}
"""The five regional files, by the kind ``hko_stations.json``'s per-file
aliases name them."""
RAINFALL_URL = "https://data.weather.gov.hk/weatherAPI/opendata/hourlyRainfall.php?lang=en"
RAINFALL_FILE = "hourly_rainfall.json"
TIDE_URL = "https://data.weather.gov.hk/weatherAPI/hko_data/tide/ALL_en.csv"
TIDE_FILE = "tide.csv"


@dataclass(frozen=True)
class Feed:
    """One source of the network: its files (kind → file name, saved under
    the round's ``hko/``) and the URL each is fetched from."""

    source: str
    url: str
    files: dict[str, str]
    urls: dict[str, str]


FEEDS = (
    Feed(SOURCE, BASE_URL, FILES, {kind: f"{BASE_URL}/{name}" for kind, name in FILES.items()}),
    Feed(RAINFALL_SOURCE, RAINFALL_URL, {"rainfall": RAINFALL_FILE}, {"rainfall": RAINFALL_URL}),
    Feed(TIDE_SOURCE, TIDE_URL, {"tide": TIDE_FILE}, {"tide": TIDE_URL}),
)
SOURCES = tuple(feed.source for feed in FEEDS)
KINDS = frozenset(kind for feed in FEEDS for kind in feed.files)
"""Every kind of file, as the table's per-file aliases name them."""

TABLE_PATH = Path(__file__).with_name("hko_stations.json")
HKT = timezone(timedelta(hours=8))
KMH = 3.6

COMPASS = {
    "North": 360,
    "Northeast": 45,
    "East": 90,
    "Southeast": 135,
    "South": 180,
    "Southwest": 225,
    "West": 270,
    "Northwest": 315,
}
VISIBILITY = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(km|m)\s*$")
DECIMALS = {"tide": 2}
"""Elements kept to more than a tenth: the tide file gives centimetres."""


def network_directory(raw_root: Path, moment: datetime) -> Path:
    return raw_root / raw_directory(moment) / NETWORK


# --- fetch -------------------------------------------------------------


def _fetch_feed(feed: Feed, directory: Path, relative: str, now: datetime, force: bool) -> SourceStatus:
    fetched = missing = 0
    errors: list[str] = []
    for kind, name in feed.files.items():
        path = directory / name
        if path.exists() and not force:
            fetched += 1
            continue
        try:
            response = get(feed.urls[kind])
        except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        if response.body is None:
            missing += 1
            continue
        write_bytes_atomic(path, response.body)
        fetched += 1
    ok = fetched > 0
    return SourceStatus(
        feed.source,
        ok,
        fetched=now,
        url=feed.url,
        file=relative if ok else None,
        error="; ".join(errors) or (None if ok else "no file is served"),
        detail={"files": fetched, "missing": missing},
    )


def fetch(
    raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime | None = None
) -> list[SourceStatus]:
    """Every feed's latest files into ``synop.<round>/hko/``, one status per
    feed, so one that fails does not take the others with it. ``since``
    does not matter: there is nothing but the latest to ask for."""
    del since
    now = now or datetime.now(UTC)
    directory = network_directory(raw_root, moment)
    relative = f"{raw_directory(moment)}/{NETWORK}"
    return [_fetch_feed(feed, directory, relative, now, force) for feed in FEEDS]


# --- the station table -------------------------------------------------


@cache
def station_table() -> dict[str, Any]:
    """``hko_stations.json``: stations by code, the aliases, and the
    resolver's per-file aliases."""
    try:
        payload = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SynopProductError(f"{TABLE_PATH.name} is not readable: {exc}") from exc
    stations = {entry["code"]: entry for entry in payload["stations"]}
    return {
        "stations": stations,
        "byName": {entry["name"]: code for code, entry in stations.items()},
        "aliases": payload.get("aliases", {}),
        "fileAliases": payload.get("fileAliases", {}),
    }


def resolve(kind: str, name: str) -> str | None:
    """A file's station name → the station code, or None. The rainfall
    feed spells ``King’s Park`` with a typographic apostrophe."""
    table = station_table()
    name = name.strip().replace("’", "'")
    code = table["fileAliases"].get(kind, {}).get(name) or table["aliases"].get(name) or table["byName"].get(name)
    return code if code in table["stations"] else None


def station_metas() -> dict[str, StationMeta]:
    metas: dict[str, StationMeta] = {}
    for code, entry in station_table()["stations"].items():
        station_id = f"{NETWORK}:{code}"
        metas[station_id] = StationMeta(
            id=station_id,
            name=entry["name"],
            names=entry.get("names") or None,
            lat=entry["lat"],
            lon=entry["lon"],
            elev=entry.get("elev"),
            wmo=None,
            rank=entry["rank"],
        )
    return metas


# --- read --------------------------------------------------------------


def _value(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _bounded(key: str, value: float | None) -> float | int | None:
    if value is None:
        return None
    minimum, maximum = ELEMENT_BOUNDS[key]
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        return None
    if key in ("rh", "wd", "snow", "vis"):
        return int(math.floor(value + 0.5))
    return round(value, DECIMALS.get(key, 1))


def _speed(text: str | None) -> float | int | None:
    if text is not None and text.strip() == "Calm":
        return 0.0
    value = _value(text)
    return _bounded("ws", None if value is None else value / KMH)


def wind(direction: str | None, speed: str | None, gust: str | None) -> dict[str, float | int | None]:
    """A wind row → ``wd``, ``ws``, ``gust``. ``Calm`` has no direction and
    a speed of 0; ``Variable`` and anything not a compass point has no
    direction."""
    word = (direction or "").strip()
    mean = 0.0 if word == "Calm" else _speed(speed)
    heading = COMPASS.get(word) if mean else None
    gusts = _value(gust)
    return {"wd": heading, "ws": mean, "gust": _bounded("gust", None if gusts is None else gusts / KMH)}


def visibility(text: str | None) -> int | None:
    match = VISIBILITY.match(text or "")
    if match is None:
        return None
    number = float(match.group(1)) * (1000 if match.group(2) == "km" else 1)
    return _bounded("vis", number)  # type: ignore[return-value]


def parse_time(text: str) -> int:
    return int(datetime.strptime(text.strip(), "%Y%m%d%H%M").replace(tzinfo=HKT).timestamp())


Reading = tuple[str, int, dict[str, float | int | None]]


def parse_csv(kind: str, text: str) -> list[Reading]:
    """One regional file → ``(CSV station name, epoch, elements)`` per row."""
    rows = list(csv.reader(io.StringIO(text)))
    readings: list[Reading] = []
    for row in rows[1:]:
        if len(row) < 3 or not row[0].strip():
            continue
        try:
            epoch = parse_time(row[0])
        except ValueError:
            continue
        name = row[1]
        if kind == "temperature":
            elements = {"t": _bounded("t", _value(row[2]))}
        elif kind == "humidity":
            elements = {"rh": _bounded("rh", _value(row[2]))}
        elif kind == "pressure":
            elements = {"slp": _bounded("slp", _value(row[2]))}
        elif kind == "wind":
            elements = wind(row[2], row[3] if len(row) > 3 else None, row[4] if len(row) > 4 else None)
        else:
            elements = {"vis": visibility(row[2])}
        readings.append((name, epoch, elements))
    return readings


def parse_rainfall(text: str) -> list[Reading]:
    """The hourly rainfall document → one ``pr1h`` reading per station, all
    at its ``obsTime``: the total over the hour ending then. ``M`` (the
    system under maintenance) is no value."""
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("the rainfall document is not an object")
    stamp = datetime.fromisoformat(str(payload["obsTime"]))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=HKT)
    epoch = int(stamp.timestamp())
    readings: list[Reading] = []
    for entry in payload.get("hourlyRainfall") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("automaticWeatherStation"), str):
            continue
        if entry.get("unit", "mm") != "mm":
            continue
        value = entry.get("value")
        readings.append(
            (entry["automaticWeatherStation"], epoch, {"pr1h": _bounded("pr1h", _value(None if value is None else str(value)))})
        )
    return readings


def parse_tide(text: str) -> list[Reading]:
    """The tide file → ``(gauge name, epoch, {"tide": metres})`` per row:
    ``Tide Station,Date,Time,Height(m)``, the date and time HKT, a missing
    height dashes."""
    readings: list[Reading] = []
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        if len(row) < 4 or not row[0].strip():
            continue
        try:
            stamp = datetime.strptime(f"{row[1].strip()} {row[2].strip()}", "%Y-%m-%d %H:%M").replace(tzinfo=HKT)
        except ValueError:
            continue
        readings.append((row[0], int(stamp.timestamp()), {"tide": _bounded("tide", _value(row[3]))}))
    return readings


def _parse(kind: str, text: str) -> list[Reading]:
    if kind == "rainfall":
        return parse_rainfall(text)
    if kind == "tide":
        return parse_tide(text)
    return parse_csv(kind, text)


def read(raw_root: Path, statuses: dict[str, SourceStatus]) -> NetworkRead:
    """The round's files → the table's stations and their readings, merged
    per (station, time) across the files and feeds."""
    stations = station_metas()
    merged: dict[tuple[str, int], dict[str, float | int | None]] = {}
    any_ok = False
    for feed in FEEDS:
        status = statuses.get(feed.source)
        if status is None or not status.ok or not status.file:
            continue
        any_ok = True
        directory = raw_root / status.file
        placed: set[tuple[str, int]] = set()
        unplaced = 0
        read_bytes: list[bytes] = []
        for kind, name in feed.files.items():
            path = directory / name
            if not path.exists():
                continue
            try:
                body = path.read_bytes()
                readings = _parse(kind, body.decode("utf-8-sig"))
            except (OSError, UnicodeDecodeError, ValueError, KeyError) as exc:
                LOG.warning("hko: %s: %s; skipping it", name, exc)
                continue
            read_bytes.append(body)
            for station_name, epoch, elements in readings:
                code = resolve(kind, station_name)
                if code is None:
                    unplaced += 1
                    continue
                values = {key: value for key, value in elements.items() if value is not None}
                if not values:
                    continue
                key = (f"{NETWORK}:{code}", epoch)
                merged.setdefault(key, {}).update(values)
                placed.add(key)
        status.detail["reports"] = len(placed)
        status.detail["unplaced"] = unplaced
        if read_bytes:
            status.detail["sha256"] = sha256_hex(*read_bytes)
        if unplaced:
            LOG.warning("hko: %d %s readings name a station the table does not place; they are dropped", unplaced, feed.source)
    if not any_ok:
        return NetworkRead(stations, [], False)
    observations = [
        (station_id, epoch, {element: elements.get(element) for element in ELEMENTS})
        for (station_id, epoch), elements in sorted(merged.items())
    ]
    return NetworkRead(stations, observations, True)

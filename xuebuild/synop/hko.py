"""The ``hko`` network: the Hong Kong Observatory's regional weather, about
fifty automatic and manned stations every ten minutes.

The Observatory publishes the latest reading of each element as one CSV
under ``https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/``
— air temperature, relative humidity, mean sea-level pressure, the ten-
minute wind and the ten-minute visibility — each row a time (HKT,
``YYYYMMDDHHMM``), a station *name* and the values. Only the latest is
served: a round that does not run loses that time for good.

The CSVs carry no station number and no position, so the stations come
from ``hko_stations.json`` beside this module, built once from the
Observatory's station page (``hko.gov.hk/en/cis/stn.htm``, Chinese names
from its ``/tc/`` twin): the code, English and Chinese name, position and
ground elevation of every current weather, buoy and wind station. A CSV
name is the table's English name, or an alias the table lists for every
file (``Chek Lap Kok`` is the airport, ``HKA``) or for one file only
(``Tsing Yi`` is the New Tsing Yi Station for temperature and humidity
but the Shell Oil Depot anemometer for wind). A name the table does not
resolve is dropped and counted as ``unplaced``.

Missing values are ``N/A`` or empty. Wind directions are the eight compass
points, ``Calm`` (no direction, speed 0) or ``Variable``; speeds are km/h.
Visibility reads ``18 km``. There are no quality flags.

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
from datetime import UTC, datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from ..errors import SynopProductError, XueError
from ..pointproduct import write_bytes_atomic
from .http import get
from .schema import ELEMENT_BOUNDS, ELEMENTS, round_directory
from .station import NetworkRead, StationMeta

LOG = logging.getLogger(__name__)

NETWORK = "hko"
SOURCE = "hko-regional-weather"
BASE_URL = "https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather"
FILES = {
    "temperature": "latest_1min_temperature.csv",
    "humidity": "latest_1min_humidity.csv",
    "pressure": "latest_1min_pressure.csv",
    "wind": "latest_10min_wind.csv",
    "visibility": "latest_10min_visibility.csv",
}
"""The five files, by the kind ``hko_stations.json``'s per-file aliases
name them."""

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


def network_directory(raw_root: Path, moment: datetime) -> Path:
    return raw_root / round_directory(moment) / NETWORK


# --- fetch -------------------------------------------------------------


def fetch(
    raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime | None = None
) -> list[SourceStatus]:
    """The five latest CSVs into ``synop.<round>/hko/``. ``since`` does not
    matter: there is nothing but the latest to ask for."""
    del since
    now = now or datetime.now(UTC)
    directory = network_directory(raw_root, moment)
    relative = f"{round_directory(moment)}/{NETWORK}"
    fetched = missing = 0
    errors: list[str] = []
    for name in FILES.values():
        path = directory / name
        if path.exists() and not force:
            fetched += 1
            continue
        url = f"{BASE_URL}/{name}"
        try:
            response = get(url)
        except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        if response.body is None:
            missing += 1
            continue
        write_bytes_atomic(path, response.body)
        fetched += 1
    ok = fetched > 0
    return [
        SourceStatus(
            SOURCE,
            ok,
            fetched=now,
            url=BASE_URL,
            file=relative if ok else None,
            error="; ".join(errors) or (None if ok else "no file is served"),
            detail={"files": fetched, "missing": missing},
        )
    ]


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
    """A CSV's station name → the station code, or None."""
    table = station_table()
    name = name.strip()
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
    return round(value, 1)


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


def parse_csv(kind: str, text: str) -> list[tuple[str, int, dict[str, float | int | None]]]:
    """One file → ``(CSV station name, epoch, elements)`` per row."""
    rows = list(csv.reader(io.StringIO(text)))
    readings: list[tuple[str, int, dict[str, float | int | None]]] = []
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


def read(raw_root: Path, statuses: dict[str, SourceStatus]) -> NetworkRead:
    """The round's CSVs → the table's stations and their readings, merged
    per (station, time) across the files."""
    status = statuses.get(SOURCE)
    stations = station_metas()
    if status is None or not status.ok or not status.file:
        return NetworkRead(stations, [], False)
    directory = raw_root / status.file
    merged: dict[tuple[str, int], dict[str, float | int | None]] = {}
    unplaced = 0
    for kind, name in FILES.items():
        path = directory / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            LOG.warning("hko: %s: %s; skipping it", name, exc)
            continue
        for station_name, epoch, elements in parse_csv(kind, text):
            code = resolve(kind, station_name)
            if code is None:
                unplaced += 1
                continue
            slot = merged.setdefault((f"{NETWORK}:{code}", epoch), {})
            slot.update({key: value for key, value in elements.items() if value is not None})
    observations = [
        (station_id, epoch, {element: elements.get(element) for element in ELEMENTS})
        for (station_id, epoch), elements in sorted(merged.items())
        if elements
    ]
    status.detail["reports"] = len(observations)
    status.detail["unplaced"] = unplaced
    if unplaced:
        LOG.warning("hko: %d readings name a station the table does not place; they are dropped", unplaced)
    return NetworkRead(stations, observations, True)

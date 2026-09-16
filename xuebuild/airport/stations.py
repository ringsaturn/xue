"""``stations.cache.json`` → :class:`Station`.

The Aviation Weather Center's station table is the product's only source
of a station's name and of its IATA and WMO identifiers; the position and
the elevation are in it too, and are preferred to the ones a report
carries because they do not change from report to report. It is rewritten
once a day, so a round fetches it conditionally (:mod:`.fetch`) and most
rounds read the copy on disk.

A station in a round's METARs that the table does not know still enters the
product, with a null name and the report's own position — the table lags
new stations by a day, and a station without a name is better than a hole
in the map.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Station:
    icao: str
    name: str | None
    lat: float
    lon: float
    elev: float | None
    iata: str | None
    wmo: str | None
    country: str | None
    priority: int | None


def _string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_stations(text: str) -> dict[str, Station]:
    """The table by ICAO id. An entry without an id or a position is
    skipped; a duplicated id keeps the first entry, as the file lists
    them."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"the station table is not JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("the station table must be a list of stations")
    stations: dict[str, Station] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        station = _station(entry)
        if station is not None:
            stations.setdefault(station.icao, station)
    return stations


def _station(entry: dict[str, Any]) -> Station | None:
    icao = _string(entry.get("icaoId")) or _string(entry.get("id"))
    lat = _number(entry.get("lat"))
    lon = _number(entry.get("lon"))
    if icao is None or lat is None or lon is None:
        return None
    priority = entry.get("priority")
    return Station(
        icao=icao.upper(),
        name=_string(entry.get("site")),
        lat=lat,
        lon=lon,
        elev=_number(entry.get("elev")),
        iata=_string(entry.get("iataId")),
        wmo=_string(entry.get("wmoId")),
        country=_string(entry.get("country")),
        priority=priority if isinstance(priority, int) and not isinstance(priority, bool) else None,
    )

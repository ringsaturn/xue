"""FIRMS's active fire CSV → detections in the integer encoding of
``docs/fire.md`` §2. One reader for both instruments: VIIRS and MODIS
differ only in the fire channel's column name and in how confidence is
spelt.

The numbers are read as decimals, not floats, so a coordinate FIRMS writes
with five places lands on the same 1e-4 integer on every machine.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation

from .schema import BT_RANGE, LATITUDE_RANGE, LONGITUDE_RANGE
from .sensors import Source


@dataclass(frozen=True, order=True)
class Detection:
    time: int
    """Epoch seconds, UTC: the granule's acquisition minute."""
    lat: int
    lon: int
    frp: int
    bt: int
    conf: str
    dn: str

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.time, self.lat, self.lon)


@dataclass
class SourceRead:
    detections: dict[str, list[Detection]]
    """Sensor id → its detections in this file."""
    reports: int
    rejected: int


def _scaled(value: str, places: int) -> int:
    return int(Decimal(value.strip()).scaleb(places).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def modis_confidence(percent: int) -> str:
    """FIRMS's own classes for the MODIS percentage."""
    if percent < 30:
        return "l"
    if percent < 80:
        return "n"
    return "h"


def _confidence(value: str, modis: bool) -> str | None:
    value = value.strip()
    if modis:
        try:
            percent = int(value)
        except ValueError:
            return None
        return modis_confidence(percent) if 0 <= percent <= 100 else None
    head = value[:1].lower()
    return head if head in ("l", "n", "h") else None


def _acquired(date: str, time: str) -> int | None:
    time = time.strip().zfill(4)
    try:
        moment = datetime.strptime(f"{date.strip()} {time}", "%Y-%m-%d %H%M").replace(tzinfo=UTC)
    except ValueError:
        return None
    return int(moment.timestamp())


def _within(value: int, bounds: tuple[int, int]) -> bool:
    return bounds[0] <= value <= bounds[1]


def read_csv(text: str, source: Source) -> SourceRead:
    """Every row of one FIRMS file, or a count of why not. A row is
    rejected when its satellite is not one of the source's sensors, or any
    field of §2 does not parse or is out of range."""
    detections: dict[str, list[Detection]] = {sensor: [] for sensor in source.satellites.values()}
    reports = rejected = 0
    fire_channel = "brightness" if source.modis else "bright_ti4"
    for row in csv.DictReader(io.StringIO(text)):
        reports += 1
        sensor = source.satellites.get((row.get("satellite") or "").strip())
        try:
            lat = _scaled(row["latitude"], 4)
            lon = _scaled(row["longitude"], 4)
            frp = _scaled(row["frp"], 1)
            bt = _scaled(row[fire_channel], 1)
        except (KeyError, AttributeError, InvalidOperation, ValueError):
            rejected += 1
            continue
        time = _acquired(row.get("acq_date") or "", row.get("acq_time") or "")
        conf = _confidence(row.get("confidence") or "", source.modis)
        dn = (row.get("daynight") or "").strip()
        if (
            sensor is None
            or time is None
            or conf is None
            or dn not in ("D", "N")
            or not _within(lat, LATITUDE_RANGE)
            or not _within(lon, LONGITUDE_RANGE)
            or frp < 0
            or not _within(bt, BT_RANGE)
        ):
            rejected += 1
            continue
        detections[sensor].append(Detection(time, lat, lon, frp, bt, conf, dn))
    return SourceRead(detections, reports, rejected)

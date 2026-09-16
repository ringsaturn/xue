"""``metars.cache.csv`` → :class:`MetarReport`.

The Aviation Weather Center decodes every METAR it holds — the world's
last ninety minutes — into one CSV row, so this module is a reader, not a
METAR parser: it takes the columns by name, converts them to SI
(:mod:`.units`) and keeps the report verbatim in ``raw``. It imports no
other parser.

Two shapes of the file are in circulation. The cached file served under
``/data/cache/`` starts with the header row; the same data from the API
endpoint is preceded by a short preamble (``No errors``, ``No warnings``,
``… ms``, ``data source=metars``, ``N results``). Both are read: the
header is the first row whose first field is ``raw_text``, and anything
before it is preamble.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .schema import (
    CATEGORIES,
    ELEVATION_RANGE,
    ICAO,
    LATITUDE_RANGE,
    LONGITUDE_RANGE,
    METAR_TYPES,
    PRESSURE_RANGE,
    TEMPERATURE_RANGE,
    WIND_DIRECTION_RANGE,
    WIND_SPEED_RANGE,
)
from .units import altimeter, bounded, cloud_layers, round_to, visibility, wind_speed

HEADER_FIELD = "raw_text"
CLOUD_LAYERS = 4
"""The CSV carries four fixed sky_cover / cloud_base_ft_agl column pairs."""

VARIABLE_WIND = re.compile(r"\bVRB\d{2,3}(?:G\d{2,3})?(?:KT|MPS|KMH)\b")
"""A variable wind direction in the report itself. The service decodes
``VRB04KT`` into ``wind_dir_degrees`` 0, which is a north wind — the one
place the decoded columns lose what the report said, so the direction is
taken back out of ``raw``."""


@dataclass(frozen=True)
class MetarReport:
    """One decoded observation, in SI. ``lat`` / ``lon`` / ``elev`` are the
    station's as the report carries them — the station table is better, and
    the build prefers it, but a station the table does not know still has a
    position this way."""

    icao: str
    time: datetime
    lat: float
    lon: float
    elev: float | None
    t: float | None
    td: float | None
    wd: int | None
    ws: float | None
    gust: float | None
    vis: int | None
    qnh: float | None
    slp: float | None
    wx: str | None
    cloud: list[list[Any]]
    category: str | None
    auto: bool
    type: str
    raw: str

    def to_json(self) -> dict[str, Any]:
        """The report as a shard writes it. The station's identity and
        position are on the station, not repeated on every report."""
        return {
            "time": self.time.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "raw": self.raw,
            "t": self.t,
            "td": self.td,
            "wd": self.wd,
            "ws": self.ws,
            "gust": self.gust,
            "vis": self.vis,
            "qnh": self.qnh,
            "slp": self.slp,
            "wx": self.wx,
            "cloud": self.cloud,
            "category": self.category,
            "auto": self.auto,
            "type": self.type,
        }


def _columns(header: list[str]) -> dict[str, list[int]]:
    """Column name → its positions. Names repeat: the four cloud layers
    are four ``sky_cover`` / ``cloud_base_ft_agl`` pairs."""
    positions: dict[str, list[int]] = {}
    for index, name in enumerate(header):
        positions.setdefault(name.strip(), []).append(index)
    return positions


def _text(row: list[str], index: int | None) -> str | None:
    if index is None or index >= len(row):
        return None
    value = row[index].strip()
    # The AWC writes an absent flight category as the four letters "null".
    return None if value in ("", "null") else value


def _float(row: list[str], index: int | None) -> float | None:
    value = _text(row, index)
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(row: list[str], index: int | None) -> int | None:
    value = _float(row, index)
    return None if value is None else int(round(value))


def _optional_int(value: float | None) -> int | None:
    """``bounded`` answers a float; a degree is an integer."""
    return None if value is None else int(value)


def _flag(row: list[str], index: int | None) -> bool:
    value = _text(row, index)
    return value is not None and value.upper() == "TRUE"


def _first(positions: dict[str, list[int]], name: str) -> int | None:
    found = positions.get(name)
    return found[0] if found else None


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC).replace(microsecond=0)


def parse_metars(text: str) -> list[MetarReport]:
    """Every row the file decodes into a report, in file order. A row
    without a station id, a time or a position is skipped: it is a station
    the product cannot place, not a reason to fail the round."""
    rows = csv.reader(text.splitlines())
    positions: dict[str, list[int]] | None = None
    reports: list[MetarReport] = []
    for row in rows:
        if not row:
            continue
        if positions is None:
            if row[0].strip() == HEADER_FIELD:
                positions = _columns(row)
            continue  # preamble, or the header itself
        report = _report(row, positions)
        if report is not None:
            reports.append(report)
    if positions is None:
        raise ValueError("the METAR cache has no header row")
    return reports


def _report(row: list[str], positions: dict[str, list[int]]) -> MetarReport | None:
    icao = _text(row, _first(positions, "station_id"))
    time = _time(_text(row, _first(positions, "observation_time")))
    lat = bounded(_float(row, _first(positions, "latitude")), LATITUDE_RANGE)
    lon = bounded(_float(row, _first(positions, "longitude")), LONGITUDE_RANGE)
    raw = _text(row, _first(positions, "raw_text"))
    # A row the product cannot place, or cannot name, is skipped: some
    # stations report -99.99 for a position the service does not have.
    if icao is None or time is None or lat is None or lon is None or raw is None:
        return None
    icao = icao.upper()
    if not ICAO.match(icao):
        return None
    covers = positions.get("sky_cover", [])
    bases = positions.get("cloud_base_ft_agl", [])
    layers: list[tuple[str, float | None]] = []
    for layer in range(min(CLOUD_LAYERS, len(covers))):
        cover = _text(row, covers[layer])
        if cover is None:
            continue
        layers.append((cover.upper(), _float(row, bases[layer]) if layer < len(bases) else None))
    wind_direction = _optional_int(bounded(_int(row, _first(positions, "wind_dir_degrees")), WIND_DIRECTION_RANGE))
    if VARIABLE_WIND.search(raw):
        wind_direction = None  # VRB is decoded as 0, which would read as north
    # The contract admits the two kinds and the four categories the
    # service publishes; anything else is read as the generic kind and as
    # no category rather than costing the round. `raw` keeps the truth.
    metar_type = (_text(row, _first(positions, "metar_type")) or "METAR").upper()
    if metar_type not in METAR_TYPES:
        metar_type = "METAR"
    category = _text(row, _first(positions, "flight_category"))
    return MetarReport(
        icao=icao,
        time=time,
        lat=lat,
        lon=lon,
        elev=bounded(_float(row, _first(positions, "elevation_m")), ELEVATION_RANGE),
        t=bounded(round_to(_float(row, _first(positions, "temp_c")), 1), TEMPERATURE_RANGE),
        td=bounded(round_to(_float(row, _first(positions, "dewpoint_c")), 1), TEMPERATURE_RANGE),
        wd=wind_direction,
        ws=bounded(wind_speed(_float(row, _first(positions, "wind_speed_kt"))), WIND_SPEED_RANGE),
        gust=bounded(wind_speed(_float(row, _first(positions, "wind_gust_kt"))), WIND_SPEED_RANGE),
        vis=visibility(_text(row, _first(positions, "visibility_statute_mi"))),
        qnh=bounded(altimeter(_float(row, _first(positions, "altim_in_hg"))), PRESSURE_RANGE),
        slp=bounded(round_to(_float(row, _first(positions, "sea_level_pressure_mb")), 1), PRESSURE_RANGE),
        wx=_text(row, _first(positions, "wx_string")),
        cloud=cloud_layers(layers),
        category=category if category in CATEGORIES else None,
        auto=_flag(row, _first(positions, "auto")),
        type=metar_type,
        raw=raw,
    )

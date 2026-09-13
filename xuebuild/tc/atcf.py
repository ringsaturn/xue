"""ATCF text: the a-deck (aids — official and objective forecasts), the
b-deck (best track) and NCEP's ``trackatcfunix`` tracker output, which is
an a-deck by another name. One parser for all three (S3, S4, S6 of the
design).

A line is comma-separated positional fields (NRL ``abdeck.txt``)::

    BASIN, CY, YYYYMMDDHH, TECHNUM, TECH, TAU, LAT, LON, VMAX, MSLP, TY,
    RAD, WINDCODE, RAD1, RAD2, RAD3, RAD4, POUTER, ROUTER, RMW, GUSTS, EYE,
    SUBREGION, MAXSEAS, INITIALS, DIR, SPEED, STORMNAME, DEPTH, SEAS, …

Latitude and longitude are tenths of a degree with a hemisphere letter,
winds knots, pressures hPa, radii nautical miles. A forecast lead with wind
radii is three lines (RAD 34, 50, 64), merged here into one point. Zero
and negative numbers are "not reported" everywhere except the radii,
where 0 nm is a real "no such wind in that quadrant".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .registry import INVEST_MIN, KNOT, NAUTICAL_MILE, NUMBERED_MAX
from .track import Forecast, Point, Radii, Track, wrap_longitude

_FIELD_COUNT_MIN = 9


@dataclass(frozen=True)
class AtcfRecord:
    basin: str
    number: int
    time: datetime
    tech: str
    tau: int
    lat: float
    lon: float
    vmax_kt: int | None
    mslp: int | None
    cls: str | None
    rad: int | None
    windcode: str | None
    radii_nm: tuple[int, int, int, int] | None
    rmw_nm: int | None
    gust_kt: int | None
    name: str | None

    @property
    def storm_id(self) -> str:
        """``EP142026``: basin, two-digit number, the season from the
        record's own year."""
        return f"{self.basin}{self.number:02d}{self.time.year}"

    @property
    def short_id(self) -> str:
        """``EP14`` — what an invest number is reused under."""
        return f"{self.basin}{self.number:02d}"

    @property
    def numbered(self) -> bool:
        return self.number <= NUMBERED_MAX

    @property
    def invest(self) -> bool:
        return self.number >= INVEST_MIN


def _int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _positive(value: str) -> int | None:
    parsed = _int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _coordinate(value: str, positive: str, negative: str) -> float | None:
    value = value.strip()
    if len(value) < 2 or value[-1] not in (positive, negative):
        return None
    digits = _int(value[:-1])
    if digits is None:
        return None
    degrees = digits / 10.0
    return degrees if value[-1] == positive else -degrees


def parse_line(line: str) -> AtcfRecord | None:
    """One record, or None for a blank, a comment or a line too short to
    place a storm."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    fields = [field.strip() for field in stripped.split(",")]
    if len(fields) < _FIELD_COUNT_MIN:
        return None
    basin = fields[0].upper()
    number = _int(fields[1])
    if len(basin) != 2 or not basin.isalpha() or number is None or number < 0:
        return None
    try:
        time = datetime.strptime(fields[2], "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None
    tech = fields[4].upper()
    tau = _int(fields[5])
    lat = _coordinate(fields[6], "N", "S")
    lon = _coordinate(fields[7], "E", "W")
    if not tech or tau is None or lat is None or lon is None:
        return None

    def at(index: int) -> str:
        return fields[index] if index < len(fields) else ""

    rad = _positive(at(11))
    windcode = at(12).upper() or None
    radii_nm: tuple[int, int, int, int] | None = None
    if rad is not None:
        quadrants = [_int(at(index)) for index in range(13, 17)]
        if None not in quadrants:
            ne, se, sw, nw = (max(0, value) for value in quadrants if value is not None)
            radii_nm = (ne, ne, ne, ne) if windcode == "AAA" else (ne, se, sw, nw)
    cls = at(10).upper() or None
    if cls in ("XX", ""):
        cls = None
    name = at(27).upper() or None
    if name in ("", "INVEST", "UNNAMED", "UNKNOWN"):
        name = None
    return AtcfRecord(
        basin=basin,
        number=number,
        time=time,
        tech=tech,
        tau=tau,
        lat=lat,
        lon=wrap_longitude(lon),
        vmax_kt=_positive(at(8)),
        mslp=_positive(at(9)),
        cls=cls,
        rad=rad,
        windcode=windcode,
        radii_nm=radii_nm,
        rmw_nm=_positive(at(19)),
        gust_kt=_positive(at(20)),
        name=name,
    )


def parse_atcf(text: str) -> list[AtcfRecord]:
    records = []
    for line in text.splitlines():
        record = parse_line(line)
        if record is not None:
            records.append(record)
    return records


def _kt(value: int | None) -> float | None:
    return None if value is None else round(value * KNOT, 1)


def _nm(value: int | None) -> float | None:
    return None if value is None else round(value * NAUTICAL_MILE, 1)


def _merge_points(records: list[AtcfRecord], base: datetime) -> tuple[Point, ...]:
    """The records of one forecast (or one best track), one point per
    time, radii lines folded together. The first line of a lead carries
    the scalars; later lines only add their threshold."""
    by_time: dict[datetime, tuple[AtcfRecord, Radii]] = {}
    for record in sorted(records, key=lambda item: (item.tau, item.rad or 0)):
        time = base + timedelta(hours=record.tau) if record.tech != "BEST" else record.time
        if time not in by_time:
            by_time[time] = (record, {})
        head, radii = by_time[time]
        if record.rad is not None and record.radii_nm is not None and str(record.rad) in ("34", "50", "64"):
            radii[str(record.rad)] = [_nm(value) for value in record.radii_nm]
    points = []
    for time in sorted(by_time):
        head, radii = by_time[time]
        points.append(
            Point(
                time=time,
                lat=head.lat,
                lon=head.lon,
                vmax=_kt(head.vmax_kt),
                pmin=float(head.mslp) if head.mslp is not None else None,
                radii=radii or None,
                cls=head.cls,
                rmw=_nm(head.rmw_nm),
                gust=_kt(head.gust_kt),
            )
        )
    return tuple(points)


def storm_ids(records: list[AtcfRecord]) -> list[str]:
    return list(dict.fromkeys(record.storm_id for record in records))


def forecasts(records: list[AtcfRecord], tech: str, *, storm_id: str | None = None) -> dict[str, Forecast]:
    """The newest forecast of ``tech`` for each storm (or the one storm),
    keyed by storm id. An a-deck is a season's accumulation; only the
    latest base time of each storm is a current forecast."""
    tech = tech.upper()
    selected: dict[str, dict[datetime, list[AtcfRecord]]] = {}
    for record in records:
        if record.tech != tech or (storm_id is not None and record.storm_id != storm_id):
            continue
        selected.setdefault(record.storm_id, {}).setdefault(record.time, []).append(record)
    result = {}
    for sid, by_base in selected.items():
        base = max(by_base)
        result[sid] = Forecast(base=base, points=_merge_points(by_base[base], base), run=base.strftime("%Y%m%d%H"))
    return result


def best_track(records: list[AtcfRecord], storm_id: str) -> Track | None:
    selected = [record for record in records if record.tech == "BEST" and record.storm_id == storm_id]
    if not selected:
        return None
    return Track(points=_merge_points(selected, selected[0].time))


def storm_name(records: list[AtcfRecord], storm_id: str) -> str | None:
    """The name the newest line naming the storm carries (b-decks name
    every line; a-deck aids leave the field blank)."""
    named = [record for record in records if record.storm_id == storm_id and record.name]
    if not named:
        return None
    return max(named, key=lambda record: record.time).name

"""IBTrACS v04 CSV (S1): the agencies' best tracks side by side, one row
per storm and time, a column set per agency (``USA_``, ``TOKYO_``,
``CMA_``…). The first row is the header, the second the units row —
knots, hPa, nautical miles — which is skipped.

Only analyses: IBTrACS holds no forecast. What it gives the product is the
multi-agency past of a live storm (``best.agencies``), the ``SID`` that
stays stable across renumbering and the ``USA_ATCF_ID`` that ties a row to
everything else.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .registry import AGENCIES, KNOT, NAUTICAL_MILE
from .track import Point, Radii, Track, wrap_longitude

# Column prefix → (class column suffix, wind-radii columns). The USA set
# carries quadrant radii for the three thresholds; TOKYO and KMA carry
# an ellipse (a direction, a long and a short radius) for 50 kt and
# 30 kt, kept as ``ellipse`` on the point's extra fields rather than
# squeezed into the quadrant shape.
_CLASS_COLUMN = {
    "USA": "STATUS",
    "TOKYO": "GRADE",
    "CMA": "CAT",
    "HKO": "CAT",
    "KMA": "CAT",
    "NEWDELHI": "GRADE",
    "REUNION": "TYPE",
    "BOM": "TYPE",
    "NADI": "CAT",
    "WELLINGTON": None,
}
_QUADRANT_PREFIXES = ("USA", "REUNION", "BOM")

GUST_NOT_REPORTED = 999.0
"""``REUNION_GUST`` writes this for "not reported"; 999 is a real pressure, so
only the gust columns read it this way."""

MERGED_KEY = "ibtracs"
"""The ``best`` key of IBTrACS' own merged position (``LAT``, ``LON``,
``WMO_WIND``, ``WMO_PRES``, ``NATURE``), for a storm no agency column holds:
most of those before 1950 come only from the historical compilations."""

_SID = re.compile(r"^\d{7}[NS]\d{5}$")
# IBTrACS' basin of a storm's first row → the ATCF basin it files under.
# ATCF's one southern hemisphere basin takes the south Atlantic's few storms.
_ATCF_BASINS = {"NA": "AL", "EP": "EP", "WP": "WP", "NI": "IO", "SI": "SH", "SP": "SH", "SA": "SH"}


@dataclass
class IbtracsStorm:
    sid: str
    season: int
    basin: str
    name: str | None
    atcf_id: str | None
    """The newest ``USA_ATCF_ID`` on the storm (it changes when a system
    crosses into another basin's numbering)."""
    atcf_ids: list[str] = field(default_factory=list)
    provisional: bool = False
    spur: bool = False
    """``TRACK_TYPE`` names a branch of another storm's track, not a storm."""
    agencies: dict[str, Track] = field(default_factory=dict)
    """Keyed by the registry agency id (``nhc`` is under ``usa``: the USA
    columns are NHC's and JTWC's joint best track)."""


def _float(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _magnitude(value: str) -> float | None:
    """A wind, pressure or radius: some agencies' columns (MetService's)
    write a negative number for "not reported"."""
    parsed = _float(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _agency_key(prefix: str) -> str:
    if prefix == "USA":
        return "usa"
    for spec in AGENCIES.values():
        if spec.ibtracs == prefix:
            return spec.id
    return prefix.lower()


def history_id(storm: IbtracsStorm) -> str | None:
    """``WP-1951123N10140-1951``: the archive's id of a storm that never had
    an ATCF number, the two ends read as an ATCF id's are (basin, season).
    None for a ``SID`` or basin outside the shapes IBTrACS documents."""
    basin = _ATCF_BASINS.get(storm.basin)
    if basin is None or not _SID.match(storm.sid):
        return None
    return f"{basin}-{storm.sid}-{storm.season:04d}"


def parse_ibtracs(
    text: str, *, seasons: Collection[int] | None = None, merged: bool = False
) -> dict[str, IbtracsStorm]:
    """Every storm in the file, keyed by ``SID``. ``seasons`` keeps only
    those of the given ``SEASON``s (the whole file is hundreds of megabytes);
    ``merged`` gives a storm no agency holds its :data:`MERGED_KEY` track."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
        next(reader)  # units
    except StopIteration as exc:
        raise ValueError("ibtracs: no header rows") from exc
    index = {name.strip(): position for position, name in enumerate(header)}
    prefixes = [prefix for prefix in _CLASS_COLUMN if f"{prefix}_LAT" in index]
    storms: dict[str, IbtracsStorm] = {}
    points: dict[tuple[str, str], list[Point]] = {}
    merged_points: dict[str, list[Point]] = {}

    def cell(row: list[str], name: str) -> str:
        position = index.get(name)
        return row[position].strip() if position is not None and position < len(row) else ""

    for row in reader:
        sid = cell(row, "SID")
        if not sid:
            continue
        if seasons is not None and int(cell(row, "SEASON") or 0) not in seasons:
            continue
        try:
            time = datetime.strptime(cell(row, "ISO_TIME"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        storm = storms.get(sid)
        if storm is None:
            name = cell(row, "NAME").upper()
            storm = IbtracsStorm(
                sid=sid,
                season=int(cell(row, "SEASON") or 0),
                basin=cell(row, "BASIN").upper(),
                name=None if name in ("", "UNNAMED", "NOT_NAMED") else name,
                atcf_id=None,
            )
            storms[sid] = storm
        atcf_id = cell(row, "USA_ATCF_ID").upper()
        if atcf_id:
            storm.atcf_id = atcf_id
            if atcf_id not in storm.atcf_ids:
                storm.atcf_ids.append(atcf_id)
        track_type = cell(row, "TRACK_TYPE").upper()
        if track_type == "PROVISIONAL":
            storm.provisional = True
        if track_type.startswith("SPUR"):
            storm.spur = True
        if merged:
            lat = _float(cell(row, "LAT"))
            lon = _float(cell(row, "LON"))
            if lat is not None and lon is not None:
                wind = _magnitude(cell(row, "WMO_WIND"))
                merged_points.setdefault(sid, []).append(
                    Point(
                        time=time,
                        lat=lat,
                        lon=wrap_longitude(lon),
                        vmax=None if wind is None else round(wind * KNOT, 1),
                        pmin=_magnitude(cell(row, "WMO_PRES")),
                        cls=cell(row, "NATURE") or None,
                    )
                )
        for prefix in prefixes:
            lat = _float(cell(row, f"{prefix}_LAT"))
            lon = _float(cell(row, f"{prefix}_LON"))
            if lat is None or lon is None:
                continue
            radii: Radii = {}
            if prefix in _QUADRANT_PREFIXES:
                for threshold in ("34", "50", "64"):
                    quadrants = [_magnitude(cell(row, f"{prefix}_R{threshold}_{q}")) for q in ("NE", "SE", "SW", "NW")]
                    if any(value is not None for value in quadrants):
                        radii[threshold] = [None if v is None else round(v * NAUTICAL_MILE, 1) for v in quadrants]
            wind = _magnitude(cell(row, f"{prefix}_WIND"))
            pressure = _magnitude(cell(row, f"{prefix}_PRES"))
            class_column = _CLASS_COLUMN[prefix]
            cls = cell(row, f"{prefix}_{class_column}") if class_column else ""
            rmw = _magnitude(cell(row, f"{prefix}_RMW")) if f"{prefix}_RMW" in index else None
            gust = _magnitude(cell(row, f"{prefix}_GUST")) if f"{prefix}_GUST" in index else None
            if gust == GUST_NOT_REPORTED:
                gust = None
            points.setdefault((sid, prefix), []).append(
                Point(
                    time=time,
                    lat=lat,
                    lon=wrap_longitude(lon),
                    vmax=None if wind is None else round(wind * KNOT, 1),
                    pmin=pressure,
                    radii=radii or None,
                    cls=cls or None,
                    rmw=None if rmw is None else round(rmw * NAUTICAL_MILE, 1),
                    gust=None if gust is None else round(gust * KNOT, 1),
                )
            )
    for (sid, prefix), track_points in points.items():
        storm = storms[sid]
        storm.agencies[_agency_key(prefix)] = Track(
            points=tuple(sorted(track_points, key=lambda point: point.time)), provisional=storm.provisional
        )
    for sid, track_points in merged_points.items():
        storm = storms[sid]
        if not storm.agencies:
            storm.agencies[MERGED_KEY] = Track(
                points=tuple(sorted(track_points, key=lambda point: point.time)), provisional=storm.provisional
            )
    return storms


def by_atcf_id(storms: dict[str, IbtracsStorm]) -> dict[str, IbtracsStorm]:
    """Storms keyed by every ATCF id they have carried."""
    result = {}
    for storm in storms.values():
        for atcf_id in storm.atcf_ids:
            result[atcf_id] = storm
    return result

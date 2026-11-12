"""The Hong Kong Observatory's tropical cyclone track information
(DATA.GOV.HK dataset ``hk-hko-rss-tc-track-info``): ``tc_list.xml`` names
the systems the Observatory is issuing forecast tracks for, and each has a
``hko_tctrack_<TCID>.xml``::

    <TropicalCycloneTrack tcid="2206">
      <BulletinHeader> … <BulletinTime>2022-07-02T08:00:18+08:00</BulletinTime> …
      <WeatherReport>
        <TropicalCycloneName>CHABA</TropicalCycloneName>
        <PastInformation>      Index, Intensity, MaximumWind, Time, Latitude, Longitude
        <AnalysisInformation>  Intensity, MaximumWind, Time, Latitude, Longitude
        <ForecastInformation>  Index, then either Latitude/Longitude alone or
                               all five of the above

``TCID`` is the Observatory's own four-digit number (season and sequence),
not an ATCF id. Winds are ten-minute means in km/h (``145km/h``, ``--`` for
an extratropical low), positions ``16.70N`` / ``121.10E``, times ISO 8601
with an offset. A forecast entry without ``Time`` is an interpolated
position the Observatory's own map draws its track through
(``HKO_TCTrackInfo_DataDictionary_en.pdf``), not a forecast, and is
skipped. No pressure and no wind radii are given.

Attribution required by the terms of use: "Hong Kong Observatory
(DATA.GOV.HK)".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.etree import ElementTree

from .track import Forecast, Point, Track, wrap_longitude

TCID = re.compile(r"^\d{4}$")

# The Observatory's six tropical cyclone categories, written as the
# abbreviations IBTrACS carries in ``HKO_CAT`` so a working track and the
# IBTrACS one under the same ``best.hko`` key speak one vocabulary. "Low
# Pressure Area" and "Extratropical Low" have no such code and are kept
# verbatim.
CLASSES = {
    "Tropical Depression": "TD",
    "Tropical Storm": "TS",
    "Severe Tropical Storm": "STS",
    "Typhoon": "T",
    "Severe Typhoon": "ST",
    "Super Typhoon": "SuperT",
}
CATEGORY_NAMES = frozenset(CLASSES) | {"Low Pressure Area", "Extratropical Low"}
"""What the feed writes in place of a name before a system is named."""

KMH = 1 / 3.6
"""m/s per km/h."""

_WIND = re.compile(r"^(\d+(?:\.\d+)?)\s*km/h$", re.IGNORECASE)
_COORD = re.compile(r"^(\d+(?:\.\d+)?)\s*([NSEW])$", re.IGNORECASE)


@dataclass(frozen=True)
class ListEntry:
    tcid: str
    name: str | None


@dataclass(frozen=True)
class HkoTrack:
    tcid: str
    name: str | None
    """None while the feed gives a category in place of a name."""
    issued: datetime
    forecast: Forecast | None
    """From the analysis (lead 0) through the timed forecast positions;
    None without an analysis or without a position ahead of it."""
    history: Track
    """The past positions and the analysis: the Observatory's working best
    track, provisional by nature."""


def _text(element: ElementTree.Element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"hko: time without an offset {value!r}")
    return parsed.astimezone(UTC)


def _coordinate(value: str | None, positive: str) -> float | None:
    if value is None:
        return None
    match = _COORD.match(value)
    if not match:
        raise ValueError(f"hko: unrecognised coordinate {value!r}")
    degrees = float(match.group(1))
    return degrees if match.group(2).upper() == positive else -degrees


def _wind(value: str | None) -> float | None:
    if value is None:
        return None
    match = _WIND.match(value)
    return None if match is None else round(float(match.group(1)) * KMH, 1)


def _class(value: str | None) -> str | None:
    if value is None:
        return None
    return CLASSES.get(value, value)


def _point(element: ElementTree.Element) -> Point | None:
    """A timed position, or None for an interpolated one."""
    time = _time(_text(element, "Time"))
    lat = _coordinate(_text(element, "Latitude"), "N")
    lon = _coordinate(_text(element, "Longitude"), "E")
    if time is None or lat is None or lon is None:
        return None
    return Point(
        time=time,
        lat=lat,
        lon=wrap_longitude(lon),
        vmax=_wind(_text(element, "MaximumWind")),
        cls=_class(_text(element, "Intensity")),
    )


def _name(value: str | None) -> str | None:
    if value is None:
        return None
    if value in CATEGORY_NAMES or value.title() in CATEGORY_NAMES:
        return None
    return value.upper()


def _root(payload: bytes) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise ValueError(f"hko: not well-formed XML: {exc}") from exc


def parse_list(payload: bytes) -> list[ListEntry]:
    root = _root(payload)
    if root.tag != "TropicalCycloneList":
        raise ValueError(f"hko: expected TropicalCycloneList, got {root.tag!r}")
    entries = []
    for storm in root.findall("TropicalCyclone"):
        tcid = _text(storm, "TropicalCycloneID")
        if tcid is None or not TCID.match(tcid):
            continue
        entries.append(ListEntry(tcid, _name(_text(storm, "TropicalCycloneEnglishName"))))
    return entries


def parse_track(payload: bytes) -> HkoTrack:
    root = _root(payload)
    if root.tag != "TropicalCycloneTrack":
        raise ValueError(f"hko: expected TropicalCycloneTrack, got {root.tag!r}")
    tcid = (root.get("tcid") or "").strip()
    if not TCID.match(tcid):
        raise ValueError(f"hko: track without a four-digit tcid ({tcid!r})")
    issued = _time(_text(root, "BulletinHeader/BulletinTime"))
    if issued is None:
        raise ValueError("hko: track without a BulletinTime")
    report = root.find("WeatherReport")
    if report is None:
        raise ValueError("hko: track without a WeatherReport")

    # A time listed twice keeps its last entry; the analysis is the newest.
    history: dict[datetime, Point] = {}
    for element in report.findall("PastInformation"):
        point = _point(element)
        if point is not None:
            history[point.time] = point
    analysis_element = report.find("AnalysisInformation")
    analysis = None if analysis_element is None else _point(analysis_element)
    if analysis is not None:
        history = {time: point for time, point in history.items() if time < analysis.time}
        history[analysis.time] = analysis

    forecast = None
    if analysis is not None:
        points = {analysis.time: analysis}
        for element in report.findall("ForecastInformation"):
            point = _point(element)
            if point is not None and point.time > analysis.time:
                points[point.time] = point
        # The last bulletin on a system that has weakened or turned
        # extratropical carries an analysis and nothing ahead of it.
        if len(points) > 1:
            forecast = Forecast(base=analysis.time, points=tuple(points[t] for t in sorted(points)), issued=issued)

    return HkoTrack(
        tcid=tcid,
        name=_name(_text(report, "TropicalCycloneName")),
        issued=issued,
        forecast=forecast,
        history=Track(points=tuple(history[t] for t in sorted(history)), provisional=True, source="hko"),
    )

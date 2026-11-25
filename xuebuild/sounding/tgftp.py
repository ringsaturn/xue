"""TEMP as text (FM 35) from NOAA's ``tgftp``, the ``nws-tgftp`` source.

Some centres' ascents are in the WIS2 cache in neither form: Canada's node
publishes no TEMP topic, and India's sends a part of its stations. Their
TAC bulletins are still on the GTS, and the US National Weather Service's
telecommunication gateway puts the newest of each heading on
``https://tgftp.nws.noaa.gov/data/raw/`` as one small text file, the four
parts of a TEMP under four directories: ``us/`` part A, ``uk/`` B, ``ul/``
C and ``ue/`` D.

A file is named by its heading and is overwritten by the next bulletin
under it, so it is read every hour and is never taken from the previous
issue's download: the same name is another ascent twelve hours later.

The text is decoded into the BUFR keys :func:`.bufr.assemble` reads, one
:class:`.bufr.Sounding` per part, and the parts are merged by the rule
every other part is (:func:`.bufr.merge_parts`). Two things the text does
not hold:

* a position. A fixed land station's report carries its WMO number alone,
  so the position comes from ``positions.json``, and a station the table
  lacks is dropped and counted.
* a launch time. The report's clock is its hour, which is the nominal hour
  at most centres and the launch hour at some Indian ones (23 for the 00Z
  ascent), so it files the ascent and is not published as ``launched``. A
  clock later than the bulletin's arrival is a mistyped day, and the
  heading's hour files that one.

The bulletins are typed or relayed by hand along the way, and a group lost
from one shifts the rest. A part is therefore read up to the first level
its own ordering refuses (a height below the one under it, a pressure
above) and no further.
"""

from __future__ import annotations

import functools
import html
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import bufr

SOURCE_ID = "nws-tgftp"

BASE_URL = "https://tgftp.nws.noaa.gov/data/raw/"

PART_DIRECTORIES = ("us", "uk", "ul", "ue")
"""Parts A, B, C and D."""

COUNTRIES = ("cn", "in")
"""The countries read, as the heading's two-letter designators: Canada and
India, whose stations the BUFR sources lack."""

LOOKBACK_HOURS = 48
"""How far back a listing with no watermark is taken: the build's
staleness limit."""

FILE_NAME = re.compile(
    rf"^(?:{'|'.join(PART_DIRECTORIES)})(?:{'|'.join(COUNTRIES)})\d{{2}}\.[a-z]{{4}}\.[a-z]*\.txt$"
)

_ROW = re.compile(
    r"<a\s+href=\"[^\"]*\">(?P<name>[^<]+)</a>\s*</td>\s*<td[^>]*>\s*"
    r"(?P<modified>\d{2}-[A-Z][a-z]{2}-\d{4} \d{2}:\d{2})\s*</td>"
)

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_HEADING = re.compile(r"^(U[SKLE][A-Z]{2}\d{2} [A-Z]{4} (\d{2})(\d{2})(\d{2})(?: [A-Z]{3})?)\s*$", re.MULTILINE)
_REPORT = re.compile(r"\bTT(?P<part>AA|BB|CC|DD)\b(?P<body>.*?)(?==|\bTT(?:AA|BB|CC|DD)\b|\Z)", re.DOTALL)
_GROUP = re.compile(r"^[0-9/]{5}$")

SECTION_ENDS = (
    "31313", "41414", "51515", "52525", "53535", "54545", "56565", "57575", "58585", "59595", "61616", "62626"
)
"""Indicators of the sections after the levels: the sounding system, cloud,
and the regional and national ones. They end the levels only where a
level's own group would stand: 31313 is a temperature too."""

MIN_TEMPERATURES = 4
"""Temperatures a part A needs to be an ascent. A station whose flight
failed still sends the whole form, slashes for every level it missed."""

KNOT = 0.514444
"""Metres per second."""

STANDARD = {
    "AA": (
        ("00", 100000),
        ("92", 92500),
        ("85", 85000),
        ("70", 70000),
        ("50", 50000),
        ("40", 40000),
        ("30", 30000),
        ("25", 25000),
        ("20", 20000),
        ("15", 15000),
        ("10", 10000),
    ),
    "CC": (("70", 7000), ("50", 5000), ("30", 3000), ("20", 2000), ("10", 1000)),
}

POSITIONS_PATH = Path(__file__).with_name("positions.json")


@functools.cache
def positions() -> dict[str, tuple[float, float, float | None]]:
    """``(lat, lon, elevation)`` by WMO number, checked in like the names
    (``docs/sounding.md`` §3)."""
    table = json.loads(POSITIONS_PATH.read_text(encoding="utf-8"))
    return {str(wmo): (float(lat), float(lon), elev) for wmo, (lat, lon, elev) in table["positions"].items()}


@dataclass(frozen=True)
class ListedFile:
    key: str
    """The path under :data:`BASE_URL`, ``us/uscn01.cyev..txt``."""
    last_modified: datetime


def parse_listing(page: str, prefix: str) -> list[ListedFile]:
    """The files of one part's directory index under the headings read.
    The index gives times to the minute, and sizes too rounded to use."""
    files: list[ListedFile] = []
    for match in _ROW.finditer(page):
        name = html.unescape(match.group("name")).strip()
        if FILE_NAME.match(name) is None:
            continue
        day, month, rest = match.group("modified").split("-")
        year, clock = rest.split(" ")
        try:
            modified = datetime(
                int(year), _MONTHS.index(month) + 1, int(day), int(clock[:2]), int(clock[3:]), tzinfo=UTC
            )
        except ValueError:
            continue
        files.append(ListedFile(key=prefix + name, last_modified=modified))
    return files


def list_files(*, get: Callable[[str], bytes], base_url: str = BASE_URL) -> list[ListedFile]:
    files: list[ListedFile] = []
    for part in PART_DIRECTORIES:
        files.extend(parse_listing(get(f"{base_url}{part}/").decode("utf-8", errors="replace"), f"{part}/"))
    return files


@dataclass(frozen=True)
class FileResult:
    soundings: tuple[bufr.Sounding, ...]
    reports: int
    dropped: int
    """Reports of a station with no known position, or with too few levels
    holding anything."""


def parse_file(data: bytes, *, name: str, arrived: datetime) -> FileResult:
    """The parts in one file. A file without a TEMP report in it (a NIL
    bulletin) is no reports."""
    # Stray bytes outside ASCII stand where a space was.
    text = data.decode("ascii", errors="replace").replace("\ufffd", " ")
    heading = _HEADING.search(text.replace("\r", ""))
    arrived = arrived.astimezone(UTC)
    bulletin = bufr.BulletinName(
        header=heading.group(1) if heading is not None else name,
        nominal=None
        if heading is None
        else bufr.nominal_from_day(*(int(value) for value in heading.group(2, 3, 4)), arrived),
        arrived=arrived,
        correction=None,
    )
    soundings: list[bufr.Sounding] = []
    reports = dropped = 0
    for match in _REPORT.finditer(text):
        # A relay that loses a space runs two groups together.
        groups = [
            word[start : start + 5]
            for word in match.group("body").split()
            for start in (range(0, len(word), 5) if len(word) % 5 == 0 else (0,))
        ]
        if len(groups) < 3 or "NIL" in groups:
            continue
        reports += 1
        sounding = _report(match.group("part"), groups, bulletin)
        if sounding is None:
            dropped += 1
            continue
        soundings.append(sounding)
    return FileResult(tuple(soundings), reports, dropped)


def _report(part: str, groups: list[str], bulletin: bufr.BulletinName) -> bufr.Sounding | None:
    stamp, wmo = groups[0], groups[1]
    if not re.fullmatch(r"\d{4}[0-9/]", stamp) or not re.fullmatch(r"\d{5}", wmo):
        return None
    day, hour = int(stamp[:2]), int(stamp[2:4])
    # The day has 50 added to it when the winds are in knots.
    unit = 1.0
    if day > 50:
        day, unit = day - 50, KNOT
    clock = bufr.nominal_from_day(day, hour, 0, bulletin.arrived)
    if clock is not None and clock > bulletin.arrived:
        clock = bulletin.nominal
    position = positions().get(wmo)
    if clock is None or position is None:
        return None
    lat, lon, elev = position
    body = groups[2:]
    if part in STANDARD:
        rows = standard_levels(part, stamp[4], body, unit=unit, elev=elev)
    else:
        rows = significant_levels(part, body, unit=unit)
    rows = [row for row in rows if any(row.get(key) is not None for key in _MEASURED)]
    if len(rows) < 2 or (
        part == "AA" and sum(row["airTemperature"] is not None for row in rows) < MIN_TEMPERATURES
    ):
        return None
    return bufr.assemble(
        rows,
        key=wmo,
        wigos=None,
        wmo=wmo,
        time=bufr.synoptic_hour(clock),
        launched=None,
        lat=lat,
        lon=lon,
        elev=elev,
        sonde=None,
        name=bulletin,
        gateway=SOURCE_ID,
    )


_MEASURED = ("nonCoordinateGeopotentialHeight", "airTemperature", "dewpointTemperature", "windDirection")


def _row(
    pressure: int,
    sig: int,
    *,
    z: float | None = None,
    temperature: str | None = None,
    wind: str | None = None,
    unit: float = 1.0,
) -> dict[str, Any]:
    t, td = _temperature(temperature)
    direction, speed = _wind(wind, unit)
    return {
        "pressure": pressure,
        "extendedVerticalSoundingSignificance": sig,
        "nonCoordinateGeopotentialHeight": z,
        "airTemperature": t,
        "dewpointTemperature": td,
        "windDirection": direction,
        "windSpeed": speed,
    }


def _temperature(group: str | None) -> tuple[float | None, float | None]:
    """``TTTDD``, kelvin. The tenth's parity is the sign, and the dew-point
    depression is in tenths up to 5 °C and in whole degrees, 50 added,
    from 6 °C. 51 to 55 are not used."""
    if group is None or not _GROUP.match(group) or not group[:3].isdigit():
        return None, None
    tenths = int(group[:3])
    if tenths % 2:
        tenths = -tenths
    if not group[3:].isdigit() or 51 <= int(group[3:]) <= 55:
        return tenths / 10.0 + 273.15, None
    code = int(group[3:])
    depression = code if code <= 50 else (code - 50) * 10
    return tenths / 10.0 + 273.15, (tenths - depression) / 10.0 + 273.15


def _wind(group: str | None, unit: float) -> tuple[float | None, float | None]:
    """``ddfff``: the direction in tens of degrees, the odd five degrees
    carried as 500 added to the speed."""
    if group is None or not re.fullmatch(r"\d{5}", group):
        return None, None
    direction, speed = int(group[:2]) * 10, int(group[2:])
    if speed >= 500:
        direction, speed = direction + 5, speed - 500
    if direction > 360:
        return None, None
    return float(direction), speed * unit


def _is_wind(group: str) -> bool:
    return group == "/////" or (bool(re.fullmatch(r"\d{5}", group)) and int(group[:2]) <= 36)


def height(pressure: int, code: str) -> float | None:
    """A standard surface's ``hhh`` in geopotential metres. The text leaves
    out the thousands, and from 500 hPa up the units as well, so the surface
    itself supplies them."""
    if not re.fullmatch(r"\d{3}", code):
        return None
    value = int(code)
    if pressure == 100000:
        return float(500 - value if value > 500 else value)
    if pressure == 92500:
        return float(value)
    if pressure == 85000:
        return float(value if value >= 900 else 1000 + value)
    if pressure == 70000:
        return float(2000 + value if value >= 500 else 3000 + value)
    tens = value * 10
    if pressure in (50000, 40000, 30000):
        return float(tens)
    if pressure == 25000:
        return float(tens if value >= 500 else 10000 + tens)
    if pressure in (20000, 15000, 10000):
        return float(10000 + tens)
    if pressure == 7000:
        return float(10000 + tens)
    if pressure == 5000:
        return float(10000 + tens if value >= 500 else 20000 + tens)
    if pressure in (3000, 2000):
        return float(20000 + tens)
    return float(20000 + tens if value >= 500 else 30000 + tens)


def _wind_reach(part: str, code: str) -> int | None:
    """The lowest pressure whose level has a wind group, by the report's
    ``Id``: the hundreds (part C, the tens) of hectopascals of the last
    standard surface with a wind. None for ``/``, no winds."""
    if not code.isdigit():
        return None
    hundreds = int(code)
    if part == "CC":
        return hundreds * 1000 if hundreds else None
    # 0 is the 1000 hPa surface and 9 the 925.
    return {0: 100000, 9: 92500, 8: 85000}.get(hundreds, hundreds * 10000)


def standard_levels(
    part: str, reach_code: str, groups: list[str], *, unit: float, elev: float | None
) -> list[dict[str, Any]]:
    """Parts A and C: the surface (A only), the standard surfaces, the
    tropopauses and the levels of maximum wind."""
    rows: list[dict[str, Any]] = []
    reach = _wind_reach(part, reach_code)
    index = 0
    scale = 100 if part == "AA" else 10

    def take(has_wind: bool) -> tuple[str | None, str | None]:
        """The temperature and wind groups of a level at ``index``."""
        nonlocal index
        temperature = groups[index] if index < len(groups) else None
        index += 1
        wind = None
        if index < len(groups):
            following = groups[index]
            # The report's Id says whether the group is there, except that
            # a missing one is always itself and a group whose direction
            # is past 360 degrees is the next level's.
            if following == "/////" or (has_wind and _is_wind(following)):
                wind = following
                index += 1
        return temperature, wind

    if part == "AA" and groups and groups[0].startswith("99"):
        code = groups[0][2:]
        index = 1
        temperature, wind = take(True)
        if code.isdigit():
            value = int(code)
            rows.append(
                _row(
                    (value + 1000 if value < 200 else value) * 100,
                    bufr.SURFACE_BIT,
                    z=elev,
                    temperature=temperature,
                    wind=wind,
                    unit=unit,
                )
            )
    previous_height: float | None = None
    for code, pressure in STANDARD[part]:
        if index >= len(groups) or not _GROUP.match(groups[index]) or not groups[index].startswith(code):
            continue
        z = height(pressure, groups[index][2:])
        index += 1
        temperature, wind = take(reach is not None and pressure >= reach)
        if z is not None:
            if previous_height is not None and z <= previous_height:
                return rows
            previous_height = z
        rows.append(_row(pressure, bufr.STANDARD_BIT, z=z, temperature=temperature, wind=wind, unit=unit))
    while index < len(groups):
        group = groups[index]
        index += 1
        if group in SECTION_ENDS or not _GROUP.match(group):
            return rows
        kind, code = group[:2], group[2:]
        if kind not in ("88", "77", "66") or code == "999":
            continue
        pressure = int(code) * scale if code.isdigit() else 0
        if kind == "88":
            temperature, wind = take(True)
            if pressure:
                rows.append(_row(pressure, bufr.TROPOPAUSE_BIT, temperature=temperature, wind=wind, unit=unit))
            continue
        wind = groups[index] if index < len(groups) else None
        index += 1
        # The vertical shear about the maximum, which is not published.
        if index < len(groups) and groups[index].startswith("4"):
            index += 1
        if pressure:
            rows.append(_row(pressure, bufr.MAX_WIND_BIT, wind=wind, unit=unit))
    return rows


_NUMBERED = ("00", *(str(digit) * 2 for digit in range(1, 10)))
"""The numbers of a significant level."""


def significant_levels(part: str, groups: list[str], *, unit: float) -> list[dict[str, Any]]:
    """Parts B and D: the significant temperature levels, and after
    ``21212`` the significant wind levels, each a numbered pressure (00 the
    surface, then 11 … 99, 11 …) and a value. The surface takes no
    height here: part A gives it, and without part A the next height up
    can be the stratosphere's, with nothing to interpolate between."""
    rows: list[dict[str, Any]] = []
    wind = False
    previous: int | None = None
    index = 0
    while index < len(groups) - 1:
        level, value = groups[index], groups[index + 1]
        if level == "21212" and not wind:
            wind, previous = True, None
            index += 1
            continue
        index += 2
        number, code = level[:2], level[2:]
        if not _GROUP.match(level) or number not in _NUMBERED:
            break
        if not code.isdigit():
            continue
        pressure = int(code) * 10 if part == "DD" else (int(code) + 1000 if int(code) < 100 else int(code)) * 100
        if not pressure or (previous is not None and pressure >= previous):
            break
        previous = pressure
        surface = number == "00" and part == "BB"
        sig = bufr.SIGNIFICANT_WIND_BIT if wind else bufr.SIGNIFICANT_TEMPERATURE_BIT
        rows.append(
            _row(
                pressure,
                sig | (bufr.SURFACE_BIT if surface else 0),
                temperature=None if wind else value,
                wind=value if wind else None,
                unit=unit,
            )
        )
    return rows

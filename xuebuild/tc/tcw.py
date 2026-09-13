"""JTWC's JMV 3.0 ``.tcw`` product (S5): one file per system, the machine
half of a warning or a formation alert, followed by the text bulletin and
the system's history.

A warning::

    WTPN51 PHNC 120400                              WMO header
    WARNING    ATCG MIL 14E NEP 260912032055        kind, id, area, stamp
    2026091200 14E NORBERT    010  02 295 09 SATL 030
    T000 171N 1261W 055 R050 030 NE QD 000 SE QD ... R034 090 NE QD ...
    T012 ...
    AMP                                             the text follows
    ...
    //
    1426090718 126N1087W  15                        history: NNYY, mmddhh
    ...
    NNNN

The third line is the synoptic base time, the short id, the name (or
``INVEST``), the warning number, the number of active systems, motion
(degrees, knots), the position source and its accuracy in nm. A ``T``
line is lead hours, position in tenths, sustained wind in knots, then for
each ``Rnnn`` threshold four quadrant radii in nm. Winds are one-minute
sustained; radii over open water.

A formation alert (``ALERT``) carries no forecast: a base time, the two
ends of the formation line, its half-width in nm, the centre, then the
text and the same history block. It is parsed for the history and the
line, so an invest under an alert is a system the product knows about.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .registry import KNOT, NAUTICAL_MILE
from .track import Forecast, Point, Radii, Track, wrap_longitude

_T_LINE = re.compile(r"^T(\d{3})\s+(\d+)([NS])\s+(\d+)([EW])\s+(\d+)\s*(.*)$")
_RADII = re.compile(r"R(\d{3})\s+(\d{3})\s+NE\s+QD\s+(\d{3})\s+SE\s+QD\s+(\d{3})\s+SW\s+QD\s+(\d{3})\s+NW\s+QD")
_HISTORY = re.compile(r"^(\d{2})(\d{2})(\d{6})\s+(\d+)([NS])(\d+)([EW])\s+(\d+)\s*$")
_KINDS = ("WARNING", "ALERT")
_COORD_PAIR = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*$")


@dataclass(frozen=True)
class TcwProduct:
    kind: str
    """``warning`` or ``alert``."""
    short_id: str
    """The header's id — ``14E`` on a warning, ``97X`` on an alert, whose
    basin letter is a placeholder: the file name (``ep9726.tcw``) is the
    identity the builder trusts."""
    name: str | None
    base: datetime
    issued: datetime | None
    number: str | None
    forecast: Forecast | None
    """The warning's track; None on an alert."""
    history: Track
    """The system's past positions, six-hourly, as the product lists them
    — JTWC's working best track, provisional by nature."""
    alert_line: tuple[tuple[float, float], tuple[float, float]] | None
    """An alert's formation line as two (lat, lon) ends."""
    alert_half_width: float | None
    """Its half-width, km."""
    alert_center: tuple[float, float] | None


def _tenths(value: str, hemisphere: str, positive: str) -> float:
    degrees = int(value) / 10.0
    return degrees if hemisphere == positive else -degrees


def _stamp(value: str, year: int | None = None) -> datetime | None:
    """``260912032055`` (YYMMDDHHMMSS) or ``2609120100`` (YYMMDDHHMM)."""
    for pattern in ("%y%m%d%H%M%S", "%y%m%d%H%M"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _history(lines: list[str], base: datetime) -> Track:
    """The rows after the text, ``NNYYmmddhh lat lon vmax``. The year is
    two digits; the century is the base time's, and a December row seen
    from a January base is the year before."""
    points: dict[datetime, Point] = {}
    for line in lines:
        match = _HISTORY.match(line.strip())
        if not match:
            continue
        _number, yy, mmddhh, lat, ns, lon, ew, vmax = match.groups()
        year = (base.year // 100) * 100 + int(yy)
        try:
            time = datetime.strptime(f"{year}{mmddhh}", "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            continue
        if time > base + timedelta(days=1):
            time = time.replace(year=time.year - 1)
        # A row is repeated when a warning was reissued; the last wins.
        points[time] = Point(
            time=time,
            lat=_tenths(lat, ns, "N"),
            lon=wrap_longitude(_tenths(lon, ew, "E")),
            vmax=round(int(vmax) * KNOT, 1),
        )
    return Track(points=tuple(points[time] for time in sorted(points)), provisional=True)


def _t_line(line: str, base: datetime) -> Point | None:
    match = _T_LINE.match(line.strip())
    if not match:
        return None
    lead, lat, ns, lon, ew, vmax, rest = match.groups()
    radii: Radii = {}
    for threshold, ne, se, sw, nw in _RADII.findall(rest):
        key = str(int(threshold))
        if key in ("34", "50", "64"):
            radii[key] = [round(int(value) * NAUTICAL_MILE, 1) for value in (ne, se, sw, nw)]
    return Point(
        time=base + timedelta(hours=int(lead)),
        lat=_tenths(lat, ns, "N"),
        lon=wrap_longitude(_tenths(lon, ew, "E")),
        vmax=round(int(vmax) * KNOT, 1),
        radii=radii or None,
    )


def parse_tcw(text: str) -> TcwProduct:
    lines = text.splitlines()
    # A warning opens with its WMO header line, an alert without one: the
    # product line is the first that names a kind.
    header_index = next(
        (index for index, line in enumerate(lines[:2]) if line.split()[:1] and line.split()[0].upper() in _KINDS),
        None,
    )
    if header_index is None:
        raise ValueError("tcw: no WARNING or ALERT line in the first two")
    header = lines[header_index].split()
    if len(header) < 4:
        raise ValueError(f"tcw: unrecognised header line {lines[header_index]!r}")
    kind = header[0].upper()
    lines = lines[header_index - 1 :] if header_index else [""] + lines
    short_id = header[3].upper()
    issued = _stamp(header[-1]) if header[-1].isdigit() else None
    # Everything after the machine block is text until ``//``; the history
    # follows it.
    try:
        end_of_text = next(index for index, line in enumerate(lines) if line.strip() == "//")
    except StopIteration:
        end_of_text = len(lines)
    history_lines = lines[end_of_text + 1 :]

    if kind == "WARNING":
        head = lines[2].split()
        if len(head) < 4:
            raise ValueError(f"tcw: unrecognised warning line {lines[2]!r}")
        base = datetime.strptime(head[0], "%Y%m%d%H").replace(tzinfo=UTC)
        name = head[2].upper()
        if name in ("INVEST", "UNNAMED", "UNKNOWN", "NONAME"):
            name = None
        number = head[3]
        points = []
        for line in lines[3:end_of_text]:
            if line.strip() == "AMP":
                break
            point = _t_line(line, base)
            if point is not None:
                points.append(point)
        return TcwProduct(
            kind="warning",
            short_id=short_id,
            name=name,
            base=base,
            issued=issued,
            number=number,
            forecast=Forecast(base=base, points=tuple(points), issued=issued, number=number),
            history=_history(history_lines, base),
            alert_line=None,
            alert_half_width=None,
            alert_center=None,
        )

    # ALERT: base, two line ends, half-width, centre, then the validity
    # stamps nobody needs here.
    base = datetime.strptime(lines[2].strip(), "%Y%m%d%H").replace(tzinfo=UTC)
    coords = []
    half_width: float | None = None
    for line in lines[3:end_of_text]:
        pair = _COORD_PAIR.match(line)
        if pair and len(coords) < 3:
            coords.append((float(pair.group(1)), wrap_longitude(float(pair.group(2)))))
        elif half_width is None and line.strip().isdigit() and len(coords) == 2:
            half_width = round(int(line.strip()) * NAUTICAL_MILE, 1)
    return TcwProduct(
        kind="alert",
        short_id=short_id,
        name=None,
        base=base,
        issued=issued,
        number=None,
        forecast=None,
        history=_history(history_lines, base),
        alert_line=(coords[0], coords[1]) if len(coords) >= 2 else None,
        alert_half_width=half_width,
        alert_center=coords[2] if len(coords) >= 3 else None,
    )

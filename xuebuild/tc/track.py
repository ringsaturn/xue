"""The shapes every parser produces and ``schema.py`` writes: a point, a
forecast (points by lead from a base time), a best track (points by time)
and an ensemble (member × lead arrays). SI throughout — m/s, hPa, km,
seconds — converted at the parser, so nothing downstream knows a knot.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# The three wind-radii thresholds every source reports on, keyed by the
# knot value the agencies name them by (ECMWF's are 18 / 26 / 33 m/s —
# the same three to within a knot). A source without radii leaves the
# point's ``radii`` None; a threshold it does not report leaves that key
# out.
RADII_THRESHOLDS = ("34", "50", "64")
QUADRANTS = ("ne", "se", "sw", "nw")

MISSING = -32768
"""The missing value of an ensemble's fixed-point arrays."""


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_ymdh(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=UTC)


def wrap_longitude(value: float) -> float:
    """Into (-180, 180], the product's one convention. A value already
    inside is returned untouched, so a source's tenths stay tenths."""
    if -180.0 < value <= 180.0:
        return value
    wrapped = round(((value + 180.0) % 360.0) - 180.0, 4)
    return 180.0 if wrapped == -180.0 else wrapped


Radii = dict[str, list[float | None]]
"""Threshold → ``[ne, se, sw, nw]`` in km, a quadrant None when not reported."""


@dataclass(frozen=True)
class Point:
    time: datetime
    lat: float
    lon: float
    vmax: float | None = None
    """Maximum sustained wind, m/s, on whatever averaging the source uses."""
    pmin: float | None = None
    """Minimum central pressure, hPa."""
    radii: Radii | None = None
    cls: str | None = None
    """The source's own class string (``TS``, ``TY``, ``STS``…), verbatim."""
    rmw: float | None = None
    """Radius of maximum wind, km."""
    gust: float | None = None
    """Peak gust, m/s."""
    cone: float | None = None
    """Forecast-circle radius, km (phase 2 fills it)."""

    def to_json(self, base: datetime | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"time": iso_z(self.time)}
        if base is not None:
            payload["lead"] = round((self.time - base).total_seconds())
        payload.update({"lat": self.lat, "lon": self.lon, "vmax": self.vmax, "pmin": self.pmin})
        payload["radii"] = None if self.radii is None else {key: list(value) for key, value in self.radii.items()}
        payload["class"] = self.cls
        payload["rmw"] = self.rmw
        payload["gust"] = self.gust
        payload["cone"] = self.cone
        return payload


@dataclass(frozen=True)
class Forecast:
    """One centre's or one model's forecast: points at leads from ``base``.

    ``base`` is the time the leads count from — a model's cycle, an
    agency's synoptic time (NHC's advisory at 03Z forecasts from the 00Z
    position, with its first point at lead 3 h). ``issued`` is when it was
    released, the same as ``base`` where a source does not say. A point's
    valid time is ``base + lead``; the frontend aligns on valid time and
    never on either of these.
    """

    base: datetime
    points: tuple[Point, ...]
    issued: datetime | None = None
    number: str | None = None
    """Advisory / warning number, verbatim, when the source carries one."""
    run: str | None = None
    """A model's cycle id (``YYYYMMDDHH``), what the raster manifest calls
    ``run``; None for an agency."""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "issued": iso_z(self.issued or self.base),
            "base": iso_z(self.base),
        }
        if self.run is not None:
            payload["run"] = self.run
        if self.number is not None:
            payload["number"] = self.number
        payload["points"] = [point.to_json(self.base) for point in self.points]
        return payload


@dataclass(frozen=True)
class Track:
    """A best track: points by time, no leads."""

    points: tuple[Point, ...]
    provisional: bool = False
    """True when the source marks the track as still being revised — every
    working best track of a live storm is."""
    source: str | None = None
    """Where the product took it from: ``ibtracs``, or the centre whose
    working file it is (``nhc`` for a b-deck, ``jtwc`` for a warning's
    history)."""

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "provisional": self.provisional,
            "points": [point.to_json() for point in self.points],
        }


@dataclass(frozen=True)
class Member:
    id: int
    lat: list[float | None]
    lon: list[float | None]
    vmax: list[float | None]
    pmin: list[float | None]


@dataclass(frozen=True)
class Ensemble:
    """A member set on one lead axis, written as fixed-point integer
    arrays flattened member-major (``index = member × len(leads) +
    lead``): lat and lon ×100, vmax ×10 (m/s), pmin ×10 (hPa), a missing
    value ``MISSING`` — 51 members × 61 leads is 3 111 slots per field,
    which as integers gzip to a few kilobytes.
    """

    base: datetime
    leads: tuple[int, ...]
    """Seconds from ``base``, ascending."""
    members: tuple[Member, ...]
    issued: datetime | None = None
    run: str | None = None
    mean: Forecast | None = None
    """The ensemble mean track when the source publishes one (GEFS'
    ``AEMN``); None otherwise."""

    def to_json(self) -> dict[str, Any]:
        def flatten(values: list[list[float | None]], scale: int) -> list[int]:
            return [MISSING if v is None else round(v * scale) for row in values for v in row]

        payload: dict[str, Any] = {
            "issued": iso_z(self.issued or self.base),
            "base": iso_z(self.base),
        }
        if self.run is not None:
            payload["run"] = self.run
        payload.update(
            {
                "leads": list(self.leads),
                "members": [member.id for member in self.members],
                "lat": flatten([m.lat for m in self.members], 100),
                "lon": flatten([m.lon for m in self.members], 100),
                "vmax": flatten([m.vmax for m in self.members], 10),
                "pmin": flatten([m.pmin for m in self.members], 10),
            }
        )
        payload["mean"] = None if self.mean is None else self.mean.to_json()
        return payload


def member_from_points(member_id: int, leads: tuple[int, ...], base: datetime, points: dict[int, Point]) -> Member:
    """Lay a member's points (by lead seconds) onto the shared axis."""
    lat: list[float | None] = []
    lon: list[float | None] = []
    vmax: list[float | None] = []
    pmin: list[float | None] = []
    for lead in leads:
        point = points.get(lead)
        lat.append(None if point is None else point.lat)
        lon.append(None if point is None else point.lon)
        vmax.append(None if point is None else point.vmax)
        pmin.append(None if point is None else point.pmin)
    return Member(member_id, lat, lon, vmax, pmin)


def lead_seconds(point: Point, base: datetime) -> int:
    return round((point.time - base).total_seconds())


def valid_time(base: datetime, lead_hours: float) -> datetime:
    return base + timedelta(hours=lead_hours)


@dataclass
class SourceStatus:
    """What one source contributed to a build, ok or not — recorded in
    ``index.json``'s ``sources`` so a missing line on the map has a reason
    next to it rather than a silent absence."""

    id: str
    ok: bool
    fetched: datetime | None = None
    url: str | None = None
    error: str | None = None
    cycle: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.fetched is not None:
            payload["fetched"] = iso_z(self.fetched)
        if self.url is not None:
            payload["url"] = self.url
        if self.cycle is not None:
            payload["cycle"] = self.cycle
        if self.error is not None:
            payload["error"] = self.error
        payload.update(self.detail)
        return payload

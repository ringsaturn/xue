"""ECMWF's tropical cyclone track products (S2): ``tf`` BUFR from the open
data set — ``oper`` holds the HRES track of each system, ``enfo`` one
message per system with the 51 ENS members as subsets.

The bytes are read through ``bufr_dump -j f`` (eccodes; the same tool
family the ECMWF GRIB repack uses), which gives one flat stream of
``{key, value}`` entries for the whole file. A message begins where
``centre`` appears at data index 1; in a compressed message every
per-subset value is an array over the subsets, in an uncompressed one the
subsets follow each other with a ``subsetNumber`` marker each. This parser
treats both alike by reading every entry as "one value per subset".

Within a subset the layout is, in order: the header (storm identifier,
name, member number and type, base time), the analysis block — the
observed centre (significance 1), the model's analysed centre
(significance 4 or 5) with its pressure, the maximum-wind location
(significance 3) with the wind — then ``delayedDescriptorReplicationFactor``
periods, each ``timePeriod`` hours after the base with the same
centre / pressure / max-wind / wind sequence and, after each, wind radii
by quadrant for the 18, 26 and 33 m/s thresholds. A member that has lost
the system has nulls at that period.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .track import Forecast, Point, Radii, wrap_longitude

# The three BUFR thresholds and the knot names the product keys radii by.
_THRESHOLDS = {18: "34", 26: "50", 33: "64"}


@dataclass
class _Period:
    """One (subset, period) slot while the stream is being walked."""

    lead_hours: float
    center: tuple[float, float] | None = None
    analysed: tuple[float, float] | None = None
    pressure: float | None = None
    wind: float | None = None
    radii: dict[str, list[float | None]] = field(default_factory=dict)

    def position(self) -> tuple[float, float] | None:
        # Forecast periods carry the centre under significance 1; the
        # analysis block puts the model's own position under 4/5 and the
        # observed one under 1 — the model's is the track's t=0.
        return self.analysed or self.center


@dataclass(frozen=True)
class MemberTrack:
    member: int
    kind: int
    """``ensembleForecastType``: 0 the unperturbed control / HRES, 4 (in
    practice) a perturbed member."""
    forecast: Forecast


@dataclass(frozen=True)
class BufrStorm:
    identifier: str
    """``stormIdentifier``: ``14E``, or ``70W`` for a system the model found
    on its own, numbered afresh every run."""
    name: str | None
    base: datetime
    members: tuple[MemberTrack, ...]


def _chunks(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split the flat stream at every message / uncompressed-subset start."""
    chunks: list[list[dict[str, Any]]] = []
    for entry in entries:
        if entry.get("key") == "subsetNumber":
            continue
        if entry.get("index") == 1 and entry.get("key") == "centre":
            chunks.append([])
        if chunks:
            chunks[-1].append(entry)
    return chunks


def _subset_count(chunk: list[dict[str, Any]]) -> int:
    for entry in chunk:
        if isinstance(entry.get("value"), list):
            return len(entry["value"])
    return 1


def _values(entry: dict[str, Any], count: int) -> list[Any]:
    value = entry.get("value")
    if isinstance(value, list):
        return value
    return [value] * count


def _scalar(entry: dict[str, Any]) -> Any:
    value = entry.get("value")
    if isinstance(value, list):
        return next((item for item in value if item is not None), None)
    return value


def _parse_chunk(chunk: list[dict[str, Any]]) -> BufrStorm | None:
    count = _subset_count(chunk)
    identifier: str | None = None
    name: str | None = None
    members: list[int | None] = [None] * count
    kinds: list[int | None] = [None] * count
    clock: dict[str, int] = {}
    # periods[subset] is the ordered list of that subset's slots; the first
    # is the analysis.
    periods: list[list[_Period]] = [[_Period(0.0)] for _ in range(count)]
    significance: list[int | None] = [None] * count
    threshold: str | None = None
    for entry in chunk:
        key = entry.get("key")
        if key == "stormIdentifier":
            identifier = str(_scalar(entry) or "").strip() or None
        elif key == "longStormName":
            raw = str(_scalar(entry) or "").strip()
            name = raw.upper() if raw and raw != (identifier or "") and not raw[:1].isdigit() else None
        elif key == "ensembleMemberNumber":
            members = [None if v is None else int(v) for v in _values(entry, count)]
        elif key == "ensembleForecastType":
            kinds = [None if v is None else int(v) for v in _values(entry, count)]
        elif key in ("year", "month", "day", "hour", "minute"):
            value = _scalar(entry)
            if value is not None:
                clock[key] = int(value)
        elif key == "timePeriod":
            lead = _scalar(entry)
            if lead is None:
                continue
            for subset in range(count):
                periods[subset].append(_Period(float(lead)))
            threshold = None
        elif key == "meteorologicalAttributeSignificance":
            significance = [None if v is None else int(v) for v in _values(entry, count)]
        elif key in ("latitude", "longitude"):
            for subset, value in enumerate(_values(entry, count)):
                slot = periods[subset][-1]
                sig = significance[subset]
                attribute = "center" if sig == 1 else "analysed" if sig in (4, 5) else None
                if attribute is None or value is None:
                    continue
                current = getattr(slot, attribute)
                if key == "latitude":
                    setattr(slot, attribute, (float(value), current[1] if current else float("nan")))
                else:
                    setattr(slot, attribute, (current[0] if current else float("nan"), float(value)))
        elif key == "pressureReducedToMeanSeaLevel":
            for subset, value in enumerate(_values(entry, count)):
                if value is not None:
                    periods[subset][-1].pressure = float(value) / 100.0
        elif key == "windSpeedAt10M":
            for subset, value in enumerate(_values(entry, count)):
                if value is not None:
                    periods[subset][-1].wind = float(value)
        elif key == "windSpeedThreshold":
            value = _scalar(entry)
            threshold = _THRESHOLDS.get(int(value)) if value is not None else None
        elif key == "effectiveRadiusWithRespectToWindSpeedsAboveThreshold":
            if threshold is None:
                continue
            for subset, value in enumerate(_values(entry, count)):
                quadrants = periods[subset][-1].radii.setdefault(threshold, [])
                if len(quadrants) < 4:
                    quadrants.append(None if value is None else round(float(value) / 1000.0, 1))
    if identifier is None or len(clock) < 4:
        return None
    base = datetime(clock["year"], clock["month"], clock["day"], clock["hour"], clock.get("minute", 0), tzinfo=UTC)
    tracks = []
    for subset in range(count):
        points = []
        for slot in periods[subset]:
            position = slot.position()
            if position is None or any(math.isnan(coordinate) for coordinate in position):
                continue
            radii: Radii = {
                key: quadrants for key, quadrants in slot.radii.items() if any(q is not None for q in quadrants)
            }
            points.append(
                Point(
                    time=base + timedelta(hours=slot.lead_hours),
                    lat=position[0],
                    lon=wrap_longitude(position[1]),
                    vmax=slot.wind,
                    pmin=slot.pressure,
                    radii=radii or None,
                )
            )
        member = members[subset]
        tracks.append(
            MemberTrack(
                member=member if member is not None else subset + 1,
                kind=kinds[subset] if kinds[subset] is not None else 0,
                forecast=Forecast(base=base, points=tuple(points), run=base.strftime("%Y%m%d%H")),
            )
        )
    return BufrStorm(identifier=identifier, name=name, base=base, members=tuple(tracks))


def parse_bufr_tracks(dump: dict[str, Any]) -> list[BufrStorm]:
    """Every system in a ``bufr_dump -j f`` document, in file order. A
    system that appears in more than one message (one member per message
    is how the uncompressed case is written) is merged into one."""
    entries = dump.get("messages")
    if not isinstance(entries, list):
        raise TypeError("bufr dump: no messages array")
    storms: dict[tuple[str, datetime], BufrStorm] = {}
    for chunk in _chunks(entries):
        storm = _parse_chunk(chunk)
        if storm is None:
            continue
        key = (storm.identifier, storm.base)
        if key in storms:
            existing = storms[key]
            storms[key] = BufrStorm(
                identifier=existing.identifier,
                name=existing.name or storm.name,
                base=existing.base,
                members=existing.members + storm.members,
            )
        else:
            storms[key] = storm
    return list(storms.values())

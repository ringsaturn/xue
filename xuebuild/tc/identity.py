"""Which sightings are the same storm (§3.2, §3.4 of the design).

Every source names a system its own way. The rules, in priority order:

1. A numbered ATCF id (``EP142026``: basin, number ≤ 49, season) is the
   primary key. JTWC, NHC, the NCEP tracker, ECMWF (``14E`` ⇔ ``EP14``)
   and IBTrACS (``USA_ATCF_ID``) all carry it, so an A-level storm needs
   no heuristics at all.
2. An invest (number 90–99) is reused within a season, so it is only a
   name for a while: a B-level system is identified by its synthetic id
   ``x-<basin>-<first seen YYYYMMDDHH>-<n>`` and the invest number is an
   alias with a validity window. The previous hour's ``index.json`` is the
   memory: the same alias seen again within 48 h is the same system.
3. ECMWF numbers the systems it finds on its own (``70W``…) afresh every
   run, so those never carry across: one is attached to a known system by
   proximity — within ``MATCH_KM`` of that system's position nearest in
   time — and otherwise becomes a C-level system of its own, matched to
   the previous hour's C-level systems the same way so its id holds while
   the model keeps finding it.
4. When an invest is numbered, the B-level system is *merged into* the
   A-level storm: a previous B/C system that no source names this hour,
   whose last position lies within ``MATCH_KM`` / 48 h of a current
   A-level storm, becomes an alias of it, and ``crosswalk`` maps the old id
   to the new so a deep link keeps working.

Only a storm's *position* is compared, never its intensity: two systems
within 300 km at the same time are one system, and a mistaken merge of
binary cyclones is the accepted failure mode, softened by the ATCF key
taking precedence wherever any source supplies it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .registry import INVEST_MIN, NUMBERED_MAX
from .track import iso_z

MATCH_KM = 333.0
"""About three degrees of arc: how far apart two positions at the same
time may be and still be one system."""
MERGE_HOURS = 48
"""How long an unseen system stays matchable before it is a new one."""
_EARTH_RADIUS_KM = 6371.0


@dataclass(frozen=True)
class Sighting:
    """One source's view of one system this hour."""

    source: str
    key: str
    """The source's own id: a full ATCF id (``EP142026``), an invest
    short id (``EP97``), or an ECMWF identifier (``70W``)."""
    kind: str
    """``numbered`` (rule 1), ``invest`` (rule 2) or ``unnumbered`` (rule 3)."""
    basin: str
    time: datetime
    """When the position below is for — the current or base time."""
    lat: float
    lon: float
    name: str | None = None
    first_seen: datetime | None = None
    """The earliest time the source knows the system at (a history row),
    for the synthetic id of a new B-level system."""
    payload: Any = None
    """Whatever the builder attaches; opaque here."""


@dataclass
class System:
    id: str
    level: str
    basin: str
    name: str | None = None
    aliases: dict[str, dict[str, str]] = field(default_factory=dict)
    """Alias → ``{"from": ISO, "to": ISO}``: the invest numbers and ECMWF
    identifiers the system has gone under, with their validity."""
    sightings: list[Sighting] = field(default_factory=list)
    positions: list[tuple[datetime, float, float]] = field(default_factory=list)

    def note_alias(self, alias: str, time: datetime) -> None:
        window = self.aliases.setdefault(alias, {"from": iso_z(time), "to": iso_z(time)})
        window["from"] = min(window["from"], iso_z(time))
        window["to"] = max(window["to"], iso_z(time))

    def add(self, sighting: Sighting) -> None:
        self.sightings.append(sighting)
        self.positions.append((sighting.time, sighting.lat, sighting.lon))
        if self.name is None and sighting.name:
            self.name = sighting.name

    def nearest_position(self, time: datetime) -> tuple[datetime, float, float] | None:
        if not self.positions:
            return None
        return min(self.positions, key=lambda item: abs((item[0] - time).total_seconds()))


@dataclass(frozen=True)
class PreviousSystem:
    """What the previous ``index.json`` remembers of a system."""

    id: str
    level: str
    basin: str
    aliases: dict[str, dict[str, str]]
    last_time: datetime
    last_lat: float
    last_lon: float
    name: str | None = None


@dataclass
class Resolution:
    systems: list[System]
    crosswalk: dict[str, str]
    """Old id → the id it merged into."""


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def atcf_number(key: str) -> int | None:
    """The cyclone number of ``EP142026`` or ``EP14``."""
    digits = key[2:4]
    return int(digits) if len(key) >= 4 and digits.isdigit() else None


def sighting_kind(key: str) -> str:
    number = atcf_number(key)
    if number is None:
        return "unnumbered"
    if number <= NUMBERED_MAX:
        return "numbered"
    if number >= INVEST_MIN:
        return "invest"
    return "unnumbered"


def synthetic_id(basin: str, first_seen: datetime, taken: set[str]) -> str:
    stem = f"x-{basin.lower()}-{first_seen.strftime('%Y%m%d%H')}"
    ordinal = 1
    while f"{stem}-{ordinal}" in taken:
        ordinal += 1
    return f"{stem}-{ordinal}"


def _near(system_positions: list[tuple[datetime, float, float]], time: datetime, lat: float, lon: float) -> bool:
    if not system_positions:
        return False
    nearest = min(system_positions, key=lambda item: abs((item[0] - time).total_seconds()))
    if abs((nearest[0] - time).total_seconds()) > MERGE_HOURS * 3600:
        return False
    return distance_km(nearest[1], nearest[2], lat, lon) <= MATCH_KM


def resolve(sightings: list[Sighting], previous: list[PreviousSystem], now: datetime) -> Resolution:
    systems: dict[str, System] = {}
    crosswalk: dict[str, str] = {}
    taken: set[str] = {item.id for item in previous}
    previous_by_id = {item.id: item for item in previous}
    matched_previous: set[str] = set()

    def remember(system: System, prior: PreviousSystem | None) -> None:
        if prior is not None:
            matched_previous.add(prior.id)
            for alias, window in prior.aliases.items():
                current = system.aliases.setdefault(alias, dict(window))
                current["from"] = min(current["from"], window["from"])
                current["to"] = max(current["to"], window["to"])
            if system.name is None:
                system.name = prior.name
            system.positions.append((prior.last_time, prior.last_lat, prior.last_lon))

    # Rule 1: every numbered id is a storm, before anything is matched.
    for sighting in sightings:
        if sighting.kind != "numbered":
            continue
        storm_id = sighting.key
        system = systems.get(storm_id)
        if system is None:
            system = System(id=storm_id, level="A", basin=sighting.basin)
            systems[storm_id] = system
            taken.add(storm_id)
            remember(system, previous_by_id.get(storm_id))
        system.add(sighting)

    # Rule 2: invests, by alias memory first, then grouped by short id.
    fresh_invests: dict[str, System] = {}
    for sighting in sorted((s for s in sightings if s.kind == "invest"), key=lambda s: s.time):
        alias = sighting.key
        system = fresh_invests.get(alias)
        if system is None:
            prior = next(
                (
                    item
                    for item in previous
                    if alias in item.aliases and now - item.last_time <= timedelta(hours=MERGE_HOURS)
                ),
                None,
            )
            if prior is not None and prior.id in systems:
                system = systems[prior.id]
            elif prior is not None:
                # A storm numbered since keeps answering to its invest
                # number for a while; the alias stays on the A-level id.
                system = System(id=prior.id, level="B" if prior.level != "A" else "A", basin=prior.basin)
                systems[prior.id] = system
                remember(system, prior)
            else:
                first_seen = sighting.first_seen or sighting.time
                system = System(id=synthetic_id(sighting.basin, first_seen, taken), level="B", basin=sighting.basin)
                systems[system.id] = system
                taken.add(system.id)
            fresh_invests[alias] = system
        system.level = "B" if system.level == "C" else system.level
        system.note_alias(alias, sighting.time)
        system.add(sighting)

    # Rule 3: unnumbered model systems attach by proximity, else stand alone.
    for sighting in (s for s in sightings if s.kind == "unnumbered"):
        host = next(
            (
                system
                for system in systems.values()
                if system.basin == sighting.basin and _near(system.positions, sighting.time, sighting.lat, sighting.lon)
            ),
            None,
        )
        if host is None:
            prior = next(
                (
                    item
                    for item in previous
                    if item.level == "C"
                    and item.id not in systems
                    and item.basin == sighting.basin
                    and _near(
                        [(item.last_time, item.last_lat, item.last_lon)], sighting.time, sighting.lat, sighting.lon
                    )
                ),
                None,
            )
            if prior is not None:
                host = System(id=prior.id, level="C", basin=prior.basin)
                systems[prior.id] = host
                remember(host, prior)
            else:
                host = System(
                    id=synthetic_id(sighting.basin, sighting.first_seen or sighting.time, taken),
                    level="C",
                    basin=sighting.basin,
                )
                systems[host.id] = host
                taken.add(host.id)
        host.note_alias(f"{sighting.source}:{sighting.key}", sighting.time)
        host.add(sighting)

    # Rule 4: a previous B/C system nobody names this hour merges into the
    # A-level storm it turned into, if one is where it was headed.
    for prior in previous:
        if prior.id in systems or prior.id in matched_previous or prior.level == "A":
            continue
        if now - prior.last_time > timedelta(hours=MERGE_HOURS):
            continue
        target = next(
            (
                system
                for system in systems.values()
                if system.level == "A"
                and system.basin == prior.basin
                and _near(system.positions, prior.last_time, prior.last_lat, prior.last_lon)
            ),
            None,
        )
        if target is not None:
            crosswalk[prior.id] = target.id
            for alias, window in prior.aliases.items():
                target.aliases.setdefault(alias, dict(window))
    # Old crosswalk entries whose target still exists stay valid.
    return Resolution(systems=list(systems.values()), crosswalk=crosswalk)

"""A station as every network adapter hands it to the build."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StationMeta:
    id: str
    """``<network>:<the network's own station identifier>``."""
    name: str | None
    """In Latin script; ``names`` carries the network's own spellings."""
    names: dict[str, str] | None
    lat: float
    lon: float
    elev: float | None
    rank: int
    agency: str | None
    """The agency that operates the station and owns its data, which a
    display credits beside the network."""

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "names": self.names,
            "lat": self.lat,
            "lon": self.lon,
            "elev": self.elev,
            "rank": self.rank,
            "agency": self.agency,
        }


Observation = tuple[str, int, dict[str, float | None]]
"""``(station id, epoch seconds at the end of the hour, elements)``."""


@dataclass
class NetworkRead:
    """What an adapter read out of one round's fetched files."""

    stations: dict[str, StationMeta]
    """Every station the round's files place, by id."""
    observations: list[Observation]
    ok: bool
    """Whether the observation source arrived; the round publishes when
    any network's did."""

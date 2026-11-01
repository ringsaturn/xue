"""A station as every network adapter hands it to the build."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StationMeta:
    id: str
    """``<network>:<the network's own station number>``."""
    name: str | None
    """In Latin script; ``names`` carries the network's own spellings."""
    names: dict[str, str] | None
    lat: float
    lon: float
    elev: float | None
    wmo: str | None
    rank: int
    wigos: str | None = None
    """The WIGOS station identifier, where the source gives one."""

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "names": self.names,
            "lat": self.lat,
            "lon": self.lon,
            "elev": self.elev,
            "wmo": self.wmo,
            "rank": self.rank,
            "wigos": self.wigos,
        }


Observation = tuple[str, int, dict[str, Any]]
"""``(station id, epoch seconds, elements)``: one station at one time.
A network with quality codes adds them under :data:`QC_KEY`, element →
code, beside the values."""

QC_KEY = "_qc"
"""Not an element key (those match ``^[a-z][a-z0-9]*$``), so the codes
travel with the values through the merge without being taken for one."""


@dataclass
class NetworkRead:
    """What an adapter read out of one round's fetched files."""

    stations: dict[str, StationMeta]
    """Every station the network's table places, by id."""
    observations: list[Observation]
    ok: bool
    """Whether the observation source arrived; the round publishes when
    any network's did."""

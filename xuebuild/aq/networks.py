"""The networks the ``aq`` product carries, in the order the index lists
them. A fixed tuple rather than a registry that discovers adapters: adding
a network is a reviewed change to this file, its adapter module, its
section of ``docs/aq.md`` and its fixture — and to nothing else, not the
schema and not the shell."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..airport.fetch import SourceStatus
from . import airnow, hkepd
from .station import NetworkRead


@dataclass(frozen=True)
class Network:
    id: str
    name: str
    cadence: int
    """Seconds between a station's reports."""
    avg_period: int
    """Seconds each value is a mean over, ending at its time."""
    preliminary: bool
    """Whether the network publishes unvalidated data, which a display
    must say."""
    attribution: str
    license: str
    url: str
    sources: tuple[str, ...]
    """The adapter's source ids, in the order ``sources[]`` lists them."""
    fetch: Callable[..., list[SourceStatus]]
    """``fetch(raw_root, round, since, force=…, now=…)``: write the round's
    raw files and say how it went. ``since`` is the network's newest
    published time, or None."""
    read: Callable[[Path, dict[str, SourceStatus]], NetworkRead]

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "cadence": self.cadence,
            "avgPeriod": self.avg_period,
            "preliminary": self.preliminary,
            "attribution": self.attribution,
            "license": self.license,
            "url": self.url,
        }


AIRNOW = Network(
    id=airnow.NETWORK,
    name="AirNow",
    cadence=3600,
    avg_period=3600,
    preliminary=True,
    attribution="AirNow (U.S. EPA) and the reporting agencies; preliminary data",
    license="AirNow-DEG",
    url="https://www.airnow.gov/",
    sources=(airnow.SOURCE,),
    fetch=airnow.fetch,
    read=airnow.read,
)

HKEPD = Network(
    id=hkepd.NETWORK,
    name="Hong Kong EPD",
    cadence=3600,
    avg_period=3600,
    preliminary=True,
    attribution="Environmental Protection Department, HKSAR Government (DATA.GOV.HK)",
    license="DATA.GOV.HK",
    url="https://www.aqhi.gov.hk/",
    sources=(hkepd.SOURCE,),
    fetch=hkepd.fetch,
    read=hkepd.read,
)

NETWORKS: tuple[Network, ...] = (AIRNOW, HKEPD)


def network_by_id(network_id: str) -> Network | None:
    return next((network for network in NETWORKS if network.id == network_id), None)


__all__ = ["NETWORKS", "Network", "network_by_id"]

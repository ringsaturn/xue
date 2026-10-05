"""The networks the ``synop`` product carries, in the order the index
lists them. A fixed tuple rather than a registry that discovers adapters:
adding a network is a reviewed change to this file, its adapter module,
its section of ``docs/synop.md`` and its fixture — and to nothing else, not
the schema and not the shell."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..airport.fetch import SourceStatus
from . import amedas
from .station import NetworkRead


@dataclass(frozen=True)
class Network:
    id: str
    name: str
    cadence: int
    """Seconds between a station's reports."""
    pr_period: int | None
    """Seconds the ``pr`` and ``sun`` elements accumulate over."""
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
            "prPeriod": self.pr_period,
            "attribution": self.attribution,
            "license": self.license,
            "url": self.url,
        }


AMEDAS = Network(
    id=amedas.NETWORK,
    name="JMA AMeDAS",
    cadence=600,
    pr_period=600,
    attribution="出典：気象庁ホームページ (Japan Meteorological Agency)",
    license="PDL-1.0",
    url="https://www.jma.go.jp/bosai/amedas/",
    sources=(amedas.MAP_SOURCE, amedas.TABLE_SOURCE),
    fetch=amedas.fetch,
    read=amedas.read,
)

NETWORKS: tuple[Network, ...] = (AMEDAS,)


def network_by_id(network_id: str) -> Network | None:
    return next((network for network in NETWORKS if network.id == network_id), None)


__all__ = ["NETWORKS", "Network", "network_by_id"]

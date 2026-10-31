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
from . import amedas, dwd, hko
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
    partial: bool = False
    """Whether one station's report at one time can arrive in pieces —
    several messages, several files, several rounds. The merge then lays a
    new report over the old element by element, so a piece that leaves an
    element out does not erase what an earlier piece said."""

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

DWD = Network(
    id=dwd.NETWORK,
    name="DWD SYNOP Germany",
    cadence=600,
    pr_period=600,
    attribution="Source: Deutscher Wetterdienst",
    license="CC-BY-4.0",
    url="https://opendata.dwd.de/",
    sources=(dwd.SOURCE,),
    fetch=dwd.fetch,
    read=dwd.read,
    partial=True,
)

HKO = Network(
    id=hko.NETWORK,
    name="Hong Kong Observatory",
    cadence=600,
    pr_period=None,
    attribution="Hong Kong Observatory (DATA.GOV.HK)",
    license="DATA.GOV.HK",
    url="https://data.gov.hk/",
    sources=(hko.SOURCE,),
    fetch=hko.fetch,
    read=hko.read,
    partial=True,
)

NETWORKS: tuple[Network, ...] = (AMEDAS, DWD, HKO)


def network_by_id(network_id: str) -> Network | None:
    return next((network for network in NETWORKS if network.id == network_id), None)


__all__ = ["NETWORKS", "Network", "network_by_id"]

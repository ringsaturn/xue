"""The networks the ``alerts`` product carries, in the order the index
lists them, and the boundary set each one's area codes resolve against.

A fixed tuple rather than a registry that discovers adapters: adding a
network is a reviewed change to this file, its adapter module, its
section of ``docs/alerts.md`` and its fixture. A new boundary release is
a change to the pinned :class:`AreaSet` below, after ``xue alerts-areas``
built it and ``make upload-r2-alerts-areas`` put it on the bucket — the
length and CRC32 are what a round promises the file is.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from . import eccc, jma, nws
from .alert import NetworkRead
from .schema import areas_path


@dataclass(frozen=True)
class AreaSet:
    set: str
    version: str
    byte_length: int
    crc32: str
    layers: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "set": self.set,
            "version": self.version,
            "path": areas_path(self.set, self.version),
            "byteLength": self.byte_length,
            "crc32": self.crc32,
            "layers": list(self.layers),
        }


@dataclass(frozen=True)
class Network:
    id: str
    name: str
    attribution: str
    license: str
    url: str
    sources: tuple[str, ...]
    fetch: Callable[..., list[SourceStatus]]
    """``fetch(raw_root, round, since, force=…, now=…)``: write the round's
    raw files and say how it went. ``since`` is the network's ``latest``
    in the previous round's index, or None."""
    read: Callable[[Path, datetime, dict[str, SourceStatus]], NetworkRead]
    areas: AreaSet | None

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "attribution": self.attribution, "license": self.license, "url": self.url}


NWS_ZONES = AreaSet(
    set="nws-zones",
    version="20260416",
    byte_length=8907673,
    crc32="3f5d3096",
    layers=("zone", "county", "marine", "offshore", "fire"),
)
"""The AWIPS public zones, counties, coastal and offshore marine zones and
fire weather zones effective 2026-04-16 (``z_16ap26``, ``c_16ap26``,
``mz16ap26``, ``oz16ap26``, ``fz16ap26``)."""

JMA_CLASS20 = AreaSet(
    set="jma-class20",
    version="20260226",
    byte_length=2795153,
    crc32="43b3b6eb",
    layers=("class20",),
)
"""The agency's 予報区等GISデータ, 市町村等（気象警報等）, released
2026-02-26. Built and measured, but not named by the ``jma`` network yet:
whether a simplified derivative may be redistributed is still being
checked (``docs/alerts.md`` §10.2)."""

NWS = Network(
    id=nws.NETWORK,
    name="US National Weather Service",
    attribution="US National Weather Service",
    license="public-domain",
    url="https://www.weather.gov/",
    sources=(nws.SOURCE,),
    fetch=nws.fetch,
    read=nws.read,
    areas=NWS_ZONES,
)

JMA = Network(
    id=jma.NETWORK,
    name="Japan Meteorological Agency",
    attribution="出典：気象庁ホームページ (Japan Meteorological Agency)",
    license="PDL-1.0",
    url="https://www.jma.go.jp/bosai/warning/",
    sources=(jma.SOURCE,),
    fetch=jma.fetch,
    read=jma.read,
    areas=None,
)

ECCC = Network(
    id=eccc.NETWORK,
    name="Environment and Climate Change Canada",
    attribution="Contains information licensed under the Open Government Licence – Canada (Environment and Climate Change Canada)",
    license="OGL-Canada-2.0",
    url="https://weather.gc.ca/",
    sources=(eccc.SOURCE,),
    fetch=eccc.fetch,
    read=eccc.read,
    areas=None,
)

NETWORKS: tuple[Network, ...] = (NWS, JMA, ECCC)

AREA_SETS: dict[str, AreaSet] = {area_set.set: area_set for area_set in (NWS_ZONES, JMA_CLASS20)}


def network_by_id(network_id: str) -> Network | None:
    return next((network for network in NETWORKS if network.id == network_id), None)

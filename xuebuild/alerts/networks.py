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
from . import cma, eccc, hko, jma, meteoalarm, nws
from .alert import NetworkRead
from .schema import areas_bbox_filename, areas_bbox_path, areas_path


@dataclass(frozen=True)
class AreaSet:
    set: str
    version: str
    byte_length: int
    crc32: str
    layers: tuple[str, ...]
    bbox_byte_length: int
    bbox_crc32: str
    """``<set>.<version>.bbox.json``, every code's box, written beside the
    tiles by ``xue alerts-areas`` and pinned the same way."""

    @property
    def bbox_filename(self) -> str:
        return areas_bbox_filename(self.set, self.version)

    def to_json(self) -> dict[str, Any]:
        return {
            "set": self.set,
            "version": self.version,
            "path": areas_path(self.set, self.version),
            "byteLength": self.byte_length,
            "crc32": self.crc32,
            "layers": list(self.layers),
            "bbox": {
                "path": areas_bbox_path(self.set, self.version),
                "byteLength": self.bbox_byte_length,
                "crc32": self.bbox_crc32,
            },
        }


@dataclass(frozen=True)
class Network:
    id: str
    name: str
    attribution: str
    license: str
    license_url: str
    """The terms page the ``license`` names."""
    url: str
    sources: tuple[str, ...]
    fetch: Callable[..., list[SourceStatus]]
    """``fetch(raw_root, round, since, force=…, now=…)``: write the round's
    raw files and say how it went. ``since`` is the network's ``latest``
    in the previous round's index, or None."""
    read: Callable[[Path, datetime, dict[str, SourceStatus]], NetworkRead]
    areas: AreaSet | None
    area_layers: Callable[[dict[str, Any]], tuple[str, ...]] | None = None
    """The layers of ``areas`` a line's codes resolve against, in the order
    they are looked up (``docs/alerts.md`` §9)."""
    areas_beside_geometry: bool = False
    """A line's ``areas`` are the parts of the warned area its polygon does
    not draw (MeteoAlarm), rather than the zones the polygon touches
    (NWS): its box is then the union of both."""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "attribution": self.attribution,
            "license": self.license,
            "licenseUrl": self.license_url,
            "url": self.url,
        }


NWS_ZONES = AreaSet(
    set="nws-zones",
    version="20261102",
    byte_length=31023141,
    crc32="879ebf6c",
    layers=("zone", "county", "marine", "offshore", "fire"),
    bbox_byte_length=524181,
    bbox_crc32="05a06972",
)
"""The AWIPS public zones, counties, coastal and offshore marine zones and
fire weather zones effective 2026-04-16 (``z_16ap26``, ``c_16ap26``,
``mz16ap26``, ``oz16ap26``, ``fz16ap26``)."""

JMA_CLASS20 = AreaSet(
    set="jma-class20",
    version="20261102",
    byte_length=12593916,
    crc32="49385bc7",
    layers=("class20",),
    bbox_byte_length=82289,
    bbox_crc32="c1108b5b",
)
"""The agency's 予報区等GISデータ, 市町村等（気象警報等）, released
2026-02-26, tiled by xue; reused under the Public Data
License v1.0 with the credit ``docs/alerts.md`` §9.2 gives."""

METEOALARM_AREAS = AreaSet(
    set="meteoalarm-areas",
    version="20261102",
    byte_length=3563611,
    crc32="25117c18",
    layers=("de_kreise", "de_kueste", "de_binnenseen", "fr_departement"),
    bbox_byte_length=28815,
    bbox_crc32="97b9b07f",
)
"""The MeteoAlarm areas with openly licensed boundaries, keyed by the
geocode a line names (``WARNCELLID:…``, ``NUTS3:…``): the DWD's warn cells
(CC BY 4.0, Datenbasis: Deutscher Wetterdienst; © GeoBasis-DE / BKG) and
IGN's ADMIN EXPRESS départements (Licence Ouverte 2.0) under their NUTS
2013 codes, as fetched on 2026-11-01 (``docs/alerts.md`` §9.3)."""

CMA_AREAS = AreaSet(
    set="cma-areas",
    version="20261112",
    byte_length=21162186,
    crc32="62936b04",
    layers=("province", "prefecture", "county"),
    bbox_byte_length=143930,
    bbox_crc32="46f79c49",
)
"""China's provinces, prefectures and counties by their six-digit
administrative codes: the Amap outlines packaged by cnmaps-data (MIT,
commit e2a1a346, 2026-04-02), GCJ-02 turned into WGS84 by xue
(``docs/alerts.md`` §9.4)."""

NWS = Network(
    id=nws.NETWORK,
    name="US National Weather Service",
    attribution="US National Weather Service",
    license="public-domain",
    license_url="https://www.weather.gov/disclaimer",
    url="https://www.weather.gov/",
    sources=(nws.SOURCE,),
    fetch=nws.fetch,
    read=nws.read,
    areas=NWS_ZONES,
    area_layers=nws.area_layers,
)

JMA = Network(
    id=jma.NETWORK,
    name="Japan Meteorological Agency",
    attribution="出典：気象庁ホームページ (Japan Meteorological Agency)",
    license="PDL-1.0",
    license_url="https://www.jma.go.jp/jma/kishou/info/coment.html",
    url="https://www.jma.go.jp/bosai/warning/",
    sources=(jma.SOURCE,),
    fetch=jma.fetch,
    read=jma.read,
    areas=JMA_CLASS20,
    area_layers=lambda line: ("class20",),
)

ECCC = Network(
    id=eccc.NETWORK,
    name="Environment and Climate Change Canada",
    attribution="Contains information licensed under the Open Government Licence – Canada (Environment and Climate Change Canada)",
    license="OGL-Canada-2.0",
    license_url="https://open.canada.ca/en/open-government-licence-canada",
    url="https://weather.gc.ca/",
    sources=(eccc.SOURCE,),
    fetch=eccc.fetch,
    read=eccc.read,
    areas=None,
)

METEOALARM = Network(
    id=meteoalarm.NETWORK,
    name="EUMETNET – MeteoAlarm",
    attribution="EUMETNET – MeteoAlarm",
    license="MeteoAlarm-Terms",
    license_url="https://meteoalarm.org/en/live/page/terms-and-conditions",
    url="https://meteoalarm.org/en/live/",
    sources=meteoalarm.SOURCES,
    fetch=meteoalarm.fetch,
    read=meteoalarm.read,
    areas=METEOALARM_AREAS,
    area_layers=lambda line: METEOALARM_AREAS.layers,
    areas_beside_geometry=True,
)
"""Reused under MeteoAlarm's Terms and Conditions §5: CC BY 4.0 with
further requirements (credit, issue time, link, disclaimer, delay) that
``docs/alerts.md`` §10.4 lists."""

CMA = Network(
    id=cma.NETWORK,
    name="China Meteorological Administration",
    attribution="China Meteorological Administration, via the WMO Severe Weather Information Centre",
    license="WMO-SWIC",
    license_url="https://severeweather.wmo.int/note.html",
    url="https://www.12379.cn/",
    sources=(cma.SOURCE,),
    fetch=cma.fetch,
    read=cma.read,
    areas=CMA_AREAS,
    area_layers=lambda line: CMA_AREAS.layers,
)
"""The warning signals of China's meteorological observatories as SWIC
aggregates them for WMO (``docs/alerts.md`` §10.5)."""

HKO = Network(
    id=hko.NETWORK,
    name="Hong Kong Observatory",
    attribution="Hong Kong Observatory, HKSAR Government (DATA.GOV.HK)",
    license="DATA.GOV.HK",
    license_url="https://data.gov.hk/en/terms-and-conditions",
    url="https://www.hko.gov.hk/en/wxinfo/dailywx/wxwarntoday.htm",
    sources=(hko.SOURCE,),
    fetch=hko.fetch,
    read=hko.read,
    areas=CMA_AREAS,
    area_layers=lambda line: CMA_AREAS.layers,
)

NETWORKS: tuple[Network, ...] = (NWS, JMA, ECCC, METEOALARM, CMA, HKO)

AREA_SETS: dict[str, AreaSet] = {area_set.set: area_set for area_set in (NWS_ZONES, JMA_CLASS20, METEOALARM_AREAS, CMA_AREAS)}


def network_by_id(network_id: str) -> Network | None:
    return next((network for network in NETWORKS if network.id == network_id), None)

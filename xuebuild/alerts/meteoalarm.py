"""The ``meteoalarm`` network: the awareness warnings Europe's national
meteorological services publish through MeteoAlarm (EUMETNET), from one
JSON feed per country.

Each ``feeds-<country>`` document is that country's warnings in force as
CAP 1.2 under the MeteoAlarm CAP profile: one alert per warning, one
``info`` block per language. The feeds are slow (Germany's 1.5 MB takes
half a minute), uncompressed and carry no validators, so every country is
fetched on every round, a few at a time, each with its own deadline, and
each fails alone: a country whose feed did not arrive keeps its previous
round's warnings (``NetworkRead.carry``) while the others are read anew.

A feed lists many warnings that have already expired; they are dropped
here. A cancelled warning simply leaves the feed (the profile does not
put ``Cancel`` messages in it), so a warning that leaves ends as ``gone``.
Green (``1; green; Minor``, no particular awareness required) is not a
warning and is not published. The level and category are the profile's
``awareness_level`` and ``awareness_type`` codes, which the services set
with more care than CAP ``severity``.

Countries name their areas differently: most by geocode (MeteoAlarm's
``EMMA_ID``, Germany's DWD warn cells, NUTS regions), some draw a polygon
per area (Switzerland, Norway, the United Kingdom …). An area's polygon is
its geometry; an area without one is its geocode, ``<type>:<value>``
(``WARNCELLID:501000005``, ``EMMA_ID:IT001``, ``NUTS3:FR711``). The
``meteoalarm-areas`` set draws Germany's warn cells (DWD) and
France's NUTS 3 départements (IGN); ``EMMA_ID`` codes have no openly
licensed boundaries yet and stay unresolved. Polygon and code areas are
disjoint parts of the warned area, so the build boxes a line by both
(``Network.areas_beside_geometry``).

Israel's feed is not read (it was a third of every round's bytes and all
of its wall clock).

Terms: MeteoAlarm Terms and Conditions §5 — CC BY 4.0 equivalent, with the
credit, issue time, link and disclaimer ``docs/alerts.md`` §10.4 gives.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from ..errors import XueError
from .alert import NetworkRead, network_directory, parse_utc, texts, utc
from .geometry import normalize
from .http import get
from .schema import LANGUAGE, raw_directory, sha256_hex, write_bytes_atomic
from .tables import meteoalarm_category, meteoalarm_level

LOG = logging.getLogger(__name__)

NETWORK = "meteoalarm"
URL = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-{country}"

COUNTRIES: dict[str, str] = {
    "andorra": "AD",
    "austria": "AT",
    "belgium": "BE",
    "bosnia-herzegovina": "BA",
    "bulgaria": "BG",
    "croatia": "HR",
    "cyprus": "CY",
    "czechia": "CZ",
    "denmark": "DK",
    "estonia": "EE",
    "finland": "FI",
    "france": "FR",
    "germany": "DE",
    "greece": "GR",
    "hungary": "HU",
    "iceland": "IS",
    "ireland": "IE",
    "italy": "IT",
    "latvia": "LV",
    "lithuania": "LT",
    "luxembourg": "LU",
    "malta": "MT",
    "moldova": "MD",
    "montenegro": "ME",
    "netherlands": "NL",
    "norway": "NO",
    "poland": "PL",
    "portugal": "PT",
    "republic-of-north-macedonia": "MK",
    "romania": "RO",
    "serbia": "RS",
    "slovakia": "SK",
    "slovenia": "SI",
    "spain": "ES",
    "sweden": "SE",
    "switzerland": "CH",
    "ukraine": "UA",
    "united-kingdom": "GB",
}
"""The feeds read, by the feed's country name, with the country's ISO 3166
code (``native.country``)."""

SOURCE_PREFIX = "meteoalarm-"
SOURCES: tuple[str, ...] = tuple(f"{SOURCE_PREFIX}{country}" for country in COUNTRIES)

WORKERS = 8
"""Feeds fetched at once. The slowest feed sets a round's wall clock
whatever the pool, and eight keep the rest inside it."""
FEED_TIMEOUT = 30
"""Seconds a socket read may stall."""
FEED_DEADLINE = 120
"""Seconds one feed may take, retries included, before it counts as
failed and its country carries over: a round must reach the bucket well
inside MeteoAlarm's ten-minute bound."""

GEOCODE_PREFERENCE = ("WARNCELLID", "EMMA_ID")
"""Of an area's geocodes, the one its code is made of: the first of these
it has, else its first. Germany gives both a warn cell, which the boundary
set draws, and an ``EMMA_ID``; elsewhere MeteoAlarm's own ``EMMA_ID`` is
the code, and a country that gives none names a NUTS region (France,
Bulgaria, Romania, Hungary …)."""


def _feed_filename(country: str) -> str:
    return f"{country}.json"


def _download(country: str) -> bytes:
    url = URL.format(country=country)
    response = get(url, accept="application/json", timeout=FEED_TIMEOUT, pace=False, max_elapsed=FEED_DEADLINE)
    if response.body is None:
        raise XueError(f"{url}: HTTP {response.status}")
    payload = json.loads(response.body)
    if not isinstance(payload, dict) or not isinstance(payload.get("warnings"), list):
        raise XueError(f"{url}: not a MeteoAlarm feed")
    return response.body


def fetch(raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime) -> list[SourceStatus]:
    """Every country's feed into ``<round>/meteoalarm/<country>.json``, a
    status each. A feed that has not arrived by :data:`FEED_DEADLINE` (and
    a little) is a failure; its thread is left to finish on its own."""
    directory = network_directory(raw_root, moment, NETWORK)
    relative = f"{raw_directory(moment)}/{NETWORK}"
    statuses: dict[str, SourceStatus] = {}
    started = time.monotonic()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="meteoalarm")
    futures = {pool.submit(_download, country): country for country in COUNTRIES}
    try:
        for future in concurrent.futures.as_completed(futures, timeout=FEED_DEADLINE * 2 + 30):
            country = futures[future]
            source, url = f"{SOURCE_PREFIX}{country}", URL.format(country=country)
            try:
                body = future.result()
            except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
                LOG.warning("alerts meteoalarm %s: %s", country, exc)
                statuses[country] = SourceStatus(source, False, fetched=now, url=url, error=str(exc))
                continue
            # Written as each feed lands, so no more than the pool's
            # answers are held at once.
            write_bytes_atomic(directory / _feed_filename(country), body)
            statuses[country] = SourceStatus(source, True, fetched=now, url=url, file=f"{relative}/{_feed_filename(country)}")
    except concurrent.futures.TimeoutError:
        LOG.warning("alerts meteoalarm: %d feeds still running after %.0f s", len(COUNTRIES) - len(statuses), time.monotonic() - started)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for country in COUNTRIES:
        if country not in statuses:
            source = f"{SOURCE_PREFIX}{country}"
            statuses[country] = SourceStatus(source, False, fetched=now, url=URL.format(country=country), error="timed out")
            stale = directory / _feed_filename(country)
            if stale.exists():
                stale.unlink()
    return [statuses[country] for country in COUNTRIES]


def _parameters(info: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for parameter in info.get("parameter") or []:
        if isinstance(parameter, dict) and isinstance(parameter.get("valueName"), str) and isinstance(parameter.get("value"), str):
            found.setdefault(parameter["valueName"], parameter["value"])
    return found


def _ring(polygon: str) -> list[list[float]] | None:
    """A CAP polygon, ``lat,lon lat,lon …`` → a GeoJSON ring of
    ``[lon, lat]``; None for one that does not parse."""
    ring: list[list[float]] = []
    for pair in polygon.split():
        parts = pair.split(",")
        if len(parts) != 2:
            return None
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        ring.append([lon, lat])
    return ring if len(ring) >= 4 else None


def _area_code(area: dict[str, Any]) -> str | None:
    """An area's code, ``<type>:<value>`` of one of its geocodes
    (:data:`GEOCODE_PREFERENCE`); a code the boundary set does not hold
    leaves the line's box null rather than short of the area."""
    geocodes = [
        (geocode["valueName"].strip(), geocode["value"].strip())
        for geocode in area.get("geocode") or []
        if isinstance(geocode, dict) and isinstance(geocode.get("valueName"), str) and isinstance(geocode.get("value"), str)
    ]
    geocodes = [(kind, value) for kind, value in geocodes if kind and value]
    for wanted in GEOCODE_PREFERENCE:
        for kind, value in geocodes:
            if kind == wanted:
                return f"{kind}:{value}"
    return f"{geocodes[0][0]}:{geocodes[0][1]}" if geocodes else None


def _areas(info: dict[str, Any], use_polygon: bool) -> tuple[list[str], list[list[list[list[float]]]], list[str]]:
    """An info block's areas → the codes of those drawn by code, the
    polygons of those drawn by polygon, and every area's description."""
    codes: list[str] = []
    polygons: list[list[list[list[float]]]] = []
    descriptions: list[str] = []
    for area in info.get("area") or []:
        if not isinstance(area, dict):
            continue
        if isinstance(area.get("areaDesc"), str) and area["areaDesc"].strip():
            descriptions.append(area["areaDesc"].strip())
        rings = [ring for polygon in area.get("polygon") or [] if isinstance(polygon, str) for ring in [_ring(polygon)] if ring]
        code = _area_code(area)
        if rings and (use_polygon or code is None):
            polygons.extend([ring] for ring in rings)
        elif code is not None:
            codes.append(code)
    return list(dict.fromkeys(codes)), polygons, descriptions


def references(alert: dict[str, Any]) -> list[str]:
    """The alerts a message's CAP ``references`` name (space-separated
    ``sender,identifier,sent`` triples), as product ids."""
    value = alert.get("references")
    if not isinstance(value, str):
        return []
    found = []
    for triple in value.split():
        parts = triple.split(",")
        if len(parts) >= 2 and parts[1]:
            found.append(f"{NETWORK}:{parts[1]}")
    return found


def convert(warning: dict[str, Any], country: str, moment: datetime) -> dict[str, Any] | None:
    """One feed entry in force → a network-file line; None for one that
    is not (expired, green, not Actual, a cancel) or not a warning at all."""
    alert = warning.get("alert") if isinstance(warning, dict) else None
    if not isinstance(alert, dict):
        return None
    identifier = alert.get("identifier")
    sent = utc(alert.get("sent"))
    infos = [info for info in alert.get("info") or [] if isinstance(info, dict)]
    if not isinstance(identifier, str) or not identifier or sent is None or not infos:
        return None
    if str(alert.get("status", "")).lower() != "actual" or str(alert.get("msgType", "")).lower() == "cancel":
        return None
    primary = infos[0]
    event = primary.get("event")
    if not isinstance(event, str) or not event.strip():
        return None
    expires = max((stamp for info in infos for stamp in [parse_utc(info.get("expires"))] if stamp), default=None)
    if expires is not None and expires <= moment:
        return None
    parameters = _parameters(primary)
    level = meteoalarm_level(parameters.get("awareness_level"))
    if level is None:
        return None
    use_polygon = parameters.get("use_polygon_over_geocode", "true").strip().lower() != "false"
    codes, polygons, descriptions = _areas(primary, use_polygon)
    languages: dict[str, dict[str, str]] = {}
    for info in infos:
        language = info.get("language")
        if not isinstance(language, str) or not LANGUAGE.match(language) or language in languages:
            continue
        _, _, area_names = _areas(info, use_polygon)
        text = texts(
            headline=info.get("headline"),
            description=info.get("description"),
            instruction=info.get("instruction"),
            area="; ".join(area_names),
        )
        if text:
            languages[language] = text
    line: dict[str, Any] = {
        "id": f"{NETWORK}:{identifier}",
        "network": NETWORK,
        "category": meteoalarm_category(parameters.get("awareness_type")),
        "level": level,
        "native": {
            "event": event.strip(),
            "code": parameters.get("awareness_type"),
            "level": parameters.get("awareness_level"),
            "severity": primary.get("severity") if isinstance(primary.get("severity"), str) else None,
            "urgency": primary.get("urgency") if isinstance(primary.get("urgency"), str) else None,
            "certainty": primary.get("certainty") if isinstance(primary.get("certainty"), str) else None,
            "msgType": alert.get("msgType") if isinstance(alert.get("msgType"), str) else None,
            "country": COUNTRIES[country],
        },
        "sent": sent,
        "onset": utc(primary.get("onset")) or utc(primary.get("effective")),
        "expires": utc(expires.isoformat()) if expires is not None else None,
        "status": "actual",
        "texts": languages,
        "areas": codes,
        "geometry": normalize({"type": "MultiPolygon", "coordinates": polygons}) if polygons else None,
        "url": primary.get("web") if isinstance(primary.get("web"), str) and primary["web"].startswith("http") else None,
        "sender": primary.get("senderName") if isinstance(primary.get("senderName"), str) and primary["senderName"] else None,
    }
    replaces = sorted(set(references(alert)) - {line["id"]})
    if replaces:
        line["replaces"] = replaces
    return line


def read(raw_root: Path, moment: datetime, statuses: dict[str, SourceStatus]) -> NetworkRead:
    """Each country that arrived, read anew; each that did not, carried
    over from the previous round by its ``native.country``. Not ``ok``
    only when no country arrived."""
    alerts: dict[str, dict[str, Any]] = {}
    latest: datetime | None = None
    failed: set[str] = set()
    for country, code in COUNTRIES.items():
        status = statuses.get(f"{SOURCE_PREFIX}{country}")
        if status is None or not status.ok or not status.file:
            failed.add(code)
            continue
        try:
            body = (raw_root / status.file).read_bytes()
            payload = json.loads(body)
            warnings = payload.get("warnings") if isinstance(payload, dict) else None
            if not isinstance(warnings, list):
                raise ValueError(f"{status.file} is not a MeteoAlarm feed")
        except (OSError, ValueError) as exc:
            LOG.warning("alerts meteoalarm %s: read failed: %s", country, exc)
            status.ok = False
            status.error = f"parse: {exc}"
            failed.add(code)
            continue
        skipped = 0
        for warning in warnings:
            alert = warning.get("alert") if isinstance(warning, dict) else None
            stamp = parse_utc(alert.get("sent")) if isinstance(alert, dict) else None
            if stamp is not None and (latest is None or stamp > latest):
                latest = stamp
            line = convert(warning, country, moment)
            if line is None:
                skipped += 1
                continue
            alerts.setdefault(line["id"], line)
        status.detail.update({"reports": len(warnings), "skipped": skipped, "sha256": sha256_hex(body)})
        del body, payload, warnings
    if len(failed) == len(COUNTRIES):
        return NetworkRead([], False)
    return NetworkRead(
        list(alerts.values()),
        True,
        latest=latest,
        carry=(lambda line: line.get("native", {}).get("country") in failed) if failed else None,
    )

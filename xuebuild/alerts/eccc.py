"""The ``eccc`` network: Environment and Climate Change Canada's public
weather alerts, from the MSC GeoMet OGC API collection ``weather-alerts``.

The collection is every alert in force nationwide as GeoJSON, one feature
per alert and forecast region, each with the region's polygon and the
alert's English and French texts; it is the Datamart's CAP files already
joined to their areas. One feature is one alert here — its ``id`` is the
CAP identifier and the region, so the texts and the polygon stay
together — and ``areas`` is always empty: every alert draws its own area,
so the network needs no boundary set.

The colour is MSC's risk colour (yellow, orange, red), the level where
there is one. Features whose status is ``ended`` or ``cancelled`` are not
in force; they end as ``cancelled``.

Licence: Open Government Licence – Canada.
"""

from __future__ import annotations

import json
import logging
import urllib.error
from datetime import datetime
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from ..errors import XueError
from .alert import NetworkRead, network_directory, parse_utc, texts, utc
from .geometry import simplify
from .http import get
from .schema import round_directory, write_bytes_atomic
from .tables import eccc_category, eccc_level

LOG = logging.getLogger(__name__)

NETWORK = "eccc"
SOURCE = "eccc-alerts"
URL = "https://api.weather.gc.ca/collections/weather-alerts/items?f=json&limit=1000"
PAGE_PATTERN = "page-{:03d}.json"
MAX_PAGES = 20
"""A storm day is a few hundred features, one page; the cap only stops a
pagination loop."""

NOT_IN_FORCE = frozenset({"ended", "cancelled"})


def _next_link(payload: Any) -> str | None:
    links = payload.get("links") if isinstance(payload, dict) else None
    for link in links if isinstance(links, list) else []:
        if isinstance(link, dict) and link.get("rel") == "next" and isinstance(link.get("href"), str):
            return link["href"]
    return None


def fetch(raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime) -> list[SourceStatus]:
    directory = network_directory(raw_root, moment, NETWORK)
    relative = f"{round_directory(moment)}/{NETWORK}"
    url: str | None = URL
    pages: list[bytes] = []
    try:
        while url is not None:
            if len(pages) >= MAX_PAGES:
                raise XueError(f"{URL}: more than {MAX_PAGES} pages")
            response = get(url, accept="application/geo+json")
            if response.body is None:
                raise XueError(f"{url}: HTTP {response.status}")
            pages.append(response.body)
            url = _next_link(json.loads(response.body))
    except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("alerts eccc: %s", exc)
        return [SourceStatus(SOURCE, False, fetched=now, url=URL, error=str(exc))]
    for stale in directory.glob("page-*.json") if directory.is_dir() else []:
        stale.unlink()
    for number, body in enumerate(pages, start=1):
        write_bytes_atomic(directory / PAGE_PATTERN.format(number), body)
    return [SourceStatus(SOURCE, True, fetched=now, url=URL, file=relative, detail={"pages": len(pages)})]


def _string(properties: dict[str, Any], key: str) -> str | None:
    value = properties.get(key)
    return value if isinstance(value, str) and value else None


def convert(feature: dict[str, Any]) -> dict[str, Any] | None:
    identifier = feature.get("id")
    properties = feature.get("properties")
    if not isinstance(identifier, str) or not identifier or not isinstance(properties, dict):
        return None
    event = _string(properties, "alert_name_en") or _string(properties, "alert_name_fr")
    sent = utc(properties.get("publication_datetime"))
    if event is None or sent is None:
        return None
    if (_string(properties, "status_en") or "").lower() in NOT_IN_FORCE:
        return None
    colour = _string(properties, "risk_colour_en")
    alert_type = _string(properties, "alert_type")
    languages = {
        language: texts(
            headline=properties.get(f"alert_name_{language}"),
            description=properties.get(f"alert_text_{language}"),
            area=properties.get(f"feature_name_{language}"),
        )
        for language in ("en", "fr")
    }
    return {
        "id": f"{NETWORK}:{identifier}",
        "network": NETWORK,
        "category": eccc_category(_string(properties, "alert_short_name_en")),
        "level": eccc_level(colour, alert_type),
        "native": {
            "event": event,
            "code": _string(properties, "alert_code"),
            "level": colour,
            "type": alert_type,
            "impact": _string(properties, "impact_en"),
            "confidence": _string(properties, "confidence_en"),
            "status": _string(properties, "status_en"),
            "province": _string(properties, "province"),
            "ends": utc(properties.get("event_end_datetime")),
        },
        "sent": sent,
        "onset": utc(properties.get("validity_datetime")),
        "expires": utc(properties.get("expiration_datetime")),
        "status": "actual",
        "texts": {language: text for language, text in languages.items() if text},
        "areas": [],
        "geometry": simplify(feature.get("geometry")),
        "url": None,
        "sender": None,
    }


def read(raw_root: Path, moment: datetime, statuses: dict[str, SourceStatus]) -> NetworkRead:
    status = statuses.get(SOURCE)
    if status is None or not status.ok or not status.file:
        return NetworkRead([], False)
    directory = raw_root / status.file
    alerts: dict[str, dict[str, Any]] = {}
    cancelled: set[str] = set()
    latest: datetime | None = None
    reports = skipped = 0
    for path in sorted(directory.glob("page-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(features, list):
            raise ValueError(f"{path.name} is not a FeatureCollection")
        stamp = parse_utc(payload.get("timeStamp"))
        if stamp is not None and (latest is None or stamp > latest):
            latest = stamp
        for feature in features:
            reports += 1
            if not isinstance(feature, dict):
                skipped += 1
                continue
            properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
            if isinstance(feature.get("id"), str) and str(properties.get("status_en", "")).lower() in NOT_IN_FORCE:
                cancelled.add(f"{NETWORK}:{feature['id']}")
            line = convert(feature)
            if line is None:
                skipped += 1
                continue
            alerts.setdefault(line["id"], line)
    status.detail.update({"reports": reports, "skipped": skipped})
    return NetworkRead(list(alerts.values()), True, latest=latest, cancelled=cancelled - set(alerts))

"""The ``nws`` network: every alert the US National Weather Service has in
force, from one request to ``api.weather.gov/alerts/active``.

The API answers with a GeoJSON FeatureCollection of CAP 1.2 alerts. Most
name their areas only, by UGC code (``geocode.UGC``: public forecast
zones ``OKZ025``, counties ``OKC109``, marine zones ``ANZ639``), which the
``nws-zones`` boundary set draws; storm-based warnings (tornado, severe
thunderstorm, flash flood, special marine) and some flood products also
carry a polygon, which is the warned area and is kept inline. The service
requires a User-Agent with contact details and states no rate limit; one
request every five minutes is far inside what it expects.

An alert is in force until its event ``ends``, not until its message
``expires``: the API keeps listing a long-running marine or flood alert
whose last message expired days ago (a third of the alerts on
2026-10-31), so ``expires`` here is ``ends`` where the alert states one,
and the message's own expiry stays in ``native.expires``.

A ``Cancel`` message is not in force: the alerts it references end as
``cancelled``. An ``Update`` replaces the alert it references under a new
identifier, so the old one simply leaves the snapshot. Test and exercise
messages are filtered by the query and again here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from .alert import NetworkRead, fetch_document, network_directory, parse_utc, read_json, texts, utc
from .geometry import simplify
from .schema import round_directory
from .tables import nws_category, nws_level

LOG = logging.getLogger(__name__)

NETWORK = "nws"
SOURCE = "nws-alerts"
URL = "https://api.weather.gov/alerts/active?status=actual"
FILENAME = "active.json"


def fetch(raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime) -> list[SourceStatus]:
    destination = network_directory(raw_root, moment, NETWORK) / FILENAME
    relative = f"{round_directory(moment)}/{NETWORK}/{FILENAME}"
    return [fetch_document(SOURCE, URL, destination, relative, now=now, accept="application/geo+json")]


def _first(values: object) -> str | None:
    if isinstance(values, list) and values and isinstance(values[0], str):
        return values[0]
    return None


def _unique(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def convert(feature: dict[str, Any]) -> dict[str, Any] | None:
    """One feature in force → a network-file line; None for one that is
    not (a test, a cancel) or not an alert at all."""
    properties = feature.get("properties")
    if not isinstance(properties, dict):
        return None
    identifier = properties.get("id")
    event = properties.get("event")
    sent = utc(properties.get("sent"))
    if not isinstance(identifier, str) or not identifier or not isinstance(event, str) or not event or sent is None:
        return None
    if properties.get("status") != "Actual" or properties.get("messageType") == "Cancel":
        return None
    severity = properties.get("severity") if isinstance(properties.get("severity"), str) else None
    codes = properties.get("eventCode") if isinstance(properties.get("eventCode"), dict) else {}
    parameters = properties.get("parameters") if isinstance(properties.get("parameters"), dict) else {}
    geocode = properties.get("geocode") if isinstance(properties.get("geocode"), dict) else {}
    language = "en"
    return {
        "id": f"{NETWORK}:{identifier}",
        "network": NETWORK,
        "category": nws_category(event),
        "level": nws_level(event, severity),
        "native": {
            "event": event,
            "code": _first(codes.get("NationalWeatherService")),
            "level": severity,
            "urgency": properties.get("urgency") if isinstance(properties.get("urgency"), str) else None,
            "certainty": properties.get("certainty") if isinstance(properties.get("certainty"), str) else None,
            "vtec": _first(parameters.get("VTEC")),
            "expires": utc(properties.get("expires")),
        },
        "sent": sent,
        "onset": utc(properties.get("onset")) or utc(properties.get("effective")),
        "expires": utc(properties.get("ends")) or utc(properties.get("expires")),
        "status": "actual",
        "texts": {
            language: texts(
                headline=properties.get("headline"),
                description=properties.get("description"),
                instruction=properties.get("instruction"),
                area=properties.get("areaDesc"),
            )
        },
        "areas": _unique(geocode.get("UGC")),
        "geometry": simplify(feature.get("geometry")),
        "url": properties.get("@id") if isinstance(properties.get("@id"), str) else None,
        "sender": properties.get("senderName") if isinstance(properties.get("senderName"), str) else None,
    }


def cancelled_by(feature: dict[str, Any]) -> list[str]:
    """The alerts a ``Cancel`` message withdraws."""
    properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    if properties.get("messageType") != "Cancel":
        return []
    references = properties.get("references") if isinstance(properties.get("references"), list) else []
    return [
        f"{NETWORK}:{reference['identifier']}"
        for reference in references
        if isinstance(reference, dict) and isinstance(reference.get("identifier"), str)
    ]


def read(raw_root: Path, moment: datetime, statuses: dict[str, SourceStatus]) -> NetworkRead:
    status = statuses.get(SOURCE)
    payload = read_json(raw_root, status)
    if payload is None:
        return NetworkRead([], False)
    assert status is not None
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        raise ValueError("active.json is not a FeatureCollection")
    alerts: dict[str, dict[str, Any]] = {}
    cancelled: set[str] = set()
    skipped = 0
    for feature in features:
        if not isinstance(feature, dict):
            skipped += 1
            continue
        cancelled.update(cancelled_by(feature))
        line = convert(feature)
        if line is None:
            skipped += 1
            continue
        alerts.setdefault(line["id"], line)
    status.detail.update({"reports": len(features), "skipped": skipped})
    return NetworkRead(list(alerts.values()), True, latest=parse_utc(payload.get("updated")), cancelled=cancelled)

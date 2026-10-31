"""The ``jma`` network: the Japan Meteorological Agency's warnings and
advisories (気象警報・注意報), as the agency's own warning map reads them.

There is no published API. The map page under
``https://www.jma.go.jp/bosai/warning/`` reads ``data/r8/map_time.json``
(``{"latestControlDatetime": …}``, the time of the newest report) and
``data/r8/map.json`` (every forecast office's latest report per element
group, nationwide, about 900 KB). The ``r8`` documents follow the
warning system in force since May 2026 (レベル２注意報 … レベル５特別警報,
with the new レベル４危険警報); the earlier ``data/warning/`` path stopped
updating then.

A report lists, for every municipality-level area (class20, a seven-digit
code) of its office, the kinds in it with a status: 発表 (issued), 継続
(continued), 解除 (lifted), 危険警報から注意報 (a 危険警報 lowered to 注意報)
or 発表警報・注意報はなし (nothing). A kind is in force in an area when it
has a code and its status is anything but 解除. One alert is one (office,
kind) in force anywhere, with the areas it is in force in; the office is
the forecast office's area code from ``jma_offices.json``, matched by the
report's ``publishingOffice`` name. Japanese warnings have no expiry —
they are in force until lifted — and no body beyond the report's
headline.

``map_time.json`` is the change detector: a round whose time equals the
previous round's network ``latest`` skips ``map.json`` and carries the
previous lines over. It is also the health check: the agency's map has no
service commitment and moved once already, so a time that has not
advanced for :data:`STALE_AFTER` makes the source ``ok: false``.

Content of the agency's site is reusable under the Public Data License
v1.0 with the source credited (出典：気象庁ホームページ).
"""

from __future__ import annotations

import json
import logging
import urllib.error
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from ..errors import XueError
from .alert import NetworkRead, network_directory, parse_utc, read_json, texts, utc
from .http import get
from .schema import iso_z, round_directory, write_bytes_atomic
from .tables import jma_kind, jma_kind_of

LOG = logging.getLogger(__name__)

NETWORK = "jma"
SOURCE = "jma-warning"
BASE_URL = "https://www.jma.go.jp/bosai/warning/data/r8"
MAP_TIME_URL = f"{BASE_URL}/map_time.json"
MAP_URL = f"{BASE_URL}/map.json"
PAGE_URL = "https://www.jma.go.jp/bosai/warning/#lang=ja&area_type=offices&area_code={office}"
MAP_TIME_FILENAME = "map_time.json"
MAP_FILENAME = "map.json"

STALE_AFTER = timedelta(hours=6)

LIFTED = "解除"
LOWERED = "危険警報から注意報"
"""A 危険警報 lowered to 注意報: the kind listed is the 注意報 now in force,
and the element's 危険警報 is lifted."""

OFFICES_PATH = Path(__file__).with_name("jma_offices.json")


@cache
def offices_by_name() -> dict[str, str]:
    """``publishingOffice`` → the office's area code. The table is the
    ``offices`` level of ``bosai/common/const/area.json``, committed: 58
    entries that change when the agency reorganises, not between rounds."""
    table = json.loads(OFFICES_PATH.read_text(encoding="utf-8"))
    return {entry["officeName"]: code for code, entry in table.items()}


def parse_map_time(text: str) -> datetime:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"map_time.json is not JSON: {exc}") from exc
    moment = parse_utc(payload.get("latestControlDatetime") if isinstance(payload, dict) else None)
    if moment is None:
        raise ValueError("map_time.json has no latestControlDatetime")
    return moment


def fetch(raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime) -> list[SourceStatus]:
    """``map_time.json``, then ``map.json`` unless the time is the one the
    previous round published (``since``)."""
    directory = network_directory(raw_root, moment, NETWORK)
    relative = f"{round_directory(moment)}/{NETWORK}"
    try:
        response = get(MAP_TIME_URL)
        if response.body is None:
            raise XueError(f"{MAP_TIME_URL}: HTTP {response.status}")
        map_time = parse_map_time(response.body.decode("utf-8"))
        write_bytes_atomic(directory / MAP_TIME_FILENAME, response.body)
    except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("alerts jma: map_time.json: %s", exc)
        return [SourceStatus(SOURCE, False, fetched=now, url=MAP_URL, error=str(exc))]
    detail: dict[str, Any] = {"mapTime": iso_z(map_time)}
    if since is not None and map_time == since and not force:
        detail["unchanged"] = True
        return [SourceStatus(SOURCE, True, fetched=now, url=MAP_URL, file=relative, detail=detail)]
    try:
        response = get(MAP_URL)
        if response.body is None:
            raise XueError(f"{MAP_URL}: HTTP {response.status}")
        json.loads(response.body)
    except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("alerts jma: map.json: %s", exc)
        return [SourceStatus(SOURCE, False, fetched=now, url=MAP_URL, error=str(exc), detail=detail)]
    write_bytes_atomic(directory / MAP_FILENAME, response.body)
    return [SourceStatus(SOURCE, True, fetched=now, url=MAP_URL, file=relative, detail=detail)]


def convert(reports: list[Any]) -> tuple[list[dict[str, Any]], set[str], int]:
    """``map.json`` → the lines in force, the ids lifted, and the count of
    reports or kinds this table cannot place (an unknown office or code)."""
    found: dict[str, dict[str, Any]] = {}
    lifted: set[str] = set()
    skipped = 0
    for report in reports:
        if not isinstance(report, dict):
            skipped += 1
            continue
        office = offices_by_name().get(report.get("publishingOffice"))
        sent = utc(report.get("reportDatetime"))
        warning = report.get("warning") if isinstance(report.get("warning"), dict) else {}
        items = warning.get("class20Items") if isinstance(warning.get("class20Items"), list) else []
        if office is None or sent is None:
            LOG.warning("alerts jma: a report from %r cannot be placed; skipping it", report.get("publishingOffice"))
            skipped += 1
            continue
        headline = report.get("headlineText")
        for item in items:
            area = item.get("areaCode") if isinstance(item, dict) else None
            kinds = item.get("kinds") if isinstance(item, dict) else None
            if not isinstance(area, str) or not isinstance(kinds, list):
                continue
            for entry in kinds:
                code = entry.get("code") if isinstance(entry, dict) else None
                status = entry.get("status") if isinstance(entry, dict) else None
                if not isinstance(code, str):
                    continue
                kind = jma_kind(code)
                if kind is None:
                    skipped += 1
                    continue
                identifier = f"{NETWORK}:{office}-{code}"
                if status == LIFTED:
                    lifted.add(identifier)
                    continue
                if status == LOWERED:
                    urgent = jma_kind_of(kind["element"], 4)
                    if urgent is not None:
                        lifted.add(f"{NETWORK}:{office}-{urgent}")
                line = found.get(identifier)
                if line is None or sent > line["sent"]:
                    previous_areas = line["areas"] if line is not None else []
                    line = {
                        "id": identifier,
                        "network": NETWORK,
                        "category": kind["category"],
                        "level": kind["level"],
                        "native": {"event": kind["ja"], "code": code, "level": str(kind["level"] * 10), "element": kind["element"]},
                        "sent": sent,
                        "onset": None,
                        "expires": None,
                        "status": "actual",
                        "texts": {"ja": texts(headline=headline)},
                        "areas": previous_areas,
                        "geometry": None,
                        "url": PAGE_URL.format(office=office),
                        "sender": report.get("publishingOffice"),
                    }
                    found[identifier] = line
                if area not in line["areas"]:
                    line["areas"].append(area)
    for line in found.values():
        line["areas"].sort()
    return list(found.values()), lifted - set(found), skipped


def read(raw_root: Path, moment: datetime, statuses: dict[str, SourceStatus]) -> NetworkRead:
    status = statuses.get(SOURCE)
    if status is None or not status.ok or not status.file:
        return NetworkRead([], False)
    directory = raw_root / status.file
    map_time = parse_map_time((directory / MAP_TIME_FILENAME).read_text(encoding="utf-8"))
    fetched = status.fetched or moment
    if fetched - map_time > STALE_AFTER:
        status.ok = False
        status.error = f"map_time.json has not advanced since {iso_z(map_time)}"
        LOG.warning("alerts jma: %s", status.error)
        return NetworkRead([], False, latest=map_time)
    if status.detail.get("unchanged"):
        return NetworkRead([], True, latest=map_time, unchanged=True)
    reports = read_json(raw_root, SourceStatus(SOURCE, True, file=f"{status.file}/{MAP_FILENAME}"))
    if not isinstance(reports, list):
        raise ValueError("map.json is not a list of reports")
    alerts, lifted, skipped = convert(reports)
    status.detail.update({"reports": len(reports), "skipped": skipped})
    return NetworkRead(alerts, True, latest=map_time, cancelled=lifted)

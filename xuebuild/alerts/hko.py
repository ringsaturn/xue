"""The ``hko`` network: the warnings and signals the Hong Kong Observatory
has in force, from its open data API on DATA.GOV.HK.

``warnsum`` is the summary, one entry per warning statement in force
(``WRAIN``, ``WTCSGNL`` …) naming its code — the signal or colour where
the warning has several (``WRAINB``, ``TC8NE``, ``WFIRER``) — its action
and its issue, update and expiry times; ``warningInfo`` is each
statement's text. Both are read in English, Traditional and Simplified
Chinese (``lang=en|tc|sc``), six small documents a round.

A warning covers the whole territory, so every line names one area,
``810000`` (the Hong Kong SAR's administrative code), drawn by the
``cma-areas`` set. The API has no per-warning identifier: an alert is
``hko:<code>-<issue time>`` (``hko:WRAINR-20260912T071500Z``), so a
warning issued again later, or raised to another signal or colour, is a
new alert, and the one it follows leaves the snapshot as ``gone``. An
entry whose action is ``CANCEL`` (and the ``CANCEL`` tropical cyclone
code, all signals lowered) is not in force: the alert it names ends as
``cancelled``.

The Observatory's CAP feed (``alerts.weather.gov.hk``) carries the same
warnings with a polygon, but it is website material, whose commercial
reuse needs the Observatory's written permission; the API is published on
DATA.GOV.HK, under its terms of use.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from .alert import NetworkRead, fetch_document, network_directory, parse_utc, read_json, texts
from .schema import iso_z, raw_directory, sha256_hex
from .tables import hko_category, hko_level

LOG = logging.getLogger(__name__)

NETWORK = "hko"
SOURCE = "hko-warnings"
URL = "https://data.weather.gov.hk/weatherAPI/opendata/weather.php?dataType={kind}&lang={lang}"
LANGUAGES = {"en": "en", "tc": "zh-Hant", "sc": "zh-Hans"}
"""The API's ``lang`` → the BCP 47 tag the line's ``texts`` carry."""
KINDS = ("warnsum", "warningInfo")
AREA = "810000"
PAGE = "https://www.hko.gov.hk/en/wxinfo/dailywx/wxwarntoday.htm"
CANCEL = "CANCEL"


def _filename(kind: str, lang: str) -> str:
    return f"{kind}-{lang}.json"


def fetch(raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime) -> list[SourceStatus]:
    """All six documents, or a failure: a round reads them together."""
    directory = network_directory(raw_root, moment, NETWORK)
    relative = f"{raw_directory(moment)}/{NETWORK}"
    for kind in KINDS:
        for lang in LANGUAGES:
            status = fetch_document(SOURCE, URL.format(kind=kind, lang=lang), directory / _filename(kind, lang), relative, now=now)
            if not status.ok:
                return [status]
    return [SourceStatus(SOURCE, True, fetched=now, url=URL.format(kind="warnsum", lang="en"), file=relative)]


def _string(entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _identifier(code: str, issued: datetime) -> str:
    return f"{NETWORK}:{code}-{issued.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _contents(info: Any) -> dict[str, str]:
    """``warningInfo`` → statement code (and sub-type) → its text, the
    paragraphs joined by blank lines."""
    found: dict[str, str] = {}
    details = info.get("details") if isinstance(info, dict) else None
    for detail in details if isinstance(details, list) else []:
        if not isinstance(detail, dict) or not isinstance(detail.get("contents"), list):
            continue
        text = "\n\n".join(part.strip() for part in detail["contents"] if isinstance(part, str) and part.strip())
        for key in (_string(detail, "subtype"), _string(detail, "warningStatementCode")):
            if key and text:
                found.setdefault(key, text)
    return found


def convert(
    statement: str, summaries: dict[str, dict[str, Any]], infos: dict[str, dict[str, str]], moment: datetime
) -> dict[str, Any] | None:
    """One statement of the English summary in force → a network-file
    line, with the other languages' names and texts beside it."""
    entry = summaries["en"].get(statement)
    if not isinstance(entry, dict):
        return None
    code = _string(entry, "code")
    issued = parse_utc(_string(entry, "issueTime"))
    action = _string(entry, "actionCode")
    if code is None or issued is None or code == CANCEL or (action or "").upper() == CANCEL:
        return None
    expires = parse_utc(_string(entry, "expireTime"))
    if expires is not None and expires <= moment:
        return None
    updated = parse_utc(_string(entry, "updateTime")) or issued
    languages: dict[str, dict[str, str]] = {}
    for lang, tag in LANGUAGES.items():
        local = summaries.get(lang, {}).get(statement)
        local = local if isinstance(local, dict) else {}
        name, kind = _string(local, "name"), _string(local, "type")
        headline = f"{name} ({kind})" if name and kind else name
        description = infos.get(lang, {}).get(code) or infos.get(lang, {}).get(statement)
        text = texts(headline=headline, description=description)
        if text:
            languages[tag] = text
    event = _string(entry, "name") or statement
    return {
        "id": _identifier(code, issued),
        "network": NETWORK,
        "category": hko_category(code),
        "level": hko_level(code),
        "native": {
            "event": event,
            "code": code,
            "level": _string(entry, "type"),
            "statement": statement,
            "action": action,
        },
        "sent": iso_z(updated.astimezone(UTC)),
        "onset": iso_z(issued.astimezone(UTC)),
        "expires": iso_z(expires.astimezone(UTC)) if expires is not None else None,
        "status": "actual",
        "texts": languages,
        "areas": [AREA],
        "geometry": None,
        "url": PAGE,
        "sender": "Hong Kong Observatory",
    }


def read(raw_root: Path, moment: datetime, statuses: dict[str, SourceStatus]) -> NetworkRead:
    status = statuses.get(SOURCE)
    if status is None or not status.ok or not status.file:
        return NetworkRead([], False)
    directory = raw_root / status.file
    bodies: list[bytes] = []
    summaries: dict[str, dict[str, Any]] = {}
    infos: dict[str, dict[str, str]] = {}
    for kind in KINDS:
        for lang in LANGUAGES:
            path = directory / _filename(kind, lang)
            bodies.append(path.read_bytes())
            payload = read_json(raw_root, SourceStatus(SOURCE, True, file=f"{status.file}/{path.name}"))
            if kind == "warnsum":
                if not isinstance(payload, dict):
                    raise ValueError(f"{path.name} is not a warning summary")
                summaries[lang] = payload
            else:
                infos[lang] = _contents(payload)
    alerts: dict[str, dict[str, Any]] = {}
    cancelled: set[str] = set()
    latest: datetime | None = None
    skipped = 0
    for statement, entry in summaries["en"].items():
        if not isinstance(entry, dict):
            skipped += 1
            continue
        stamp = parse_utc(_string(entry, "updateTime")) or parse_utc(_string(entry, "issueTime"))
        if stamp is not None and (latest is None or stamp > latest):
            latest = stamp
        line = convert(statement, summaries, infos, moment)
        if line is None:
            skipped += 1
            code, issued = _string(entry, "code"), parse_utc(_string(entry, "issueTime"))
            if code and code != CANCEL and issued is not None and (_string(entry, "actionCode") or "").upper() == CANCEL:
                cancelled.add(_identifier(code, issued))
            continue
        alerts[line["id"]] = line
    status.detail.update({"reports": len(summaries["en"]), "skipped": skipped, "sha256": sha256_hex(*bodies)})
    return NetworkRead(list(alerts.values()), True, latest=latest, cancelled=cancelled - set(alerts))

"""The ``cma`` network: the warning signals China's meteorological
observatories issue (national, provincial, prefecture and county), as the
WMO Severe Weather Information Centre (SWIC, run by the Hong Kong
Observatory for WMO) aggregates them.

The CMA's own feed in the WMO Register of Alerting Authorities
(``http://www.12379.cn/rss/china_rss.xml``) answers 404; SWIC holds the
same CAP messages. ``json/wmo_all.json`` lists every alert in force
worldwide (about 900 KB) with the path of its CAP file; the ``cn-cma-xx``
entries are China's. Each CAP file is one message, in Chinese and English,
whose area is one or more CPEAS geographic codes (twelve digits, the
first six the administrative code of the county, prefecture or province):
no polygon. The codes resolve against ``cma-areas`` by those six digits;
a township code (its last six digits not zero) is drawn by its county.
A prefecture observatory's warning coded to one county of its own
prefecture is drawn as the prefecture (``_issuer_areas``).

A CAP path carries the message's digest, so a file never changes: fetched
files are kept in ``<raw>/alerts-cache/cma/`` across rounds and each round
fetches only the messages it has not seen, then copies the ones in force
into its own directory, which the build reads alone. A message that does
not arrive keeps its previous round's line (``NetworkRead.carry``, by the
line's ``url``).

A warning that is updated is a new message naming the old in
``references``; one that is lifted leaves the list (SWIC lists no
``Cancel``), so it ends as ``gone``. The CAP severity follows the
signal's colour (blue Minor, yellow Moderate, orange Severe, red
Extreme), and that is the level.

Terms: SWIC's notes to users make the information free for re-use by
other websites that say which service issued it: the CMA is credited by
name, SWIC as the channel (``docs/alerts.md`` §10.5).
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import shutil
import urllib.error
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from ..errors import XueError
from ..http import get
from .alert import NetworkRead, network_directory, parse_utc, texts, utc
from .schema import LANGUAGE, iso_z, raw_directory, sha256_hex, write_bytes_atomic
from .tables import cma_category, cma_colour, cma_level

LOG = logging.getLogger(__name__)

NETWORK = "cma"
SOURCE = "cma-swic"
LIST_URL = "https://severeweather.wmo.int/json/wmo_all.json"
CAP_URL = "https://severeweather.wmo.int/v2/cap-alerts/{path}"
FEED = "cn-cma-xx"
"""The SWIC feed directory China's messages are filed under."""

LIST_FILENAME = "wmo_all.json"
CAP_DIRECTORY = "cap"
CACHE = Path("alerts-cache") / NETWORK
"""Under the raw root, beside the rounds: every CAP file fetched, kept
while SWIC still lists it."""

WORKERS = 8
CAP_TIMEOUT = 30
CAP_DEADLINE = 60

CAP_NS = "urn:oasis:names:tc:emergency:cap:1.2"
GEOCODE = "CPEAS Geographic Code"
_PATH = re.compile(rf"^{FEED}/\d{{4}}/\d{{2}}/\d{{2}}/\d{{2}}/\d{{2}}/\d{{2}}-[0-9a-f]{{32}}\.xml$")


def _cap_filename(path: str) -> str:
    """``cn-cma-xx/2026/11/12/01/45/14-<md5>.xml`` → ``20261112014514-<md5>.xml``."""
    parts = path.split("/")
    return "".join(parts[1:6]) + parts[6]


def _listed(payload: Any) -> list[str]:
    """The CAP paths of the China entries a listing holds, in its order."""
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("not a SWIC listing")
    paths = []
    for item in items:
        path = (item.get("url") or item.get("capURL")) if isinstance(item, dict) else None
        if isinstance(path, str) and _PATH.match(path):
            paths.append(path)
    return list(dict.fromkeys(paths))


def _download(path: str) -> bytes:
    url = CAP_URL.format(path=path)
    response = get(url, accept="application/xml", compressed=True, timeout=CAP_TIMEOUT, pace=False, max_elapsed=CAP_DEADLINE)
    if response.body is None:
        raise XueError(f"{url}: HTTP {response.status}")
    ET.fromstring(response.body)
    return response.body


def fetch(raw_root: Path, moment: datetime, since: datetime | None, *, force: bool = False, now: datetime) -> list[SourceStatus]:
    """The listing into ``<round>/cma/wmo_all.json`` and every China
    message it names into ``<round>/cma/cap/``, from the cache where it
    is there. ``new`` counts the messages fetched this round, ``missing``
    those that failed, which carry over at read."""
    directory = network_directory(raw_root, moment, NETWORK)
    relative = f"{raw_directory(moment)}/{NETWORK}"
    try:
        response = get(LIST_URL, accept="application/json", compressed=True)
        if response.body is None:
            raise XueError(f"{LIST_URL}: HTTP {response.status}")
        paths = _listed(json.loads(response.body))
    except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("alerts cma: %s", exc)
        return [SourceStatus(SOURCE, False, fetched=now, url=LIST_URL, error=str(exc))]
    cache = raw_root / CACHE
    cache.mkdir(parents=True, exist_ok=True)
    wanted = {_cap_filename(path): path for path in paths}
    missing: list[str] = []
    todo = [path for name, path in wanted.items() if not (cache / name).exists()]
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="cma") as pool:
        futures = {pool.submit(_download, path): path for path in todo}
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            try:
                write_bytes_atomic(cache / _cap_filename(path), future.result())
            except (XueError, urllib.error.URLError, OSError, ValueError, ET.ParseError) as exc:
                LOG.warning("alerts cma %s: %s", path, exc)
                missing.append(path)
    caps = directory / CAP_DIRECTORY
    if caps.exists():
        shutil.rmtree(caps)
    caps.mkdir(parents=True)
    for name in wanted:
        if (cache / name).exists():
            shutil.copyfile(cache / name, caps / name)
    for stale in cache.iterdir():
        if stale.name not in wanted:
            stale.unlink()
    write_bytes_atomic(directory / LIST_FILENAME, response.body)
    detail: dict[str, Any] = {"new": len(todo) - len(missing), "missing": len(missing)}
    return [SourceStatus(SOURCE, True, fetched=now, url=LIST_URL, file=relative, detail=detail)]


def _q(name: str) -> str:
    return f"{{{CAP_NS}}}{name}"


def _text(element: ET.Element | None, name: str) -> str | None:
    if element is None:
        return None
    value = element.findtext(_q(name))
    return value.strip() if isinstance(value, str) and value.strip() else None


def _area_codes(info: ET.Element) -> tuple[list[str], list[str], list[str]]:
    """An info block's areas → the six-digit codes the boundary set draws,
    the twelve-digit CPEAS codes as given, and the area descriptions."""
    codes: list[str] = []
    geocodes: list[str] = []
    descriptions: list[str] = []
    for area in info.findall(_q("area")):
        description = _text(area, "areaDesc")
        if description:
            descriptions.append(description)
        for geocode in area.findall(_q("geocode")):
            value = _text(geocode, "value")
            if _text(geocode, "valueName") == GEOCODE and value and re.fullmatch(r"\d{12}", value):
                geocodes.append(value)
                codes.append(value[:6])
    return list(dict.fromkeys(codes)), list(dict.fromkeys(geocodes)), descriptions


def _issuer_areas(identifier: str, codes: list[str]) -> list[str]:
    """The areas a prefecture observatory's warning is drawn over.

    The identifier opens with the issuing office's six-digit administrative
    code (``131000…`` for Langfang). Many prefecture observatories code a
    city-wide warning to their seat county alone (Langfang's 大雾 to 安次区
    131002, its text saying 全市), so a warning from a prefecture-level
    office (``XXYY00``, not a province's ``XX0000``) whose one code is a
    county of that same prefecture is drawn as the prefecture. A warning
    naming several counties, or the prefecture itself, keeps its codes."""
    issuer = identifier[:6]
    if not re.fullmatch(r"\d{4}00", issuer) or issuer.endswith("0000"):
        return codes
    if len(codes) == 1 and codes[0] != issuer and codes[0][:4] == issuer[:4]:
        return [issuer]
    return codes


def references(alert: ET.Element) -> list[str]:
    """The messages CAP ``references`` names (space-separated
    ``sender,identifier,sent`` triples), as product ids. The identifier is
    taken second from the end: a CMA sender may itself hold a comma
    (``Yuhang_District_Meteorological_Observatory,_Hangzhou_City``)."""
    value = _text(alert, "references")
    found = []
    for triple in (value or "").split():
        parts = triple.split(",")
        if len(parts) >= 3 and parts[-2]:
            found.append(f"{NETWORK}:{parts[-2]}")
    return found


def convert(body: bytes, path: str, moment: datetime) -> dict[str, Any] | None:
    """One CAP message in force → a network-file line; None for one that
    is not (expired, not Actual, a cancel) or not a warning at all."""
    alert = ET.fromstring(body)
    if alert.tag != _q("alert"):
        return None
    identifier = _text(alert, "identifier")
    sent = utc(_text(alert, "sent"))
    if identifier is None or sent is None:
        return None
    if (_text(alert, "status") or "").lower() != "actual" or (_text(alert, "msgType") or "").lower() == "cancel":
        return None
    infos = alert.findall(_q("info"))
    by_language = {(_text(info, "language") or ""): info for info in infos}
    primary = next((info for language, info in by_language.items() if language.startswith("zh")), infos[0] if infos else None)
    event = _text(primary, "event")
    if primary is None or event is None:
        return None
    expires = parse_utc(_text(primary, "expires"))
    if expires is not None and expires <= moment:
        return None
    codes, geocodes, _ = _area_codes(primary)
    codes = _issuer_areas(identifier, codes)
    languages: dict[str, dict[str, str]] = {}
    for language, info in by_language.items():
        if not LANGUAGE.match(language) or language in languages:
            continue
        _, _, areas = _area_codes(info)
        text = texts(
            headline=_text(info, "headline"),
            description=_text(info, "description"),
            instruction=_text(info, "instruction"),
            area="; ".join(areas),
        )
        if text:
            languages[language] = text
    severity = _text(primary, "severity")
    english = next((info for language, info in by_language.items() if language.startswith("en")), None)
    sender = _text(alert, "sender")
    line: dict[str, Any] = {
        "id": f"{NETWORK}:{identifier}",
        "network": NETWORK,
        "category": cma_category(event),
        "level": cma_level(severity),
        "native": {
            "event": event,
            "code": None,
            "level": severity,
            "colour": cma_colour(severity),
            "eventEn": _text(english, "event"),
            "urgency": _text(primary, "urgency"),
            "certainty": _text(primary, "certainty"),
            "msgType": _text(alert, "msgType"),
            "geocodes": geocodes,
        },
        "sent": sent,
        "onset": utc(_text(primary, "onset")) or utc(_text(primary, "effective")),
        "expires": iso_z(expires.astimezone(UTC)) if expires is not None else None,
        "status": "actual",
        "texts": languages,
        "areas": codes,
        "geometry": None,
        "url": CAP_URL.format(path=path),
        "sender": sender.replace("_", " ") if sender else None,
    }
    replaces = sorted(set(references(alert)) - {line["id"]})
    if replaces:
        line["replaces"] = replaces
    return line


def read(raw_root: Path, moment: datetime, statuses: dict[str, SourceStatus]) -> NetworkRead:
    status = statuses.get(SOURCE)
    if status is None or not status.ok or not status.file:
        return NetworkRead([], False)
    directory = raw_root / status.file
    body = (directory / LIST_FILENAME).read_bytes()
    payload = json.loads(body)
    paths = _listed(payload)
    stamp = payload.get("lastUpdated") if isinstance(payload, dict) else None
    # SWIC writes its UTC times without a zone, "2026-11-12 02:09:20".
    latest = parse_utc(f"{stamp.replace(' ', 'T')}Z") if isinstance(stamp, str) else None
    missing = {CAP_URL.format(path=path) for path in paths if not (directory / CAP_DIRECTORY / _cap_filename(path)).exists()}
    alerts: dict[str, dict[str, Any]] = {}
    skipped = 0
    caps: list[bytes] = []
    for path in paths:
        file = directory / CAP_DIRECTORY / _cap_filename(path)
        if not file.exists():
            continue
        cap = file.read_bytes()
        caps.append(cap)
        try:
            line = convert(cap, path, moment)
        except ET.ParseError as exc:
            LOG.warning("alerts cma %s: %s", path, exc)
            line = None
        if line is None:
            skipped += 1
            continue
        alerts.setdefault(line["id"], line)
    status.detail.update({"reports": len(paths), "skipped": skipped, "sha256": sha256_hex(body, *caps)})
    return NetworkRead(
        list(alerts.values()),
        True,
        latest=latest,
        carry=(lambda line: line.get("url") in missing) if missing else None,
    )

"""Pull the three Aviation Weather Center cache files of one round into
``<raw>/airport.<round>/`` and leave a ``fetch.json`` beside them saying
how it went. The build (:mod:`.build`) reads only these directories — so a
fixture is a fetched directory, and a build never touches the network.

A round is three GETs: the METAR cache (rewritten every minute), the TAF
cache (every ten), and the station table (every day, so it is fetched
conditionally and kept at the raw root, outside any one round). The
service asks for at most a hundred requests a minute, a custom
User-Agent on every request and ``If-Modified-Since`` where it applies;
all three are honoured here, and a round is three requests against a
budget of a thousand.

A source failing is recorded, not raised: ``fetch.json`` carries ``ok:
false`` and the reason, the build carries it into the product's
``sources``, and the other source publishes. The station table is the
exception — a fetch that fails over a copy already on disk stays ``ok``
with the error noted, because yesterday's names are as good as today's.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.error
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import __version__
from ..errors import DownloadError, XueError
from ..fetch import _http_error_code, _request
from ..pointproduct import iso_z, write_bytes_atomic
from .schema import round_directory, round_name

LOG = logging.getLogger(__name__)

SOURCE_IDS = ("awc-metars", "awc-tafs", "awc-stations")

BASE_URL = "https://aviationweather.gov/data/cache"
METARS_URL = f"{BASE_URL}/metars.cache.csv.gz"
TAFS_URL = f"{BASE_URL}/tafs.cache.xml.gz"
STATIONS_URL = f"{BASE_URL}/stations.cache.json"

USER_AGENT = f"xue/{__version__} (+https://github.com/ringsaturn/xue)"
"""The service requires a custom User-Agent and answers a generic one with
a block. It is sent on every request this module makes."""

REQUEST_INTERVAL = 0.6
"""Seconds between requests: the published limit is a hundred a minute."""

METARS_FILENAME = "metars.cache.csv"
TAFS_FILENAME = "tafs.cache.xml"
STATIONS_FILENAME = "airport-stations.json"
STATIONS_RECORD = "airport-stations.fetch.json"
FETCH_FILENAME = "fetch.json"

STATIONS_MAX_AGE = timedelta(hours=24)
"""How long a station table on disk is used without asking again."""

_last_request = 0.0


@dataclass
class SourceStatus:
    """What one source contributed to a round, ok or not — recorded in
    ``fetch.json`` and carried into ``index.json``'s ``sources`` so a
    station missing from the map has a reason beside it."""

    id: str
    ok: bool
    fetched: datetime | None = None
    url: str | None = None
    file: str | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.fetched is not None:
            payload["fetched"] = iso_z(self.fetched)
        if self.url is not None:
            payload["url"] = self.url
        if self.file is not None:
            payload["file"] = self.file
        if self.error is not None:
            payload["error"] = self.error
        payload.update(self.detail)
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SourceStatus:
        fetched = payload.get("fetched")
        known = ("id", "ok", "fetched", "url", "file", "error")
        return cls(
            id=str(payload.get("id")),
            ok=bool(payload.get("ok")),
            fetched=datetime.fromisoformat(fetched) if isinstance(fetched, str) else None,
            url=payload.get("url"),
            file=payload.get("file"),
            error=payload.get("error"),
            detail={key: value for key, value in payload.items() if key not in known},
        )


@dataclass
class FetchRecord:
    """One round's ``fetch.json``: a status per source, each naming its
    file relative to the raw root (the station table sits beside the round
    directories, not inside one)."""

    round: str
    sources: list[SourceStatus]

    def to_json(self) -> dict[str, Any]:
        return {"round": self.round, "sources": [status.to_json() for status in self.sources]}

    def status(self, source_id: str) -> SourceStatus | None:
        return next((status for status in self.sources if status.id == source_id), None)


def round_raw_directory(raw_root: Path, moment: datetime) -> Path:
    return raw_root / round_directory(moment)


def _pace() -> None:
    global _last_request
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _get(url: str, *, modified_since: str | None = None, timeout: float = 60) -> tuple[bytes | None, str | None]:
    """The body and the server's ``Last-Modified``, or ``(None, None)``
    when the file has not changed since ``modified_since``."""
    headers = {"User-Agent": USER_AGENT}
    if modified_since:
        headers["If-Modified-Since"] = modified_since
    _pace()
    try:
        response = _request(url, headers=headers, timeout=timeout, attempts=3, max_elapsed=90)
    except DownloadError as exc:
        if _http_error_code(exc) == 304:
            return None, modified_since
        raise
    with response:
        status = getattr(response, "status", None)
        if status == 304:
            return None, modified_since
        if status != 200:
            raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
        body = response.read()
        last_modified = response.headers.get("Last-Modified") if hasattr(response, "headers") else None
    if url.endswith(".gz"):
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise DownloadError(f"{url} is not gzip: {exc}") from exc
    return body, last_modified


def _fetch_file(url: str, destination: Path, relative: str, source_id: str) -> SourceStatus:
    now = datetime.now(UTC)
    body, _ = _get(url)
    assert body is not None  # only a conditional request can answer 304
    write_bytes_atomic(destination, body)
    return SourceStatus(source_id, True, fetched=now, url=url, file=relative, detail={"bytes": len(body)})


def fetch_stations(raw_root: Path, *, force: bool = False, now: datetime | None = None) -> SourceStatus:
    """The station table, at most once a day. A copy younger than
    :data:`STATIONS_MAX_AGE` is used as it is; otherwise the file is asked
    for conditionally and a ``304`` costs nothing but the request."""
    now = now or datetime.now(UTC)
    destination = raw_root / STATIONS_FILENAME
    record_path = raw_root / STATIONS_RECORD
    record: dict[str, Any] = {}
    if record_path.exists():
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = {}
    fetched = record.get("fetched")
    age: timedelta | None = None
    if isinstance(fetched, str):
        try:
            age = now - datetime.fromisoformat(fetched)
        except ValueError:
            age = None
    if not force and destination.exists() and age is not None and age < STATIONS_MAX_AGE:
        LOG.info("airport: the station table is %s old; not asking again", age)
        return SourceStatus(
            "awc-stations",
            True,
            fetched=datetime.fromisoformat(str(fetched)),
            url=STATIONS_URL,
            file=STATIONS_FILENAME,
            detail={"cached": True, "modified": record.get("modified")},
        )
    modified_since = record.get("modified") if destination.exists() else None
    try:
        body, last_modified = _get(STATIONS_URL, modified_since=modified_since if isinstance(modified_since, str) else None)
    except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
        if destination.exists():
            # A day-old table names the same airports; the round publishes.
            LOG.warning("airport: keeping the station table on disk: %s", exc)
            return SourceStatus(
                "awc-stations",
                True,
                fetched=now,
                url=STATIONS_URL,
                file=STATIONS_FILENAME,
                error=str(exc),
                detail={"cached": True, "modified": modified_since},
            )
        return SourceStatus("awc-stations", False, fetched=now, url=STATIONS_URL, error=str(exc))
    if body is None:
        LOG.info("airport: the station table is unchanged since %s", modified_since)
    else:
        write_bytes_atomic(destination, body)
    write_bytes_atomic(
        record_path,
        json.dumps(
            {
                "url": STATIONS_URL,
                "fetched": iso_z(now),
                "modified": last_modified or modified_since,
                "bytes": destination.stat().st_size,
            },
            indent=2,
        ).encode("utf-8"),
    )
    return SourceStatus(
        "awc-stations",
        True,
        fetched=now,
        url=STATIONS_URL,
        file=STATIONS_FILENAME,
        detail={"cached": body is None, "modified": last_modified or modified_since},
    )


def fetch_round(
    raw_root: Path, moment: datetime, *, force: bool = False, now: datetime | None = None
) -> FetchRecord:
    """Fetch the round's three files, writing ``fetch.json``. A round
    already fetched is left alone unless ``force`` — a rebuild of a round
    must read the bytes that round was built from."""
    directory = round_raw_directory(raw_root, moment)
    record_path = directory / FETCH_FILENAME
    if not force:
        existing = read_fetch_record(raw_root, moment)
        if existing is not None and all(status.ok for status in existing.sources):
            LOG.info("airport %s: already fetched", round_name(moment))
            return existing
    statuses: list[SourceStatus] = []
    for source_id, url, filename in (
        ("awc-metars", METARS_URL, METARS_FILENAME),
        ("awc-tafs", TAFS_URL, TAFS_FILENAME),
    ):
        relative = f"{round_directory(moment)}/{filename}"
        try:
            status = _fetch_file(url, directory / filename, relative, source_id)
            LOG.info("airport %s: fetched %s (%d bytes)", source_id, filename, status.detail["bytes"])
        except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
            LOG.warning("airport %s: %s", source_id, exc)
            status = SourceStatus(source_id, False, fetched=datetime.now(UTC), url=url, error=str(exc))
        statuses.append(status)
    statuses.append(fetch_stations(raw_root, force=force, now=now))
    record = FetchRecord(round_name(moment), statuses)
    write_bytes_atomic(record_path, json.dumps(record.to_json(), indent=2).encode("utf-8"))
    return record


def read_fetch_record(raw_root: Path, moment: datetime) -> FetchRecord | None:
    path = round_raw_directory(raw_root, moment) / FETCH_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sources = [SourceStatus.from_json(status) for status in payload.get("sources", [])]
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return FetchRecord(
            round_name(moment),
            [SourceStatus(source_id, False, error="fetch.json unreadable") for source_id in SOURCE_IDS],
        )
    return FetchRecord(str(payload.get("round", round_name(moment))), sources)

"""Pull every FIRMS file for one round into ``<raw>/fire.<round>/`` and
leave a ``fetch.json`` beside them saying how each source went. The build
(:mod:`.build`) reads only these directories, so a fixture is a fetched
directory and a build never touches the network.

Only the last-24-hour files are fetched: the seven-day ones are 35–60 MB
each and the window is the previous round's files anyway. A source failing
is recorded, not raised.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from ..airport.fetch import FetchRecord, SourceStatus
from ..errors import DownloadError
from ..pointproduct import write_bytes_atomic
from ..synop.http import get
from .schema import round_directory, round_name
from .sensors import SOURCES

LOG = logging.getLogger(__name__)

FETCH_FILENAME = "fetch.json"
TIMEOUT = 180
"""Seconds for one file: a VIIRS day is about 5 MB, and FIRMS is slow at times."""


def source_ids() -> tuple[str, ...]:
    return tuple(source.id for source in SOURCES)


def fetch_round(
    raw_root: Path,
    moment: datetime,
    *,
    sources: tuple[str, ...] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> FetchRecord:
    """Fetch the round's files (or the sources named), writing
    ``fetch.json``. A round already fetched in full is left alone unless
    ``force``: a rebuild must read the bytes the round was built from."""
    if not force:
        existing = read_fetch_record(raw_root, moment)
        if existing is not None and all(status.ok for status in existing.sources):
            LOG.info("fire %s: already fetched", round_name(moment))
            return existing
    directory = round_directory(moment)
    statuses: list[SourceStatus] = []
    for source in SOURCES:
        if sources is not None and source.id not in sources:
            continue
        fetched = now or datetime.now(UTC)
        relative = f"{directory}/{source.raw_name}"
        try:
            response = get(source.url, timeout=TIMEOUT)
            if response.status != 200 or response.body is None:
                raise DownloadError(f"request failed for {source.url}: HTTP {response.status}")
            write_bytes_atomic(raw_root / relative, response.body)
            status = SourceStatus(source.id, True, fetched=fetched, url=source.url, file=relative)
        except (DownloadError, OSError) as exc:
            status = SourceStatus(source.id, False, fetched=fetched, url=source.url, error=str(exc))
        LOG.info("fire %s: %s %s", status.id, "ok" if status.ok else "failed", status.error or "")
        statuses.append(status)
    record = FetchRecord(round_name(moment), statuses)
    write_bytes_atomic(raw_root / directory / FETCH_FILENAME, json.dumps(record.to_json(), indent=2).encode("utf-8"))
    return record


def read_fetch_record(raw_root: Path, moment: datetime) -> FetchRecord | None:
    path = raw_root / round_directory(moment) / FETCH_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        sources = [SourceStatus.from_json(status) for status in payload.get("sources", [])]
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return FetchRecord(
            round_name(moment), [SourceStatus(source, False, error="fetch.json unreadable") for source in source_ids()]
        )
    return FetchRecord(str(payload.get("round", round_name(moment))), sources)

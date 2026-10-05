"""Pull every network's files for one round into ``<raw>/synop.<round>/``
and leave a ``fetch.json`` beside them saying how each source went. The
build (:mod:`.build`) reads only these directories, so a fixture is a
fetched directory and a build never touches the network.

Each network is asked for what it holds after its newest time in the
previous round's index, so a round is usually one or two requests per
network, and a network that was down catches up as far as its own
retention reaches. A source failing is recorded, not raised.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..airport.fetch import FetchRecord, SourceStatus
from ..pointproduct import write_bytes_atomic
from .networks import NETWORKS
from .schema import round_directory, round_name

LOG = logging.getLogger(__name__)

FETCH_FILENAME = "fetch.json"


def source_ids() -> tuple[str, ...]:
    return tuple(source for network in NETWORKS for source in network.sources)


def previous_latest(previous_index: dict[str, Any] | None) -> dict[str, datetime]:
    """Each network's newest published observation time, by id."""
    latest: dict[str, datetime] = {}
    for network in (previous_index or {}).get("networks", []):
        value = network.get("latest")
        if isinstance(value, str):
            latest[network["id"]] = datetime.fromisoformat(value)
    return latest


def fetch_round(
    raw_root: Path,
    moment: datetime,
    *,
    previous_index: dict[str, Any] | None = None,
    networks: tuple[str, ...] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> FetchRecord:
    """Fetch the round from every network (or the ones named), writing
    ``fetch.json``. A round already fetched in full is left alone unless
    ``force``: a rebuild must read the bytes the round was built from."""
    now = now or datetime.now(UTC)
    if not force:
        existing = read_fetch_record(raw_root, moment)
        if existing is not None and all(status.ok for status in existing.sources):
            LOG.info("synop %s: already fetched", round_name(moment))
            return existing
    since = previous_latest(previous_index)
    statuses: list[SourceStatus] = []
    for network in NETWORKS:
        if networks is not None and network.id not in networks:
            continue
        for status in network.fetch(raw_root, moment, since.get(network.id), force=force, now=now):
            LOG.info("synop %s: %s %s", status.id, "ok" if status.ok else "failed", status.error or status.detail)
            statuses.append(status)
    record = FetchRecord(round_name(moment), statuses)
    write_bytes_atomic(
        raw_root / round_directory(moment) / FETCH_FILENAME, json.dumps(record.to_json(), indent=2).encode("utf-8")
    )
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

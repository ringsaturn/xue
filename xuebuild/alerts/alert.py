"""What every network adapter hands the build, and the helpers they share."""

from __future__ import annotations

import json
import logging
import urllib.error
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..airport.fetch import SourceStatus
from ..errors import XueError
from .http import get
from .schema import iso_z, round_directory, write_bytes_atomic

LOG = logging.getLogger(__name__)


@dataclass
class NetworkRead:
    """What an adapter read out of one round's fetched files."""

    alerts: list[dict[str, Any]]
    """Network-file lines, in no particular order, before the build drops
    the expired ones."""
    ok: bool
    """Whether the network's state arrived this round. When not, the
    build carries the previous round's lines over."""
    latest: datetime | None = None
    """The upstream's own time of its newest state."""
    cancelled: set[str] = field(default_factory=set)
    """Ids the upstream lifted or cancelled explicitly this round: an
    alert of the previous round that is gone and named here ended as
    ``cancelled`` rather than ``gone``."""
    unchanged: bool = False
    """The upstream says nothing changed since the previous round: its
    lines carry over as they are, and the network is still ``ok``."""


def network_directory(raw_root: Path, moment: datetime, network: str) -> Path:
    return raw_root / round_directory(moment) / network


def utc(value: object) -> str | None:
    """An upstream ISO 8601 time with any offset → ``…Z`` to the second;
    None for anything that is not one."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return iso_z(moment.astimezone(UTC))


def parse_utc(value: object) -> datetime | None:
    text = utc(value)
    return datetime.fromisoformat(text) if text else None


def fetch_document(
    source: str, url: str, destination: Path, relative: str, *, now: datetime, accept: str | None = None
) -> SourceStatus:
    """One GET into ``destination``; a failure is recorded, not raised."""
    try:
        response = get(url, accept=accept)
        if response.body is None:
            raise XueError(f"{url}: HTTP {response.status}")
        json.loads(response.body)
    except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
        LOG.warning("alerts %s: %s", source, exc)
        return SourceStatus(source, False, fetched=now, url=url, error=str(exc))
    write_bytes_atomic(destination, response.body)
    return SourceStatus(source, True, fetched=now, url=url, file=relative)


def read_json(raw_root: Path, status: SourceStatus | None) -> Any:
    """The JSON file a fetched source names, or None when the source did
    not arrive."""
    if status is None or not status.ok or not status.file:
        return None
    return json.loads((raw_root / status.file).read_text(encoding="utf-8"))


def texts(**fields: object) -> dict[str, str]:
    """The non-empty string fields of one language's texts."""
    return {key: value for key, value in fields.items() if isinstance(value, str) and value.strip()}

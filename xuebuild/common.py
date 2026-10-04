"""Small helpers every layer of the pipeline shares: the run converter,
the manifests and the point products. A leaf module: it imports nothing
from the package, so anything may import it without a cycle."""

from __future__ import annotations

import zlib
from datetime import UTC, datetime


def iso_z(moment: datetime) -> str:
    """An ISO 8601 UTC timestamp to the second, spelled with ``Z``."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def crc32_hex(payload: bytes) -> str:
    """The CRC32 every ``?v=`` and manifest entry carries: 8 lowercase hex."""
    return f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"

"""Small helpers every layer of the pipeline shares: the run converter,
the manifests and the point products. A leaf module: it imports nothing
from the package, so anything may import it without a cycle."""

from __future__ import annotations

import os
import zlib
from datetime import UTC, datetime
from pathlib import Path


def iso_z(moment: datetime) -> str:
    """An ISO 8601 UTC timestamp to the second, spelled with ``Z``."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def crc32_hex(payload: bytes) -> str:
    """The CRC32 every ``?v=`` and manifest entry carries: 8 lowercase hex."""
    return f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Write through a ``.part`` file synced to disk and renamed over the
    target, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)

"""What the point products share: the tropical cyclone product
(``xuebuild/tc``), the soundings and the airports are each a small
pipeline of their own, but they publish through one contract (``docs/tc.md``
§1) — a mutable ``latest-<product>.json`` pointer naming an immutable
directory's ``index.json`` by path, byte length and CRC32, every file
served under ``?v=<crc32>``. The serialisation and the pointer shape live
here so the three cannot drift; each product keeps its own validators.
"""

from __future__ import annotations

import json
import os
import re
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

POINTER_SCHEMA_VERSION = 1
CRC32 = re.compile(r"^[0-9a-f]{8}$")


def crc32_hex(payload: bytes) -> str:
    return f"{zlib.crc32(payload) & 0xFFFFFFFF:08x}"


def encode_json(payload: dict[str, Any]) -> bytes:
    """The one serialisation — compact separators, keys as built, ASCII
    escaped — so a file's CRC32 is a function of its content alone."""
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def pointer_payload(
    product: str, issued: datetime, index_path: str, index_bytes: bytes
) -> dict[str, Any]:
    """The pointer's fields; the product's own validator checks them."""
    return {
        "schemaVersion": POINTER_SCHEMA_VERSION,
        "product": product,
        "issued": iso_z(issued),
        "path": index_path,
        "byteLength": len(index_bytes),
        "crc32": crc32_hex(index_bytes),
    }


def pointer_shape_error(payload: object, product: str) -> str | None:
    """The product-independent checks of a pointer: None when they pass,
    else the message. A product adds its own path rule on top."""
    if not isinstance(payload, dict):
        return f"{product} pointer must be an object"
    if payload.get("schemaVersion") != POINTER_SCHEMA_VERSION:
        return f"{product} pointer schemaVersion must be {POINTER_SCHEMA_VERSION}"
    if payload.get("product") != product:
        return f"{product} pointer product must be {product!r}"
    path = payload.get("path")
    if (
        not isinstance(path, str)
        or not path.endswith("/index.json")
        or path.startswith(("/", "http:", "https:"))
        or ".." in Path(path).parts
    ):
        return f"{product} pointer path must be a relative <issue directory>/index.json path"
    byte_length = payload.get("byteLength")
    if (
        isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
        or byte_length <= 0
    ):
        return f"{product} pointer byteLength must be a positive integer"
    if not isinstance(payload.get("crc32"), str) or not CRC32.match(payload["crc32"]):
        return f"{product} pointer crc32 must be 8 lowercase hex characters"
    return None

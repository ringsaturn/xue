"""What the point products share: the tropical cyclone product
(``xuebuild/tc``), the soundings and the airports are each a small
pipeline of their own, but they publish through one contract (``docs/tc.md``
§1) — a mutable ``latest-<product>.json`` pointer naming an immutable
directory's ``index.json`` by path, byte length and CRC32, every file
served under ``?v=<crc32>``. The serialisation, the pointer shape and the
validators' primitives live here so the three cannot drift; each product
keeps its own validators and raises its own error class through them.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .common import crc32_hex, iso_z, write_bytes_atomic  # noqa: F401 — re-exported for the products

POINTER_SCHEMA_VERSION = 1
CRC32 = re.compile(r"^[0-9a-f]{8}$")
ISSUE_HOUR = re.compile(r"^\d{10}$")

ProductError = type[Exception]
"""A product's own error class (``TcProductError`` …): every check below
raises the one it is handed."""


def encode_json(payload: dict[str, Any]) -> bytes:
    """The one serialisation — compact separators, keys as built, ASCII
    escaped — so a file's CRC32 is a function of its content alone."""
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


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


def parse_issue_hour(error: ProductError, value: str) -> datetime:
    """``2026091206`` → that UTC hour: the issue id of the hourly products."""
    if not ISSUE_HOUR.match(value):
        raise error("issue must be a UTC hour, YYYYMMDDHH")
    try:
        return datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as exc:
        raise error(f"issue is not a valid hour: {value}") from exc


def check_time(error: ProductError, value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise error(f"{label} must be an ISO 8601 UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise error(f"{label} is not a valid timestamp") from exc


def check_number(
    error: ProductError, value: object, label: str, *, minimum: float | None = None, maximum: float | None = None
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error(f"{label} must be a number")
    if math.isnan(value):
        raise error(f"{label} must not be NaN")
    if minimum is not None and value < minimum:
        raise error(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise error(f"{label} must be at most {maximum}")


def check_sources(
    error: ProductError,
    sources: object,
    label: str,
    source_key: re.Pattern[str],
    extra: Callable[[dict[str, Any], str], None],
) -> None:
    """The ``sources[]`` list every index and storm file carries: objects
    with a unique, well-formed ``id``, a boolean ``ok`` and an ``error``
    where not ok. ``extra(status, where)`` checks a product's own fields of
    each entry, in turn, before the next entry is looked at."""
    if not isinstance(sources, list):
        raise error(f"{label} must be a list")
    seen: set[str] = set()
    for index, status in enumerate(sources):
        where = f"{label}[{index}]"
        if not isinstance(status, dict):
            raise error(f"{where} must be an object")
        source_id = status.get("id")
        if not isinstance(source_id, str) or not source_key.match(source_id):
            raise error(f"{where}.id must be a source id")
        if source_id in seen:
            raise error(f"{label} lists {source_id} twice")
        seen.add(source_id)
        if not isinstance(status.get("ok"), bool):
            raise error(f"{where}.ok must be a boolean")
        if not status["ok"] and not isinstance(status.get("error"), str):
            raise error(f"{where} failed without an error message")
        extra(status, where)


def check_pointer(
    error: ProductError, payload: object, product: str, directory: Callable[[datetime], str], mismatch: str
) -> None:
    """The shared pointer checks, then the product's path rule: the path
    names the directory of the issue it says it is (``mismatch`` if not)."""
    message = pointer_shape_error(payload, product)
    if message is not None:
        raise error(message)
    assert isinstance(payload, dict)
    issued = check_time(error, payload.get("issued"), "pointer.issued")
    if Path(payload["path"]).parts[0] != directory(issued):
        raise error(mismatch)


def read_index_file(
    error: ProductError, path: Path, product: str, validate: Callable[[object], None]
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise error(f"cannot read {product} index {path}: {exc}") from exc
    validate(payload)
    return payload


def load_previous_index(
    path: Path | None,
    output_root: Path,
    *,
    product: str,
    pointer_filename: str,
    read_index: Callable[[Path], dict[str, Any]],
    error: ProductError,
    log: logging.Logger,
) -> dict[str, Any] | None:
    """The previous issue's index: the one given, else the one the local
    pointer names, else nothing (a first build, or a fresh checkout). An
    explicitly given index must be valid; one found through the pointer
    that is not is skipped with a warning."""
    if path is not None:
        return read_index(path)
    pointer = output_root / pointer_filename
    if not pointer.exists():
        return None
    try:
        named = json.loads(pointer.read_text(encoding="utf-8")).get("path")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(named, str):
        return None
    index_path = output_root / named
    if not index_path.exists():
        return None
    try:
        return read_index(index_path)
    except error as exc:
        log.warning("%s: ignoring the previous index at %s: %s", product, index_path, exc)
        return None

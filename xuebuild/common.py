"""Small helpers every layer of the pipeline shares: the run converter,
the manifests, the fetchers and the point products. It imports nothing
from the package but ``errors``, so anything may import it without a
cycle."""

from __future__ import annotations

import os
import urllib.parse
import xml.etree.ElementTree as ElementTree
import zlib
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from .errors import DownloadError


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


S3_NAMESPACE = "{http://s3.amazonaws.com/doc/2006-03-01/}"
"""The XML namespace of an S3 ``ListObjectsV2`` response."""


def s3_list_query(
    prefix: str, token: str | None, *, max_keys: int | None = None, delimiter: str | None = None
) -> str:
    """The query string of one unsigned ``list-type=2`` page."""
    query = {"list-type": "2", "prefix": prefix}
    if max_keys is not None:
        query["max-keys"] = str(max_keys)
    if delimiter:
        query["delimiter"] = delimiter
    if token is not None:
        query["continuation-token"] = token
    return urllib.parse.urlencode(query)


def s3_parse_page(payload: str | bytes, label: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise DownloadError(f"{label} is not XML: {exc}") from exc


def s3_continuation(root: ElementTree.Element, label: str, *, strict: bool = True) -> str | None:
    """The page's continuation token, or None on the last page. ``strict``
    refuses a truncated page without a token; otherwise such a page (and
    any ``IsTruncated`` that is not exactly ``true``) ends the listing."""
    if not strict:
        if root.findtext(f"{S3_NAMESPACE}IsTruncated") != "true":
            return None
        return root.findtext(f"{S3_NAMESPACE}NextContinuationToken") or None
    if (root.findtext(f"{S3_NAMESPACE}IsTruncated") or "").lower() != "true":
        return None
    token = root.findtext(f"{S3_NAMESPACE}NextContinuationToken") or None
    if token is None:
        raise DownloadError(f"{label} is truncated but carries no continuation token")
    return token


def s3_list_pages(
    fetch_page: Callable[[str | None], str | bytes],
    label: str,
    *,
    strict: bool = True,
    max_pages: int | None = None,
) -> Iterator[ElementTree.Element]:
    """Every page of a listing, each parsed, following the continuation
    token: ``fetch_page`` takes the token (None for the first page) and
    returns the response body. ``max_pages`` bounds a listing that would
    otherwise never end."""
    token: str | None = None
    pages = 0
    while True:
        if max_pages is not None and pages >= max_pages:
            raise DownloadError(f"{label} did not terminate")
        root = s3_parse_page(fetch_page(token), label)
        pages += 1
        yield root
        token = s3_continuation(root, label, strict=strict)
        if token is None:
            return

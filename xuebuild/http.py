"""The one way a point-product adapter asks an upstream for a file: a
custom User-Agent naming the project and how to reach it (api.weather.gov
refuses requests without one), a polite pause between requests, and a 404
told apart from a failure — a retention running out, or an hour not posted
yet, is an answer, not an error. ``modified_since`` adds
``If-Modified-Since`` and a 304 is an answer too; ``compressed`` asks for
gzip on the wire and hands the body back decompressed."""

from __future__ import annotations

import gzip
import time
from dataclasses import dataclass

from . import __version__
from .errors import DownloadError
from .fetch import _http_error_code, _request

USER_AGENT = f"xue/{__version__} (+https://github.com/ringsaturn/xue)"
REQUEST_INTERVAL = 0.3
"""Seconds between requests. A round is a handful of requests; a day of
synop backfill is 144, which this keeps under a minute and well clear of
looking like a crawl."""

_last_request = 0.0


@dataclass(frozen=True)
class Response:
    status: int
    """200, 304 (unchanged since ``modified_since``) or 404 (not there)."""
    body: bytes | None
    last_modified: str | None = None


def _pace() -> None:
    global _last_request
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def get(
    url: str,
    *,
    modified_since: str | None = None,
    accept: str | None = None,
    compressed: bool = False,
    timeout: float = 60,
    pace: bool = True,
    max_elapsed: float = 90,
) -> Response:
    """Fetch ``url``. Raises :class:`DownloadError` for anything but 200,
    304 and 404. ``pace=False`` skips the pause, for an adapter that bounds
    its own concurrency against one host (the pause is process-wide, not
    per thread)."""
    headers = {"User-Agent": USER_AGENT}
    if compressed:
        headers["Accept-Encoding"] = "gzip"
    if accept:
        headers["Accept"] = accept
    if modified_since:
        headers["If-Modified-Since"] = modified_since
    if pace:
        _pace()
    try:
        response = _request(url, headers=headers, timeout=timeout, attempts=3, max_elapsed=max_elapsed)
    except DownloadError as exc:
        code = _http_error_code(exc)
        if code == 304:
            return Response(304, None, modified_since)
        if code == 404:
            return Response(404, None)
        raise
    with response:
        status = getattr(response, "status", None)
        if status == 304:
            return Response(304, None, modified_since)
        if status != 200:
            raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
        body = response.read()
        headers_in = getattr(response, "headers", None)
        last_modified = headers_in.get("Last-Modified") if headers_in is not None else None
        encoding = headers_in.get("Content-Encoding", "") if headers_in is not None else ""
    if compressed and (encoding.lower() == "gzip" or body[:2] == b"\x1f\x8b"):
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise DownloadError(f"{url}: a gzip body that does not decompress: {exc}") from exc
    return Response(200, body, last_modified)

"""The one way a synop adapter asks a network for a file: a custom
User-Agent, a polite pause between requests, ``If-Modified-Since`` where
the caller has a date, and a 404 told apart from a failure — a network's
retention running out is an answer, not an error."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .. import __version__
from ..errors import DownloadError
from ..fetch import _http_error_code, _request

USER_AGENT = f"xue/{__version__} (+https://github.com/ringsaturn/xue)"
REQUEST_INTERVAL = 0.3
"""Seconds between requests. A round is a handful of requests; a day's
backfill is 144, which this keeps under a minute and well clear of looking
like a crawl."""

_last_request = 0.0


@dataclass(frozen=True)
class Response:
    status: int
    """200, 304 (unchanged since the date given) or 404 (not there)."""
    body: bytes | None
    last_modified: str | None


def _pace() -> None:
    global _last_request
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def get(url: str, *, modified_since: str | None = None, timeout: float = 60) -> Response:
    """Fetch ``url``. Raises :class:`DownloadError` for anything but 200,
    304 and 404."""
    headers = {"User-Agent": USER_AGENT}
    if modified_since:
        headers["If-Modified-Since"] = modified_since
    _pace()
    try:
        response = _request(url, headers=headers, timeout=timeout, attempts=3, max_elapsed=90)
    except DownloadError as exc:
        code = _http_error_code(exc)
        if code == 304:
            return Response(304, None, modified_since)
        if code == 404:
            return Response(404, None, None)
        raise
    with response:
        status = getattr(response, "status", None)
        if status == 304:
            return Response(304, None, modified_since)
        if status != 200:
            raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
        body = response.read()
        last_modified = response.headers.get("Last-Modified") if hasattr(response, "headers") else None
    return Response(200, body, last_modified)

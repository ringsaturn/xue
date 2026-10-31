"""The one way an aq adapter asks a network for a file: a custom
User-Agent, a polite pause between requests, and a 404 told apart from a
failure — an hour a network has not posted yet is an answer, not an
error."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .. import __version__
from ..errors import DownloadError
from ..fetch import _http_error_code, _request

USER_AGENT = f"xue/{__version__} (+https://github.com/ringsaturn/xue)"
REQUEST_INTERVAL = 0.3
"""Seconds between requests. A round is a handful of requests; a first
build reads a day of AirNow hours, 24 files, which this keeps well clear of
looking like a crawl."""

_last_request = 0.0


@dataclass(frozen=True)
class Response:
    status: int
    """200 or 404 (not there)."""
    body: bytes | None
    last_modified: str | None


def _pace() -> None:
    global _last_request
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def get(url: str, *, timeout: float = 60) -> Response:
    """Fetch ``url``. Raises :class:`DownloadError` for anything but 200
    and 404."""
    headers = {"User-Agent": USER_AGENT}
    _pace()
    try:
        response = _request(url, headers=headers, timeout=timeout, attempts=3, max_elapsed=90)
    except DownloadError as exc:
        if _http_error_code(exc) == 404:
            return Response(404, None, None)
        raise
    with response:
        status = getattr(response, "status", None)
        if status != 200:
            raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
        body = response.read()
        last_modified = response.headers.get("Last-Modified") if hasattr(response, "headers") else None
    return Response(200, body, last_modified)

"""The one way an alerts adapter asks a service for a document: a
User-Agent naming the project and how to reach it (api.weather.gov
refuses requests without one), gzip on the wire, a polite pause between
requests, and a 404 told apart from a failure."""

from __future__ import annotations

import gzip
import time
from dataclasses import dataclass

from .. import __version__
from ..errors import DownloadError
from ..fetch import _http_error_code, _request

USER_AGENT = f"xue/{__version__} (+https://github.com/ringsaturn/xue)"
REQUEST_INTERVAL = 0.3
"""Seconds between requests. A round is a handful."""

_last_request = 0.0


@dataclass(frozen=True)
class Response:
    status: int
    """200 or 404 (not there)."""
    body: bytes | None


def _pace() -> None:
    global _last_request
    wait = REQUEST_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def get(url: str, *, accept: str | None = None, timeout: float = 60) -> Response:
    """Fetch ``url``, decompressed. Raises :class:`DownloadError` for
    anything but 200 and 404."""
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if accept:
        headers["Accept"] = accept
    _pace()
    try:
        response = _request(url, headers=headers, timeout=timeout, attempts=3, max_elapsed=90)
    except DownloadError as exc:
        if _http_error_code(exc) == 404:
            return Response(404, None)
        raise
    with response:
        status = getattr(response, "status", None)
        if status != 200:
            raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
        body = response.read()
        encoding = response.headers.get("Content-Encoding", "") if hasattr(response, "headers") else ""
    if encoding.lower() == "gzip" or body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise DownloadError(f"{url}: a gzip body that does not decompress: {exc}") from exc
    return Response(200, body)

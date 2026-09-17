"""The EUMETSAT Data Store: how Meteosat products are found and fetched.

EUMETSAT redistributes nothing on a public bucket; its Data Store is an
OpenSearch catalogue whose **search is anonymous** and whose **downloads
need a bearer token** minted from a registered account's consumer key and
secret (https://api.eumetsat.int/api-key, free). The two are read from the
environment as ``EUMETSAT_CONSUMER_KEY`` / ``EUMETSAT_CONSUMER_SECRET`` —
the workflow's secrets of the same names — and a fetch that needs them and
finds neither fails naming them, so a build without an account stops at
the first download rather than partway through a window.

A product is one repeat cycle of one collection; its ``sip-entries`` are
the files it is made of, each with its own download link. This module
knows the catalogue's shapes and the token dance; which files a slot needs
and what to do with them is the reader's (:class:`readers.FCIReader`).
Every function takes a ``fetch`` / ``download`` callable so the tests
never touch the network, the way the bucket listings do.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..errors import DownloadError

LOG = logging.getLogger(__name__)

SEARCH_URL = "https://api.eumetsat.int/data/search-products/1.0.0/os"
TOKEN_URL = "https://api.eumetsat.int/token"
KEY_VARIABLE = "EUMETSAT_CONSUMER_KEY"
SECRET_VARIABLE = "EUMETSAT_CONSUMER_SECRET"
#: The most products one search page asks for; a day of ten-minute cycles
#: is 144 and a search here spans hours.
PAGE_SIZE = 100
#: How long a rate-limited request waits at most before giving up on the
#: store's ``retryAfter``.
RETRY_AFTER_CAP = 120.0
#: A token is renewed this long before the store says it expires.
TOKEN_MARGIN = 60.0


@dataclass(frozen=True)
class Entry:
    """One file of a product: its name and its own download link."""

    name: str
    href: str


@dataclass(frozen=True)
class Product:
    """One product of a collection: a repeat cycle's files."""

    id: str
    start: datetime
    """The sensing start, from the product's ``date`` interval."""
    end: datetime
    updated: datetime
    """When the store last wrote it: the publication."""
    entries: tuple[Entry, ...]


def credentials(environment: dict[str, str] | None = None) -> tuple[str, str]:
    """The consumer key and secret from the environment, or the error that
    names what to set."""
    environment = os.environ if environment is None else environment
    key = environment.get(KEY_VARIABLE, "").strip()
    secret = environment.get(SECRET_VARIABLE, "").strip()
    if not key or not secret:
        raise DownloadError(
            f"downloading from the EUMETSAT Data Store needs a registered account's {KEY_VARIABLE} and "
            f"{SECRET_VARIABLE} in the environment (https://api.eumetsat.int/api-key)"
        )
    return key, secret


def has_credentials(environment: dict[str, str] | None = None) -> bool:
    try:
        credentials(environment)
    except DownloadError:
        return False
    return True


def search_url(collection: str, start: datetime, end: datetime, *, count: int = PAGE_SIZE, index: int = 0) -> str:
    """The OpenSearch query for a collection's products sensed in
    ``[start, end]``, JSON, one page."""
    query = {
        "format": "json",
        "pi": collection,
        "dtstart": _iso(start),
        "dtend": _iso(end),
        "c": str(count),
        "si": str(index),
    }
    return f"{SEARCH_URL}?{urllib.parse.urlencode(query)}"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_stamp(text: str) -> datetime:
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(UTC)
    except ValueError as exc:
        raise DownloadError(f"the Data Store wrote an unreadable time: {text!r}") from exc


def parse_products(payload: str, collection: str) -> tuple[list[Product], int]:
    """The products of one search page and the total the search holds."""
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"the Data Store search for {collection} is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise DownloadError(f"the Data Store search for {collection} is not a feature collection")
    try:
        total = int(document.get("totalResults", 0))
    except (TypeError, ValueError):
        total = 0
    products: list[Product] = []
    for feature in document.get("features") or []:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties") or {}
        product_id = str(properties.get("identifier") or feature.get("id") or "")
        interval = str(properties.get("date") or "")
        if not product_id or "/" not in interval:
            continue
        start_text, end_text = interval.split("/", 1)
        links = properties.get("links") or {}
        entries = tuple(
            Entry(name=str(entry.get("title") or ""), href=str(entry.get("href") or ""))
            for entry in links.get("sip-entries") or []
            if isinstance(entry, dict) and entry.get("href") and entry.get("title")
        )
        products.append(
            Product(
                id=product_id,
                start=_parse_stamp(start_text),
                end=_parse_stamp(end_text),
                updated=_parse_stamp(str(properties.get("updated") or end_text)),
                entries=entries,
            )
        )
    return products, total


def _fetch_text(url: str) -> str:
    from ..fetch import fetch_text  # noqa: PLC0415 - fetch.py dispatches to this package

    return fetch_text(url)


def search(
    collection: str, start: datetime, end: datetime, *, fetch: Callable[[str], str] | None = None
) -> list[Product]:
    """Every product of the collection sensed in ``[start, end]``, oldest
    first; anonymous, one request per page."""
    fetch = fetch or _fetch_text
    products: list[Product] = []
    index = 0
    while True:
        page, total = parse_products(fetch(search_url(collection, start, end, index=index)), collection)
        products += page
        index += len(page)
        if not page or index >= total:
            break
    return sorted(products, key=lambda product: (product.start, product.updated))


class TokenCache:
    """One bearer token for the process, renewed before it expires and
    when the store refuses it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self, *, opener: Callable[..., object] | None = None, now: Callable[[], float] = time.monotonic) -> str:
        with self._lock:
            if self._token is not None and now() < self._expires_at - TOKEN_MARGIN:
                return self._token
            key, secret = credentials()
            self._token, lifetime = request_token(key, secret, opener=opener)
            self._expires_at = now() + lifetime
            return self._token

    def forget(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0


def request_token(key: str, secret: str, *, opener: Callable[..., object] | None = None) -> tuple[str, float]:
    """``POST /token`` with the consumer key and secret as HTTP Basic: the
    access token and its lifetime in seconds."""
    import urllib.request  # noqa: PLC0415

    opener = opener or urllib.request.urlopen
    basic = base64.b64encode(f"{key}:{secret}".encode()).decode("ascii")
    request = urllib.request.Request(
        TOKEN_URL,
        data=b"grant_type=client_credentials",
        method="POST",
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        response = opener(request, timeout=30)
        with response:
            body = response.read()  # type: ignore[attr-defined]
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"the EUMETSAT Data Store refused the credentials: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DownloadError(f"the EUMETSAT token request failed: {exc}") from exc
    try:
        document = json.loads(body)
        token = str(document["access_token"])
        lifetime = float(document.get("expires_in", 3600))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise DownloadError("the EUMETSAT token response is not the expected JSON") from exc
    if not token:
        raise DownloadError("the EUMETSAT token response carries no access token")
    return token, lifetime


TOKENS = TokenCache()


def retry_after_seconds(body: bytes, *, now: datetime | None = None) -> float | None:
    """The wait a rate-limited (429) answer asks for: the store writes
    ``message.retryAfter`` as an epoch in milliseconds."""
    try:
        document = json.loads(body)
        stamp = float(document["message"]["retryAfter"]) / 1000.0
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    current = (now or datetime.now(UTC)).timestamp()
    return max(0.0, stamp - current)


def download_bytes(
    url: str,
    *,
    opener: Callable[..., object] | None = None,
    tokens: TokenCache = TOKENS,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 4,
) -> bytes:
    """One file of a product, with the bearer token: a refused token is
    renewed once, a rate limit waited out as the store asks (capped), and
    anything else is the error."""
    import urllib.request  # noqa: PLC0415

    from ..fetch import USER_AGENT  # noqa: PLC0415 - fetch.py dispatches to this package

    opener = opener or urllib.request.urlopen
    renewed = False
    last_error: Exception | None = None
    for attempt in range(attempts):
        token = tokens.token(opener=opener)
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT})
        try:
            response = opener(request, timeout=300)
            with response:
                status = getattr(response, "status", 200)
                body = response.read()  # type: ignore[attr-defined]
            if status != 200:
                raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
            return body
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code == 401 and not renewed:
                renewed = True
                tokens.forget()
                continue
            if exc.code == 429 and attempt + 1 < attempts:
                body = exc.read() if hasattr(exc, "read") else b""
                delay = min(retry_after_seconds(body) or 10.0, RETRY_AFTER_CAP)
                LOG.warning("the EUMETSAT Data Store rate-limited %s, waiting %.0f s", url, delay)
                sleep(delay)
                continue
            if exc.code in {500, 502, 503, 504} and attempt + 1 < attempts:
                sleep(min(5.0 * (attempt + 1), 30.0))
                continue
            break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                sleep(min(5.0 * (attempt + 1), 30.0))
                continue
            break
    raise DownloadError(f"download failed for {url}: {last_error}") from last_error

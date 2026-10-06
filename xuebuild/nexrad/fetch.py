"""Listing and downloading NEXRAD Level 3 sweeps from the public
``unidata-nexrad-level3`` bucket, and the network's station table.

The bucket is flat — ``<SITE>_<PRODUCT>_<YYYY>_<MM>_<DD>_<HH>_<MM>_<SS>``,
no directories — so a site's sweeps of one hour are one prefix listing, and
the key's time is the sweep's own start (``docs/nexrad.md`` §2). Files land
under ``<raw>/nexrad/<SITE>/<key>`` and are never fetched twice: a sweep is
immutable once in the bucket.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from ..errors import DownloadError, NexradProductError, XueError
from ..pointproduct import write_bytes_atomic
from ..synop.http import get

LOG = logging.getLogger(__name__)

BUCKET_URL = "https://unidata-nexrad-level3.s3.amazonaws.com"
STATIONS_URL = "https://www.ncei.noaa.gov/access/homr/file/nexrad-stations.txt"
SOURCE_CODES = {"n0b": "N0B", "n0g": "N0G"}
KEY = re.compile(r"^(?P<site>[A-Z0-9]{3})_(?P<code>[A-Z0-9]{3})_(?P<time>\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2})$")
_S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"
DOWNLOAD_WORKERS = 8


@dataclass(frozen=True)
class Station:
    site: str
    """The bucket's three-letter id (``TLX``)."""
    icao: str
    name: str
    latitude: float
    longitude: float
    height_m: float
    """Station elevation above mean sea level. The antenna's own height is
    the product header's; this is the table's fallback."""


def key_time(key: str) -> datetime:
    match = KEY.match(key)
    if match is None:
        raise NexradProductError(f"{key!r} is not a Level 3 key")
    return datetime.strptime(match["time"], "%Y_%m_%d_%H_%M_%S").replace(tzinfo=UTC)


def list_keys(site: str, product: str, start: datetime, end: datetime) -> list[str]:
    """Every key of one site and product with ``start < time ≤ end``,
    oldest first: one listing per hour the interval touches."""
    code = SOURCE_CODES[product]
    keys: list[str] = []
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour <= end:
        prefix = f"{site}_{code}_{hour:%Y_%m_%d_%H}"
        token: str | None = None
        while True:
            url = f"{BUCKET_URL}/?list-type=2&prefix={quote(prefix)}&max-keys=1000"
            if token:
                url += f"&continuation-token={quote(token)}"
            response = get(url)
            if response.body is None:
                raise DownloadError(f"listing {prefix} failed: HTTP {response.status}")
            root = ElementTree.fromstring(response.body)
            for item in root.iter(f"{_S3}Key"):
                key = item.text or ""
                if KEY.match(key) and start < key_time(key) <= end:
                    keys.append(key)
            truncated = root.findtext(f"{_S3}IsTruncated") == "true"
            token = root.findtext(f"{_S3}NextContinuationToken")
            if not truncated or not token:
                break
        hour += timedelta(hours=1)
    return sorted(set(keys))


def published_sites(product: str, day: datetime) -> set[str]:
    """The sites with at least one ``product`` sweep on ``day`` (UTC): the
    bucket's site prefixes, each asked for one key of that day. The terminal
    Doppler radars share the bucket and never publish N0B or N0G, so they
    fall out here."""
    code = SOURCE_CODES[product]
    url = f"{BUCKET_URL}/?list-type=2&delimiter=_&max-keys=1000"
    response = get(url)
    if response.body is None:
        raise DownloadError(f"listing the bucket's sites failed: HTTP {response.status}")
    root = ElementTree.fromstring(response.body)
    prefixes = [item.text or "" for item in root.iter(f"{_S3}Prefix")]
    sites = [prefix[:-1] for prefix in prefixes if re.fullmatch(r"[A-Z0-9]{3}_", prefix)]

    def publishes(site: str) -> str | None:
        prefix = f"{site}_{code}_{day:%Y_%m_%d}"
        listing = get(f"{BUCKET_URL}/?list-type=2&prefix={quote(prefix)}&max-keys=1")
        if listing.body is None:
            raise DownloadError(f"listing {prefix} failed: HTTP {listing.status}")
        return site if ElementTree.fromstring(listing.body).find(f"{_S3}Contents") is not None else None

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        return {site for site in pool.map(publishes, sites) if site is not None}


def raw_path(raw_root: Path, key: str) -> Path:
    return raw_root / "nexrad" / key[:3] / key


def download(raw_root: Path, keys: list[str]) -> tuple[int, list[str]]:
    """Fetch the keys not on disk yet; returns how many were fetched and
    the keys that failed (recorded, not raised: one late object must not
    cost a round)."""
    missing = [key for key in keys if not raw_path(raw_root, key).exists()]

    def one(key: str) -> str | None:
        try:
            response = get(f"{BUCKET_URL}/{key}")
            if response.body is None:
                return key
            write_bytes_atomic(raw_path(raw_root, key), response.body)
            return None
        except (XueError, urllib.error.URLError, OSError) as exc:
            LOG.warning("nexrad: %s: %s", key, exc)
            return key

    with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        failed = [key for key in pool.map(one, missing) if key is not None]
    return len(missing) - len(failed), failed


def parse_stations(text: str) -> dict[str, Station]:
    """NCEI's ``nexrad-stations.txt``: fixed columns, a header and a dashed
    rule, then one station per line. Only ``STNTYPE`` NEXRAD rows are kept,
    keyed by the bucket's three-letter id (the ICAO without its first
    letter)."""
    lines = text.splitlines()
    rule = next((index for index, line in enumerate(lines) if line.startswith("-")), None)
    if rule is None or rule == 0:
        raise NexradProductError("nexrad-stations.txt has no header rule")
    columns = [(match.start(), match.end()) for match in re.finditer(r"-+", lines[rule])]
    names = [lines[rule - 1][start:end].strip() for start, end in columns]
    stations: dict[str, Station] = {}
    for line in lines[rule + 1 :]:
        if not line.strip():
            continue
        row = {name: line[start:end].strip() for name, (start, end) in zip(names, columns)}
        if row.get("STNTYPE") != "NEXRAD":
            continue
        icao = row.get("ICAO", "")
        if len(icao) != 4:
            continue
        try:
            stations[icao[1:]] = Station(
                site=icao[1:],
                icao=icao,
                name=row.get("NAME", ""),
                latitude=float(row["LAT"]),
                longitude=float(row["LON"]),
                height_m=float(row["ELEV"]) * 0.3048,
            )
        except (KeyError, ValueError):
            LOG.warning("nexrad: station row %r is unreadable; skipping it", icao)
    if not stations:
        raise NexradProductError("nexrad-stations.txt lists no NEXRAD station")
    return stations


def fetch_stations(raw_root: Path) -> Path:
    """The station table, fetched once and kept: the network changes a
    site in years, not hours."""
    path = raw_root / "nexrad" / "nexrad-stations.txt"
    if not path.exists():
        response = get(STATIONS_URL)
        if response.body is None:
            raise DownloadError(f"{STATIONS_URL}: HTTP {response.status}")
        write_bytes_atomic(path, response.body)
    return path

"""The WMO WIS2 global caches a CMA product is read from: the Met
Office/NOAA cache and JMA's, listed by run and read by Range, and caches
that cannot be listed, read by name (``docs/contribution/sources.md``,
"cmadust" and "cmageps").

A product's files are one per lead time, named by the producer. Neither
listable cache has held every file of a run on its own, but their union
has, so a lead time is read from the first cache that lists it. The Met
Office/NOAA cache keys a file by its name under a fixed prefix; JMA's puts
a directory named for the file and an upload stamp between the prefix and
the name, so a key is only ever learnt from a listing. The other caches
(CMA's, KMA's, DWD's) answer no listing but serve a file at a URL its name
and the day it arrived make (:class:`NamedCache`); they are a fallback and
never prove a run complete. The product modules (:mod:`xuebuild.cmadust`,
:mod:`xuebuild.cmageps`) name their caches as data, a tuple of
descriptors; the listing is this module's.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from xml.etree import ElementTree

from .common import S3_NAMESPACE, s3_list_pages, s3_list_query
from .model import GfsRun

#: The two caches' endpoints, the defaults each product's own environment
#: variable falls back to.
NOAA_CACHE_URL = "https://wis2globalcache.s3.amazonaws.com"
JMA_CACHE_URL = "https://global-cache-of-japan.s3.ap-northeast-1.amazonaws.com"

_LISTING = "WIS2 cache listing"


@dataclass(frozen=True)
class Cache:
    name: str
    base_url: str
    prefix: str


@dataclass(frozen=True)
class NamedCache:
    """A cache that cannot be listed but serves a file by Range at a URL
    its name and its *arrival* date make: ``url_template`` holds
    ``{date}`` (the UTC day the file reached the cache, spelt with
    ``date_format``) and ``{file}``. A file arrives the day of its cycle
    or after, so each of ``arrival_days`` past the cycle's day is tried in
    turn. Such a cache never proves a run complete; it is a fallback for a
    file the listed caches name but fail to serve, or do not name."""

    name: str
    url_template: str
    date_format: str = "%Y%m%d"
    arrival_days: tuple[int, ...] = (0, 1)

    def urls(self, run: GfsRun, file_name: str) -> list[str]:
        return [
            self.url_template.format(date=(run.time + timedelta(days=days)).strftime(self.date_format), file=file_name)
            for days in self.arrival_days
        ]


@dataclass(frozen=True)
class CacheObject:
    cache: str
    url: str
    size: int | None
    """The listed size; None for a file a :class:`NamedCache` may hold,
    whose size nothing states before it is read."""


#: ``(cycle, lead hour)`` of a file name, or None for a file of no interest.
ParseName = Callable[[str], "tuple[datetime, int] | None"]


def object_url(cache: Cache, key: str) -> str:
    return f"{cache.base_url}/{urllib.parse.quote(key, safe='/')}"


def run_prefix(cache: Cache, run: GfsRun) -> str:
    """The listing prefix of every file of one run on one cache: every CMA
    file name starts with the issuing centre and the cycle."""
    return f"{cache.prefix}Z_NAFP_C_BABJ_{run.time:%Y%m%d%H}0000"


def parse_listing(
    root: ElementTree.Element, cache: Cache, run: GfsRun, parse_name: ParseName
) -> dict[int, list[CacheObject]]:
    """The run's files on one listing page, by lead hour. A key counts when
    its last segment is a file of this run; on the JMA cache two uploads of
    one file may both be listed, the newer (the larger stamp) first."""
    found: dict[int, list[tuple[str, CacheObject]]] = {}
    for contents in root.iter(f"{S3_NAMESPACE}Contents"):
        key = contents.findtext(f"{S3_NAMESPACE}Key") or ""
        parsed = parse_name(key.rsplit("/", 1)[-1])
        if parsed is None or parsed[0] != run.time:
            continue
        try:
            size = int(contents.findtext(f"{S3_NAMESPACE}Size") or "")
        except ValueError:
            continue
        found.setdefault(parsed[1], []).append((key, CacheObject(cache.name, object_url(cache, key), size)))
    return {hour: [item for _, item in sorted(items, key=lambda pair: pair[0], reverse=True)] for hour, items in found.items()}


def list_run(
    caches: tuple[Cache | NamedCache, ...], run: GfsRun, parse_name: ParseName, *, fetch: Callable[[str], str]
) -> dict[int, list[CacheObject]]:
    """Every file of one run on every listable cache, by lead hour, in the
    order to try them; a :class:`NamedCache` is not listed. A cache that
    cannot be listed is an error, not an empty cache: a run must never read
    as incomplete because a listing failed."""
    objects: dict[int, list[CacheObject]] = {}
    for cache in caches:
        if not isinstance(cache, Cache):
            continue
        prefix = run_prefix(cache, run)
        for root in s3_list_pages(lambda token: fetch(f"{cache.base_url}/?{s3_list_query(prefix, token)}"), _LISTING):
            for hour, items in parse_listing(root, cache, run, parse_name).items():
                objects.setdefault(hour, []).extend(items)
    return objects


def candidates(
    caches: tuple[Cache | NamedCache, ...], run: GfsRun, objects: dict[int, list[CacheObject]], hour: int, file_name: str
) -> list[CacheObject]:
    """Where one lead time's file is read from, in order: wherever the
    listings put it, cache by cache in ``caches`` order, then each
    :class:`NamedCache`'s URLs for it."""
    listed = objects.get(hour, [])
    found: list[CacheObject] = []
    for cache in caches:
        if isinstance(cache, Cache):
            found += [item for item in listed if item.cache == cache.name]
        else:
            found += [CacheObject(cache.name, url, None) for url in cache.urls(run, file_name)]
    return found

"""Pull the TEMP bulletins that have arrived since the last issue into
``<raw>/sounding.<issue>/<gateway>/`` and leave a ``fetch.json`` beside
them saying how it went. The build (:mod:`.build`) reads only these
directories — so a fixture is a fetched directory, and a build never
touches the network.

The source is the WIS2 Global Cache, a public S3 bucket that answers
``ListObjectsV2`` unsigned. Two GTS→WIS2 gateways, one run by JMA and one
by DWD, republish every country's GTS bulletins under the WMO abbreviated
heading tree, and the upper-air soundings are the ``I/U/S/`` subtree. The
two carry *different* sets — one has the US centre's bulletins, the other
the European ones — so both are listed every hour and the build
deduplicates by station and nominal time.

The listing is complete every time (two or three pages per gateway); what
is incremental is the download. Each object's ``LastModified`` is compared
with the watermark the previous issue's index recorded for that gateway,
and only the newer ones are fetched — typically a few dozen of the two
thousand objects in the bucket's 24-hour retention window. A first build,
or a gap longer than the retention, downloads everything.

A source is not raised, it is recorded: a source that cannot be listed
leaves ``ok: false`` with the reason and the others publish.

The gateways are transitional: a centre that runs its own WIS2 node stops
sending its soundings to them (Météo-France's are on ``fr-meteofrance``
and in neither gateway). So the national nodes are a third kind of
source, ``wis2:<centre>``, one per node directory under ``data/`` that
publishes the TEMP topic
``data/<centre>/data/core/weather/surface-based-observations/temp/``.
The nodes are discovered every hour by listing ``data/`` — a new node is
picked up the hour it appears — and listed the same way as a gateway. A
node whose topic is empty is not a source of that issue at all.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..common import S3_NAMESPACE, iso_z, s3_list_pages, s3_list_query, write_bytes_atomic
from ..errors import DownloadError, SoundingProductError, XueError
from ..fetch import get_ok
from .bufr import parse_file_name

LOG = logging.getLogger(__name__)

SOURCE_IDS = ("jp-jma-gts-to-wis2", "de-dwd-gts-to-wis2")
"""The two GTS→WIS2 gateway directories, in the order the build reads
them."""

NATIVE_SOURCE = "wis2"
"""Every national WIS2 node with a TEMP topic, discovered at fetch time.
It is a source selector, not a source: what is fetched and published is
one ``wis2:<centre>`` per node. A discovery that fails is recorded under
this id."""

DEFAULT_SOURCES = (*SOURCE_IDS, NATIVE_SOURCE)

NATIVE_ID = re.compile(r"^wis2:(?P<centre>[a-z0-9]+(?:-[a-z0-9]+)*)$")

CENTRES_PREFIX = "data/"
NATIVE_PREFIX = "data/{centre}/data/core/weather/surface-based-observations/temp/"

NOT_NODES = ("-gts-to-wis2", "-global-discovery-catalogue")
"""Directories under ``data/`` that are not a national node's topic tree:
the gateways (read as sources of their own) and the catalogues."""

NATIVE_LOOKBACK_HOURS = 48
"""How far back a node with no watermark is taken. The nodes' objects
outlive the gateways' day (five or six days when this was written), and a
sounding older than the build's 48-hour staleness is never published, so
a first build need not download it."""

BUCKET_BASE_URL = "https://wis2globalcache.s3.us-east-1.amazonaws.com/"
GATEWAY_PREFIX = "data/{gateway}/data/core/I/U/S/"
"""``I/U/S/`` is the WMO heading tree's upper-air sounding subtree: the
whole ``I/U/`` tree is a hundred thousand objects (aircraft and satellite
winds), and this branch is two thousand."""

USER_AGENT = f"xue/{__version__} (+https://github.com/ringsaturn/xue)"

RETENTION_HOURS = 24
"""The cache keeps an object for a day. A watermark older than that has
lost objects behind it, so the next build takes the whole listing."""

FETCH_CONCURRENCY = 8
"""Bulletins downloaded at a time. They are 1–50 KB each off a public
bucket, and an hourly build takes a few dozen; a first build takes the
whole retention window, and that is the case this number is for."""


@dataclass
class SourceStatus:
    """One gateway's outcome, the shape ``xuebuild/tc/track.py`` gives a
    track source: what is not ``ok`` carries an ``error``."""

    id: str
    ok: bool
    fetched: datetime | None = None
    url: str | None = None
    error: str | None = None
    watermark: datetime | None = None
    """The newest ``LastModified`` in the listing, whether or not it was
    downloaded — where the next issue starts."""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.fetched is not None:
            payload["fetched"] = iso_z(self.fetched)
        if self.url is not None:
            payload["url"] = self.url
        if self.error is not None:
            payload["error"] = self.error
        payload["watermark"] = None if self.watermark is None else iso_z(self.watermark)
        payload.update(self.detail)
        return payload


@dataclass
class FetchResult:
    status: SourceStatus
    files: list[str]
    arrived: dict[str, str] = field(default_factory=dict)
    """A national node's ``LastModified`` per file, which stands in for the
    arrival stamp a gateway writes into the file name. Kept in
    ``fetch.json`` for the build, never published."""

    def to_json(self) -> dict[str, Any]:
        payload = self.status.to_json()
        payload["files"] = self.files
        if self.arrived:
            payload["arrived"] = self.arrived
        return payload


@dataclass(frozen=True)
class RemoteObject:
    key: str
    last_modified: datetime
    size: int

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]


def issue_raw_directory(raw_root: Path, issue: datetime) -> Path:
    return raw_root / f"sounding.{issue.astimezone(UTC).strftime('%Y%m%d%H')}"


def is_native(source: str) -> bool:
    return NATIVE_ID.match(source) is not None


def is_source(source: str) -> bool:
    """A gateway, the node selector, or one node."""
    return source in DEFAULT_SOURCES or is_native(source)


def source_directory(root: Path, source: str) -> Path:
    """Where a source's objects and ``fetch.json`` sit under an issue's raw
    directory: ``<gateway>/``, and ``wis2/<centre>/`` for a node, so the
    selector's own record (``wis2/fetch.json``) sits beside its nodes."""
    return root.joinpath(*source.split(":"))


def source_prefix(source: str) -> str:
    match = NATIVE_ID.match(source)
    if match is not None:
        return NATIVE_PREFIX.format(centre=match.group("centre"))
    if source in SOURCE_IDS:
        return GATEWAY_PREFIX.format(gateway=source)
    raise SoundingProductError(
        f"unknown sounding source {source!r}; choose from {', '.join(DEFAULT_SOURCES)} or wis2:<centre>"
    )


def fetched_sources(root: Path, sources: tuple[str, ...]) -> tuple[str, ...]:
    """The sources an issue's raw directory holds, in build order:
    ``sources`` as given, with the node selector expanded into the nodes
    the fetch wrote (and itself only when its discovery failed)."""
    found: list[str] = []
    for source in sources:
        if source != NATIVE_SOURCE:
            found.append(source)
            continue
        directory = source_directory(root, NATIVE_SOURCE)
        if (directory / "fetch.json").exists():
            found.append(NATIVE_SOURCE)
        if directory.is_dir():
            found.extend(
                f"{NATIVE_SOURCE}:{child.name}"
                for child in sorted(directory.iterdir())
                if is_native(f"{NATIVE_SOURCE}:{child.name}") and (child / "fetch.json").exists()
            )
    return tuple(dict.fromkeys(found))


def _get(url: str, *, timeout: float = 60) -> bytes:
    return get_ok(url, headers={"User-Agent": USER_AGENT}, timeout=timeout, attempts=4, max_elapsed=120)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def list_objects(
    prefix: str, *, base_url: str = BUCKET_BASE_URL, get: Callable[[str], bytes] = _get
) -> list[RemoteObject]:
    """Every object under a prefix, following ``NextContinuationToken``.
    The bucket answers ``list-type=2`` without a signature; the response is
    the S3 XML schema and is parsed with the stdlib."""
    objects: list[RemoteObject] = []
    pages = s3_list_pages(
        lambda token: get(f"{base_url}?{s3_list_query(prefix, token)}"),
        f"the bucket listing for {prefix}",
        strict=False,
        max_pages=100,  # a thousand pages would be a bucket layout change
    )
    for root in pages:
        for contents in root.findall(f"{S3_NAMESPACE}Contents"):
            key = contents.findtext(f"{S3_NAMESPACE}Key")
            modified = _parse_time(contents.findtext(f"{S3_NAMESPACE}LastModified"))
            size = contents.findtext(f"{S3_NAMESPACE}Size")
            if not key or modified is None or size is None:
                continue
            objects.append(RemoteObject(key=key, last_modified=modified, size=int(size)))
    return objects


def list_centres(*, base_url: str = BUCKET_BASE_URL, get: Callable[[str], bytes] = _get) -> list[str]:
    """The national node directories under ``data/``: one delimited
    listing, a page or two of ``CommonPrefixes``."""
    centres: list[str] = []
    pages = s3_list_pages(
        lambda token: get(f"{base_url}?{s3_list_query(CENTRES_PREFIX, token, delimiter='/')}"),
        f"the bucket listing for {CENTRES_PREFIX}",
        strict=False,
        max_pages=20,
    )
    for root in pages:
        for common in root.findall(f"{S3_NAMESPACE}CommonPrefixes"):
            prefix = common.findtext(f"{S3_NAMESPACE}Prefix") or ""
            centre = prefix.removeprefix(CENTRES_PREFIX).strip("/")
            if not centre or centre.endswith(NOT_NODES) or not is_native(f"{NATIVE_SOURCE}:{centre}"):
                continue
            centres.append(centre)
    return sorted(set(centres))


def objects_to_fetch(
    objects: list[RemoteObject],
    watermark: datetime | None,
    *,
    now: datetime,
    lookback: timedelta | None = None,
) -> list[RemoteObject]:
    """The objects a build has not seen: those modified after the
    watermark. No watermark, or one older than the cache's retention
    (nothing behind it survives to be compared against), takes the lot —
    or, given a ``lookback``, what was modified within it."""
    if watermark is None or now - watermark > timedelta(hours=RETENTION_HOURS):
        candidates = list(objects)
        if lookback is not None:
            candidates = [item for item in candidates if item.last_modified >= now - lookback]
    else:
        candidates = [item for item in objects if item.last_modified > watermark]
    return sorted(candidates, key=lambda item: (item.last_modified, item.key))


def fetch_gateway(
    gateway: str,
    directory: Path,
    *,
    watermark: datetime | None,
    now: datetime | None = None,
    reuse_root: Path | None = None,
    base_url: str = BUCKET_BASE_URL,
    get: Callable[[str], bytes] = _get,
) -> FetchResult:
    """List one source's sounding subtree — a gateway's, or a national
    node's TEMP topic — and download what is new."""
    now = now or datetime.now(UTC)
    if gateway == NATIVE_SOURCE or not is_source(gateway):
        raise SoundingProductError(
            f"unknown sounding source {gateway!r}; choose from {', '.join(SOURCE_IDS)} or wis2:<centre>"
        )
    native = is_native(gateway)
    prefix = source_prefix(gateway)
    listing = list_objects(prefix, base_url=base_url, get=get)
    newest = max((item.last_modified for item in listing), default=None)
    lookback = timedelta(hours=NATIVE_LOOKBACK_HOURS) if native else None
    wanted = [
        item
        for item in objects_to_fetch(listing, watermark, now=now, lookback=lookback)
        if native or _is_bulletin(gateway, item)
    ]
    # A node's topic is flat, but nothing promises it: two objects that
    # share a name in different subdirectories would share a file here,
    # so only the newest of them is taken.
    if native:
        wanted = list({item.name: item for item in wanted}.values())

    failures: list[Exception] = []

    def take(item: RemoteObject) -> str:
        """``downloaded`` / ``reused`` / ``present`` / ``failed``, having
        made sure the object is on disk. One object the cache will not
        serve is not worth losing the hour's other two thousand, so it is
        counted; every object failing means the bucket is down and the
        gateway fails as a whole."""
        destination = directory / item.name
        if destination.exists() and destination.stat().st_size == item.size:
            return "present"
        if _reuse(reuse_root, gateway, item, destination):
            return "reused"
        try:
            write_bytes_atomic(destination, get(base_url + urllib.parse.quote(item.key)))
        except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
            LOG.warning("sounding %s: %s did not download: %s", gateway, item.name, exc)
            failures.append(exc)
            return "failed"
        return "downloaded"

    # An hour's worth of new bulletins is a few dozen small objects, but a
    # first build (no watermark) is the bucket's whole retention window —
    # a couple of thousand. They are independent GETs of a public bucket,
    # so they go out a few at a time.
    if len(wanted) <= 1:
        outcomes = [take(item) for item in wanted]
    else:
        with ThreadPoolExecutor(max_workers=min(FETCH_CONCURRENCY, len(wanted))) as executor:
            outcomes = list(executor.map(take, wanted))
    if wanted and len(failures) == len(wanted):
        raise DownloadError(f"no object under {prefix} could be downloaded: {failures[0]}")
    files = [item.name for item, outcome in zip(wanted, outcomes) if outcome != "failed"]
    arrived = (
        {item.name: iso_z(item.last_modified) for item, outcome in zip(wanted, outcomes) if outcome != "failed"}
        if native
        else {}
    )
    downloaded = outcomes.count("downloaded")
    reused = outcomes.count("reused")
    total_bytes = sum(item.size for item, outcome in zip(wanted, outcomes) if outcome != "failed")
    return FetchResult(
        SourceStatus(
            gateway,
            True,
            fetched=now,
            url=base_url + prefix,
            watermark=newest,
            detail={
                "listed": len(listing),
                "objects": downloaded,
                "reused": reused,
                "failed": len(failures),
                "bytes": total_bytes,
            },
        ),
        files,
        arrived,
    )


def _is_bulletin(gateway: str, item: RemoteObject) -> bool:
    if parse_file_name(item.name) is not None:
        return True
    LOG.debug("sounding %s: skipping %s (not a WMO bulletin name)", gateway, item.name)
    return False


def _reuse(reuse_root: Path | None, gateway: str, item: RemoteObject, destination: Path) -> bool:
    """A bulletin the previous issue already downloaded is the same object
    — the cache's keys are content-stamped by arrival time — so copy it
    rather than ask the bucket for it again. Only the previous issue's
    directory is looked at; anything older has been pruned with the run."""
    if reuse_root is None:
        return False
    candidate = source_directory(reuse_root, gateway) / item.name
    try:
        if not candidate.is_file() or candidate.stat().st_size != item.size:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(candidate, destination)
    except OSError:
        return False
    return True


def fetch_sources(
    raw_root: Path,
    issue: datetime,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    *,
    watermarks: dict[str, datetime | None] | None = None,
    force: bool = False,
    now: datetime | None = None,
    base_url: str = BUCKET_BASE_URL,
    get: Callable[[str], bytes] = _get,
) -> dict[str, FetchResult]:
    """Fetch each source into its directory under the issue's raw
    directory, the node selector expanded into the nodes it discovers. A
    source already fetched (its ``fetch.json`` says ok) is left alone
    unless ``force``; a failed one is retried."""
    now = now or datetime.now(UTC)
    root = issue_raw_directory(raw_root, issue)
    previous_root = issue_raw_directory(raw_root, issue - timedelta(hours=1))
    reuse_root = previous_root if previous_root.is_dir() else None
    results: dict[str, FetchResult] = {}

    def fetch_one(source: str) -> FetchResult | None:
        """The source's outcome, written to its ``fetch.json`` — or None for
        a node whose topic is empty, which is no source of this issue."""
        directory = source_directory(root, source)
        record = directory / "fetch.json"
        if not force and record.exists():
            existing = _read_record(record)
            if existing is not None and existing.status.ok:
                LOG.info("sounding %s: already fetched", source)
                return existing
        try:
            result = fetch_gateway(
                source,
                directory,
                watermark=(watermarks or {}).get(source),
                now=now,
                reuse_root=reuse_root,
                base_url=base_url,
                get=get,
            )
            if is_native(source) and not result.status.detail.get("listed"):
                return None
            LOG.info(
                "sounding %s: %d bulletin(s) in the window, %d downloaded",
                source,
                len(result.files),
                result.status.detail.get("objects", 0),
            )
        except XueError as exc:
            LOG.warning("sounding %s: %s", source, exc)
            result = FetchResult(SourceStatus(source, False, fetched=now, error=str(exc)), [])
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOG.warning("sounding %s: %s", source, exc)
            result = FetchResult(
                SourceStatus(source, False, fetched=now, error=f"{type(exc).__name__}: {exc}"), []
            )
        write_bytes_atomic(record, json.dumps(result.to_json(), indent=2).encode("utf-8"))
        return result

    for source in sources:
        if source != NATIVE_SOURCE:
            result = fetch_one(source)
            if result is not None:
                results[source] = result
            continue
        selector_record = source_directory(root, NATIVE_SOURCE) / "fetch.json"
        try:
            centres = list_centres(base_url=base_url, get=get)
        except (XueError, urllib.error.URLError, OSError, ValueError) as exc:
            LOG.warning("sounding %s: the node discovery failed: %s", NATIVE_SOURCE, exc)
            failed = FetchResult(
                SourceStatus(NATIVE_SOURCE, False, fetched=now, url=base_url + CENTRES_PREFIX, error=f"discovery: {exc}"),
                [],
            )
            write_bytes_atomic(selector_record, json.dumps(failed.to_json(), indent=2).encode("utf-8"))
            results[NATIVE_SOURCE] = failed
            continue
        selector_record.unlink(missing_ok=True)
        nodes = [f"{NATIVE_SOURCE}:{centre}" for centre in centres]
        # A hundred-odd listings, nearly all of an empty topic: they go out
        # a few at a time, like the downloads.
        with ThreadPoolExecutor(max_workers=FETCH_CONCURRENCY) as executor:
            outcomes = list(executor.map(fetch_one, nodes))
        found = {node: result for node, result in zip(nodes, outcomes) if result is not None}
        LOG.info("sounding %s: %d node(s) listed, %d with a TEMP topic", NATIVE_SOURCE, len(nodes), len(found))
        results.update(found)
    return results


def _result_from_record(record: dict[str, Any]) -> FetchResult:
    known = {"id", "ok", "fetched", "url", "error", "watermark", "files", "arrived"}
    status = SourceStatus(
        id=str(record.get("id")),
        ok=bool(record.get("ok")),
        fetched=_parse_time(record.get("fetched")),
        url=record.get("url"),
        error=record.get("error"),
        watermark=_parse_time(record.get("watermark")),
        detail={key: value for key, value in record.items() if key not in known},
    )
    arrived = record.get("arrived")
    return FetchResult(
        status,
        [str(name) for name in record.get("files", [])],
        {str(name): str(value) for name, value in arrived.items()} if isinstance(arrived, dict) else {},
    )


def _read_record(record: Path) -> FetchResult | None:
    try:
        return _result_from_record(json.loads(record.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return None


def read_fetch_record(directory: Path, gateway: str) -> FetchResult | None:
    """What a gateway's directory says about its fetch, or None when it
    was never attempted."""
    record = source_directory(directory, gateway) / "fetch.json"
    if not record.exists():
        return None
    result = _read_record(record)
    if result is None:
        return FetchResult(SourceStatus(gateway, False, error="fetch.json unreadable"), [])
    return result

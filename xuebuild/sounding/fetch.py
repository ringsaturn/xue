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

A source is not raised, it is recorded: a gateway that cannot be listed
leaves ``ok: false`` with the reason and the other one publishes.

The gateways are transitional (WMO retires them as national WIS2 nodes
take over), so a second kind of source is reserved for the native topic
directories, ``wis2:<centre>``, listing
``data/<centre>/data/core/weather/surface-based-observations/temp/`` the
same way. It is not implemented in v1; the id shape is spoken for so that
adding it is a configuration change.
"""

from __future__ import annotations

import json
import logging
import shutil
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..errors import DownloadError, SoundingProductError, XueError
from ..fetch import _request
from .bufr import parse_file_name

LOG = logging.getLogger(__name__)

SOURCE_IDS = ("jp-jma-gts-to-wis2", "de-dwd-gts-to-wis2")
"""The two GTS→WIS2 gateway directories, in the order the build reads
them. ``wis2:<centre>`` is reserved for a native topic source (module
docstring) and is not implemented."""

BUCKET_BASE_URL = "https://wis2globalcache.s3.us-east-1.amazonaws.com/"
GATEWAY_PREFIX = "data/{gateway}/data/core/I/U/S/"
"""``I/U/S/`` is the WMO heading tree's upper-air sounding subtree: the
whole ``I/U/`` tree is a hundred thousand objects (aircraft and satellite
winds), and this branch is two thousand."""

S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

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
            payload["fetched"] = _iso(self.fetched)
        if self.url is not None:
            payload["url"] = self.url
        if self.error is not None:
            payload["error"] = self.error
        payload["watermark"] = None if self.watermark is None else _iso(self.watermark)
        payload.update(self.detail)
        return payload


@dataclass
class FetchResult:
    status: SourceStatus
    files: list[str]

    def to_json(self) -> dict[str, Any]:
        payload = self.status.to_json()
        payload["files"] = self.files
        return payload


@dataclass(frozen=True)
class RemoteObject:
    key: str
    last_modified: datetime
    size: int

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def issue_raw_directory(raw_root: Path, issue: datetime) -> Path:
    return raw_root / f"sounding.{issue.astimezone(UTC).strftime('%Y%m%d%H')}"


def _get(url: str, *, timeout: float = 60) -> bytes:
    response = _request(
        url, headers={"User-Agent": USER_AGENT}, timeout=timeout, attempts=4, max_elapsed=120
    )
    with response:
        status = getattr(response, "status", None)
        if status != 200:
            raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
        return response.read()  # type: ignore[no-any-return]


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(payload)
    temporary.replace(path)


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
    token: str | None = None
    for _ in range(100):  # a thousand pages would be a bucket layout change
        query = {"list-type": "2", "prefix": prefix}
        if token is not None:
            query["continuation-token"] = token
        body = get(f"{base_url}?{urllib.parse.urlencode(query)}")
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError as exc:
            raise DownloadError(f"the bucket listing for {prefix} is not XML: {exc}") from exc
        for contents in root.findall(f"{S3_NS}Contents"):
            key = contents.findtext(f"{S3_NS}Key")
            modified = _parse_time(contents.findtext(f"{S3_NS}LastModified"))
            size = contents.findtext(f"{S3_NS}Size")
            if not key or modified is None or size is None:
                continue
            objects.append(RemoteObject(key=key, last_modified=modified, size=int(size)))
        if root.findtext(f"{S3_NS}IsTruncated") != "true":
            break
        token = root.findtext(f"{S3_NS}NextContinuationToken")
        if not token:
            break
    else:
        raise DownloadError(f"the bucket listing for {prefix} did not terminate")
    return objects


def objects_to_fetch(
    objects: list[RemoteObject], watermark: datetime | None, *, now: datetime
) -> list[RemoteObject]:
    """The objects a build has not seen: those modified after the
    watermark. No watermark, or one older than the cache's retention
    (nothing behind it survives to be compared against), takes the lot."""
    if watermark is None or now - watermark > timedelta(hours=RETENTION_HOURS):
        candidates = list(objects)
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
    """List one gateway's sounding subtree and download what is new."""
    now = now or datetime.now(UTC)
    if gateway not in SOURCE_IDS:
        raise SoundingProductError(f"unknown sounding source {gateway!r}; choose from {', '.join(SOURCE_IDS)}")
    prefix = GATEWAY_PREFIX.format(gateway=gateway)
    listing = list_objects(prefix, base_url=base_url, get=get)
    newest = max((item.last_modified for item in listing), default=None)
    wanted = [item for item in objects_to_fetch(listing, watermark, now=now) if _is_bulletin(gateway, item)]

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
            _write(destination, get(base_url + urllib.parse.quote(item.key)))
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
    candidate = reuse_root / gateway / item.name
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
    sources: tuple[str, ...] = SOURCE_IDS,
    *,
    watermarks: dict[str, datetime | None] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, FetchResult]:
    """Fetch each gateway into its directory under the issue's raw
    directory. A gateway already fetched (its ``fetch.json`` says ok) is
    left alone unless ``force``; a failed one is retried."""
    now = now or datetime.now(UTC)
    root = issue_raw_directory(raw_root, issue)
    previous_root = issue_raw_directory(raw_root, issue - timedelta(hours=1))
    reuse_root = previous_root if previous_root.is_dir() else None
    results: dict[str, FetchResult] = {}
    for gateway in sources:
        directory = root / gateway
        record = directory / "fetch.json"
        if not force and record.exists():
            existing = _read_record(record)
            if existing is not None and existing.status.ok:
                LOG.info("sounding %s: already fetched", gateway)
                results[gateway] = existing
                continue
        try:
            result = fetch_gateway(
                gateway,
                directory,
                watermark=(watermarks or {}).get(gateway),
                now=now,
                reuse_root=reuse_root,
            )
            LOG.info(
                "sounding %s: %d bulletin(s) in the window, %d downloaded",
                gateway,
                len(result.files),
                result.status.detail.get("objects", 0),
            )
        except XueError as exc:
            LOG.warning("sounding %s: %s", gateway, exc)
            result = FetchResult(SourceStatus(gateway, False, fetched=now, error=str(exc)), [])
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOG.warning("sounding %s: %s", gateway, exc)
            result = FetchResult(
                SourceStatus(gateway, False, fetched=now, error=f"{type(exc).__name__}: {exc}"), []
            )
        _write(record, json.dumps(result.to_json(), indent=2).encode("utf-8"))
        results[gateway] = result
    return results


def _result_from_record(record: dict[str, Any]) -> FetchResult:
    known = {"id", "ok", "fetched", "url", "error", "watermark", "files"}
    status = SourceStatus(
        id=str(record.get("id")),
        ok=bool(record.get("ok")),
        fetched=_parse_time(record.get("fetched")),
        url=record.get("url"),
        error=record.get("error"),
        watermark=_parse_time(record.get("watermark")),
        detail={key: value for key, value in record.items() if key not in known},
    )
    return FetchResult(status, [str(name) for name in record.get("files", [])])


def _read_record(record: Path) -> FetchResult | None:
    try:
        return _result_from_record(json.loads(record.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError, AttributeError):
        return None


def read_fetch_record(directory: Path, gateway: str) -> FetchResult | None:
    """What a gateway's directory says about its fetch, or None when it
    was never attempted."""
    record = directory / gateway / "fetch.json"
    if not record.exists():
        return None
    result = _read_record(record)
    if result is None:
        return FetchResult(SourceStatus(gateway, False, error="fetch.json unreadable"), [])
    return result

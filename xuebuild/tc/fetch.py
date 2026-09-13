"""Pull every source's current files into ``<raw>/tc.<issue>/<source>/``
and leave a ``fetch.json`` beside them saying how it went. The build
(:mod:`.build`) reads only these directories — so a fixture is a fetched
directory, and a build never touches the network.

A source failing is recorded, not raised: ``fetch.json`` carries ``ok:
false`` and the reason, the build carries it into the product's
``sources``, and the other sources publish. Only an error the operator has
to fix (a missing ``bufr_dump``) is a :class:`~xuebuild.errors.XueError`.

The model sources are by cycle: the newest cycle at or before the issue
hour whose key file has landed (the tracker's ``avno`` file, GEFS'
``aemn`` mean, ECMWF's ``tf`` BUFR), looked for back over the last day.
The agencies are "whatever is current": JTWC's RSS names its live
products, NHC's ``CurrentStorms.json`` its active storms.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import urllib.error
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..errors import DownloadError, XueError
from ..fetch import ECMWF_BASE_URLS, _http_error_code, _request
from .track import SourceStatus

LOG = logging.getLogger(__name__)

SOURCE_IDS = ("jtwc", "nhc", "gfs", "gefs", "ecmwf", "ecmwfens", "ibtracs")

JTWC_RSS_URL = "https://www.metoc.navy.mil/jtwc/rss/jtwc.rss"
# JTWC's server answers a non-browser User-Agent with 403.
JTWC_USER_AGENT = "Mozilla/5.0 (compatible; xue/0.1; +https://github.com/ringsaturn/xue)"
NHC_CURRENT_STORMS_URL = "https://www.nhc.noaa.gov/CurrentStorms.json"
NHC_ATCF_URL = "https://ftp.nhc.noaa.gov/atcf"
NCEP_TRACKER_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/ens_tracker/prod"
IBTRACS_URL = (
    "https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs"
    "/v04r01/access/csv/ibtracs.ACTIVE.list.v04r01.csv"
)
GEFS_MEMBERS = ("ac00", "aemn") + tuple(f"ap{n:02d}" for n in range(1, 31))
CYCLE_LOOKBACK = 4
"""Cycles to look back for a model's newest available run (a day)."""

_TCW_LINK = re.compile(r"https://www\.metoc\.navy\.mil/jtwc/products/([a-z]{2}\d{4})\.tcw")


@dataclass
class FetchResult:
    status: SourceStatus
    files: list[str]

    def to_json(self) -> dict[str, Any]:
        payload = self.status.to_json()
        payload["files"] = self.files
        return payload


def issue_raw_directory(raw_root: Path, issue: datetime) -> Path:
    return raw_root / f"tc.{issue.astimezone(UTC).strftime('%Y%m%d%H')}"


def _get(url: str, *, headers: dict[str, str] | None = None, timeout: float = 60) -> bytes:
    response = _request(url, headers=headers, timeout=timeout, attempts=3, max_elapsed=90)
    with response:
        status = getattr(response, "status", None)
        if status != 200:
            raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
        return response.read()  # type: ignore[no-any-return]


def _get_optional(url: str, **kwargs: Any) -> bytes | None:
    """The body, or None on 404 — a file that has not landed yet."""
    try:
        return _get(url, **kwargs)
    except DownloadError as exc:
        if _http_error_code(exc) in (403, 404):
            return None
        raise


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(payload)
    temporary.replace(path)


def _cycles(issue: datetime) -> list[datetime]:
    floored = issue.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    floored = floored.replace(hour=(floored.hour // 6) * 6)
    return [floored - timedelta(hours=6 * back) for back in range(CYCLE_LOOKBACK)]


def fetch_jtwc(directory: Path) -> FetchResult:
    now = datetime.now(UTC)
    rss = _get(JTWC_RSS_URL, headers={"User-Agent": JTWC_USER_AGENT})
    _write(directory / "jtwc.rss", rss)
    files = ["jtwc.rss"]
    names = list(dict.fromkeys(_TCW_LINK.findall(rss.decode("utf-8", errors="replace"))))
    for name in names:
        url = f"https://www.metoc.navy.mil/jtwc/products/{name}.tcw"
        body = _get_optional(url, headers={"User-Agent": JTWC_USER_AGENT})
        if body is None:
            LOG.warning("jtwc: %s listed but not served", url)
            continue
        _write(directory / f"{name}.tcw", body)
        files.append(f"{name}.tcw")
    return FetchResult(
        SourceStatus("jtwc", True, fetched=now, url=JTWC_RSS_URL, detail={"products": len(files) - 1}), files
    )


def fetch_nhc(directory: Path) -> FetchResult:
    now = datetime.now(UTC)
    current = _get(NHC_CURRENT_STORMS_URL)
    _write(directory / "CurrentStorms.json", current)
    files = ["CurrentStorms.json"]
    try:
        storms = json.loads(current).get("activeStorms", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        raise DownloadError(f"CurrentStorms.json is not the expected JSON: {exc}") from exc
    for storm in storms:
        storm_id = str(storm.get("id", "")).lower()
        if not re.match(r"^[a-z]{2}\d{6}$", storm_id):
            continue
        # The a-deck is gzip on the FTP mirror; the b-deck is plain.
        a_deck = _get_optional(f"{NHC_ATCF_URL}/aid_public/a{storm_id}.dat.gz")
        if a_deck is not None:
            try:
                a_deck = gzip.decompress(a_deck)
            except (OSError, EOFError) as exc:
                raise DownloadError(f"a-deck for {storm_id} is not gzip: {exc}") from exc
            _write(directory / f"a{storm_id}.dat", a_deck)
            files.append(f"a{storm_id}.dat")
        b_deck = _get_optional(f"{NHC_ATCF_URL}/btk/b{storm_id}.dat")
        if b_deck is not None:
            _write(directory / f"b{storm_id}.dat", b_deck)
            files.append(f"b{storm_id}.dat")
    return FetchResult(
        SourceStatus("nhc", True, fetched=now, url=NHC_CURRENT_STORMS_URL, detail={"storms": len(storms)}), files
    )


def _tracker_file(cycle: datetime, model: str, member: str) -> str:
    return f"{NCEP_TRACKER_URL}/{model}.{cycle:%Y%m%d}/{cycle:%H}/tctrack/{member}.t{cycle:%H}z.cyclone.trackatcfunix"


def fetch_gfs(directory: Path, issue: datetime) -> FetchResult:
    now = datetime.now(UTC)
    for cycle in _cycles(issue):
        url = _tracker_file(cycle, "gfs", "avno")
        body = _get_optional(url)
        if body is None:
            continue
        name = f"{cycle:%Y%m%d%H}/avno.t{cycle:%H}z.cyclone.trackatcfunix"
        _write(directory / name, body)
        return FetchResult(SourceStatus("gfs", True, fetched=now, url=url, cycle=f"{cycle:%Y%m%d%H}"), [name])
    raise DownloadError(f"no GFS tracker output in the last {CYCLE_LOOKBACK} cycles")


def fetch_gefs(directory: Path, issue: datetime) -> FetchResult:
    now = datetime.now(UTC)
    for cycle in _cycles(issue):
        mean_url = _tracker_file(cycle, "gefs", "aemn")
        mean = _get_optional(mean_url)
        if mean is None:
            continue
        files = []
        for member in GEFS_MEMBERS:
            body = mean if member == "aemn" else _get_optional(_tracker_file(cycle, "gefs", member))
            if body is None:
                LOG.warning("gefs: member %s of %s missing", member, f"{cycle:%Y%m%d%H}")
                continue
            name = f"{cycle:%Y%m%d%H}/{member}.t{cycle:%H}z.cyclone.trackatcfunix"
            _write(directory / name, body)
            files.append(name)
        return FetchResult(
            SourceStatus(
                "gefs", True, fetched=now, url=mean_url, cycle=f"{cycle:%Y%m%d%H}", detail={"members": len(files)}
            ),
            files,
        )
    raise DownloadError(f"no GEFS tracker output in the last {CYCLE_LOOKBACK} cycles")


def ecmwf_tf_url(cycle: datetime, stream: str, *, base_url: str | None = None) -> str:
    """The ``tf`` BUFR of one cycle: 360 h from 00 and 12 UTC, 144 h from
    06 and 18, one file per stream (``oper`` for HRES, ``enfo`` for ENS)."""
    horizon = 360 if cycle.hour in (0, 12) else 144
    base = (base_url or ECMWF_BASE_URLS[0]).rstrip("/")
    return f"{base}/{cycle:%Y%m%d}/{cycle:%H}z/ifs/0p25/{stream}/{cycle:%Y%m%d%H}0000-{horizon}h-{stream}-tf.bufr"


def _fetch_ecmwf(directory: Path, issue: datetime, source_id: str, stream: str) -> FetchResult:
    now = datetime.now(UTC)
    for cycle in _cycles(issue):
        for base_url in ECMWF_BASE_URLS:
            url = ecmwf_tf_url(cycle, stream, base_url=base_url)
            try:
                body = _get_optional(url, timeout=120)
            except DownloadError as exc:
                LOG.warning("%s: %s failed (%s), trying the next mirror", source_id, url, exc)
                continue
            if body is None:
                break  # not on this mirror means not published yet; mirrors agree
            name = f"{cycle:%Y%m%d%H}-{stream}-tf.bufr"
            _write(directory / name, body)
            return FetchResult(SourceStatus(source_id, True, fetched=now, url=url, cycle=f"{cycle:%Y%m%d%H}"), [name])
    raise DownloadError(f"no ECMWF {stream} track file in the last {CYCLE_LOOKBACK} cycles")


def fetch_ecmwf(directory: Path, issue: datetime) -> FetchResult:
    return _fetch_ecmwf(directory, issue, "ecmwf", "oper")


def fetch_ecmwfens(directory: Path, issue: datetime) -> FetchResult:
    return _fetch_ecmwf(directory, issue, "ecmwfens", "enfo")


def fetch_ibtracs(directory: Path) -> FetchResult:
    now = datetime.now(UTC)
    body = _get(IBTRACS_URL, timeout=120)
    name = "ibtracs.ACTIVE.list.v04r01.csv"
    _write(directory / name, body)
    return FetchResult(SourceStatus("ibtracs", True, fetched=now, url=IBTRACS_URL), [name])


def fetch_source(source_id: str, directory: Path, issue: datetime) -> FetchResult:
    if source_id == "jtwc":
        return fetch_jtwc(directory)
    if source_id == "nhc":
        return fetch_nhc(directory)
    if source_id == "gfs":
        return fetch_gfs(directory, issue)
    if source_id == "gefs":
        return fetch_gefs(directory, issue)
    if source_id == "ecmwf":
        return fetch_ecmwf(directory, issue)
    if source_id == "ecmwfens":
        return fetch_ecmwfens(directory, issue)
    if source_id == "ibtracs":
        return fetch_ibtracs(directory)
    raise XueError(f"unknown tc source {source_id!r}; choose from {', '.join(SOURCE_IDS)}")


def fetch_sources(
    raw_root: Path, issue: datetime, sources: tuple[str, ...] = SOURCE_IDS, *, force: bool = False
) -> dict[str, FetchResult]:
    """Fetch each source into its directory under the issue's raw
    directory. A source already fetched (its ``fetch.json`` says ok) is
    left alone unless ``force``; a failed one is retried."""
    root = issue_raw_directory(raw_root, issue)
    results: dict[str, FetchResult] = {}
    for source_id in sources:
        directory = root / source_id
        record = directory / "fetch.json"
        if not force and record.exists():
            try:
                existing = json.loads(record.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if existing and existing.get("ok"):
                LOG.info("tc %s: already fetched", source_id)
                results[source_id] = _result_from_record(existing)
                continue
        try:
            result = fetch_source(source_id, directory, issue)
            LOG.info("tc %s: fetched %d file(s)", source_id, len(result.files))
        except XueError as exc:
            LOG.warning("tc %s: %s", source_id, exc)
            result = FetchResult(SourceStatus(source_id, False, fetched=datetime.now(UTC), error=str(exc)), [])
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOG.warning("tc %s: %s", source_id, exc)
            result = FetchResult(
                SourceStatus(source_id, False, fetched=datetime.now(UTC), error=f"{type(exc).__name__}: {exc}"), []
            )
        _write(record, json.dumps(result.to_json(), indent=2).encode("utf-8"))
        results[source_id] = result
    return results


def _result_from_record(record: dict[str, Any]) -> FetchResult:
    fetched = record.get("fetched")
    status = SourceStatus(
        id=str(record.get("id")),
        ok=bool(record.get("ok")),
        fetched=datetime.fromisoformat(fetched) if isinstance(fetched, str) else None,
        url=record.get("url"),
        error=record.get("error"),
        cycle=record.get("cycle"),
        detail={k: v for k, v in record.items() if k not in ("id", "ok", "fetched", "url", "error", "cycle", "files")},
    )
    return FetchResult(status, [str(name) for name in record.get("files", [])])


def read_fetch_record(directory: Path, source_id: str) -> FetchResult | None:
    record = directory / source_id / "fetch.json"
    if not record.exists():
        return None
    try:
        return _result_from_record(json.loads(record.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return FetchResult(SourceStatus(source_id, False, error="fetch.json unreadable"), [])

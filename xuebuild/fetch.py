from __future__ import annotations

import gzip
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from .errors import DownloadError
from .idx import (
    ByteRange,
    ecmwf_field_byte_range,
    ecmwf_level_selector,
    field_byte_range,
)
from .model import GfsRun
from .sources import SourceSpec, source_spec
from .variables import VARIABLES

LOG = logging.getLogger(__name__)
# GFS objects (pgrb2 and sflux, including .idx files) are mirrored bit-for-bit
# on several clouds. Google's mirror serves through its global edge and is
# dramatically faster than the us-east-1 AWS bucket from East Asia (measured
# 2026-08-18: ~2.7 MB/s vs ~0.6 MB/s), so it is the default; override with
# XUE_GFS_BASE_URL (e.g. https://noaa-gfs-bdp-pds.s3.amazonaws.com).
BASE_URL = os.environ.get(
    "XUE_GFS_BASE_URL", "https://storage.googleapis.com/global-forecast-system"
)
# HRRR is mirrored the same way (registry.opendata.aws/noaa-hrrr-pds), and
# neither copy of a cycle fills in a whole run at once: the hours land one
# by one, not always in order, and one mirror can hold an hour the other
# does not yet (seen 2026-09-12: f002 on Google, not on S3, then the other
# way round). So a frame is fetched from the first mirror that has it, and a
# cycle is complete only when one mirror has every hour of it.
HRRR_BASE_URLS = tuple(
    url.strip().rstrip("/")
    for url in os.environ.get(
        "XUE_HRRR_BASE_URLS",
        "https://storage.googleapis.com/high-resolution-rapid-refresh,https://noaa-hrrr-bdp-pds.s3.amazonaws.com",
    ).split(",")
    if url.strip()
)
if not HRRR_BASE_URLS:
    raise ValueError("XUE_HRRR_BASE_URLS must contain at least one URL")
ECMWF_BASE_URLS = tuple(
    url.strip().rstrip("/")
    for url in os.environ.get(
        "XUE_ECMWF_BASE_URLS",
        ",".join(
            (
                "https://storage.googleapis.com/ecmwf-open-data",
                "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
                "https://data.ecmwf.int/forecasts",
            )
        ),
    ).split(",")
    if url.strip()
)
if not ECMWF_BASE_URLS:
    raise ValueError("XUE_ECMWF_BASE_URLS must contain at least one URL")
ECMWF_REQUEST_INTERVAL = float(os.environ.get("XUE_ECMWF_REQUEST_INTERVAL", "0.75"))
# Mirrors that answer bursts without throttling, so the pacing below does not
# apply to them. Google's copy took the whole 22-record set of eight frames
# four frames at a time — 184 range requests in one burst — with every
# response a 206 (measured 2026-09-11); the S3 bucket and data.ecmwf.int are
# the ones that answer 503 Slow Down, and stay paced.
ECMWF_UNPACED_BASE_URLS = tuple(
    url.strip().rstrip("/")
    for url in os.environ.get("XUE_ECMWF_UNPACED_BASE_URLS", "https://storage.googleapis.com/ecmwf-open-data").split(",")
    if url.strip()
)
ECMWF_FRAME_ATTEMPTS = 3
# MRMS is on its own AWS Open Data bucket (registry.opendata.aws/noaa-mrms-pds),
# anonymous, listable, and about a minute behind real time. There is no
# mirror and no ``.idx``: a frame is one whole gzipped GRIB per product,
# named by the product's observation time to the second, which only a
# directory listing can tell.
MRMS_BASE_URL = os.environ.get("XUE_MRMS_BASE_URL", "https://noaa-mrms-pds.s3.amazonaws.com").rstrip("/")
MRMS_DOMAIN = "CONUS"
MRMS_FETCH_FILENAME = "fetch.json"
CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
USER_AGENT = "xue/0.1 (+https://registry.opendata.aws/noaa-gfs-bdp-pds/)"
_ECMWF_PACING_LOCK = threading.Lock()
_ECMWF_NEXT_REQUEST_AT = 0.0


def floor_to_cycle(now: datetime, cycle_hours: int = 6) -> datetime:
    """The most recent cycle start at or before ``now``."""
    current = now.astimezone(UTC)
    return current.replace(hour=(current.hour // cycle_hours) * cycle_hours, minute=0, second=0, microsecond=0)


def parse_run(value: str, model: str = "gfs") -> GfsRun:
    """A ``YYYYMMDDHH`` cycle of ``model``, on one of the hours it runs."""
    spec = source_spec(model)
    cycles = ", ".join(f"{hour:02d}" for hour in range(0, 24, spec.cycle_hours)) if spec.cycle_hours > 1 else "any hour"
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as exc:
        raise DownloadError(f"--run must be 'latest' or YYYYMMDDHH at {cycles} UTC") from exc
    if parsed.hour % spec.cycle_hours:
        raise DownloadError(f"{spec.manifest_model} cycles start at {cycles} UTC, not {parsed.hour:02d}")
    try:
        return GfsRun(parsed)
    except ValueError as exc:
        raise DownloadError(str(exc)) from exc


# NOAA moved the per-cycle files into an ``atmos/`` subdirectory with this
# run; the archived cycles before it (the bucket reaches back to about
# 2021-01) keep them directly under the cycle, so historical showcase cases
# built from old runs need the flat layout.
ATMOS_SUBDIRECTORY_FROM = "2021032300"


def _noaa_cycle_prefix(run: GfsRun) -> str:
    subdirectory = "atmos/" if run.id >= ATMOS_SUBDIRECTORY_FROM else ""
    return f"{BASE_URL}/gfs.{run.date}/{run.cycle}/{subdirectory}"


def object_url(run: GfsRun, forecast_hour: int) -> str:
    return f"{_noaa_cycle_prefix(run)}gfs.t{run.cycle}z.pgrb2.0p25.f{forecast_hour:03d}"


def sflux_object_url(run: GfsRun, forecast_hour: int) -> str:
    """GFS surface flux files, published next to pgrb2 in the same bucket."""
    return f"{_noaa_cycle_prefix(run)}gfs.t{run.cycle}z.sfluxgrbf{forecast_hour:03d}.grib2"


def hrrr_object_url(run: GfsRun, forecast_hour: int, *, base_url: str | None = None) -> str:
    """The HRRR 2-D surface file of one forecast hour — its own bucket
    (``noaa-hrrr-bdp-pds``, mirrored by Google like GFS), one directory per
    day, the CONUS domain's files under ``conus/``, the hour in two digits:
    the model never runs past F48."""
    base = (base_url or HRRR_BASE_URLS[0]).rstrip("/")
    return f"{base}/hrrr.{run.date}/conus/hrrr.t{run.cycle}z.wrfsfcf{forecast_hour:02d}.grib2"


def wave_object_url(run: GfsRun, forecast_hour: int) -> str:
    """The cycle's GFS-Wave gridded file on the global 0.25° grid — the
    ``wave`` companion family of the GFS source. It sits beside ``atmos/``
    rather than inside it, and has since the wave model joined the cycle
    with the same GFSv16 upgrade that introduced ``atmos/``. Its files land
    on their own schedule — the f240 usually within minutes of the pgrb2
    f240, occasionally twenty minutes after it — which is why a run is not
    complete until its companion frames are there too."""
    return f"{BASE_URL}/gfs.{run.date}/{run.cycle}/wave/gridded/gfswave.t{run.cycle}z.global.0p25.f{forecast_hour:03d}.grib2"


def companion_object_url(run: GfsRun, forecast_hour: int, family: str) -> str:
    """The object one companion family (``sources.CompanionFile.id``) of a
    NOAA source publishes for one forecast hour."""
    if family == "wave":
        return wave_object_url(run, forecast_hour)
    raise DownloadError(f"unknown companion file family: {family}")


# The ECMWF open data stream one companion family is read from: the wave
# model's output sits beside the atmosphere's ``oper`` stream under its own
# directory, one file and one ``.index`` per step on the same 0.25° grid
# and axis.
ECMWF_COMPANION_STREAMS: dict[str, str] = {"wave": "wave"}


def ecmwf_object_url(
    run: GfsRun, forecast_hour: int, *, base_url: str | None = None, stream: str = "oper"
) -> str:
    """ECMWF open data with an unpadded ``-{h}h-`` step in the object name:
    the ``oper`` atmosphere by default, or a companion family's stream."""
    filename = f"{run.date}{run.cycle}0000-{forecast_hour}h-{stream}-fc.grib2"
    base = (base_url or ECMWF_BASE_URLS[0]).rstrip("/")
    return f"{base}/{run.date}/{run.cycle}z/ifs/0p25/{stream}/{filename}"


def ecmwf_companion_object_url(
    run: GfsRun, forecast_hour: int, family: str, *, base_url: str | None = None
) -> str:
    """The object one companion family of the ECMWF source publishes for one
    forecast hour, on one mirror."""
    try:
        stream = ECMWF_COMPANION_STREAMS[family]
    except KeyError:
        raise DownloadError(f"unknown ECMWF companion file family: {family}") from None
    return ecmwf_object_url(run, forecast_hour, base_url=base_url, stream=stream)


# -- MRMS ---------------------------------------------------------------------
#
# ``CONUS/<Product>_<Level>/<YYYYMMDD>/MRMS_<Product>_<Level>_<YYYYMMDD>-<HHMMSS>.grib2.gz``,
# one object per product per frame. The composite reflectivity is stamped a
# jittered forty seconds past each two-minute mark (``000042``, ``000241``,
# ``000437``...), the precipitation rate on the mark; neither can be
# computed, so the day's directory is listed and each key's time is snapped
# down to its slot (``SourceSpec.cadence_seconds``).

_MRMS_KEY_TIME = re.compile(r"_(\d{8})-(\d{6})\.grib2\.gz$")
_S3_NAMESPACE = "{http://s3.amazonaws.com/doc/2006-03-01/}"


@dataclass(frozen=True)
class MrmsObject:
    """One MRMS object of one product, as the bucket lists it."""

    key: str
    observed: datetime
    """The observation time in the object name, to the second."""

    def slot(self, cadence_seconds: int) -> datetime:
        """The cadence slot the observation falls in: its time snapped down."""
        seconds = int(self.observed.timestamp()) // cadence_seconds * cadence_seconds
        return datetime.fromtimestamp(seconds, tz=UTC)


def mrms_product_prefix(product: str, day: datetime) -> str:
    """The bucket prefix of one product's directory for one UTC day."""
    return f"{MRMS_DOMAIN}/{product}/{day.astimezone(UTC):%Y%m%d}/"


def mrms_object_url(key: str) -> str:
    return f"{MRMS_BASE_URL}/{key}"


def parse_mrms_listing(xml_text: str) -> tuple[list[MrmsObject], str | None]:
    """The objects of one ``ListObjectsV2`` page and its continuation token,
    if the page was truncated. Keys that are not a product frame — the
    bucket carries nothing else under a product day, but a listing is
    somebody else's file — are skipped."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise DownloadError(f"MRMS listing is not XML: {exc}") from exc
    objects: list[MrmsObject] = []
    for contents in root.iter(f"{_S3_NAMESPACE}Contents"):
        key = contents.findtext(f"{_S3_NAMESPACE}Key") or ""
        match = _MRMS_KEY_TIME.search(key)
        if not match:
            continue
        try:
            observed = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            continue
        objects.append(MrmsObject(key=key, observed=observed))
    token = None
    if (root.findtext(f"{_S3_NAMESPACE}IsTruncated") or "").lower() == "true":
        token = root.findtext(f"{_S3_NAMESPACE}NextContinuationToken") or None
        if token is None:
            raise DownloadError("MRMS listing is truncated but carries no continuation token")
    return objects, token


def list_mrms_objects(product: str, day: datetime, *, fetch: Callable[[str], str] | None = None) -> list[MrmsObject]:
    """Every frame of one product on one UTC day, in key order. A day is at
    most 720 two-minute frames, one page at ``max-keys=1000``; the
    continuation token is followed all the same."""
    fetch = fetch or fetch_text
    prefix = mrms_product_prefix(product, day)
    objects: list[MrmsObject] = []
    token: str | None = None
    while True:
        query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token is not None:
            query["continuation-token"] = token
        page, token = parse_mrms_listing(fetch(f"{MRMS_BASE_URL}/?{urllib.parse.urlencode(query)}"))
        objects.extend(page)
        if token is None:
            return objects


def mrms_window_frames(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    input_ids: tuple[str, ...] | None = None,
    *,
    fetch: Callable[[str], str] | None = None,
) -> dict[datetime, dict[str, MrmsObject]]:
    """The frames of one window, keyed by slot: for every slot from the run's
    hour through ``hours`` past it (inclusive) that every requested product
    has an object in, the object of each product, in the source's input
    order. A slot any product lacks is left out — the axis allows the gap,
    and the next build takes the slot if the object lands later. Two objects
    of one product in one slot (a reissue) resolve to the later one.
    ``input_ids`` narrows the products, the way a bundle group's fetch
    does."""
    if spec.cadence_seconds is None:
        raise DownloadError(f"{spec.manifest_model} declares no observation cadence")
    wanted = spec.input_variable_ids if input_ids is None else tuple(vid for vid in spec.input_variable_ids if vid in input_ids)
    if not wanted:
        raise DownloadError(f"{spec.manifest_model} has no products to fetch for {input_ids}")
    start = run.time
    end = start + timedelta(hours=hours)
    days: list[datetime] = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    per_product: list[dict[datetime, MrmsObject]] = []
    for variable_id in wanted:
        product = VARIABLES[variable_id].mrms_product
        if not product:
            raise DownloadError(f"{variable_id} is not an MRMS product")
        slots: dict[datetime, MrmsObject] = {}
        for listed_day in days:
            for item in list_mrms_objects(product, listed_day, fetch=fetch):
                slot = item.slot(spec.cadence_seconds)
                if start <= slot <= end and (slot not in slots or item.observed > slots[slot].observed):
                    slots[slot] = item
        per_product.append(slots)
    common = sorted(set.intersection(*(set(slots) for slots in per_product)))
    return {slot: {vid: slots[slot] for vid, slots in zip(wanted, per_product)} for slot in common}


def mrms_frame_name(spec: SourceSpec, run: GfsRun, slot: datetime) -> str:
    """The local name of one fetched frame: ``mrms.<run>.t<HHMM>.grib2``,
    the slot's offset from the run — hours and minutes, not a forecast
    hour, since there is no forecast."""
    offset = int((slot - run.time).total_seconds())
    if offset < 0 or offset % 60:
        raise DownloadError(f"{spec.manifest_model} slot {slot.isoformat()} is not a whole minute after run {run.id}")
    return f"{spec.id}.{run.id}.t{offset // 3600:02d}{offset % 3600 // 60:02d}.grib2"


def _download_mrms_frame(
    spec: SourceSpec, run: GfsRun, slot: datetime, objects: dict[str, MrmsObject], destination: Path, *, force: bool
) -> Path:
    """One frame: each product's object, decompressed, in input order, one
    GRIB with one message per product — the shape every other source's
    frame has downstream of the download."""
    from .gdal import inspect_grib

    output = destination / mrms_frame_name(spec, run, slot)
    if output.exists() and not force:
        try:
            for variable_id in objects:
                inspect_grib(output, variable_id)
            LOG.info("reusing readable GRIB %s", output)
            return output
        except Exception:
            raise DownloadError(
                "existing GRIB is unreadable or lacks required records (a file fetched "
                f"before a product joined the download set qualifies), "
                f"enable a forced download to replace it: {output}"
            )
    LOG.info("downloading GRIB %s", output)
    payload = b""
    for variable_id, item in objects.items():
        url = mrms_object_url(item.key)
        response = _request(url)
        with response:
            status = getattr(response, "status", None)
            if status != 200:
                raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
            body = _read_response(response)
        try:
            payload += gzip.decompress(body)
        except (OSError, EOFError) as exc:
            raise DownloadError(f"MRMS object is not a gzip file: {url}: {exc}") from exc
    _atomic_write(output, payload)
    try:
        for variable_id in objects:
            inspect_grib(output, variable_id)
    except Exception as exc:
        if output.exists():
            output.unlink()
        raise DownloadError(f"downloaded GRIB cannot be read by GDAL: {output}: {exc}") from exc
    return output


def _fetch_mrms_run(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool,
    input_ids: tuple[str, ...] | None,
) -> list[Path]:
    """Fetch one window of the MRMS mosaic (:func:`mrms_window_frames`),
    ``spec.fetch_concurrency`` frames at a time, and leave a ``fetch.json``
    beside the frames saying which object served each slot — what a
    rolling rebuild reads to know which frames it already has. Results are
    in slot order."""
    destination = raw_root / f"{spec.id}.{run.id}"
    frames = mrms_window_frames(spec, run, hours, input_ids)
    if not frames:
        raise DownloadError(f"{spec.manifest_model} has no frames on the bucket for run {run.id} through +{hours} h")
    LOG.info("%s run %s: %d frames of %d products", spec.manifest_model, run.id, len(frames), len(next(iter(frames.values()))))

    def download(slot: datetime) -> Path:
        return _download_mrms_frame(spec, run, slot, frames[slot], destination, force=force)

    slots = list(frames)
    if spec.fetch_concurrency <= 1:
        paths = [download(slot) for slot in slots]
    else:
        with ThreadPoolExecutor(max_workers=spec.fetch_concurrency) as executor:
            paths = list(executor.map(download, slots))
    record = {
        "model": spec.id,
        "run": run.id,
        "hours": hours,
        "cadenceSeconds": spec.cadence_seconds,
        "frames": [
            {
                "path": path.name,
                "slot": slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "objects": {
                    variable_id: {"key": item.key, "observed": item.observed.strftime("%Y-%m-%dT%H:%M:%SZ")}
                    for variable_id, item in frames[slot].items()
                },
            }
            for slot, path in zip(slots, paths)
        ],
    }
    (destination / MRMS_FETCH_FILENAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return paths


def _mrms_run_is_complete(spec: SourceSpec, run: GfsRun, hours: int, *, fetch: Callable[[str], str] | None = None) -> bool:
    """Whether the window has fully landed: the bucket carries a frame of
    every product at or past the window's end. The end slot itself may be a
    gap — an observation axis allows one — so what proves the window is
    that the products have moved past it."""
    end = run.time + timedelta(hours=hours)
    for variable_id in spec.input_variable_ids:
        product = VARIABLES[variable_id].mrms_product
        latest = None
        for day in (end, end + timedelta(days=1)):
            objects = list_mrms_objects(product, day, fetch=fetch)
            if objects:
                latest = max(item.observed for item in objects)
                break
        if latest is None or latest < end:
            return False
    return True


def model_object_url(run: GfsRun, forecast_hour: int, model: str) -> str:
    if model == "ecmwf":
        return ecmwf_object_url(run, forecast_hour)
    if model == "sflux":
        return sflux_object_url(run, forecast_hour)
    if model == "hrrr":
        return hrrr_object_url(run, forecast_hour)
    return object_url(run, forecast_hour)


def _is_ecmwf_url(url: str) -> bool:
    return any(url == base or url.startswith(base + "/") for base in ECMWF_BASE_URLS)


def _is_paced_url(url: str) -> bool:
    """Whether a request to ``url`` must wait its turn: an ECMWF mirror that
    is not on the unpaced list."""
    if not _is_ecmwf_url(url):
        return False
    return not any(url == base or url.startswith(base + "/") for base in ECMWF_UNPACED_BASE_URLS)


def _pace_ecmwf_request(url: str) -> None:
    global _ECMWF_NEXT_REQUEST_AT
    if not _is_paced_url(url) or ECMWF_REQUEST_INTERVAL <= 0:
        return
    with _ECMWF_PACING_LOCK:
        now = time.monotonic()
        delay = max(0.0, _ECMWF_NEXT_REQUEST_AT - now)
        if delay:
            time.sleep(delay)
        _ECMWF_NEXT_REQUEST_AT = time.monotonic() + ECMWF_REQUEST_INTERVAL


def _retry_after_seconds(error: Exception) -> float | None:
    if not isinstance(error, urllib.error.HTTPError):
        return None
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _http_error_code(error: BaseException) -> int | None:
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, urllib.error.HTTPError):
            return current.code
        current = current.__cause__
    return None


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    attempts: int | None = None,
    timeout: float = 30,
    max_elapsed: float | None = None,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> object:
    is_ecmwf = _is_ecmwf_url(url)
    attempts = attempts or (4 if is_ecmwf else 6)
    max_elapsed = max_elapsed or (180 if is_ecmwf else 60)
    base_delay = 10.0 if is_ecmwf else 0.5
    max_delay = 60.0 if is_ecmwf else 8.0
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, method=method, headers=request_headers)
    last_error: Exception | None = None
    started = time.monotonic()
    for attempt in range(attempts):
        if attempt and time.monotonic() - started >= max_elapsed:
            break
        try:
            _pace_ecmwf_request(url)
            return opener(request, timeout=timeout)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code in {
                408,
                429,
                500,
                502,
                503,
                504,
            }
            if not retryable or attempt + 1 == attempts:
                break
            retry_after = _retry_after_seconds(exc)
            delay_cap = min(max_delay, base_delay * (2**attempt))
            delay = retry_after if retry_after is not None else random.uniform(0, delay_cap)
            remaining = max_elapsed - (time.monotonic() - started)
            if remaining <= 0:
                break
            delay = min(delay, remaining)
            LOG.warning("request failed (%s), retrying in %.1fs: %s", exc, delay, url)
            time.sleep(delay)
    raise DownloadError(f"request failed for {url}: {last_error}") from last_error


def remote_exists(url: str) -> bool:
    try:
        response = _request(url, method="HEAD", timeout=15)
        with response:
            return getattr(response, "status", None) == 200
    except DownloadError as exc:
        if _http_error_code(exc) == 404:
            return False
        raise


def _run_is_complete(
    run: GfsRun,
    hours: int,
    model: str,
    exists: Callable[[str], bool],
) -> bool:
    if model == "mrms":
        return _mrms_run_is_complete(source_spec(model), run, hours)
    if model == "hrrr":
        # Every hour, on one mirror: the hours of a cycle land out of order
        # and the two copies disagree for a while, so the ends prove nothing.
        # A mirror that cannot be probed is a mirror that cannot serve.
        for base_url in HRRR_BASE_URLS:
            try:
                if all(
                    exists(hrrr_object_url(run, hour, base_url=base_url))
                    for hour in source_spec(model).forecast_hours(hours)
                ):
                    return True
            except DownloadError as exc:
                LOG.warning("could not probe HRRR mirror %s: %s", base_url, exc)
        return False
    if model != "ecmwf":
        urls = [model_object_url(run, 0, model), model_object_url(run, hours, model)]
        for companion in source_spec(model).companion_files:
            urls += [companion_object_url(run, 0, companion.id), companion_object_url(run, hours, companion.id)]
        return all(exists(url) for url in urls)

    # Both ends of every family, on one mirror: the wave stream lands on
    # its own schedule, and a run is complete only when it is there too.
    transient_error: DownloadError | None = None
    for base_url in ECMWF_BASE_URLS:
        urls = [ecmwf_object_url(run, 0, base_url=base_url), ecmwf_object_url(run, hours, base_url=base_url)]
        for companion in source_spec(model).companion_files:
            urls += [
                ecmwf_companion_object_url(run, hour, companion.id, base_url=base_url) for hour in (0, hours)
            ]
        try:
            if all(exists(url) for url in urls):
                return True
        except DownloadError as exc:
            transient_error = exc
            LOG.warning("could not probe ECMWF mirror %s: %s", base_url, exc)
    if transient_error is not None:
        raise DownloadError(
            f"could not establish whether ECMWF run {run.id} is complete"
        ) from transient_error
    return False


def resolve_run(
    value: str,
    *,
    hours: int,
    now: datetime | None = None,
    max_cycles: int = 20,
    exists: Callable[[str], bool] = remote_exists,
    model: str = "gfs",
) -> GfsRun:
    spec = source_spec(model)
    label = spec.manifest_model
    if not spec.fetched:
        raise DownloadError(f"{label} is read from a local file, not fetched")
    if spec.observation:
        # A fetched observation has no published axis: a run is the window
        # starting at the run's hour, ``hours`` long, and it is complete when
        # the bucket has moved past its end. There is no "latest" window yet
        # — a live rolling window is the pointer's business, and there is no
        # pointer for it yet.
        if hours < 1:
            raise DownloadError(f"a {label} window must be at least an hour long")
        if value == "latest":
            raise DownloadError(f"{label} has no live feed yet: name the window's first hour with --run YYYYMMDDHH")
        run = parse_run(value, model)
        if not _run_is_complete(run, hours, model, exists):
            raise DownloadError(f"{label} run {run.id} has not fully landed on the bucket through +{hours} h")
        return run
    # Validate the horizon against the model's published axis up front, so an
    # off-axis --hours fails with the axis description instead of a 404.
    spec.forecast_hours(hours)
    if value != "latest":
        run = parse_run(value, model)
        if not _run_is_complete(run, hours, model, exists):
            raise DownloadError(f"{label} run {run.id} is incomplete for f000 through f{hours:03d}")
        return run

    cycle = timedelta(hours=spec.cycle_hours)
    candidate = floor_to_cycle(now or datetime.now(UTC), spec.cycle_hours)
    for _ in range(max_cycles):
        run = GfsRun(candidate)
        if model == "ecmwf" and hours > 90 and run.cycle in {"06", "18"}:
            candidate -= cycle
            continue
        LOG.info("checking %s run %s", label, run.id)
        if _run_is_complete(run, hours, model, exists):
            return run
        candidate -= cycle
    raise DownloadError(f"could not find a complete {label} cycle in the last {max_cycles} runs")


def _read_response(response: object) -> bytes:
    return response.read()  # type: ignore[attr-defined]


def fetch_text(url: str) -> str:
    response = _request(url)
    with response:
        status = getattr(response, "status", None)
        if status != 200:
            raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
        body = _read_response(response)
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DownloadError(f".idx response was not UTF-8: {url}") from exc


def fetch_range(url: str, byte_range: ByteRange) -> bytes:
    expected_header = f"bytes={byte_range.start}-{byte_range.end}"
    response = _request(url, headers={"Range": expected_header})
    with response:
        status = getattr(response, "status", None)
        headers = getattr(response, "headers", {})
        content_range = headers.get("Content-Range")
        body = _read_response(response)
    if status != 206:
        raise DownloadError(f"expected HTTP 206 for Range request, received {status}")
    match = CONTENT_RANGE_RE.match(content_range or "")
    if not match:
        raise DownloadError(f"missing or invalid Content-Range: {content_range!r}")
    start, end = int(match.group(1)), int(match.group(2))
    if (start, end) != (byte_range.start, byte_range.end):
        raise DownloadError(
            f"Content-Range mismatch: requested {byte_range.start}-{byte_range.end}, received {start}-{end}"
        )
    if len(body) != byte_range.length:
        raise DownloadError(f"Range body length mismatch: expected {byte_range.length}, received {len(body)}")
    return body


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _frame_variable_ids(
    spec: SourceSpec, forecast_hour: int, input_ids: tuple[str, ...] | None = None
) -> tuple[str, ...]:
    """Input variables one frame of this source actually carries — the
    analysis file can lack some (sflux has no PRATE record at f000).

    ``input_ids`` narrows the download to the variables a build needs, which
    is what lets a showcase case fetch only its own fields."""
    wanted = spec.input_variable_ids if input_ids is None else input_ids
    if forecast_hour == 0:
        return tuple(variable_id for variable_id in wanted if variable_id not in spec.optional_at_analysis)
    return tuple(wanted)


def _download_noaa_records(url: str, variable_ids: tuple[str, ...]) -> bytes:
    """The requested records of one NOAA object, located through its
    ``.idx`` sidecar and fetched as byte ranges, in the order given."""
    if not variable_ids:
        return b""
    index_text = fetch_text(url + ".idx")
    byte_ranges = [
        field_byte_range(
            index_text,
            variable.index_field,
            excluded_phrases=variable.excluded_index_phrases,
            alternate_fields=variable.alternate_index_fields,
        )
        for variable in (VARIABLES[variable_id] for variable_id in variable_ids)
    ]
    return b"".join(fetch_range(url, byte_range) for byte_range in byte_ranges)


def _download_noaa_payload(
    run: GfsRun, forecast_hour: int, spec: SourceSpec, input_ids: tuple[str, ...] | None = None
) -> bytes:
    """One frame's GRIB: the primary file's records, then each companion
    family's — so a frame narrowed to one family's variables (a bundle group
    of the fan-out build) touches only that family's object."""
    wanted = _frame_variable_ids(spec, forecast_hour, input_ids)
    payload = _download_noaa_records(
        model_object_url(run, forecast_hour, spec.id),
        tuple(variable_id for variable_id in wanted if spec.companion_of(variable_id) is None),
    )
    for companion in spec.companion_files:
        url = companion_object_url(run, forecast_hour, companion.id)
        records = _download_noaa_records(
            url, tuple(variable_id for variable_id in wanted if variable_id in companion.variable_ids)
        )
        if records and companion.repack:
            records = _repack_grid_simple(records, url)
        payload += records
    return payload


def _repack_grid_simple(records: bytes, url: str) -> bytes:
    """The same GRIB messages repacked to ``grid_simple`` with eccodes — for
    a family whose packing the wheel's GDAL cannot read (GFS-Wave's JPEG
    2000). Done on the family's bytes alone, before they join the frame, so
    the pgrb2 records keep the packing they came with."""
    from .eccodescli import repack_grid_simple

    with tempfile.TemporaryDirectory(prefix="xue-repack-") as scratch:
        raw = Path(scratch) / "records.grib2"
        repacked = Path(scratch) / "records.simple.grib2"
        raw.write_bytes(records)
        try:
            repack_grid_simple(raw, repacked)
            return repacked.read_bytes()
        except Exception as exc:
            raise DownloadError(f"could not repack GRIB records from {url}: {exc}") from exc


def _download_hrrr_payload(
    run: GfsRun, forecast_hour: int, spec: SourceSpec, input_ids: tuple[str, ...] | None = None
) -> bytes:
    """One HRRR frame from the first mirror that has it: a mirror still
    missing the hour answers 404 for the ``.idx``, and the next is tried;
    any other failure is the frame's."""
    wanted = _frame_variable_ids(spec, forecast_hour, input_ids)
    missing: list[str] = []
    for base_url in HRRR_BASE_URLS:
        url = hrrr_object_url(run, forecast_hour, base_url=base_url)
        try:
            return _download_noaa_records(url, wanted)
        except DownloadError as exc:
            if _http_error_code(exc) != 404:
                raise
            missing.append(base_url)
            LOG.warning("HRRR mirror has no f%02d of %s yet, trying the next: %s", forecast_hour, run.id, base_url)
    raise DownloadError(f"no HRRR mirror has run {run.id} f{forecast_hour:02d}: " + ", ".join(missing))


def _download_ecmwf_records(url: str, variable_ids: tuple[str, ...]) -> bytes:
    """The requested records of one ECMWF open data object, located through
    its ``.index`` sidecar and fetched as byte ranges, in the order given."""
    if not variable_ids:
        return b""
    index_text = fetch_text(url.removesuffix(".grib2") + ".index")
    byte_ranges = []
    for variable_id in variable_ids:
        variable = VARIABLES[variable_id]
        levtype, levelist = ecmwf_level_selector(variable)
        byte_ranges.append(
            ecmwf_field_byte_range(
                index_text,
                variable.ecmwf_param,
                levtype=levtype,
                levelist=levelist,
                alternate_params=variable.ecmwf_alternate_params,
            )
        )
    return b"".join(fetch_range(url, byte_range) for byte_range in byte_ranges)


def _download_ecmwf_payload(
    run: GfsRun, forecast_hour: int, spec: SourceSpec, input_ids: tuple[str, ...] | None = None
) -> bytes:
    """One frame's GRIB from the first mirror that serves the whole of it:
    the ``oper`` records, then each companion family's from its own stream
    — so a frame narrowed to one family's variables touches only that
    family's object, as on the NOAA side."""
    errors: list[str] = []
    wanted = _frame_variable_ids(spec, forecast_hour, input_ids)
    for base_url in ECMWF_BASE_URLS:
        try:
            payload = _download_ecmwf_records(
                ecmwf_object_url(run, forecast_hour, base_url=base_url),
                tuple(variable_id for variable_id in wanted if spec.companion_of(variable_id) is None),
            )
            for companion in spec.companion_files:
                payload += _download_ecmwf_records(
                    ecmwf_companion_object_url(run, forecast_hour, companion.id, base_url=base_url),
                    tuple(variable_id for variable_id in wanted if variable_id in companion.variable_ids),
                )
            # Index offsets are scoped to a particular replica. Restart the
            # whole frame on the next mirror if any range request fails.
            return payload
        except DownloadError as exc:
            errors.append(f"{base_url}: {exc}")
            LOG.warning(
                "ECMWF mirror failed for f%03d, switching source: %s",
                forecast_hour,
                base_url,
            )
    raise DownloadError(
        f"all ECMWF mirrors failed for run {run.id} f{forecast_hour:03d}: "
        + "; ".join(errors)
    )


def fetch_frame(
    run: GfsRun,
    forecast_hour: int,
    destination: Path,
    *,
    force: bool = False,
    model: str = "gfs",
    input_ids: tuple[str, ...] | None = None,
) -> Path:
    from .eccodescli import repack_grid_simple
    from .gdal import inspect_grib

    spec = source_spec(model)
    frame_variable_ids = _frame_variable_ids(spec, forecast_hour, input_ids)
    output = destination / f"{spec.id}.{run.id}.f{forecast_hour:03d}.grib2"
    if output.exists() and not force:
        try:
            for variable_id in frame_variable_ids:
                inspect_grib(output, variable_id)
            LOG.info("reusing readable GRIB %s", output)
            return output
        except Exception:
            raise DownloadError(
                "existing GRIB is unreadable or lacks required records (a file fetched "
                f"before a variable joined the download set qualifies), "
                f"enable a forced download to replace it: {output}"
            )
    LOG.info("downloading GRIB %s", output)
    url = model_object_url(run, forecast_hour, model)
    if spec.id == "ecmwf":
        payload = _download_ecmwf_payload(run, forecast_hour, spec, input_ids)
    elif spec.id == "hrrr":
        payload = _download_hrrr_payload(run, forecast_hour, spec, input_ids)
    else:
        payload = _download_noaa_payload(run, forecast_hour, spec, input_ids)
    if spec.id == "ecmwf":
        # Open data messages are CCSDS/AEC packed (DRS 5.42); repack to
        # grid_simple so any GDAL build can read the stored file.
        raw = output.with_suffix(".ccsds.grib2")
        repacked = output.with_suffix(".repack.grib2")
        _atomic_write(raw, payload)
        try:
            repack_grid_simple(raw, repacked)
            repacked.replace(output)
        except Exception as exc:
            raise DownloadError(f"could not repack ECMWF GRIB {url}: {exc}") from exc
        finally:
            raw.unlink(missing_ok=True)
            repacked.unlink(missing_ok=True)
    else:
        _atomic_write(output, payload)
    try:
        for variable_id in frame_variable_ids:
            inspect_grib(output, variable_id)
    except Exception as exc:
        if output.exists():
            output.unlink()
        raise DownloadError(f"downloaded GRIB cannot be read by GDAL: {url}: {exc}") from exc
    return output


def fetch_run(
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool = False,
    model: str = "gfs",
    input_ids: tuple[str, ...] | None = None,
) -> list[Path]:
    """Fetch every frame of a run, ``spec.fetch_concurrency`` frames at a
    time (each frame is several fresh HTTPS round-trips, so a sequential
    fetch is latency-bound, not bandwidth-bound). Results keep frame order.
    ECMWF retries a failed frame in place so completed frames remain reusable."""
    spec = source_spec(model)
    if spec.id == "mrms":
        return _fetch_mrms_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)
    destination = raw_root / f"{spec.id}.{run.id}"
    forecast_hours = spec.forecast_hours(hours)
    frame_attempts = ECMWF_FRAME_ATTEMPTS if model == "ecmwf" else 1

    def fetch_with_retries(hour: int) -> Path:
        for attempt in range(frame_attempts):
            try:
                return fetch_frame(run, hour, destination, force=force, model=model, input_ids=input_ids)
            except DownloadError as exc:
                if attempt + 1 == frame_attempts:
                    raise
                delay_cap = min(180.0, 60.0 * (2**attempt))
                delay = random.uniform(60.0, delay_cap)
                LOG.warning(
                    "ECMWF frame f%03d failed (%s), retrying in %.1fs",
                    hour,
                    exc,
                    delay,
                )
                time.sleep(delay)
        raise AssertionError("unreachable")

    if spec.fetch_concurrency <= 1:
        return [fetch_with_retries(hour) for hour in forecast_hours]
    with ThreadPoolExecutor(max_workers=spec.fetch_concurrency) as executor:
        return list(executor.map(fetch_with_retries, forecast_hours))

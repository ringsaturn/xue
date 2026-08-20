from __future__ import annotations

import logging
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from .errors import DownloadError
from .idx import ByteRange, ecmwf_field_byte_range, field_byte_range
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
ECMWF_BASE_URL = "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
USER_AGENT = "xue/0.1 (+https://registry.opendata.aws/noaa-gfs-bdp-pds/)"


def floor_to_cycle(now: datetime) -> datetime:
    current = now.astimezone(UTC)
    return current.replace(hour=(current.hour // 6) * 6, minute=0, second=0, microsecond=0)


def parse_run(value: str) -> GfsRun:
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError as exc:
        raise DownloadError("--run must be 'latest' or YYYYMMDDHH at 00, 06, 12, or 18 UTC") from exc
    try:
        return GfsRun(parsed)
    except ValueError as exc:
        raise DownloadError(str(exc)) from exc


def object_url(run: GfsRun, forecast_hour: int) -> str:
    filename = f"gfs.t{run.cycle}z.pgrb2.0p25.f{forecast_hour:03d}"
    return f"{BASE_URL}/gfs.{run.date}/{run.cycle}/atmos/{filename}"


def sflux_object_url(run: GfsRun, forecast_hour: int) -> str:
    """GFS surface flux files, published next to pgrb2 in the same bucket."""
    filename = f"gfs.t{run.cycle}z.sfluxgrbf{forecast_hour:03d}.grib2"
    return f"{BASE_URL}/gfs.{run.date}/{run.cycle}/atmos/{filename}"


def ecmwf_object_url(run: GfsRun, forecast_hour: int) -> str:
    """ECMWF open data on AWS: ``{date}/{HH}z/ifs/0p25/oper/…`` with an
    unpadded ``-{h}h-`` step in the object name."""
    filename = f"{run.date}{run.cycle}0000-{forecast_hour}h-oper-fc.grib2"
    return f"{ECMWF_BASE_URL}/{run.date}/{run.cycle}z/ifs/0p25/oper/{filename}"


def model_object_url(run: GfsRun, forecast_hour: int, model: str) -> str:
    if model == "ecmwf":
        return ecmwf_object_url(run, forecast_hour)
    if model == "sflux":
        return sflux_object_url(run, forecast_hour)
    return object_url(run, forecast_hour)


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    # Six attempts with 0.5 s doubling backoff (~15 s of waiting in total):
    # ECMWF's open data bucket answers bursts with 503 Slow Down, and three
    # quick retries give up long before it calms down.
    attempts: int = 6,
    timeout: float = 30,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> object:
    request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    request = urllib.request.Request(url, method=method, headers=request_headers)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
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
            delay = 0.5 * (2**attempt)
            LOG.warning("request failed, retrying in %.1fs: %s", delay, url)
            time.sleep(delay)
    raise DownloadError(f"request failed for {url}: {last_error}") from last_error


def remote_exists(url: str) -> bool:
    try:
        response = _request(url, method="HEAD", attempts=2, timeout=15)
        with response:
            return getattr(response, "status", None) == 200
    except DownloadError:
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
    label = source_spec(model).manifest_model
    if value != "latest":
        run = parse_run(value)
        if not exists(model_object_url(run, 0, model)) or not exists(model_object_url(run, hours, model)):
            raise DownloadError(f"{label} run {run.id} is incomplete for f000 through f{hours:03d}")
        return run

    candidate = floor_to_cycle(now or datetime.now(UTC))
    for _ in range(max_cycles):
        run = GfsRun(candidate)
        LOG.info("checking %s run %s", label, run.id)
        if exists(model_object_url(run, 0, model)) and exists(model_object_url(run, hours, model)):
            return run
        candidate -= timedelta(hours=6)
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


def _frame_variable_ids(spec: SourceSpec, forecast_hour: int) -> tuple[str, ...]:
    """Input variables one frame of this source actually carries — the
    analysis file can lack some (sflux has no PRATE record at f000)."""
    if forecast_hour == 0:
        return tuple(
            variable_id
            for variable_id in spec.input_variable_ids
            if variable_id not in spec.optional_at_analysis
        )
    return spec.input_variable_ids


def _download_noaa_payload(run: GfsRun, forecast_hour: int, spec: SourceSpec) -> bytes:
    url = model_object_url(run, forecast_hour, spec.id)
    index_text = fetch_text(url + ".idx")
    byte_ranges = [
        field_byte_range(
            index_text,
            variable.index_field,
            excluded_phrases=variable.excluded_index_phrases,
        )
        for variable in (
            VARIABLES[variable_id] for variable_id in _frame_variable_ids(spec, forecast_hour)
        )
    ]
    return b"".join(fetch_range(url, byte_range) for byte_range in byte_ranges)


def _download_ecmwf_payload(run: GfsRun, forecast_hour: int, spec: SourceSpec) -> bytes:
    url = ecmwf_object_url(run, forecast_hour)
    index_text = fetch_text(url.removesuffix(".grib2") + ".index")
    byte_ranges = [
        ecmwf_field_byte_range(index_text, VARIABLES[variable_id].ecmwf_param)
        for variable_id in spec.input_variable_ids
    ]
    return b"".join(fetch_range(url, byte_range) for byte_range in byte_ranges)


def fetch_frame(
    run: GfsRun,
    forecast_hour: int,
    destination: Path,
    *,
    force: bool = False,
    model: str = "gfs",
) -> Path:
    from .eccodescli import repack_grid_simple
    from .gdal import inspect_grib

    spec = source_spec(model)
    frame_variable_ids = _frame_variable_ids(spec, forecast_hour)
    output = destination / f"{spec.id}.{run.id}.f{forecast_hour:03d}.grib2"
    if output.exists() and not force:
        try:
            for variable_id in frame_variable_ids:
                inspect_grib(output, variable_id)
            LOG.info("reusing readable GRIB %s", output)
            return output
        except Exception:
            raise DownloadError(
                "existing GRIB is unreadable or lacks required records (files fetched "
                f"before the 10 m wind components joined the download set qualify), "
                f"enable a forced download to replace it: {output}"
            )
    LOG.info("downloading GRIB %s", output)
    url = model_object_url(run, forecast_hour, model)
    if spec.id == "ecmwf":
        payload = _download_ecmwf_payload(run, forecast_hour, spec)
    else:
        payload = _download_noaa_payload(run, forecast_hour, spec)
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
) -> list[Path]:
    """Fetch every frame of a run, ``spec.fetch_concurrency`` frames at a
    time (each frame is several fresh HTTPS round-trips, so a sequential
    fetch is latency-bound, not bandwidth-bound). Results keep frame order;
    the first failure propagates."""
    spec = source_spec(model)
    destination = raw_root / f"{spec.id}.{run.id}"
    forecast_hours = list(range(0, hours + 1, spec.step_hours))
    if spec.fetch_concurrency <= 1:
        return [fetch_frame(run, hour, destination, force=force, model=model) for hour in forecast_hours]
    with ThreadPoolExecutor(max_workers=spec.fetch_concurrency) as executor:
        return list(
            executor.map(
                lambda hour: fetch_frame(run, hour, destination, force=force, model=model), forecast_hours
            )
        )

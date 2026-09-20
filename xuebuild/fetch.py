from __future__ import annotations

import gzip
import json
import logging
import os
import random
import re
import shutil
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

from . import cmaarchive, jmacli, om2nccli
from . import satellite
from .satellite import fetch as satellite_fetch
from .satellite import producers as satellite_producers
from .errors import DownloadError
from .idx import (
    ByteRange,
    coalesce_ranges,
    ecmwf_field_byte_range,
    ecmwf_level_selector,
    field_byte_range,
    series_byte_ranges,
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
# GEFS is mirrored the same way (registry.opendata.aws/noaa-gefs-bdp-pds; the
# chem member's aerosol files under it, ``.idx`` sidecars included), so the
# aerosol source takes Google's copy too; override with XUE_GEFS_BASE_URL
# (e.g. https://noaa-gefs-pds.s3.amazonaws.com).
GEFS_BASE_URL = os.environ.get(
    "XUE_GEFS_BASE_URL", "https://storage.googleapis.com/gfs-ensemble-forecast-system"
).rstrip("/")
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
# NCEP CFSv2 has one AWS Open Data bucket and no mirror
# (registry.opendata.aws/noaa-cfs-pds), anonymous, with an ``.idx`` beside
# every object. Override with XUE_CFS_BASE_URL.
CFS_BASE_URL = os.environ.get("XUE_CFS_BASE_URL", "https://noaa-cfs-pds.s3.amazonaws.com").rstrip("/")
# MRMS is on its own AWS Open Data bucket (registry.opendata.aws/noaa-mrms-pds),
# anonymous, listable, and about a minute behind real time. There is no
# mirror and no ``.idx``: a frame is one whole gzipped GRIB per product,
# named by the product's observation time to the second, which only a
# directory listing can tell.
MRMS_BASE_URL = os.environ.get("XUE_MRMS_BASE_URL", "https://noaa-mrms-pds.s3.amazonaws.com").rstrip("/")
MRMS_DOMAIN = "CONUS"
MRMS_FETCH_FILENAME = "fetch.json"
# What a built window holds, written beside its manifest by `build-bin` for
# an observation source (`xuebuild.cli`), from the fetch record below.
WINDOW_FILENAME = "window.json"
# The JMA precipitation nowcast tile service (xuebuild/sources.py, `jma`):
# `targetTimes_N1.json` lists the last three hours of five-minute analyses
# and is the one document this module reads itself; the tiles are fetched
# and decoded by the jma-radar tool (xuebuild/jmacli.py).
JMA_BASE_URL = os.environ.get("XUE_JMA_BASE_URL", "https://www.jma.go.jp/bosai/jmatile/data/nowc").rstrip("/")
JMA_TARGET_TIMES_URL = f"{JMA_BASE_URL}/targetTimes_N1.json"
JMA_ELEMENT = "hrpns"
# What the tool is asked for, and what `production_grid` describes: the
# zoom-8 tiles onto a square 0.005° grid over the radar coverage envelope,
# each cell the strongest class of the pixels it holds. A zoom-8 pixel is
# about 0.0055° of longitude (and 0.004-0.005° of latitude over Japan), so
# 0.005° is the finest grid the tiles support without a coarser zoom's
# nearest pick or a finer zoom's fifteenfold tile count; 0.01° read as
# blocks a kilometre wide at a city zoom.
JMA_ZOOM = 8
JMA_GRID_STEP = 0.005
JMA_BBOX = (121.0, 20.5, 149.0, 45.5)
JMA_RESAMPLING = "max"
# The tool's frame cache — one file per decoded frame, keyed by the grid —
# shared by every run under the raw root so a rolling rebuild fetches one
# frame and a rotation none. `make pull-r2-frames` / `push-r2-frames` keep a
# copy on the bucket, so a fresh runner does not ask the agency again.
JMA_FRAMES_DIRNAME = "jma-frames"
JMA_FETCH_CONCURRENCY = 6
# The Open-Meteo open data bucket (xuebuild/sources.py, `ifshres`): one
# directory per model under `data_spatial/`, one `.om` file per time step
# under a run, and a `meta.json` written after the last step — the run's
# completion marker, and the one document this module reads itself. The
# steps are read by the om2nc tool (xuebuild/om2nccli.py), which takes a
# mirror through its own `OM2NC_ENDPOINT`.
OPEN_METEO_BASE_URL = os.environ.get("XUE_OPEN_METEO_BASE_URL", "https://openmeteo.s3.amazonaws.com").rstrip("/")
OPEN_METEO_META_FILENAME = "meta.json"
# The CMA radar mosaic's archive (xuebuild/cmaarchive.py): one Zarr store
# per UTC day on a private bucket, named by the environment alone.
CMA_ARCHIVE_VARIABLE = cmaarchive.ARCHIVE_VARIABLE
CMA_PRODUCT = cmaarchive.PRODUCT
CMA_ZOOM = cmaarchive.ZOOM
CMA_GRID_STEP = cmaarchive.GRID_STEP
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


def gefsaero_object_url(run: GfsRun, forecast_hour: int) -> str:
    """The GEFS-Aerosols two-dimensional file of one forecast hour: the
    GEFS cycle's ``chem`` member, its 0.25° product directory (the only
    one the aerosol run publishes at that resolution), every field of the
    frame in one object with an ``.idx`` beside it — the NOAA layout, so
    the pgrb2 record path reads it."""
    return f"{GEFS_BASE_URL}/gefs.{run.date}/{run.cycle}/chem/pgrb2ap25/gefs.chem.t{run.cycle}z.a2d_0p25.f{forecast_hour:03d}.grib2"


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

# The sources served by the ECMWF open data service, by the model directory
# each is published under (``<date>/<cycle>z/<model>/0p25/<stream>/``): the
# IFS and the data-driven AIFS Single share the mirrors, the ``.index``
# shape, the CCSDS packing and the two streams, and are fetched by one
# path.
ECMWF_OPEN_DATA_MODELS: dict[str, str] = {"ecmwf": "ifs", "aifs": "aifs-single"}


def ecmwf_object_url(
    run: GfsRun,
    forecast_hour: int,
    *,
    base_url: str | None = None,
    stream: str = "oper",
    model: str = "ecmwf",
) -> str:
    """ECMWF open data with an unpadded ``-{h}h-`` step in the object name:
    the ``oper`` atmosphere by default, or a companion family's stream, of
    the IFS by default or of another source the service carries
    (:data:`ECMWF_OPEN_DATA_MODELS`)."""
    try:
        directory = ECMWF_OPEN_DATA_MODELS[model]
    except KeyError:
        raise DownloadError(f"{model} is not served by the ECMWF open data service") from None
    filename = f"{run.date}{run.cycle}0000-{forecast_hour}h-{stream}-fc.grib2"
    base = (base_url or ECMWF_BASE_URLS[0]).rstrip("/")
    return f"{base}/{run.date}/{run.cycle}z/{directory}/0p25/{stream}/{filename}"


def ecmwf_companion_object_url(
    run: GfsRun, forecast_hour: int, family: str, *, base_url: str | None = None, model: str = "ecmwf"
) -> str:
    """The object one companion family of an ECMWF open data source
    publishes for one forecast hour, on one mirror."""
    try:
        stream = ECMWF_COMPANION_STREAMS[family]
    except KeyError:
        raise DownloadError(f"unknown ECMWF companion file family: {family}") from None
    return ecmwf_object_url(run, forecast_hour, base_url=base_url, stream=stream, model=model)


# -- CFSv2 ---------------------------------------------------------------------
#
# ``cfs.<YYYYMMDD>/<HH>/time_grib_01/<name>.01.<run>.daily.grb2``, one object
# per variable holding that variable's whole nine-month run, with an ``.idx``
# beside it. The cycle's analysis sits in another family
# (``6hrly_grib_01/flxf<run>.01.<run>.grb2``) and is not read: the source's
# axis starts at the first six-hour step (``SourceSpec.first_hour``).

# Ensemble member: 01 is the only one of the four that runs the full nine
# months, and it is in every cycle.
CFS_MEMBER = "01"
# The object each input's records live in. These stems are NCEP's own
# spelling of the quantity and belong to this source alone — like an
# Open-Meteo variable name, but for one source only, so the registry does
# not carry them. The wind pair shares one object.
CFS_SERIES_FILES: dict[str, str] = {
    "tmp2m": "tmp2m",
    "prate": "prate",
    "ugrd10m": "wnd10m",
    "vgrd10m": "wnd10m",
    "tcdc": "tcdcclm",
    "dswrf": "dswsfc",
    "tmpsfc": "tmpsfc",
    "icec": "icecon",
    "icetk": "icethk",
}
# How many frames at each end of what a fetch wrote are read back with the
# GRIB2 header index. Every record came out of a range the server confirmed
# by Content-Range and length, so re-reading all eleven hundred frames would
# only cost time; the ends catch a mis-cut.
CFS_VERIFY_FRAMES = 2


def cfs_series_url(run: GfsRun, name: str) -> str:
    """The object holding one CFSv2 variable's whole run."""
    return f"{CFS_BASE_URL}/cfs.{run.date}/{run.cycle}/time_grib_{CFS_MEMBER}/{name}.{CFS_MEMBER}.{run.id}.daily.grb2"


def cfs_variable_url(run: GfsRun, variable_id: str) -> str:
    """The object one input variable is read from."""
    try:
        name = CFS_SERIES_FILES[variable_id]
    except KeyError:
        raise DownloadError(f"CFSv2 publishes no time series for {variable_id}") from None
    return cfs_series_url(run, name)


def cfs_frame_name(spec: SourceSpec, run: GfsRun, forecast_hour: int) -> str:
    """The GRIB one frame of a CFSv2 run is split into — the name every
    other source's fetch writes, the hour running to four digits of its own
    accord past f999."""
    return f"{spec.id}.{run.id}.f{forecast_hour:03d}.grib2"


def _cfs_series_ranges(
    index_text: str, variable_id: str, *, file_size: int | None = None
) -> dict[int, ByteRange]:
    """Where each frame of one input lives in its time-series object."""
    variable = VARIABLES[variable_id]
    return series_byte_ranges(
        index_text,
        variable.index_field,
        file_size=file_size,
        alternate_fields=variable.alternate_index_fields,
    )


def _cfs_run_is_complete(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    *,
    fetch: Callable[[str], str] | None = None,
    measure: Callable[[str], int | None] | None = None,
) -> bool:
    """Whether every object a CFSv2 run needs carries its records through
    ``hours``.

    A time-series object is written as the model runs, and its sidecar can
    describe records whose bytes have not landed, so two things are asked of
    each: the ``.idx`` names a record at ``hours`` *with a successor*, which
    is what fixes its end, and the object measures at least that far
    (:func:`remote_length`). The wind pair is asked twice, once per field,
    because the two are written in separate blocks.
    """
    read = fetch or fetch_text
    length = measure or remote_length
    for variable_id in spec.input_variable_ids:
        url = cfs_variable_url(run, variable_id)
        try:
            index_text = read(url + ".idx")
        except DownloadError as exc:
            if _http_error_code(exc) == 404:
                LOG.info("CFSv2 run %s has no %s time series yet", run.id, variable_id)
                return False
            raise
        # No file size: the sidecar's last record is left out, so a record
        # answered here is one whose end the sidecar itself fixes.
        byte_range = _cfs_series_ranges(index_text, variable_id).get(hours)
        if byte_range is None:
            LOG.info("CFSv2 run %s has no %s record at f%03d yet", run.id, variable_id, hours)
            return False
        measured = length(url)
        if measured is None or measured <= byte_range.end:
            LOG.info(
                "CFSv2 run %s has the %s f%03d record in its .idx but only %s bytes of data",
                run.id,
                variable_id,
                hours,
                measured,
            )
            return False
    return True


def _cfs_frames_to_fetch(
    paths: dict[int, Path], variable_ids: tuple[str, ...], *, force: bool
) -> list[int]:
    """The hours whose frame is missing or does not hold what this build
    asked for. A readable frame is reused as every other source's is, and a
    narrowed run downloads only the hours it is short of."""
    from .grib2 import inspect_grib_fast

    if force:
        return list(paths)
    missing: list[int] = []
    for hour, path in paths.items():
        if not path.is_file():
            missing.append(hour)
            continue
        try:
            frames = inspect_grib_fast(path, variable_ids)
        except Exception:
            missing.append(hour)
            continue
        if any(frame.lead_seconds != hour * 3600 for frame in frames.values()):
            missing.append(hour)
    return missing


def _cfs_object_index(url: str) -> tuple[str, int | None]:
    """One time-series object's sidecar and measured length. Read once per
    object rather than once per input, since the wind pair shares one."""
    return fetch_text(url + ".idx"), remote_length(url)


def _download_cfs_series(
    url: str,
    variable_id: str,
    hours: list[int],
    destination: Path,
    *,
    index_text: str,
    file_size: int | None,
) -> dict[int, tuple[int, int]]:
    """One input's records for ``hours``, in as few range requests as the
    object's layout allows, written to ``destination`` in hour order.

    Returns where each hour's record landed in that file. The records of one
    field are stored in hour order, so a whole run is one request and a run
    cut short is one per block the object interleaves (CFSv2's wind object
    alternates a block of U records with the same block of V records, so the
    pair costs one request each)."""
    ranges = _cfs_series_ranges(index_text, variable_id, file_size=file_size)
    absent = [hour for hour in hours if hour not in ranges]
    if absent:
        raise DownloadError(
            f"{url} carries no {variable_id} record at forecast hour {absent[0]} "
            f"({len(absent)} of {len(hours)} missing)"
        )
    wanted = [ranges[hour] for hour in hours]
    blobs = coalesce_ranges(wanted)
    LOG.info("downloading %s of %s in %d range request(s)", variable_id, url.rsplit("/", 1)[-1], len(blobs))
    placement: dict[int, tuple[int, int]] = {}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        bases: list[int] = []
        base = 0
        for blob in blobs:
            bases.append(base)
            handle.write(fetch_range(url, blob))
            base += blob.length
    index = 0
    for hour in hours:
        byte_range = ranges[hour]
        while not (blobs[index].start <= byte_range.start and byte_range.end <= blobs[index].end):
            index += 1
        placement[hour] = (bases[index] + byte_range.start - blobs[index].start, byte_range.length)
    return placement


def _fetch_cfs_run(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool = False,
    input_ids: tuple[str, ...] | None = None,
) -> list[Path]:
    """A CFSv2 run, fetched series-major and written frame-major.

    Every other fetched forecast reads one object per frame and takes the
    records it wants out of each; CFSv2 publishes the transpose, one object
    per variable holding the whole run, so a build reads each variable once
    as a handful of very large ranges and then cuts the frames out of what
    it read. The frames it writes are exactly what the frame-by-frame
    sources write — one GRIB per hour, the records in the order the build
    asked for them — so nothing downstream of the fetch knows the
    difference.

    A ``--bundles`` group narrows ``input_ids`` and so touches only that
    group's objects, which is what lets the fanned-out publish work
    unchanged.
    """
    from .grib2 import inspect_grib_fast

    destination = raw_root / f"{spec.id}.{run.id}"
    forecast_hours = spec.forecast_hours(hours)
    variable_ids = _frame_variable_ids(spec, forecast_hours[0], input_ids)
    if not variable_ids:
        raise DownloadError("a CFSv2 fetch needs at least one input variable")
    paths = {hour: destination / cfs_frame_name(spec, run, hour) for hour in forecast_hours}
    needed = _cfs_frames_to_fetch(paths, variable_ids, force=force)
    if not needed:
        LOG.info("reusing %d readable CFSv2 frames in %s", len(paths), destination)
        return [paths[hour] for hour in forecast_hours]
    LOG.info(
        "fetching %d of %d CFSv2 frames of run %s (%d variables)",
        len(needed),
        len(paths),
        run.id,
        len(variable_ids),
    )
    # The series are cut up as soon as they are all down, so they live in a
    # directory of their own under the run: `discover_inputs` lists files,
    # not directories, so a crash between the two leaves nothing a build
    # would mistake for a frame.
    series_root = destination / "series"
    series_root.mkdir(parents=True, exist_ok=True)
    series_paths = {
        variable_id: series_root / f"{spec.id}.{run.id}.series.{variable_id}.grb2"
        for variable_id in variable_ids
    }
    try:
        # One sidecar read and one measurement per object, before anything is
        # downloaded: the wind pair shares an object, and a run that turns out
        # to be short of an hour says so before a hundred megabytes move.
        objects = {
            url: _cfs_object_index(url)
            for url in dict.fromkeys(cfs_variable_url(run, variable_id) for variable_id in variable_ids)
        }

        def download(variable_id: str) -> tuple[str, dict[int, tuple[int, int]]]:
            url = cfs_variable_url(run, variable_id)
            index_text, file_size = objects[url]
            return variable_id, _download_cfs_series(
                url,
                variable_id,
                needed,
                series_paths[variable_id],
                index_text=index_text,
                file_size=file_size,
            )

        workers = max(1, min(spec.fetch_concurrency, len(variable_ids)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            placements = dict(executor.map(download, variable_ids))
        handles = {variable_id: series_paths[variable_id].open("rb") for variable_id in variable_ids}
        try:
            for hour in needed:
                payload = bytearray()
                for variable_id in variable_ids:
                    offset, length = placements[variable_id][hour]
                    handle = handles[variable_id]
                    handle.seek(offset)
                    record = handle.read(length)
                    if len(record) != length:
                        raise DownloadError(f"{series_paths[variable_id]} is short of the f{hour:03d} record")
                    payload += record
                _atomic_write(paths[hour], bytes(payload))
        finally:
            for handle in handles.values():
                handle.close()
    finally:
        shutil.rmtree(series_root, ignore_errors=True)
    # Every frame is cut from ranges the server confirmed by Content-Range
    # and length, so the records are read back out of the ends of what was
    # written rather than out of all thousand-odd frames.
    for hour in needed[:CFS_VERIFY_FRAMES] + needed[-CFS_VERIFY_FRAMES:]:
        try:
            frames = inspect_grib_fast(paths[hour], variable_ids)
        except Exception as exc:
            raise DownloadError(f"the CFSv2 frame written for f{hour:03d} cannot be read: {exc}") from exc
        for variable_id, frame in frames.items():
            if frame.lead_seconds != hour * 3600:
                raise DownloadError(
                    f"the CFSv2 frame written for f{hour:03d} carries {variable_id} at "
                    f"f{frame.lead_seconds // 3600:03d}"
                )
    return [paths[hour] for hour in forecast_hours]


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
    # The same objects fetched under another run's name — the previous
    # window of the rolling feed, whose frames this one overlaps — are the
    # same frame; the file is linked into place rather than downloaded again.
    previous = None
    if not force:
        previous = _fetched_mrms_frames(destination.parent, spec).get(frozenset(item.key for item in objects.values()))
    if previous is not None and previous != output and previous.is_file():
        LOG.info("reusing %s as %s", previous.name, output.name)
        destination.mkdir(parents=True, exist_ok=True)
        try:
            os.link(previous, output)
        except OSError:
            shutil.copyfile(previous, output)
        return output
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


def window_summary(raw_dir: Path) -> dict[str, object]:
    """The frames a fetched window holds, off its ``fetch.json``: how many,
    and the first and newest slot. The newest slot is what a rolling
    publish compares the bucket against."""
    record_path = raw_dir / MRMS_FETCH_FILENAME
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        slots = [frame["slot"] for frame in record["frames"]]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DownloadError(f"no fetch record at {record_path}: {exc}") from exc
    if not slots:
        raise DownloadError(f"the fetch record at {record_path} holds no frames")
    return {
        "model": record.get("model"),
        "cadenceSeconds": record.get("cadenceSeconds"),
        "frameCount": len(slots),
        "firstSlot": min(slots),
        "latestSlot": max(slots),
    }


def _fetched_mrms_frames(raw_root: Path, spec: SourceSpec) -> dict[frozenset[str], Path]:
    """Every frame some run directory under ``raw_root`` has fetched, by the
    set of bucket objects it was assembled from — what the ``fetch.json``
    each fetch leaves records. Read once per fetch and memoized on the
    root, since a window's frames are looked up one by one."""
    cached = _FETCHED_MRMS_FRAMES.get(raw_root)
    if cached is not None:
        return cached
    frames: dict[frozenset[str], Path] = {}
    for record_path in sorted(raw_root.glob(f"{spec.id}.*/{MRMS_FETCH_FILENAME}")):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for frame in record.get("frames", []):
            keys = frozenset(item["key"] for item in frame.get("objects", {}).values())
            if keys and set(frame.get("objects", {})) == set(spec.input_variable_ids):
                frames[keys] = record_path.parent / frame["path"]
    _FETCHED_MRMS_FRAMES[raw_root] = frames
    return frames


_FETCHED_MRMS_FRAMES: dict[Path, dict[frozenset[str], Path]] = {}


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
    _FETCHED_MRMS_FRAMES.pop(raw_root, None)
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


def latest_mrms_slot(
    spec: SourceSpec, *, now: datetime | None = None, fetch: Callable[[str], str] | None = None
) -> datetime:
    """The newest slot every product has an object in — the end of the live
    window. Today's directory is listed for each product, and yesterday's
    too when the day has just begun or a product's day is still empty, so
    the answer is the same on either side of midnight. A feed with no
    common slot in two days is down."""
    if spec.cadence_seconds is None:
        raise DownloadError(f"{spec.manifest_model} declares no observation cadence")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    products = []
    for variable_id in spec.input_variable_ids:
        product = VARIABLES[variable_id].mrms_product
        if not product:
            raise DownloadError(f"{variable_id} is not an MRMS product")
        products.append(product)
    per_product = [
        {item.slot(spec.cadence_seconds) for item in list_mrms_objects(product, today, fetch=fetch)}
        for product in products
    ]
    if current - today < timedelta(hours=1) or not all(per_product):
        for slots, product in zip(per_product, products):
            slots.update(item.slot(spec.cadence_seconds) for item in list_mrms_objects(product, today - timedelta(days=1), fetch=fetch))
    common = set.intersection(*per_product) if per_product else set()
    if not common:
        raise DownloadError(f"{spec.manifest_model} has no frame of every product on the bucket today or yesterday")
    return max(common)


# -- JMA ----------------------------------------------------------------------
#
# The agency's page lists its analyses in `targetTimes_N1.json` as
# `{"basetime": "YYYYMMDDHHMMSS", "validtime": ..., "elements": [...]}`,
# newest first, an analysis being an entry whose two times agree; the
# listing reaches three hours back. Everything else — the tiles, their
# decoding, the grid — is the jma-radar tool's (xuebuild/jmacli.py), which
# reads the same listing itself.

_JMA_TIME_FORMAT = "%Y%m%d%H%M%S"


def parse_jma_listing(payload: str, element: str = JMA_ELEMENT) -> list[datetime]:
    """The analysis times a `targetTimes` document lists for `element`,
    oldest first: entries whose basetime and validtime agree (a forecast's
    validtime is later). Entries that are not that shape are skipped."""
    try:
        entries = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"JMA listing is not JSON: {exc}") from exc
    if not isinstance(entries, list):
        raise DownloadError("JMA listing is not a JSON array")
    times: set[datetime] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        basetime, validtime = entry.get("basetime"), entry.get("validtime")
        if not isinstance(basetime, str) or basetime != validtime or element not in entry.get("elements", []):
            continue
        try:
            times.add(datetime.strptime(basetime, _JMA_TIME_FORMAT).replace(tzinfo=UTC))
        except ValueError:
            continue
    return sorted(times)


def jma_analysis_times(*, fetch: Callable[[str], str] | None = None) -> list[datetime]:
    """Every analysis the tile service lists now, oldest first."""
    return parse_jma_listing((fetch or fetch_text)(JMA_TARGET_TIMES_URL))


def jma_window_slots(
    spec: SourceSpec, run: GfsRun, hours: int, *, fetch: Callable[[str], str] | None = None
) -> list[datetime]:
    """The listed analyses of one window: from the run's hour through
    ``hours`` past it, inclusive. Each is already on the five-minute mark,
    so the slot is the time itself."""
    start, end = run.time, run.time + timedelta(hours=hours)
    return [slot for slot in jma_analysis_times(fetch=fetch) if start <= slot <= end]


def latest_jma_slot(
    spec: SourceSpec, *, now: datetime | None = None, fetch: Callable[[str], str] | None = None
) -> datetime:
    """The newest analysis the tile service lists — the end of the live
    window. A listing with no analysis at all is a feed that is down."""
    times = jma_analysis_times(fetch=fetch)
    if not times:
        raise DownloadError(f"{spec.manifest_model} lists no analysis on the tile service")
    return times[-1]


def _jma_run_is_complete(
    spec: SourceSpec, run: GfsRun, hours: int, *, fetch: Callable[[str], str] | None = None
) -> bool:
    """Whether a named window has fully landed: the listing reaches past
    the window's end. The listing is three hours deep, so a window older
    than that cannot be fetched either way — the tiles outlive the listing
    by days, but this pipeline never guesses tile URLs."""
    times = jma_analysis_times(fetch=fetch)
    end = run.time + timedelta(hours=hours)
    return bool(times) and times[-1] >= end and times[0] <= run.time


def jma_frame_name(spec: SourceSpec, run: GfsRun) -> str:
    """The local name of a window's series file: ``jma.<run>.nc``."""
    return f"{spec.id}.{run.id}.nc"


#: How long, and how often, a fetch waits for the jma-radar tool's copy of
#: the listing to catch up with this build's. The listing is served through
#: a CDN that caches it for a minute per edge, and the tool reads it over a
#: connection of its own: the analysis this build was started for is on the
#: copy the build read and, a second later, not yet on the tool's. Three
#: waits of twenty seconds cover one cache lifetime.
JMA_LISTING_RETRIES = 3
JMA_LISTING_RETRY_SECONDS = 20.0


def _fetch_jma_run(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool,
    input_ids: tuple[str, ...] | None,
    fetch: Callable[[str], str] | None = None,
) -> list[Path]:
    """Fetch one window of the JMA nowcast through the jma-radar tool: the
    analyses the listing holds between the run's hour and ``hours`` past it,
    decoded onto the published grid and written as one NetCDF series, with
    a ``fetch.json`` beside it in the shape the MRMS fetch leaves (one
    entry per frame with its slot) so ``window_summary`` and the rolling
    publish read both alike. The tool keeps one file per decoded frame
    under ``raw_root/jma-frames``; ``force`` discards the window's cached
    frames first so they are decoded from fresh tiles. The tool's copy of
    the listing may lag the one this build read (``fetch``, the network by
    default) by up to the CDN's minute; while it delivers less than that
    listing holds for the window, the tool is asked again, so a rolling
    publish started for a new analysis does not publish a window without
    it."""
    if input_ids is not None and any(variable_id not in spec.input_variable_ids for variable_id in input_ids):
        raise DownloadError(f"{spec.manifest_model} publishes {list(spec.input_variable_ids)}, not {list(input_ids)}")
    jmacli.version()
    destination = raw_root / f"{spec.id}.{run.id}"
    frames_dir = raw_root / JMA_FRAMES_DIRNAME
    output = destination / jma_frame_name(spec, run)
    if force and frames_dir.is_dir():
        for slot in jma_window_slots(spec, run, hours):
            for cached in frames_dir.glob(f"*/{JMA_ELEMENT}_{slot:{_JMA_TIME_FORMAT}}.nc"):
                LOG.info("discarding cached frame %s", cached)
                cached.unlink()
    def window() -> dict:
        return jmacli.fetch_window(
            start=run.id,
            hours=hours,
            zoom=JMA_ZOOM,
            step=JMA_GRID_STEP,
            bbox=JMA_BBOX,
            method=JMA_RESAMPLING,
            frames_dir=frames_dir,
            output=output,
            variable=spec.input_variable_ids[0],
            concurrency=JMA_FETCH_CONCURRENCY,
        )

    def delivered(summary: dict) -> datetime | None:
        stamps = [frame["validtime"] for frame in summary["frames"]]
        return datetime.strptime(max(stamps), _JMA_TIME_FORMAT).replace(tzinfo=UTC) if stamps else None

    listed = jma_window_slots(spec, run, hours, fetch=fetch)
    expected = listed[-1] if listed else None
    summary = window()
    for attempt in range(JMA_LISTING_RETRIES):
        newest = delivered(summary)
        if expected is None or (newest is not None and newest >= expected):
            break
        LOG.info(
            "jma-radar delivered %s while the listing reaches %s; asking again in %.0f s (%d of %d)",
            newest.strftime("%Y-%m-%dT%H:%M:%SZ") if newest else "nothing",
            expected.strftime("%Y-%m-%dT%H:%M:%SZ"),
            JMA_LISTING_RETRY_SECONDS,
            attempt + 1,
            JMA_LISTING_RETRIES,
        )
        time.sleep(JMA_LISTING_RETRY_SECONDS)
        summary = window()
    else:
        newest = delivered(summary)
        if expected is not None and (newest is None or newest < expected):
            LOG.warning(
                "jma-radar still delivers %s while the listing reaches %s; building what it delivered",
                newest.strftime("%Y-%m-%dT%H:%M:%SZ") if newest else "nothing",
                expected.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
    frames = summary["frames"]
    if not frames:
        raise DownloadError(f"{spec.manifest_model} lists no analysis for run {run.id} through +{hours} h")
    if not output.is_file():
        raise DownloadError(f"jma-radar reported a window but wrote no series at {output}")
    record = {
        "model": spec.id,
        "run": run.id,
        "hours": hours,
        "cadenceSeconds": spec.cadence_seconds,
        "grid": summary.get("grid"),
        "series": output.name,
        "frames": [
            {
                "path": output.name,
                "slot": datetime.strptime(frame["validtime"], _JMA_TIME_FORMAT).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "basetime": frame["basetime"],
                "frame": frame.get("path"),
            }
            for frame in frames
        ],
    }
    (destination / MRMS_FETCH_FILENAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    LOG.info("%s run %s: %d frames in %s", spec.manifest_model, run.id, len(frames), output)
    return [output]


def latest_observation_slot(spec: SourceSpec, *, now: datetime | None = None) -> datetime:
    """The newest frame a live observation source's feed holds — what the
    rolling publish compares the live window against, and what
    ``resolve_run("latest")`` ends the window at."""
    if spec.id == "mrms":
        return latest_mrms_slot(spec, now=now)
    if spec.id == "jma":
        return latest_jma_slot(spec, now=now)
    if spec.id == "cma":
        return latest_cma_slot(spec, now=now)
    if spec.platform is not None:
        return latest_satellite_slot(spec, now=now)
    raise DownloadError(f"{spec.manifest_model} is not a live observation source")


# -- CMA radar mosaic ---------------------------------------------------------
#
# The archive is one Zarr store per UTC day, each with a complete 240-slot
# ``time`` axis and a ``slot_status`` saying which slots were written (1),
# never fetched (0) or never published by the portal (2). Everything below
# reads it through xuebuild/cmaarchive.py: a listing reads the two small
# index arrays of each day the window touches, a fetch reads the written
# frames as well and writes them as one NetCDF series.

_CMA_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def cma_archive() -> str:
    """The archive base (an ``s3://bucket/prefix`` or a directory), from
    :data:`CMA_ARCHIVE_VARIABLE`; unset is the operator's error."""
    return cmaarchive.archive_base()


def cma_archive_slots(
    start: datetime, hours: int, *, written: Callable[[str, datetime, datetime], list[datetime]] | None = None
) -> list[datetime]:
    """The written slots the archive holds from ``start`` through ``hours``
    past it, inclusive, in time order — the frames a window from there
    would hold. ``written`` is the archive lookup (the bucket by default)."""
    base = cma_archive()
    return sorted((written or cmaarchive.written_slots)(base, start, start + timedelta(hours=hours)))


def cma_window_slots(
    spec: SourceSpec, run: GfsRun, hours: int, *, written: Callable[..., list[datetime]] | None = None
) -> list[datetime]:
    """The written slots of one window: from the run's hour through
    ``hours`` past it, inclusive. Each is on the six-minute mark."""
    return cma_archive_slots(run.time, hours, written=written)


def latest_cma_slot(
    spec: SourceSpec, *, now: datetime | None = None, written: Callable[..., list[datetime]] | None = None
) -> datetime:
    """The newest slot the archive has written — the end of the live
    window. The stores of today and yesterday are asked (the day the
    window starts in and the one before it, so the answer is the same on
    either side of midnight); an archive with nothing written in the last
    day is a feed that is down."""
    current = (now or datetime.now(UTC)).astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    start = current - timedelta(hours=25)
    slots = cma_archive_slots(start, 26, written=written)
    if not slots:
        raise DownloadError(f"{spec.manifest_model} has no written slot in the archive since {start.isoformat()}")
    return slots[-1]


def _cma_run_is_complete(
    spec: SourceSpec, run: GfsRun, hours: int, *, written: Callable[..., list[datetime]] | None = None
) -> bool:
    """Whether a named window has fully landed: the archive has written a
    slot at or past the window's end. The hour after the end is asked for
    too, so a window whose last slot the portal never published still
    counts once the archive has moved past it."""
    end = run.time + timedelta(hours=hours)
    slots = cma_archive_slots(run.time, hours + 1, written=written)
    return any(slot >= end for slot in slots)


def cma_frame_name(spec: SourceSpec, run: GfsRun) -> str:
    """The local name of a window's series file: ``cma.<run>.nc``."""
    return f"{spec.id}.{run.id}.nc"


def _fetch_cma_run(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool,
    input_ids: tuple[str, ...] | None,
) -> list[Path]:
    """Fetch one window of the CMA mosaic out of its archive: the written
    slots between the run's hour and ``hours`` past it, read out of the
    daily stores and written as one NetCDF series, with a ``fetch.json``
    beside it in the shape the MRMS fetch leaves (one entry per frame with
    its slot) so ``window_summary`` and the rolling publish read both
    alike. Nothing is cached between rounds: a window is a few megabytes
    of range reads and the archive's newest frames change every round, so
    the series is always read afresh (``force`` changes nothing)."""
    if input_ids is not None and any(variable_id not in spec.input_variable_ids for variable_id in input_ids):
        raise DownloadError(f"{spec.manifest_model} publishes {list(spec.input_variable_ids)}, not {list(input_ids)}")
    base = cma_archive()
    destination = raw_root / f"{spec.id}.{run.id}"
    output = destination / cma_frame_name(spec, run)
    if output.exists():
        output.unlink()
    window = cmaarchive.read_window(base, run.time, run.time + timedelta(hours=hours))
    if window is None:
        raise DownloadError(f"{spec.manifest_model} has no written slot for run {run.id} through +{hours} h")
    cmaarchive.write_series(window, output)
    if not output.is_file():
        raise DownloadError(f"the CMA series was not written at {output}")
    record = {
        "model": spec.id,
        "run": run.id,
        "hours": hours,
        "cadenceSeconds": spec.cadence_seconds,
        "grid": window.grid,
        "series": output.name,
        "frames": [{"path": output.name, "slot": slot.strftime(_CMA_TIME_FORMAT)} for slot in window.times],
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / MRMS_FETCH_FILENAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    LOG.info("%s run %s: %d frames in %s", spec.manifest_model, run.id, len(window.times), output)
    return [output]


# -- Geostationary satellites ------------------------------------------------
#
# A satellite source (``SourceSpec.platform``) is fetched through
# xuebuild/satellite/: a window's slots are listed on the agency's bucket,
# each new slot's tiles fetched, mosaicked and warped onto the published
# plate carrée grid as one cached frame, and the window's frames stacked
# into one NetCDF series — the shape the JMA and CMA feeds arrive in, so
# the converter downstream is the observation path unchanged.


def open_meteo_run_url(spec: SourceSpec, run: GfsRun) -> str:
    """The directory one run of an Open-Meteo model lives in, with its
    trailing slash: ``data_spatial/<model>/YYYY/MM/DD/HH00Z/``."""
    if spec.open_meteo is None:
        raise DownloadError(f"{spec.manifest_model} is not an Open-Meteo source")
    return f"{OPEN_METEO_BASE_URL}/data_spatial/{spec.open_meteo}/{run.time:%Y/%m/%d/%H00Z}/"


def open_meteo_resolution(spec: SourceSpec) -> float:
    """The grid step om2nc resamples the reduced Gaussian grid onto, read
    off the grid the source publishes rather than declared twice: a global
    grid of ``width`` columns is ``360 / width`` degrees, and the fetch and
    a complete build's grid check can then only agree."""
    width, height = spec.production_grid
    step = 360.0 / width
    if round(180.0 / step) + 1 != height:
        raise DownloadError(
            f"{spec.manifest_model}: a {width} x {height} grid is not a global {step:g}° one"
        )
    return step


def _open_meteo_run_is_complete(
    spec: SourceSpec, run: GfsRun, *, fetch: Callable[[str], str] | None = None
) -> bool:
    """Whether a run has fully landed: Open-Meteo writes its ``meta.json``
    after the last time step, so the document existing, naming this run and
    saying ``completed`` is the whole test — one request per candidate
    cycle, and no listing. A missing document is a run that is still being
    written (or was never written), which is a False rather than an error;
    anything else about the response is an error, so a bucket that cannot be
    read never reads as an incomplete run."""
    url = f"{open_meteo_run_url(spec, run)}{OPEN_METEO_META_FILENAME}"
    try:
        payload = (fetch or fetch_text)(url)
    except DownloadError as exc:
        if _http_error_code(exc) == 404 or "received 404" in str(exc):
            return False
        raise
    try:
        meta = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DownloadError(f"{url} is not JSON: {exc}") from exc
    reference = str(meta.get("reference_time", "")).replace("Z", "+00:00")
    try:
        reference_time = datetime.fromisoformat(reference).astimezone(UTC)
    except ValueError:
        raise DownloadError(f"{url} names no readable reference_time") from None
    return bool(meta.get("completed")) and reference_time == run.time


def open_meteo_series_name(spec: SourceSpec, run: GfsRun, variable_id: str) -> str:
    """The local name of one variable's series:
    ``ifshres.<run>.<variable>.nc`` — the ``<stem>.<variable>.nc`` layout
    ``observation.series_files`` resolves a run directory by, with the Xue
    id in the name even though the variable inside keeps Open-Meteo's."""
    return f"{spec.id}.{run.id}.{variable_id}.nc"


def _fetch_open_meteo_run(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool,
    input_ids: tuple[str, ...] | None,
) -> list[Path]:
    """Fetch one run of an Open-Meteo model through the om2nc tool: one CF
    NetCDF series per variable under ``raw_root/<id>.<run>/``, with a
    ``fetch.json`` beside them recording what was asked for.

    One call of the tool per variable, run in order: the tool fetches a
    variable's steps in parallel itself, and one file per variable is what
    the converter reads (and what a fanned-out publish needs, since a job
    fetches only its own bundles' inputs). A variable the source lists as
    optional at the analysis is asked for its own steps — the analysis hour
    dropped — because its ``.om`` files simply do not carry it there. A
    series already on disk is left alone unless ``force``: the tool writes
    ``<name>.part`` and renames on success, so a file that exists is whole.
    """
    if spec.open_meteo is None:
        raise DownloadError(f"{spec.manifest_model} is not an Open-Meteo source")
    if input_ids is not None and any(variable_id not in spec.input_variable_ids for variable_id in input_ids):
        raise DownloadError(f"{spec.manifest_model} publishes {list(spec.input_variable_ids)}, not {list(input_ids)}")
    tool_version = om2nccli.version()
    variable_ids = tuple(
        variable_id
        for variable_id in spec.input_variable_ids
        if input_ids is None or variable_id in input_ids
    )
    if not variable_ids:
        raise DownloadError(f"a {spec.manifest_model} fetch needs at least one variable")
    unnamed = [variable_id for variable_id in variable_ids if not VARIABLES[variable_id].open_meteo]
    if unnamed:
        # A registry mistake rather than a run's problem: the tool takes the
        # Open-Meteo name and there is nothing to ask it for.
        raise DownloadError(f"the variable registry gives {unnamed} no Open-Meteo name")
    destination = raw_root / f"{spec.id}.{run.id}"
    destination.mkdir(parents=True, exist_ok=True)
    hours_axis = spec.forecast_hours(hours)
    resolution = open_meteo_resolution(spec)
    written: list[Path] = []
    record_variables: list[dict[str, object]] = []
    for variable_id in variable_ids:
        steps = [hour for hour in hours_axis if hour or variable_id not in spec.optional_at_analysis]
        output = destination / open_meteo_series_name(spec, run, variable_id)
        if output.is_file() and not force:
            LOG.info("reusing %s", output)
        else:
            om2nccli.fetch_variable(
                model=spec.open_meteo,
                init=f"{run.time:%Y-%m-%dT%H}Z",
                steps=steps,
                variable=VARIABLES[variable_id].open_meteo,
                resolution=resolution,
                output=output,
                concurrency=spec.fetch_concurrency,
            )
        written.append(output)
        record_variables.append({"variable": variable_id, "series": output.name, "steps": len(steps)})
    record = {
        "model": spec.id,
        "run": run.id,
        "hours": hours,
        "openMeteoModel": spec.open_meteo,
        "resolution": resolution,
        "om2nc": tool_version,
        "variables": record_variables,
    }
    (destination / MRMS_FETCH_FILENAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    LOG.info("%s run %s: %d series in %s", spec.manifest_model, run.id, len(written), destination)
    return written


def satellite_platform(spec: SourceSpec) -> satellite.Platform:
    if spec.platform is None:
        raise DownloadError(f"{spec.manifest_model} is not a satellite source")
    return satellite.platform(spec.platform)


def satellite_grid(spec: SourceSpec) -> satellite_fetch.TargetGrid:
    """The grid a satellite source is warped onto, held to the source
    table's ``production_grid`` so a build's completeness check and the
    fetch agree."""
    if spec.grid_step is None:
        raise DownloadError(f"{spec.manifest_model} declares no grid step")
    grid = satellite_fetch.target_grid(satellite_platform(spec), spec.grid_step)
    if (grid.width, grid.height) != spec.production_grid:
        raise DownloadError(
            f"{spec.manifest_model}: the platform's region at {spec.grid_step}° is {grid.width} x {grid.height}, "
            f"not the production grid {spec.production_grid}"
        )
    return grid


def latest_satellite_slot(
    spec: SourceSpec, *, now: datetime | None = None, fetch: Callable[[str], str] | None = None
) -> datetime:
    """The newest slot whose tiles have all landed for the source's first
    channel — the end of the live window."""
    platform = satellite_platform(spec)
    return satellite_fetch.latest_slot(
        platform, platform.channel(spec.input_variable_ids[0]), now=now, fetch=fetch, cadence_seconds=spec.cadence_seconds
    )


def _satellite_run_is_complete(
    spec: SourceSpec, run: GfsRun, hours: int, *, now: datetime | None = None, fetch: Callable[[str], str] | None = None
) -> bool:
    """Whether a named window has fully landed: the bucket's newest
    complete slot is at or past the window's end. ``now`` is where the
    reader's newest-few listing starts from (a day's directories, an
    hour's, a six-hour search), so a test on a fixture passes its own."""
    return latest_satellite_slot(spec, now=now, fetch=fetch) >= run.time + timedelta(hours=hours)


def satellite_ancillary_root(raw_root: Path) -> Path:
    """Where a producer's ancillary fields live, beside the run and frame
    directories: the staged CAMEL months ``make pull-r2-ancillary``
    mirrors under ``ancillary/camel/`` and the GFS records the DEBRA
    producer caches under ``ancillary/gfs/``."""
    return raw_root / "ancillary"


def satellite_series_stem(spec: SourceSpec, run: GfsRun) -> str:
    """The stem of a window's series files, one per variable:
    ``himawari.<run>`` → ``himawari.<run>.ir104.nc``."""
    return f"{spec.id}.{run.id}"


def _fetch_satellite_run(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool,
    input_ids: tuple[str, ...] | None,
    fetch: Callable[[str], str] | None = None,
    download: Callable[[str], bytes] | None = None,
) -> list[Path]:
    """Fetch one window of a satellite source: every slot from the run's
    hour through ``hours`` past it that the bucket holds whole, warped onto
    the published grid (or read back from the frame cache under
    ``raw_root/<role>-frames``, which ``force`` bypasses) and stacked into
    one NetCDF series, with a ``fetch.json`` beside it in the shape the
    MRMS fetch leaves so ``window_summary`` and the rolling publish read
    both alike."""
    if input_ids is not None and any(variable_id not in spec.input_variable_ids for variable_id in input_ids):
        raise DownloadError(f"{spec.manifest_model} publishes {list(spec.input_variable_ids)}, not {list(input_ids)}")
    platform = satellite_platform(spec)
    channel_ids = spec.input_variable_ids if input_ids is None else tuple(vid for vid in spec.input_variable_ids if vid in input_ids)
    channels = tuple(platform.channel(channel_id) for channel_id in channel_ids)
    # A composite is produced when every channel it reads is fetched: the
    # rule `published_bundle_ids` applies to the source table, applied to
    # what this fetch was asked for.
    producers = tuple(
        satellite_producers.producer_for(bundle_id)
        for bundle_id in spec.bundle_composite_ids
        if all(channel_id in channel_ids for channel_id in satellite_producers.producer_for(bundle_id).inputs_for(platform))
    )
    grid = satellite_grid(spec)
    destination = raw_root / f"{spec.id}.{run.id}"
    window = satellite_fetch.fetch_window(
        platform,
        channels,
        run.time,
        hours,
        grid=grid,
        raw_root=raw_root,
        destination=destination,
        series_stem=satellite_series_stem(spec, run),
        units={channel.id: VARIABLES[channel.id].output_unit for channel in channels},
        producers=producers,
        ancillary_root=satellite_ancillary_root(raw_root),
        cadence_seconds=spec.cadence_seconds,
        force=force,
        fetch=fetch,
        download=download,
        concurrency=spec.fetch_concurrency,
    )
    record = {
        "model": spec.id,
        "run": run.id,
        "hours": hours,
        "cadenceSeconds": spec.cadence_seconds,
        "platform": platform.spacecraft,
        "grid": {
            "step": grid.step,
            "width": grid.width,
            "height": grid.height,
            "firstLongitude": grid.first_longitude,
            "firstLatitude": grid.first_latitude,
        },
        "series": {variable_id: path.name for variable_id, path in window.series.items()},
        "producers": [
            {"bundle": producer.bundle_id, "id": producer.id, "version": producer.version, "inputs": list(producer.inputs_for(platform))}
            for producer in producers
        ],
        "frames": [
            {
                "slot": item.slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "frames": {variable_id: path.name for variable_id, path in item.frames.items()},
                "tilesFetched": item.tiles,
            }
            for item in window.slots
        ],
    }
    (destination / MRMS_FETCH_FILENAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    LOG.info("%s run %s: %d slots in %d series under %s", spec.manifest_model, run.id, len(window.slots), len(window.series), destination)
    return list(window.series.values())


def model_object_url(run: GfsRun, forecast_hour: int, model: str) -> str:
    if model in ECMWF_OPEN_DATA_MODELS:
        return ecmwf_object_url(run, forecast_hour, model=model)
    if model == "cfs":
        # CFSv2 has no object per forecast hour: a run is one object per
        # variable holding the whole series. The hour is ignored and the
        # run's reference object named — the one a probe would ask for.
        return cfs_variable_url(run, source_spec(model).input_variable_ids[0])
    if model == "sflux":
        return sflux_object_url(run, forecast_hour)
    if model == "hrrr":
        return hrrr_object_url(run, forecast_hour)
    if model == "gefsaero":
        return gefsaero_object_url(run, forecast_hour)
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


def remote_length(url: str) -> int | None:
    """How many bytes of an object the bucket is serving, or None when it
    does not exist or will not say. What tells an object still being
    written from one whose sidecar simply runs ahead of it."""
    try:
        response = _request(url, method="HEAD", timeout=15)
    except DownloadError as exc:
        if _http_error_code(exc) == 404:
            return None
        raise
    with response:
        if getattr(response, "status", None) != 200:
            return None
        value = getattr(response, "headers", {}).get("Content-Length")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _run_is_complete(
    run: GfsRun,
    hours: int,
    model: str,
    exists: Callable[[str], bool],
    *,
    now: datetime | None = None,
) -> bool:
    if model == "mrms":
        return _mrms_run_is_complete(source_spec(model), run, hours)
    if model == "jma":
        return _jma_run_is_complete(source_spec(model), run, hours)
    if model == "cma":
        return _cma_run_is_complete(source_spec(model), run, hours)
    if model == "cfs":
        return _cfs_run_is_complete(source_spec(model), run, hours)
    if source_spec(model).open_meteo is not None:
        return _open_meteo_run_is_complete(source_spec(model), run)
    if source_spec(model).platform is not None:
        return _satellite_run_is_complete(source_spec(model), run, hours, now=now)
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
    if model not in ECMWF_OPEN_DATA_MODELS:
        urls = [model_object_url(run, 0, model), model_object_url(run, hours, model)]
        for companion in source_spec(model).companion_files:
            urls += [companion_object_url(run, 0, companion.id), companion_object_url(run, hours, companion.id)]
        return all(exists(url) for url in urls)

    # Both ends of every family, on one mirror: the wave stream lands on
    # its own schedule, and a run is complete only when it is there too.
    label = source_spec(model).manifest_model
    transient_error: DownloadError | None = None
    for base_url in ECMWF_BASE_URLS:
        urls = [
            ecmwf_object_url(run, 0, base_url=base_url, model=model),
            ecmwf_object_url(run, hours, base_url=base_url, model=model),
        ]
        for companion in source_spec(model).companion_files:
            urls += [
                ecmwf_companion_object_url(run, hour, companion.id, base_url=base_url, model=model)
                for hour in (0, hours)
            ]
        try:
            if all(exists(url) for url in urls):
                return True
        except DownloadError as exc:
            transient_error = exc
            LOG.warning("could not probe %s mirror %s: %s", label, base_url, exc)
    if transient_error is not None:
        raise DownloadError(
            f"could not establish whether {label} run {run.id} is complete"
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
        # starting at the run's hour, ``hours`` long. A named window is a
        # past one and must have fully landed — the bucket has moved past its
        # end. The latest window is the live one: the ``hours`` whole hours
        # ending with the hour of the bucket's newest frame, so the run is
        # that hour less ``hours - 1`` and the window's end lies ahead of
        # the newest frame, by definition incomplete. The window advances
        # by an hour whenever the newest frame crosses one, and each build
        # in between is a fuller copy of the same run.
        if hours < 1:
            raise DownloadError(f"a {label} window must be at least an hour long")
        if value == "latest":
            if not spec.live:
                raise DownloadError(f"{label} has no live feed: name the window's first hour with --run YYYYMMDDHH")
            newest = latest_observation_slot(spec, now=now)
            start = newest.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours - 1)
            LOG.info("%s newest frame %s, live window from %s", label, newest.isoformat(), start.isoformat())
            return GfsRun(start)
        run = parse_run(value, model)
        if not _run_is_complete(run, hours, model, exists, now=now):
            raise DownloadError(f"{label} run {run.id} has not fully landed on the bucket through +{hours} h")
        return run
    # Validate the horizon against the model's published axis up front, so an
    # off-axis --hours fails with the axis description instead of a 404.
    spec.forecast_hours(hours)
    if value != "latest":
        run = parse_run(value, model)
        if not _run_is_complete(run, hours, model, exists, now=now):
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
        if _run_is_complete(run, hours, model, exists, now=now):
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
            qualifier=variable.index_qualifier,
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
                ecmwf_object_url(run, forecast_hour, base_url=base_url, model=spec.id),
                tuple(variable_id for variable_id in wanted if spec.companion_of(variable_id) is None),
            )
            for companion in spec.companion_files:
                payload += _download_ecmwf_records(
                    ecmwf_companion_object_url(run, forecast_hour, companion.id, base_url=base_url, model=spec.id),
                    tuple(variable_id for variable_id in wanted if variable_id in companion.variable_ids),
                )
            # Index offsets are scoped to a particular replica. Restart the
            # whole frame on the next mirror if any range request fails.
            return payload
        except DownloadError as exc:
            errors.append(f"{base_url}: {exc}")
            LOG.warning(
                "%s mirror failed for f%03d, switching source: %s",
                spec.manifest_model,
                forecast_hour,
                base_url,
            )
    raise DownloadError(
        f"all {spec.manifest_model} mirrors failed for run {run.id} f{forecast_hour:03d}: "
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
    if spec.id in ECMWF_OPEN_DATA_MODELS:
        payload = _download_ecmwf_payload(run, forecast_hour, spec, input_ids)
    elif spec.id == "hrrr":
        payload = _download_hrrr_payload(run, forecast_hour, spec, input_ids)
    else:
        payload = _download_noaa_payload(run, forecast_hour, spec, input_ids)
    if spec.id in ECMWF_OPEN_DATA_MODELS:
        # Open data messages are CCSDS/AEC packed (DRS 5.42), the IFS's and
        # the AIFS's alike; repack to grid_simple so any GDAL build can read
        # the stored file.
        raw = output.with_suffix(".ccsds.grib2")
        repacked = output.with_suffix(".repack.grib2")
        _atomic_write(raw, payload)
        try:
            repack_grid_simple(raw, repacked)
            repacked.replace(output)
        except Exception as exc:
            raise DownloadError(f"could not repack {spec.manifest_model} GRIB {url}: {exc}") from exc
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
    if spec.id == "jma":
        return _fetch_jma_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)
    if spec.id == "cma":
        return _fetch_cma_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)
    if spec.id == "cfs":
        return _fetch_cfs_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)
    if spec.platform is not None:
        return _fetch_satellite_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)
    if spec.open_meteo is not None:
        return _fetch_open_meteo_run(spec, run, hours, raw_root, force=force, input_ids=input_ids)
    destination = raw_root / f"{spec.id}.{run.id}"
    forecast_hours = spec.forecast_hours(hours)
    frame_attempts = ECMWF_FRAME_ATTEMPTS if model in ECMWF_OPEN_DATA_MODELS else 1

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
                    "%s frame f%03d failed (%s), retrying in %.1fs",
                    spec.manifest_model,
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

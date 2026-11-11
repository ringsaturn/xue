"""CMA GRAPES GEPS, the China Meteorological Administration's global
ensemble, as WMO WIS2 exchanges it: the ensemble's central values, cut out
of its product files into the one-GRIB-per-hour frames both encoders read
(``docs/contribution/sources.md``, "cmageps").

CMA publishes no members: each lead time is one GRIB2 file of *ensemble
products*, ``Z_NAFP_C_BABJ_<run>0000_P_CMA-WIPPSGEPS-GLB-<HHH>.grib2``, about
80 MB of JPEG2000-packed records — the mean and spread of the 500 hPa height
and the sea-level pressure (template 4.2), the 0/25/50/75/100th percentiles
of the instantaneous fields (4.6) and of the precipitation over a window
(4.10), and exceedance probabilities (4.5, 4.9). The files carry no index,
so a frame is a walk of the message heads over Range — section 0 gives each
message's length, sections 1 and 4 its identity — and one Range read of
each record wanted. The WIS2 global caches carry the files (:data:`CACHES`,
:mod:`xuebuild.wis2`): the two that can be listed prove a run complete
together, neither being complete on its own, and a lead time is read from
the first that serves it, CMA's own cache, read by name, last.

What is published is the ensemble's centre under the deterministic
identities every other source uses: the mean where CMA writes a mean (the
height and the pressure), the median (50th percentile) everywhere else.
Each chosen record is rewritten to the plain template those identities are
matched on — 4.2 and 4.6 become 4.0, 4.10 becomes 4.8 with its interval and
statistical process kept — by ``grib_set``, and the frame is then repacked
from JPEG2000 to ``grid_simple`` like any frame whose packing GDAL may not
read (:attr:`~xuebuild.sources.SourceSpec.packing`). The median is a
field of its own, not a member: it is smoother than any run of the model,
and a precipitation median is drier than the mean wherever only some
members rain.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import wis2
from .errors import ConversionError, DownloadError
from .grib2 import MessageInfo, matches_variable
from .idx import ByteRange
from .model import GfsRun
from .sources import SourceSpec
from .variables import VARIABLES
from .wis2 import Cache, CacheObject, NamedCache

LOG = logging.getLogger(__name__)

#: The WIS2 global caches, in the order a lead time is read from them: the
#: Met Office/NOAA one first, which is beside the runners that build, then
#: JMA's — the two that can be listed, whose listings together prove a run
#: complete — then CMA's own, which cannot be listed but keeps files for
#: days, read by name under the day each file arrived. Further caches are
#: further entries (KMA's and DWD's serve the product by name too, but
#: keep it about a day).
NOAA_CACHE_URL = os.environ.get("XUE_CMAGEPS_NOAA_CACHE_URL", wis2.NOAA_CACHE_URL).rstrip("/")
NOAA_CACHE_PREFIX = "data/cn-cma/data/core/weather/prediction/forecast/medium-range/probabilistic/global/"
JMA_CACHE_URL = os.environ.get("XUE_CMAGEPS_JMA_CACHE_URL", wis2.JMA_CACHE_URL).rstrip("/")
JMA_CACHE_PREFIX = "wis2/data/cn-cma:data.core.weather.prediction.forecast.medium-range.probabilistic.global/"
CMA_CACHE_URL = os.environ.get("XUE_CMAGEPS_CMA_CACHE_URL", "https://gc.wis.cma.cn").rstrip("/")
CACHES: tuple[Cache | NamedCache, ...] = (
    Cache("noaa", NOAA_CACHE_URL, NOAA_CACHE_PREFIX),
    Cache("jma", JMA_CACHE_URL, JMA_CACHE_PREFIX),
    NamedCache(
        "cma",
        CMA_CACHE_URL
        + "/{date}/cache/a/wis2/cn-cma/data/core/weather/prediction/forecast/medium-range/probabilistic/global/{file}",
    ),
)

_NAME_RE = re.compile(r"^Z_NAFP_C_BABJ_(\d{10})0000_P_CMA-WIPPSGEPS-GLB-(\d{3})\.grib2$")

#: The bytes read of each message to identify it: sections 0, 1, 3 and 4
#: of these files are under 200 bytes together. A head that ends before
#: section 4 does is read again, longer, up to the cap.
HEAD_BYTES = 512
HEAD_LIMIT = 8192

#: The ensemble products the walk reads (code table 4.0): the derived
#: forecast of every member (4.2: ``derivedForecast`` 0 is the unweighted
#: mean), the percentile forecasts (4.6, and 4.10 over a time interval),
#: and the templates each becomes once it is the deterministic field it
#: stands in for.
MEAN_TEMPLATE = 2
PERCENTILE_TEMPLATE = 6
PERCENTILE_INTERVAL_TEMPLATE = 10
PLAIN_TEMPLATES = {MEAN_TEMPLATE: 0, PERCENTILE_TEMPLATE: 0, PERCENTILE_INTERVAL_TEMPLATE: 8}
#: The derived forecast (code table 4.7) and the percentile the centre is.
UNWEIGHTED_MEAN = 0
MEDIAN = 50

GROUND_SURFACE = 1
#: A record's surface as the file spells it → the surface it is published
#: on. CMA writes its instantaneous gust (0/2/22) at 10 m above the ground;
#: the instantaneous gust GFS publishes, and the identity the registry's
#: ``gust`` carries, is the same quantity on the ground surface.
SURFACE_ALIASES: dict[tuple[tuple[int, int, int], int, float], int] = {((0, 2, 22), 103, 10.0): GROUND_SURFACE}

_MISSING_UINT32 = 0xFFFFFFFF
_HOURS = 1


def file_name(run: GfsRun, hour: int) -> str:
    """The name of one lead time's file."""
    return f"Z_NAFP_C_BABJ_{run.time:%Y%m%d%H}0000_P_CMA-WIPPSGEPS-GLB-{hour:03d}.grib2"


def parse_file_name(name: str) -> tuple[datetime, int] | None:
    """``(cycle, lead hour)`` of a file name, or None for anything else — a
    partial upload, whose numeric suffix stands where ``.grib2`` would."""
    match = _NAME_RE.match(name)
    if not match:
        return None
    try:
        cycle = datetime.strptime(match.group(1), "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None
    return cycle, int(match.group(2))


def object_url(run: GfsRun, hour: int) -> str:
    """One lead time's file on the Met Office/NOAA cache, the one cache
    that keys a file by its name alone (the JMA cache's key is learnt from
    a listing)."""
    return wis2.object_url(CACHES[0], f"{NOAA_CACHE_PREFIX}{file_name(run, hour)}")


def list_run(run: GfsRun, *, fetch: Callable[[str], str]) -> dict[int, list[CacheObject]]:
    """Every file of one run on the listable caches, by lead hour, in the
    order to try them."""
    return wis2.list_run(CACHES, run, parse_file_name, fetch=fetch)


def candidates(run: GfsRun, hour: int, objects: dict[int, list[CacheObject]]) -> list[CacheObject]:
    """Where one lead time's file is read from, in :data:`CACHES` order."""
    return wis2.candidates(CACHES, run, objects, hour, file_name(run, hour))


def missing_hours(spec: SourceSpec, hours: int, objects: dict[int, list[CacheObject]]) -> list[int]:
    return [hour for hour in spec.forecast_hours(hours) if not objects.get(hour)]


def run_is_complete(spec: SourceSpec, run: GfsRun, hours: int, *, fetch: Callable[[str], str]) -> bool:
    """Whether the caches together list every lead time of the axis through
    ``hours``. A file appears on a cache whole, and the six-hourly files a
    run publishes are all the axis asks for (the three-hourly ones to +78 h
    are never read)."""
    return not missing_hours(spec, hours, list_run(run, fetch=fetch))


# -- One message -----------------------------------------------------------


class HeadTruncated(Exception):
    """The bytes read end before the message's section 4 does."""


@dataclass(frozen=True)
class EnsembleRecord:
    """One message of a product file, as far as its head says."""

    offset: int
    length: int
    discipline: int
    category: int
    number: int
    template: int
    level_type: int
    level_scale: int | None
    level_scaled_value: int | None
    reference_time: datetime
    forecast_hours: int | None
    """The forecast time in hours; None when the file gives it in another unit."""
    derived: int | None = None
    """Code table 4.7 of a derived forecast (template 4.2)."""
    percentile: int | None = None
    """The percentile of a percentile forecast (4.6, 4.10)."""
    interval_end: datetime | None = None
    statistical: int | None = None
    interval_hours: int | None = None
    """End, code table 4.10 process and length in hours of the one time
    range of a 4.10 record; None elsewhere, and the length None too when
    the record has more than one range or another unit."""

    @property
    def triple(self) -> tuple[int, int, int]:
        return self.discipline, self.category, self.number

    @property
    def level_value(self) -> float | None:
        if self.level_scaled_value is None or self.level_scale is None:
            return None
        return self.level_scaled_value / (10.0**self.level_scale)


def _time(body: bytes, offset: int) -> datetime:
    year, month, day, hour, minute, second = struct.unpack_from(">HBBBBB", body, offset)
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def _section4(offset: int, length: int, discipline: int, reference_time: datetime, body: bytes) -> EnsembleRecord:
    template = struct.unpack_from(">H", body, 7)[0]
    if len(body) < 34:
        raise ConversionError("a GRIB2 product definition section is too short")
    scale = struct.unpack_from(">b", body, 23)[0]
    scaled = struct.unpack_from(">I", body, 24)[0]
    missing_level = scaled == _MISSING_UINT32
    forecast_time = struct.unpack_from(">i", body, 18)[0]
    record = EnsembleRecord(
        offset=offset,
        length=length,
        discipline=discipline,
        category=body[9],
        number=body[10],
        template=template,
        level_type=body[22],
        level_scale=None if missing_level else scale,
        level_scaled_value=None if missing_level else scaled,
        reference_time=reference_time,
        forecast_hours=forecast_time if body[17] == _HOURS else None,
    )
    if template == MEAN_TEMPLATE and len(body) >= 36:
        return replace(record, derived=body[34])
    if template == PERCENTILE_TEMPLATE and len(body) >= 35:
        return replace(record, percentile=body[34])
    if template == PERCENTILE_INTERVAL_TEMPLATE and len(body) >= 59:
        ranges = body[42]
        length_hours = struct.unpack_from(">I", body, 50)[0]
        return replace(
            record,
            percentile=body[34],
            interval_end=_time(body, 35),
            statistical=body[47],
            interval_hours=length_hours if ranges == 1 and body[49] == _HOURS else None,
        )
    return record


def parse_head(head: bytes, offset: int, source: str) -> tuple[int, EnsembleRecord]:
    """The total length and identity of the message whose first bytes are
    ``head``. Raises :class:`HeadTruncated` when ``head`` ends before
    section 4 does, and :class:`DownloadError` for anything that is not a
    GRIB2 message."""
    if len(head) < 16 or head[:4] != b"GRIB" or head[7] != 2:
        raise DownloadError(f"no GRIB2 message at offset {offset} in {source}")
    total = struct.unpack_from(">Q", head, 8)[0]
    discipline = head[6]
    reference_time: datetime | None = None
    position = 16
    while position + 5 <= total - 4:
        if position + 5 > len(head):
            raise HeadTruncated
        section_length, number = struct.unpack_from(">IB", head, position)
        if section_length < 5 or position + section_length > total - 4:
            raise DownloadError(f"a GRIB2 section length is out of bounds at offset {offset} in {source}")
        if number in (1, 4) and position + section_length > len(head):
            raise HeadTruncated
        body = head[position : position + section_length]
        try:
            if number == 1:
                reference_time = _time(body, 12)
            elif number == 4:
                if reference_time is None:
                    raise DownloadError(f"a GRIB2 product definition precedes the identification in {source}")
                return total, _section4(offset, total, discipline, reference_time, body)
        except (ValueError, struct.error, ConversionError) as exc:
            raise DownloadError(f"unreadable GRIB2 message head at offset {offset} in {source}: {exc}") from exc
        position += section_length
    raise DownloadError(f"a GRIB2 message at offset {offset} in {source} has no product definition")


# -- Choosing the centre ---------------------------------------------------


def is_central(record: EnsembleRecord, hour: int, interval_start: int | None) -> bool:
    """Whether a record is the ensemble's centre of its quantity at ``hour``:
    the unweighted mean of a derived forecast, the median of a percentile
    forecast, and for a percentile over an interval only the one window
    that ends at ``hour`` and starts at ``interval_start``, the previous
    step of the published axis (the file also carries longer windows)."""
    if record.forecast_hours is None:
        return False
    if record.template == MEAN_TEMPLATE:
        return record.derived == UNWEIGHTED_MEAN and record.forecast_hours == hour
    if record.template == PERCENTILE_TEMPLATE:
        return record.percentile == MEDIAN and record.forecast_hours == hour
    if record.template == PERCENTILE_INTERVAL_TEMPLATE:
        return (
            record.percentile == MEDIAN
            and interval_start is not None
            and record.interval_hours == hour - interval_start
            and record.forecast_hours == interval_start
            and record.interval_end == record.reference_time + timedelta(hours=hour)
        )
    return False


def published_surface(record: EnsembleRecord) -> tuple[int, int | None, int | None]:
    """``(type, scale factor, scaled value)`` of the surface a record is
    published on: its own, or the alias :data:`SURFACE_ALIASES` names; the
    ground surface's value is zero, which these files leave missing."""
    level_type = SURFACE_ALIASES.get((record.triple, record.level_type, record.level_value or 0.0), record.level_type)
    if level_type != record.level_type:
        return level_type, 0, 0
    if level_type == GROUND_SURFACE and record.level_scaled_value is None:
        return level_type, 0, 0
    return level_type, record.level_scale, record.level_scaled_value


def published_identity(record: EnsembleRecord, hour: int) -> MessageInfo:
    """The identity a central record has once rewritten
    (:func:`rewrite_keys`), which the registry's matcher is asked about."""
    level_type, scale, scaled = published_surface(record)
    return MessageInfo(
        band=1,
        discipline=record.discipline,
        parameter_category=record.category,
        parameter_number=record.number,
        level_type=level_type,
        level_value=None if scaled is None or scale is None else scaled / (10.0**scale),
        reference_time=record.reference_time,
        valid_time=record.reference_time + timedelta(hours=hour),
        statistical_process=record.statistical if record.template == PERCENTILE_INTERVAL_TEMPLATE else None,
    )


def rewrite_keys(record: EnsembleRecord) -> dict[str, int]:
    """The ``grib_set`` keys that make a central record the plain one: the
    deterministic template (eccodes carries every key the two share across,
    a 4.10's interval and statistical process included), and the surface
    when :func:`published_surface` moves it."""
    keys = {"productDefinitionTemplateNumber": PLAIN_TEMPLATES[record.template]}
    level_type, scale, scaled = published_surface(record)
    if level_type != record.level_type:
        keys["typeOfFirstFixedSurface"] = level_type
    if (scale, scaled) != (record.level_scale, record.level_scaled_value):
        keys["scaleFactorOfFirstFixedSurface"] = int(scale or 0)
        keys["scaledValueOfFirstFixedSurface"] = int(scaled or 0)
    return keys


def walk(
    url: str,
    size: int | None,
    hour: int,
    interval_start: int | None,
    variable_ids: tuple[str, ...],
    read_range: Callable[[str, ByteRange], bytes],
) -> dict[str, EnsembleRecord]:
    """The central record of each of ``variable_ids`` in one file, found by
    reading the message heads in turn and stopping once every one is
    found. Exactly one central record must match each variable. ``size``
    is None for a file no listing measured (a :class:`~xuebuild.wis2.NamedCache`'s):
    a read past its end fails like any other, and a body that is not a
    GRIB2 message (a cache's "not found" page served as 200) has no
    ``GRIB`` magic."""
    found: dict[str, EnsembleRecord] = {}
    end = size if size is not None else 1 << 62
    offset = 0
    while offset < end and len(found) < len(variable_ids):
        length = min(HEAD_BYTES, end - offset)
        while True:
            head = read_range(url, ByteRange(offset, offset + length - 1))
            try:
                total, record = parse_head(head, offset, url)
                break
            except HeadTruncated:
                if length >= min(HEAD_LIMIT, end - offset):
                    raise DownloadError(f"{url}: no GRIB2 product definition in the {length} bytes at offset {offset}") from None
                length = min(2 * length, HEAD_LIMIT, end - offset)
        if total < 20 or offset + total > end:
            raise DownloadError(f"{url}: a GRIB2 message length is out of bounds at offset {offset}")
        if is_central(record, hour, interval_start):
            identity = published_identity(record, hour)
            for variable_id in variable_ids:
                if matches_variable(VARIABLES[variable_id], identity):
                    if variable_id in found:
                        raise DownloadError(f"{url}: more than one central {variable_id} record")
                    found[variable_id] = record
        offset += total
    missing = [variable_id for variable_id in variable_ids if variable_id not in found]
    if missing:
        raise DownloadError(f"{url} carries no central {', '.join(missing)} record at +{hour} h")
    return found


def read_record(url: str, record: EnsembleRecord, read_range: Callable[[str, ByteRange], bytes]) -> bytes:
    data = read_range(url, ByteRange(record.offset, record.offset + record.length - 1))
    if data[:4] != b"GRIB" or data[-4:] != b"7777":
        raise DownloadError(f"{url}: the record at offset {record.offset} is not one whole GRIB2 message")
    return data


def frame_records(
    spec: SourceSpec,
    run: GfsRun,
    hour: int,
    variable_ids: tuple[str, ...],
    candidates: list[CacheObject],
    read_range: Callable[[str, ByteRange], bytes],
    rewrite: Callable[[Path, Path, dict[str, int]], None],
) -> bytes:
    """One frame's records in the order of ``variable_ids``, each rewritten
    to its plain template, from the first cache that serves them whole."""
    axis = spec.forecast_hours(hour, cycle=run.time.hour)
    interval_start = axis[-2] if len(axis) >= 2 else None
    errors: list[str] = []
    for candidate in candidates:
        try:
            records = walk(candidate.url, candidate.size, hour, interval_start, variable_ids, read_range)
            for record in records.values():
                if record.reference_time != run.time:
                    raise DownloadError(f"{candidate.url} is of the run {record.reference_time:%Y%m%d%H}, not {run.id}")
            payload = []
            with tempfile.TemporaryDirectory(prefix="xue-cmageps-") as scratch:
                for variable_id in variable_ids:
                    record = records[variable_id]
                    source = Path(scratch) / f"{variable_id}.grib2"
                    destination = Path(scratch) / f"{variable_id}.plain.grib2"
                    source.write_bytes(read_record(candidate.url, record, read_range))
                    try:
                        rewrite(source, destination, rewrite_keys(record))
                    except Exception as exc:
                        raise DownloadError(f"could not rewrite the {variable_id} record of {candidate.url}: {exc}") from exc
                    payload.append(destination.read_bytes())
            return b"".join(payload)
        except DownloadError as exc:
            LOG.warning("%s +%d h from the %s cache failed (%s)", spec.manifest_model, hour, candidate.cache, exc)
            errors.append(f"{candidate.cache}: {exc}")
    raise DownloadError(
        f"{spec.manifest_model} +{hour} h could not be read from any cache ({'; '.join(errors) or 'none lists it'})"
    )

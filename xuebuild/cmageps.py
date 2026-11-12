"""CMA GRAPES GEPS, the China Meteorological Administration's global
ensemble, as WMO WIS2 exchanges it: the ensemble products the source reads,
cut out of its product files into the one-GRIB-per-hour frames both
encoders read (``docs/contribution/sources.md``, "cmageps").

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

Which record each input is, is the registry's: every variable the source
reads names its product in a ``template-ensemble`` rule (the template's
shape, the parameter on its surface, the statistic, the interval), and a
head is matched against those rules by the GRIB2 header index's own test
(:func:`xuebuild.grib2.matches_variable`), the same the converter later
locates the band with. The records are written as CMA wrote them, the
frame then repacked from JPEG2000 to ``grid_simple`` like any frame whose
packing GDAL may not read (:attr:`~xuebuild.sources.SourceSpec.packing`).
"""

from __future__ import annotations

import logging
import os
import re
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import wis2
from .errors import ConversionError, DownloadError
from .grib2 import MessageInfo, UnsupportedTemplate, matches_variable, product_identity
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
class ProductRecord:
    """One message of a product file, as far as its head says: where it is,
    and its identity as the header index reads it (None for a template the
    index does not read, which no input is)."""

    offset: int
    length: int
    identity: MessageInfo | None


def _time(body: bytes, offset: int) -> datetime:
    year, month, day, hour, minute, second = struct.unpack_from(">HBBBBB", body, offset)
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


def parse_head(head: bytes, offset: int, source: str) -> ProductRecord:
    """The extent and identity of the message whose first bytes are
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
                try:
                    identity: MessageInfo | None = product_identity(body, discipline, reference_time, source)
                except UnsupportedTemplate:
                    identity = None
                return ProductRecord(offset, total, identity)
        except (ValueError, struct.error, ConversionError) as exc:
            raise DownloadError(f"unreadable GRIB2 message head at offset {offset} in {source}: {exc}") from exc
        position += section_length
    raise DownloadError(f"a GRIB2 message at offset {offset} in {source} has no product definition")


# -- Choosing the records ----------------------------------------------------


def is_record_of(variable_id: str, record: ProductRecord, hour: int) -> bool:
    """Whether a record is ``variable_id``'s at ``hour``: matched by the
    variable's rules, and valid at the frame — a percentile's or a
    probability's interval ending there, an instantaneous field forecast
    for it."""
    identity = record.identity
    if identity is None or identity.valid_time != identity.reference_time + timedelta(hours=hour):
        return False
    return matches_variable(VARIABLES[variable_id], identity)


def walk(
    url: str,
    size: int | None,
    hour: int,
    variable_ids: tuple[str, ...],
    read_range: Callable[[str, ByteRange], bytes],
) -> dict[str, ProductRecord]:
    """The record of each of ``variable_ids`` in one file, found by reading
    the message heads in turn and stopping once every one is found.
    Exactly one record may match each variable. ``size`` is None for a file
    no listing measured (a :class:`~xuebuild.wis2.NamedCache`'s): a read
    past its end fails like any other, and a body that is not a GRIB2
    message (a cache's "not found" page served as 200) has no ``GRIB``
    magic."""
    found: dict[str, ProductRecord] = {}
    end = size if size is not None else 1 << 62
    offset = 0
    while offset < end and len(found) < len(variable_ids):
        length = min(HEAD_BYTES, end - offset)
        while True:
            head = read_range(url, ByteRange(offset, offset + length - 1))
            try:
                record = parse_head(head, offset, url)
                break
            except HeadTruncated:
                if length >= min(HEAD_LIMIT, end - offset):
                    raise DownloadError(f"{url}: no GRIB2 product definition in the {length} bytes at offset {offset}") from None
                length = min(2 * length, HEAD_LIMIT, end - offset)
        if record.length < 20 or offset + record.length > end:
            raise DownloadError(f"{url}: a GRIB2 message length is out of bounds at offset {offset}")
        for variable_id in variable_ids:
            if is_record_of(variable_id, record, hour):
                if variable_id in found:
                    raise DownloadError(f"{url}: more than one {variable_id} record")
                found[variable_id] = record
        offset += record.length
    missing = [variable_id for variable_id in variable_ids if variable_id not in found]
    if missing:
        raise DownloadError(f"{url} carries no {', '.join(missing)} record at +{hour} h")
    return found


def read_record(url: str, record: ProductRecord, read_range: Callable[[str, ByteRange], bytes]) -> bytes:
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
) -> bytes:
    """One frame's records in the order of ``variable_ids``, as CMA wrote
    them, from the first cache that serves them whole."""
    errors: list[str] = []
    for candidate in candidates:
        try:
            records = walk(candidate.url, candidate.size, hour, variable_ids, read_range)
            for record in records.values():
                assert record.identity is not None
                if record.identity.reference_time != run.time:
                    raise DownloadError(
                        f"{candidate.url} is of the run {record.identity.reference_time:%Y%m%d%H}, not {run.id}"
                    )
            return b"".join(read_record(candidate.url, records[variable_id], read_range) for variable_id in variable_ids)
        except DownloadError as exc:
            LOG.warning("%s +%d h from the %s cache failed (%s)", spec.manifest_model, hour, candidate.cache, exc)
            errors.append(f"{candidate.cache}: {exc}")
    raise DownloadError(
        f"{spec.manifest_model} +{hour} h could not be read from any cache ({'; '.join(errors) or 'none lists it'})"
    )

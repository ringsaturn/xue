"""The CMA-CW/DUST sand and dust forecast as WMO WIS2 exchanges it, fetched
into the series files both encoders read (``docs/contribution/sources.md``,
"cmadust").

The China Meteorological Administration publishes the forecast of the WMO
SDS-WAS Asian Center as one NetCDF per lead time, hourly to +72 h and
three-hourly to +168 h from the 00Z and 12Z cycles: ``Z_NAFP_C_BABJ_<run>
0000_P_CEMC-CMA-CW-DUST_15KM_ASIA-<HHH>00.nc``, about 112 MB each, almost
all of it the four pressure-level fields. Two WIS2 global caches carry the
files, the Met Office/NOAA one and JMA's; neither has held every file of a
run on its own, but their union has, so a lead time is read from the first
cache that lists it.

A file is NetCDF3 *classic*, uncompressed: the header names every
variable's byte offset, so a frame is one Range read of the header (which
the coordinates follow) and one of each two-dimensional field wanted, a
megabyte apiece. The header is parsed here with :mod:`struct`, and each
variable's run is written as one classic NetCDF series of its own
(``cmadust.<run>.<id>.nc``, the ``<stem>.<variable>.nc`` layout
:func:`xuebuild.observation.series_files` resolves), so the fetch needs
neither netCDF4 nor GDAL and the converters read it like any series.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import struct
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import numpy as np

from .common import S3_NAMESPACE, s3_list_pages, s3_list_query
from .errors import DownloadError
from .idx import ByteRange
from .model import GfsRun
from .sources import SourceSpec

LOG = logging.getLogger(__name__)

MODEL = "cmadust"

#: The two WIS2 global caches, in the order a lead time is read from them.
#: The Met Office/NOAA cache keys a file by its name under a fixed prefix;
#: JMA's puts a directory named for the file and an upload stamp between
#: the prefix and the name, so a key is only ever learnt from a listing.
NOAA_CACHE_URL = os.environ.get("XUE_CMADUST_NOAA_CACHE_URL", "https://wis2globalcache.s3.amazonaws.com").rstrip("/")
NOAA_CACHE_PREFIX = "data/cn-cma/data/core/weather/prediction/forecast/medium-range/deterministic/limited-area/"
JMA_CACHE_URL = os.environ.get(
    "XUE_CMADUST_JMA_CACHE_URL", "https://global-cache-of-japan.s3.ap-northeast-1.amazonaws.com"
).rstrip("/")
JMA_CACHE_PREFIX = "wis2/data/cn-cma:data.core.weather.prediction.forecast.medium-range.deterministic.limited-area/"


@dataclass(frozen=True)
class Cache:
    name: str
    base_url: str
    prefix: str


CACHES = (Cache("noaa", NOAA_CACHE_URL, NOAA_CACHE_PREFIX), Cache("jma", JMA_CACHE_URL, JMA_CACHE_PREFIX))

#: The file's name for each published variable, and the unit it is in.
FILE_VARIABLES = {"aoddust": "aod550_dust", "dustsfc": "sconc_dust"}
FILE_UNITS = {"aoddust": "-", "dustsfc": "ug/m3"}
#: The unit each series is written in: the registry's for the optical depth,
#: the file's own spelling of µg/m³ for the concentration, which the
#: particulate rule reads as is and which keeps the header ASCII.
SERIES_UNITS = {"aoddust": "1", "dustsfc": "ug/m3"}
LONG_NAMES = {"aoddust": "Dust optical depth at 550 nm", "dustsfc": "Surface dust concentration"}

#: The model's regular grid: 835 x 301 cells of 0.15°, centres from 25°E
#: and from 15°N, latitudes ascending in the file.
GRID_WEST = 25.0
GRID_SOUTH = 15.0
GRID_STEP = 0.15
GRID_WIDTH = 835
GRID_HEIGHT = 301
#: A coordinate further than this from its cell centre is another grid.
COORDINATE_TOLERANCE = 1e-4

#: The first read of a file: the header and, right behind it, the
#: coordinates. A header longer than this is read again at the length the
#: parse asks for, up to the cap.
HEAD_BYTES = 16384
HEAD_LIMIT = 1 << 20

FETCH_FILENAME = "fetch.json"

_NAME_RE = re.compile(r"^Z_NAFP_C_BABJ_(\d{10})0000_P_CEMC-CMA-CW-DUST_15KM_ASIA-(\d{5})\.nc$")
_LISTING = "WIS2 cache listing"


def file_name(run: GfsRun, hour: int) -> str:
    """The name of one lead time's file. The last five digits are the lead
    in hundredths of an hour: ``02400`` is +24 h."""
    return f"Z_NAFP_C_BABJ_{run.time:%Y%m%d%H}0000_P_CEMC-CMA-CW-DUST_15KM_ASIA-{hour * 100:05d}.nc"


def parse_file_name(name: str) -> tuple[datetime, int] | None:
    """``(cycle, lead hour)`` of a file name, or None for anything else — a
    partial upload's numeric suffix, a ``.bin``, a lead off the whole hour."""
    match = _NAME_RE.match(name)
    if not match:
        return None
    lead = int(match.group(2))
    if lead % 100:
        return None
    try:
        cycle = datetime.strptime(match.group(1), "%Y%m%d%H").replace(tzinfo=UTC)
    except ValueError:
        return None
    return cycle, lead // 100


def run_prefix(cache: Cache, run: GfsRun) -> str:
    """The listing prefix of every file of one run on one cache."""
    return f"{cache.prefix}Z_NAFP_C_BABJ_{run.time:%Y%m%d%H}0000"


def object_url(cache: Cache, key: str) -> str:
    return f"{cache.base_url}/{urllib.parse.quote(key, safe='/')}"


@dataclass(frozen=True)
class CacheObject:
    cache: str
    url: str
    size: int


def parse_listing(root: ElementTree.Element, cache: Cache, run: GfsRun) -> dict[int, list[CacheObject]]:
    """The run's files on one listing page, by lead hour. A key counts when
    its last segment is a file of this run; on the JMA cache two uploads of
    one file may both be listed, the newer (the larger stamp) first."""
    found: dict[int, list[tuple[str, CacheObject]]] = {}
    for contents in root.iter(f"{S3_NAMESPACE}Contents"):
        key = contents.findtext(f"{S3_NAMESPACE}Key") or ""
        parsed = parse_file_name(key.rsplit("/", 1)[-1])
        if parsed is None or parsed[0] != run.time:
            continue
        try:
            size = int(contents.findtext(f"{S3_NAMESPACE}Size") or "")
        except ValueError:
            continue
        found.setdefault(parsed[1], []).append((key, CacheObject(cache.name, object_url(cache, key), size)))
    return {hour: [item for _, item in sorted(items, key=lambda pair: pair[0], reverse=True)] for hour, items in found.items()}


def list_run(run: GfsRun, *, fetch: Callable[[str], str]) -> dict[int, list[CacheObject]]:
    """Every file of one run on both caches, by lead hour, in the order to
    try them. A cache that cannot be listed is an error, not an empty
    cache: a run must never read as incomplete because a listing failed."""
    objects: dict[int, list[CacheObject]] = {}
    for cache in CACHES:
        prefix = run_prefix(cache, run)
        for root in s3_list_pages(lambda token: fetch(f"{cache.base_url}/?{s3_list_query(prefix, token)}"), _LISTING):
            for hour, items in parse_listing(root, cache, run).items():
                objects.setdefault(hour, []).extend(items)
    return objects


def missing_hours(spec: SourceSpec, hours: int, objects: dict[int, list[CacheObject]]) -> list[int]:
    return [hour for hour in spec.forecast_hours(hours) if not objects.get(hour)]


def run_is_complete(spec: SourceSpec, run: GfsRun, hours: int, *, fetch: Callable[[str], str]) -> bool:
    """Whether the caches together list every lead time through ``hours``."""
    return not missing_hours(spec, hours, list_run(run, fetch=fetch))


# -- NetCDF classic --------------------------------------------------------

NC_DIMENSION = 0x0A
NC_VARIABLE = 0x0B
NC_ATTRIBUTE = 0x0C
#: nc_type -> (struct code, size)
NC_TYPES = {1: ("b", 1), 2: ("c", 1), 3: ("h", 2), 4: ("i", 4), 5: ("f", 4), 6: ("d", 8)}
NC_CHAR = 2
NC_FLOAT = 5
NC_DOUBLE = 6
STREAMING = 0xFFFFFFFF


class HeaderTruncated(Exception):
    """The bytes read so far end inside the header; ``needed`` is a length
    that reaches past the point the parse stopped at."""

    def __init__(self, needed: int) -> None:
        super().__init__(needed)
        self.needed = needed


@dataclass(frozen=True)
class NetcdfVariable:
    name: str
    dimensions: tuple[str, ...]
    shape: tuple[int, ...]
    nc_type: int
    attributes: dict[str, Any]
    vsize: int
    begin: int
    record: bool

    @property
    def nbytes(self) -> int:
        """The variable's unpadded size, for a fixed-size variable."""
        return math.prod(self.shape) * NC_TYPES[self.nc_type][1]


@dataclass(frozen=True)
class NetcdfHeader:
    version: int
    numrecs: int
    dimensions: dict[str, int]
    attributes: dict[str, Any]
    variables: dict[str, NetcdfVariable]
    length: int

    @property
    def data_end(self) -> int:
        """The file's length as its header describes it: the end of the last
        fixed-size variable, or of the last record."""
        fixed = [variable.begin + variable.vsize for variable in self.variables.values() if not variable.record]
        records = [variable for variable in self.variables.values() if variable.record]
        ends = fixed or [self.length]
        if records:
            record_size = sum(variable.vsize for variable in records)
            ends.append(min(variable.begin for variable in records) + self.numrecs * record_size)
        return max(ends)


class _Reader:
    def __init__(self, buffer: bytes) -> None:
        self.buffer = buffer
        self.offset = 0

    def take(self, count: int) -> bytes:
        end = self.offset + count
        if end > len(self.buffer):
            raise HeaderTruncated(max(end, 2 * len(self.buffer)))
        chunk = self.buffer[self.offset : end]
        self.offset = end
        return chunk

    def uint32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def uint64(self) -> int:
        return struct.unpack(">Q", self.take(8))[0]

    def name(self) -> str:
        length = self.uint32()
        text = self.take(length)
        self.take(-length % 4)
        try:
            return text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DownloadError(f"a NetCDF name is not UTF-8: {text!r}") from exc

    def values(self, nc_type: int, count: int) -> Any:
        if nc_type not in NC_TYPES:
            raise DownloadError(f"unknown NetCDF type {nc_type}")
        code, size = NC_TYPES[nc_type]
        raw = self.take(count * size)
        self.take(-(count * size) % 4)
        if nc_type == NC_CHAR:
            return raw.rstrip(b"\0").decode("utf-8", errors="replace")
        return struct.unpack(f">{count}{code}", raw)

    def attributes(self) -> dict[str, Any]:
        tag, count = self.uint32(), self.uint32()
        if tag == 0 and count == 0:
            return {}
        if tag != NC_ATTRIBUTE:
            raise DownloadError(f"expected a NetCDF attribute list, found tag {tag:#x}")
        attributes: dict[str, Any] = {}
        for _ in range(count):
            name = self.name()
            nc_type, length = self.uint32(), self.uint32()
            attributes[name] = self.values(nc_type, length)
        return attributes


def parse_header(buffer: bytes) -> NetcdfHeader:
    """The header of a NetCDF classic (``CDF\\x01``) or 64-bit offset
    (``CDF\\x02``) file from its first bytes. Raises :class:`HeaderTruncated`
    when ``buffer`` ends inside it, and :class:`DownloadError` for anything
    that is not such a header."""
    reader = _Reader(buffer)
    magic = reader.take(4)
    if magic[:3] != b"CDF" or magic[3] not in (1, 2):
        raise DownloadError(f"not a NetCDF classic or 64-bit offset file (magic {magic!r})")
    version = magic[3]
    numrecs = reader.uint32()
    tag, count = reader.uint32(), reader.uint32()
    dimension_names: list[str] = []
    dimensions: dict[str, int] = {}
    if not (tag == 0 and count == 0):
        if tag != NC_DIMENSION:
            raise DownloadError(f"expected the NetCDF dimension list, found tag {tag:#x}")
        for _ in range(count):
            name = reader.name()
            dimension_names.append(name)
            dimensions[name] = reader.uint32()
    attributes = reader.attributes()
    tag, count = reader.uint32(), reader.uint32()
    variables: dict[str, NetcdfVariable] = {}
    if not (tag == 0 and count == 0):
        if tag != NC_VARIABLE:
            raise DownloadError(f"expected the NetCDF variable list, found tag {tag:#x}")
        for _ in range(count):
            name = reader.name()
            ids = [reader.uint32() for _ in range(reader.uint32())]
            if any(index >= len(dimension_names) for index in ids):
                raise DownloadError(f"NetCDF variable {name} names a dimension the file does not declare")
            names = tuple(dimension_names[index] for index in ids)
            variable_attributes = reader.attributes()
            nc_type = reader.uint32()
            if nc_type not in NC_TYPES:
                raise DownloadError(f"NetCDF variable {name} has unknown type {nc_type}")
            vsize = reader.uint32()
            begin = reader.uint32() if version == 1 else reader.uint64()
            shape = tuple(dimensions[dimension] for dimension in names)
            variables[name] = NetcdfVariable(
                name=name,
                dimensions=names,
                shape=shape,
                nc_type=nc_type,
                attributes=variable_attributes,
                vsize=vsize,
                begin=begin,
                record=bool(names) and dimensions[names[0]] == 0,
            )
    return NetcdfHeader(
        version=version,
        numrecs=numrecs,
        dimensions=dimensions,
        attributes=attributes,
        variables=variables,
        length=reader.offset,
    )


def read_header(url: str, read_range: Callable[[str, ByteRange], bytes], size: int) -> tuple[NetcdfHeader, bytes]:
    """The header of a remote file and the bytes read for it, which run on
    past the header (into the coordinates, on this product)."""
    length = min(HEAD_BYTES, size)
    while True:
        buffer = read_range(url, ByteRange(0, length - 1))
        try:
            return parse_header(buffer), buffer
        except HeaderTruncated as exc:
            if length >= min(HEAD_LIMIT, size):
                raise DownloadError(f"{url}: no NetCDF header in its first {length} bytes") from None
            length = min(exc.needed, HEAD_LIMIT, size)


def read_variable(
    url: str, header: NetcdfHeader, name: str, read_range: Callable[[str, ByteRange], bytes], head: bytes = b""
) -> np.ndarray:
    """One fixed-size float or double variable, read from the bytes already
    at hand when they cover it, else by one Range request."""
    variable = header.variables.get(name)
    if variable is None:
        raise DownloadError(f"{url} carries no {name}")
    if variable.record:
        raise DownloadError(f"{url}: {name} is a record variable, which this reader does not take")
    if variable.nc_type not in (NC_FLOAT, NC_DOUBLE):
        raise DownloadError(f"{url}: {name} is not a float variable")
    start, end = variable.begin, variable.begin + variable.nbytes
    raw = head[start:end] if end <= len(head) else read_range(url, ByteRange(start, end - 1))
    dtype = ">f4" if variable.nc_type == NC_FLOAT else ">f8"
    return np.frombuffer(raw, dtype=dtype).reshape(variable.shape)


# -- One frame -------------------------------------------------------------


def grid_coordinates() -> tuple[np.ndarray, np.ndarray]:
    """The cell centres as the series carry them: latitudes ascending, the
    file's own order, at their round values."""
    latitudes = np.round(GRID_SOUTH + GRID_STEP * np.arange(GRID_HEIGHT), 6)
    longitudes = np.round(GRID_WEST + GRID_STEP * np.arange(GRID_WIDTH), 6)
    return latitudes, longitudes


def check_frame(url: str, header: NetcdfHeader, head: bytes, variable_ids: tuple[str, ...], read_range: Callable[[str, ByteRange], bytes]) -> None:
    """Hold one file to the product: the grid, the coordinates and each
    wanted field's layout and unit. A file that has drifted from it is
    refused rather than read on another grid."""
    if header.dimensions.get("lon") != GRID_WIDTH or header.dimensions.get("lat") != GRID_HEIGHT:
        raise DownloadError(
            f"{url} is {header.dimensions.get('lon')} x {header.dimensions.get('lat')} cells, "
            f"not {GRID_WIDTH} x {GRID_HEIGHT}"
        )
    latitudes, longitudes = grid_coordinates()
    for name, expected in (("lat", latitudes), ("lon", longitudes)):
        actual = read_variable(url, header, name, read_range, head).astype(np.float64)
        if actual.shape != expected.shape or not np.allclose(actual, expected, rtol=0, atol=COORDINATE_TOLERANCE):
            raise DownloadError(f"{url}: {name} is not the product's 0.15° grid from {expected[0]:g}")
    for variable_id in variable_ids:
        name = FILE_VARIABLES[variable_id]
        variable = header.variables.get(name)
        if variable is None:
            raise DownloadError(f"{url} carries no {name}")
        if variable.dimensions != ("lat", "lon") or variable.nc_type != NC_FLOAT:
            raise DownloadError(f"{url}: {name} is not a float (lat, lon) field")
        unit = str(variable.attributes.get("units", "")).strip()
        if unit != FILE_UNITS[variable_id]:
            raise DownloadError(f"{url}: {name} is in {unit or '<no unit>'}, not {FILE_UNITS[variable_id]}")


def _frame_planes(
    candidate: CacheObject, variable_ids: tuple[str, ...], read_range: Callable[[str, ByteRange], bytes]
) -> dict[str, np.ndarray]:
    header, head = read_header(candidate.url, read_range, candidate.size)
    if header.data_end != candidate.size:
        # The listing's size is the stored object's; a file whose header
        # describes another length is a cut-off upload.
        raise DownloadError(f"{candidate.url} is {candidate.size} bytes, its header describes {header.data_end}")
    check_frame(candidate.url, header, head, variable_ids, read_range)
    planes: dict[str, np.ndarray] = {}
    for variable_id in variable_ids:
        name = FILE_VARIABLES[variable_id]
        plane = read_variable(candidate.url, header, name, read_range).astype(np.float32)
        fill = _declared_fill(header.variables[name])
        if fill is not None:
            plane = np.where(plane == fill, np.float32(np.nan), plane)
        planes[variable_id] = plane
    return planes


def _declared_fill(variable: NetcdfVariable) -> np.float32 | None:
    for name in ("_FillValue", "missing_value"):
        value = variable.attributes.get(name)
        if isinstance(value, tuple) and value:
            return np.float32(value[0])
    return None


def fetch_frame(
    hour: int, candidates: list[CacheObject], variable_ids: tuple[str, ...], read_range: Callable[[str, ByteRange], bytes]
) -> tuple[str, dict[str, np.ndarray]]:
    """One lead time's planes from the first cache that serves them whole,
    and the cache's name."""
    errors: list[str] = []
    for candidate in candidates:
        try:
            return candidate.cache, _frame_planes(candidate, variable_ids, read_range)
        except DownloadError as exc:
            LOG.warning("CMA-CW-DUST +%d h from the %s cache failed (%s)", hour, candidate.cache, exc)
            errors.append(f"{candidate.cache}: {exc}")
    raise DownloadError(f"CMA-CW-DUST +{hour} h could not be read from any cache ({'; '.join(errors) or 'none lists it'})")


# -- The series ------------------------------------------------------------


@dataclass
class _HeaderWriter:
    chunks: list[bytes] = field(default_factory=list)

    def uint32(self, value: int) -> None:
        self.chunks.append(struct.pack(">I", value))

    def name(self, text: str) -> None:
        raw = text.encode("utf-8")
        self.uint32(len(raw))
        self.chunks.append(raw + b"\0" * (-len(raw) % 4))

    def attribute(self, name: str, value: str | float) -> None:
        self.name(name)
        if isinstance(value, str):
            raw = value.encode("utf-8")
            self.uint32(NC_CHAR)
            self.uint32(len(raw))
            self.chunks.append(raw + b"\0" * (-len(raw) % 4))
        else:
            self.uint32(NC_FLOAT)
            self.uint32(1)
            self.chunks.append(struct.pack(">f", value))

    def attributes(self, attributes: dict[str, str | float]) -> None:
        if not attributes:
            self.uint32(0)
            self.uint32(0)
            return
        self.uint32(NC_ATTRIBUTE)
        self.uint32(len(attributes))
        for name, value in attributes.items():
            self.attribute(name, value)

    def bytes(self) -> bytes:
        return b"".join(self.chunks)


def write_series(
    path: Path,
    variable_id: str,
    run: datetime,
    hours: list[int],
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    frames: np.ndarray,
    attributes: dict[str, str],
) -> None:
    """One variable's CF series as a NetCDF classic file: ``time`` in hours
    from the run (whose epoch names the cycle), the run as the global
    ``forecast_reference_time``, latitudes ascending, float32 with a NaN
    fill, the variable named by its Xue id. Written through ``.part`` and
    renamed, so a series that exists is whole."""
    count, height, width = frames.shape
    if (count, height, width) != (len(hours), latitudes.size, longitudes.size):
        raise ValueError("frames do not match the axis and the grid")
    epoch = run.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    dimensions = (("time", count), ("latitude", height), ("longitude", width))
    variables: list[tuple[str, tuple[int, ...], int, dict[str, str | float], bytes]] = [
        (
            "time",
            (0,),
            NC_DOUBLE,
            {"standard_name": "time", "long_name": "valid time", "units": f"hours since {epoch}", "calendar": "standard", "axis": "T"},
            np.asarray(hours, dtype=">f8").tobytes(),
        ),
        (
            "latitude",
            (1,),
            NC_DOUBLE,
            {"standard_name": "latitude", "long_name": "latitude", "units": "degrees_north", "axis": "Y"},
            np.asarray(latitudes, dtype=">f8").tobytes(),
        ),
        (
            "longitude",
            (2,),
            NC_DOUBLE,
            {"standard_name": "longitude", "long_name": "longitude", "units": "degrees_east", "axis": "X"},
            np.asarray(longitudes, dtype=">f8").tobytes(),
        ),
        (
            variable_id,
            (0, 1, 2),
            NC_FLOAT,
            {"long_name": LONG_NAMES[variable_id], "units": SERIES_UNITS[variable_id], "_FillValue": float("nan")},
            b"",
        ),
    ]
    sizes = [len(data) for *_, data in variables[:-1]] + [count * height * width * 4]

    def header(begins: list[int]) -> bytes:
        writer = _HeaderWriter()
        writer.chunks.append(b"CDF\x01")
        writer.uint32(0)
        writer.uint32(NC_DIMENSION)
        writer.uint32(len(dimensions))
        for name, length in dimensions:
            writer.name(name)
            writer.uint32(length)
        writer.attributes(dict(attributes))
        writer.uint32(NC_VARIABLE)
        writer.uint32(len(variables))
        for (name, ids, nc_type, variable_attributes, _), size, begin in zip(variables, sizes, begins):
            writer.name(name)
            writer.uint32(len(ids))
            for index in ids:
                writer.uint32(index)
            writer.attributes(variable_attributes)
            writer.uint32(nc_type)
            writer.uint32(size + (-size % 4))
            writer.uint32(begin)
        return writer.bytes()

    # The offsets are fields of the header, so its length is known once it
    # is written with any; every size is a multiple of four, so no padding
    # sits between the variables.
    length = len(header([0] * len(variables)))
    begins = [length + sum(sizes[:index]) for index in range(len(variables))]
    if begins[-1] + sizes[-1] >= 1 << 31:
        raise DownloadError(f"{path.name} would pass the classic format's 2 GiB offset limit")
    temporary = path.with_suffix(path.suffix + ".part")
    with temporary.open("wb") as handle:
        handle.write(header(begins))
        for *_, data in variables[:-1]:
            handle.write(data)
        for frame in frames:
            handle.write(np.asarray(frame, dtype=">f4").tobytes())
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def series_name(run: GfsRun, variable_id: str) -> str:
    return f"{MODEL}.{run.id}.{variable_id}.nc"


def series_attributes(run: GfsRun, command: str) -> dict[str, str]:
    return {
        "Conventions": "CF-1.10",
        "title": "CMA-CW v1.0/DUST sand and dust storm forecast",
        "institution": "China Meteorological Administration, WMO SDS-WAS Asian Center",
        "source": "CMA-CW v1.0/DUST via WMO WIS2 global caches, 0.15 degree regular grid",
        "license": "WMO Unified Data Policy (Resolution 1 (Cg-Ext(2021))), core data: free and unrestricted",
        "history": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {command}",
        "forecast_reference_time": run.time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def fetch_run(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool,
    input_ids: tuple[str, ...] | None,
    fetch_text: Callable[[str], str],
    read_range: Callable[[str, ByteRange], bytes],
) -> list[Path]:
    """Fetch one run into ``raw_root/cmadust.<run>/``: one series per
    variable and a ``fetch.json`` saying which cache served each lead time.
    A series already on disk is kept unless ``force``. A lead time neither
    cache lists makes the run incomplete, never a gap in the axis."""
    if input_ids is not None and any(variable_id not in spec.input_variable_ids for variable_id in input_ids):
        raise DownloadError(f"{spec.manifest_model} publishes {list(spec.input_variable_ids)}, not {list(input_ids)}")
    variable_ids = tuple(v for v in spec.input_variable_ids if input_ids is None or v in input_ids)
    if not variable_ids:
        raise DownloadError(f"a {spec.manifest_model} fetch needs at least one variable")
    destination = raw_root / f"{spec.id}.{run.id}"
    destination.mkdir(parents=True, exist_ok=True)
    paths = {variable_id: destination / series_name(run, variable_id) for variable_id in variable_ids}
    wanted = tuple(variable_id for variable_id in variable_ids if force or not paths[variable_id].is_file())
    for variable_id in variable_ids:
        if variable_id not in wanted:
            LOG.info("reusing %s", paths[variable_id])
    axis = spec.forecast_hours(hours)
    caches: dict[int, str] = {}
    if wanted:
        objects = list_run(run, fetch=fetch_text)
        missing = missing_hours(spec, hours, objects)
        if missing:
            raise DownloadError(
                f"{spec.manifest_model} run {run.id} is incomplete: neither cache lists "
                f"+{', +'.join(map(str, missing[:8]))}{' ...' if len(missing) > 8 else ''} h"
            )
        frames = {variable_id: np.empty((len(axis), GRID_HEIGHT, GRID_WIDTH), dtype=np.float32) for variable_id in wanted}

        def one(index: int) -> None:
            hour = axis[index]
            cache, planes = fetch_frame(hour, objects[hour], wanted, read_range)
            for variable_id, plane in planes.items():
                frames[variable_id][index] = plane
            caches[hour] = cache

        with ThreadPoolExecutor(max_workers=max(1, spec.fetch_concurrency)) as executor:
            list(executor.map(one, range(len(axis))))
        latitudes, longitudes = grid_coordinates()
        attributes = series_attributes(run, f"xue fetch --model {spec.id} --run {run.id} --hours {hours}")
        for variable_id in wanted:
            write_series(paths[variable_id], variable_id, run.time, axis, latitudes, longitudes, frames[variable_id], attributes)
            LOG.info("wrote %s (%d frames)", paths[variable_id], len(axis))
    record = {
        "model": spec.id,
        "run": run.id,
        "hours": hours,
        "frames": len(axis),
        "caches": {cache.name: sum(1 for name in caches.values() if name == cache.name) for cache in CACHES},
        "variables": [
            {"variable": variable_id, "file": FILE_VARIABLES[variable_id], "series": paths[variable_id].name, "fetched": variable_id in wanted}
            for variable_id in variable_ids
        ],
    }
    (destination / FETCH_FILENAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    LOG.info("%s run %s: %d series in %s", spec.manifest_model, run.id, len(paths), destination)
    return list(paths.values())

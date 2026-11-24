"""The column store (docs/zarr-profile.md, "Column store"): a run's isobaric
families and surface fields cut so that one cell's column over the whole axis
is one chunk per quantity, the Zarr profile's fourth branch.

The reference writer, held byte for byte by the wheel's ``write_columns``:
:func:`build_plan` says which arrays a source's run has, from the registry and
the run's manifest, and :func:`write_column_store` cuts them, from that plan
and the finished map stores alone. Both writers are handed one plan. Attaching
the store to a run is ``columnbuild.py``.

The work is one shard of one array at a time, in a process each: a band of 96
rows reads only the map tiles it overlaps, by seeking in the map's shard, so a
run whose planes do not fit in memory is cut on a small machine.

The shard, its index and the codecs are the grid stores' own
(:mod:`xuebuild.zarrstore`). The reader here is NumPy-only, as ``zarrstore``'s
is, and is what the build checks itself with.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import zlib
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO

import numpy as np

from . import binformat, zarrstore, zstdcli
from .columnarrays import ISOBARIC_SURFACE, PASCALS_PER_HPA
from .common import crc32_hex
from .errors import BundleError, XueError
from .sources import SourceSpec
from .variables import variable_spec

COLUMNS_VERSION = 1
"""The ``xue_columns`` block's ``version``."""

PLAN_VERSION = 1
"""The ``columnsPlan`` of the plan document both writers take."""

PREDICTORS = ("previous", "raw")

STORE_NAME = "columns.zarr"
BLOCK = (8, 8)
"""The block of cells an inner chunk holds, ``(height, width)``. Measured on a
GFS run: 4 x 4 and 16 x 16 make the store 8 % and 3 % larger."""

SHARD_ROWS = 96
"""Rows per shard, twelve blocks. A CDN will not cache an object over 512 MB
and a global array is up to a gigabyte, so an array is cut into bands; the band
also keeps the index a reader fetches at some 35 KB."""

ZSTD_LEVEL = 6
"""The level the blocks are compressed at, where every other store takes 15. A
run's whole store is cut on one machine after the run is live, and 15 costs 4 to
7 times the time of 6 for 1 to 8 % fewer bytes."""

MAX_LEVEL_HPA = 2**31 - 1
"""The `level` coordinate is ``int32``."""

INDEX_LOCATION = "start"
"""A plain range finds an index at the start; one at the end takes a suffix
range, which a browser sends a CORS preflight for."""

OTHER_BRANCHES = ("xue", "xue_profile", "xue_bundles", "xue_polar", "xue_network")
"""The group attributes of the profile's other branches."""

_ARRAY_ID = re.compile(r"^[a-z][a-z0-9]*$")
_RESERVED_IDS = ("time", "level", "latitude", "longitude")
_NEVER_WRITTEN = (zarrstore._EMPTY_ENTRY, zarrstore._EMPTY_ENTRY)
_CODEC_ZSTD = {"name": "zstd", "configuration": {"level": ZSTD_LEVEL, "checksum": True}}
_CRC32_HEX = re.compile(r"^[0-9a-f]{8}$")


class ColumnStoreError(XueError):
    """A column store that cannot be cut or does not read back."""


def _same(left: object, right: object) -> bool:
    """Whether two JSON values are the same: ``1`` and ``1.0`` are not, since
    they are spelled apart in a document; key order does not matter."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        assert isinstance(right, dict)
        return left.keys() == right.keys() and all(_same(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        assert isinstance(right, list)
        return len(left) == len(right) and all(map(_same, left, right))
    return left == right


# -- geometry --------------------------------------------------------------------


@dataclass(frozen=True)
class ColumnGeometry:
    """How one array is cut. ``levels`` is None on a surface array."""

    frame_count: int
    levels: int | None
    height: int
    width: int
    block_height: int = BLOCK[0]
    block_width: int = BLOCK[1]
    shard_height: int = SHARD_ROWS
    shard_width: int = 0
    """Columns per shard; 0 is the width rounded up to whole blocks."""

    def __post_init__(self) -> None:
        if not self.shard_width:
            object.__setattr__(self, "shard_width", -(-self.width // self.block_width) * self.block_width)
        if min(self.frame_count, self.level_count, self.height, self.width, self.block_height, self.block_width) < 1:
            raise ColumnStoreError("a column array has no empty dimension")
        if self.shard_height < 1 or self.shard_height % self.block_height or self.shard_width % self.block_width:
            raise ColumnStoreError("a shard must be whole blocks")
        if self.shard_width < self.width:
            raise ColumnStoreError("a shard must span the grid's width")

    @property
    def level_count(self) -> int:
        return 1 if self.levels is None else self.levels

    @property
    def _leading(self) -> tuple[int, ...]:
        return (self.frame_count,) if self.levels is None else (self.frame_count, self.levels)

    @property
    def shape(self) -> tuple[int, ...]:
        return (*self._leading, self.height, self.width)

    @property
    def inner_shape(self) -> tuple[int, ...]:
        return (*self._leading, self.block_height, self.block_width)

    @property
    def shard_shape(self) -> tuple[int, ...]:
        return (*self._leading, self.shard_height, self.shard_width)

    @property
    def inner_length(self) -> int:
        return math.prod(self.inner_shape)

    @property
    def block_rows(self) -> int:
        """Block rows of a shard, whether or not the grid reaches them."""
        return self.shard_height // self.block_height

    @property
    def block_columns(self) -> int:
        return self.shard_width // self.block_width

    @property
    def chunks_per_shard(self) -> int:
        return self.block_rows * self.block_columns

    @property
    def index_bytes(self) -> int:
        return zarrstore._INDEX_ENTRY.size * self.chunks_per_shard + zarrstore._INDEX_CHECKSUM.size

    @property
    def shard_count(self) -> int:
        return -(-self.height // self.shard_height)

    def shard_path(self, shard: int) -> str:
        return "/".join(("c", *("0" for _ in self._leading), str(shard), "0"))

    def shard_rows(self, shard: int) -> range:
        """The grid rows of a shard, fewer than its height on the last."""
        first = shard * self.shard_height
        return range(first, min(first + self.shard_height, self.height))

    def locate(self, row: int, column: int) -> tuple[int, int]:
        """The shard of a cell and its block's entry in that shard's index."""
        if not (0 <= row < self.height and 0 <= column < self.width):
            raise ColumnStoreError(f"cell ({row}, {column}) is outside the grid")
        shard, within = divmod(row, self.shard_height)
        return shard, (within // self.block_height) * self.block_columns + column // self.block_width

    @classmethod
    def from_document(cls, document: dict[str, Any], where: str) -> ColumnGeometry:
        """The geometry an array's document declares, checked: a wrong one
        would have a reader index outside the array."""
        shape = document.get("shape")
        try:
            outer = document["chunk_grid"]["configuration"]["chunk_shape"]
            (codec,) = document["codecs"]
            inner = codec["configuration"]["chunk_shape"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ColumnStoreError(f"{where}: not an array under one sharding codec") from exc
        shapes = (shape, outer, inner)
        if codec.get("name") != "sharding_indexed" or not all(
            isinstance(value, list) and all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in value)
            for value in shapes
        ):
            raise ColumnStoreError(f"{where}: not an array under one sharding codec")
        if len(shape) not in (3, 4) or {len(outer), len(inner)} != {len(shape)}:
            raise ColumnStoreError(f"{where}: shape and chunk shapes must agree in three or four dimensions")
        if outer[:-2] != shape[:-2] or inner[:-2] != shape[:-2]:
            raise ColumnStoreError(f"{where}: a chunk must be the whole axis and every level")
        return cls(
            frame_count=shape[0],
            levels=shape[1] if len(shape) == 4 else None,
            height=shape[-2],
            width=shape[-1],
            block_height=inner[-2],
            block_width=inner[-1],
            shard_height=outer[-2],
            shard_width=outer[-1],
        )


# -- what a map store holds --------------------------------------------------------


@dataclass(frozen=True)
class MapArray:
    """One variable's array in a map store: its shard and how to read it."""

    path: Path
    geometry: zarrstore.ArrayGeometry
    delta: bool
    index_location: str
    fill_value: int


@dataclass(frozen=True)
class ColumnMember:
    bundle_id: str
    variable: dict[str, Any]
    array: MapArray

    @property
    def id(self) -> str:
        return self.variable["id"]

    @property
    def quantization(self) -> dict[str, Any]:
        return self.variable["quantization"]

    @property
    def nodata_code(self) -> int:
        code = self.quantization.get("nodataCode")
        if not isinstance(code, int) or isinstance(code, bool) or not 0 <= code <= 255:
            raise ColumnStoreError(f"{self.id}: nodataCode is not a byte")
        return code


@dataclass(frozen=True)
class ColumnArray:
    """One array of the store about to be cut."""

    id: str
    levels: tuple[int, ...] | None
    time: dict[str, Any]
    members: tuple[ColumnMember, ...]
    delta: bool
    fill_value: int
    geometry: ColumnGeometry


@dataclass(frozen=True)
class ColumnLayout:
    """Everything the store is a function of besides the codes."""

    identity: dict[str, Any]
    """``model``, ``product``, ``runTime`` and ``profile``."""
    time: dict[str, Any]
    grid: dict[str, Any]
    arrays: tuple[ColumnArray, ...]


# -- the plan ----------------------------------------------------------------------


def isobaric_level(variable_id: str) -> int | None:
    """The isobaric surface of a registered variable in hPa, None on any other
    surface."""
    spec = variable_spec(variable_id)
    if spec.grib2_level_type != ISOBARIC_SURFACE:
        return None
    pascals = spec.grib2_level_value
    if pascals is None or pascals <= 0 or pascals % PASCALS_PER_HPA:
        raise ColumnStoreError(f"{variable_id}: its isobaric surface is not a whole number of hPa")
    return int(pascals) // PASCALS_PER_HPA


def _map_store_path(entries: dict[str, Any], bundle_id: str) -> str:
    """The full-resolution map store a manifest names for a member's bundle."""
    if bundle_id not in entries:
        raise ColumnStoreError(f"the manifest lacks bundle {bundle_id}, a member of the column store")
    store = entries[bundle_id].get("zarr")
    if not isinstance(store, dict) or not isinstance(store.get("path"), str):
        raise ColumnStoreError(f"bundle {bundle_id} names no whole map store to cut columns from")
    return store["path"]


def build_plan(source: SourceSpec, manifest: dict[str, Any]) -> dict[str, Any]:
    """The plan of a run's column store, the document either writer takes: the
    source's ``column_arrays``, each with its members' predictor and isobaric
    surfaces from the registry and its members' map store from the manifest."""
    if not source.column_arrays:
        raise ColumnStoreError(f"{source.id} ships no column store")
    entries = {entry["variable"]: entry for entry in manifest["bundles"]}
    arrays: list[dict[str, Any]] = []
    for spec in source.column_arrays:
        predictors = {
            "raw" if variable_spec(variable_id).predictor == "raw" else "previous" for _, variable_id in spec.members
        }
        if len(predictors) != 1:
            raise ColumnStoreError(f"column array {spec.id} mixes predicted and RAW variables")
        levels = [isobaric_level(variable_id) for _, variable_id in spec.members]
        array: dict[str, Any] = {"id": spec.id, "predictor": predictors.pop()}
        if None not in levels:
            array["levels"] = levels
        elif len(levels) != 1:
            raise ColumnStoreError(f"column array {spec.id}: more than one member, not all on isobaric surfaces")
        array["members"] = [
            {"bundle": bundle_id, "variable": variable_id, "store": _map_store_path(entries, bundle_id)}
            for bundle_id, variable_id in spec.members
        ]
        arrays.append(array)
    return {"columnsPlan": PLAN_VERSION, "arrays": arrays}


def _closed(node: object, required: tuple[str, ...], optional: tuple[str, ...], where: str) -> dict[str, Any]:
    if not isinstance(node, dict) or not set(required) <= set(node) <= {*required, *optional}:
        raise ColumnStoreError(f"{where} must be an object of {', '.join((*required, *optional))}")
    return node


def _check_plan(plan: object) -> list[dict[str, Any]]:
    """A plan's arrays, its shape checked: an unknown version, key or
    predictor is refused, as the native writer refuses it."""
    document = _closed(plan, ("columnsPlan", "arrays"), (), "the column plan")
    if not _same(document["columnsPlan"], PLAN_VERSION):
        raise ColumnStoreError(f"column plan version {document['columnsPlan']!r} is not {PLAN_VERSION}")
    arrays = document["arrays"]
    if not isinstance(arrays, list) or not arrays:
        raise ColumnStoreError("a column store holds at least one array")
    seen_arrays: set[str] = set()
    seen_variables: set[str] = set()
    for number, entry in enumerate(arrays):
        array = _closed(entry, ("id", "predictor", "members"), ("levels",), f"column plan array {number}")
        array_id = array["id"]
        if not isinstance(array_id, str) or not _ARRAY_ID.fullmatch(array_id) or array_id in _RESERVED_IDS:
            raise ColumnStoreError(f"{array_id!r} cannot name a column array")
        if array_id in seen_arrays:
            raise ColumnStoreError(f"column array {array_id} occurs twice")
        seen_arrays.add(array_id)
        if array["predictor"] not in PREDICTORS:
            raise ColumnStoreError(f"column array {array_id}: unknown predictor {array['predictor']!r}")
        members = array["members"]
        if not isinstance(members, list) or not members:
            raise ColumnStoreError(f"column array {array_id} has no members")
        for member in members:
            _closed(member, ("bundle", "variable", "store"), (), f"column array {array_id}: a member")
            if not all(isinstance(value, str) and value for value in member.values()):
                raise ColumnStoreError(f"column array {array_id}: a member names its bundle, variable and store")
            if not _ARRAY_ID.fullmatch(member["bundle"]):
                raise ColumnStoreError(f"{member['bundle']!r} is not a bundle id")
            if member["variable"] in seen_variables:
                raise ColumnStoreError(f"variable {member['variable']} occurs twice in one column store")
            seen_variables.add(member["variable"])
        if "levels" in array:
            _check_levels(array["levels"], len(members), f"column array {array_id}")
        elif len(members) != 1 or members[0]["variable"] != array_id:
            raise ColumnStoreError(f"column array {array_id}: a surface array has one member and is named by it")
    return arrays


def _check_levels(levels: object, count: int, where: str) -> None:
    if (
        not isinstance(levels, list)
        or not all(_is_count(level) and level <= MAX_LEVEL_HPA for level in levels)
        or any(after >= before for before, after in zip(levels, levels[1:]))
        or len(levels) != count
    ):
        raise ColumnStoreError(f"{where}: levels must be strictly descending hPa of at most {MAX_LEVEL_HPA}, one per member")


class _MapStores:
    """The map stores a plan names, each group document read once."""

    def __init__(self, run_directory: Path) -> None:
        self._run_directory = run_directory
        self._groups: dict[str, dict[str, Any]] = {}

    def member(self, bundle_id: str, variable_id: str, path: str) -> tuple[dict[str, Any], ColumnMember]:
        """One member and its bundle's metadata."""
        store = self._run_directory / path
        if path not in self._groups:
            self._groups[path] = zarrstore.read_group(store)
        group = self._groups[path]
        where = f"{path}/{variable_id}"
        attributes = group["attributes"]
        metadata = attributes["xue"] if "xue" in attributes else attributes["xue_bundles"].get(bundle_id)
        if not isinstance(metadata, dict):
            raise ColumnStoreError(f"{path} carries no bundle {bundle_id}")
        binformat.parse_metadata(metadata)
        variable = next((v for v in metadata["variables"] if v["id"] == variable_id), None)
        # The consolidated block holds every array's document, so a cut needs
        # no object of the store but its group document and the shards.
        document = ((group.get("consolidated_metadata") or {}).get("metadata") or {}).get(variable_id)
        if variable is None or not isinstance(document, dict):
            raise ColumnStoreError(f"bundle {bundle_id} of {path} has no array {variable_id}")
        geometry, delta, index_location, fill_value = zarrstore.array_layout(document)
        grid = metadata["grid"]
        if geometry.shape != (metadata["time"]["frameCount"], grid["height"], grid["width"]):
            raise ColumnStoreError(f"{where}: the array's shape is not its bundle's")
        if geometry.shard_count != 1:
            raise ColumnStoreError(f"{where}: a column store is cut from arrays of one shard")
        shard = store / variable_id / "c" / "0" / "0" / "0"
        return metadata, ColumnMember(bundle_id, variable, MapArray(shard, geometry, delta, index_location, fill_value))


def resolve_plan(run_directory: Path, plan: dict[str, Any]) -> ColumnLayout:
    """Read the metadata of the stores a plan names and settle the store's
    documents, refusing what a column store cannot hold."""
    stores = _MapStores(run_directory)
    identity: dict[str, Any] | None = None
    grid: dict[str, Any] = {}
    store_time: dict[str, Any] = {}
    planned: list[ColumnArray] = []
    for spec in _check_plan(plan):
        array_id = spec["id"]
        members: list[ColumnMember] = []
        times: list[dict[str, Any]] = []
        for entry in spec["members"]:
            metadata, member = stores.member(entry["bundle"], entry["variable"], entry["store"])
            if "projection" in metadata["grid"]:
                raise ColumnStoreError(f"{entry['bundle']}: a column store is for regular and Gaussian grids")
            facts = {key: metadata.get(key) for key in ("model", "product", "runTime", "profile")}
            if identity is None:
                identity, grid, store_time = facts, metadata["grid"], metadata["time"]
            if not _same(facts, identity):
                raise ColumnStoreError(f"{entry['bundle']}: its metadata is of another run or profile than the store's")
            if not _same(metadata["grid"], grid):
                raise ColumnStoreError(f"{entry['bundle']}: its grid is not the column store's")
            members.append(member)
            times.append(metadata["time"])
        if not all(_same(block, times[0]) for block in times):
            raise ColumnStoreError(f"column array {array_id}: its members do not share one time axis")
        fills = {member.nodata_code for member in members}
        if len(fills) != 1:
            raise ColumnStoreError(f"column array {array_id}: its members do not share one nodataCode")
        levels = tuple(spec["levels"]) if "levels" in spec else None
        frame_count = times[0]["frameCount"]
        planned.append(
            ColumnArray(
                id=array_id,
                levels=levels,
                time=times[0],
                members=tuple(members),
                # One frame has nothing to be predicted from.
                delta=frame_count > 1 and spec["predictor"] == "previous",
                fill_value=fills.pop(),
                geometry=ColumnGeometry(frame_count, len(levels) if levels else None, grid["height"], grid["width"]),
            )
        )
    assert identity is not None
    return ColumnLayout(identity=identity, time=store_time, grid=grid, arrays=tuple(planned))


# -- reading a map store's shard -----------------------------------------------------


@lru_cache(maxsize=256)
def _map_index(path: str, _length: int, _modified: int, count: int, index_location: str) -> np.ndarray:
    """A map shard's index, ``[chunk, 2]``. Cached per process: every band of
    an array reads the same one, and its checksum is a Python loop."""
    length = zarrstore._INDEX_ENTRY.size * count + zarrstore._INDEX_CHECKSUM.size
    with open(path, "rb") as shard:
        if index_location == "end":
            shard.seek(-length, os.SEEK_END)
        index = shard.read(length)
    return np.array(zarrstore._parse_index(index, count), dtype=np.uint64)


def read_map_rows(array: MapArray, rows: range) -> np.ndarray:
    """A band of rows of every frame of one map array, ``[frames, rows,
    width]``, read by seeking to the tiles it overlaps."""
    geometry = array.geometry
    if not array.path.is_file():
        raise ColumnStoreError(f"no map shard at {array.path}")
    status = array.path.stat()
    if status.st_size < geometry.shard_index_bytes:
        raise ColumnStoreError(f"{array.path} is smaller than its index")
    index = _map_index(
        str(array.path), status.st_size, status.st_mtime_ns, geometry.chunks_per_shard, array.index_location
    )
    band = np.full((geometry.frame_count, len(rows), geometry.width), array.fill_value, dtype=np.uint8)
    inner_length = math.prod(geometry.inner_shape)
    with array.path.open("rb") as shard:
        for time_chunk in range(geometry.time_chunks):
            frames = geometry.frames(time_chunk)
            _, first_entry = geometry.shard_of(time_chunk)
            for tile_row in range(rows.start // geometry.tile_height, (rows.stop - 1) // geometry.tile_height + 1):
                top = tile_row * geometry.tile_height
                low, high = max(rows.start, top), min(rows.stop, top + geometry.tile_height)
                for tile_column in range(geometry.tile_columns):
                    tile = tile_row * geometry.tile_columns + tile_column
                    offset, length = (int(value) for value in index[first_entry + tile])
                    if (offset, length) == _NEVER_WRITTEN:
                        continue
                    if offset + length > status.st_size:
                        raise ColumnStoreError(f"{array.path}: an index entry exceeds the shard")
                    shard.seek(offset)
                    block = _decode(shard.read(length), inner_length).reshape(geometry.inner_shape)
                    if array.delta:
                        block = zarrstore._delta_decode(block)
                    left = tile_column * geometry.tile_width
                    right = min(left + geometry.tile_width, geometry.width)
                    band[frames.start : frames.stop, low - rows.start : high - rows.start, left:right] = block[
                        : len(frames), low - top : high - top, : right - left
                    ]
    return band


def _decode(payload: bytes, expected: int) -> np.ndarray:
    if not zstdcli.frame_has_checksum(payload):
        raise ColumnStoreError("an inner chunk must be a Zstandard frame with a content checksum")
    return np.frombuffer(zstdcli.decompress(payload, expected_length=expected), dtype=np.uint8)


# -- writing -------------------------------------------------------------------------


@dataclass(frozen=True)
class _ShardTask:
    path: Path
    geometry: ColumnGeometry
    shard: int
    members: tuple[MapArray, ...]
    delta: bool
    fill_value: int


def _block_rows(task: _ShardTask) -> Iterator[np.ndarray]:
    """A shard's block rows that the grid reaches, each ``[block, frames,
    levels, blockHeight, blockWidth]``, cells past the grid at the fill."""
    geometry = task.geometry
    rows = geometry.shard_rows(task.shard)
    padded = -(-len(rows) // geometry.block_height) * geometry.block_height
    cube = np.full(
        (geometry.frame_count, geometry.level_count, padded, geometry.shard_width), task.fill_value, dtype=np.uint8
    )
    for level, member in enumerate(task.members):
        cube[:, level, : len(rows), : geometry.width] = read_map_rows(member, rows)
    for top in range(0, padded, geometry.block_height):
        strip = cube[:, :, top : top + geometry.block_height, :].reshape(
            geometry.frame_count,
            geometry.level_count,
            geometry.block_height,
            geometry.block_columns,
            geometry.block_width,
        )
        yield np.ascontiguousarray(strip.transpose(3, 0, 1, 2, 4))


def _write_shard(task: _ShardTask) -> tuple[int, str]:
    """Cut one shard; its length and CRC-32. The payloads are written as they
    are compressed and the index, whose length is known, over the gap left for
    it, so no shard is held compressed in memory."""
    geometry = task.geometry
    entries = bytearray()
    cursor = geometry.index_bytes
    task.path.parent.mkdir(parents=True, exist_ok=True)
    with task.path.open("wb") as shard:
        shard.seek(cursor)
        for blocks in _block_rows(task):
            if task.delta:
                stored = blocks.copy()
                stored[:, 1:] -= blocks[:, :-1]
                blocks = stored
            for block in blocks:
                payload = zstdcli.compress(block.tobytes(), level=ZSTD_LEVEL, checksum=True)
                entries += zarrstore._INDEX_ENTRY.pack(cursor, len(payload))
                shard.write(payload)
                cursor += len(payload)
        written = len(entries) // zarrstore._INDEX_ENTRY.size
        entries += zarrstore._INDEX_ENTRY.pack(*_NEVER_WRITTEN) * (geometry.chunks_per_shard - written)
        shard.seek(0)
        shard.write(bytes(entries) + zarrstore._INDEX_CHECKSUM.pack(zarrstore.crc32c(bytes(entries))))
    return cursor, _file_crc32(task.path)


def _file_crc32(path: Path) -> str:
    crc32 = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            crc32 = zlib.crc32(chunk, crc32)
    return f"{crc32:08x}"


def _run_tasks(tasks: list[_ShardTask], jobs: int) -> list[tuple[int, str]]:
    if jobs <= 1 or len(tasks) <= 1:
        return [_write_shard(task) for task in tasks]
    pool = ProcessPoolExecutor(max_workers=min(jobs, len(tasks)))
    try:
        return list(pool.map(_write_shard, tasks))
    finally:
        pool.shutdown(cancel_futures=True)


def _linear_packing(members: tuple[ColumnMember, ...]) -> dict[str, float] | None:
    """CF packing, only where one spelling holds for every level."""
    codebooks = [member.quantization for member in members]
    if any(codebook.get("type") != "linear" or not _same(codebook, codebooks[0]) for codebook in codebooks):
        return None
    return {"scale_factor": codebooks[0]["scale"], "add_offset": codebooks[0]["offset"]}


def _array_document(array: ColumnArray, dimensions: list[str]) -> dict[str, Any]:
    geometry = array.geometry
    attributes: dict[str, Any] = {"xue_columns": {"array": array.id}}
    for name, key in (("long_name", "label"), ("units", "unit")):
        values = [member.variable[key] for member in array.members if key in member.variable]
        if len(values) == len(array.members) and all(_same(value, values[0]) for value in values):
            attributes[name] = values[0]
    attributes.update(_linear_packing(array.members) or {})
    attributes["_FillValue"] = array.fill_value
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": list(geometry.shape),
        "data_type": "uint8",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(geometry.shard_shape)}},
        "chunk_key_encoding": {"name": "default"},
        "fill_value": array.fill_value,
        "codecs": [
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": list(geometry.inner_shape),
                    "codecs": ([zarrstore._CODEC_DELTA] if array.delta else [])
                    + [zarrstore._CODEC_BYTES, _CODEC_ZSTD],
                    "index_codecs": [zarrstore._CODEC_BYTES_LITTLE, zarrstore._CODEC_CRC32C],
                    "index_location": INDEX_LOCATION,
                },
            }
        ],
        "attributes": attributes,
        "dimension_names": dimensions,
    }


def _dimensions(layout: ColumnLayout, array: ColumnArray) -> list[str]:
    time_name = "time" if _same(array.time, layout.time) else f"time_{array.id}"
    if array.levels is None:
        return [time_name, "latitude", "longitude"]
    first = next(other.levels for other in layout.arrays if other.levels is not None)
    return [time_name, "level" if array.levels == first else f"level_{array.id}", "latitude", "longitude"]


def _write_level(zarr_dir: Path, name: str, levels: tuple[int, ...]) -> None:
    attributes = {"standard_name": "air_pressure", "units": "hPa", "positive": "down"}
    document = zarrstore._coordinate_metadata(len(levels), "int32", 0, attributes, name)
    zarrstore._write(zarr_dir / name / "zarr.json", zarrstore._dump_json(document))
    zarrstore._write(zarr_dir / name / "c" / "0", np.array(levels, dtype="<i4").tobytes())


def _write_time_and_grid(zarr_dir: Path, layout: ColumnLayout, name: str, time_block: dict[str, Any]) -> None:
    """One time coordinate, and the grid's two with it: the grid store's own
    encoding, which takes the three together."""
    schema = {"schemaVersion": 3, "time": time_block}
    unit_seconds, offsets = binformat.time_axis(schema)
    axis = SimpleNamespace(
        metadata={"runTime": layout.identity["runTime"], "grid": layout.grid},
        unit_seconds=unit_seconds,
        frame_offsets=offsets,
        width=layout.grid["width"],
        height=layout.grid["height"],
    )
    zarrstore._write_coordinates(axis, zarr_dir, (name, "latitude", "longitude"))  # type: ignore[arg-type]


def _write_coordinates(zarr_dir: Path, layout: ColumnLayout) -> list[str]:
    """Every coordinate the arrays name; their names in the consolidated
    block's order."""
    names = ["time", "latitude", "longitude"]
    own: list[str] = []
    _write_time_and_grid(zarr_dir, layout, "time", layout.time)
    for array in layout.arrays:
        dimensions = _dimensions(layout, array)
        if dimensions[0] != "time":
            _write_time_and_grid(zarr_dir, layout, dimensions[0], array.time)
            own.append(dimensions[0])
        if array.levels is None:
            continue
        if dimensions[1] == "level":
            if "level" not in names:
                _write_level(zarr_dir, "level", array.levels)
                names.insert(1, "level")
        else:
            _write_level(zarr_dir, dimensions[1], array.levels)
            own.append(dimensions[1])
    return names + own


def write_column_store(
    run_directory: Path, plan: dict[str, Any], output: Path, jobs: int | None = None
) -> dict[str, Any]:
    """Cut the column store of a :func:`build_plan` plan into ``output``, which
    must not exist: the shards in ``jobs`` processes (one per core by default),
    then the documents, the group's last. The bytes do not depend on ``jobs``.
    Returns ``path``, ``byteLength``, ``crc32`` and each array's shard bytes,
    the wheel's report."""
    if output.exists():
        raise ColumnStoreError(f"{output} exists")
    layout = resolve_plan(run_directory, plan)
    output.mkdir(parents=True)
    try:
        return _write_store(output, layout, jobs or os.cpu_count() or 1)
    except BaseException:
        # Half a store must not stand where one is expected.
        shutil.rmtree(output, ignore_errors=True)
        raise


def _write_store(output: Path, layout: ColumnLayout, jobs: int) -> dict[str, Any]:
    tasks = [
        _ShardTask(
            output / array.id / array.geometry.shard_path(shard),
            array.geometry,
            shard,
            tuple(member.array for member in array.members),
            array.delta,
            array.fill_value,
        )
        for array in layout.arrays
        for shard in range(array.geometry.shard_count)
    ]
    results = iter(_run_tasks(tasks, jobs))
    sizes: dict[str, int] = {}
    metadata: dict[str, Any] = {}
    entries: list[dict[str, Any]] = []
    for array in layout.arrays:
        shards = [next(results) for _ in range(array.geometry.shard_count)]
        document = _array_document(array, _dimensions(layout, array))
        metadata[array.id] = document
        zarrstore._write(output / array.id / "zarr.json", zarrstore._dump_json(document))
        sizes[array.id] = sum(length for length, _ in shards)
        entry: dict[str, Any] = {"id": array.id}
        if not _same(array.time, layout.time):
            entry["time"] = array.time
        if array.levels is not None:
            entry["levels"] = list(array.levels)
        entry["members"] = [{"bundle": member.bundle_id, "variable": member.variable} for member in array.members]
        # The group document's CRC-32 is the store's `?v=`, and the codes are
        # nowhere else in it: without these a store cut again from other codes
        # would keep the address of the one it replaces.
        entry["shards"] = [crc32 for _, crc32 in shards]
        entries.append(entry)
    for name in _write_coordinates(output, layout):
        metadata[name] = zarrstore.read_json(output / name / "zarr.json")
    root = zarrstore._dump_json(
        {
            "zarr_format": 3,
            "node_type": "group",
            "attributes": {
                "xue_columns": {
                    "version": COLUMNS_VERSION,
                    **layout.identity,
                    "time": layout.time,
                    "grid": layout.grid,
                    "arrays": entries,
                }
            },
            "consolidated_metadata": {"kind": "inline", "must_understand": False, "metadata": metadata},
        }
    )
    zarrstore._write(output / "zarr.json", root)
    return {
        "path": str(output),
        "byteLength": zarrstore.store_byte_length(output),
        "crc32": crc32_hex(root),
        "arrays": sizes,
    }


# -- reading (NumPy only) --------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ColumnStoreError(message)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _check_array(block: dict[str, Any], entry: object, seen: set[str], where: str) -> tuple[str, object]:
    """One ``arrays`` entry's shape; its id and its own or the store's time."""
    _require(isinstance(entry, dict), f"{where}: an array entry must be an object")
    assert isinstance(entry, dict)
    array_id = entry.get("id")
    _require(
        isinstance(array_id, str) and bool(_ARRAY_ID.fullmatch(array_id)) and array_id not in _RESERVED_IDS,
        f"{where}: {array_id!r} cannot name an array",
    )
    members = entry.get("members")
    _require(isinstance(members, list) and bool(members), f"{where}: {array_id} has no members")
    if "levels" in entry:
        _check_levels(entry["levels"], len(members), f"{where}: {array_id}")
    else:
        _require(len(members) == 1, f"{where}: the surface array {array_id} must have one member")
        variable = members[0].get("variable") if isinstance(members[0], dict) else None
        _require(
            not isinstance(variable, dict) or variable.get("id") == array_id,
            f"{where}: the surface array {array_id} must be named for its variable",
        )
    for member in members:
        variable = member.get("variable") if isinstance(member, dict) else None
        _require(
            isinstance(variable, dict)
            and isinstance(variable.get("id"), str)
            and isinstance(variable.get("quantization"), dict)
            and isinstance(member.get("bundle"), str)
            and bool(_ARRAY_ID.fullmatch(member["bundle"])),
            f"{where}: {array_id} has a member without a bundle and a variable",
        )
        _require(variable["id"] not in seen, f"{where}: variable {variable['id']} occurs twice")
        seen.add(variable["id"])
    if "shards" in entry:
        shards = entry["shards"]
        _require(
            isinstance(shards, list) and all(isinstance(crc, str) and _CRC32_HEX.fullmatch(crc) for crc in shards),
            f"{where}: {array_id}'s shards must be CRC-32s",
        )
    return array_id, entry.get("time", block["time"])


def _check_document(
    document: object,
    entry: dict[str, Any],
    time_block: dict[str, Any],
    grid: dict[str, Any],
    store_levels: list[int] | None,
    where: str,
) -> ColumnGeometry:
    """A data array's document held to its ``arrays`` entry. ``store_levels``
    are the first levelled array's, the ``level`` coordinate's."""
    _require(isinstance(document, dict), f"{where}: no array document")
    assert isinstance(document, dict)
    geometry = ColumnGeometry.from_document(document, where)
    levels = entry.get("levels")
    expected = (time_block["frameCount"], *([len(levels)] if levels is not None else []), grid["height"], grid["width"])
    _require(geometry.shape == expected, f"{where}: shape {geometry.shape} is not the block's {expected}")
    _require(document.get("data_type") == "uint8", f"{where}: a data array is uint8")
    configuration = document["codecs"][0]["configuration"]
    chain = configuration.get("codecs")
    _require(
        isinstance(chain, list)
        and [codec.get("name") for codec in chain] in (["bytes", "zstd"], [zarrstore.DELTA_CODEC, "bytes", "zstd"]),
        f"{where}: the inner chain must be [bytes, zstd] or [xue.delta, bytes, zstd]",
    )
    if chain[0]["name"] == zarrstore.DELTA_CODEC:
        _require(chain[0].get("configuration") == {"axis": 0}, f"{where}: xue.delta runs along axis 0")
    _require(configuration.get("index_location", "end") in zarrstore.INDEX_LOCATIONS, f"{where}: unknown index location")
    _require(
        configuration.get("index_codecs") == [zarrstore._CODEC_BYTES_LITTLE, zarrstore._CODEC_CRC32C],
        f"{where}: the index codecs must be [bytes little-endian, crc32c]",
    )
    fill_value = document.get("fill_value")
    fills = {member["variable"]["quantization"].get("nodataCode") for member in entry["members"]}
    _require(isinstance(fill_value, int) and fills == {fill_value}, f"{where}: fill_value is not its members' nodataCode")
    array_id = entry["id"]
    attributes = document.get("attributes")
    _require(
        isinstance(attributes, dict) and attributes.get("xue_columns") == {"array": array_id},
        f"{where}: the xue_columns attribute does not name the array",
    )
    dimensions = [f"time_{array_id}" if "time" in entry else "time"]
    if levels is not None:
        dimensions.append("level" if levels == store_levels else f"level_{array_id}")
    names = document.get("dimension_names")
    _require(names == [*dimensions, "latitude", "longitude"], f"{where}: dimension names {names} are not the profile's")
    return geometry


@dataclass(frozen=True)
class _ReadArray:
    entry: dict[str, Any]
    geometry: ColumnGeometry
    delta: bool
    index_location: str
    fill_value: int


class ColumnStore:
    """A column store opened for reading: the block and every data array's
    consolidated document checked, then columns through the shard index."""

    def __init__(self, zarr_dir: Path) -> None:
        self.path = zarr_dir
        try:
            group = zarrstore.read_json(zarr_dir / "zarr.json")
        except BundleError as exc:
            raise ColumnStoreError(str(exc)) from exc
        attributes = group.get("attributes")
        where = str(zarr_dir)
        _require(group.get("node_type") == "group" and isinstance(attributes, dict), f"{where} is not a Zarr group")
        block = attributes.get("xue_columns")
        _require(
            isinstance(block, dict) and not any(name in attributes for name in OTHER_BRANCHES),
            f"{where} is not a column store (exactly one xue_columns block)",
        )
        _require(_same(block.get("version"), COLUMNS_VERSION), f"{where}: column store version {block.get('version')!r} is not {COLUMNS_VERSION}")
        for key in ("model", "product", "runTime", "profile"):
            _require(isinstance(block.get(key), str) and bool(block[key]), f"{where}: xue_columns.{key} must be a non-empty string")
        grid = block.get("grid")
        _require(
            isinstance(grid, dict) and "projection" not in grid and _is_count(grid.get("width")) and _is_count(grid.get("height")),
            f"{where}: the grid must be a regular or Gaussian one",
        )
        self.block = block
        self._arrays: dict[str, _ReadArray] = {}
        self._indexes: dict[tuple[str, int], list[tuple[int, int]]] = {}
        consolidated = group.get("consolidated_metadata")
        documents = consolidated.get("metadata") if isinstance(consolidated, dict) and consolidated.get("kind") == "inline" else None
        _require(isinstance(documents, dict), f"{where} carries no consolidated metadata")
        _require(isinstance(block.get("arrays"), list) and bool(block["arrays"]), f"{where}: arrays must be a non-empty list")
        seen: set[str] = set()
        store_axis = self._axis(block.get("time"), [], where)
        store_levels: list[int] | None = None
        for entry in block["arrays"]:
            array_id, time_block = _check_array(block, entry, seen, where)
            _require(array_id not in self._arrays, f"{where}: array {array_id} occurs twice")
            axis = self._axis(time_block, [member["variable"] for member in entry["members"]], f"{where}/{array_id}")
            _require("time" not in entry or axis != store_axis, f"{where}: array {array_id} repeats the store's time block")
            store_levels = store_levels or entry.get("levels")
            document = documents.get(array_id)
            geometry = _check_document(document, entry, time_block, grid, store_levels, f"{where}/{array_id}")
            configuration = document["codecs"][0]["configuration"]
            self._arrays[array_id] = _ReadArray(
                entry,
                geometry,
                configuration["codecs"][0]["name"] == zarrstore.DELTA_CODEC,
                configuration.get("index_location", "end"),
                document["fill_value"],
            )
        _require(
            any("time" not in entry for entry in block["arrays"]),
            f"{where}: no array is on the store's own time axis",
        )

    def _axis(self, time_block: object, variables: list[dict[str, Any]], where: str) -> tuple[int, list[int]]:
        """A time block's unit and offsets, the block, the grid and the
        variables on it held to the rules of the bundle metadata they were
        copied from. ``numericId`` is a bundle's own, so they are renumbered."""
        _require(isinstance(time_block, dict) and _is_count(time_block.get("frameCount")), f"{where}: the time block is malformed")
        head = {
            "schemaVersion": 3,
            **{key: self.block[key] for key in ("model", "product", "runTime", "profile", "grid")},
            "time": time_block,
        }
        try:
            for first in range(0, len(variables), 255):
                numbered = [{**variable, "numericId": n} for n, variable in enumerate(variables[first : first + 255], start=1)]
                binformat.parse_metadata({**head, "variables": numbered})
            return binformat.time_axis(head)
        except BundleError as exc:
            raise ColumnStoreError(f"{where}: {exc}") from exc

    @property
    def array_ids(self) -> list[str]:
        return list(self._arrays)

    def geometry(self, array_id: str) -> ColumnGeometry:
        return self._array(array_id).geometry

    def _array(self, array_id: str) -> _ReadArray:
        if array_id not in self._arrays:
            raise ColumnStoreError(f"{self.path} holds no array {array_id}")
        return self._arrays[array_id]

    def shard_index(self, array_id: str, shard: int) -> list[tuple[int, int]]:
        """One shard's ``(offset, nbytes)`` pairs, CRC-32C verified and held."""
        if (array_id, shard) not in self._indexes:
            array = self._array(array_id)
            length = array.geometry.index_bytes
            path = self.path / array_id / array.geometry.shard_path(shard)
            with _opened(path) as handle:
                if array.index_location == "end":
                    handle.seek(max(path.stat().st_size - length, 0))
                try:
                    pairs = zarrstore._parse_index(handle.read(length), array.geometry.chunks_per_shard)
                except BundleError as exc:
                    raise ColumnStoreError(f"{path}: {exc}") from exc
            self._indexes[array_id, shard] = pairs
        return self._indexes[array_id, shard]

    def block_codes(self, array_id: str, row: int, column: int) -> np.ndarray:
        """The whole block holding a cell, ``[frames, levels, blockHeight,
        blockWidth]``."""
        array = self._array(array_id)
        geometry = array.geometry
        shard, entry = geometry.locate(row, column)
        offset, length = self.shard_index(array_id, shard)[entry]
        full = (geometry.frame_count, geometry.level_count, geometry.block_height, geometry.block_width)
        if (offset, length) == _NEVER_WRITTEN:
            return np.full(full, array.fill_value, dtype=np.uint8)
        with _opened(self.path / array_id / geometry.shard_path(shard)) as handle:
            handle.seek(offset)
            payload = handle.read(length)
        _require(len(payload) == length, f"{self.path}/{array_id}: an index entry exceeds the shard")
        stored = _decode(payload, geometry.inner_length).reshape(full)
        return zarrstore._delta_decode(stored) if array.delta else stored

    def column(self, array_id: str, row: int, column: int) -> np.ndarray:
        """One cell's codes, ``[frames, levels]``."""
        geometry = self._array(array_id).geometry
        return self.block_codes(array_id, row, column)[:, :, row % geometry.block_height, column % geometry.block_width]


def _opened(path: Path) -> BinaryIO:
    if not path.is_file():
        raise ColumnStoreError(f"no shard at {path}")
    return path.open("rb")

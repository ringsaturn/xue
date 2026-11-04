"""Xue bundles as Zarr v3 stores.

Two layouts, both normative in ``docs/zarr-profile.md``. A **shared store**
(profile 2, ``bundles[.<tier>].zarr``) holds every bundle of one resolution
tier of a run, round or segment: :func:`write_bundle_arrays` writes one
bundle's arrays and coordinates into it, :func:`write_part` the partial group
document over the bundles one build wrote, and :func:`finalize_store` the
store's group document from its parts (and, on a top-up, the live group
document). A **one-bundle store** (profile 1, :func:`write_store`) is what a
series store still is, and what every map store was before shared stores;
the readers here open both.

Either writer packs a bundle's quantized codes from the planes a build holds
in memory (:class:`CodesSource`). The metadata, shard bytes, index and
CRC-32C are assembled with NumPy alone, so the build gains no dependency.
"""

from __future__ import annotations

import json
import math
import os
import posixpath
import re
import shutil
import struct
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import binformat, zstdcli
from .errors import BundleError, ConversionError

PROFILE_VERSION = 1
"""The ``xue_profile`` of a one-bundle store: every series store, and every
map store written before shared stores."""

SHARED_PROFILE_VERSION = 2
"""The ``xue_profile`` of a shared store (``bundles[.<tier>].zarr``)."""

PROFILE_VERSIONS = (PROFILE_VERSION, SHARED_PROFILE_VERSION)

SHARED_STORE_STEM = "bundles"
"""The reserved stem of a shared store; no bundle may be named it."""

PART_PREFIX = "zarr.part."
"""File name prefix of a partial group document inside a shared store
(``zarr.part.<first bundle id>.json``). Local only: the upload targets
exclude the pattern, and :func:`finalize_store` merges the parts into the
group document."""

_SHARED_STORE_NAME = re.compile(r"^bundles(?:\.([a-z]+))?\.zarr$")
_BUNDLE_ID = re.compile(r"^[a-z][a-z0-9]*$")

TIME_CHUNK = 6
"""Frames per Zarr time chunk. The bundle's own group length, on a regular
grid rather than restarted at every change of step."""

SERIES_BLOCK = (8, 8)
"""The spatial block a series store is cut into, ``(width, height)``
(docs/zarr-profile.md, "Series store"). Measured as the sweet spot: the store
is the same size as the bundle and one cell's whole series is one inner chunk
of a few KB, where a 4 x 4 block costs four times the index for a third of
the payload and a 16 x 16 one quadruples the payload for a quarter of the
index."""

ZSTD_LEVEL = zstdcli.DEFAULT_LEVEL
DELTA_CODEC = "xue.delta"
INDEX_LOCATIONS = ("start", "end")
DEFAULT_INDEX_LOCATION = "end"
"""Where a shard keeps its index. ``end`` is the Zarr default and the one
clients fetch as an HTTP suffix range without knowing the object's length —
zarrita reads only that form; ``start`` lets a reader that already has the
object length find the index with the first request."""

_EMPTY_ENTRY = 0xFFFFFFFFFFFFFFFF
"""The shard index's marker for an inner chunk that was never written; the
export writes every chunk, so it never appears, but a reader honours it."""
_INDEX_ENTRY = struct.Struct("<QQ")
_INDEX_CHECKSUM = struct.Struct("<I")
_DIMENSIONS = ("time", "latitude", "longitude")

_CODEC_BYTES = {"name": "bytes"}
_CODEC_BYTES_LITTLE = {"name": "bytes", "configuration": {"endian": "little"}}
_CODEC_ZSTD = {"name": "zstd", "configuration": {"level": ZSTD_LEVEL, "checksum": True}}
_CODEC_CRC32C = {"name": "crc32c"}
_CODEC_DELTA = {"name": DELTA_CODEC, "configuration": {"axis": 0}}


# -- CRC-32C -------------------------------------------------------------------

def _crc32c_table() -> np.ndarray:
    table = np.empty(256, dtype=np.uint32)
    for byte in range(256):
        value = byte
        for _ in range(8):
            value = (value >> 1) ^ 0x82F63B78 if value & 1 else value >> 1
        table[byte] = value
    return table


_CRC32C_TABLE = _crc32c_table().tolist()


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli), the checksum the ``crc32c`` codec appends to a
    shard index. Not the CRC-32/IEEE the container uses for its chunks: the
    two are different polynomials over different bytes."""
    value = 0xFFFFFFFF
    table = _CRC32C_TABLE
    for byte in data:
        value = table[(value ^ byte) & 0xFF] ^ (value >> 8)
    return value ^ 0xFFFFFFFF


# -- geometry ------------------------------------------------------------------

@dataclass(frozen=True)
class ArrayGeometry:
    """How one array is cut: the regular time chunk over the bundle's tiles.

    The inner chunk is one time chunk of one of the bundle's tiles. The
    shard (Zarr's outer chunk) is ``shard_frames`` frames of the *whole
    grid*, rounded up to a whole number of inner chunks along each axis
    because the sharding codec requires it — 721 rows of 52 become 728, an
    axis of 121 frames becomes 126 — and the chunk grid is allowed to run
    past the array's shape. The exporter writes **one shard per array**:
    ``shard_frames`` is the whole axis rounded up, and the shard's inner
    chunks are the time chunks in order, each one the bundle's tiles in the
    bundle's row-major order — so a time chunk is one contiguous run of the
    object, as a group is in the container. A reader takes ``shard_frames``
    from the array's own chunk shape and handles any multiple of the time
    chunk, so a store cut one shard per time chunk still reads.
    """

    frame_count: int
    height: int
    width: int
    tile_height: int
    tile_width: int
    time_chunk: int = TIME_CHUNK
    shard_frames: int | None = None
    """Frames per shard; ``None`` is the whole axis, rounded up to whole
    time chunks."""

    def __post_init__(self) -> None:
        if self.shard_frames is None:
            object.__setattr__(self, "shard_frames", self.time_chunks * self.time_chunk)
        assert self.shard_frames is not None
        if self.shard_frames <= 0 or self.shard_frames % self.time_chunk:
            raise BundleError("a shard must hold a whole number of time chunks")

    @property
    def tile_rows(self) -> int:
        return -(-self.height // self.tile_height)

    @property
    def tile_columns(self) -> int:
        return -(-self.width // self.tile_width)

    @property
    def tile_count(self) -> int:
        return self.tile_rows * self.tile_columns

    @property
    def time_chunks(self) -> int:
        return -(-self.frame_count // self.time_chunk)

    @property
    def shape(self) -> tuple[int, int, int]:
        return self.frame_count, self.height, self.width

    @property
    def time_chunks_per_shard(self) -> int:
        assert self.shard_frames is not None
        return self.shard_frames // self.time_chunk

    @property
    def shard_count(self) -> int:
        assert self.shard_frames is not None
        return -(-self.frame_count // self.shard_frames)

    @property
    def chunks_per_shard(self) -> int:
        """Inner chunks a shard indexes: every time chunk of every tile,
        whether or not the axis reaches it."""
        return self.time_chunks_per_shard * self.tile_count

    @property
    def shard_index_bytes(self) -> int:
        """One shard's index: a pair per inner chunk plus the CRC-32C."""
        return _INDEX_ENTRY.size * self.chunks_per_shard + _INDEX_CHECKSUM.size

    @property
    def shard_shape(self) -> tuple[int, int, int]:
        assert self.shard_frames is not None
        return self.shard_frames, self.tile_rows * self.tile_height, self.tile_columns * self.tile_width

    def shard_of(self, time_chunk: int) -> tuple[int, int]:
        """The shard holding a time chunk, and the position of that time
        chunk's first tile in the shard's index (row-major over the inner
        chunk grid: time chunk, then tile)."""
        shard, within = divmod(time_chunk, self.time_chunks_per_shard)
        return shard, within * self.tile_count

    @property
    def inner_shape(self) -> tuple[int, int, int]:
        return self.time_chunk, self.tile_height, self.tile_width

    def frames(self, time_chunk: int) -> range:
        """The array frames a time chunk holds (fewer than the chunk shape on
        the last one; the rest of the chunk is padding)."""
        first = time_chunk * self.time_chunk
        return range(first, min(first + self.time_chunk, self.frame_count))

    def tile_origin(self, tile: int) -> tuple[int, int]:
        return (tile // self.tile_columns) * self.tile_height, (tile % self.tile_columns) * self.tile_width

    def tile_shape(self, tile: int) -> tuple[int, int]:
        """The tile's *clipped* shape: what of it lies inside the grid."""
        row, column = self.tile_origin(tile)
        return min(self.tile_height, self.height - row), min(self.tile_width, self.width - column)

    @classmethod
    def from_metadata(cls, metadata: dict[str, Any]) -> ArrayGeometry:
        """The geometry an array's ``zarr.json`` describes."""
        shape = metadata.get("shape")
        chunk_shape = metadata.get("chunk_grid", {}).get("configuration", {}).get("chunk_shape")
        codecs = metadata.get("codecs")
        if (
            not isinstance(shape, list)
            or len(shape) != 3
            or not isinstance(chunk_shape, list)
            or len(chunk_shape) != 3
            or not isinstance(codecs, list)
            or len(codecs) != 1
            or codecs[0].get("name") != "sharding_indexed"
        ):
            raise BundleError("array metadata is not a sharded three-dimensional array")
        inner = codecs[0].get("configuration", {}).get("chunk_shape")
        if not isinstance(inner, list) or len(inner) != 3:
            raise BundleError("sharding codec has no inner chunk shape")
        if int(chunk_shape[0]) <= 0 or int(chunk_shape[0]) % int(inner[0]):
            raise BundleError("outer chunk shape is not a whole number of time chunks")
        geometry = cls(
            frame_count=int(shape[0]),
            height=int(shape[1]),
            width=int(shape[2]),
            tile_height=int(inner[1]),
            tile_width=int(inner[2]),
            time_chunk=int(inner[0]),
            shard_frames=int(chunk_shape[0]),
        )
        if list(geometry.shard_shape) != [int(value) for value in chunk_shape]:
            raise BundleError("outer chunk shape is not whole time chunks of whole tiles")
        return geometry


# -- report --------------------------------------------------------------------

@dataclass
class ExportReport:
    """What one one-bundle store write wrote."""

    path: Path
    byte_length: int
    crc32: str
    delta: bool
    index_location: str
    arrays: dict[str, int] = field(default_factory=dict)
    chunk_count: int = 0

    def descriptor(self, manifest_dir: Path) -> dict[str, Any]:
        """The manifest's ``zarr`` descriptor: the store's root, relative to
        the manifest, the sum of every object in it and the CRC-32 of its
        root ``zarr.json`` — the one value a client cache-busts with."""
        return {
            "path": self.path.relative_to(manifest_dir).as_posix(),
            "byteLength": self.byte_length,
            "crc32": self.crc32,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "byteLength": self.byte_length,
            "crc32": self.crc32,
            "delta": self.delta,
            "indexLocation": self.index_location,
            "arrays": dict(self.arrays),
            "chunkCount": self.chunk_count,
        }


# -- writing -------------------------------------------------------------------

def _dump_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write(path: Path, data: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def _pad_block(block: np.ndarray, geometry: ArrayGeometry, nodata: int) -> np.ndarray:
    """A clipped block at the full inner-chunk shape, nodata beyond the grid
    and beyond the axis: a Zarr chunk is always stored whole."""
    padded = np.full(geometry.inner_shape, nodata, dtype=np.uint8)
    frames, height, width = block.shape
    padded[:frames, :height, :width] = block
    return padded


def _delta_encode(block: np.ndarray) -> np.ndarray:
    """The ``xue.delta`` codec's encode: the first frame whole, every later
    one its modulo-256 difference against the frame before it — the same
    two lines the container's PREVIOUS predictor is (docs/format.md)."""
    stored = block.copy()
    stored[1:] = (block[1:] - block[:-1]).astype(np.uint8)
    return stored


def _delta_decode(stored: np.ndarray) -> np.ndarray:
    return np.cumsum(stored, axis=0, dtype=np.uint8)


def _pack_shard(payloads: list[bytes], index_location: str) -> bytes:
    """One shard: the inner chunks back to back, plus the index.

    The index is one ``(offset, nbytes)`` pair of little-endian ``uint64``
    per inner chunk in row-major inner-chunk order — time chunk by time
    chunk, the bundle's tile order within each — followed by the CRC-32C of
    those pairs. Offsets are from the start of the shard, so with the index
    at the start they begin after it.
    """
    index_length = _INDEX_ENTRY.size * len(payloads) + _INDEX_CHECKSUM.size
    cursor = index_length if index_location == "start" else 0
    entries = bytearray()
    for payload in payloads:
        entries += _INDEX_ENTRY.pack(cursor, len(payload))
        cursor += len(payload)
    index = bytes(entries) + _INDEX_CHECKSUM.pack(crc32c(bytes(entries)))
    body = b"".join(payloads)
    return index + body if index_location == "start" else body + index


class _ShardWriter:
    """One shard written as its chunks arrive: the payloads back to back,
    then the index (docs/zarr-profile.md, "Shard index").

    With the index at the end, the default, nothing but the index entries is
    held: a satellite disk's shard is hundreds of megabytes, and joining it
    in memory before writing doubled the export's peak. An index at the
    start needs every length before the first payload, so that form is
    buffered and packed by :func:`_pack_shard`."""

    def __init__(self, path: Path, chunk_count: int, index_location: str) -> None:
        self.path = path
        self.chunk_count = chunk_count
        self.index_location = index_location
        self.count = 0
        self._entries = bytearray()
        self._cursor = 0
        self._buffered: list[bytes] = []
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("wb") if index_location == "end" else None

    def extend(self, payloads: list[bytes]) -> None:
        self.count += len(payloads)
        if self._file is None:
            self._buffered.extend(payloads)
            return
        for payload in payloads:
            self._entries += _INDEX_ENTRY.pack(self._cursor, len(payload))
            self._cursor += len(payload)
            self._file.write(payload)

    def close(self) -> int:
        """Write the index and return the shard's length."""
        if self.count != self.chunk_count:
            if self._file is not None:
                self._file.close()
            raise AssertionError("shard payload count disagrees with the geometry")
        if self._file is None:
            return _write(self.path, _pack_shard(self._buffered, self.index_location))
        entries = bytes(self._entries)
        index = entries + _INDEX_CHECKSUM.pack(crc32c(entries))
        self._file.write(index)
        self._file.close()
        return self._cursor + len(index)


def _compress_block(block: np.ndarray) -> bytes:
    return zstdcli.compress(block.tobytes(), level=ZSTD_LEVEL, checksum=True)


def _array_metadata(
    geometry: ArrayGeometry,
    variable: dict[str, Any],
    predictor: str,
    *,
    delta: bool,
    index_location: str,
    bundle: str | None = None,
    dimensions: tuple[str, str, str] = _DIMENSIONS,
) -> dict[str, Any]:
    """One variable array's document. ``bundle`` is set in a shared store
    only, where it leads the ``xue`` block so an array opened alone knows
    which bundle's metadata describes it."""
    quantization = variable["quantization"]
    block: dict[str, Any] = {"bundle": bundle} if bundle is not None else {}
    block.update({"variable": variable, "predictor": predictor})
    attributes: dict[str, Any] = {"xue": block}
    if quantization.get("type") == "linear":
        # CF packing, so a generic client dequantizes on its own; the
        # logarithmic codebook has no CF spelling and is left to the
        # ``xue.variable.quantization`` block.
        attributes["scale_factor"] = quantization["scale"]
        attributes["add_offset"] = quantization["offset"]
    attributes["_FillValue"] = quantization["nodataCode"]
    inner_codecs = ([_CODEC_DELTA] if delta else []) + [_CODEC_BYTES, _CODEC_ZSTD]
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": list(geometry.shape),
        "data_type": "uint8",
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": list(geometry.shard_shape)}},
        "chunk_key_encoding": {"name": "default"},
        "fill_value": quantization["nodataCode"],
        "codecs": [
            {
                "name": "sharding_indexed",
                "configuration": {
                    "chunk_shape": list(geometry.inner_shape),
                    "codecs": inner_codecs,
                    "index_codecs": [_CODEC_BYTES_LITTLE, _CODEC_CRC32C],
                    "index_location": index_location,
                },
            }
        ],
        "attributes": attributes,
        "dimension_names": list(dimensions),
    }


def _coordinate_metadata(length: int, data_type: str, fill_value: Any, attributes: dict[str, Any], name: str) -> dict[str, Any]:
    return {
        "zarr_format": 3,
        "node_type": "array",
        "shape": [length],
        "data_type": data_type,
        "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [length]}},
        "chunk_key_encoding": {"name": "default"},
        "fill_value": fill_value,
        "codecs": [_CODEC_BYTES_LITTLE],
        "attributes": attributes,
        "dimension_names": [name],
    }


def _write_atomic(path: Path, data: bytes) -> int:
    """Write by rename: in a shared store several bundles write the same
    coordinate array concurrently, always the same bytes, and a rename never
    leaves a reader (or the last writer) a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return len(data)


def _write_coordinates(
    source: CodesSource, zarr_dir: Path, names: tuple[str, str, str] = _DIMENSIONS, *, atomic: bool = False
) -> dict[str, int]:
    """The three one-dimensional coordinate arrays: for xarray and its kind,
    never read by the viewer, which reads the group's metadata block.
    ``names`` are the bundle's dimension names (``time`` or ``time_<bundle>``,
    and so on): a coordinate array is named by the dimension it labels, and
    its encoding does not depend on the name."""
    grid = source.metadata["grid"]
    run_time = source.metadata["runTime"]
    unit_seconds = source.unit_seconds
    time_values = np.array([offset * unit_seconds for offset in source.frame_offsets], dtype="<i4")
    latitude = grid["firstLatitude"] + np.arange(source.height, dtype=np.float64) * grid["latitudeStep"]
    longitude = grid["firstLongitude"] + np.arange(source.width, dtype=np.float64) * grid["longitudeStep"]
    coordinates = {
        names[0]: (
            time_values,
            "int32",
            0,
            {"units": f"seconds since {run_time}", "calendar": "proleptic_gregorian", "standard_name": "time"},
        ),
        names[1]: (
            latitude.astype("<f8"),
            "float64",
            "NaN",
            {"units": "degrees_north", "standard_name": "latitude"},
        ),
        names[2]: (
            longitude.astype("<f8"),
            "float64",
            "NaN",
            {"units": "degrees_east", "standard_name": "longitude"},
        ),
    }
    write = _write_atomic if atomic else _write
    sizes = {}
    for name, (values, data_type, fill_value, attributes) in coordinates.items():
        directory = zarr_dir / name
        sizes[name] = write(
            directory / "zarr.json",
            _dump_json(_coordinate_metadata(len(values), data_type, fill_value, attributes, name)),
        ) + write(directory / "c" / "0", values.tobytes())
    return sizes


# -- chunk sources -------------------------------------------------------------

class CodesSource:
    """Where the writers take a bundle's codes from: the planes a build
    holds in memory. A store is a function of the bundle's metadata, its
    codes, its tile and each variable's predictor, nothing else.

    ``planes`` maps each variable id to its planes in frame order (one
    ``height x width`` or flat ``uint8`` array per frame of the axis);
    ``predictors`` maps it to ``previous`` or ``raw``; ``tile`` is the
    ``(width, height)`` the bundle is cut with. The metadata is taken through
    the bytes a container embeds (:func:`binformat.metadata_json_bytes`), so
    the store's ``xue`` block is the document a bundle of the same codes
    would carry. ``array_names`` lists the variables in the metadata's
    order, which is the store's array order."""

    def __init__(
        self,
        metadata: dict[str, Any],
        planes: dict[str, Any],
        predictors: dict[str, str],
        tile: tuple[int, int],
    ) -> None:
        self.metadata = json.loads(binformat.metadata_json_bytes(metadata).decode("utf-8"))
        grid = self.metadata["grid"]
        self.width = int(grid["width"])
        self.height = int(grid["height"])
        self.unit_seconds, self.frame_offsets = binformat.time_axis(self.metadata)
        self.frame_count = len(self.frame_offsets)
        self.tile = (min(tile[0], self.width), min(tile[1], self.height))
        self.array_names = [variable["id"] for variable in self.metadata["variables"]]
        if sorted(planes) != sorted(self.array_names) or sorted(predictors) != sorted(self.array_names):
            raise BundleError("the codes do not name the metadata's variables")
        for name in self.array_names:
            if len(planes[name]) != self.frame_count:
                raise BundleError(f"{name}: {len(planes[name])} planes for an axis of {self.frame_count} frames")
            if predictors[name] not in ("previous", "raw"):
                raise BundleError(f"{name}: unknown predictor {predictors[name]!r}")
        self._planes = planes
        self._predictors = predictors

    def _plane(self, name: str, frame: int) -> np.ndarray:
        plane = np.asarray(self._planes[name][frame], dtype=np.uint8)
        if plane.size != self.width * self.height:
            raise BundleError(f"{name}: frame {frame} is not one plane of the grid")
        return plane.reshape(self.height, self.width)

    def predictor(self, name: str) -> str:
        return self._predictors[name]

    def _cut(self, stack: np.ndarray, geometry: ArrayGeometry) -> list[np.ndarray]:
        blocks = []
        for tile in range(geometry.tile_count):
            row, column = geometry.tile_origin(tile)
            height, width = geometry.tile_shape(tile)
            blocks.append(stack[:, row : row + height, column : column + width])
        return blocks

    def tile_blocks(self, name: str, geometry: ArrayGeometry, frames: range) -> list[np.ndarray]:
        return self._cut(np.stack([self._plane(name, frame) for frame in frames]), geometry)

    def series_blocks(self, name: str, geometry: ArrayGeometry) -> list[np.ndarray]:
        if geometry.time_chunks != 1:
            raise AssertionError("a series geometry has one time chunk")
        return self._cut(np.stack([self._plane(name, frame) for frame in range(self.frame_count)]), geometry)


# -- the writer ----------------------------------------------------------------

def _write_array(
    source: CodesSource,
    name: str,
    variable: dict[str, Any],
    geometry: ArrayGeometry,
    array_dir: Path,
    *,
    delta: bool,
    index_location: str,
    executor: ThreadPoolExecutor,
    series: bool = False,
    bundle: str | None = None,
    dimensions: tuple[str, str, str] = _DIMENSIONS,
) -> tuple[int, int]:
    """One variable's array: its metadata and its one shard. Returns bytes
    and inner chunks written. ``series`` reads the source a plane at a time,
    the way a series geometry's thousands of tiny blocks want. ``bundle``
    and ``dimensions`` are a shared store's (:func:`write_bundle_arrays`)."""
    predictor = source.predictor(name)
    use_delta = delta and predictor == "previous"
    nodata = variable["quantization"]["nodataCode"]
    written = _write(
        array_dir / "zarr.json",
        _dump_json(
            _array_metadata(
                geometry,
                variable,
                predictor,
                delta=use_delta,
                index_location=index_location,
                bundle=bundle,
                dimensions=dimensions,
            )
        ),
    )
    if geometry.shard_count != 1:
        raise AssertionError("the exporter writes one shard per array")
    chunks = 0
    shard = _ShardWriter(array_dir / "c" / "0" / "0" / "0", geometry.chunks_per_shard, index_location)
    for time_chunk in range(geometry.time_chunks):
        blocks = (
            source.series_blocks(name, geometry)
            if series
            else source.tile_blocks(name, geometry, geometry.frames(time_chunk))
        )
        stored = [_pad_block(block, geometry, nodata) for block in blocks]
        del blocks
        if use_delta:
            stored = [_delta_encode(block) for block in stored]
        payloads = list(executor.map(_compress_block, stored))
        del stored
        shard.extend(payloads)
        chunks += len(payloads)
    # The axis rounded up to whole time chunks: the index names every inner
    # chunk of the shard's shape, and a time chunk past the axis is padding
    # the exporter still stores (at the fill value, a few bytes each).
    nodata_block = _compress_block(np.full(geometry.inner_shape, nodata, dtype=np.uint8))
    shard.extend([nodata_block] * max(0, geometry.chunks_per_shard - shard.count))
    written += shard.close()
    return written, chunks


def _store_geometry(source: CodesSource, series: bool, block: tuple[int, int] | None) -> ArrayGeometry:
    if series:
        block = block or SERIES_BLOCK
        if len(block) != 2 or block[0] < 1 or block[1] < 1:
            raise ConversionError("a series block must be two positive cell counts")
        return ArrayGeometry(
            frame_count=source.frame_count,
            height=source.height,
            width=source.width,
            tile_height=block[1],
            tile_width=block[0],
            time_chunk=source.frame_count,
        )
    tile_width, tile_height = source.tile
    return ArrayGeometry(
        frame_count=source.frame_count,
        height=source.height,
        width=source.width,
        tile_height=tile_height,
        tile_width=tile_width,
    )


def write_store(
    source: CodesSource,
    zarr_dir: Path,
    *,
    delta: bool = False,
    index_location: str = DEFAULT_INDEX_LOCATION,
    executor: ThreadPoolExecutor | None = None,
    series: bool = False,
    block: tuple[int, int] | None = None,
) -> ExportReport:
    """Write the Zarr v3 store of one bundle from a :class:`CodesSource`.

    ``zarr_dir`` is the store's root (``<bundle>.zarr``) and is replaced
    whole. ``delta`` puts the ``xue.delta`` codec in front of ``bytes`` on
    every PREVIOUS variable — never on a RAW one — so the standard chain
    stays the default any Zarr client reads without registering anything.
    ``executor`` bounds the compression a build shares between bundles.

    ``series`` cuts the other access pattern instead (docs/zarr-profile.md,
    "Series store"): one inner chunk is the whole time axis of one ``block``
    spatial block, so a cell's series is one chunk.
    """
    if index_location not in INDEX_LOCATIONS:
        raise ConversionError(f"index location must be one of {', '.join(INDEX_LOCATIONS)}, not {index_location!r}")
    geometry = _store_geometry(source, series, block)
    variables = {variable["id"]: variable for variable in source.metadata["variables"]}

    if zarr_dir.exists():
        if not zarr_dir.is_dir():
            raise ConversionError(f"{zarr_dir} exists and is not a directory")
        shutil.rmtree(zarr_dir)
    zarr_dir.mkdir(parents=True)

    own_executor = executor is None
    pool = executor or ThreadPoolExecutor(max_workers=os.cpu_count() or 4)
    try:
        report = ExportReport(path=zarr_dir, byte_length=0, crc32="", delta=delta, index_location=index_location)
        for name in source.array_names:
            written, chunks = _write_array(
                source,
                name,
                variables[name],
                geometry,
                zarr_dir / name,
                delta=delta,
                index_location=index_location,
                executor=pool,
                series=series,
            )
            report.arrays[name] = written
            report.byte_length += written
            report.chunk_count += chunks
        for name, written in _write_coordinates(source, zarr_dir).items():
            report.arrays[name] = written
            report.byte_length += written
        # The root group last: its `xue` block is the bundle's metadata
        # verbatim, which is what the viewer's parsers already read, and
        # its consolidated metadata is every array document just written,
        # inline — the Zarr v3 spelling zarr-python reads — so a client on
        # a store that cannot be listed (a plain HTTP origin) still finds
        # the arrays. The group's CRC-32, the store's `?v=`, is over this
        # document, so it covers every array's metadata too.
        root = _dump_json(
            {
                "zarr_format": 3,
                "node_type": "group",
                "attributes": {"xue": source.metadata, "xue_profile": PROFILE_VERSION},
                "consolidated_metadata": {
                    "kind": "inline",
                    "must_understand": False,
                    "metadata": {name: read_json(zarr_dir / name / "zarr.json") for name in report.arrays},
                },
            }
        )
        report.byte_length += _write(zarr_dir / "zarr.json", root)
        report.crc32 = f"{binformat.crc32_plane(root):08x}"
    finally:
        if own_executor:
            pool.shutdown()
    return report


# -- the shared store (profile 2) ----------------------------------------------

def shared_store_name(tier: str | None = None) -> str:
    """``bundles.zarr`` for the full tier, ``bundles.<tier>.zarr`` for a rung
    of the resolution ladder."""
    return f"{SHARED_STORE_STEM}.{tier}.zarr" if tier else f"{SHARED_STORE_STEM}.zarr"


def is_shared_store_path(path: str) -> bool:
    """Whether a store path names a shared store: its final component is
    exactly ``bundles.zarr`` or ``bundles.<tier>.zarr``, ``<tier>`` a
    non-empty run of lowercase ASCII letters other than ``series``. The rule
    the three manifest validators apply to let several entries name one
    store (docs/zarr-profile.md, "Manifest"); ``bundles.series.zarr`` is not
    one, so a series store keeps its own path."""
    match = _SHARED_STORE_NAME.fullmatch(posixpath.basename(path))
    return match is not None and match.group(1) != "series"


def _grid_suffix(name: str, primary: str) -> str | None:
    """``""`` for the primary name, the family id of ``<primary>_<family>``,
    ``None`` for anything else."""
    if name == primary:
        return ""
    family = name.removeprefix(f"{primary}_")
    return family if family != name and _BUNDLE_ID.fullmatch(family) else None


def _check_dimensions(bundle_id: str, dimensions: tuple[str, str, str]) -> tuple[str, str, str]:
    """A bundle's dimension names (docs/zarr-profile.md, "Coordinates"):
    ``time`` or ``time_<bundle id>``, and ``latitude`` / ``longitude`` or
    ``latitude_<family>`` / ``longitude_<family>`` of one family id."""
    dimensions = tuple(dimensions)  # type: ignore[assignment]
    if len(dimensions) == 3 and dimensions[0] in ("time", f"time_{bundle_id}"):
        latitude, longitude = _grid_suffix(dimensions[1], "latitude"), _grid_suffix(dimensions[2], "longitude")
        if latitude is not None and latitude == longitude:
            return dimensions
    raise BundleError(f"{bundle_id}: dimension names {list(dimensions)} are not the profile's")


@dataclass
class BundleWrite:
    """One bundle written into a shared store.

    ``documents`` holds every array document the bundle uses, its variable
    arrays in the metadata's order and then the coordinate arrays its
    dimensions name; ``byte_length`` is those arrays' objects (each
    ``zarr.json`` plus shard or chunk), which is what a viewer downloads for
    the bundle alone and what its manifest entry reports. ``crc32`` is the
    store's group document's, stamped once that is written."""

    bundle_id: str
    path: Path
    metadata: dict[str, Any]
    dimensions: tuple[str, str, str]
    documents: dict[str, dict[str, Any]]
    byte_length: int
    delta: bool
    index_location: str
    arrays: dict[str, int] = field(default_factory=dict)
    chunk_count: int = 0
    crc32: str = ""

    def entry(self) -> tuple[str, dict[str, Any], dict[str, dict[str, Any]]]:
        """What :func:`group_document` takes for this bundle."""
        return self.bundle_id, self.metadata, self.documents

    def descriptor(self, manifest_dir: Path) -> dict[str, Any]:
        return {
            "path": self.path.relative_to(manifest_dir).as_posix(),
            "byteLength": self.byte_length,
            "crc32": self.crc32,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "bundle": self.bundle_id,
            "byteLength": self.byte_length,
            "crc32": self.crc32,
            "delta": self.delta,
            "indexLocation": self.index_location,
            "dimensions": list(self.dimensions),
            "arrays": dict(self.arrays),
            "chunkCount": self.chunk_count,
        }


def write_bundle_arrays(
    source: CodesSource,
    zarr_dir: Path,
    bundle_id: str,
    dimensions: tuple[str, str, str] = _DIMENSIONS,
    *,
    delta: bool = False,
    index_location: str = DEFAULT_INDEX_LOCATION,
    executor: ThreadPoolExecutor | None = None,
) -> BundleWrite:
    """Write one bundle's variable arrays and the coordinate arrays its
    ``dimensions`` name into the shared store ``zarr_dir``, which is created
    if absent and otherwise left as it is: other bundles' arrays, written
    before or concurrently, stay. The group document is not touched; it is
    :func:`write_part` / :func:`finalize_store`'s.

    ``dimensions`` is what the encoder decided for the bundle
    (``discover.store_dimensions``); a coordinate shared with another
    bundle is the same bytes from either, and is written by rename so two
    concurrent writers never tear it. Arrays, codecs and chunking are exactly
    a one-bundle store's, with ``attributes.xue.bundle`` added."""
    if index_location not in INDEX_LOCATIONS:
        raise ConversionError(f"index location must be one of {', '.join(INDEX_LOCATIONS)}, not {index_location!r}")
    if not _BUNDLE_ID.fullmatch(bundle_id) or bundle_id == SHARED_STORE_STEM:
        raise BundleError(f"{bundle_id!r} cannot name a bundle of a shared store")
    dimensions = _check_dimensions(bundle_id, dimensions)
    geometry = _store_geometry(source, False, None)
    variables = {variable["id"]: variable for variable in source.metadata["variables"]}
    if zarr_dir.exists() and not zarr_dir.is_dir():
        raise ConversionError(f"{zarr_dir} exists and is not a directory")
    zarr_dir.mkdir(parents=True, exist_ok=True)

    own_executor = executor is None
    pool = executor or ThreadPoolExecutor(max_workers=os.cpu_count() or 4)
    try:
        report = BundleWrite(
            bundle_id=bundle_id,
            path=zarr_dir,
            metadata=source.metadata,
            dimensions=dimensions,
            documents={},
            byte_length=0,
            delta=delta,
            index_location=index_location,
        )
        for name in source.array_names:
            if name in dimensions:
                raise BundleError(f"{bundle_id}: variable {name} is named like a coordinate")
            written, chunks = _write_array(
                source,
                name,
                variables[name],
                geometry,
                zarr_dir / name,
                delta=delta,
                index_location=index_location,
                executor=pool,
                bundle=bundle_id,
                dimensions=dimensions,
            )
            report.arrays[name] = written
            report.chunk_count += chunks
        report.arrays.update(_write_coordinates(source, zarr_dir, dimensions, atomic=True))
        report.byte_length = sum(report.arrays.values())
        report.documents = {name: read_json(zarr_dir / name / "zarr.json") for name in report.arrays}
    finally:
        if own_executor:
            pool.shutdown()
    return report


def group_document(
    bundles: list[tuple[str, dict[str, Any], dict[str, dict[str, Any]]]],
) -> dict[str, Any]:
    """A shared store's group document over ``bundles`` — each ``(bundle
    id, metadata, array documents)`` in manifest order.

    Key order is part of the bytes (docs/zarr-profile.md, "Group document"):
    ``attributes`` holds ``xue_profile`` then ``xue_bundles``, the bundles in
    the given order; ``consolidated_metadata.metadata`` holds every variable
    array, bundle by bundle and in each bundle's ``variables`` order, then
    the primary coordinates ``time``, ``latitude``, ``longitude`` that some
    bundle uses, then every other coordinate in the order its first bundle
    names it. A bundle's coordinates are the ``dimension_names`` of its
    variable arrays, so the document alone says which arrays a bundle uses.
    Two bundles naming one coordinate must carry the same document for it."""
    if not bundles:
        raise BundleError("a shared store holds at least one bundle")
    metadata_by_bundle: dict[str, dict[str, Any]] = {}
    variable_arrays: dict[str, dict[str, Any]] = {}
    coordinates: dict[str, dict[str, Any]] = {}
    for bundle_id, metadata, documents in bundles:
        if bundle_id in metadata_by_bundle:
            raise BundleError(f"bundle {bundle_id} appears twice in one shared store")
        metadata_by_bundle[bundle_id] = metadata
        dimensions: tuple[str, ...] | None = None
        for variable in metadata["variables"]:
            name = variable["id"]
            document = documents.get(name)
            if document is None:
                raise BundleError(f"{bundle_id}: no array document for {name}")
            if name in variable_arrays or name in coordinates:
                raise BundleError(f"array {name} appears twice in one shared store")
            names = tuple(document.get("dimension_names") or ())
            if dimensions is None:
                dimensions = names
            elif names != dimensions:
                raise BundleError(f"{bundle_id}: its arrays do not share one set of dimension names")
            variable_arrays[name] = document
        assert dimensions is not None
        _check_dimensions(bundle_id, dimensions)  # type: ignore[arg-type]
        for name in dimensions:
            document = documents.get(name)
            if document is None:
                raise BundleError(f"{bundle_id}: no array document for its coordinate {name}")
            if name in variable_arrays:
                raise BundleError(f"coordinate {name} is named like a variable")
            if name in coordinates and coordinates[name] != document:
                raise BundleError(f"coordinate {name} differs between the bundles that use it")
            coordinates.setdefault(name, document)
    ordered = dict(variable_arrays)
    ordered.update({name: coordinates[name] for name in _DIMENSIONS if name in coordinates})
    ordered.update({name: document for name, document in coordinates.items() if name not in _DIMENSIONS})
    return {
        "zarr_format": 3,
        "node_type": "group",
        "attributes": {"xue_profile": SHARED_PROFILE_VERSION, "xue_bundles": metadata_by_bundle},
        "consolidated_metadata": {"kind": "inline", "must_understand": False, "metadata": ordered},
    }


def part_path(zarr_dir: Path, first_bundle_id: str) -> Path:
    """``<store>/zarr.part.<first bundle id>.json``: a build's bundles are
    disjoint from every other build's of the same run, so the first one names
    the part uniquely."""
    return zarr_dir / f"{PART_PREFIX}{first_bundle_id}.json"


def write_part(zarr_dir: Path, bundles: list[BundleWrite]) -> tuple[Path, str]:
    """The partial group document over the bundles one build wrote into
    ``zarr_dir``, in manifest order: :func:`group_document` over them alone.
    Returns its path and its CRC-32, which a partial manifest's entries carry
    until :func:`finalize_store` writes the store's group document."""
    data = _dump_json(group_document([bundle.entry() for bundle in bundles]))
    path = part_path(zarr_dir, bundles[0].bundle_id)
    _write(path, data)
    return path, f"{binformat.crc32_plane(data):08x}"


def find_parts(zarr_dir: Path) -> list[Path]:
    return sorted(zarr_dir.glob(f"{PART_PREFIX}*.json"))


def parse_shared_group(document: object, where: str = "group") -> dict[str, Any]:
    """Validate a profile-2 group (or part) document's shape: a group whose
    ``xue_profile`` is 2 and whose ``xue_bundles`` is a non-empty object, and
    no ``xue`` block of a one-bundle store beside it."""
    if not isinstance(document, dict) or document.get("node_type") != "group":
        raise BundleError(f"{where} is not a Zarr group document")
    attributes = document.get("attributes")
    if not isinstance(attributes, dict) or attributes.get("xue_profile") != SHARED_PROFILE_VERSION:
        raise BundleError(f"{where} is not a shared (profile {SHARED_PROFILE_VERSION}) store")
    bundles = attributes.get("xue_bundles")
    if not isinstance(bundles, dict) or not bundles or "xue" in attributes:
        raise BundleError(f"{where}: xue_bundles must be a non-empty object")
    metadata = (document.get("consolidated_metadata") or {}).get("metadata")
    if not isinstance(metadata, dict):
        raise BundleError(f"{where} carries no consolidated metadata")
    return document


def _document_bundles(document: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any], dict[str, dict[str, Any]]]]:
    """Each bundle of a shared group document with the array documents it
    uses, as :func:`group_document` takes them."""
    arrays = document["consolidated_metadata"]["metadata"]
    found = {}
    for bundle_id, metadata in document["attributes"]["xue_bundles"].items():
        names = [variable["id"] for variable in metadata["variables"]]
        missing = [name for name in names if name not in arrays]
        if missing:
            raise BundleError(f"{bundle_id}: the group document carries no array {missing[0]}")
        dimensions = arrays[names[0]].get("dimension_names") or []
        documents = {name: arrays[name] for name in [*names, *dimensions] if name in arrays}
        found[bundle_id] = (bundle_id, metadata, documents)
    return found


def merge_group_documents(
    bundle_order: list[str],
    parts: list[dict[str, Any]],
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The group document of a store whose bundles are ``bundle_order`` (the
    manifest's bundles that name the store, in manifest order), from the
    partial documents of the builds that wrote them and, for a top-up,
    ``base``, the group document the bucket serves now. A part's bundle
    wins over the base's; a bundle in two parts, a part's bundle the order
    does not name, or a bundle neither names is an error. The result is
    exactly :func:`group_document` over the same bundles, so a split build
    and a top-up write the bytes a whole build writes."""
    fresh: dict[str, tuple[str, dict[str, Any], dict[str, dict[str, Any]]]] = {}
    for number, part in enumerate(parts):
        for bundle_id, entry in _document_bundles(parse_shared_group(part, f"part {number}")).items():
            if bundle_id in fresh:
                raise BundleError(f"bundle {bundle_id} appears in more than one partial group document")
            fresh[bundle_id] = entry
    held = _document_bundles(parse_shared_group(base, "the live group")) if base is not None else {}
    stray = [bundle_id for bundle_id in fresh if bundle_id not in bundle_order]
    if stray:
        raise BundleError(f"partial group documents carry bundles the manifest does not name here: {stray}")
    entries = []
    for bundle_id in bundle_order:
        entry = fresh.get(bundle_id) or held.get(bundle_id)
        if entry is None:
            raise BundleError(f"no partial or live group document carries bundle {bundle_id}")
        entries.append(entry)
    return group_document(entries)


def finalize_store(
    zarr_dir: Path,
    bundle_order: list[str],
    *,
    base: dict[str, Any] | None = None,
    remove_parts: bool = False,
) -> str:
    """Write a shared store's group document, ``zarr.json``, from the parts
    in it (and ``base`` on a top-up), and return its CRC-32 — the ``crc32``
    of every manifest entry naming the store. Written last, after every
    array, and rewritten only when its bytes change, so assembling twice is
    a no-op. ``remove_parts`` drops the parts afterwards (a whole build);
    ``assemble-run`` keeps them, so it can run again."""
    parts = find_parts(zarr_dir)
    if not parts and base is None:
        raise BundleError(f"no {PART_PREFIX}*.json in {zarr_dir}")
    data = _dump_json(merge_group_documents(bundle_order, [read_json(path) for path in parts], base))
    path = zarr_dir / "zarr.json"
    if not path.is_file() or path.read_bytes() != data:
        _write(path, data)
    if remove_parts:
        for part in parts:
            part.unlink()
    return f"{binformat.crc32_plane(data):08x}"


@dataclass(frozen=True)
class SharedMember:
    """One bundle of a shared store written whole by :func:`write_shared_store`."""

    bundle_id: str
    source: CodesSource
    dimensions: tuple[str, str, str] = _DIMENSIONS
    delta: bool = False


def write_shared_store(
    zarr_dir: Path,
    members: list[SharedMember],
    *,
    index_location: str = DEFAULT_INDEX_LOCATION,
    executor: ThreadPoolExecutor | None = None,
) -> dict[str, BundleWrite]:
    """A whole shared store in one go: ``zarr_dir`` replaced, every member
    written in order, the part, then the group document, every report
    stamped with its CRC-32. What a segment, a fixture or a test writes; a
    build writes its bundles concurrently through the same three steps."""
    if zarr_dir.exists():
        if not zarr_dir.is_dir():
            raise ConversionError(f"{zarr_dir} exists and is not a directory")
        shutil.rmtree(zarr_dir)
    reports = [
        write_bundle_arrays(
            member.source,
            zarr_dir,
            member.bundle_id,
            member.dimensions,
            delta=member.delta,
            index_location=index_location,
            executor=executor,
        )
        for member in members
    ]
    write_part(zarr_dir, reports)
    crc32 = finalize_store(zarr_dir, [report.bundle_id for report in reports], remove_parts=True)
    for report in reports:
        report.crc32 = crc32
    return {report.bundle_id: report for report in reports}


# -- reading (NumPy only, for tests and inspection) -----------------------------

def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BundleError(f"{path} is not a JSON object")
    return payload


def read_group(zarr_dir: Path) -> dict[str, Any]:
    """A store's group document, either profile, its ``xue_profile`` checked:
    1 (one bundle in ``attributes.xue``) or 2 (``attributes.xue_bundles``);
    any other is refused."""
    group = read_json(zarr_dir / "zarr.json")
    attributes = group.get("attributes") if isinstance(group.get("attributes"), dict) else {}
    profile = attributes.get("xue_profile")
    if profile == SHARED_PROFILE_VERSION:
        return parse_shared_group(group, str(zarr_dir))
    if profile == PROFILE_VERSION and isinstance(attributes.get("xue"), dict) and "xue_bundles" not in attributes:
        return group
    raise BundleError(f"{zarr_dir}: xue_profile {profile!r} is not one this reader implements")


def bundle_metadata(zarr_dir: Path, bundle_id: str | None = None) -> dict[str, Any]:
    """One bundle's metadata out of a store of either profile: a one-bundle
    store's ``attributes.xue``, or ``attributes.xue_bundles[bundle_id]`` of a
    shared one, which a shared store needs the bundle's id to find. A shared
    store whose group document is not written yet (a fanned-out job's) is
    read through the part that carries the bundle."""
    if not (zarr_dir / "zarr.json").is_file() and bundle_id is not None:
        for path in find_parts(zarr_dir):
            bundles = parse_shared_group(read_json(path), str(path))["attributes"]["xue_bundles"]
            if bundle_id in bundles:
                return bundles[bundle_id]
        raise BundleError(f"{zarr_dir} has no group document and no part carrying {bundle_id}")
    group = read_group(zarr_dir)
    attributes = group["attributes"]
    if attributes["xue_profile"] == PROFILE_VERSION:
        return attributes["xue"]
    if bundle_id is None:
        raise BundleError(f"{zarr_dir} is a shared store: name the bundle to read")
    metadata = attributes["xue_bundles"].get(bundle_id)
    if not isinstance(metadata, dict):
        raise BundleError(f"{zarr_dir} carries no bundle {bundle_id}")
    return metadata


def array_dimensions(zarr_dir: Path, variable_id: str) -> tuple[str, str, str]:
    """The dimension names one array declares: ``time`` / ``latitude`` /
    ``longitude``, or a shared store's bundle-suffixed ones."""
    names = read_json(zarr_dir / variable_id / "zarr.json").get("dimension_names")
    if not isinstance(names, list) or len(names) != 3 or not all(isinstance(name, str) for name in names):
        raise BundleError(f"{zarr_dir / variable_id}: dimension_names must be three names")
    return names[0], names[1], names[2]


def _array_layout(zarr_dir: Path, variable_id: str) -> tuple[ArrayGeometry, bool, str, int]:
    metadata = read_json(zarr_dir / variable_id / "zarr.json")
    geometry = ArrayGeometry.from_metadata(metadata)
    configuration = metadata["codecs"][0]["configuration"]
    names = [codec.get("name") for codec in configuration.get("codecs", [])]
    if names not in ([DELTA_CODEC, "bytes", "zstd"], ["bytes", "zstd"]):
        raise BundleError(f"unexpected inner codec chain {names}")
    index_location = configuration.get("index_location", "end")
    if index_location not in INDEX_LOCATIONS:
        raise BundleError(f"unexpected index location {index_location!r}")
    fill_value = metadata.get("fill_value")
    if not isinstance(fill_value, int) or not 0 <= fill_value <= 255:
        raise BundleError("fill_value must be a uint8 code")
    return geometry, names[0] == DELTA_CODEC, index_location, fill_value


def _parse_index(index: bytes, count: int) -> list[tuple[int, int]]:
    """The ``(offset, nbytes)`` pairs of one shard index — the block at one
    end of a shard — CRC-32C verified."""
    if len(index) != _INDEX_ENTRY.size * count + _INDEX_CHECKSUM.size:
        raise BundleError("shard index is not one pair per inner chunk plus its checksum")
    entries, checksum = index[: -_INDEX_CHECKSUM.size], index[-_INDEX_CHECKSUM.size :]
    if _INDEX_CHECKSUM.unpack(checksum)[0] != crc32c(entries):
        raise BundleError("shard index CRC-32C mismatch")
    return [_INDEX_ENTRY.unpack_from(entries, position * _INDEX_ENTRY.size) for position in range(count)]


def shard_index_bytes(shard: bytes, count: int, index_location: str) -> bytes:
    """The index block a shard keeps at the end its array declares."""
    length = _INDEX_ENTRY.size * count + _INDEX_CHECKSUM.size
    if len(shard) < length:
        raise BundleError("shard is smaller than its index")
    return shard[:length] if index_location == "start" else shard[len(shard) - length :]


def _shard_index(shard: bytes, count: int, index_location: str) -> list[tuple[int, int]]:
    """The ``(offset, nbytes)`` pairs of a shard, CRC-32C verified."""
    return _parse_index(shard_index_bytes(shard, count, index_location), count)


def _read_shard(zarr_dir: Path, variable_id: str, time_chunk: int) -> tuple[ArrayGeometry, int, list[np.ndarray | None]]:
    """One time chunk's inner chunks back to codes at the full inner shape,
    in tile order; ``None`` for a chunk the shard never held (or a shard that
    was never written), which reads as the fill value. The shard is whichever
    the array's chunk shape puts the time chunk in, and the chunk's entries
    are the run of the shard's index at the time chunk's position."""
    geometry, delta, index_location, fill_value = _array_layout(zarr_dir, variable_id)
    if not 0 <= time_chunk < geometry.time_chunks:
        raise BundleError("time chunk is outside the array")
    shard_number, position = geometry.shard_of(time_chunk)
    shard_path = zarr_dir / variable_id / "c" / str(shard_number) / "0" / "0"
    if not shard_path.is_file():
        return geometry, fill_value, [None] * geometry.tile_count
    shard = shard_path.read_bytes()
    entries = _shard_index(shard, geometry.chunks_per_shard, index_location)[position : position + geometry.tile_count]
    blocks: list[np.ndarray | None] = []
    for offset, length in entries:
        if offset == _EMPTY_ENTRY and length == _EMPTY_ENTRY:
            blocks.append(None)
            continue
        if offset + length > len(shard):
            raise BundleError("shard index entry exceeds the shard")
        payload = shard[offset : offset + length]
        if not zstdcli.frame_has_checksum(payload):
            raise BundleError("an inner chunk must be a Zstandard frame with a content checksum")
        stored = np.frombuffer(
            zstdcli.decompress(payload, expected_length=math.prod(geometry.inner_shape)), dtype=np.uint8
        ).reshape(geometry.inner_shape)
        blocks.append(_delta_decode(stored) if delta else stored)
    return geometry, fill_value, blocks


def _trim(block: np.ndarray | None, geometry: ArrayGeometry, time_chunk: int, tile: int, fill_value: int) -> np.ndarray:
    frames = geometry.frames(time_chunk)
    height, width = geometry.tile_shape(tile)
    if block is None:
        return np.full((len(frames), height, width), fill_value, dtype=np.uint8)
    return block[: len(frames), :height, :width]


def read_array_chunk(zarr_dir: Path, variable_id: str, time_chunk: int, tile: int) -> np.ndarray:
    """One inner chunk back to codes, ``(frames, height, width)`` trimmed to
    the frames and the cells that lie inside the array."""
    geometry, fill_value, blocks = _read_shard(zarr_dir, variable_id, time_chunk)
    if not 0 <= tile < geometry.tile_count:
        raise BundleError("tile is outside the array")
    return _trim(blocks[tile], geometry, time_chunk, tile, fill_value)


def read_plane(zarr_dir: Path, variable_id: str, frame_index: int) -> np.ndarray:
    """One frame of one array as a ``(height, width)`` plane of codes."""
    geometry, _delta, _index_location, _fill_value = _array_layout(zarr_dir, variable_id)
    if not 0 <= frame_index < geometry.frame_count:
        raise BundleError("frame index is outside the array")
    time_chunk, frame_in_chunk = divmod(frame_index, geometry.time_chunk)
    geometry, fill_value, blocks = _read_shard(zarr_dir, variable_id, time_chunk)
    plane = np.empty((geometry.height, geometry.width), dtype=np.uint8)
    for tile, block in enumerate(blocks):
        row, column = geometry.tile_origin(tile)
        height, width = geometry.tile_shape(tile)
        plane[row : row + height, column : column + width] = _trim(block, geometry, time_chunk, tile, fill_value)[
            frame_in_chunk
        ]
    return plane


def read_frames(zarr_dir: Path, variable_id: str) -> list[np.ndarray]:
    """Every frame of one array as ``(height, width)`` planes of codes, in
    axis order, each inner chunk decoded once: what a reader of the whole
    axis (a window cut into segments) takes, where :func:`read_plane` would
    decode a time chunk once per frame."""
    geometry, delta, index_location, fill_value = _array_layout(zarr_dir, variable_id)
    if geometry.shard_count != 1:
        return [read_plane(zarr_dir, variable_id, frame) for frame in range(geometry.frame_count)]
    shard = (zarr_dir / variable_id / "c" / "0" / "0" / "0").read_bytes()
    entries = _shard_index(shard, geometry.chunks_per_shard, index_location)
    inner_length = math.prod(geometry.inner_shape)
    frames: list[np.ndarray] = []
    for time_chunk in range(geometry.time_chunks):
        span = geometry.frames(time_chunk)
        stack = np.empty((len(span), geometry.height, geometry.width), dtype=np.uint8)
        for tile in range(geometry.tile_count):
            offset, length = entries[time_chunk * geometry.tile_count + tile]
            block: np.ndarray | None = None
            if not (offset == _EMPTY_ENTRY and length == _EMPTY_ENTRY):
                if offset + length > len(shard):
                    raise BundleError("shard index entry exceeds the shard")
                payload = shard[offset : offset + length]
                if not zstdcli.frame_has_checksum(payload):
                    raise BundleError("an inner chunk must be a Zstandard frame with a content checksum")
                stored = np.frombuffer(
                    zstdcli.decompress(payload, expected_length=inner_length), dtype=np.uint8
                ).reshape(geometry.inner_shape)
                block = _delta_decode(stored) if delta else stored
            row, column = geometry.tile_origin(tile)
            height, width = geometry.tile_shape(tile)
            stack[:, row : row + height, column : column + width] = _trim(
                block, geometry, time_chunk, tile, fill_value
            )
        frames.extend(stack)
    return frames


def store_byte_length(zarr_dir: Path) -> int:
    """The sum of every object in a store, what the manifest's ``byteLength``
    reports."""
    return sum(path.stat().st_size for path in zarr_dir.rglob("*") if path.is_file())

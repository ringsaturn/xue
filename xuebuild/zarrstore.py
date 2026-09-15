"""A Xue bundle as a Zarr v3 store.

A container v2 bundle is, up to its index format, a sharded Zarr v3 ``uint8``
array: a chunk is one spatial tile of one temporal group, a group is one
contiguous run of chunks in row-major tile order, each chunk is one
Zstandard frame, and the temporal residual is a wrapping difference against
the previous frame inside the chunk. Zarr spells the same things as a
``sharding_indexed`` codec (one shard per time chunk, the inner chunks
concatenated in row-major order with a small offset table), a ``zstd``
codec, and — optionally — an array-to-array ``xue.delta`` codec that is the
residual arithmetic moved into the codec chain. This module writes that
store from a bundle that has already been written, so the two carry the same
codes by construction: nothing is re-quantized, and every chunk is re-encoded
from the codes the bundle decodes to. ``docs/zarr-profile.md`` is normative
for the layout.

The store is written with NumPy alone — the metadata documents, the shard
bytes, the 16-byte-per-chunk index and its CRC-32C are all assembled here —
so the build pipeline gains no dependency. zarr-python and xarray are what
the tests open the result with, and what `xuebuild.zarrcodec` registers the
delta codec for; neither is needed to produce a store.

Two choices differ from the bundle and are deliberate. The time axis is cut
on a *regular* grid of six frames, where the bundle cuts its groups inside
segments of constant step (a mixed-step axis restarts its groups at the
change of cadence), so a Zarr chunk may straddle two of the bundle's groups
and the two layouts coincide only up to the first change of step; and an
edge tile is stored at the full inner-chunk shape padded with the variable's
nodata code, where the bundle clips it to the grid. A chunk that covers the
same frames and the same unclipped tile as one of the bundle's carries
byte-identical compressed bytes under the delta chain (or the standard chain
for a RAW variable), and the export report measures how many do rather than
assuming it.

Beside the shards the store carries one object of the profile's own,
``index.bin``: every shard's index — the same bytes each shard keeps at one
end of itself — concatenated, array by array and time chunk by time chunk,
and described by the group's ``xue_index`` attribute. A sharded array pays
one dependent request per shard for the index before the first inner chunk
can be located, and a point series touches every shard once, so it paid
twice the container's requests; a reader that fetches this one object at
open holds every offset up front, the way the container's structural prefix
holds its whole index. A client that does not know the object ignores it.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import binformat, zstdcli
from .errors import BundleError, ConversionError

PROFILE_VERSION = 1
"""The ``xue_profile`` attribute a store's root group declares."""

TIME_CHUNK = 6
"""Frames per Zarr time chunk. The bundle's own group length, on a regular
grid rather than restarted at every change of step."""

ZSTD_LEVEL = zstdcli.DEFAULT_LEVEL
DELTA_CODEC = "xue.delta"
INDEX_LOCATIONS = ("start", "end")
DEFAULT_INDEX_LOCATION = "end"
"""Where a shard keeps its index. ``end`` is the Zarr default and the one
clients fetch as an HTTP suffix range without knowing the object's length —
zarrita reads only that form; ``start`` lets a reader that already has the
object length find the index with the first request."""

INDEX_OBJECT = "index.bin"
"""The whole-store index object at the store's root: every shard's index,
verbatim, in group ``variables`` order and then time-chunk order, so a
reader locates array ``a``, shard ``t`` at ``(a × timeChunks + t) ×
shardIndexBytes``. Described by the group's ``xue_index`` attribute."""
INDEX_ATTRIBUTE = "xue_index"

ENVIRONMENT_VARIABLE = "XUE_ZARR"
"""Set to ``1`` to make a build derive a store beside every bundle it
writes, the way ``build-bin --zarr`` does — for the scheduled publishes."""

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


def enabled_by_environment() -> bool:
    return os.environ.get(ENVIRONMENT_VARIABLE, "").strip() == "1"


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

    The shard (Zarr's outer chunk) is one time chunk of the *whole grid*,
    rounded up to a whole number of inner chunks along each axis because the
    sharding codec requires it — 721 rows of 52 become 728 — and the chunk
    grid is allowed to run past the array's shape. The inner chunk is the
    bundle's tile, so the inner chunks of a shard are the bundle's tiles in
    the bundle's row-major order.
    """

    frame_count: int
    height: int
    width: int
    tile_height: int
    tile_width: int
    time_chunk: int = TIME_CHUNK

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
    def shard_index_bytes(self) -> int:
        """One shard's index: a pair per inner chunk plus the CRC-32C."""
        return _INDEX_ENTRY.size * self.tile_count + _INDEX_CHECKSUM.size

    @property
    def shard_shape(self) -> tuple[int, int, int]:
        return self.time_chunk, self.tile_rows * self.tile_height, self.tile_columns * self.tile_width

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
        geometry = cls(
            frame_count=int(shape[0]),
            height=int(shape[1]),
            width=int(shape[2]),
            tile_height=int(inner[1]),
            tile_width=int(inner[2]),
            time_chunk=int(inner[0]),
        )
        if list(geometry.shard_shape) != [int(value) for value in chunk_shape]:
            raise BundleError("outer chunk shape is not one time chunk of whole tiles")
        return geometry


# -- report --------------------------------------------------------------------

@dataclass
class ExportReport:
    """What one export wrote, and how much of it the bundle already held.

    ``comparable_chunks`` are the inner chunks that cover exactly the frames
    and the unclipped tile of one of the bundle's chunks; ``identical_chunks``
    are those among them whose compressed bytes equal the bundle's. Under
    the delta chain every comparable chunk of a PREVIOUS variable is
    identical, under the standard chain every comparable chunk of a RAW
    variable; the counts say so rather than the code assuming it.
    """

    path: Path
    byte_length: int
    crc32: str
    delta: bool
    index_location: str
    arrays: dict[str, int] = field(default_factory=dict)
    chunk_count: int = 0
    comparable_chunks: int = 0
    identical_chunks: int = 0
    index_bytes: int = 0
    """The size of ``index.bin``: counted in ``byte_length``, listed under no
    array."""

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
            "comparableChunks": self.comparable_chunks,
            "identicalChunks": self.identical_chunks,
            "indexBytes": self.index_bytes,
        }


# -- writing -------------------------------------------------------------------

def _dump_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write(path: Path, data: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return len(data)


def _predictor_name(bundle: binformat.Bundle, numeric_id: int) -> str:
    """``previous`` for a variable stored as residuals, ``raw`` otherwise. A
    v2 file says so per variable; a v1 file says so per plane, and a variable
    with any predicted plane counts as predicted."""
    if bundle.container_version == binformat.VERSION_V2:
        predictor = bundle.predictors[numeric_id]
    else:
        predicted = {binformat.PREDICTOR_ANCHOR, binformat.PREDICTOR_PREVIOUS}
        predictor = (
            binformat.PREDICTOR_PREVIOUS
            if any(entry.predictor in predicted for entry in bundle.entries if entry.variable_id == numeric_id)
            else binformat.PREDICTOR_RAW
        )
    return "previous" if predictor == binformat.PREDICTOR_PREVIOUS else "raw"


def _bundle_tile(bundle: binformat.Bundle, tile: tuple[int, int] | None) -> tuple[int, int]:
    """The (width, height) a bundle is cut with. A v2 file carries its own;
    a v1 file has no tiles, so the caller names one or the source's is
    looked up by the model the metadata declares and clamped to the grid."""
    if bundle.container_version == binformat.VERSION_V2:
        return bundle.tiles.tile_width, bundle.tiles.tile_height
    if tile is None:
        from .sources import SOURCES  # noqa: PLC0415  (only the v1 fallback needs the registry)

        model = bundle.metadata.get("model")
        source = next((source for source in SOURCES.values() if source.manifest_model == model), None)
        if source is None:
            raise ConversionError(f"a v1 bundle of an unregistered model ({model!r}) needs an explicit tile")
        tile = source.tile
    return min(tile[0], bundle.width), min(tile[1], bundle.height)


def _tile_blocks(
    bundle: binformat.Bundle, numeric_id: int, geometry: ArrayGeometry, frames: range
) -> list[np.ndarray]:
    """One time chunk's codes, tile by tile in row-major order, each block
    ``(len(frames), clipped height, clipped width)``.

    A v2 file is read chunk by chunk: the time chunk's frames come from one
    or two of the bundle's groups (two across a change of step), sliced to
    the frames wanted. A v1 file is read plane by plane and cut into tiles
    here. Either way the bundle's caches are dropped afterwards so a pass
    over a run never holds more than one time chunk decoded.
    """
    if bundle.container_version == binformat.VERSION_V2:
        pieces: list[tuple[int, int, int]] = []
        for frame in frames:
            group_index, _ = bundle.frame_group[frame]
            if not pieces or pieces[-1][0] != group_index:
                pieces.append((group_index, frame, frame + 1))
            else:
                pieces[-1] = (group_index, pieces[-1][1], frame + 1)
        blocks = []
        for tile in range(geometry.tile_count):
            parts = []
            for group_index, first, stop in pieces:
                chunk = bundle.decode_chunk(bundle.chunk_position(group_index, tile, numeric_id))
                origin = bundle.groups[group_index].first_frame
                parts.append(chunk[first - origin : stop - origin])
            blocks.append(parts[0] if len(parts) == 1 else np.concatenate(parts))
    else:
        stack = np.stack(
            [
                bundle.decode_plane(numeric_id, bundle.frame_offsets[frame]).reshape(geometry.height, geometry.width)
                for frame in frames
            ]
        )
        blocks = []
        for tile in range(geometry.tile_count):
            row, column = geometry.tile_origin(tile)
            height, width = geometry.tile_shape(tile)
            blocks.append(stack[:, row : row + height, column : column + width])
    bundle.clear_cache()
    return blocks


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


def _pack_shard(payloads: list[bytes], index_location: str) -> tuple[bytes, bytes]:
    """One shard: the inner chunks back to back, plus the index — returned
    beside the shard as well, since ``index.bin`` is those same bytes.

    The index is one ``(offset, nbytes)`` pair of little-endian ``uint64``
    per inner chunk in row-major inner-chunk order — the bundle's tile order
    — followed by the CRC-32C of those pairs. Offsets are from the start of
    the shard, so with the index at the start they begin after it.
    """
    index_length = _INDEX_ENTRY.size * len(payloads) + _INDEX_CHECKSUM.size
    cursor = index_length if index_location == "start" else 0
    entries = bytearray()
    for payload in payloads:
        entries += _INDEX_ENTRY.pack(cursor, len(payload))
        cursor += len(payload)
    index = bytes(entries) + _INDEX_CHECKSUM.pack(crc32c(bytes(entries)))
    body = b"".join(payloads)
    return (index + body if index_location == "start" else body + index), index


def _index_attribute(geometry: ArrayGeometry, arrays: list[str]) -> dict[str, Any]:
    """The group's ``xue_index`` block: where ``index.bin`` is and how it is
    cut. Every array of a store shares one geometry, so one block size and
    one time-chunk count describe them all."""
    return {
        "path": INDEX_OBJECT,
        "shardIndexBytes": geometry.shard_index_bytes,
        "timeChunks": geometry.time_chunks,
        "arrays": list(arrays),
    }


def _array_metadata(
    geometry: ArrayGeometry, variable: dict[str, Any], predictor: str, *, delta: bool, index_location: str
) -> dict[str, Any]:
    quantization = variable["quantization"]
    attributes: dict[str, Any] = {"xue": {"variable": variable, "predictor": predictor}}
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
        "dimension_names": list(_DIMENSIONS),
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


def _write_coordinates(bundle: binformat.Bundle, zarr_dir: Path) -> dict[str, int]:
    """The three one-dimensional coordinate arrays: for xarray and its kind,
    never read by the viewer, which reads the root group's ``xue`` block."""
    grid = bundle.metadata["grid"]
    run_time = bundle.metadata["runTime"]
    unit_seconds = bundle.unit_seconds
    time_values = np.array([offset * unit_seconds for offset in bundle.frame_offsets], dtype="<i4")
    latitude = grid["firstLatitude"] + np.arange(bundle.height, dtype=np.float64) * grid["latitudeStep"]
    longitude = grid["firstLongitude"] + np.arange(bundle.width, dtype=np.float64) * grid["longitudeStep"]
    coordinates = {
        "time": (
            time_values,
            "int32",
            0,
            {"units": f"seconds since {run_time}", "calendar": "proleptic_gregorian", "standard_name": "time"},
        ),
        "latitude": (
            latitude.astype("<f8"),
            "float64",
            "NaN",
            {"units": "degrees_north", "standard_name": "latitude"},
        ),
        "longitude": (
            longitude.astype("<f8"),
            "float64",
            "NaN",
            {"units": "degrees_east", "standard_name": "longitude"},
        ),
    }
    sizes = {}
    for name, (values, data_type, fill_value, attributes) in coordinates.items():
        directory = zarr_dir / name
        sizes[name] = _write(
            directory / "zarr.json",
            _dump_json(_coordinate_metadata(len(values), data_type, fill_value, attributes, name)),
        ) + _write(directory / "c" / "0", values.tobytes())
    return sizes


def _comparable_chunk(
    bundle: binformat.Bundle, geometry: ArrayGeometry, time_chunk: int, tile: int, numeric_id: int
) -> int | None:
    """The bundle chunk a Zarr inner chunk covers exactly, if there is one:
    a group starting on the same frame with the same length, on a tile the
    grid does not clip. Only a v2 file has chunks to compare against."""
    if bundle.container_version != binformat.VERSION_V2:
        return None
    if geometry.tile_shape(tile) != (geometry.tile_height, geometry.tile_width):
        return None
    first = time_chunk * geometry.time_chunk
    for group_index, group in enumerate(bundle.groups):
        if group.first_frame == first:
            if group.frame_count == geometry.time_chunk:
                return bundle.chunk_position(group_index, tile, numeric_id)
            return None
        if group.first_frame > first:
            return None
    return None


def _write_array(
    bundle: binformat.Bundle,
    numeric_id: int,
    variable: dict[str, Any],
    geometry: ArrayGeometry,
    array_dir: Path,
    *,
    delta: bool,
    index_location: str,
    executor: ThreadPoolExecutor,
) -> tuple[int, int, int, int, list[bytes]]:
    """One variable's array: its metadata and one shard per time chunk.
    Returns bytes written, inner chunks written, comparable and identical
    chunks (see :class:`ExportReport`), and each shard's index bytes in
    time-chunk order, for ``index.bin``."""
    predictor = _predictor_name(bundle, numeric_id)
    use_delta = delta and predictor == "previous"
    nodata = variable["quantization"]["nodataCode"]
    written = _write(
        array_dir / "zarr.json",
        _dump_json(_array_metadata(geometry, variable, predictor, delta=use_delta, index_location=index_location)),
    )
    chunks = comparable = identical = 0
    indexes: list[bytes] = []
    for time_chunk in range(geometry.time_chunks):
        blocks = _tile_blocks(bundle, numeric_id, geometry, geometry.frames(time_chunk))
        stored = [_pad_block(block, geometry, nodata) for block in blocks]
        if use_delta:
            stored = [_delta_encode(block) for block in stored]
        payloads = list(
            executor.map(
                lambda block: zstdcli.compress(block.tobytes(), level=ZSTD_LEVEL, checksum=True), stored
            )
        )
        for tile, payload in enumerate(payloads):
            position = _comparable_chunk(bundle, geometry, time_chunk, tile, numeric_id)
            if position is None:
                continue
            comparable += 1
            start, end = bundle.chunk_span(position)
            identical += bundle.data[start:end] == payload
        shard, index = _pack_shard(payloads, index_location)
        if len(index) != geometry.shard_index_bytes:
            raise AssertionError("shard index length disagrees with the geometry")
        written += _write(array_dir / "c" / str(time_chunk) / "0" / "0", shard)
        indexes.append(index)
        chunks += len(payloads)
    return written, chunks, comparable, identical, indexes


def export_bundle(
    xue_path: Path,
    zarr_dir: Path,
    *,
    delta: bool = False,
    index_location: str = DEFAULT_INDEX_LOCATION,
    tile: tuple[int, int] | None = None,
    executor: ThreadPoolExecutor | None = None,
) -> ExportReport:
    """Derive the Zarr v3 store of one bundle.

    ``zarr_dir`` is the store's root (``<bundle>.zarr``) and is replaced
    whole. ``delta`` puts the ``xue.delta`` codec in front of ``bytes`` on
    every PREVIOUS variable — never on a RAW one — so the standard chain
    stays the default any Zarr client reads without registering anything.
    ``tile`` is only for a v1 bundle, which carries no tiling of its own.
    ``executor`` bounds the compression a build shares between bundles.
    """
    if index_location not in INDEX_LOCATIONS:
        raise ConversionError(f"index location must be one of {', '.join(INDEX_LOCATIONS)}, not {index_location!r}")
    bundle = binformat.read_bundle(xue_path)
    tile_width, tile_height = _bundle_tile(bundle, tile)
    geometry = ArrayGeometry(
        frame_count=bundle.frame_count,
        height=bundle.height,
        width=bundle.width,
        tile_height=tile_height,
        tile_width=tile_width,
    )
    variables = {variable["numericId"]: variable for variable in bundle.metadata["variables"]}
    # The arrays in the order the group's `variables` lists them — the order
    # `index.bin` concatenates their shard indexes in.
    array_names = [variable["id"] for variable in bundle.metadata["variables"]]
    if sorted(array_names) != sorted(bundle.variable_ids.values()):
        raise BundleError("the metadata's variables do not name the bundle's arrays")

    if zarr_dir.exists():
        if not zarr_dir.is_dir():
            raise ConversionError(f"{zarr_dir} exists and is not a directory")
        shutil.rmtree(zarr_dir)
    zarr_dir.mkdir(parents=True)

    own_executor = executor is None
    pool = executor or ThreadPoolExecutor(max_workers=os.cpu_count() or 4)
    try:
        # The root group first: its `xue` block is the bundle's metadata
        # verbatim, which is what the viewer's parsers already read.
        root = _dump_json(
            {
                "zarr_format": 3,
                "node_type": "group",
                "attributes": {
                    "xue": bundle.metadata,
                    "xue_profile": PROFILE_VERSION,
                    INDEX_ATTRIBUTE: _index_attribute(geometry, array_names),
                },
            }
        )
        report = ExportReport(
            path=zarr_dir,
            byte_length=_write(zarr_dir / "zarr.json", root),
            crc32=f"{binformat.crc32_plane(root):08x}",
            delta=delta,
            index_location=index_location,
        )
        name_to_numeric = {name: numeric_id for numeric_id, name in bundle.variable_ids.items()}
        index_blocks: list[bytes] = []
        for name in array_names:
            numeric_id = name_to_numeric[name]
            written, chunks, comparable, identical, indexes = _write_array(
                bundle,
                numeric_id,
                variables[numeric_id],
                geometry,
                zarr_dir / name,
                delta=delta,
                index_location=index_location,
                executor=pool,
            )
            report.arrays[name] = written
            report.byte_length += written
            report.chunk_count += chunks
            report.comparable_chunks += comparable
            report.identical_chunks += identical
            if len(indexes) != geometry.time_chunks:
                raise AssertionError("an array wrote a different number of shards than the geometry has time chunks")
            index_blocks.extend(indexes)
        for name, written in _write_coordinates(bundle, zarr_dir).items():
            report.arrays[name] = written
            report.byte_length += written
        # The whole-store index last: the shard indexes verbatim, back to
        # back, in the order the `xue_index` attribute describes.
        report.index_bytes = _write(zarr_dir / INDEX_OBJECT, b"".join(index_blocks))
        report.byte_length += report.index_bytes
    finally:
        if own_executor:
            pool.shutdown()
    return report


def store_path_for(xue_path: Path) -> Path:
    """``tmp2m.xue`` → ``tmp2m.zarr``, ``tmp2m.half.xue`` → ``tmp2m.half.zarr``."""
    return xue_path.with_suffix(".zarr")


# -- reading (NumPy only, for tests and inspection) -----------------------------

def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BundleError(f"{path} is not a JSON object")
    return payload


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
    end of a shard, or the same bytes in ``index.bin`` — CRC-32C verified."""
    if len(index) != _INDEX_ENTRY.size * count + _INDEX_CHECKSUM.size:
        raise BundleError("shard index is not one pair per inner chunk plus its checksum")
    entries, checksum = index[: -_INDEX_CHECKSUM.size], index[-_INDEX_CHECKSUM.size :]
    if _INDEX_CHECKSUM.unpack(checksum)[0] != crc32c(entries):
        raise BundleError("shard index CRC-32C mismatch")
    return [_INDEX_ENTRY.unpack_from(entries, position * _INDEX_ENTRY.size) for position in range(count)]


def shard_index_bytes(shard: bytes, count: int, index_location: str) -> bytes:
    """The index block a shard keeps at the end its array declares — the
    bytes ``index.bin`` repeats for it."""
    length = _INDEX_ENTRY.size * count + _INDEX_CHECKSUM.size
    if len(shard) < length:
        raise BundleError("shard is smaller than its index")
    return shard[:length] if index_location == "start" else shard[len(shard) - length :]


def _shard_index(shard: bytes, count: int, index_location: str) -> list[tuple[int, int]]:
    """The ``(offset, nbytes)`` pairs of a shard, CRC-32C verified."""
    return _parse_index(shard_index_bytes(shard, count, index_location), count)


@dataclass(frozen=True)
class StoreIndex:
    """``index.bin`` as the group's ``xue_index`` attribute describes it:
    one block of ``shard_index_bytes`` per (array, time chunk), arrays in
    the attribute's order."""

    shard_index_bytes: int
    time_chunks: int
    arrays: tuple[str, ...]
    data: bytes

    def block(self, array: str, time_chunk: int) -> bytes:
        """The verbatim shard index of one shard, unverified."""
        if array not in self.arrays:
            raise BundleError(f"the store index does not cover array {array!r}")
        if not 0 <= time_chunk < self.time_chunks:
            raise BundleError("time chunk is outside the store index")
        start = (self.arrays.index(array) * self.time_chunks + time_chunk) * self.shard_index_bytes
        return self.data[start : start + self.shard_index_bytes]

    def entries(self, array: str, time_chunk: int, tile_count: int) -> list[tuple[int, int]]:
        """One shard's ``(offset, nbytes)`` pairs, CRC-32C verified."""
        return _parse_index(self.block(array, time_chunk), tile_count)


def read_store_index(zarr_dir: Path) -> StoreIndex:
    """``index.bin`` and the attribute describing it, held to each other: the
    attribute's fields well-formed and the object exactly as long as they
    say. Blocks are verified as they are read, not here."""
    attributes = read_json(zarr_dir / "zarr.json").get("attributes", {})
    described = attributes.get(INDEX_ATTRIBUTE) if isinstance(attributes, dict) else None
    if not isinstance(described, dict):
        raise BundleError("the group declares no whole-store index")
    path, block, time_chunks, arrays = (
        described.get("path"),
        described.get("shardIndexBytes"),
        described.get("timeChunks"),
        described.get("arrays"),
    )
    if (
        not isinstance(path, str)
        or not path
        or "/" in path
        or not isinstance(block, int)
        or block <= _INDEX_CHECKSUM.size
        or (block - _INDEX_CHECKSUM.size) % _INDEX_ENTRY.size
        or not isinstance(time_chunks, int)
        or time_chunks <= 0
        or not isinstance(arrays, list)
        or not arrays
        or not all(isinstance(name, str) and name for name in arrays)
        or len(set(arrays)) != len(arrays)
    ):
        raise BundleError("malformed xue_index attribute")
    try:
        data = (zarr_dir / path).read_bytes()
    except OSError as exc:
        raise BundleError(f"cannot read the store index: {exc}") from exc
    if len(data) != len(arrays) * time_chunks * block:
        raise BundleError("the store index is not one block per array and time chunk")
    return StoreIndex(block, time_chunks, tuple(arrays), data)


def _read_shard(
    zarr_dir: Path, variable_id: str, time_chunk: int, *, through_index: bool = False
) -> tuple[ArrayGeometry, int, list[np.ndarray | None]]:
    """One shard's inner chunks back to codes at the full inner shape, in
    tile order; ``None`` for a chunk the shard never held (or a shard that
    was never written), which reads as the fill value. With
    ``through_index`` the offsets come from ``index.bin`` and the shard's own
    index is never looked at."""
    geometry, delta, index_location, fill_value = _array_layout(zarr_dir, variable_id)
    if not 0 <= time_chunk < geometry.time_chunks:
        raise BundleError("time chunk is outside the array")
    shard_path = zarr_dir / variable_id / "c" / str(time_chunk) / "0" / "0"
    if not shard_path.is_file():
        return geometry, fill_value, [None] * geometry.tile_count
    shard = shard_path.read_bytes()
    if through_index:
        store_index = read_store_index(zarr_dir)
        if store_index.shard_index_bytes != geometry.shard_index_bytes or store_index.time_chunks != geometry.time_chunks:
            raise BundleError("the store index does not describe this array's geometry")
        entries = store_index.entries(variable_id, time_chunk, geometry.tile_count)
    else:
        entries = _shard_index(shard, geometry.tile_count, index_location)
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


def read_array_chunk(
    zarr_dir: Path, variable_id: str, time_chunk: int, tile: int, *, through_index: bool = False
) -> np.ndarray:
    """One inner chunk back to codes, ``(frames, height, width)`` trimmed to
    the frames and the cells that lie inside the array. ``through_index``
    locates it by ``index.bin`` instead of the shard's own index."""
    geometry, fill_value, blocks = _read_shard(zarr_dir, variable_id, time_chunk, through_index=through_index)
    if not 0 <= tile < geometry.tile_count:
        raise BundleError("tile is outside the array")
    return _trim(blocks[tile], geometry, time_chunk, tile, fill_value)


def read_plane(zarr_dir: Path, variable_id: str, frame_index: int, *, through_index: bool = False) -> np.ndarray:
    """One frame of one array as a ``(height, width)`` plane of codes."""
    geometry, _delta, _index_location, _fill_value = _array_layout(zarr_dir, variable_id)
    if not 0 <= frame_index < geometry.frame_count:
        raise BundleError("frame index is outside the array")
    time_chunk, frame_in_chunk = divmod(frame_index, geometry.time_chunk)
    geometry, fill_value, blocks = _read_shard(zarr_dir, variable_id, time_chunk, through_index=through_index)
    plane = np.empty((geometry.height, geometry.width), dtype=np.uint8)
    for tile, block in enumerate(blocks):
        row, column = geometry.tile_origin(tile)
        height, width = geometry.tile_shape(tile)
        plane[row : row + height, column : column + width] = _trim(block, geometry, time_chunk, tile, fill_value)[
            frame_in_chunk
        ]
    return plane


def store_byte_length(zarr_dir: Path) -> int:
    """The sum of every object in a store, what the manifest's ``byteLength``
    reports."""
    return sum(path.stat().st_size for path in zarr_dir.rglob("*") if path.is_file())

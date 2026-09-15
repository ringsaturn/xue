"""A Xue bundle as a Zarr v3 store.

A container v2 bundle is, up to its index format, a sharded Zarr v3 ``uint8``
array: a chunk is one spatial tile of one temporal group, a group is one
contiguous run of chunks in row-major tile order, each chunk is one
Zstandard frame, and the temporal residual is a wrapping difference against
the previous frame inside the chunk. Zarr spells the same things as a
``sharding_indexed`` codec (one shard per array — the whole time axis of the
whole grid — its inner chunks concatenated time chunk by time chunk in
row-major tile order with one offset table), a ``zstd`` codec, and —
optionally — an array-to-array ``xue.delta`` codec that is the
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

One shard per array, rather than one per time chunk, is what keeps a store
at a handful of objects: a bucket bills every object written, and a store
cut every six frames costs a GFS run some five thousand objects where the
container cost sixty. A shard the size of the array is what the container
already was — one object, read by range — and its index, one pair per inner
chunk over the whole axis, is the container's whole index too: a reader
fetches it once at open (a suffix range, no object length needed) and holds
every offset, so a point series costs one request per time chunk and a
frame one per tile row, as they did in the container.
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


CONTAINER_VARIABLE = "XUE_CONTAINER"


def container_enabled_by_environment() -> bool:
    """Whether a build keeps the ``.xue`` container it wrote: ``XUE_CONTAINER=0``
    retires it — the store derived from it is the only delivery published,
    the manifest names no ``path``, and the file is removed once the store
    and the video companions have been read out of it. The transitional
    form of plan 017's phase 7, taken one source at a time; anything else,
    the variable unset included, keeps the container."""
    return os.environ.get(CONTAINER_VARIABLE, "").strip() != "0"


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
) -> tuple[int, int, int, int]:
    """One variable's array: its metadata and its one shard. Returns bytes
    written, inner chunks written, comparable and identical chunks (see
    :class:`ExportReport`)."""
    predictor = _predictor_name(bundle, numeric_id)
    use_delta = delta and predictor == "previous"
    nodata = variable["quantization"]["nodataCode"]
    written = _write(
        array_dir / "zarr.json",
        _dump_json(_array_metadata(geometry, variable, predictor, delta=use_delta, index_location=index_location)),
    )
    if geometry.shard_count != 1:
        raise AssertionError("the exporter writes one shard per array")
    chunks = comparable = identical = 0
    shard_payloads: list[bytes] = []
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
        shard_payloads.extend(payloads)
        chunks += len(payloads)
    # The axis rounded up to whole time chunks: the index names every inner
    # chunk of the shard's shape, and a time chunk past the axis is padding
    # the exporter still stores (at the fill value, a few bytes each).
    nodata_block = zstdcli.compress(
        np.full(geometry.inner_shape, nodata, dtype=np.uint8).tobytes(), level=ZSTD_LEVEL, checksum=True
    )
    shard_payloads.extend([nodata_block] * (geometry.chunks_per_shard - len(shard_payloads)))
    shard = _pack_shard(shard_payloads, index_location)
    if len(shard_payloads) != geometry.chunks_per_shard:
        raise AssertionError("shard payload count disagrees with the geometry")
    written += _write(array_dir / "c" / "0" / "0" / "0", shard)
    return written, chunks, comparable, identical


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
    # The arrays in the order the group's `variables` lists them.
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
                "attributes": {"xue": bundle.metadata, "xue_profile": PROFILE_VERSION},
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
        for name in array_names:
            numeric_id = name_to_numeric[name]
            written, chunks, comparable, identical = _write_array(
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
        for name, written in _write_coordinates(bundle, zarr_dir).items():
            report.arrays[name] = written
            report.byte_length += written
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


def store_byte_length(zarr_dir: Path) -> int:
    """The sum of every object in a store, what the manifest's ``byteLength``
    reports."""
    return sum(path.stat().st_size for path in zarr_dir.rglob("*") if path.is_file())

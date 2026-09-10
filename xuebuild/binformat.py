"""Xue container serialization, validation, and reference decoding.

This module is the Python source of truth for the binary layout defined in
docs/format.md. The writer produces complete files, and ``Bundle`` is the
reference decoder used by ``verify-bin`` and by cross-language golden tests
against the Rust implementation.

Two container versions live here. **v1** is plane-major: one payload is one
whole plane of one frame. **v2** is what the encoder writes now: a payload is
a *chunk* — one spatial tile of one temporal group for one variable — laid
out group by group, so a reader can fetch only the tiles a viewport covers
and read one cell's series by fetching one chunk per group. The metadata JSON,
the codebooks and the modulo-256 residual arithmetic are shared; only the
payload's granularity and the index describing it differ. Both versions are
readable, and only v2 is written.
"""

from __future__ import annotations

import json
import math
import os
import struct
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from . import zstdcli
from .errors import BundleError

MAGIC = b"XUE\0\0\0\0\0"
VERSION = 1
"""The plane-major container. Still read; no longer written."""
VERSION_V2 = 2
"""The tiled container: a payload is a chunk, not a plane."""
CONTAINER_VERSIONS = (VERSION, VERSION_V2)
HEADER_SIZE = 80
INDEX_MAGIC = b"IDX1"
INDEX_HEADER_SIZE = 16
ENTRY_SIZE = 40
INDEX_VERSION = 1
NO_DEPENDENCY = 0xFFFF

INDEX_MAGIC_V2 = b"IDX2"
INDEX_HEADER_SIZE_V2 = 32
INDEX_VERSION_V2 = 2
VARIABLE_ENTRY_SIZE = 4
GROUP_ENTRY_SIZE = 4
CHUNK_ENTRY_SIZE = 8
MAX_GROUP_LENGTH = 255
"""A group's frameCount is a u8, so a group holds at most 255 frames."""

PREDICTOR_RAW = 0
PREDICTOR_ANCHOR = 1
PREDICTOR_PREVIOUS = 2
PREDICTOR_ZERO = 3
PREDICTORS = {PREDICTOR_RAW, PREDICTOR_ANCHOR, PREDICTOR_PREVIOUS, PREDICTOR_ZERO}
PREDICTORS_V2 = {PREDICTOR_RAW, PREDICTOR_PREVIOUS}
"""A v2 chunk is decompressed whole, so an anchor buys no random access and
ANCHOR/ZERO are not valid: one chunk has exactly one encoding."""

COMPRESSION_NONE = 0
COMPRESSION_ZSTD = 1
COMPRESSION_ZSTD_DICT = 2
COMPRESSIONS = {COMPRESSION_NONE, COMPRESSION_ZSTD, COMPRESSION_ZSTD_DICT}

FLAG_ZSTD_CHECKSUM = 0x01

HOUR_SECONDS = 3600
"""The coarsest time-axis unit, and the only one schema versions 1 and 2 can
describe. A schemaVersion 3 axis names its own unit, which must divide it."""

# The schemaVersion 3 time block. Versions 1 and 2 use firstForecastHour with
# stepHours or hours instead; mixing the two shapes is invalid.
_V3_TIME_FIELDS = frozenset({"unitSeconds", "firstFrameOffset", "frameStep", "frameOffsets"})

# Schema v3 variable identity: the GRIB2 parameter triple and fixed surface
# every variable declares, plus the optional statistical process (code table
# 4.10) a derived field carries.
_PARAMETER_FIELDS = frozenset(
    {"discipline", "parameterCategory", "parameterNumber", "typeOfFirstFixedSurface"}
)
# Present but nullable: a fixed surface with no value writes both halves as
# null the way GRIB2 writes them missing, and only a derived field carries a
# statistical process at all.
_PARAMETER_NULLABLE_FIELDS = frozenset(
    {
        "scaleFactorOfFirstFixedSurface",
        "scaledValueOfFirstFixedSurface",
        "typeOfStatisticalProcessing",
    }
)

_HEADER_STRUCT = struct.Struct("<8sHHIQQQQQQQQ")
_INDEX_HEADER_STRUCT = struct.Struct("<4sHHII")
_ENTRY_STRUCT = struct.Struct("<BBBBHHHHIQIIBB6s")

_INDEX_HEADER_V2_STRUCT = struct.Struct("<4sHHHHHBBIIQ")
_VARIABLE_ENTRY_STRUCT = struct.Struct("<BBH")
_GROUP_ENTRY_STRUCT = struct.Struct("<HBB")
_CHUNK_ENTRY_STRUCT = struct.Struct("<II")


def align8(value: int) -> int:
    return (value + 7) // 8 * 8


def crc32_plane(plane: bytes | np.ndarray) -> int:
    return zlib.crc32(bytes(plane)) & 0xFFFFFFFF


@dataclass(frozen=True)
class TileGeometry:
    """How a grid is cut into tiles: derived arithmetic, never stored.

    Tiles start at the grid's first cell and are laid out row-major; the last
    column and the last row are clipped to the grid, so a tile size need not
    divide the grid (721 rows have no tidy power of two). A tile is cells,
    never degrees — its geographic footprint follows from the metadata grid,
    and the horizontal wrap of a global grid belongs to the grid, not to any
    tile.
    """

    width: int
    height: int
    tile_width: int
    tile_height: int

    def __post_init__(self) -> None:
        if not 1 <= self.tile_width <= self.width or not 1 <= self.tile_height <= self.height:
            raise BundleError("tile size must be between 1 and the grid dimensions")

    @property
    def columns(self) -> int:
        return (self.width + self.tile_width - 1) // self.tile_width

    @property
    def rows(self) -> int:
        return (self.height + self.tile_height - 1) // self.tile_height

    @property
    def count(self) -> int:
        return self.columns * self.rows

    def origin(self, tile: int) -> tuple[int, int]:
        """The (row, column) of a tile's north-west cell in the grid."""
        return (tile // self.columns) * self.tile_height, (tile % self.columns) * self.tile_width

    def shape(self, tile: int) -> tuple[int, int]:
        """The clipped (height, width) of a tile in cells."""
        row, column = self.origin(tile)
        return min(self.tile_height, self.height - row), min(self.tile_width, self.width - column)

    def tile_of(self, row: int, column: int) -> int:
        """The tile containing a grid cell."""
        if not 0 <= row < self.height or not 0 <= column < self.width:
            raise BundleError("cell is outside the grid")
        return (row // self.tile_height) * self.columns + column // self.tile_width

    def tiles_in_rect(self, row: int, column: int, height: int, width: int) -> list[int]:
        """Every tile a grid rectangle touches, in row-major order."""
        if height <= 0 or width <= 0:
            return []
        first_row, first_column = row // self.tile_height, column // self.tile_width
        last_row = (row + height - 1) // self.tile_height
        last_column = (column + width - 1) // self.tile_width
        return [
            tile_row * self.columns + tile_column
            for tile_row in range(first_row, last_row + 1)
            for tile_column in range(first_column, last_column + 1)
        ]


@dataclass(frozen=True)
class VariableEntry:
    """One v2 variable: its id and the predictor every one of its chunks uses."""

    variable_id: int
    predictor: int

    def pack(self) -> bytes:
        return _VARIABLE_ENTRY_STRUCT.pack(self.variable_id, self.predictor, 0)

    @classmethod
    def unpack(cls, data: bytes) -> "VariableEntry":
        variable_id, predictor, reserved = _VARIABLE_ENTRY_STRUCT.unpack(data)
        if reserved != 0:
            raise BundleError("variable entry reserved must be 0")
        return cls(variable_id=variable_id, predictor=predictor)


@dataclass(frozen=True)
class GroupEntry:
    """One temporal group, as a run of frame indices on the shared axis."""

    first_frame: int
    frame_count: int

    def pack(self) -> bytes:
        return _GROUP_ENTRY_STRUCT.pack(self.first_frame, self.frame_count, 0)

    @classmethod
    def unpack(cls, data: bytes) -> "GroupEntry":
        first_frame, frame_count, reserved = _GROUP_ENTRY_STRUCT.unpack(data)
        if reserved != 0:
            raise BundleError("group entry reserved must be 0")
        return cls(first_frame=first_frame, frame_count=frame_count)


@dataclass(frozen=True)
class ChunkEntry:
    """One chunk's compressed length and the CRC32 of what it reconstructs to.

    A chunk's *offset* is not stored: the physical order is fixed by the spec
    and chunks are strictly adjacent, so an offset is the prefix sum of the
    lengths before it.
    """

    compressed_length: int
    crc32: int

    def pack(self) -> bytes:
        return _CHUNK_ENTRY_STRUCT.pack(self.compressed_length, self.crc32)

    @classmethod
    def unpack(cls, data: bytes) -> "ChunkEntry":
        compressed_length, checksum = _CHUNK_ENTRY_STRUCT.unpack(data)
        return cls(compressed_length=compressed_length, crc32=checksum)


@dataclass(frozen=True)
class ChunkPayload:
    """One chunk in physical file order, before offsets are assigned."""

    entry: ChunkEntry
    payload: bytes


@dataclass(frozen=True)
class PlaneEntry:
    variable_id: int
    predictor: int
    compression: int
    flags: int
    frame_offset: int
    dependency_offset: int
    group_id: int
    compressed_length: int
    data_offset: int
    decoded_length: int
    crc32: int
    minimum_code: int
    maximum_code: int

    def pack(self) -> bytes:
        return _ENTRY_STRUCT.pack(
            self.variable_id,
            self.predictor,
            self.compression,
            self.flags,
            self.frame_offset,
            self.dependency_offset,
            self.group_id,
            0,
            self.compressed_length,
            self.data_offset,
            self.decoded_length,
            self.crc32,
            self.minimum_code,
            self.maximum_code,
            b"\x00" * 6,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "PlaneEntry":
        fields = _ENTRY_STRUCT.unpack(data)
        if fields[7] != 0:
            raise BundleError("index entry reserved0 must be 0")
        if fields[14] != b"\x00" * 6:
            raise BundleError("index entry reserved1 must be zero bytes")
        return cls(
            variable_id=fields[0],
            predictor=fields[1],
            compression=fields[2],
            flags=fields[3],
            frame_offset=fields[4],
            dependency_offset=fields[5],
            group_id=fields[6],
            compressed_length=fields[8],
            data_offset=fields[9],
            decoded_length=fields[10],
            crc32=fields[11],
            minimum_code=fields[12],
            maximum_code=fields[13],
        )


@dataclass(frozen=True)
class PlanePayload:
    """One payload in physical file order, before offsets are assigned."""

    entry: PlaneEntry
    payload: bytes


def write_bundle(
    path: Path,
    metadata: dict[str, Any],
    planes: list[PlanePayload],
    *,
    dictionary: bytes = b"",
) -> None:
    """Assemble a complete Xue v1 file and publish it atomically."""
    metadata_bytes = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    metadata_offset = HEADER_SIZE
    index_offset = align8(metadata_offset + len(metadata_bytes))
    index_length = INDEX_HEADER_SIZE + ENTRY_SIZE * len(planes)
    if dictionary:
        dictionary_offset = align8(index_offset + index_length)
        data_offset = align8(dictionary_offset + len(dictionary))
    else:
        dictionary_offset = 0
        data_offset = align8(index_offset + index_length)

    entries: list[PlaneEntry] = []
    cursor = data_offset
    for plane in planes:
        if plane.entry.compressed_length != len(plane.payload):
            raise BundleError("entry compressedLength does not match payload")
        entries.append(replace(plane.entry, data_offset=cursor))
        cursor += len(plane.payload)
    file_size = align8(cursor)

    entries_sorted = sorted(entries, key=lambda entry: (entry.variable_id, entry.frame_offset))
    if len({(entry.variable_id, entry.frame_offset) for entry in entries_sorted}) != len(entries_sorted):
        raise BundleError("duplicate (variableId, frameOffset) entries")

    header = _HEADER_STRUCT.pack(
        MAGIC,
        VERSION,
        HEADER_SIZE,
        0,
        file_size,
        metadata_offset,
        len(metadata_bytes),
        index_offset,
        index_length,
        data_offset,
        dictionary_offset,
        len(dictionary),
    )

    output = bytearray(file_size)
    output[0:HEADER_SIZE] = header
    output[metadata_offset : metadata_offset + len(metadata_bytes)] = metadata_bytes
    output[index_offset : index_offset + INDEX_HEADER_SIZE] = _INDEX_HEADER_STRUCT.pack(
        INDEX_MAGIC, ENTRY_SIZE, INDEX_VERSION, len(entries_sorted), 0
    )
    for position, entry in enumerate(entries_sorted):
        start = index_offset + INDEX_HEADER_SIZE + position * ENTRY_SIZE
        output[start : start + ENTRY_SIZE] = entry.pack()
    if dictionary:
        output[dictionary_offset : dictionary_offset + len(dictionary)] = dictionary
    cursor = data_offset
    for plane in planes:
        output[cursor : cursor + len(plane.payload)] = plane.payload
        cursor += len(plane.payload)

    _publish(path, output)


def _publish(path: Path, output: bytes | bytearray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(output)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def write_bundle_v2(
    path: Path,
    metadata: dict[str, Any],
    *,
    tile_width: int,
    tile_height: int,
    variables: list[VariableEntry],
    groups: list[GroupEntry],
    chunks: list[ChunkPayload],
    compression: int = COMPRESSION_ZSTD,
    dictionary: bytes = b"",
) -> None:
    """Assemble a complete Xue v2 file and publish it atomically.

    ``chunks`` is already in physical order — group, then tile (row-major),
    then variable (ascending id) — because that order is what makes a group
    one contiguous range and a viewport's tiles contiguous per tile row. The
    writer only checks that the caller produced the right number of them.
    """
    metadata_bytes = json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    grid = metadata["grid"]
    tiles = TileGeometry(grid["width"], grid["height"], tile_width, tile_height)
    frame_count = metadata["time"]["frameCount"]
    if len(variables) != len({variable.variable_id for variable in variables}):
        raise BundleError("duplicate variableId in the variable table")
    if [variable.variable_id for variable in variables] != sorted(
        variable.variable_id for variable in variables
    ):
        raise BundleError("variable table must be sorted by variableId")
    if any(variable.predictor not in PREDICTORS_V2 for variable in variables):
        raise BundleError("v2 predictors must be RAW or PREVIOUS")
    cursor = 0
    for group in groups:
        if group.first_frame != cursor or not 1 <= group.frame_count <= MAX_GROUP_LENGTH:
            raise BundleError("temporal groups must partition the axis in order")
        cursor += group.frame_count
    if cursor != frame_count:
        raise BundleError("temporal groups must cover the whole axis")
    expected_chunks = len(groups) * tiles.count * len(variables)
    if len(chunks) != expected_chunks:
        raise BundleError(f"expected {expected_chunks} chunks, got {len(chunks)}")

    metadata_offset = HEADER_SIZE
    index_offset = align8(metadata_offset + len(metadata_bytes))
    index_length = (
        INDEX_HEADER_SIZE_V2
        + VARIABLE_ENTRY_SIZE * len(variables)
        + GROUP_ENTRY_SIZE * len(groups)
        + CHUNK_ENTRY_SIZE * len(chunks)
    )
    if dictionary:
        dictionary_offset = align8(index_offset + index_length)
        data_offset = align8(dictionary_offset + len(dictionary))
    else:
        dictionary_offset = 0
        data_offset = align8(index_offset + index_length)

    cursor = data_offset
    for chunk in chunks:
        if chunk.entry.compressed_length != len(chunk.payload):
            raise BundleError("chunk compressedLength does not match payload")
        if not chunk.payload:
            raise BundleError("a chunk must have a payload")
        cursor += len(chunk.payload)
    file_size = align8(cursor)

    output = bytearray(file_size)
    output[0:HEADER_SIZE] = _HEADER_STRUCT.pack(
        MAGIC,
        VERSION_V2,
        HEADER_SIZE,
        0,
        file_size,
        metadata_offset,
        len(metadata_bytes),
        index_offset,
        index_length,
        data_offset,
        dictionary_offset,
        len(dictionary),
    )
    output[metadata_offset : metadata_offset + len(metadata_bytes)] = metadata_bytes
    output[index_offset : index_offset + INDEX_HEADER_SIZE_V2] = _INDEX_HEADER_V2_STRUCT.pack(
        INDEX_MAGIC_V2,
        INDEX_VERSION_V2,
        INDEX_HEADER_SIZE_V2,
        tile_width,
        tile_height,
        len(groups),
        len(variables),
        compression,
        len(chunks),
        0,
        0,
    )
    table = bytearray()
    for variable in variables:
        table += variable.pack()
    for group in groups:
        table += group.pack()
    for chunk in chunks:
        table += chunk.entry.pack()
    table_start = index_offset + INDEX_HEADER_SIZE_V2
    output[table_start : table_start + len(table)] = table
    if dictionary:
        output[dictionary_offset : dictionary_offset + len(dictionary)] = dictionary
    cursor = data_offset
    for chunk in chunks:
        output[cursor : cursor + len(chunk.payload)] = chunk.payload
        cursor += len(chunk.payload)

    _publish(path, output)


def _checked_range(offset: int, length: int, file_size: int, label: str) -> None:
    if offset < 0 or length < 0 or offset + length > file_size:
        raise BundleError(f"{label} range [{offset}, {offset}+{length}) exceeds file size {file_size}")


def _require_zero(data: bytes, start: int, end: int, label: str) -> None:
    if any(data[start:end]):
        raise BundleError(f"{label} padding bytes must be zero")


class Bundle:
    """Reference reader with complete structural validation."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self._cache: dict[tuple[int, int], np.ndarray] = {}
        self._chunk_cache: dict[int, np.ndarray] = {}
        self._parse_header()
        self._parse_metadata()
        if self.container_version == VERSION_V2:
            self._parse_index_v2()
        else:
            self._parse_index()
            self._validate_entries()

    # -- parsing -----------------------------------------------------------

    def _parse_header(self) -> None:
        if len(self.data) < HEADER_SIZE:
            raise BundleError("file is smaller than the fixed header")
        (
            magic,
            version,
            header_size,
            flags,
            file_size,
            metadata_offset,
            metadata_length,
            index_offset,
            index_length,
            data_offset,
            dictionary_offset,
            dictionary_length,
        ) = _HEADER_STRUCT.unpack_from(self.data, 0)
        if magic != MAGIC:
            raise BundleError("invalid magic, not a Xue file")
        if version not in CONTAINER_VERSIONS:
            raise BundleError(f"unsupported Xue version {version}")
        if header_size != HEADER_SIZE:
            raise BundleError("headerSize must be 80")
        if flags != 0:
            raise BundleError("header flags must be 0")
        if file_size != len(self.data):
            raise BundleError(f"header fileSize {file_size} does not match actual length {len(self.data)}")
        if metadata_offset != HEADER_SIZE:
            raise BundleError("metadataOffset must be 80")
        _checked_range(metadata_offset, metadata_length, file_size, "metadata")
        if index_offset != align8(metadata_offset + metadata_length):
            raise BundleError("indexOffset must immediately follow aligned metadata")
        _require_zero(self.data, metadata_offset + metadata_length, index_offset, "metadata")
        _checked_range(index_offset, index_length, file_size, "index")
        if dictionary_length == 0:
            if dictionary_offset != 0:
                raise BundleError("dictionaryOffset must be 0 when no dictionary is embedded")
            expected_data = align8(index_offset + index_length)
        else:
            if dictionary_offset != align8(index_offset + index_length):
                raise BundleError("dictionaryOffset must immediately follow the aligned index")
            _require_zero(self.data, index_offset + index_length, dictionary_offset, "index")
            _checked_range(dictionary_offset, dictionary_length, file_size, "dictionary")
            expected_data = align8(dictionary_offset + dictionary_length)
        if data_offset != expected_data:
            raise BundleError("dataOffset must immediately follow the previous aligned section")
        if dictionary_length == 0:
            _require_zero(self.data, index_offset + index_length, data_offset, "index")
        else:
            _require_zero(self.data, dictionary_offset + dictionary_length, data_offset, "dictionary")
        _checked_range(data_offset, 0, file_size, "data")

        self.container_version = version
        self.metadata_offset = metadata_offset
        self.metadata_length = metadata_length
        self.index_offset = index_offset
        self.index_length = index_length
        self.data_offset = data_offset
        self.dictionary_offset = dictionary_offset
        self.dictionary_length = dictionary_length

    def _parse_metadata(self) -> None:
        raw = self.data[self.metadata_offset : self.metadata_offset + self.metadata_length]
        try:
            metadata = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BundleError("metadata is not valid UTF-8 JSON") from exc
        if not isinstance(metadata, dict):
            raise BundleError("metadata must be a JSON object")
        schema_version = metadata.get("schemaVersion")
        if schema_version not in (1, 2, 3):
            raise BundleError("unsupported metadata schemaVersion")
        grid = metadata.get("grid")
        time_info = metadata.get("time")
        variables = metadata.get("variables")
        if not isinstance(grid, dict) or not isinstance(time_info, dict) or not isinstance(variables, list):
            raise BundleError("metadata must contain grid, time, and variables")
        width, height = grid.get("width"), grid.get("height")
        if not isinstance(width, int) or not isinstance(height, int) or width <= 0 or height <= 0:
            raise BundleError("metadata grid dimensions are invalid")
        if width * height > 64 * 1024 * 1024:
            raise BundleError("metadata grid exceeds the plane safety limit")
        frame_count = time_info.get("frameCount")
        if not isinstance(frame_count, int) or frame_count <= 0:
            raise BundleError("metadata time description is invalid")
        unit_seconds, frame_offsets, axis_version = self._parse_time_axis(time_info, frame_count, schema_version)
        variable_ids: dict[int, str] = {}
        parameters = 0
        for variable in variables:
            if not isinstance(variable, dict):
                raise BundleError("metadata variable must be an object")
            numeric_id = variable.get("numericId")
            name = variable.get("id")
            if not isinstance(numeric_id, int) or not 1 <= numeric_id <= 255 or not isinstance(name, str):
                raise BundleError("metadata variable numericId or id is invalid")
            if numeric_id in variable_ids:
                raise BundleError("metadata contains duplicate variable numericId values")
            self._parse_parameter(variable.get("parameter"), schema_version)
            parameters += "parameter" in variable
            variable_ids[numeric_id] = name
        if not variable_ids:
            raise BundleError("metadata must declare at least one variable")
        # Every axis and every variable set has exactly one valid encoding:
        # the declared version must be the lowest able to express both.
        required_version = max(axis_version, 3 if parameters else 1)
        if schema_version != required_version:
            raise BundleError(f"metadata must declare schemaVersion {required_version}")

        self.metadata = metadata
        self.width = width
        self.height = height
        self.plane_length = width * height
        self.frame_count = frame_count
        self.unit_seconds = unit_seconds
        self.frame_offsets = frame_offsets
        self._offset_index = {hour: index for index, hour in enumerate(frame_offsets)}
        self.variable_ids = variable_ids

    @staticmethod
    def _parse_parameter(parameter: Any, schema_version: int) -> None:
        """Validate a variable's GRIB2 identity block.

        The block is what schemaVersion 3 introduces, so it must be present
        in a version 3 file and absent below — the declared version is always
        the lowest able to express the metadata (docs/format.md)."""
        if schema_version < 3:
            if parameter is not None:
                raise BundleError("a GRIB2 parameter block requires schemaVersion 3")
            return
        if not isinstance(parameter, dict):
            raise BundleError("schemaVersion 3 requires a parameter block on every variable")
        unknown = set(parameter) - _PARAMETER_FIELDS - _PARAMETER_NULLABLE_FIELDS
        if unknown:
            raise BundleError(f"unknown parameter fields: {', '.join(sorted(unknown))}")
        for field in _PARAMETER_FIELDS:
            value = parameter.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255:
                raise BundleError(f"parameter {field} is invalid")
        scale_factor = parameter.get("scaleFactorOfFirstFixedSurface", ...)
        scaled_value = parameter.get("scaledValueOfFirstFixedSurface", ...)
        if scale_factor is ... or scaled_value is ...:
            raise BundleError("parameter fixed surface value is incomplete")
        if (scale_factor is None) != (scaled_value is None):
            # GRIB2 encodes a surface with no value by writing both as
            # missing; one of the two alone describes nothing.
            raise BundleError("parameter fixed surface scale factor and value must both be present or both null")
        if scale_factor is not None and (
            not isinstance(scale_factor, int)
            or isinstance(scale_factor, bool)
            or not -127 <= scale_factor <= 127
            or not isinstance(scaled_value, int)
            or isinstance(scaled_value, bool)
            or not 0 <= scaled_value <= 0xFFFFFFFE
        ):
            raise BundleError("parameter fixed surface value is invalid")
        statistical = parameter.get("typeOfStatisticalProcessing")
        if statistical is not None and (
            not isinstance(statistical, int) or isinstance(statistical, bool) or not 0 <= statistical <= 255
        ):
            raise BundleError("parameter typeOfStatisticalProcessing is invalid")

    @classmethod
    def _parse_time_axis(
        cls, time_info: dict[str, Any], frame_count: int, schema_version: int
    ) -> tuple[int, list[int], int]:
        """The axis unit, the frame offsets, and the lowest schema version
        able to express them.

        Schema versions 1 and 2 describe a whole-hour axis with
        ``firstForecastHour`` plus one of ``stepHours`` (uniform, version 1)
        and ``hours`` (listed outright, version 2). Version 3 replaces the
        block with a unit-neutral one — ``unitSeconds`` plus offsets in that
        unit — so a series finer than an hour (the six-minute radar mosaic)
        has an exact axis (docs/format.md)."""
        if schema_version >= 3:
            return cls._parse_offset_axis(time_info, frame_count)
        if _V3_TIME_FIELDS & set(time_info):
            raise BundleError("a unit-neutral time axis requires schemaVersion 3")
        first_hour = time_info.get("firstForecastHour")
        if not isinstance(first_hour, int) or isinstance(first_hour, bool) or first_hour < 0:
            raise BundleError("metadata time description is invalid")
        step_hours = time_info.get("stepHours")
        listed_hours = time_info.get("hours")
        if (step_hours is None) == (listed_hours is None):
            raise BundleError("metadata time must declare exactly one of stepHours and hours")
        if step_hours is not None:
            if not isinstance(step_hours, int) or step_hours <= 0:
                raise BundleError("metadata stepHours is invalid")
            if first_hour + (frame_count - 1) * step_hours >= NO_DEPENDENCY:
                raise BundleError("forecast hours exceed the u16 range")
            return HOUR_SECONDS, [first_hour + index * step_hours for index in range(frame_count)], 1
        cls._check_listed_offsets(listed_hours, frame_count, first_hour)
        return HOUR_SECONDS, list(listed_hours), 2

    @classmethod
    def _parse_offset_axis(cls, time_info: dict[str, Any], frame_count: int) -> tuple[int, list[int], int]:
        """The schemaVersion 3 time block: offsets on a declared unit."""
        unknown = set(time_info) - _V3_TIME_FIELDS - {"frameCount"}
        if unknown:
            raise BundleError(f"unknown time fields: {', '.join(sorted(unknown))}")
        unit_seconds = time_info.get("unitSeconds")
        if (
            not isinstance(unit_seconds, int)
            or isinstance(unit_seconds, bool)
            or not 1 <= unit_seconds <= HOUR_SECONDS
            or HOUR_SECONDS % unit_seconds
        ):
            raise BundleError("metadata unitSeconds must be a whole divisor of 3600")
        first_offset = time_info.get("firstFrameOffset")
        if not isinstance(first_offset, int) or isinstance(first_offset, bool) or first_offset < 0:
            raise BundleError("metadata firstFrameOffset is invalid")
        frame_step = time_info.get("frameStep")
        listed = time_info.get("frameOffsets")
        if (frame_step is None) == (listed is None):
            raise BundleError("metadata time must declare exactly one of frameStep and frameOffsets")
        if frame_step is not None:
            if not isinstance(frame_step, int) or isinstance(frame_step, bool) or frame_step <= 0:
                raise BundleError("metadata frameStep is invalid")
            if first_offset + (frame_count - 1) * frame_step >= NO_DEPENDENCY:
                raise BundleError("frame offsets exceed the u16 range")
            offsets = [first_offset + index * frame_step for index in range(frame_count)]
        else:
            cls._check_listed_offsets(listed, frame_count, first_offset, uniform_message="frameStep")
            offsets = list(listed)
        # The unit is the coarsest one that expresses every offset exactly, so
        # an axis has one encoding rather than one per divisor of its step.
        if math.gcd(HOUR_SECONDS // unit_seconds, *offsets) != 1:
            raise BundleError("metadata unitSeconds is finer than the axis needs")
        return unit_seconds, offsets, 3

    @staticmethod
    def _check_listed_offsets(
        listed: Any, frame_count: int, first: int, *, uniform_message: str = "stepHours"
    ) -> None:
        if (
            not isinstance(listed, list)
            or len(listed) != frame_count
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in listed)
            or listed[0] != first
            or any(after <= before for before, after in zip(listed, listed[1:]))
            or listed[-1] >= NO_DEPENDENCY
        ):
            raise BundleError("metadata listed time axis is invalid")
        if len({after - before for before, after in zip(listed, listed[1:])}) < 2:
            raise BundleError(f"a uniform axis must be encoded as {uniform_message}")

    def _parse_index_v2(self) -> None:
        """The v2 index: three tables over a fixed physical order.

        Nothing here stores where a chunk is — the order is the spec's, and
        chunks are strictly adjacent, so offsets are the prefix sums of the
        lengths. What is validated is that the geometry, the axis partition
        and those prefix sums all agree with the metadata and the file length.
        """
        if self.index_length < INDEX_HEADER_SIZE_V2:
            raise BundleError("index is smaller than its header")
        (
            magic,
            version,
            header_size,
            tile_width,
            tile_height,
            group_count,
            variable_count,
            compression,
            chunk_count,
            reserved0,
            reserved1,
        ) = _INDEX_HEADER_V2_STRUCT.unpack_from(self.data, self.index_offset)
        if magic != INDEX_MAGIC_V2:
            raise BundleError("invalid index magic")
        if version != INDEX_VERSION_V2:
            raise BundleError("index version must be 2")
        if header_size != INDEX_HEADER_SIZE_V2:
            raise BundleError("index headerSize must be 32 for v2")
        if reserved0 != 0 or reserved1 != 0:
            raise BundleError("index reserved words must be 0")
        if compression not in (COMPRESSION_ZSTD, COMPRESSION_ZSTD_DICT):
            raise BundleError(f"unsupported v2 compression {compression}")
        if compression == COMPRESSION_ZSTD_DICT and self.dictionary_length == 0:
            raise BundleError("ZSTD_DICT requires an embedded dictionary")
        if group_count < 1 or variable_count < 1:
            raise BundleError("a v2 file must declare at least one group and one variable")
        if variable_count != len(self.variable_ids):
            raise BundleError("index variableCount does not match the metadata variables")
        self.tiles = TileGeometry(self.width, self.height, tile_width, tile_height)
        self.compression = compression

        expected_length = (
            INDEX_HEADER_SIZE_V2
            + VARIABLE_ENTRY_SIZE * variable_count
            + GROUP_ENTRY_SIZE * group_count
            + CHUNK_ENTRY_SIZE * chunk_count
        )
        if chunk_count != group_count * self.tiles.count * variable_count:
            raise BundleError("chunkCount does not match groupCount x tileCount x variableCount")
        if self.index_length != expected_length:
            raise BundleError("indexLength does not match the index tables")

        cursor = self.index_offset + INDEX_HEADER_SIZE_V2
        variables: list[VariableEntry] = []
        for _ in range(variable_count):
            variables.append(VariableEntry.unpack(self.data[cursor : cursor + VARIABLE_ENTRY_SIZE]))
            cursor += VARIABLE_ENTRY_SIZE
        ids = [variable.variable_id for variable in variables]
        if ids != sorted(ids) or len(set(ids)) != len(ids):
            raise BundleError("variable entries must be sorted and unique by variableId")
        if set(ids) != set(self.variable_ids):
            raise BundleError("variable entries do not match the metadata variables")
        for variable in variables:
            if variable.predictor not in PREDICTORS_V2:
                raise BundleError(f"predictor {variable.predictor} is not valid in v2")
        self.variable_entries = variables
        self.variable_positions = {variable_id: position for position, variable_id in enumerate(ids)}
        self.predictors = {variable.variable_id: variable.predictor for variable in variables}

        groups: list[GroupEntry] = []
        frame_cursor = 0
        for _ in range(group_count):
            group = GroupEntry.unpack(self.data[cursor : cursor + GROUP_ENTRY_SIZE])
            cursor += GROUP_ENTRY_SIZE
            if group.frame_count == 0:
                raise BundleError("a temporal group must hold at least one frame")
            if group.first_frame != frame_cursor:
                raise BundleError("temporal groups must partition the axis in order")
            frame_cursor += group.frame_count
            groups.append(group)
        if frame_cursor != self.frame_count:
            raise BundleError("temporal groups must end exactly at the axis frameCount")
        self.groups = groups
        self.frame_group = [
            (index, frame - group.first_frame)
            for index, group in enumerate(groups)
            for frame in range(group.first_frame, group.first_frame + group.frame_count)
        ]

        chunks: list[ChunkEntry] = []
        offsets: list[int] = []
        position = self.data_offset
        for _ in range(chunk_count):
            chunk = ChunkEntry.unpack(self.data[cursor : cursor + CHUNK_ENTRY_SIZE])
            cursor += CHUNK_ENTRY_SIZE
            if chunk.compressed_length == 0:
                raise BundleError("a chunk must have a payload")
            offsets.append(position)
            position += chunk.compressed_length
            if position > len(self.data):
                raise BundleError("chunk payloads exceed the file size")
            chunks.append(chunk)
        if align8(position) != len(self.data):
            raise BundleError("fileSize must equal the aligned end of the last chunk")
        _require_zero(self.data, position, len(self.data), "trailing")
        self.chunks = chunks
        self.chunk_offsets = offsets

    def _parse_index(self) -> None:
        if self.index_length < INDEX_HEADER_SIZE:
            raise BundleError("index is smaller than its header")
        magic, entry_size, version, entry_count, reserved = _INDEX_HEADER_STRUCT.unpack_from(
            self.data, self.index_offset
        )
        if magic != INDEX_MAGIC:
            raise BundleError("invalid index magic")
        if entry_size != ENTRY_SIZE:
            raise BundleError("index entrySize must be 40 for v1")
        if version != INDEX_VERSION:
            raise BundleError("index version must be 1")
        if reserved != 0:
            raise BundleError("index reserved must be 0")
        if self.index_length != INDEX_HEADER_SIZE + entry_count * ENTRY_SIZE:
            raise BundleError("indexLength does not match entryCount")
        expected_entries = self.frame_count * len(self.variable_ids)
        if entry_count != expected_entries:
            raise BundleError(f"entryCount {entry_count} does not match metadata, expected {expected_entries}")
        entries = []
        for position in range(entry_count):
            start = self.index_offset + INDEX_HEADER_SIZE + position * ENTRY_SIZE
            entries.append(PlaneEntry.unpack(self.data[start : start + ENTRY_SIZE]))
        self.entries = entries
        self.entry_map = {(entry.variable_id, entry.frame_offset): entry for entry in entries}

    def _validate_entries(self) -> None:
        if len(self.entry_map) != len(self.entries):
            raise BundleError("duplicate (variableId, frameOffset) index entries")
        ordering = [(entry.variable_id, entry.frame_offset) for entry in self.entries]
        if ordering != sorted(ordering):
            raise BundleError("index entries must be sorted by (variableId, frameOffset)")
        for variable_id in self.variable_ids:
            hours = sorted(hour for vid, hour in self.entry_map if vid == variable_id)
            if hours != self.frame_offsets:
                raise BundleError(f"variable {variable_id} does not cover every forecast hour")

        occupied: list[tuple[int, int, PlaneEntry]] = []
        for entry in self.entries:
            if entry.variable_id not in self.variable_ids:
                raise BundleError(f"unknown variableId {entry.variable_id}")
            if entry.predictor not in PREDICTORS:
                raise BundleError(f"unknown predictor {entry.predictor}")
            if entry.compression not in COMPRESSIONS:
                raise BundleError(f"unknown compression {entry.compression}")
            if entry.flags & ~FLAG_ZSTD_CHECKSUM:
                raise BundleError(f"unknown entry flags 0x{entry.flags:02x}")
            if entry.compression == COMPRESSION_ZSTD_DICT and self.dictionary_length == 0:
                raise BundleError("ZSTD_DICT entry requires an embedded dictionary")
            if entry.decoded_length != self.plane_length:
                raise BundleError("entry decodedLength does not match the metadata grid")
            if entry.minimum_code > entry.maximum_code:
                raise BundleError("entry minimumCode exceeds maximumCode")
            if entry.predictor == PREDICTOR_ZERO:
                if entry.compressed_length != 0:
                    raise BundleError("ZERO entries must have no payload")
                continue
            if entry.compressed_length == 0:
                raise BundleError("non-ZERO entries must have a payload")
            if entry.data_offset < self.data_offset:
                raise BundleError("payload overlaps a structural section")
            _checked_range(entry.data_offset, entry.compressed_length, len(self.data), "payload")
            occupied.append((entry.data_offset, entry.data_offset + entry.compressed_length, entry))

        occupied.sort()
        cursor = self.data_offset
        for start, end, _entry in occupied:
            if start != cursor:
                raise BundleError("payloads must be strictly adjacent with no unindexed gaps")
            cursor = end
        if align8(cursor) != len(self.data):
            raise BundleError("fileSize must equal the aligned end of the last payload")
        _require_zero(self.data, cursor, len(self.data), "trailing")

        # Dependency validation: same variable, same group, acyclic.
        for entry in self.entries:
            if entry.predictor in (PREDICTOR_RAW, PREDICTOR_ZERO):
                if entry.dependency_offset != NO_DEPENDENCY:
                    raise BundleError("RAW and ZERO entries must have dependencyOffset 65535")
                continue
            if entry.predictor == PREDICTOR_ANCHOR:
                dependency_offset = entry.dependency_offset
            else:
                # PREVIOUS references the preceding frame on the time axis,
                # carried explicitly in dependencyOffset (never the sentinel).
                index = self._offset_index.get(entry.frame_offset)
                if index is None or index == 0:
                    raise BundleError("PREVIOUS entry has no preceding frame on the time axis")
                dependency_offset = self.frame_offsets[index - 1]
                if entry.dependency_offset != dependency_offset:
                    raise BundleError("PREVIOUS entry dependencyOffset must reference the preceding frame on the time axis")
            dependency = self.entry_map.get((entry.variable_id, dependency_offset))
            if dependency is None:
                raise BundleError("entry depends on a plane that does not exist")
            if dependency.group_id != entry.group_id:
                raise BundleError("dependencies must stay inside the same temporal group")
        for key in self.entry_map:
            self._dependency_chain(key)

    def _dependency_offset(self, entry: PlaneEntry) -> int | None:
        # ANCHOR and PREVIOUS both carry their dependency explicitly;
        # _validate_entries has pinned PREVIOUS to the preceding axis frame.
        if entry.predictor in (PREDICTOR_ANCHOR, PREDICTOR_PREVIOUS):
            return entry.dependency_offset
        return None

    def _dependency_chain(self, key: tuple[int, int]) -> list[PlaneEntry]:
        chain: list[PlaneEntry] = []
        seen: set[tuple[int, int]] = set()
        current: tuple[int, int] | None = key
        while current is not None:
            if current in seen or len(chain) > self.frame_count:
                raise BundleError("cyclic or too-deep dependency chain")
            seen.add(current)
            entry = self.entry_map.get(current)
            if entry is None:
                raise BundleError("dependency chain references a missing plane")
            chain.append(entry)
            dependency = self._dependency_offset(entry)
            current = None if dependency is None else (entry.variable_id, dependency)
        return chain

    # -- decoding ----------------------------------------------------------

    def _payload(self, entry: PlaneEntry) -> bytes:
        raw = self.data[entry.data_offset : entry.data_offset + entry.compressed_length]
        if entry.compression == COMPRESSION_NONE:
            if len(raw) != entry.decoded_length:
                raise BundleError("uncompressed payload length mismatch")
            return raw
        if entry.compression == COMPRESSION_ZSTD_DICT:
            raise BundleError("ZSTD_DICT decoding is not implemented by the reference reader")
        if bool(entry.flags & FLAG_ZSTD_CHECKSUM) != zstdcli.frame_has_checksum(raw):
            raise BundleError("entry checksum flag does not match the Zstandard frame")
        return zstdcli.decompress(raw, expected_length=entry.decoded_length)

    # -- v2 chunks ---------------------------------------------------------

    def chunk_position(self, group: int, tile: int, variable_id: int) -> int:
        """A chunk's position in the file's fixed physical order."""
        try:
            variable_position = self.variable_positions[variable_id]
        except KeyError as exc:
            raise BundleError(f"unknown variableId {variable_id}") from exc
        if not 0 <= group < len(self.groups) or not 0 <= tile < self.tiles.count:
            raise BundleError("chunk coordinates are outside the file")
        return (group * self.tiles.count + tile) * len(self.variable_entries) + variable_position

    def chunk_span(self, position: int) -> tuple[int, int]:
        """The byte range ``[start, end)`` of a chunk's compressed payload."""
        start = self.chunk_offsets[position]
        return start, start + self.chunks[position].compressed_length

    def decode_chunk(self, position: int) -> np.ndarray:
        """One reconstructed chunk as ``(frames, tileHeight, tileWidth)`` codes."""
        cached = self._chunk_cache.get(position)
        if cached is not None:
            return cached
        entry = self.chunks[position]
        group = self.groups[position // (self.tiles.count * len(self.variable_entries))]
        tile = position // len(self.variable_entries) % self.tiles.count
        variable_id = self.variable_entries[position % len(self.variable_entries)].variable_id
        tile_height, tile_width = self.tiles.shape(tile)
        shape = (group.frame_count, tile_height, tile_width)
        start, end = self.chunk_span(position)
        raw = self.data[start:end]
        if self.compression == COMPRESSION_ZSTD_DICT:
            raise BundleError("ZSTD_DICT decoding is not implemented by the reference reader")
        if not zstdcli.frame_has_checksum(raw):
            raise BundleError("a v2 chunk must be a Zstandard frame with a content checksum")
        decoded = np.frombuffer(
            zstdcli.decompress(raw, expected_length=math.prod(shape)), dtype=np.uint8
        ).reshape(shape)
        if self.predictors[variable_id] == PREDICTOR_PREVIOUS:
            # The chunk's frames are consecutive on the axis, so the residual
            # chain never leaves it: a modulo-256 running sum replays it.
            chunk = np.cumsum(decoded, axis=0, dtype=np.uint8)
        else:
            chunk = decoded
        if crc32_plane(chunk) != entry.crc32:
            raise BundleError(f"chunk CRC32 mismatch at position {position}")
        self._chunk_cache[position] = chunk
        return chunk

    def _decode_plane_v2(self, variable_id: int, frame_offset: int, tiles: list[int] | None) -> np.ndarray:
        frame = self._offset_index.get(frame_offset)
        if frame is None:
            raise BundleError(f"no plane for variable {variable_id} hour {frame_offset}")
        group, frame_in_group = self.frame_group[frame]
        plane = np.zeros((self.height, self.width), dtype=np.uint8)
        for tile in range(self.tiles.count) if tiles is None else tiles:
            chunk = self.decode_chunk(self.chunk_position(group, tile, variable_id))
            row, column = self.tiles.origin(tile)
            tile_height, tile_width = self.tiles.shape(tile)
            plane[row : row + tile_height, column : column + tile_width] = chunk[frame_in_group]
        return plane.ravel()

    def decode_series(self, variable_id: int, column: int, row: int) -> np.ndarray:
        """One cell's code on every frame of the axis.

        In v2 this costs one chunk per group of the single tile containing the
        cell, no matter how many frames the axis has. A v1 file has no such
        shortcut: the series is read plane by plane.
        """
        if self.container_version != VERSION_V2:
            return np.array(
                [self.decode_plane(variable_id, offset)[row * self.width + column] for offset in self.frame_offsets],
                dtype=np.uint8,
            )
        tile = self.tiles.tile_of(row, column)
        tile_row, tile_column = self.tiles.origin(tile)
        series = np.empty(self.frame_count, dtype=np.uint8)
        cursor = 0
        for group_index, group in enumerate(self.groups):
            chunk = self.decode_chunk(self.chunk_position(group_index, tile, variable_id))
            series[cursor : cursor + group.frame_count] = chunk[:, row - tile_row, column - tile_column]
            cursor += group.frame_count
        return series

    def decode_plane(self, variable_id: int, frame_offset: int, tiles: list[int] | None = None) -> np.ndarray:
        if self.container_version == VERSION_V2:
            return self._decode_plane_v2(variable_id, frame_offset, tiles)
        if tiles is not None:
            raise BundleError("a v1 file has no tiles")
        key = (variable_id, frame_offset)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        entry = self.entry_map.get(key)
        if entry is None:
            raise BundleError(f"no plane for variable {variable_id} hour {frame_offset}")
        if entry.predictor == PREDICTOR_ZERO:
            plane = np.zeros(self.plane_length, dtype=np.uint8)
        else:
            payload = np.frombuffer(self._payload(entry), dtype=np.uint8)
            if entry.predictor == PREDICTOR_RAW:
                plane = payload
            else:
                dependency_offset = self._dependency_offset(entry)
                assert dependency_offset is not None
                base = self.decode_plane(variable_id, dependency_offset)
                plane = (payload + base).astype(np.uint8)
        if crc32_plane(plane) != entry.crc32:
            raise BundleError(f"plane CRC32 mismatch for variable {variable_id} hour {frame_offset}")
        if int(plane.min()) != entry.minimum_code or int(plane.max()) != entry.maximum_code:
            raise BundleError(f"plane code range mismatch for variable {variable_id} hour {frame_offset}")
        self._cache[key] = plane
        return plane

    def verify_all(self, executor: ThreadPoolExecutor | None = None) -> None:
        if self.container_version == VERSION_V2:
            return self._verify_all_v2(executor)
        # Dependencies never leave a temporal group (validated above), so
        # groups decode independently on a thread pool; each group drops its
        # planes from the cache as soon as it finishes, keeping peak memory
        # at a few planes per worker. Callers verifying several bundles at
        # once pass a shared ``executor`` so the total zstd subprocess load
        # stays bounded by one pool.
        groups: dict[tuple[int, int], list[int]] = {}
        for (variable_id, hour), entry in self.entry_map.items():
            groups.setdefault((variable_id, entry.group_id), []).append(hour)

        def verify_group(item: tuple[tuple[int, int], list[int]]) -> None:
            (variable_id, _group_id), hours = item
            try:
                for hour in sorted(hours):
                    self.decode_plane(variable_id, hour)
            finally:
                for hour in hours:
                    self._cache.pop((variable_id, hour), None)

        if executor is not None:
            list(executor.map(verify_group, groups.items()))
            return
        with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as own_executor:
            list(own_executor.map(verify_group, groups.items()))

    def _verify_all_v2(self, executor: ThreadPoolExecutor | None) -> None:
        # Every chunk stands alone (its own Zstandard frame, its own CRC32),
        # so verification is one independent job per chunk; each drops its
        # reconstruction immediately, keeping peak memory at a tile per worker.
        def verify_chunk(position: int) -> None:
            try:
                self.decode_chunk(position)
            finally:
                self._chunk_cache.pop(position, None)

        positions = range(len(self.chunks))
        if executor is not None:
            list(executor.map(verify_chunk, positions))
            return
        with ThreadPoolExecutor(max_workers=min(16, os.cpu_count() or 4)) as own_executor:
            list(own_executor.map(verify_chunk, positions))


def read_bundle(path: Path) -> Bundle:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BundleError(f"cannot read bundle: {path}: {exc}") from exc
    return Bundle(data)

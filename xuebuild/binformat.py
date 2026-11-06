"""Xue container serialization and bundle metadata validation.

This module is the Python source of truth for the binary layout defined in
docs/format.md. The writer produces complete files, the golden fixtures the
Rust decoder is tested against among them; reading a container back is the
wheel's (``xue.Bundle``, ``rust/xue-py``). :class:`BundleMetadata` validates
the metadata document a container embeds and a store's group carries.

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
import re
import struct
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from .errors import BundleError

MAGIC = b"XUE\0\0\0\0\0"
VERSION = 1
"""The plane-major container. Read, never written."""
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

# The optional blocks beside ``parameter`` (docs/format.md §"Band and
# Producer"): the spectral band a satellite image was taken in, in the fields
# of GRIB2 product definition template 4.31 with their byte widths, and the
# algorithm that derived a composite field. Each is present whole or absent.
_BAND_FIELDS: dict[str, tuple[int, int]] = {
    "satelliteSeries": (0, 0xFFFF),
    "satelliteNumber": (0, 0xFFFF),
    "instrumentType": (0, 0xFFFF),
    "scaleFactorOfCentralWaveNumber": (-127, 127),
    "scaledValueOfCentralWaveNumber": (0, 0xFFFFFFFE),
}
_PRODUCER_FIELDS = frozenset({"id", "version"})
_PRODUCER_ID = re.compile(r"^[a-z][a-z0-9]*$")
# The third optional block: an aerosol product's type and intervals, in the
# fields of product definition template 4.48. The type fields are plain
# integers; each interval is a code table 4.91 type and two limits, a limit
# a (scale factor, scaled value) pair that is whole or null like the fixed
# surface's, and null throughout when the interval's type is 255 (missing).
_AEROSOL_MISSING_INTERVAL = 255
_AEROSOL_TYPE_FIELDS: dict[str, tuple[int, int]] = {
    "aerosolType": (0, 0xFFFF),
    "typeOfSizeInterval": (0, 0xFF),
    "typeOfWavelengthInterval": (0, 0xFF),
}
_AEROSOL_INTERVALS: dict[str, tuple[tuple[str, str], ...]] = {
    "typeOfSizeInterval": (
        ("scaleFactorOfFirstSize", "scaledValueOfFirstSize"),
        ("scaleFactorOfSecondSize", "scaledValueOfSecondSize"),
    ),
    "typeOfWavelengthInterval": (
        ("scaleFactorOfFirstWavelength", "scaledValueOfFirstWavelength"),
        ("scaleFactorOfSecondWavelength", "scaledValueOfSecondWavelength"),
    ),
}
_AEROSOL_FIELDS = frozenset(_AEROSOL_TYPE_FIELDS) | frozenset(
    field for pairs in _AEROSOL_INTERVALS.values() for pair in pairs for field in pair
)

_HEADER_STRUCT = struct.Struct("<8sHHIQQQQQQQQ")
_INDEX_HEADER_STRUCT = struct.Struct("<4sHHII")
_ENTRY_STRUCT = struct.Struct("<BBBBHHHHIQIIBB6s")

_INDEX_HEADER_V2_STRUCT = struct.Struct("<4sHHHHHBBIIQ")
_VARIABLE_ENTRY_STRUCT = struct.Struct("<BBH")
_GROUP_ENTRY_STRUCT = struct.Struct("<HBB")
_CHUNK_ENTRY_STRUCT = struct.Struct("<II")


# The fields a regular grid names and a projected one must not.
_REGULAR_GRID_FIELDS = ("firstLongitude", "firstLatitude", "longitudeStep", "latitudeStep")
# The closed set of ``projection.kind``; a grid of any other kind is one this
# reader cannot place on the map.
_PROJECTION_KINDS = frozenset({"lambert-conformal"})
_PROJECTION_NUMBER_FIELDS = (
    "radius",
    "standardParallel1",
    "standardParallel2",
    "latitudeOfOrigin",
    "centralMeridian",
    "x0",
    "y0",
    "dx",
    "dy",
)
_GAUSSIAN_FIELDS = frozenset({"n", "firstRow", "rowStride"})
# Each latitude costs a few passes of a recurrence of order 2n, so n is bounded
# before a point read pays for it.
_MAX_GAUSSIAN_N = 32768


def validate_projection(projection: Any) -> None:
    """A ``grid.projection`` block (docs/format.md, "Regional Grids"): closed,
    of a known kind, with finite numbers, a cone that exists (its two parallels
    neither at a pole nor mirrored across the equator, where the constant is
    zero), and steps in the grid's column and row order."""
    if not isinstance(projection, dict):
        raise BundleError("metadata grid projection must be an object")
    kind = projection.get("kind")
    if not isinstance(kind, str) or kind not in _PROJECTION_KINDS:
        raise BundleError("metadata grid projection kind is not one this reader implements")
    if set(projection) != {"kind", *_PROJECTION_NUMBER_FIELDS}:
        raise BundleError("metadata grid projection must carry exactly its ten fields")
    for field in _PROJECTION_NUMBER_FIELDS:
        value = projection[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise BundleError(f"metadata grid projection {field} is invalid")
    if projection["radius"] <= 0:
        raise BundleError("metadata grid projection radius must be positive")
    for field in ("standardParallel1", "standardParallel2", "latitudeOfOrigin"):
        if abs(projection[field]) >= 90:
            raise BundleError("metadata grid projection latitudes must lie between the poles")
    if projection["standardParallel1"] + projection["standardParallel2"] == 0:
        raise BundleError("metadata grid projection parallels leave no cone")
    if abs(projection["centralMeridian"]) > 180:
        raise BundleError("metadata grid projection centralMeridian is out of range")
    if projection["dx"] <= 0 or projection["dy"] >= 0:
        raise BundleError("metadata grid projection steps must run east and south")


def validate_gaussian(gaussian: Any, height: int) -> None:
    """A ``grid.gaussian`` block: closed, and every one of the grid's ``height``
    rows a row of the Gaussian grid it names."""
    if not isinstance(gaussian, dict):
        raise BundleError("metadata grid gaussian must be an object")
    if set(gaussian) != _GAUSSIAN_FIELDS:
        raise BundleError("metadata grid gaussian block must carry exactly n, firstRow and rowStride")
    for field in _GAUSSIAN_FIELDS:
        value = gaussian[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BundleError(f"metadata grid gaussian {field} is invalid")
    if not 1 <= gaussian["n"] <= _MAX_GAUSSIAN_N:
        raise BundleError("metadata grid gaussian n is out of range")
    if gaussian["rowStride"] < 1:
        raise BundleError("metadata grid gaussian rowStride must be positive")
    if gaussian["firstRow"] + (height - 1) * gaussian["rowStride"] >= 2 * gaussian["n"]:
        raise BundleError("metadata grid gaussian rows run past the Gaussian grid")


def validate_projected_grid(grid: dict[str, Any]) -> None:
    """A grid carrying ``projection``: the block itself, which replaces the
    regular fields outright and takes no ``gaussian`` beside it, on a grid
    that does not wrap."""
    validate_projection(grid["projection"])
    if any(field in grid for field in _REGULAR_GRID_FIELDS):
        raise BundleError("a projected grid must not carry the regular grid fields")
    if "gaussian" in grid:
        raise BundleError("a projected grid must not carry a gaussian block")
    if grid.get("wrapLongitude") is not False:
        raise BundleError("a projected grid must declare wrapLongitude false")


def parse_grid_block(grid: dict[str, Any], height: int, schema_version: int) -> int:
    """The lowest schema version the grid's kind needs: 4 for a projection, no
    floor for a regular grid. A ``gaussian`` block sits beside the regular
    fields a reader that ignores it still uses, so it needs version 3 and
    raises nothing. Versions up to 3 keep whatever regular fields they have."""
    if "projection" in grid:
        validate_projected_grid(grid)
        return 4
    if "gaussian" in grid:
        if schema_version != 3:
            raise BundleError("a gaussian block requires schemaVersion 3")
        validate_gaussian(grid["gaussian"], height)
    return 1


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
    metadata_bytes = metadata_json_bytes(metadata)
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
    metadata_bytes = metadata_json_bytes(metadata)
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


class BundleMetadata:
    """A bundle's metadata document, validated by the rules every reader
    applies (docs/format.md): the schema version, the grid, the time axis and
    each variable's identity blocks. The store writers check the document
    they embed with it; containers are decoded by the wheel's ``xue.Bundle``.

    The parsed fields are attributes: ``metadata``, ``width``, ``height``,
    ``plane_length``, ``frame_count``, ``unit_seconds``, ``frame_offsets``
    and ``variable_ids`` (``numericId`` to id)."""

    def __init__(self, raw: bytes) -> None:
        self.data = raw
        self.metadata_offset = 0
        self.metadata_length = len(raw)
        self._parse_metadata()

    def _parse_metadata(self) -> None:
        raw = self.data[self.metadata_offset : self.metadata_offset + self.metadata_length]
        try:
            metadata = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BundleError("metadata is not valid UTF-8 JSON") from exc
        if not isinstance(metadata, dict):
            raise BundleError("metadata must be a JSON object")
        schema_version = metadata.get("schemaVersion")
        if schema_version not in (1, 2, 3, 4):
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
        grid_version = parse_grid_block(grid, height, schema_version)
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
            self._parse_band(variable, schema_version)
            self._parse_producer(variable, schema_version)
            self._parse_aerosol(variable, schema_version)
            variable_ids[numeric_id] = name
        if not variable_ids:
            raise BundleError("metadata must declare at least one variable")
        # Every axis, variable set and grid has exactly one valid encoding:
        # the declared version must be the lowest able to express all three.
        required_version = max(axis_version, 3 if parameters else 1, grid_version)
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

    @staticmethod
    def _parse_band(variable: dict[str, Any], schema_version: int) -> None:
        """Validate a variable's optional spectral band block. Absent on
        every variable a GRIB source produces; present, it is whole and
        within template 4.31's byte widths, and it needs schemaVersion 3
        without raising the version a file must declare."""
        if "band" not in variable:
            return
        band = variable["band"]
        if schema_version < 3:
            raise BundleError("a band block requires schemaVersion 3")
        if not isinstance(band, dict):
            raise BundleError("band must be an object")
        if set(band) != set(_BAND_FIELDS):
            raise BundleError("band block must carry exactly its five fields")
        for field, (low, high) in _BAND_FIELDS.items():
            value = band[field]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise BundleError(f"band {field} is invalid")

    @staticmethod
    def _parse_producer(variable: dict[str, Any], schema_version: int) -> None:
        """Validate a variable's optional producer block: the algorithm
        that derived a composite field, named by id and version."""
        if "producer" not in variable:
            return
        producer = variable["producer"]
        if schema_version < 3:
            raise BundleError("a producer block requires schemaVersion 3")
        if not isinstance(producer, dict):
            raise BundleError("producer must be an object")
        if set(producer) != _PRODUCER_FIELDS:
            raise BundleError("producer block must carry exactly id and version")
        if not isinstance(producer["id"], str) or not _PRODUCER_ID.match(producer["id"]):
            raise BundleError("producer id is invalid")
        if not isinstance(producer["version"], str) or not producer["version"]:
            raise BundleError("producer version is invalid")

    @staticmethod
    def _parse_aerosol(variable: dict[str, Any], schema_version: int) -> None:
        """Validate a variable's optional aerosol block: template 4.48's
        type and two intervals, every field present, each limit a whole
        pair, null exactly when its interval's type is missing
        (docs/format.md §"Band, Producer and Aerosol")."""
        if "aerosol" not in variable:
            return
        aerosol = variable["aerosol"]
        if schema_version < 3:
            raise BundleError("an aerosol block requires schemaVersion 3")
        if not isinstance(aerosol, dict):
            raise BundleError("aerosol must be an object")
        if set(aerosol) != _AEROSOL_FIELDS:
            raise BundleError("aerosol block must carry exactly its eleven fields")
        for field, (low, high) in _AEROSOL_TYPE_FIELDS.items():
            value = aerosol[field]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise BundleError(f"aerosol {field} is invalid")
        for type_field, pairs in _AEROSOL_INTERVALS.items():
            missing = aerosol[type_field] == _AEROSOL_MISSING_INTERVAL
            for scale_field, value_field in pairs:
                scale_factor, scaled_value = aerosol[scale_field], aerosol[value_field]
                if (scale_factor is None) != (scaled_value is None):
                    raise BundleError(f"aerosol {scale_field} and {value_field} must both be present or both null")
                if (scale_factor is None) != missing:
                    raise BundleError(f"aerosol {value_field} must be null exactly when {type_field} is missing")
                if missing:
                    continue
                if (
                    not isinstance(scale_factor, int)
                    or isinstance(scale_factor, bool)
                    or not -127 <= scale_factor <= 127
                    or not isinstance(scaled_value, int)
                    or isinstance(scaled_value, bool)
                    or not 0 <= scaled_value <= 0xFFFFFFFE
                ):
                    raise BundleError(f"aerosol {value_field} is invalid")

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


def metadata_json_bytes(metadata: dict[str, Any]) -> bytes:
    """The metadata JSON exactly as a container embeds it: compact, raw
    UTF-8. A store written from a build's codes takes its ``xue`` block from
    these bytes, so it carries the very document a container would."""
    return json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def time_axis(metadata: dict[str, Any]) -> tuple[int, list[int]]:
    """A metadata document's axis unit and frame offsets, read by the same
    rules a bundle's embedded metadata is (:meth:`BundleMetadata._parse_time_axis`)."""
    time_info = metadata["time"]
    unit_seconds, offsets, _version = BundleMetadata._parse_time_axis(
        time_info, time_info["frameCount"], metadata["schemaVersion"]
    )
    return unit_seconds, offsets


# `xuebuild.indicators.store` builds the validator under this name.
Bundle = BundleMetadata


def parse_metadata(metadata: dict[str, Any]) -> BundleMetadata:
    """Validate a metadata document as a container would embed it."""
    return BundleMetadata(metadata_json_bytes(metadata))

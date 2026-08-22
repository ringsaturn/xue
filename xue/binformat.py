"""Xue v1 container serialization, validation, and reference decoding.

This module is the Python source of truth for the binary layout defined in
docs/format.md. The writer produces complete files, and ``Bundle`` is the
reference decoder used by ``verify-bin`` and by cross-language golden tests
against the Rust implementation.
"""

from __future__ import annotations

import json
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
HEADER_SIZE = 80
INDEX_MAGIC = b"IDX1"
INDEX_HEADER_SIZE = 16
ENTRY_SIZE = 40
INDEX_VERSION = 1
NO_DEPENDENCY = 0xFFFF

PREDICTOR_RAW = 0
PREDICTOR_ANCHOR = 1
PREDICTOR_PREVIOUS = 2
PREDICTOR_ZERO = 3
PREDICTORS = {PREDICTOR_RAW, PREDICTOR_ANCHOR, PREDICTOR_PREVIOUS, PREDICTOR_ZERO}

COMPRESSION_NONE = 0
COMPRESSION_ZSTD = 1
COMPRESSION_ZSTD_DICT = 2
COMPRESSIONS = {COMPRESSION_NONE, COMPRESSION_ZSTD, COMPRESSION_ZSTD_DICT}

FLAG_ZSTD_CHECKSUM = 0x01

_HEADER_STRUCT = struct.Struct("<8sHHIQQQQQQQQ")
_INDEX_HEADER_STRUCT = struct.Struct("<4sHHII")
_ENTRY_STRUCT = struct.Struct("<BBBBHHHHIQIIBB6s")


def align8(value: int) -> int:
    return (value + 7) // 8 * 8


def crc32_plane(plane: bytes | np.ndarray) -> int:
    return zlib.crc32(bytes(plane)) & 0xFFFFFFFF


@dataclass(frozen=True)
class PlaneEntry:
    variable_id: int
    predictor: int
    compression: int
    flags: int
    forecast_hour: int
    dependency_hour: int
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
            self.forecast_hour,
            self.dependency_hour,
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
            forecast_hour=fields[4],
            dependency_hour=fields[5],
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

    entries_sorted = sorted(entries, key=lambda entry: (entry.variable_id, entry.forecast_hour))
    if len({(entry.variable_id, entry.forecast_hour) for entry in entries_sorted}) != len(entries_sorted):
        raise BundleError("duplicate (variableId, forecastHour) entries")

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

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(output)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


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
        self._parse_header()
        self._parse_metadata()
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
        if version != VERSION:
            raise BundleError(f"unsupported Xue version {version}")
        if header_size != HEADER_SIZE:
            raise BundleError("headerSize must be 80 for v1")
        if flags != 0:
            raise BundleError("header flags must be 0 for v1")
        if file_size != len(self.data):
            raise BundleError(f"header fileSize {file_size} does not match actual length {len(self.data)}")
        if metadata_offset != HEADER_SIZE:
            raise BundleError("metadataOffset must be 80 for v1")
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
        if schema_version not in (1, 2):
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
        first_hour = time_info.get("firstForecastHour")
        if (
            not isinstance(frame_count, int)
            or not isinstance(first_hour, int)
            or frame_count <= 0
            or first_hour < 0
        ):
            raise BundleError("metadata time description is invalid")
        forecast_hours = self._parse_time_axis(time_info, schema_version, frame_count, first_hour)
        variable_ids: dict[int, str] = {}
        for variable in variables:
            if not isinstance(variable, dict):
                raise BundleError("metadata variable must be an object")
            numeric_id = variable.get("numericId")
            name = variable.get("id")
            if not isinstance(numeric_id, int) or not 1 <= numeric_id <= 255 or not isinstance(name, str):
                raise BundleError("metadata variable numericId or id is invalid")
            if numeric_id in variable_ids:
                raise BundleError("metadata contains duplicate variable numericId values")
            variable_ids[numeric_id] = name
        if not variable_ids:
            raise BundleError("metadata must declare at least one variable")

        self.metadata = metadata
        self.width = width
        self.height = height
        self.plane_length = width * height
        self.frame_count = frame_count
        self.forecast_hours = forecast_hours
        self._hour_index = {hour: index for index, hour in enumerate(forecast_hours)}
        self.variable_ids = variable_ids

    @staticmethod
    def _parse_time_axis(
        time_info: dict[str, Any], schema_version: int, frame_count: int, first_hour: int
    ) -> list[int]:
        """The frame list from the ``time`` block. Exactly one of ``stepHours``
        (a uniform axis, schemaVersion 1) and ``hours`` (a mixed-step axis
        listed outright, schemaVersion 2) must be present, and the declared
        schema version must be the lowest able to express the axis — every
        axis has exactly one valid encoding (docs/format.md)."""
        step_hours = time_info.get("stepHours")
        listed_hours = time_info.get("hours")
        if (step_hours is None) == (listed_hours is None):
            raise BundleError("metadata time must declare exactly one of stepHours and hours")
        if step_hours is not None:
            if schema_version != 1:
                raise BundleError("a uniform time axis must declare schemaVersion 1")
            if not isinstance(step_hours, int) or step_hours <= 0:
                raise BundleError("metadata stepHours is invalid")
            if first_hour + (frame_count - 1) * step_hours >= NO_DEPENDENCY:
                raise BundleError("forecast hours exceed the u16 range")
            return [first_hour + index * step_hours for index in range(frame_count)]
        if schema_version != 2:
            raise BundleError("an explicit hours axis requires schemaVersion 2")
        if (
            not isinstance(listed_hours, list)
            or len(listed_hours) != frame_count
            or not all(isinstance(hour, int) for hour in listed_hours)
            or listed_hours[0] != first_hour
            or any(after <= before for before, after in zip(listed_hours, listed_hours[1:]))
            or listed_hours[-1] >= NO_DEPENDENCY
        ):
            raise BundleError("metadata hours axis is invalid")
        if len({after - before for before, after in zip(listed_hours, listed_hours[1:])}) < 2:
            raise BundleError("a uniform hours axis must be encoded as stepHours")
        return list(listed_hours)

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
        self.entry_map = {(entry.variable_id, entry.forecast_hour): entry for entry in entries}

    def _validate_entries(self) -> None:
        if len(self.entry_map) != len(self.entries):
            raise BundleError("duplicate (variableId, forecastHour) index entries")
        ordering = [(entry.variable_id, entry.forecast_hour) for entry in self.entries]
        if ordering != sorted(ordering):
            raise BundleError("index entries must be sorted by (variableId, forecastHour)")
        for variable_id in self.variable_ids:
            hours = sorted(hour for vid, hour in self.entry_map if vid == variable_id)
            if hours != self.forecast_hours:
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
                if entry.dependency_hour != NO_DEPENDENCY:
                    raise BundleError("RAW and ZERO entries must have dependencyHour 65535")
                continue
            if entry.predictor == PREDICTOR_ANCHOR:
                dependency_hour = entry.dependency_hour
            else:
                # PREVIOUS references the preceding frame on the time axis,
                # carried explicitly in dependencyHour (never the sentinel).
                index = self._hour_index.get(entry.forecast_hour)
                if index is None or index == 0:
                    raise BundleError("PREVIOUS entry has no preceding frame on the time axis")
                dependency_hour = self.forecast_hours[index - 1]
                if entry.dependency_hour != dependency_hour:
                    raise BundleError("PREVIOUS entry dependencyHour must reference the preceding frame on the time axis")
            dependency = self.entry_map.get((entry.variable_id, dependency_hour))
            if dependency is None:
                raise BundleError("entry depends on a plane that does not exist")
            if dependency.group_id != entry.group_id:
                raise BundleError("dependencies must stay inside the same temporal group")
        for key in self.entry_map:
            self._dependency_chain(key)

    def _dependency_hour(self, entry: PlaneEntry) -> int | None:
        # ANCHOR and PREVIOUS both carry their dependency explicitly;
        # _validate_entries has pinned PREVIOUS to the preceding axis frame.
        if entry.predictor in (PREDICTOR_ANCHOR, PREDICTOR_PREVIOUS):
            return entry.dependency_hour
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
            dependency = self._dependency_hour(entry)
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

    def decode_plane(self, variable_id: int, forecast_hour: int) -> np.ndarray:
        key = (variable_id, forecast_hour)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        entry = self.entry_map.get(key)
        if entry is None:
            raise BundleError(f"no plane for variable {variable_id} hour {forecast_hour}")
        if entry.predictor == PREDICTOR_ZERO:
            plane = np.zeros(self.plane_length, dtype=np.uint8)
        else:
            payload = np.frombuffer(self._payload(entry), dtype=np.uint8)
            if entry.predictor == PREDICTOR_RAW:
                plane = payload
            else:
                dependency_hour = self._dependency_hour(entry)
                assert dependency_hour is not None
                base = self.decode_plane(variable_id, dependency_hour)
                plane = (payload + base).astype(np.uint8)
        if crc32_plane(plane) != entry.crc32:
            raise BundleError(f"plane CRC32 mismatch for variable {variable_id} hour {forecast_hour}")
        if int(plane.min()) != entry.minimum_code or int(plane.max()) != entry.maximum_code:
            raise BundleError(f"plane code range mismatch for variable {variable_id} hour {forecast_hour}")
        self._cache[key] = plane
        return plane

    def verify_all(self, executor: ThreadPoolExecutor | None = None) -> None:
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


def read_bundle(path: Path) -> Bundle:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise BundleError(f"cannot read bundle: {path}: {exc}") from exc
    return Bundle(data)

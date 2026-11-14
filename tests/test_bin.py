from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from tests._support import requires_native
from xuebuild import binformat, temporal, zstdcli
from xuebuild.binformat import (
    COMPRESSION_NONE,
    COMPRESSION_ZSTD,
    NO_DEPENDENCY,
    PREDICTOR_ANCHOR,
    PREDICTOR_PREVIOUS,
    PREDICTOR_RAW,
    ChunkPayload,
    GroupEntry,
    PlaneEntry,
    PlanePayload,
    TileGeometry,
    VariableEntry,
    align8,
    crc32_plane,
    write_bundle,
    write_bundle_v2,
)
from xuebuild.errors import BundleError, ConversionError, ManifestError
from xuebuild.manifest import build_bin_manifest, validate_bin_manifest
from xuebuild.quantize import (
    COMPACT_PRECIPITATION,
    COMPACT_TEMPERATURE,
    QUALITY_PRECIPITATION,
    QUALITY_TEMPERATURE,
)


def read_back(path: Path):
    """A container the writer made, opened by the wheel's decoder."""
    import xue  # noqa: PLC0415 - the wheel; its tests skip without it

    return xue.Bundle.open(path)


def round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def reference_quantize_temperature(value: float) -> int:
    clamped = min(50.0, max(-60.0, value))
    return round_half_up((clamped + 60.0) / 0.5)


def reference_quantize_prate(value: float) -> int:
    if value < 0.01:
        return 0
    if value > 128.0:
        return 254
    lo = math.log1p(0.01 / 0.05)
    hi = math.log1p(128.0 / 0.05)
    unit = (math.log1p(value / 0.05) - lo) / (hi - lo)
    return 1 + round_half_up(252 * unit)


class TemperatureQuantizeTests(unittest.TestCase):
    def test_bounds_and_clamping(self) -> None:
        values = np.array([-100.0, -60.0, -59.75, 0.0, 49.99, 50.0, 100.0])
        codes = QUALITY_TEMPERATURE.quantize(values)
        self.assertEqual(codes.tolist(), [0, 0, 1, 120, 220, 220, 220])

    def test_round_half_up_not_half_even(self) -> None:
        # -59.75 sits exactly on a half step: (0.25 / 0.5) + 0.5 = 1.0.
        values = np.array([-59.75, -59.25, 0.25])
        self.assertEqual(QUALITY_TEMPERATURE.quantize(values).tolist(), [1, 2, 121])

    def test_matches_scalar_reference(self) -> None:
        rng = np.random.default_rng(7)
        values = rng.uniform(-70, 60, 4096)
        codes = QUALITY_TEMPERATURE.quantize(values)
        expected = [reference_quantize_temperature(float(value)) for value in values]
        self.assertEqual(codes.tolist(), expected)

    def test_round_trip_error_bound(self) -> None:
        rng = np.random.default_rng(11)
        values = rng.uniform(-60, 50, 4096)
        decoded = QUALITY_TEMPERATURE.decode(QUALITY_TEMPERATURE.quantize(values))
        self.assertLessEqual(float(np.abs(decoded - values).max()), 0.25 + 1e-9)

    def test_nonfinite_rejected(self) -> None:
        with self.assertRaises(ConversionError):
            QUALITY_TEMPERATURE.quantize(np.array([0.0, float("nan")]))

    def test_compact_profile(self) -> None:
        self.assertEqual(COMPACT_TEMPERATURE.maximum_code, 110)
        values = np.array([-60.0, 50.0, -9.5])
        self.assertEqual(COMPACT_TEMPERATURE.quantize(values).tolist(), [0, 110, 51])


class PrecipitationQuantizeTests(unittest.TestCase):
    def test_special_codes(self) -> None:
        values = np.array([0.0, 0.0099, 0.01, 128.0, 128.0001, 500.0])
        codes = QUALITY_PRECIPITATION.quantize(values)
        self.assertEqual(codes[0], 0)
        self.assertEqual(codes[1], 0)
        self.assertEqual(codes[2], 1)
        self.assertEqual(codes[3], 253)
        self.assertEqual(codes[4], 254)
        self.assertEqual(codes[5], 254)

    def test_matches_scalar_reference(self) -> None:
        rng = np.random.default_rng(3)
        values = np.concatenate([rng.uniform(0, 0.02, 512), rng.uniform(0.02, 140, 2048)])
        codes = QUALITY_PRECIPITATION.quantize(values)
        expected = [reference_quantize_prate(float(value)) for value in values]
        self.assertEqual(codes.tolist(), expected)

    def test_decode_strictly_increasing_including_overflow(self) -> None:
        codes = np.arange(0, 255, dtype=np.uint8)
        decoded = QUALITY_PRECIPITATION.decode(codes)
        self.assertTrue(np.all(np.diff(decoded) > 0))
        self.assertAlmostEqual(float(decoded[253]), 128.0, places=9)
        self.assertGreater(float(decoded[254]), 128.0)

    def test_nodata_rejected(self) -> None:
        with self.assertRaises(ConversionError):
            QUALITY_PRECIPITATION.decode(np.array([255], dtype=np.uint8))
        with self.assertRaises(ConversionError):
            QUALITY_PRECIPITATION.quantize(np.array([float("inf")]))

    def test_compact_codebook(self) -> None:
        values = np.array([0.0, 0.01, 128.0, 129.0])
        codes = COMPACT_PRECIPITATION.quantize(values)
        self.assertEqual(codes.tolist(), [0, 1, 125, 126])

    def test_balanced_profile_mixes_quality_temperature_with_compact_precipitation(self) -> None:
        from xuebuild.quantize import PROFILES

        self.assertIs(PROFILES["balanced"]["tmp2m"], QUALITY_TEMPERATURE)
        self.assertIs(PROFILES["balanced"]["prate"], COMPACT_PRECIPITATION)


class WindCodebookTests(unittest.TestCase):
    """Symmetric linear codebooks for the 10 m wind."""

    def test_symmetric_range_and_round_trip(self) -> None:
        from xuebuild.quantize import COMPACT_WIND, QUALITY_WIND

        self.assertEqual(QUALITY_WIND.maximum_code, 254)
        self.assertEqual(COMPACT_WIND.maximum_code, 127)
        values = np.array([-80.0, -63.5, -12.34, 0.0, 5.25, 63.5, 90.0])
        codes = QUALITY_WIND.quantize(values)
        decoded = QUALITY_WIND.decode(codes)
        inside = (values >= -63.5) & (values <= 63.5)
        self.assertTrue(np.all(np.abs(decoded[inside] - values[inside]) <= 0.25 + 1e-9))
        # Out-of-range extremes clamp to the codebook edges like temperature.
        self.assertEqual(decoded[0], -63.5)
        self.assertEqual(decoded[-1], 63.5)
        # Zero wind is representable exactly (code 127 decodes to 0.0).
        self.assertEqual(QUALITY_WIND.decode(QUALITY_WIND.quantize(np.array([0.0])))[0], 0.0)

    def test_every_profile_covers_both_components(self) -> None:
        from xuebuild.quantize import PROFILES

        for name, profile in PROFILES.items():
            for component in ("ugrd10m", "vgrd10m"):
                self.assertIn(component, profile, name)
                self.assertEqual(profile[component].nodata_code, 255)


class TemporalTests(unittest.TestCase):
    def test_residual_round_trip_with_wraparound(self) -> None:
        current = np.array([0, 255, 10, 128], dtype=np.uint8)
        base = np.array([255, 0, 200, 128], dtype=np.uint8)
        residual = temporal.encode_residual(current, base)
        self.assertEqual(residual.tolist(), [1, 255, 66, 0])
        self.assertEqual(temporal.decode_residual(residual, base).tolist(), current.tolist())

    def test_grouping_121_frames(self) -> None:
        groups = temporal.group_forecast_hours(list(range(121)))
        self.assertEqual(len(groups), 21)
        self.assertEqual(groups[0], [0, 1, 2, 3, 4, 5])
        self.assertEqual(groups[-1], [120])
        self.assertEqual(temporal.anchor_hour(groups[0]), 3)
        self.assertEqual(temporal.anchor_hour(groups[-1]), 120)

    def test_segment_split(self) -> None:
        self.assertEqual(temporal.split_segments([0]), [[0]])
        self.assertEqual(temporal.split_segments([0, 3]), [[0, 3]])
        self.assertEqual(temporal.split_segments([0, 1, 2, 3, 6, 9]), [[0, 1, 2, 3], [6, 9]])
        # The GFS 240-hour axis: 121 hourly frames, then 40 three-hourly.
        axis = list(range(121)) + list(range(123, 241, 3))
        segments = temporal.split_segments(axis)
        self.assertEqual([len(segment) for segment in segments], [121, 40])
        self.assertEqual(segments[1][0], 123)
        self.assertEqual(segments[1][-1], 240)

    def test_grouping_never_straddles_a_segment_boundary(self) -> None:
        axis = list(range(121)) + list(range(123, 241, 3))
        groups = temporal.group_forecast_hours(axis)
        # 21 groups for the hourly segment (20x6 + 1), 7 for the three-hourly
        # (6x6 + 4); every group's internal step is constant.
        self.assertEqual(len(groups), 28)
        self.assertEqual(groups[20], [120])
        self.assertEqual(groups[21], [123, 126, 129, 132, 135, 138])
        self.assertEqual(groups[-1], [231, 234, 237, 240])
        for group in groups:
            steps = {after - before for before, after in zip(group, group[1:])}
            self.assertLessEqual(len(steps), 1, group)
        self.assertEqual([hour for group in groups for hour in group], axis)

    def test_length_mismatch_rejected(self) -> None:
        with self.assertRaises(ConversionError):
            temporal.encode_residual(np.zeros(3, dtype=np.uint8), np.zeros(4, dtype=np.uint8))


def synthetic_metadata(
    frame_count: int = 2,
    width: int = 4,
    height: int = 3,
    *,
    time: dict | None = None,
    schema_version: int = 1,
) -> dict:
    return {
        "schemaVersion": schema_version,
        "model": "GFS",
        "product": "pgrb2.0p25",
        "runTime": "2026-08-15T06:00:00Z",
        "time": time or {"firstForecastHour": 0, "stepHours": 1, "frameCount": frame_count},
        "grid": {
            "width": width,
            "height": height,
            "layout": "row-major",
            "rowOrder": "north-to-south",
            "columnOrder": "west-to-east",
            "firstLongitude": -180.0,
            "firstLatitude": 90.0,
            "longitudeStep": 0.25,
            "latitudeStep": -0.25,
            "wrapLongitude": False,
        },
        "variables": [
            {
                "numericId": 1,
                "id": "tmp2m",
                "label": "2 meter temperature",
                "unit": "°C",
                "quantization": {
                    "type": "linear",
                    "offset": -60.0,
                    "scale": 0.5,
                    "minimumCode": 0,
                    "maximumCode": 220,
                    "nodataCode": 255,
                },
            }
        ],
    }


def raw_entry(hour: int, plane: np.ndarray, payload: bytes, *, group_id: int = 0) -> PlaneEntry:
    return PlaneEntry(
        variable_id=1,
        predictor=PREDICTOR_RAW,
        compression=COMPRESSION_ZSTD,
        flags=binformat.FLAG_ZSTD_CHECKSUM,
        frame_offset=hour,
        dependency_offset=NO_DEPENDENCY,
        group_id=group_id,
        compressed_length=len(payload),
        data_offset=0,
        decoded_length=plane.size,
        crc32=crc32_plane(plane),
        minimum_code=int(plane.min()),
        maximum_code=int(plane.max()),
    )


def build_synthetic(path: Path) -> tuple[np.ndarray, np.ndarray]:
    plane0 = np.arange(12, dtype=np.uint8)
    plane1 = (plane0 + np.uint8(3)).astype(np.uint8)
    payload0 = zstdcli.compress(plane0.tobytes())
    residual = temporal.encode_residual(plane1, plane0)
    payload1 = zstdcli.compress(residual.tobytes())
    entry1 = PlaneEntry(
        variable_id=1,
        predictor=PREDICTOR_ANCHOR,
        compression=COMPRESSION_ZSTD,
        flags=binformat.FLAG_ZSTD_CHECKSUM,
        frame_offset=1,
        dependency_offset=0,
        group_id=0,
        compressed_length=len(payload1),
        data_offset=0,
        decoded_length=plane1.size,
        crc32=crc32_plane(plane1),
        minimum_code=int(plane1.min()),
        maximum_code=int(plane1.max()),
    )
    write_bundle(
        path,
        synthetic_metadata(),
        [PlanePayload(raw_entry(0, plane0, payload0), payload0), PlanePayload(entry1, payload1)],
    )
    return plane0, plane1


@requires_native
class BinFormatTests(unittest.TestCase):
    """The v1 writer, read back through the wheel's decoder: every reader
    rejection is the Rust decoder's to test (`rust/xue`)."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bundle.xue"

    def test_write_and_read_round_trip(self) -> None:
        plane0, plane1 = build_synthetic(self.path)
        bundle = read_back(self.path)
        self.assertEqual((bundle.metadata["grid"]["width"], bundle.metadata["grid"]["height"]), (4, 3))
        self.assertEqual(bundle.frame_count, 2)
        self.assertEqual(bundle.decode(1, 0).tolist(), plane0.tolist())
        self.assertEqual(bundle.decode(1, 1).tolist(), plane1.tolist())

    def test_layout_offsets_and_alignment(self) -> None:
        build_synthetic(self.path)
        data = self.path.read_bytes()
        header = struct.unpack_from("<8sHHIQQQQQQQQ", data, 0)
        self.assertEqual(header[0], b"XUE\x00\x00\x00\x00\x00")
        self.assertEqual(header[1], 1)
        self.assertEqual(header[2], 80)
        self.assertEqual(header[4], len(data))
        self.assertEqual(header[5], 80)
        metadata_length = header[6]
        self.assertEqual(header[7], align8(80 + metadata_length))
        self.assertEqual(header[7] % 8, 0)
        self.assertEqual(header[9], align8(header[7] + header[8]))
        self.assertEqual(header[10], 0)
        self.assertEqual(header[11], 0)
        self.assertEqual(len(data) % 8, 0)

    def test_previous_predictor_round_trip(self) -> None:
        plane0 = np.arange(12, dtype=np.uint8)
        plane1 = (plane0 * np.uint8(3)).astype(np.uint8)
        payload0 = zstdcli.compress(plane0.tobytes())
        residual = temporal.encode_residual(plane1, plane0)
        payload1 = zstdcli.compress(residual.tobytes())
        entry1 = PlaneEntry(
            variable_id=1,
            predictor=PREDICTOR_PREVIOUS,
            compression=COMPRESSION_ZSTD,
            flags=binformat.FLAG_ZSTD_CHECKSUM,
            frame_offset=1,
            dependency_offset=0,
            group_id=0,
            compressed_length=len(payload1),
            data_offset=0,
            decoded_length=plane1.size,
            crc32=crc32_plane(plane1),
            minimum_code=int(plane1.min()),
            maximum_code=int(plane1.max()),
        )
        write_bundle(
            self.path,
            synthetic_metadata(),
            [PlanePayload(raw_entry(0, plane0, payload0), payload0), PlanePayload(entry1, payload1)],
        )
        self.assertEqual(read_back(self.path).decode(1, 1).tolist(), plane1.tolist())

    def test_uncompressed_payload_round_trip(self) -> None:
        plane = np.arange(12, dtype=np.uint8)
        payload = plane.tobytes()
        entry = PlaneEntry(
            variable_id=1,
            predictor=PREDICTOR_RAW,
            compression=COMPRESSION_NONE,
            flags=0,
            frame_offset=0,
            dependency_offset=NO_DEPENDENCY,
            group_id=0,
            compressed_length=len(payload),
            data_offset=0,
            decoded_length=plane.size,
            crc32=crc32_plane(plane),
            minimum_code=0,
            maximum_code=11,
        )
        write_bundle(self.path, synthetic_metadata(frame_count=1), [PlanePayload(entry, payload)])
        self.assertEqual(read_back(self.path).decode(1, 0).tolist(), plane.tolist())


def mixed_axis_metadata(hours: list[int], **kwargs) -> dict:
    return synthetic_metadata(
        time={"firstForecastHour": hours[0], "frameCount": len(hours), "hours": list(hours)},
        schema_version=2,
        **kwargs,
    )


# -- container v2 ------------------------------------------------------------

# A grid no tile size divides: 7 x 5 cells in 3 x 2 tiles leaves a one-cell
# last column and a one-row last row, so both clippings are always under test.
TILED_WIDTH, TILED_HEIGHT = 7, 5
TILE_WIDTH, TILE_HEIGHT = 3, 2
TILED_HOURS = [0, 1, 2, 3, 4, 5, 6, 7]


def tiled_planes() -> dict[int, dict[int, np.ndarray]]:
    points = TILED_WIDTH * TILED_HEIGHT
    return {
        hour: {1: ((np.arange(points, dtype=np.uint16) * 5 + hour * 11) % 251).astype(np.uint8)}
        for hour in TILED_HOURS
    }


def build_tiled(
    path: Path,
    *,
    predictor: int = PREDICTOR_PREVIOUS,
    group_length: int = 3,
) -> tuple[dict[int, dict[int, np.ndarray]], list[ChunkPayload]]:
    planes = tiled_planes()
    tiles = TileGeometry(TILED_WIDTH, TILED_HEIGHT, TILE_WIDTH, TILE_HEIGHT)
    variables, groups, raw = temporal.build_chunks(
        TILED_HOURS, planes, tiles, {1: predictor}, group_length=group_length
    )
    chunks = []
    for entry, stored in raw:
        payload = zstdcli.compress(stored.tobytes())
        chunks.append(ChunkPayload(replace(entry, compressed_length=len(payload)), payload))
    write_bundle_v2(
        path,
        synthetic_metadata(
            width=TILED_WIDTH,
            height=TILED_HEIGHT,
            frame_count=len(TILED_HOURS),
        ),
        tile_width=TILE_WIDTH,
        tile_height=TILE_HEIGHT,
        variables=variables,
        groups=groups,
        chunks=chunks,
    )
    return planes, chunks


class TileGeometryTests(unittest.TestCase):
    """The tiling is derived arithmetic, never stored, so both implementations
    must derive it identically — including the clipped last row and column the
    721-row production grid always has."""

    def test_production_grid_tiling(self) -> None:
        tiles = TileGeometry(1440, 721, 48, 52)
        self.assertEqual((tiles.columns, tiles.rows, tiles.count), (30, 14, 420))
        self.assertEqual(tiles.shape(0), (52, 48))
        self.assertEqual(tiles.shape(419), (45, 48))
        self.assertEqual(tiles.origin(419), (676, 1392))
        area = sum(height * width for height, width in (tiles.shape(t) for t in range(tiles.count)))
        self.assertEqual(area, 1440 * 721)

    def test_every_cell_lands_in_exactly_one_tile(self) -> None:
        tiles = TileGeometry(TILED_WIDTH, TILED_HEIGHT, TILE_WIDTH, TILE_HEIGHT)
        for row in range(TILED_HEIGHT):
            for column in range(TILED_WIDTH):
                tile = tiles.tile_of(row, column)
                origin_row, origin_column = tiles.origin(tile)
                height, width = tiles.shape(tile)
                self.assertTrue(origin_row <= row < origin_row + height)
                self.assertTrue(origin_column <= column < origin_column + width)

    def test_rectangles_and_bounds(self) -> None:
        tiles = TileGeometry(TILED_WIDTH, TILED_HEIGHT, TILE_WIDTH, TILE_HEIGHT)
        self.assertEqual(tiles.tiles_in_rect(0, 0, TILED_HEIGHT, TILED_WIDTH), list(range(tiles.count)))
        self.assertEqual(tiles.tiles_in_rect(3, 4, 1, 1), [tiles.tile_of(3, 4)])
        with self.assertRaises(BundleError):
            tiles.tile_of(TILED_HEIGHT, 0)
        with self.assertRaises(BundleError):
            TileGeometry(16, 8, 0, 4)
        with self.assertRaises(BundleError):
            TileGeometry(16, 8, 17, 4)


class TiledContainerTests(unittest.TestCase):
    """The v2 writer's bytes. Reading them back is the wheel's decoder's,
    and its rejections the Rust decoder's to test (`rust/xue`)."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "tiled.xue"

    @requires_native
    def test_round_trip_planes_and_series(self) -> None:
        for predictor in (PREDICTOR_PREVIOUS, PREDICTOR_RAW):
            with self.subTest(predictor=predictor):
                planes, _chunks = build_tiled(self.path, predictor=predictor)
                bundle = read_back(self.path)
                for hour in TILED_HOURS:
                    self.assertEqual(bundle.decode(1, hour).tolist(), planes[hour][1].tolist())
                for row in range(TILED_HEIGHT):
                    for column in range(TILED_WIDTH):
                        expected = [int(planes[hour][1][row * TILED_WIDTH + column]) for hour in TILED_HOURS]
                        self.assertEqual(bundle.series(1, column, row).tolist(), expected)

    def test_layout_offsets_and_index_size(self) -> None:
        build_tiled(self.path)
        data = self.path.read_bytes()
        header = struct.unpack_from("<8sHHIQQQQQQQQ", data, 0)
        self.assertEqual(header[1], 2)
        self.assertEqual(header[4], len(data))
        self.assertEqual(len(data) % 8, 0)
        index = struct.unpack_from("<4sHHHHHBBIIQ", data, header[7])
        magic, version, header_size, tile_width, tile_height, groups, variables, _, chunks, r0, r1 = index
        self.assertEqual((magic, version, header_size), (b"IDX2", 2, 32))
        self.assertEqual((tile_width, tile_height), (TILE_WIDTH, TILE_HEIGHT))
        self.assertEqual((r0, r1), (0, 0))
        tiles = TileGeometry(TILED_WIDTH, TILED_HEIGHT, TILE_WIDTH, TILE_HEIGHT)
        self.assertEqual(chunks, groups * tiles.count * variables)
        self.assertEqual(header[8], 32 + 4 * variables + 4 * groups + 8 * chunks)

    def test_chunk_offsets_are_the_prefix_sums(self) -> None:
        """Offsets are not stored, so the writer must lay the payloads out
        back to back from ``dataOffset`` in index order: a reader computes
        each one as the prefix sum of the lengths before it."""
        _planes, chunks = build_tiled(self.path)
        data = self.path.read_bytes()
        header = struct.unpack_from("<8sHHIQQQQQQQQ", data, 0)
        index = struct.unpack_from("<4sHHHHHBBIIQ", data, header[7])
        groups, variables, count = index[5], index[6], index[8]
        self.assertEqual(count, len(chunks))
        table = header[7] + 32 + 4 * variables + 4 * groups
        cursor = header[9]
        for position, chunk in enumerate(chunks):
            self.assertEqual(data[table + 8 * position : table + 8 * position + 8], chunk.entry.pack())
            self.assertEqual(data[cursor : cursor + chunk.entry.compressed_length], chunk.payload)
            cursor += chunk.entry.compressed_length
        self.assertEqual(align8(cursor), len(data))

    def test_writer_rejects_a_broken_partition(self) -> None:
        planes = tiled_planes()
        tiles = TileGeometry(TILED_WIDTH, TILED_HEIGHT, TILE_WIDTH, TILE_HEIGHT)
        variables, groups, raw = temporal.build_chunks(TILED_HOURS, planes, tiles, {1: PREDICTOR_RAW})
        chunks = [
            ChunkPayload(replace(entry, compressed_length=len(stored.tobytes())), stored.tobytes())
            for entry, stored in raw
        ]
        metadata = synthetic_metadata(
            width=TILED_WIDTH, height=TILED_HEIGHT, frame_count=len(TILED_HOURS)
        )
        arguments = dict(
            tile_width=TILE_WIDTH,
            tile_height=TILE_HEIGHT,
            variables=variables,
            groups=groups,
            chunks=chunks,
        )
        with self.assertRaises(BundleError):
            write_bundle_v2(self.path, metadata, **{**arguments, "chunks": chunks[:-1]})
        with self.assertRaises(BundleError):
            write_bundle_v2(
                self.path, metadata, **{**arguments, "groups": [GroupEntry(0, len(TILED_HOURS) - 1)]}
            )
        with self.assertRaises(BundleError):
            write_bundle_v2(
                self.path,
                metadata,
                **{**arguments, "variables": [VariableEntry(1, PREDICTOR_ANCHOR)]},
            )


class SchemaV2Tests(unittest.TestCase):
    """Mixed-step time axes (metadata schemaVersion 2, docs/bundle.md)."""

    HOURS = [0, 1, 2, 3, 6, 9]

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bundle.xue"

    def _planes(self) -> dict[int, np.ndarray]:
        return {hour: (np.arange(12) + hour).astype(np.uint8) for hour in self.HOURS}

    def _write(self, metadata: dict, previous_dependency: dict[int, int] | None = None) -> dict[int, np.ndarray]:
        """One RAW anchor per segment-aligned group, ANCHOR residuals inside
        (or PREVIOUS entries where ``previous_dependency`` maps an hour to its
        declared dependencyOffset). The anchor is each group's first frame —
        the encoder's floor(n/2) pick is not a decode-time invariant, and a
        leading anchor lets a later frame carry PREVIOUS inside its group."""
        planes = self._planes()
        payloads: list[PlanePayload] = []
        for group_id, group in enumerate(temporal.group_forecast_hours(self.HOURS)):
            anchor = group[0]
            for hour in group:
                plane = planes[hour]
                if hour == anchor:
                    payload = zstdcli.compress(plane.tobytes())
                    payloads.append(PlanePayload(raw_entry(hour, plane, payload, group_id=group_id), payload))
                    continue
                if previous_dependency and hour in previous_dependency:
                    predictor = PREDICTOR_PREVIOUS
                    dependency = previous_dependency[hour]
                    base_hour = self.HOURS[self.HOURS.index(hour) - 1]
                else:
                    predictor = PREDICTOR_ANCHOR
                    dependency = base_hour = anchor
                residual = temporal.encode_residual(plane, planes[base_hour])
                payload = zstdcli.compress(residual.tobytes())
                entry = PlaneEntry(
                    variable_id=1,
                    predictor=predictor,
                    compression=COMPRESSION_ZSTD,
                    flags=binformat.FLAG_ZSTD_CHECKSUM,
                    frame_offset=hour,
                    dependency_offset=dependency,
                    group_id=group_id,
                    compressed_length=len(payload),
                    data_offset=0,
                    decoded_length=plane.size,
                    crc32=crc32_plane(plane),
                    minimum_code=int(plane.min()),
                    maximum_code=int(plane.max()),
                )
                payloads.append(PlanePayload(entry, payload))
        write_bundle(self.path, metadata, payloads)
        return planes

    @requires_native
    def test_mixed_axis_round_trip(self) -> None:
        planes = self._write(mixed_axis_metadata(self.HOURS))
        bundle = read_back(self.path)
        self.assertEqual(list(bundle.frame_offsets), self.HOURS)
        for hour, plane in planes.items():
            self.assertEqual(bundle.decode(1, hour).tolist(), plane.tolist())

    @requires_native
    def test_previous_predictor_uses_the_axis_not_hour_minus_one(self) -> None:
        # The frame preceding f009 is f006 on this axis; a PREVIOUS entry must
        # carry exactly that hour.
        planes = self._write(mixed_axis_metadata(self.HOURS), previous_dependency={9: 6})
        self.assertEqual(read_back(self.path).decode(1, 9).tolist(), planes[9].tolist())

    def _reject(self, metadata: dict) -> None:
        with self.assertRaises(BundleError):
            binformat.parse_metadata(metadata)

    def test_axis_validation(self) -> None:
        hours = self.HOURS
        base = mixed_axis_metadata(hours)
        # Declaring both stepHours and hours, or neither.
        both = json.loads(json.dumps(base))
        both["time"]["stepHours"] = 1
        self._reject(both)
        neither = json.loads(json.dumps(base))
        del neither["time"]["hours"]
        self._reject(neither)
        # hours in a schemaVersion 1 file.
        v1_hours = json.loads(json.dumps(base))
        v1_hours["schemaVersion"] = 1
        self._reject(v1_hours)
        # Overdeclared: schemaVersion 2 with a uniform stepHours axis.
        overdeclared = synthetic_metadata(frame_count=len(hours), schema_version=2)
        overdeclared["time"]["frameCount"] = len(hours)
        self._reject(overdeclared)
        # A uniform hours array has exactly one encoding: stepHours.
        self._reject(mixed_axis_metadata([0, 1, 2, 3, 4, 5]))
        # Not strictly increasing, wrong length, wrong first element.
        self._reject(mixed_axis_metadata([0, 1, 1, 3, 6, 9]))
        wrong_count = mixed_axis_metadata(hours)
        wrong_count["time"]["frameCount"] = len(hours) - 1
        self._reject(wrong_count)
        wrong_first = mixed_axis_metadata(hours)
        wrong_first["time"]["firstForecastHour"] = 1
        self._reject(wrong_first)
        # Hours above the u16 payload range (65535 is the sentinel).
        self._reject(mixed_axis_metadata([0, 1, 2, 3, 6, 65535]))

    def test_uniform_axis_u16_bound(self) -> None:
        metadata = synthetic_metadata(frame_count=2)
        metadata["time"] = {"firstForecastHour": 65534, "stepHours": 1, "frameCount": 2}
        plane = np.arange(12, dtype=np.uint8)
        payload = zstdcli.compress(plane.tobytes())
        with self.assertRaises(BundleError):
            write_bundle(self.path, metadata, [PlanePayload(raw_entry(0, plane, payload), payload)])
            binformat.parse_metadata(metadata)


class SourceAxisTests(unittest.TestCase):
    def test_gfs_axis(self) -> None:
        from xuebuild.sources import source_spec

        spec = source_spec("gfs")
        self.assertEqual(spec.forecast_hours(0), [0])
        self.assertEqual(spec.forecast_hours(120), list(range(121)))
        axis = spec.forecast_hours(240)
        self.assertEqual(len(axis), 161)
        self.assertEqual(axis[:121], list(range(121)))
        self.assertEqual(axis[121:], list(range(123, 241, 3)))

    def test_ecmwf_axis(self) -> None:
        from xuebuild.errors import DownloadError
        from xuebuild.sources import source_spec

        spec = source_spec("ecmwf")
        self.assertEqual(spec.forecast_hours(120), list(range(0, 121, 3)))
        axis = spec.forecast_hours(240)
        self.assertEqual(axis, list(range(0, 145, 3)) + list(range(150, 241, 6)))
        for off_axis in (1, 121, 147, 241, 300):
            with self.assertRaises(DownloadError):
                spec.forecast_hours(off_axis)

    def test_sflux_matches_gfs(self) -> None:
        from xuebuild.sources import source_spec

        self.assertEqual(source_spec("sflux").forecast_hours(240), source_spec("gfs").forecast_hours(240))


class SourceLadderTests(unittest.TestCase):
    """Every source's resolution ladder is a named, ascending run of powers
    of two — what the tier names and the repeated-halving tile rule rely on,
    and what the native encoder's source table mirrors."""

    def test_every_source_s_factors_are_ascending_powers_of_two_with_a_tier_name(self) -> None:
        from xuebuild.ladder import VARIANT_TIERS, variant_tier
        from xuebuild.sources import SOURCES

        for source_id, spec in SOURCES.items():
            with self.subTest(source=source_id):
                factors = spec.variant_factors
                self.assertIsInstance(factors, tuple)
                # The WOOF nest is under 5000 cells a plane, an EFAS case
                # a box of a few thousand, a CMA-CW/DUST plane a quarter
                # of a GFS one and the HKO nowcast 124 × 121 cells: they
                # publish no reduced tier; every other
                # grid has at least the half.
                self.assertEqual(bool(factors), source_id not in ("woof", "efas", "efaswb", "era5", "cams", "cmadust", "hkonowcast"), "a source publishes at least the half tier")
                self.assertEqual(list(factors), sorted(set(factors)), "ascending, no repeats")
                for factor in factors:
                    self.assertGreaterEqual(factor, 2)
                    self.assertEqual(factor & (factor - 1), 0, f"{factor} is not a power of two")
                    self.assertIn(factor, VARIANT_TIERS)
                    self.assertEqual(variant_tier(factor), VARIANT_TIERS[factor])

    def test_the_satellite_disks_take_three_rungs_and_every_other_grid_the_half(self) -> None:
        from xuebuild.sources import SOURCES

        ladders = {source_id: spec.variant_factors for source_id, spec in SOURCES.items()}
        satellites = {source_id for source_id, spec in SOURCES.items() if spec.platform is not None}
        self.assertEqual(satellites, {"himawari", "himawarijp", "goeseast", "goeswest", "meteosat"})
        for source_id, factors in ladders.items():
            with self.subTest(source=source_id):
                # A 0.1° plane (IFS HRES, the IFS open data at 0.1°) is 6.5 M
                # cells and a satellite disk 9 M, past the shell's frame
                # budget, so they take two rungs; every other grid the half.
                expected = (
                    (2, 4) if source_id in satellites | {"ifshres", "ifs"} else () if source_id in ("woof", "efas", "efaswb", "era5", "cams", "cmadust", "hkonowcast") else (2,)
                )
                self.assertEqual(factors, expected)


class TimeMetadataTests(unittest.TestCase):
    def test_uniform_axis_declares_a_frame_step(self) -> None:
        from xuebuild.metadata import time_metadata

        self.assertEqual(
            time_metadata(list(range(0, 121, 3)), 3600),
            {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 41, "frameStep": 3},
        )
        self.assertEqual(
            time_metadata([7], 3600),
            {"unitSeconds": 3600, "firstFrameOffset": 7, "frameCount": 1, "frameStep": 1},
        )

    def test_mixed_axis_lists_its_offsets(self) -> None:
        from xuebuild.metadata import time_metadata

        axis = list(range(121)) + list(range(123, 241, 3))
        self.assertEqual(
            time_metadata(axis, 3600),
            {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 161, "frameOffsets": axis},
        )

    def test_observation_axis_lists_its_gaps(self) -> None:
        """A missed publication is an ordinary step change, so the axis lists
        its offsets outright rather than pretending to a cadence it does not have."""
        from xuebuild.metadata import time_metadata

        axis = [0, 1, 2, 5, 6, 7]
        self.assertEqual(
            time_metadata(axis, 360),
            {"unitSeconds": 360, "firstFrameOffset": 0, "frameCount": 6, "frameOffsets": axis},
        )

    def test_the_axis_unit_is_the_coarsest_that_fits(self) -> None:
        """An hour for every forecast source, so their offsets stay their
        forecast hours; finer only when the data is."""
        from xuebuild.metadata import axis_unit_seconds, lead_hours

        self.assertEqual(axis_unit_seconds([0, 3600, 7200]), 3600)
        self.assertEqual(axis_unit_seconds([0, 10800, 21600]), 3600)
        self.assertEqual(axis_unit_seconds([0]), 3600)
        self.assertEqual(axis_unit_seconds([0, 360, 720, 1440]), 360)
        self.assertEqual(axis_unit_seconds([0, 1800, 3600]), 1800)
        # The manifest's coarse reach rounds up to a whole hour.
        self.assertEqual(lead_hours(2120, 360), 212)
        self.assertEqual(lead_hours(2121, 360), 213)
        self.assertEqual(lead_hours(240, 3600), 240)


class ParameterMetadataTests(unittest.TestCase):
    """Schema v3: every variable carries its GRIB2 identity (docs/bundle.md)."""

    def _metadata(self) -> dict:
        from xuebuild.metadata import build_metadata
        from xuebuild.grid import GridInfo

        grid = GridInfo(width=4, height=3, first_longitude=-180.0, first_latitude=90.0,
                        longitude_step=0.25, latitude_step=-0.25)
        return build_metadata(datetime(2026, 8, 15, 6, tzinfo=UTC), [0, 1], grid, "quality", ("tmp2m",))

    def _reject(self, metadata: dict) -> None:
        with self.assertRaises(BundleError):
            binformat.parse_metadata(metadata)

    def test_encoder_emits_the_grib2_identity(self) -> None:
        metadata = self._metadata()
        self.assertEqual(metadata["schemaVersion"], 3)
        self.assertEqual(
            metadata["variables"][0]["parameter"],
            {
                "discipline": 0,
                "parameterCategory": 0,
                "parameterNumber": 0,
                "typeOfFirstFixedSurface": 103,
                "scaleFactorOfFirstFixedSurface": 0,
                "scaledValueOfFirstFixedSurface": 2,
            },
        )
        self.assertEqual(binformat.parse_metadata(metadata).variable_ids, {1: "tmp2m"})

    def test_derived_rate_declares_its_statistical_process(self) -> None:
        """ECMWF and sflux publish prate as a mean over the step, not the
        instantaneous field GFS carries under the same parameter."""
        from xuebuild.metadata import build_metadata
        from xuebuild.grid import GridInfo
        from xuebuild.sources import source_spec

        grid = GridInfo(width=4, height=3, first_longitude=-180.0, first_latitude=90.0,
                        longitude_step=0.25, latitude_step=-0.25)
        for model, expected in (("gfs", None), ("ecmwf", 0), ("sflux", 0)):
            metadata = build_metadata(
                datetime(2026, 8, 15, 6, tzinfo=UTC), [3, 6], grid, "quality", ("prate",),
                source=source_spec(model),
            )
            parameter = metadata["variables"][0]["parameter"]
            self.assertEqual(parameter["parameterCategory"], 1)
            self.assertEqual(parameter["parameterNumber"], 7)
            self.assertEqual(parameter.get("typeOfStatisticalProcessing"), expected, model)

    def test_entire_atmosphere_surface_carries_no_value(self) -> None:
        from xuebuild.variables import variable_spec

        parameter = variable_spec("cref").parameter_metadata()
        self.assertEqual(parameter["typeOfFirstFixedSurface"], 10)
        self.assertIsNone(parameter["scaleFactorOfFirstFixedSurface"])
        self.assertIsNone(parameter["scaledValueOfFirstFixedSurface"])

    def test_version_3_requires_a_parameter_block(self) -> None:
        metadata = self._metadata()
        del metadata["variables"][0]["parameter"]
        self._reject(metadata)

    def test_parameter_block_requires_version_3(self) -> None:
        metadata = self._metadata()
        metadata["schemaVersion"] = 1
        self._reject(metadata)

    def test_declared_version_is_the_lowest_that_expresses_the_metadata(self) -> None:
        # A v3 file whose axis needs nothing beyond stepHours is still v3:
        # the parameter block, not the axis, sets the floor.
        metadata = self._metadata()
        self.assertIn("frameStep", metadata["time"])
        metadata["schemaVersion"] = 2
        self._reject(metadata)

    def test_invalid_parameter_blocks_rejected(self) -> None:
        for mutate in (
            lambda block: block.update(parameterNumber=256),
            lambda block: block.update(discipline="0"),
            lambda block: block.update(scaledValueOfFirstFixedSurface=None),
            lambda block: block.pop("scaleFactorOfFirstFixedSurface"),
            lambda block: block.update(typeOfStatisticalProcessing=-1),
            lambda block: block.update(levelValue=2),
        ):
            metadata = self._metadata()
            mutate(metadata["variables"][0]["parameter"])
            self._reject(metadata)

    BAND = {
        "satelliteSeries": 0,
        "satelliteNumber": 174,
        "instrumentType": 297,
        "scaleFactorOfCentralWaveNumber": 0,
        "scaledValueOfCentralWaveNumber": 96061,
    }
    PRODUCER = {"id": "shachen", "version": "0.3.1"}

    def test_band_and_producer_blocks_are_optional_beside_the_parameter(self) -> None:
        """The satellite widening of schema v3 (docs/bundle.md §"Band and
        Producer"): either block may sit beside ``parameter``, neither
        raises the version, and a file without them is unchanged."""
        metadata = self._metadata()
        variable = metadata["variables"][0]
        variable["band"] = dict(self.BAND)
        variable["producer"] = dict(self.PRODUCER)
        read = binformat.parse_metadata(metadata)
        self.assertEqual(read.variable_ids, {1: "tmp2m"})
        self.assertEqual(read.metadata["variables"][0]["band"], self.BAND)
        self.assertEqual(read.metadata["variables"][0]["producer"], self.PRODUCER)
        for block in ("band", "producer"):
            metadata = self._metadata()
            metadata["schemaVersion"] = 1
            del metadata["variables"][0]["parameter"]
            metadata["variables"][0][block] = dict(getattr(self, block.upper()))
            self._reject(metadata)

    def test_malformed_band_and_producer_blocks_rejected(self) -> None:
        incomplete = dict(self.BAND)
        del incomplete["instrumentType"]
        for block, broken in (
            ("band", None),
            ("band", incomplete),
            ("band", {**self.BAND, "satelliteNumber": 65536}),
            ("band", {**self.BAND, "satelliteNumber": "174"}),
            ("band", {**self.BAND, "satelliteNumber": True}),
            ("band", {**self.BAND, "channel": 13}),
            ("producer", None),
            ("producer", {"id": "shachen"}),
            ("producer", {"id": "Shachen", "version": "0.3.1"}),
            ("producer", {"id": "shachen", "version": ""}),
            ("producer", {**self.PRODUCER, "url": "x"}),
        ):
            metadata = self._metadata()
            metadata["variables"][0][block] = broken
            self._reject(metadata)

    def test_encoder_writes_a_source_band_beside_the_parameter(self) -> None:
        """No registered source images from a spacecraft yet; the block
        comes from ``SourceSpec.bands`` and lands between ``parameter`` and
        ``quantization``, where the native encoder puts it too."""
        from dataclasses import replace

        from xuebuild.metadata import build_metadata
        from xuebuild.grid import GridInfo
        from xuebuild.sources import SatelliteBand, source_spec

        grid = GridInfo(width=4, height=3, first_longitude=-180.0, first_latitude=90.0,
                        longitude_step=0.25, latitude_step=-0.25)
        band = SatelliteBand(satellite_series=0, satellite_number=174, instrument_type=297,
                             central_wavenumber=96061)
        source = replace(source_spec("cma"), bands=(("cref", band),))
        metadata = build_metadata(
            datetime(2026, 8, 15, 6, tzinfo=UTC), [0, 1], grid, "quality", ("cref",),
            source=source, unit_seconds=360,
        )
        variable = metadata["variables"][0]
        self.assertEqual(variable["band"], self.BAND)
        self.assertEqual(list(variable), ["numericId", "id", "label", "unit", "parameter", "band", "quantization"])
        self.assertEqual(binformat.parse_metadata(metadata).metadata["variables"][0]["band"], self.BAND)
        plain = build_metadata(
            datetime(2026, 8, 15, 6, tzinfo=UTC), [0, 1], grid, "quality", ("cref",),
            source=source_spec("cma"), unit_seconds=360,
        )
        self.assertNotIn("band", plain["variables"][0])


def variant_descriptor(variable: str) -> dict:
    return {
        "path": f"gfs.2026081506/{variable}.half.xue",
        "width": 720,
        "height": 361,
        "byteLength": 11_000_000,
        "crc32": "12345678",
        "bandwidth": 10_500_000,
    }


def manifest_bundles() -> list[dict]:
    return [
        {
            "variable": "tmp2m",
            "path": "gfs.2026081506/tmp2m.xue",
            "byteLength": 45_000_000,
            "crc32": "a1b2c3d4",
            "variants": [variant_descriptor("tmp2m")],
        },
        {"variable": "prate", "path": "gfs.2026081506/prate.xue", "byteLength": 25_000_000, "crc32": "d4c3b2a1"},
    ]


class DeaccumulationTests(unittest.TestCase):
    def test_rate_from_accumulations(self) -> None:
        from xuebuild.derive import deaccumulate_precipitation

        previous = np.array([0.0, 3.0, 6.0])
        current = np.array([3.0, 3.0, 5.5])  # last point dips: packing noise
        rate = deaccumulate_precipitation(current, previous, 3)
        np.testing.assert_allclose(rate, [1.0, 0.0, 0.0])

    def test_first_frame_has_zero_rate(self) -> None:
        from xuebuild.derive import deaccumulate_precipitation

        rate = deaccumulate_precipitation(np.array([1.0, 2.0]), None, 3)
        np.testing.assert_allclose(rate, [0.0, 0.0])


class BinManifestTests(unittest.TestCase):
    def test_build_and_validate(self) -> None:
        payload = build_bin_manifest(datetime(2026, 8, 15, 6, tzinfo=UTC), bundles=manifest_bundles())
        self.assertEqual(payload["schemaVersion"], 5)
        self.assertEqual([bundle["variable"] for bundle in payload["bundles"]], ["tmp2m", "prate"])
        self.assertEqual(payload["bundles"][0]["variants"][0]["width"], 720)
        self.assertNotIn("variants", payload["bundles"][1])
        validate_bin_manifest(payload)

    def test_ecmwf_model_and_product(self) -> None:
        payload = build_bin_manifest(
            datetime(2026, 8, 15, 0, tzinfo=UTC),
            bundles=manifest_bundles(),
            model="ECMWF",
            product="ifs-0p25",
        )
        validate_bin_manifest(payload)
        mismatched = json.loads(json.dumps(payload))
        mismatched["product"] = "pgrb2.0p25"
        with self.assertRaises(ManifestError):
            validate_bin_manifest(mismatched)
        unknown = json.loads(json.dumps(payload))
        unknown["model"] = "ICON"
        with self.assertRaises(ManifestError):
            validate_bin_manifest(unknown)

    def test_invalid_variant_descriptor_rejected(self) -> None:
        payload = build_bin_manifest(datetime(2026, 8, 15, 6, tzinfo=UTC), bundles=manifest_bundles())
        for mutation in (
            {"path": "gfs.2026081506/tmp2m.half.mp4"},
            {"path": "/absolute.xue"},
            {"path": "gfs.2026081506/tmp2m.xue"},  # duplicates the canonical path
            {"width": 0},
            {"height": -1},
            {"byteLength": 0},
            {"bandwidth": 0},
            {"crc32": "XYZ"},
        ):
            broken = json.loads(json.dumps(payload))
            broken["bundles"][0]["variants"][0].update(mutation)
            with self.assertRaises(ManifestError):
                validate_bin_manifest(broken)
        empty = json.loads(json.dumps(payload))
        empty["bundles"][0]["variants"] = []
        with self.assertRaises(ManifestError):
            validate_bin_manifest(empty)

    def test_zarr_descriptor_is_optional_and_validated(self) -> None:
        """The bundle's Zarr store, on the bundle and on a variant: the same
        path rules as the bundle, and no collision with any other artifact."""
        bundles = manifest_bundles()
        bundles[0]["zarr"] = {"path": "gfs.2026081506/tmp2m.zarr", "byteLength": 12_347_075, "crc32": "760cef95"}
        bundles[0]["variants"][0]["zarr"] = {
            "path": "gfs.2026081506/tmp2m.half.zarr",
            "byteLength": 4_000_000,
            "crc32": "0d3a2e6b",
        }
        payload = build_bin_manifest(datetime(2026, 8, 15, 6, tzinfo=UTC), bundles=bundles)
        self.assertEqual(payload["bundles"][0]["zarr"]["path"], "gfs.2026081506/tmp2m.zarr")
        self.assertEqual(payload["bundles"][0]["variants"][0]["zarr"]["crc32"], "0d3a2e6b")
        self.assertNotIn("zarr", payload["bundles"][1])
        validate_bin_manifest(payload)
        for mutation in (
            {"path": "gfs.2026081506/tmp2m.zip"},
            {"path": "/absolute.zarr"},
            {"path": "../escape.zarr"},
            {"path": "gfs.2026081506/tmp2m.xue"},  # the bundle's own path
            {"path": "gfs.2026081506/tmp2m.half.zarr"},  # the variant's store
            {"byteLength": 0},
            {"byteLength": "12"},
            {"crc32": "760CEF95"},
            {"crc32": "760c"},
        ):
            broken = json.loads(json.dumps(payload))
            broken["bundles"][0]["zarr"].update(mutation)
            with self.assertRaises(ManifestError, msg=str(mutation)):
                validate_bin_manifest(broken)
        for where in ("bundles", "variants"):
            broken = json.loads(json.dumps(payload))
            target = broken["bundles"][0] if where == "bundles" else broken["bundles"][0]["variants"][0]
            target["zarr"] = "gfs.2026081506/tmp2m.zarr"
            with self.assertRaises(ManifestError):
                validate_bin_manifest(broken)

    def test_invalid_bundle_rejected(self) -> None:
        payload = build_bin_manifest(datetime(2026, 8, 15, 6, tzinfo=UTC), bundles=manifest_bundles())
        for mutation in (
            {"path": "/absolute.xue"},
            {"path": "../escape.xue"},
            {"path": "bundle.pmtiles"},
            {"path": "gfs.2026081506/prate.xue"},
            {"variable": "unknown"},
            {"byteLength": 0},
            {"crc32": "XYZ"},
            {"crc32": "A1B2C3D4"},
        ):
            broken = json.loads(json.dumps(payload))
            broken["bundles"][0].update(mutation)
            with self.assertRaises(ManifestError):
                validate_bin_manifest(broken)

    def test_missing_core_variable_rejected_but_order_is_free(self) -> None:
        payload = build_bin_manifest(datetime(2026, 8, 15, 6, tzinfo=UTC), bundles=manifest_bundles())
        only_one = json.loads(json.dumps(payload))
        only_one["bundles"] = only_one["bundles"][:1]
        with self.assertRaises(ManifestError):
            validate_bin_manifest(only_one)
        # Order is the encoder's, not the validator's: a reordered manifest
        # describes the same artifacts.
        reordered = json.loads(json.dumps(payload))
        reordered["bundles"].reverse()
        validate_bin_manifest(reordered)


if __name__ == "__main__":
    unittest.main()

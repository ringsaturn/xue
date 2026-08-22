from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
import zlib
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from xue import binformat, temporal, zstdcli
from xue.binformat import (
    COMPRESSION_NONE,
    COMPRESSION_ZSTD,
    COMPRESSION_ZSTD_DICT,
    NO_DEPENDENCY,
    PREDICTOR_ANCHOR,
    PREDICTOR_PREVIOUS,
    PREDICTOR_RAW,
    Bundle,
    PlaneEntry,
    PlanePayload,
    align8,
    crc32_plane,
    write_bundle,
)
from xue.errors import BundleError, ConversionError, ManifestError
from xue.manifest import build_bin_manifest, validate_bin_manifest
from xue.quantize import (
    COMPACT_PRECIPITATION,
    COMPACT_TEMPERATURE,
    QUALITY_PRECIPITATION,
    QUALITY_TEMPERATURE,
)


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
        from xue.quantize import PROFILES

        self.assertIs(PROFILES["balanced"]["tmp2m"], QUALITY_TEMPERATURE)
        self.assertIs(PROFILES["balanced"]["prate"], COMPACT_PRECIPITATION)


class WindCodebookTests(unittest.TestCase):
    """Symmetric linear codebooks for the 10 m wind."""

    def test_symmetric_range_and_round_trip(self) -> None:
        from xue.quantize import COMPACT_WIND, QUALITY_WIND

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
        from xue.quantize import PROFILES

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
        forecast_hour=hour,
        dependency_hour=NO_DEPENDENCY,
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
        forecast_hour=1,
        dependency_hour=0,
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


class BinFormatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bundle.xue"

    def test_write_and_read_round_trip(self) -> None:
        plane0, plane1 = build_synthetic(self.path)
        bundle = binformat.read_bundle(self.path)
        self.assertEqual(bundle.width, 4)
        self.assertEqual(bundle.height, 3)
        self.assertEqual(bundle.frame_count, 2)
        self.assertEqual(bundle.decode_plane(1, 0).tolist(), plane0.tolist())
        self.assertEqual(bundle.decode_plane(1, 1).tolist(), plane1.tolist())
        bundle.verify_all()

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

    def test_truncated_file_rejected(self) -> None:
        build_synthetic(self.path)
        data = self.path.read_bytes()
        with self.assertRaises(BundleError):
            Bundle(data[:-4])
        with self.assertRaises(BundleError):
            Bundle(data[:40])

    def test_oversized_offset_rejected(self) -> None:
        build_synthetic(self.path)
        data = bytearray(self.path.read_bytes())
        struct.pack_into("<Q", data, 40, len(data) * 2)  # indexOffset
        with self.assertRaises(BundleError):
            Bundle(bytes(data))

    def test_nonzero_padding_rejected(self) -> None:
        build_synthetic(self.path)
        data = bytearray(self.path.read_bytes())
        metadata_length = struct.unpack_from("<Q", data, 32)[0]
        index_offset = struct.unpack_from("<Q", data, 40)[0]
        if index_offset == 80 + metadata_length:
            self.skipTest("metadata happened to be aligned, no padding to corrupt")
        data[80 + metadata_length] = 0xAA
        with self.assertRaises(BundleError):
            Bundle(bytes(data))

    def test_trailing_garbage_rejected(self) -> None:
        build_synthetic(self.path)
        data = bytearray(self.path.read_bytes())
        if len(data) == struct.unpack_from("<Q", data, 16)[0]:
            data.extend(b"\x00" * 8)
            struct.pack_into("<Q", data, 16, len(data))
        with self.assertRaises(BundleError):
            Bundle(bytes(data))

    def test_invalid_checksum_rejected(self) -> None:
        build_synthetic(self.path)
        bundle = binformat.read_bundle(self.path)
        entry = bundle.entry_map[(1, 0)]
        data = bytearray(self.path.read_bytes())
        struct.pack_into("<I", data, self._entry_offset(data, 0) + 28, entry.crc32 ^ 0xFFFFFFFF)
        corrupted = Bundle(bytes(data))
        with self.assertRaises(BundleError):
            corrupted.decode_plane(1, 0)

    def _entry_offset(self, data: bytes, position: int) -> int:
        index_offset = struct.unpack_from("<Q", data, 40)[0]
        return index_offset + 16 + position * 40

    def test_unknown_predictor_rejected(self) -> None:
        build_synthetic(self.path)
        data = bytearray(self.path.read_bytes())
        data[self._entry_offset(data, 0) + 1] = 9
        with self.assertRaises(BundleError):
            Bundle(bytes(data))

    def test_zstd_dict_without_dictionary_rejected(self) -> None:
        build_synthetic(self.path)
        data = bytearray(self.path.read_bytes())
        data[self._entry_offset(data, 0) + 2] = COMPRESSION_ZSTD_DICT
        with self.assertRaises(BundleError):
            Bundle(bytes(data))

    def test_cyclic_dependency_rejected(self) -> None:
        build_synthetic(self.path)
        data = bytearray(self.path.read_bytes())
        # Make f000 an ANCHOR depending on f001 while f001 depends on f000.
        offset = self._entry_offset(data, 0)
        data[offset + 1] = PREDICTOR_ANCHOR
        struct.pack_into("<H", data, offset + 6, 1)
        with self.assertRaises(BundleError):
            Bundle(bytes(data))

    def test_dependency_outside_group_rejected(self) -> None:
        build_synthetic(self.path)
        data = bytearray(self.path.read_bytes())
        offset = self._entry_offset(data, 1)
        struct.pack_into("<H", data, offset + 8, 5)  # groupId of the ANCHOR entry
        with self.assertRaises(BundleError):
            Bundle(bytes(data))

    def test_unindexed_gap_rejected(self) -> None:
        plane = np.arange(12, dtype=np.uint8)
        payload = zstdcli.compress(plane.tobytes())
        metadata = synthetic_metadata(frame_count=1)
        write_bundle(self.path, metadata, [PlanePayload(raw_entry(0, plane, payload), payload)])
        data = bytearray(self.path.read_bytes())
        data_offset = struct.unpack_from("<Q", data, 56)[0]
        # Move the payload 8 bytes later, leaving an unindexed gap.
        grown = data[:data_offset] + b"\x00" * 8 + data[data_offset:]
        entry_offset = self._entry_offset(grown, 0)
        struct.pack_into("<Q", grown, entry_offset + 16, data_offset + 8)
        struct.pack_into("<Q", grown, 16, len(grown))
        with self.assertRaises(BundleError):
            Bundle(bytes(grown))

    def test_overlapping_payloads_rejected(self) -> None:
        build_synthetic(self.path)
        data = bytearray(self.path.read_bytes())
        first = self._entry_offset(data, 0)
        second = self._entry_offset(data, 1)
        offset0 = struct.unpack_from("<Q", data, first + 16)[0]
        struct.pack_into("<Q", data, second + 16, offset0)
        with self.assertRaises(BundleError):
            Bundle(bytes(data))

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
            forecast_hour=1,
            dependency_hour=0,
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
        bundle = binformat.read_bundle(self.path)
        self.assertEqual(bundle.decode_plane(1, 1).tolist(), plane1.tolist())

    def test_uncompressed_payload_round_trip(self) -> None:
        plane = np.arange(12, dtype=np.uint8)
        payload = plane.tobytes()
        entry = PlaneEntry(
            variable_id=1,
            predictor=PREDICTOR_RAW,
            compression=COMPRESSION_NONE,
            flags=0,
            forecast_hour=0,
            dependency_hour=NO_DEPENDENCY,
            group_id=0,
            compressed_length=len(payload),
            data_offset=0,
            decoded_length=plane.size,
            crc32=crc32_plane(plane),
            minimum_code=0,
            maximum_code=11,
        )
        write_bundle(self.path, synthetic_metadata(frame_count=1), [PlanePayload(entry, payload)])
        bundle = binformat.read_bundle(self.path)
        self.assertEqual(bundle.decode_plane(1, 0).tolist(), plane.tolist())


def mixed_axis_metadata(hours: list[int], **kwargs) -> dict:
    return synthetic_metadata(
        time={"firstForecastHour": hours[0], "frameCount": len(hours), "hours": list(hours)},
        schema_version=2,
        **kwargs,
    )


class SchemaV2Tests(unittest.TestCase):
    """Mixed-step time axes (metadata schemaVersion 2, docs/format.md)."""

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
        declared dependencyHour). The anchor is each group's first frame —
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
                    forecast_hour=hour,
                    dependency_hour=dependency,
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

    def test_mixed_axis_round_trip(self) -> None:
        planes = self._write(mixed_axis_metadata(self.HOURS))
        bundle = binformat.read_bundle(self.path)
        self.assertEqual(bundle.forecast_hours, self.HOURS)
        for hour, plane in planes.items():
            self.assertEqual(bundle.decode_plane(1, hour).tolist(), plane.tolist())
        bundle.verify_all()

    def test_previous_predictor_uses_the_axis_not_hour_minus_one(self) -> None:
        # The frame preceding f009 is f006 on this axis; a PREVIOUS entry must
        # carry exactly that hour.
        planes = self._write(mixed_axis_metadata(self.HOURS), previous_dependency={9: 6})
        bundle = binformat.read_bundle(self.path)
        self.assertEqual(bundle.decode_plane(1, 9).tolist(), planes[9].tolist())

    def test_previous_predictor_sentinel_rejected(self) -> None:
        self._write(mixed_axis_metadata(self.HOURS), previous_dependency={9: NO_DEPENDENCY})
        with self.assertRaises(BundleError):
            binformat.read_bundle(self.path)

    def test_previous_predictor_wrong_dependency_rejected(self) -> None:
        # forecastHour - 1 (f008) is not on the axis: the old uniform-axis
        # interpretation must be rejected, not silently derived.
        self._write(mixed_axis_metadata(self.HOURS), previous_dependency={9: 8})
        with self.assertRaises(BundleError):
            binformat.read_bundle(self.path)

    def _reject(self, metadata: dict) -> None:
        with self.assertRaises(BundleError):
            self._write(metadata)
            binformat.read_bundle(self.path)

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
            binformat.read_bundle(self.path)


class SourceAxisTests(unittest.TestCase):
    def test_gfs_axis(self) -> None:
        from xue.sources import source_spec

        spec = source_spec("gfs")
        self.assertEqual(spec.forecast_hours(0), [0])
        self.assertEqual(spec.forecast_hours(120), list(range(121)))
        axis = spec.forecast_hours(240)
        self.assertEqual(len(axis), 161)
        self.assertEqual(axis[:121], list(range(121)))
        self.assertEqual(axis[121:], list(range(123, 241, 3)))

    def test_ecmwf_axis(self) -> None:
        from xue.errors import DownloadError
        from xue.sources import source_spec

        spec = source_spec("ecmwf")
        self.assertEqual(spec.forecast_hours(120), list(range(0, 121, 3)))
        axis = spec.forecast_hours(240)
        self.assertEqual(axis, list(range(0, 145, 3)) + list(range(150, 241, 6)))
        for off_axis in (1, 121, 147, 241, 300):
            with self.assertRaises(DownloadError):
                spec.forecast_hours(off_axis)

    def test_sflux_matches_gfs(self) -> None:
        from xue.sources import source_spec

        self.assertEqual(source_spec("sflux").forecast_hours(240), source_spec("gfs").forecast_hours(240))


class TimeMetadataTests(unittest.TestCase):
    def test_uniform_axis_stays_schema_version_1(self) -> None:
        from xue.binconvert import _time_metadata

        self.assertEqual(
            _time_metadata(list(range(0, 121, 3))),
            (1, {"firstForecastHour": 0, "stepHours": 3, "frameCount": 41}),
        )
        self.assertEqual(_time_metadata([7]), (1, {"firstForecastHour": 7, "stepHours": 1, "frameCount": 1}))

    def test_mixed_axis_lists_hours_under_schema_version_2(self) -> None:
        from xue.binconvert import _time_metadata

        axis = list(range(121)) + list(range(123, 241, 3))
        version, time = _time_metadata(axis)
        self.assertEqual(version, 2)
        self.assertEqual(time, {"firstForecastHour": 0, "frameCount": 161, "hours": axis})


def video_descriptor() -> dict:
    return {
        "streamPath": "gfs.2026081506/tmp2m.h264",
        "indexPath": "gfs.2026081506/tmp2m.h264.index.json",
        "byteLength": 19_000_000,
        "crc32": "c63a61aa",
        "codec": "avc1.f40028",
        "width": 1440,
        "height": 721,
        "gop": 6,
        "frameCount": 121,
        "metadataJson": json.dumps({"schemaVersion": 1, "time": {"frameCount": 121}}),
    }


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
            "video": video_descriptor(),
        },
        {"variable": "prate", "path": "gfs.2026081506/prate.xue", "byteLength": 25_000_000, "crc32": "d4c3b2a1"},
    ]


class DeaccumulationTests(unittest.TestCase):
    def test_rate_from_accumulations(self) -> None:
        from xue.binconvert import deaccumulate_precipitation

        previous = np.array([0.0, 3.0, 6.0])
        current = np.array([3.0, 3.0, 5.5])  # last point dips: packing noise
        rate = deaccumulate_precipitation(current, previous, 3)
        np.testing.assert_allclose(rate, [1.0, 0.0, 0.0])

    def test_first_frame_has_zero_rate(self) -> None:
        from xue.binconvert import deaccumulate_precipitation

        rate = deaccumulate_precipitation(np.array([1.0, 2.0]), None, 3)
        np.testing.assert_allclose(rate, [0.0, 0.0])


class BinManifestTests(unittest.TestCase):
    def test_build_and_validate(self) -> None:
        payload = build_bin_manifest(datetime(2026, 8, 15, 6, tzinfo=UTC), bundles=manifest_bundles())
        self.assertEqual(payload["schemaVersion"], 5)
        self.assertEqual([bundle["variable"] for bundle in payload["bundles"]], ["tmp2m", "prate"])
        self.assertEqual(payload["bundles"][0]["video"]["codec"], "avc1.f40028")
        self.assertNotIn("video", payload["bundles"][1])
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

    def test_invalid_video_descriptor_rejected(self) -> None:
        payload = build_bin_manifest(datetime(2026, 8, 15, 6, tzinfo=UTC), bundles=manifest_bundles())
        for mutation in (
            {"streamPath": "gfs.2026081506/tmp2m.mp4"},
            {"indexPath": "gfs.2026081506/tmp2m.json"},
            {"byteLength": 0},
            {"crc32": "not-hex!"},
            {"codec": ""},
            {"width": 0},
            {"metadataJson": "{not valid json"},
        ):
            broken = json.loads(json.dumps(payload))
            broken["bundles"][0]["video"].update(mutation)
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

    def test_missing_or_misordered_variables_rejected(self) -> None:
        payload = build_bin_manifest(datetime(2026, 8, 15, 6, tzinfo=UTC), bundles=manifest_bundles())
        only_one = json.loads(json.dumps(payload))
        only_one["bundles"] = only_one["bundles"][:1]
        with self.assertRaises(ManifestError):
            validate_bin_manifest(only_one)
        reordered = json.loads(json.dumps(payload))
        reordered["bundles"].reverse()
        with self.assertRaises(ManifestError):
            validate_bin_manifest(reordered)


if __name__ == "__main__":
    unittest.main()

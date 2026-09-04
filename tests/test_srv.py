"""Tests for the delivery artifacts: the latest.json live pointer,
first-frame posters, the debug m3u8 playlist, and the resolution ladder /
per-variable video plumbing."""

from __future__ import annotations

import json
import tempfile
import unittest
import zlib
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from xue.binconvert import (
    WIND_COMPONENT_IDS,
    GridInfo,
    _decimate_codes,
    _playback_bandwidth,
    _variable_payloads,
    _wind_bundle_payloads,
    build_metadata,
    decode_poster,
    encode_poster,
)
from xue.errors import ManifestError
from xue.manifest import (
    build_latest_pointer,
    validate_bin_manifest,
    validate_latest_pointer,
    write_latest_pointer,
)
from xue.videoconvert import build_debug_playlist


class LatestPointerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_time = datetime(2026, 8, 16, 0, tzinfo=UTC)

    def _pointer(self) -> dict:
        return build_latest_pointer(
            "2026081600",
            self.run_time,
            manifest_path="gfs.2026081600/manifest.json",
            manifest_crc32="0123abcd",
        )

    def test_build_and_validate(self) -> None:
        pointer = self._pointer()
        self.assertEqual(pointer["run"], "2026081600")
        self.assertEqual(pointer["manifestPath"], "gfs.2026081600/manifest.json")
        validate_latest_pointer(pointer)

    def test_rejects_run_and_runtime_mismatch(self) -> None:
        with self.assertRaisesRegex(ManifestError, "runTime"):
            build_latest_pointer(
                "2026081606",
                self.run_time,
                manifest_path="gfs.2026081606/manifest.json",
                manifest_crc32="0123abcd",
            )

    def test_rejects_invalid_fields(self) -> None:
        for key, value in (
            ("run", "latest"),
            ("manifestPath", "/abs/manifest.json"),
            ("manifestPath", "https://x/manifest.json"),
            ("manifestPath", "a/../manifest.json"),
            ("manifestCrc32", "XYZ"),
            ("schemaVersion", 2),
        ):
            pointer = self._pointer()
            pointer[key] = value
            with self.assertRaises(ManifestError):
                validate_latest_pointer(pointer)

    def test_write_always_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "latest.json"
            write_latest_pointer(destination, self._pointer())
            replacement = build_latest_pointer(
                "2026081606",
                datetime(2026, 8, 16, 6, tzinfo=UTC),
                manifest_path="gfs.2026081606/manifest.json",
                manifest_crc32="deadbeef",
            )
            write_latest_pointer(destination, replacement)
            stored = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(stored["run"], "2026081606")


class PosterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.grid = GridInfo(
            width=144,
            height=73,
            first_longitude=-180.0,
            first_latitude=90.0,
            longitude_step=2.5,
            latitude_step=-2.5,
        )

    def test_roundtrip_matches_decimated_plane(self) -> None:
        rng = np.random.default_rng(7)
        codes = rng.integers(0, 255, size=self.grid.width * self.grid.height, dtype=np.uint8)
        payload, poster_grid = encode_poster(codes, self.grid)
        self.assertEqual((poster_grid.width, poster_grid.height), (72, 37))
        self.assertEqual(poster_grid.longitude_step, 5.0)
        self.assertEqual(poster_grid.latitude_step, -5.0)
        self.assertTrue(poster_grid.wraps)
        decoded = decode_poster(payload, poster_grid.width, poster_grid.height)
        expected = codes.reshape(self.grid.height, self.grid.width)[::2, ::2]
        np.testing.assert_array_equal(decoded, expected)

    def test_smooth_field_compresses_well(self) -> None:
        latitude = np.linspace(90, -90, self.grid.height)
        longitude = np.linspace(-180, 177.5, self.grid.width)
        lon_grid, lat_grid = np.meshgrid(longitude, latitude)
        codes = ((np.cos(np.radians(lat_grid)) * 200) + np.sin(np.radians(lon_grid)) * 20).astype(np.uint8)
        payload, poster_grid = encode_poster(codes.ravel(), self.grid)
        self.assertLess(len(payload), poster_grid.width * poster_grid.height // 2)

    def test_poster_descriptor_validates_in_manifest(self) -> None:
        metadata_json = json.dumps({"schemaVersion": 1})
        manifest = {
            "schemaVersion": 5,
            "model": "GFS",
            "product": "pgrb2.0p25",
            "runTime": "2026-08-16T00:00:00Z",
            "forecastHours": 120,
            "bundles": [
                {
                    "variable": variable,
                    "path": f"{variable}.xue",
                    "byteLength": 1000,
                    "crc32": "0123abcd",
                    "poster": {
                        "path": f"{variable}.poster.bin",
                        "width": 720,
                        "height": 361,
                        "byteLength": 50_000,
                        "crc32": "deadbeef",
                        "metadataJson": metadata_json,
                    },
                }
                for variable in ("tmp2m", "prate")
            ],
        }
        validate_bin_manifest(manifest)
        broken = json.loads(json.dumps(manifest))
        broken["bundles"][0]["poster"]["path"] = "tmp2m.poster.png"
        with self.assertRaisesRegex(ManifestError, "poster path"):
            validate_bin_manifest(broken)
        broken = json.loads(json.dumps(manifest))
        broken["bundles"][1]["poster"]["path"] = broken["bundles"][0]["poster"]["path"]
        with self.assertRaisesRegex(ManifestError, "duplicate"):
            validate_bin_manifest(broken)


class ResolutionLadderTests(unittest.TestCase):
    """Half-resolution variant bundles."""

    def test_production_grid_decimates_to_720x361(self) -> None:
        grid = GridInfo(
            width=1440,
            height=721,
            first_longitude=-179.875,
            first_latitude=89.875,
            longitude_step=0.25,
            latitude_step=-0.25,
        )
        half = grid.decimated()
        self.assertEqual((half.width, half.height), (720, 361))
        self.assertEqual((half.longitude_step, half.latitude_step), (0.5, -0.5))
        self.assertEqual((half.first_longitude, half.first_latitude), (-179.875, 89.875))
        self.assertTrue(half.wraps)

    def test_decimated_codes_match_poster_sampling(self) -> None:
        grid = GridInfo(
            width=10,
            height=7,
            first_longitude=-180.0,
            first_latitude=90.0,
            longitude_step=36.0,
            latitude_step=-30.0,
        )
        codes = np.arange(70, dtype=np.uint8)
        half = _decimate_codes(codes, grid)
        expected = codes.reshape(7, 10)[::2, ::2].ravel()
        np.testing.assert_array_equal(half, expected)
        self.assertTrue(half.flags["C_CONTIGUOUS"])

    def test_variable_payloads_share_temporal_structure_across_tiers(self) -> None:
        hours = list(range(13))
        full = {hour: np.full(70, hour, dtype=np.uint8) for hour in hours}
        half = {hour: np.full(20, hour, dtype=np.uint8) for hour in hours}
        for planes in (full, half):
            temperature = _variable_payloads("tmp2m", hours, planes)
            self.assertEqual(len(temperature), len(hours))
            anchors = [entry for entry, _payload in temperature if entry.predictor == 0]
            self.assertEqual(len(anchors), 3)  # groups 0-5, 6-11, 12
            precipitation = _variable_payloads("prate", hours, planes)
            self.assertTrue(all(entry.predictor == 0 for entry, _payload in precipitation))
            self.assertEqual([entry.group_id for entry, _payload in precipitation], hours)

    def test_playback_bandwidth_hint(self) -> None:
        # 121 frames at 12 fps take ~10.08 s; 12 MB over that is ~9.5 Mbps.
        self.assertEqual(_playback_bandwidth(12_000_000, 121), round(12_000_000 * 8 * 12 / 121))
        self.assertGreaterEqual(_playback_bandwidth(1, 121), 1)

    def test_variant_descriptor_validates_in_manifest(self) -> None:
        manifest = {
            "schemaVersion": 5,
            "model": "GFS",
            "product": "pgrb2.0p25",
            "runTime": "2026-08-16T00:00:00Z",
            "forecastHours": 120,
            "bundles": [
                {
                    "variable": variable,
                    "path": f"{variable}.xue",
                    "byteLength": 1000,
                    "crc32": "0123abcd",
                    "variants": [
                        {
                            "path": f"{variable}.half.xue",
                            "width": 720,
                            "height": 361,
                            "byteLength": 300,
                            "crc32": "deadbeef",
                            "bandwidth": 2_400_000,
                        }
                    ],
                }
                for variable in ("tmp2m", "prate")
            ],
        }
        validate_bin_manifest(manifest)
        broken = json.loads(json.dumps(manifest))
        broken["bundles"][1]["variants"][0]["path"] = broken["bundles"][0]["variants"][0]["path"]
        with self.assertRaisesRegex(ManifestError, "duplicate"):
            validate_bin_manifest(broken)


class PrateVideoManifestTests(unittest.TestCase):
    """The video descriptor is valid on any variable."""

    def test_video_descriptor_on_prate_validates(self) -> None:
        metadata_json = json.dumps({"schemaVersion": 1})
        manifest = {
            "schemaVersion": 5,
            "model": "GFS",
            "product": "pgrb2.0p25",
            "runTime": "2026-08-16T00:00:00Z",
            "forecastHours": 120,
            "bundles": [
                {
                    "variable": variable,
                    "path": f"{variable}.xue",
                    "byteLength": 1000,
                    "crc32": "0123abcd",
                    "video": {
                        "streamPath": f"{variable}.h264",
                        "indexPath": f"{variable}.h264.index.json",
                        "byteLength": 900,
                        "crc32": "deadbeef",
                        "codec": "avc1.f40028",
                        "width": 1440,
                        "height": 721,
                        "gop": 6,
                        "frameCount": 121,
                        "metadataJson": metadata_json,
                    },
                }
                for variable in ("tmp2m", "prate")
            ],
        }
        validate_bin_manifest(manifest)


class WindBundleTests(unittest.TestCase):
    """The two-variable wind bundle."""

    HOURS = list(range(13))  # groups 0-5, 6-11, 12

    def _planes(self) -> dict[int, dict[str, np.ndarray]]:
        rng = np.random.default_rng(7)
        return {
            hour: {
                component: rng.integers(0, 255, size=64, dtype=np.uint8)
                for component in WIND_COMPONENT_IDS
            }
            for hour in self.HOURS
        }

    def test_payloads_interleave_components_per_group(self) -> None:
        payloads = _wind_bundle_payloads(self.HOURS, self._planes())
        self.assertEqual(len(payloads), 2 * len(self.HOURS))
        ids = [entry.variable_id for entry, _payload in payloads]
        self.assertEqual(ids, [3] * 6 + [4] * 6 + [3] * 6 + [4] * 6 + [3, 4])
        # Each component covers every hour exactly once, with the same
        # anchor+residual structure as the temperature groups.
        for numeric_id, component in ((3, "ugrd10m"), (4, "vgrd10m")):
            hours = sorted(entry.frame_offset for entry, _payload in payloads if entry.variable_id == numeric_id)
            self.assertEqual(hours, self.HOURS)
        reference = [entry.predictor for entry, _payload in _variable_payloads("tmp2m", self.HOURS, {
            hour: planes["ugrd10m"] for hour, planes in self._planes().items()
        })]
        u_predictors = [entry.predictor for entry, _payload in payloads if entry.variable_id == 3]
        self.assertEqual(u_predictors, reference)

    def test_two_variable_bundle_round_trips(self) -> None:
        from xue import binformat, zstdcli
        from dataclasses import replace

        planes = self._planes()
        payloads = [
            binformat.PlanePayload(replace(entry, compressed_length=len(compressed)), compressed)
            for entry, raw in _wind_bundle_payloads(self.HOURS, planes)
            for compressed in (zstdcli.compress(raw, level=3),)
        ]
        grid = GridInfo(8, 8, -180.0, 90.0, 45.0, -25.0)
        metadata = build_metadata(
            datetime(2026, 8, 16, 0, tzinfo=UTC), self.HOURS, grid, "quality", WIND_COMPONENT_IDS
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wind10m.xue"
            binformat.write_bundle(path, metadata, payloads)
            bundle = binformat.read_bundle(path)
            bundle.verify_all()
            self.assertEqual(sorted(bundle.variable_ids.values()), ["ugrd10m", "vgrd10m"])
            for hour in self.HOURS:
                np.testing.assert_array_equal(bundle.decode_plane(3, hour), planes[hour]["ugrd10m"])
                np.testing.assert_array_equal(bundle.decode_plane(4, hour), planes[hour]["vgrd10m"])

    def test_manifest_accepts_optional_wind_bundle_in_order(self) -> None:
        def bundles(*, wind: bool) -> list[dict[str, object]]:
            entries: list[dict[str, object]] = [
                {"variable": "tmp2m", "path": "tmp2m.xue", "byteLength": 1000, "crc32": "0123abcd"},
                {"variable": "prate", "path": "prate.xue", "byteLength": 1000, "crc32": "4567abcd"},
            ]
            if wind:
                entries.append(
                    {"variable": "wind10m", "path": "wind10m.xue", "byteLength": 2000, "crc32": "89abcdef"}
                )
            return entries

        def manifest(entries: list[dict[str, object]]) -> dict[str, object]:
            return {
                "schemaVersion": 5,
                "model": "GFS",
                "product": "pgrb2.0p25",
                "runTime": "2026-08-16T00:00:00Z",
                "forecastHours": 120,
                "bundles": entries,
            }

        validate_bin_manifest(manifest(bundles(wind=False)))
        validate_bin_manifest(manifest(bundles(wind=True)))
        out_of_order = manifest(list(reversed(bundles(wind=True))))
        with self.assertRaisesRegex(ManifestError, "ordered"):
            validate_bin_manifest(out_of_order)
        missing_scalar = manifest([entry for entry in bundles(wind=True) if entry["variable"] != "prate"])
        with self.assertRaisesRegex(ManifestError, "prate"):
            validate_bin_manifest(missing_scalar)


class DebugPlaylistTests(unittest.TestCase):
    def test_byterange_segments_cover_the_stream_per_gop(self) -> None:
        frames = []
        offset = 0
        for index in range(13):
            length = 100 + index
            frames.append({"offset": offset, "length": length, "keyframe": index % 6 == 0})
            offset += length
        playlist = build_debug_playlist(frames, "tmp2m.h264")
        lines = playlist.strip().split("\n")
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertIn("#EXT-X-VERSION:4", lines)
        self.assertIn("#EXT-X-ENDLIST", lines)
        byteranges = [line for line in lines if line.startswith("#EXT-X-BYTERANGE:")]
        self.assertEqual(len(byteranges), 3)  # GOPs: 0-5, 6-11, 12
        spans = []
        for line in byteranges:
            length, start = line.removeprefix("#EXT-X-BYTERANGE:").split("@")
            spans.append((int(start), int(start) + int(length)))
        self.assertEqual(spans[0][0], 0)
        for previous, current in zip(spans, spans[1:]):
            self.assertEqual(previous[1], current[0])
        self.assertEqual(spans[-1][1], offset)
        self.assertEqual(playlist.count("tmp2m.h264\n"), 3)
        self.assertIn("#EXTINF:0.500000,F000-F005", lines)


if __name__ == "__main__":
    unittest.main()

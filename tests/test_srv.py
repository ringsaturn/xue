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

from xuebuild import binformat

from xuebuild.binconvert import (
    WIND_COMPONENT_IDS,
    GridInfo,
    _bundle_chunks,
    _decimate_codes,
    _playback_bandwidth,
    build_metadata,
    decode_poster,
    encode_poster,
)
from xuebuild.errors import ManifestError
from xuebuild.manifest import (
    build_latest_pointer,
    validate_bin_manifest,
    validate_latest_pointer,
    write_latest_pointer,
)
from xuebuild.videoconvert import build_debug_playlist


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

    def test_chunks_share_temporal_structure_across_tiers(self) -> None:
        """Both tiers group the axis the same way and cut the same number of
        tiles, so a viewport keeps its tile rectangle across a tier switch."""
        hours = list(range(13))
        full_grid, half_grid = GridInfo(10, 7, -180.0, 90.0, 36.0, -30.0), None
        half_grid = full_grid.decimated()
        tiers = (
            (full_grid, (4, 3), {hour: {"tmp2m": np.full(70, hour, dtype=np.uint8),
                                        "prate": np.full(70, hour, dtype=np.uint8)} for hour in hours}),
            (half_grid, (2, 2), {hour: {"tmp2m": np.full(20, hour, dtype=np.uint8),
                                        "prate": np.full(20, hour, dtype=np.uint8)} for hour in hours}),
        )
        for grid, tile, codes in tiers:
            tiles = binformat.TileGeometry(grid.width, grid.height, *tile)
            variables, groups, chunks = _bundle_chunks(("tmp2m",), hours, codes, tiles)
            # Groups 0-5, 6-11, 12 — the same partition at both resolutions.
            self.assertEqual([group.frame_count for group in groups], [6, 6, 1])
            self.assertEqual([group.first_frame for group in groups], [0, 6, 12])
            self.assertEqual(len(chunks), len(groups) * tiles.count)
            self.assertEqual([v.predictor for v in variables], [binformat.PREDICTOR_PREVIOUS])
            # Precipitation is the exception: any temporal residual makes it
            # bigger, so its chunks stack the codes RAW.
            rain, _, _ = _bundle_chunks(("prate",), hours, codes, tiles)
            self.assertEqual([v.predictor for v in rain], [binformat.PREDICTOR_RAW])

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

    TILE = (4, 4)

    def _tiles(self) -> "binformat.TileGeometry":
        return binformat.TileGeometry(8, 8, *self.TILE)

    def test_chunks_interleave_components_within_a_tile(self) -> None:
        """v1 interleaved a u group with a v group; v2 goes one level finer,
        putting u and v of the *same tile* next to each other, so a single
        range still covers a wind frame and one range covers a wind series."""
        tiles = self._tiles()
        variables, groups, chunks = _bundle_chunks(
            WIND_COMPONENT_IDS, self.HOURS, self._planes(), tiles
        )
        self.assertEqual([v.variable_id for v in variables], [3, 4])
        self.assertEqual([group.frame_count for group in groups], [6, 6, 1])
        self.assertEqual(len(chunks), len(groups) * tiles.count * 2)
        # Physical order: group, then tile row-major, then variable — so the
        # chunk at an even position is u and its immediate neighbour is v of
        # the same tile and group.
        self.assertEqual(
            [position % 2 for position in range(len(chunks))],
            [variables.index(v) for _ in range(len(chunks) // 2) for v in variables],
        )
        # Both components use the same predictor and the same partition.
        self.assertEqual({v.predictor for v in variables}, {binformat.PREDICTOR_PREVIOUS})

    def test_two_variable_bundle_round_trips(self) -> None:
        from dataclasses import replace

        from xuebuild import zstdcli

        planes = self._planes()
        tiles = self._tiles()
        variables, groups, raw = _bundle_chunks(WIND_COMPONENT_IDS, self.HOURS, planes, tiles)
        chunks = [
            binformat.ChunkPayload(replace(entry, compressed_length=len(compressed)), compressed)
            for entry, stored in raw
            for compressed in (zstdcli.compress(stored.tobytes(), level=3),)
        ]
        grid = GridInfo(8, 8, -180.0, 90.0, 45.0, -25.0)
        metadata = build_metadata(
            datetime(2026, 8, 16, 0, tzinfo=UTC), self.HOURS, grid, "quality", WIND_COMPONENT_IDS
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "wind10m.xue"
            binformat.write_bundle_v2(
                path,
                metadata,
                tile_width=self.TILE[0],
                tile_height=self.TILE[1],
                variables=variables,
                groups=groups,
                chunks=chunks,
            )
            bundle = binformat.read_bundle(path)
            bundle.verify_all()
            self.assertEqual(sorted(bundle.variable_ids.values()), ["ugrd10m", "vgrd10m"])
            for hour in self.HOURS:
                np.testing.assert_array_equal(bundle.decode_plane(3, hour), planes[hour]["ugrd10m"])
                np.testing.assert_array_equal(bundle.decode_plane(4, hour), planes[hour]["vgrd10m"])
            # A wind series is one chunk per group of one tile, per component,
            # and those two chunks are adjacent bytes.
            for column, row in ((0, 0), (7, 7), (5, 2)):
                for numeric_id, component in ((3, "ugrd10m"), (4, "vgrd10m")):
                    expected = [int(planes[hour][component][row * 8 + column]) for hour in self.HOURS]
                    self.assertEqual(bundle.decode_series(numeric_id, column, row).tolist(), expected)
            tile = bundle.tiles.tile_of(0, 0)
            u_span = bundle.chunk_span(bundle.chunk_position(0, tile, 3))
            v_span = bundle.chunk_span(bundle.chunk_position(0, tile, 4))
            self.assertEqual(u_span[1], v_span[0])

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

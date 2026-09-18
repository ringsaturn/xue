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
    VARIANT_TIERS,
    WIND_COMPONENT_IDS,
    GridInfo,
    _bundle_chunks,
    _bundle_tile,
    _decimate_codes,
    _playback_bandwidth,
    _variant_grid,
    build_metadata,
    decode_poster,
    encode_poster,
    variant_tier,
)
from xuebuild.errors import ManifestError
from xuebuild.manifest import (
    build_bin_manifest,
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
    """Reduced-resolution variant bundles: the half rung every source
    publishes, and the quarter and eighth the satellite disks add."""

    def test_a_tier_is_named_by_its_factor_and_nothing_else_has_a_name(self) -> None:
        self.assertEqual(VARIANT_TIERS, {2: "half", 4: "quarter", 8: "eighth"})
        self.assertEqual([variant_tier(factor) for factor in (2, 4, 8)], ["half", "quarter", "eighth"])
        for factor in (0, 1, 3, 6, 16):
            with self.subTest(factor=factor), self.assertRaisesRegex(ValueError, str(factor)):
                variant_tier(factor)

    def test_a_rung_s_grid_is_the_full_grid_decimated_once_per_halving(self) -> None:
        production = GridInfo(1440, 721, -179.875, 89.875, 0.25, -0.25)
        disk = GridInfo(3000, 3000, 80.72, 59.98, 0.04, -0.04)
        self.assertEqual([(_variant_grid(production, f).width, _variant_grid(production, f).height) for f in (2, 4, 8)], [(720, 361), (360, 181), (180, 91)])
        self.assertEqual([(_variant_grid(disk, f).width, _variant_grid(disk, f).height) for f in (2, 4, 8)], [(1500, 1500), (750, 750), (375, 375)])
        self.assertEqual(_variant_grid(production, 1), production)
        self.assertEqual(_variant_grid(production, 2), production.decimated())
        self.assertEqual(_variant_grid(production, 8), production.decimated().decimated().decimated())
        eighth = _variant_grid(disk, 8)
        self.assertEqual((eighth.longitude_step, eighth.latitude_step), (0.32, -0.32))
        self.assertEqual((eighth.first_longitude, eighth.first_latitude), (80.72, 59.98))

    def test_a_rung_s_tile_is_the_source_tile_over_its_factor_rounded_up(self) -> None:
        """The satellite tile 64 x 64 goes 32, 16, 8; the production
        48 x 52 goes 24 x 26, 12 x 13, 6 x 7 — each rung the rung before
        halved, so tile number n covers the same ground in every tier."""
        disk = GridInfo(3000, 3000, 80.72, 59.98, 0.04, -0.04)
        production = GridInfo(1440, 721, -179.875, 89.875, 0.25, -0.25)
        self.assertEqual([_bundle_tile((64, 64), _variant_grid(disk, f), factor=f) for f in (1, 2, 4, 8)], [(64, 64), (32, 32), (16, 16), (8, 8)])
        self.assertEqual([_bundle_tile((48, 52), _variant_grid(production, f), factor=f) for f in (1, 2, 4, 8)], [(48, 52), (24, 26), (12, 13), (6, 7)])
        # ceil(ceil(n / 2) / 2) == ceil(n / 4): the quarter is the half halved.
        for width in range(1, 130):
            half = _bundle_tile((width, width), disk, factor=2)
            self.assertEqual(_bundle_tile((width, width), disk, factor=4), _bundle_tile(half, disk, factor=2))
            self.assertEqual(_bundle_tile((width, width), disk, factor=8), _bundle_tile(_bundle_tile(half, disk, factor=2), disk, factor=2))
        # Every rung is clamped to its own grid: a crop smaller than the
        # tile is one tile at every resolution.
        crop = GridInfo(24, 24, 130.0, 40.0, 0.25, -0.25)
        self.assertEqual(_bundle_tile((48, 52), crop, factor=1), (24, 24))
        self.assertEqual(_bundle_tile((48, 52), _variant_grid(crop, 2), factor=2), (12, 12))
        self.assertEqual(_bundle_tile((48, 52), _variant_grid(crop, 8), factor=8), (3, 3))

    def test_decimation_by_a_larger_factor_is_repeated_halving(self) -> None:
        grid = GridInfo(37, 23, -180.0, 90.0, 9.7297, -8.1818)
        codes = np.arange(37 * 23, dtype=np.int32).astype(np.uint8)
        half = _decimate_codes(codes, grid, 2)
        np.testing.assert_array_equal(half, _decimate_codes(codes, grid))
        quarter = _decimate_codes(codes, grid, 4)
        eighth = _decimate_codes(codes, grid, 8)
        np.testing.assert_array_equal(quarter, _decimate_codes(half, grid.decimated(), 2))
        np.testing.assert_array_equal(eighth, _decimate_codes(quarter, grid.decimated().decimated(), 2))
        np.testing.assert_array_equal(quarter, codes.reshape(23, 37)[::4, ::4].ravel())
        np.testing.assert_array_equal(eighth, codes.reshape(23, 37)[::8, ::8].ravel())
        self.assertEqual((quarter.size, eighth.size), (10 * 6, 5 * 3))
        self.assertEqual((_variant_grid(grid, 4).width * _variant_grid(grid, 4).height, _variant_grid(grid, 8).width * _variant_grid(grid, 8).height), (quarter.size, eighth.size))
        for rung in (half, quarter, eighth):
            self.assertTrue(rung.flags["C_CONTIGUOUS"])

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

    def test_store_only_entries_validate_and_neither_is_refused(self) -> None:
        """A bundle or a variant may name its Zarr store alone: the ``.xue``
        fields are one unit that is present whole or absent whole, and an
        entry with neither delivery is refused — that is the shape the
        encoder writes once the container is retired, and the validators
        have to accept it before any encoder does."""

        def store(name: str) -> dict[str, object]:
            return {"path": f"{name}.zarr", "byteLength": 1200, "crc32": "760cef95"}

        manifest = {
            "schemaVersion": 5,
            "model": "GFS",
            "product": "pgrb2.0p25",
            "runTime": "2026-08-16T00:00:00Z",
            "forecastHours": 120,
            "bundles": [
                {
                    "variable": "tmp2m",
                    "zarr": store("tmp2m"),
                    "variants": [
                        {
                            "width": 720,
                            "height": 361,
                            "bandwidth": 2_400_000,
                            "zarr": store("tmp2m.half"),
                        }
                    ],
                },
                {
                    "variable": "prate",
                    "path": "prate.xue",
                    "byteLength": 1000,
                    "crc32": "0123abcd",
                    "zarr": store("prate"),
                    "variants": [
                        {
                            "path": "prate.half.xue",
                            "width": 720,
                            "height": 361,
                            "byteLength": 300,
                            "crc32": "deadbeef",
                            "bandwidth": 2_400_000,
                        }
                    ],
                },
            ],
        }
        validate_bin_manifest(manifest)
        built = build_bin_manifest(
            datetime(2026, 8, 16, tzinfo=UTC),
            bundles=manifest["bundles"],
        )
        self.assertNotIn("path", built["bundles"][0])
        self.assertEqual(built["bundles"][1]["path"], "prate.xue")

        neither = json.loads(json.dumps(manifest))
        del neither["bundles"][0]["zarr"]
        with self.assertRaisesRegex(ManifestError, "path or a zarr store"):
            validate_bin_manifest(neither)
        neither_variant = json.loads(json.dumps(manifest))
        del neither_variant["bundles"][0]["variants"][0]["zarr"]
        with self.assertRaisesRegex(ManifestError, "variant must carry"):
            validate_bin_manifest(neither_variant)
        half_unit = json.loads(json.dumps(manifest))
        half_unit["bundles"][0]["byteLength"] = 1000
        with self.assertRaisesRegex(ManifestError, "without a path"):
            validate_bin_manifest(half_unit)
        wrong_suffix = json.loads(json.dumps(manifest))
        wrong_suffix["bundles"][0]["path"] = "tmp2m.zarr"
        with self.assertRaisesRegex(ManifestError, "relative .xue path"):
            validate_bin_manifest(wrong_suffix)


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
        self.assertEqual([v.variable_id for v in variables], [1, 2])
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
                np.testing.assert_array_equal(bundle.decode_plane(1, hour), planes[hour]["ugrd10m"])
                np.testing.assert_array_equal(bundle.decode_plane(2, hour), planes[hour]["vgrd10m"])
            # A wind series is one chunk per group of one tile, per component,
            # and those two chunks are adjacent bytes.
            for column, row in ((0, 0), (7, 7), (5, 2)):
                for numeric_id, component in ((1, "ugrd10m"), (2, "vgrd10m")):
                    expected = [int(planes[hour][component][row * 8 + column]) for hour in self.HOURS]
                    self.assertEqual(bundle.decode_series(numeric_id, column, row).tolist(), expected)
            tile = bundle.tiles.tile_of(0, 0)
            u_span = bundle.chunk_span(bundle.chunk_position(0, tile, 1))
            v_span = bundle.chunk_span(bundle.chunk_position(0, tile, 2))
            self.assertEqual(u_span[1], v_span[0])

    def test_manifest_admits_any_well_formed_bundle_name(self) -> None:
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
        # Order is the encoder's business, not the validator's.
        validate_bin_manifest(manifest(list(reversed(bundles(wind=True)))))
        # A name this encoder has never heard of is a layer a reader skips,
        # not a malformed manifest: the variable is a file-local handle's
        # label, not an entry in a closed registry.
        unknown = manifest(
            bundles(wind=False)
            + [{"variable": "cape180", "path": "cape180.xue", "byteLength": 900, "crc32": "0011aabb"}]
        )
        validate_bin_manifest(unknown)
        # What a name must still be: lowercase alphanumeric, leading letter,
        # unique within the manifest.
        for bad in ("Tmp2m", "tmp-2m", "tmp_2m", "2mtmp", "", "tmp 2m"):
            malformed = manifest(
                bundles(wind=False)
                + [{"variable": bad, "path": "other.xue", "byteLength": 900, "crc32": "0011aabb"}]
            )
            with self.assertRaisesRegex(ManifestError, "not a bundle name"):
                validate_bin_manifest(malformed)
        duplicate = manifest(
            bundles(wind=False)
            + [{"variable": "prate", "path": "prate-again.xue", "byteLength": 900, "crc32": "0011aabb"}]
        )
        with self.assertRaisesRegex(ManifestError, "duplicate bundle variables"):
            validate_bin_manifest(duplicate)
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

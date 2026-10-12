"""Cutting a rolling window's stores into clock-aligned segments
(docs/zarr-profile.md, "Segmented stores")."""

from __future__ import annotations

import json
import shutil
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from tests._support import TempRoot
from xuebuild import binformat, segments, zarrstore
from xuebuild.binconvert import GridInfo, build_metadata
from xuebuild.errors import ManifestError
from xuebuild.manifest import build_bin_manifest, validate_bin_manifest

GRID = GridInfo(
    width=17,
    height=9,
    first_longitude=-180.0,
    first_latitude=90.0,
    longitude_step=21.0,
    latitude_step=-20.0,
)
TILE = (5, 4)
# A window from 22:00 to 01:30 every ten minutes with the 23:40 slot missing:
# four segments, the last one partial.
START = datetime(2026, 10, 11, 22, tzinfo=UTC)
OFFSETS = [offset for offset in range(22) if offset != 10]


def plane(offset: int, salt: int) -> np.ndarray:
    return ((np.arange(GRID.width * GRID.height) * (salt + 3) + offset * 7) % 200).astype(np.uint8)


def write_round(data_root: Path, round_path: str, offsets: list[int], *, salt: int = 0) -> Path:
    """A round directory as `build-bin --round` leaves it: a manifest naming
    one plain store per bundle."""
    round_directory = data_root / round_path
    round_directory.mkdir(parents=True)
    bundles = []
    for variable_id, predictor in (("tmp2m", "previous"), ("prate", "raw")):
        metadata = build_metadata(START, offsets, GRID, "balanced", (variable_id,), unit_seconds=600)
        planes = {variable_id: [plane(offset, salt) for offset in offsets]}
        source = zarrstore.CodesSource(metadata, planes, {variable_id: predictor}, TILE)
        written = zarrstore.write_store(source, round_directory / f"{variable_id}.zarr", delta=True)
        bundles.append(
            {
                "variable": variable_id,
                "zarr": {"path": f"{variable_id}.zarr", "byteLength": written.byte_length, "crc32": written.crc32},
            }
        )
    manifest = build_bin_manifest(START, bundles=bundles, expected_hours=4)
    (round_directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return round_directory


def frame_times(store: Path) -> list[datetime]:
    metadata = zarrstore.read_json(store / "zarr.json")["attributes"]["xue"]
    unit_seconds, offsets = binformat.time_axis(metadata)
    run_time = datetime.fromisoformat(metadata["runTime"].replace("Z", "+00:00"))
    return [run_time + timedelta(seconds=offset * unit_seconds) for offset in offsets]


class SegmentRoundTest(TempRoot, unittest.TestCase):
    def test_a_round_is_cut_by_the_clock_and_its_codes_survive(self) -> None:
        round_directory = write_round(self.root, "gfs.2026101122/0130", OFFSETS)
        original = {
            name: zarrstore.read_frames(round_directory / f"{name}.zarr", name) for name in ("tmp2m", "prate")
        }
        report = segments.segment_round(round_directory, data_root=self.root, source_id="gfs")
        manifest = json.loads((round_directory / "manifest.json").read_text(encoding="utf-8"))
        validate_bin_manifest(manifest, expected_hours=4)
        self.assertEqual(len(report.written), 8)
        self.assertEqual(report.reused, 0)
        for bundle in manifest["bundles"]:
            name = bundle["variable"]
            self.assertFalse((round_directory / f"{name}.zarr").exists())
            descriptor = bundle["zarr"]
            self.assertNotIn("path", descriptor)
            self.assertEqual(descriptor["byteLength"], sum(part["byteLength"] for part in descriptor["segments"]))
            starts = [part["path"].split("/")[-3] for part in descriptor["segments"]]
            self.assertEqual(starts, ["202610112200", "202610112300", "202610120000", "202610120100"])
            frames: list[np.ndarray] = []
            times: list[datetime] = []
            for part in descriptor["segments"]:
                self.assertTrue(part["path"].startswith("../../gfs.segments/"))
                store = round_directory / part["path"]
                frames.extend(zarrstore.read_frames(store, name))
                times.extend(frame_times(store))
                metadata = zarrstore.read_json(store / "zarr.json")["attributes"]["xue"]
                self.assertEqual(metadata["time"]["firstFrameOffset"], 0)
            self.assertEqual(times, [START + timedelta(minutes=10 * offset) for offset in OFFSETS])
            self.assertEqual(len(frames), len(original[name]))
            for got, want in zip(frames, original[name]):
                np.testing.assert_array_equal(got, want)

    def test_the_live_rounds_segments_are_named_not_written(self) -> None:
        live = write_round(self.root, "gfs.2026101122/0120", OFFSETS[:-1])
        segments.segment_round(live, data_root=self.root, source_id="gfs")
        live_manifest = json.loads((live / "manifest.json").read_text(encoding="utf-8"))
        # The bucket holds the live round's segments; the runner does not.
        shutil.rmtree(self.root / "gfs.segments")

        current = write_round(self.root, "gfs.2026101122/0130", OFFSETS)
        report = segments.segment_round(
            current,
            data_root=self.root,
            source_id="gfs",
            live_manifest=live_manifest,
            live_manifest_path="gfs.2026101122/0120/manifest.json",
        )
        # Three whole hours carried over per bundle; the hour in progress grew.
        self.assertEqual(report.reused, 6)
        self.assertEqual(len(report.written), 2)
        self.assertTrue(all("/202610120100/" in path.as_posix() for path in report.written))
        manifest = json.loads((current / "manifest.json").read_text(encoding="utf-8"))
        for bundle, held in zip(manifest["bundles"], live_manifest["bundles"]):
            self.assertEqual(bundle["zarr"]["segments"][:3], held["zarr"]["segments"][:3])
            self.assertNotEqual(bundle["zarr"]["segments"][3], held["zarr"]["segments"][3])

    def test_changed_codes_land_under_another_digest(self) -> None:
        first = write_round(self.root, "gfs.2026101122/0120", OFFSETS)
        segments.segment_round(first, data_root=self.root, source_id="gfs")
        second = write_round(self.root, "gfs.2026101122/0130", OFFSETS, salt=1)
        segments.segment_round(second, data_root=self.root, source_id="gfs")
        paths = [
            {
                part["path"]
                for bundle in json.loads((directory / "manifest.json").read_text(encoding="utf-8"))["bundles"]
                for part in bundle["zarr"]["segments"]
            }
            for directory in (first, second)
        ]
        self.assertFalse(paths[0] & paths[1])

    def test_an_hourly_feed_is_cut_in_six_hours(self) -> None:
        hourly = [START + timedelta(hours=hour) for hour in range(8)]
        self.assertEqual(segments.segment_seconds(hourly), 6 * 3600)
        self.assertEqual(segments.segment_seconds(hourly[:1]), 3600)
        self.assertEqual(
            segments.segment_start(datetime(2026, 10, 12, 4, 30, tzinfo=UTC), 6 * 3600),
            datetime(2026, 10, 12, 0, tzinfo=UTC),
        )


class SegmentedDescriptorTest(unittest.TestCase):
    def manifest(self, store: dict) -> dict:
        plain = {"path": "prate.zarr", "byteLength": 5, "crc32": "0123abcd"}
        return {
            "schemaVersion": 5,
            "model": "GFS",
            "product": "pgrb2.0p25",
            "runTime": "2026-10-11T22:00:00Z",
            "forecastHours": 4,
            "bundles": [{"variable": "tmp2m", "zarr": store}, {"variable": "prate", "zarr": plain}],
        }

    def segment(self, path: str, byte_length: int = 7) -> dict:
        return {"path": path, "byteLength": byte_length, "crc32": "deadbeef"}

    def test_a_segmented_store_is_accepted(self) -> None:
        store = {
            "byteLength": 14,
            "segments": [
                self.segment("../../gfs.segments/202610112200/0a1b2c3d/tmp2m.zarr"),
                self.segment("../../gfs.segments/202610112300/4e5f6a7b/tmp2m.zarr"),
            ],
        }
        validate_bin_manifest(self.manifest(store), expected_hours=4)

    def test_a_malformed_segmented_store_is_rejected(self) -> None:
        good = "../../gfs.segments/202610112200/0a1b2c3d/tmp2m.zarr"
        cases = {
            "path beside segments": {"path": "tmp2m.zarr", "crc32": "deadbeef", "byteLength": 7, "segments": [self.segment(good)]},
            "no segments": {"byteLength": 7, "segments": []},
            "sum mismatch": {"byteLength": 8, "segments": [self.segment(good)]},
            "three climbs": {"byteLength": 7, "segments": [self.segment("../../../gfs.segments/x/tmp2m.zarr")]},
            "climb after a name": {"byteLength": 7, "segments": [self.segment("../gfs.segments/../x/tmp2m.zarr")]},
            "absolute": {"byteLength": 7, "segments": [self.segment("/gfs.segments/x/tmp2m.zarr")]},
            "bad crc": {"byteLength": 7, "segments": [{"path": good, "byteLength": 7, "crc32": "DEADBEEF"}]},
        }
        for label, store in cases.items():
            with self.subTest(label):
                with self.assertRaises(ManifestError):
                    validate_bin_manifest(self.manifest(store), expected_hours=4)

    def test_a_plain_store_still_may_not_climb(self) -> None:
        with self.assertRaises(ManifestError):
            validate_bin_manifest(self.manifest(self.segment("../tmp2m.zarr")), expected_hours=4)


class SegmentedAssetsTest(unittest.TestCase):
    def test_each_segment_is_an_asset_of_its_own(self) -> None:
        from xuebuild.stac import manifest_assets

        segments_of = lambda name: {  # noqa: E731
            "byteLength": 14,
            "segments": [
                {"path": f"../../gfs.segments/202610112200/0a1b2c3d/{name}", "byteLength": 7, "crc32": "deadbeef"},
                {"path": f"../../gfs.segments/202610112300/4e5f6a7b/{name}", "byteLength": 7, "crc32": "feedface"},
            ],
        }
        manifest = {
            "bundles": [
                {
                    "variable": "tmp2m",
                    "zarr": segments_of("tmp2m.zarr"),
                    "variants": [{"width": 9, "height": 5, "bandwidth": 1, "zarr": segments_of("tmp2m.half.zarr")}],
                }
            ]
        }
        assets = manifest_assets(manifest, "01234567")
        self.assertEqual(assets["tmp2m@202610112300"]["href"], "../../gfs.segments/202610112300/4e5f6a7b/tmp2m.zarr")
        self.assertEqual(assets["tmp2m@202610112200"]["xue:segment"], "202610112200")
        self.assertEqual(assets["tmp2m-half@202610112200"]["xue:tier"], "half")
        self.assertNotIn("tmp2m", assets)


if __name__ == "__main__":
    unittest.main()

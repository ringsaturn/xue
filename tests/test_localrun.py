"""`build-local`: one archived run fetched, cropped and encoded into a
directory of its own, nothing uploaded.

The fetch is mocked to hand back a committed crop fixture, so these run the
real converter (the reference encoder) on a one-frame run. The series
companions expected are read from the source table, not spelled here.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from tests._support import FIXTURES, ClassTempRoot, requires_gdal
from xuebuild import cli, localrun
from xuebuild.binconvert import published_bundle_ids
from xuebuild.localrun import LocalRunError, build_local_run, parse_bbox
from xuebuild.sources import source_spec


class _LocalRunCase(ClassTempRoot):
    """A whole-domain build with series companions and a cropped one without,
    of the same few bundles."""

    model: str
    run_id: str
    fixture: Path
    wanted: tuple[str, ...]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.source = source_spec(cls.model)
        cls.bundles = tuple(bundle_id for bundle_id in cls.wanted if bundle_id in published_bundle_ids(cls.source))
        cls.whole_dir = cls.root / "whole"
        cls.whole, cls.whole_fetch = cls._build(cls.whole_dir, bbox=None, series=True)
        grid = cls.whole["grid"]
        # A box well inside the fixture's window, so the crop is a strict one.
        west = grid["firstLongitude"] + grid["longitudeStep"] * grid["width"] * 0.25
        east = grid["firstLongitude"] + grid["longitudeStep"] * grid["width"] * 0.6
        north = grid["firstLatitude"] + grid["latitudeStep"] * grid["height"] * 0.25
        south = grid["firstLatitude"] + grid["latitudeStep"] * grid["height"] * 0.6
        cls.bbox = (west, south, east, north)
        cls.cropped_dir = cls.root / "cropped"
        cls.cropped, _ = cls._build(cls.cropped_dir, bbox=cls.bbox, series=False)

    @classmethod
    def _build(cls, output_dir: Path, *, bbox, series: bool):
        with (
            mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}),
            mock.patch.object(localrun, "fetch_run", return_value=[cls.fixture]) as fetch,
        ):
            report = build_local_run(
                cls.model,
                cls.run_id,
                0,
                bbox=bbox,
                bundle_ids=cls.bundles,
                output_dir=output_dir,
                raw_root=cls.root / "raw" / "local",
                work_root=cls.root / "work",
                profile="quality",
                force=False,
                force_download=False,
                series=series,
            )
        return report, fetch

    def manifest(self, directory: Path) -> dict:
        return json.loads((directory / "manifest.json").read_text(encoding="utf-8"))

    def test_fetches_only_the_bundles_inputs_into_the_local_raw_root(self) -> None:
        (run, hours, raw_root), options = self.whole_fetch.call_args
        self.assertEqual((run.id, hours, raw_root), (self.run_id, 0, self.root / "raw" / "local"))
        self.assertEqual(options["model"], self.model)
        self.assertTrue(options["input_ids"])
        self.assertLessEqual(set(options["input_ids"]), set(self.source.input_variable_ids))

    def test_output_holds_only_the_manifest_and_stores(self) -> None:
        for directory in (self.whole_dir, self.cropped_dir):
            manifest = self.manifest(directory)
            posters = {bundle["poster"]["path"] for bundle in manifest["bundles"] if "poster" in bundle}
            for entry in directory.iterdir():
                with self.subTest(directory=directory.name, entry=entry.name):
                    if entry.is_dir():
                        self.assertTrue(entry.name.endswith(".zarr"))
                    else:
                        # The poster is the manifest's own first paint, named
                        # by it; nothing else (no .xue, no pointer, no STAC).
                        self.assertIn(entry.name, {"manifest.json", *posters})

    def test_manifest_names_one_store_per_bundle_and_the_requested_hours(self) -> None:
        for directory in (self.whole_dir, self.cropped_dir):
            manifest = self.manifest(directory)
            self.assertEqual(manifest["forecastHours"], 0)
            self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], list(self.bundles))
            for bundle in manifest["bundles"]:
                self.assertNotIn("path", bundle)
                self.assertTrue((directory / bundle["zarr"]["path"]).is_dir())

    def test_series_companions_follow_the_source_table(self) -> None:
        expected = [bundle_id for bundle_id in self.bundles if bundle_id in self.source.series_bundle_ids]
        manifest = self.manifest(self.whole_dir)
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"] if "series" in bundle], expected)
        self.assertEqual(
            sorted(entry.name for entry in self.whole_dir.glob("*.series.zarr")),
            sorted(f"{bundle_id}.series.zarr" for bundle_id in expected),
        )

    def test_no_series_drops_every_companion(self) -> None:
        manifest = self.manifest(self.cropped_dir)
        self.assertFalse(any("series" in bundle for bundle in manifest["bundles"]))
        self.assertEqual(list(self.cropped_dir.glob("*.series.zarr")), [])

    def test_bbox_crops_the_grid_to_cover_the_box(self) -> None:
        whole, cropped = self.whole["grid"], self.cropped["grid"]
        self.assertLess(cropped["width"], whole["width"])
        self.assertLess(cropped["height"], whole["height"])
        west, south, east, north = self.bbox
        last_longitude = cropped["firstLongitude"] + cropped["longitudeStep"] * (cropped["width"] - 1)
        last_latitude = cropped["firstLatitude"] + cropped["latitudeStep"] * (cropped["height"] - 1)
        tolerance = 1e-6
        self.assertLessEqual(cropped["firstLongitude"], west + tolerance)
        self.assertGreaterEqual(last_longitude, east - tolerance)
        self.assertGreaterEqual(cropped["firstLatitude"], north - tolerance)
        self.assertLessEqual(last_latitude, south + tolerance)
        for bundle in self.manifest(self.cropped_dir)["bundles"]:
            self.assertEqual((bundle["grid"]["width"], bundle["grid"]["height"]), (cropped["width"], cropped["height"]))


@requires_gdal
class GfsLocalRunTests(_LocalRunCase, unittest.TestCase):
    root_prefix = "xue-localrun-gfs-"
    model = "gfs"
    run_id = "2026081406"
    fixture = FIXTURES / "gfs.2026081406.f000.crop.grib2"
    wanted = ("tmp2m", "prmsl", "wind10m")


@requires_gdal
class HrrrLocalRunTests(_LocalRunCase, unittest.TestCase):
    root_prefix = "xue-localrun-hrrr-"
    model = "hrrr"
    run_id = "2026091100"
    fixture = FIXTURES / "hrrr.2026091100.f000.crop.grib2"
    wanted = ("tmp2m", "gust", "tcdc", "cref", "wind10m")


class RequestTests(unittest.TestCase):
    def test_latest_is_refused(self) -> None:
        with self.assertRaisesRegex(LocalRunError, "not latest"):
            build_local_run(
                "gfs",
                "latest",
                24,
                bbox=None,
                bundle_ids=None,
                output_dir=Path("unused"),
                raw_root=Path("unused"),
                work_root=Path("unused"),
            )

    def test_unknown_bundle_is_refused_before_fetching(self) -> None:
        with mock.patch.object(localrun, "fetch_run") as fetch, self.assertRaisesRegex(LocalRunError, "not \\['nope'\\]"):
            build_local_run(
                "gfs",
                "2026081406",
                0,
                bbox=None,
                bundle_ids=("tmp2m", "nope"),
                output_dir=Path("unused"),
                raw_root=Path("unused"),
                work_root=Path("unused"),
            )
        fetch.assert_not_called()

    def test_bbox_parses_west_south_east_north(self) -> None:
        self.assertEqual(parse_bbox("-107,25.5,-93,37"), (-107.0, 25.5, -93.0, 37.0))
        for value in ("-107,25.5,-93", "a,b,c,d"):
            with self.subTest(value=value), self.assertRaises(LocalRunError):
                parse_bbox(value)

    def test_cli_writes_into_data_local_and_fetches_into_raw_local(self) -> None:
        with mock.patch.object(cli, "build_local_run", return_value={}) as build, mock.patch("builtins.print"):
            status = cli.main(
                [
                    "build-local",
                    "--model",
                    "hrrr",
                    "--run",
                    "2025011512",
                    "--hours",
                    "18",
                    "--bbox=-107,25.5,-93,37",
                    "--bundles",
                    "tmp2m",
                    "wind10m",
                    "--no-series",
                ]
            )
        self.assertEqual(status, 0)
        (model, run, hours), options = build.call_args
        self.assertEqual((model, run, hours), ("hrrr", "2025011512", 18))
        self.assertEqual(options["output_dir"], Path("data/local/hrrr.2025011512"))
        self.assertEqual(options["raw_root"], Path("data/raw/local"))
        self.assertEqual(options["bbox"], (-107.0, 25.5, -93.0, 37.0))
        self.assertEqual(options["bundle_ids"], ("tmp2m", "wind10m"))
        self.assertFalse(options["series"])

    def test_cli_defaults_to_the_whole_axis_domain_and_bundle_list(self) -> None:
        with mock.patch.object(cli, "build_local_run", return_value={}) as build, mock.patch("builtins.print"):
            cli.main(["build-local", "--model", "gfs", "--run", "2025011506"])
        (_, _, hours), options = build.call_args
        self.assertEqual(hours, source_spec("gfs").horizon_hours)
        self.assertIsNone(options["bbox"])
        self.assertIsNone(options["bundle_ids"])
        self.assertTrue(options["series"])


if __name__ == "__main__":
    unittest.main()

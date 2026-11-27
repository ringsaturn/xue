"""The CWA radar composite over Taiwan: registry, the document parse, the
datum shift, the frame cache, the window's run, the fetch, and the series
through both encoders.

``tests/fixtures/cwa.202611271120.crop.json`` and
``cwa.202611271130.crop.json`` are two real ``O-A0059-001`` documents of
the agency's open data bucket (19:20 and 19:30 Taiwan time on 2026-11-27,
11:20 and 11:30 UTC), cut to the 40 x 40 points of the showers northeast of
the island (25.5–25.9875°N, 122.6–123.0875°E on TWD67) with the header kept
(``tests/fixtures/README.md``). The parse runs on NumPy alone; the cache and
the series need the optional ``cwa`` group (xarray + netCDF4), the way the
aurora tests need theirs.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import (
    ClassTempRoot,
    FIXTURES,
    TempRoot,
    assert_native_matches,
    requires_gdal,
    requires_native_source,
    stored_bundle,
)
from xuebuild import binconvert, cwaradar
from xuebuild.errors import DownloadError
from xuebuild.feeds import CwaRadar, fetcher
from xuebuild.fetch import observation_window_start
from xuebuild.model import GfsRun
from xuebuild.sources import source_spec
from xuebuild.stac import _source_prose

CWA = source_spec("cwa")
EARLY = FIXTURES / "cwa.202611271120.crop.json"
LATE = FIXTURES / "cwa.202611271130.crop.json"
EARLY_TIME = datetime(2026, 11, 27, 11, 20, tzinfo=UTC)
LATE_TIME = datetime(2026, 11, 27, 11, 30, tzinfo=UTC)


def _installed(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


NETCDF = _installed("netCDF4") and _installed("xarray")


def _document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _full(stamp: str = "2026-11-27T19:20:00+08:00") -> str:
    """A document on the published grid: the fixture's header, every point
    no echo but one ramp of echoes along the first row."""
    document = _document(EARLY)
    parameters = document["cwaopendata"]["dataset"]["datasetInfo"]["parameterSet"]
    height, width = cwaradar.GRID_SHAPE
    parameters.update(
        {
            "GridDimensionX": str(width),
            "GridDimensionY": str(height),
            "StartPointLongitude": "115.0",
            "StartPointLatitude": "18.0",
            "DateTime": stamp,
        }
    )
    values = np.full(height * width, -99.0)
    values[:60] = np.arange(60, dtype=np.float64)
    values[-1] = -999.0
    document["cwaopendata"]["dataset"]["contents"]["content"] = ",".join(f"{value:.3E}" for value in values)
    return json.dumps(document, ensure_ascii=False, indent=1)


class RegistryTests(unittest.TestCase):
    def test_the_source_is_a_fetched_window_of_reflectivity(self) -> None:
        self.assertEqual((CWA.window_hours, CWA.cadence_seconds, CWA.cycle_hours), (3, 600, 1))
        self.assertEqual((CWA.input_variable_ids, CWA.bundle_scalar_ids, CWA.core_bundle_ids), (("cref",),) * 3)
        self.assertTrue(CWA.observation and CWA.series_file and CWA.fetched and CWA.live)
        self.assertEqual((CWA.manifest_model, CWA.product), ("CWA-RADAR", "taiwan-cref"))

    def test_the_grid_is_the_agency_s_grid(self) -> None:
        self.assertEqual(CWA.production_grid, cwaradar.GRID_SHAPE[::-1])

    def test_the_catalog_prose_carries_the_required_attribution(self) -> None:
        prose = _source_prose(CWA)
        self.assertEqual(prose["providers"][0]["name"], "Central Weather Administration")
        self.assertIn("TWD67", prose["description"])
        self.assertEqual(prose["links"][0]["rel"], "license")
        self.assertIn("Central Weather Administration", prose["links"][0]["title"])

    def test_the_feed_is_registered_under_the_source_s_family(self) -> None:
        self.assertIsInstance(fetcher(CWA), CwaRadar)


class ParseTests(unittest.TestCase):
    def test_a_document_is_read_in_utc_with_its_sentinels_missing(self) -> None:
        mosaic = cwaradar.parse(EARLY.read_bytes())
        self.assertEqual(mosaic.valid_time, EARLY_TIME)
        self.assertEqual(mosaic.values.shape, (40, 40))
        self.assertEqual((mosaic.first_longitude, mosaic.first_latitude, mosaic.step), (122.6, 25.5, 0.0125))
        raw = np.array(_document(EARLY)["cwaopendata"]["dataset"]["contents"]["content"].split(","), dtype=float)
        raw = raw.reshape(40, 40)
        np.testing.assert_array_equal(np.isnan(mosaic.values), np.isin(raw, (-99.0, -999.0)))
        self.assertTrue((raw > 0).any(), "the fixture holds echoes")
        np.testing.assert_array_equal(mosaic.values[raw > -90], raw[raw > -90])

    def test_the_axes_are_moved_onto_wgs84_by_one_offset(self) -> None:
        mosaic = cwaradar.parse(EARLY.read_bytes())
        self.assertEqual(mosaic.longitudes[0], 122.6081)
        self.assertEqual(mosaic.latitudes[0], 25.4982)
        self.assertEqual(mosaic.latitudes[-1], 25.9857)
        np.testing.assert_allclose(np.diff(mosaic.longitudes), 0.0125, atol=1e-12)

    def test_the_head_names_the_time(self) -> None:
        head = EARLY.read_bytes()[: cwaradar.HEAD_BYTES]
        self.assertEqual(cwaradar.parse_head(head.decode("utf-8")), EARLY_TIME)

    def test_a_short_document_is_refused(self) -> None:
        document = _document(EARLY)
        content = document["cwaopendata"]["dataset"]["contents"]
        content["content"] = content["content"].rsplit(",", 1)[0]
        with self.assertRaisesRegex(DownloadError, "1599 values"):
            cwaradar.parse(json.dumps(document))

    def test_a_time_off_the_ten_minute_marks_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "ten-minute"):
            cwaradar.parse_time("2026-11-27T19:25:00+08:00")

    def test_a_time_without_an_offset_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "offset"):
            cwaradar.parse_time("2026-11-27T19:20:00")

    def test_another_unit_is_refused(self) -> None:
        document = _document(EARLY)
        document["cwaopendata"]["dataset"]["datasetInfo"]["parameterSet"]["Reflectivity"] = "mm"
        with self.assertRaisesRegex(DownloadError, "not dBZ"):
            cwaradar.parse(json.dumps(document))

    def test_the_published_grid_passes_and_a_crop_does_not(self) -> None:
        cwaradar.check_grid(cwaradar.parse(_full()))
        with self.assertRaisesRegex(DownloadError, "grid moved"):
            cwaradar.check_grid(cwaradar.parse(EARLY.read_bytes()))

    def test_a_frame_name_and_its_time_round_trip(self) -> None:
        name = cwaradar.frame_name(EARLY_TIME)
        self.assertEqual(name, "cwa_20261127112000.nc")
        self.assertEqual(cwaradar.frame_time(Path(name)), EARLY_TIME)


class WindowStartTests(TempRoot, unittest.TestCase):
    """Where a window starts while the cache has not filled it: the run is
    the hour of the window's first frame, as the pointer requires."""

    root_prefix = "xue-cwa-"

    def _touch(self, moment: datetime) -> None:
        variable_dir = self.root / cwaradar.FRAMES_DIRNAME / cwaradar.VARIABLE_DIR
        variable_dir.mkdir(parents=True, exist_ok=True)
        (variable_dir / cwaradar.frame_name(moment)).write_bytes(b"")

    def test_a_cold_cache_starts_at_the_newest_frame_s_hour(self) -> None:
        newest = datetime(2026, 11, 27, 11, 30, tzinfo=UTC)
        self.assertEqual(observation_window_start(CWA, newest, 3, self.root), datetime(2026, 11, 27, 11, 0, tzinfo=UTC))

    def test_a_partial_cache_starts_at_its_earliest_frame(self) -> None:
        self._touch(datetime(2026, 11, 27, 10, 10, tzinfo=UTC))
        newest = datetime(2026, 11, 27, 11, 30, tzinfo=UTC)
        self.assertEqual(observation_window_start(CWA, newest, 3, self.root), datetime(2026, 11, 27, 10, 0, tzinfo=UTC))

    def test_a_full_cache_starts_a_whole_window_back(self) -> None:
        self._touch(datetime(2026, 11, 27, 6, 0, tzinfo=UTC))
        newest = datetime(2026, 11, 27, 11, 30, tzinfo=UTC)
        self.assertEqual(observation_window_start(CWA, newest, 3, self.root), datetime(2026, 11, 27, 9, 0, tzinfo=UTC))

    def test_latest_resolves_through_a_range_read_of_the_head(self) -> None:
        data = EARLY.read_bytes()
        calls: list[int] = []

        def head(length: int) -> bytes:
            calls.append(length)
            return data[:length]

        self.assertEqual(cwaradar.latest_slot(fetch_head=head), EARLY_TIME)
        self.assertEqual(calls, [cwaradar.HEAD_BYTES])


@unittest.skipUnless(NETCDF, "the cwa group (xarray + netCDF4) is not installed")
class FetchTests(TempRoot, unittest.TestCase):
    root_prefix = "xue-cwa-"

    def setUp(self) -> None:
        super().setUp()
        self.run = GfsRun(datetime(2026, 11, 27, 11, 0, tzinfo=UTC))

    def test_a_fetch_writes_the_frame_and_the_window_and_grows_with_it(self) -> None:
        payloads = iter([_full("2026-11-27T19:20:00+08:00").encode(), _full("2026-11-27T19:40:00+08:00").encode()])
        outputs = cwaradar.fetch_run(
            CWA, self.run, 3, self.root, force=False, input_ids=None, fetch_bytes=lambda url: next(payloads)
        )
        destination = self.root / f"cwa.{self.run.id}"
        self.assertEqual(outputs, [destination / f"cwa.{self.run.id}.nc"])
        record = json.loads((destination / "fetch.json").read_text(encoding="utf-8"))
        self.assertEqual((record["model"], record["cadenceSeconds"]), ("cwa", 600))
        self.assertEqual(record["grid"], {"width": 921, "height": 881})
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-11-27T11:20:00Z"])
        # The next round adds its mosaic; 11:30 was never fetched, so the
        # window lists the two it has. ``force`` keeps the cache.
        cwaradar.fetch_run(CWA, self.run, 3, self.root, force=True, input_ids=None, fetch_bytes=lambda url: next(payloads))
        record = json.loads((destination / "fetch.json").read_text(encoding="utf-8"))
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-11-27T11:20:00Z", "2026-11-27T11:40:00Z"])
        values, latitudes, longitudes = cwaradar.read_frame(
            self.root / cwaradar.FRAMES_DIRNAME / cwaradar.VARIABLE_DIR / cwaradar.frame_name(EARLY_TIME)
        )
        self.assertEqual(values.shape, cwaradar.GRID_SHAPE)
        self.assertEqual((longitudes[0], latitudes[0]), (115.0081, 17.9982))
        np.testing.assert_allclose(values[0, :60], np.arange(60), atol=0.05)
        self.assertTrue(np.isnan(values[0, 60:]).all() and np.isnan(values[-1, -1]))

    def test_a_crop_is_refused_by_the_fetch(self) -> None:
        with self.assertRaisesRegex(DownloadError, "grid moved"):
            cwaradar.fetch_run(CWA, self.run, 3, self.root, force=False, input_ids=None, fetch_bytes=lambda url: EARLY.read_bytes())

    def test_an_input_the_source_does_not_publish_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "publishes"):
            cwaradar.fetch_run(CWA, self.run, 3, self.root, force=False, input_ids=("prate",), fetch_bytes=lambda url: b"")

    def test_a_window_with_nothing_cached_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "holds no frame"):
            cwaradar.write_window(self.root / cwaradar.FRAMES_DIRNAME, self.run.time, 3, self.root / "x.nc")


@requires_gdal
@unittest.skipUnless(NETCDF, "the cwa group (xarray + netCDF4) is not installed")
class ConversionTests(ClassTempRoot, unittest.TestCase):
    """The two cropped mosaics as one window through the reference encoder,
    and the native encoder held to it byte for byte."""

    root_prefix = "xue-cwa-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        frames_dir = cls.root / cwaradar.FRAMES_DIRNAME
        cls.mosaics = [cwaradar.parse(path.read_bytes()) for path in (EARLY, LATE)]
        for mosaic in cls.mosaics:
            cwaradar.write_frame(frames_dir, mosaic)
        run = GfsRun(datetime(2026, 11, 27, 11, 0, tzinfo=UTC))
        cls.inputs = cls.root / f"cwa.{run.id}"
        cwaradar.write_window(frames_dir, run.time, 3, cls.inputs / cwaradar.series_name(run))
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                cls.inputs,
                cls.root / "out",
                model="cwa",
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    def test_the_window_is_a_ten_minute_axis_on_wgs84(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["cref"])
        out = self.root / "out"
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("CWA-RADAR", "taiwan-cref"))
        self.assertEqual(manifest["runTime"], "2026-11-27T11:00:00Z")
        bundle = stored_bundle(out, "cref")
        # 11:20 and 11:30 from 11:00.
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 600, "firstFrameOffset": 2, "frameCount": 2, "frameStep": 1})
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (40, 40))
        self.assertAlmostEqual(grid["firstLongitude"], 122.6081, places=9)
        self.assertAlmostEqual(grid["firstLatitude"], 25.9857, places=9)
        self.assertAlmostEqual(grid["longitudeStep"], 0.0125, places=12)
        self.assertAlmostEqual(grid["latitudeStep"], -0.0125, places=12)
        self.assertEqual(bundle.metadata["variables"][0]["unit"], "dBZ")
        for frame, offset in enumerate((2, 3)):
            codes = np.asarray(bundle.decode_plane(1, offset)).reshape(40, 40)
            values = self.mosaics[frame].values[::-1]
            # North row first in the bundle; an echo where the mosaic has one
            # of at least half a codebook step (the bottom code is no echo,
            # no radar and anything that rounds to 0 dBZ alike).
            np.testing.assert_array_equal(codes > 0, np.nan_to_num(values, nan=-1.0) >= 0.25)

    @requires_native_source("cwa")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, self.inputs, self.root / "out", self.report, self.root / "native", model="cwa")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

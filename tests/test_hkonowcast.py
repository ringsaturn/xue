"""The Hong Kong Observatory rainfall nowcast: registry, the CSV parse, the
sampling onto a regular grid, the window's run, the fetch, and the series
through both encoders.

``tests/fixtures/hkonowcast.202611122000.crop.csv`` and
``hkonowcast.202611122036.crop.csv`` are two real issues of the
Observatory's ``Gridded_rainfall_nowcast.csv`` (20:00 and 20:36 Hong Kong
time on 2026-11-12, 12:00 and 12:36 UTC), cut to the 24 x 24 points around
the showers west of Hong Kong (21.708–22.124°N, 113.034–113.482°E) with the
header kept (``tests/fixtures/README.md``). Everything but the conversion
runs on NumPy alone: the series is written by the pure-NumPy classic
NetCDF writer the CMA dust source uses.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime, timedelta
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
from xuebuild import binconvert, hkonowcast
from xuebuild.errors import DownloadError
from xuebuild.fetch import observation_window_start, resolve_run
from xuebuild.feeds import HkoNowcast, fetcher
from xuebuild.idx import ByteRange
from xuebuild.model import GfsRun
from xuebuild.sources import source_spec
from xuebuild.stac import _source_prose

HKO = source_spec("hkonowcast")
EARLY = FIXTURES / "hkonowcast.202611122000.crop.csv"
LATE = FIXTURES / "hkonowcast.202611122036.crop.csv"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _head(path: Path) -> object:
    """A ``fetch_head`` over a fixture: its first ``length`` bytes."""
    data = path.read_bytes()
    return lambda length: data[:length]


class RegistryTests(unittest.TestCase):
    def test_the_source_is_a_fetched_window_of_rates(self) -> None:
        self.assertTrue(HKO.observation and HKO.series_file and HKO.fetched and HKO.live)
        self.assertEqual((HKO.window_hours, HKO.cadence_seconds, HKO.cycle_hours), (3, 360, 1))
        self.assertEqual((HKO.input_variable_ids, HKO.bundle_scalar_ids, HKO.core_bundle_ids), (("prate",),) * 3)
        # An accumulation over the half hour, published as its mean rate.
        self.assertEqual(HKO.statistical_processes, (("prate", 0),))
        self.assertEqual(HKO.variant_factors, ())

    def test_the_window_reaches_the_last_frame_from_any_issue_minute(self) -> None:
        for minute in range(0, 60, 12):
            issue = datetime(2026, 11, 12, 12, minute, tzinfo=UTC)
            start = hkonowcast.first_hour(issue)
            last = issue + timedelta(minutes=hkonowcast.LEAD_MINUTES[-1])
            with self.subTest(minute=minute):
                self.assertLessEqual(last, start + timedelta(hours=HKO.window_hours))

    def test_the_catalog_prose_carries_the_required_attribution(self) -> None:
        prose = _source_prose(HKO)
        self.assertEqual(prose["providers"][0]["name"], "Hong Kong Observatory")
        self.assertIn("Hong Kong Observatory (DATA.GOV.HK)", prose["description"])
        self.assertEqual(prose["links"][0]["href"], "https://data.gov.hk/en/terms-and-conditions")


class ParseTests(unittest.TestCase):
    def test_an_issue_is_read_in_utc_with_its_four_accumulations(self) -> None:
        issue = hkonowcast.parse_csv(_text(LATE))
        self.assertEqual(issue.issue_time, datetime(2026, 11, 12, 12, 36, tzinfo=UTC))
        self.assertEqual(
            issue.valid_times,
            tuple(datetime(2026, 11, 12, 12, 36, tzinfo=UTC) + timedelta(minutes=m) for m in (30, 60, 90, 120)),
        )
        self.assertEqual(issue.accumulations.shape, (4, 24, 24))
        self.assertEqual((issue.latitudes[0], issue.latitudes[-1]), (21.708, 22.124))
        self.assertEqual((issue.longitudes[0], issue.longitudes[-1]), (113.034, 113.482))
        self.assertEqual(float(issue.accumulations.max()), 3.27)
        self.assertTrue((issue.accumulations >= 0).all())

    def test_rows_land_by_coordinate_not_by_order(self) -> None:
        lines = _text(LATE).splitlines()
        shuffled = "\n".join([lines[0], *reversed(lines[1:])]) + "\n"
        a, b = hkonowcast.parse_csv(_text(LATE)), hkonowcast.parse_csv(shuffled)
        np.testing.assert_array_equal(a.accumulations, b.accumulations)
        # The first data row is the north-west point of the first accumulation.
        first = lines[1].split(",")
        self.assertEqual(float(a.accumulations[0, -1, 0]), float(first[4]))

    def test_the_head_names_the_issue(self) -> None:
        self.assertEqual(hkonowcast.parse_head(_text(LATE)[: hkonowcast.HEAD_BYTES]), datetime(2026, 11, 12, 12, 36, tzinfo=UTC))

    def test_a_truncated_file_is_refused(self) -> None:
        lines = _text(LATE).splitlines()
        with self.assertRaisesRegex(DownloadError, "values, not|every point"):
            hkonowcast.parse_csv("\n".join(lines[:-1]))

    def test_a_changed_header_is_refused(self) -> None:
        text = _text(LATE).replace("Half-hourly Nowcast Accumulated Rainfall (mm)", "Rainfall (mm)", 1)
        with self.assertRaisesRegex(DownloadError, "columns"):
            hkonowcast.parse_csv(text)

    def test_accumulations_off_the_half_hours_are_refused(self) -> None:
        text = _text(LATE).replace(",202611122236,", ",202611122230,")
        with self.assertRaisesRegex(DownloadError, "ends its accumulations"):
            hkonowcast.parse_csv(text)

    def test_two_issues_in_one_file_are_refused(self) -> None:
        lines = _text(LATE).splitlines()
        lines[-1] = lines[-1].replace("202611122036", "202611122048", 1)
        with self.assertRaisesRegex(DownloadError, "issue times"):
            hkonowcast.parse_csv("\n".join(lines))


class RegridTests(unittest.TestCase):
    def test_the_grid_is_whole_thousandths_and_every_cell_is_one_point(self) -> None:
        issue = hkonowcast.parse_csv(_text(LATE))
        window = hkonowcast.regrid(issue)
        self.assertEqual((window.longitudes.size, window.latitudes.size), (25, 24))
        self.assertEqual((window.longitudes[0], window.latitudes[0]), (113.031, 21.708))
        np.testing.assert_allclose(np.diff(window.longitudes), 0.019, atol=1e-12)
        np.testing.assert_allclose(np.diff(window.latitudes), 0.018, atol=1e-12)
        self.assertFalse(np.isnan(window.rates).any())
        # The Mercator rows are a quarter of a row at most from the regular
        # ones, so each row is its own point; the coarser columns repeat one.
        rows = hkonowcast.nearest_points(issue.latitudes, window.latitudes)
        columns = hkonowcast.nearest_points(issue.longitudes, window.longitudes)
        self.assertEqual(sorted(rows.tolist()), list(range(24)))
        self.assertEqual(set(columns.tolist()), set(range(24)))
        # Rates are the accumulations doubled, never mixed.
        self.assertEqual(set(np.unique(window.rates)), set(np.unique(issue.accumulations * 2.0)))

    def test_a_target_past_the_points_is_left_empty(self) -> None:
        points = np.array([1.0, 2.0, 3.0])
        self.assertEqual(hkonowcast.nearest_points(points, np.array([0.4, 0.6, 2.5, 3.4, 3.6])).tolist(), [-1, 0, 1, 2, -1])

    def test_the_axis_is_snapped_to_the_step(self) -> None:
        axis = hkonowcast.regular_axis(np.array([21.328, 23.487]), 18)
        self.assertEqual((axis[0], axis[-1], axis.size), (21.33, 23.49, 121))
        axis = hkonowcast.regular_axis(np.array([112.956, 115.291]), 19)
        self.assertEqual((axis[0], axis[-1], axis.size), (112.955, 115.292, 124))
        self.assertEqual(HKO.production_grid, (124, 121))


class WindowTests(TempRoot, unittest.TestCase):
    root_prefix = "xue-hkonowcast-"

    def test_the_newest_slot_is_the_last_accumulation_s_end(self) -> None:
        self.assertEqual(hkonowcast.latest_slot(fetch_head=_head(LATE)), datetime(2026, 11, 12, 14, 36, tzinfo=UTC))

    def test_the_window_starts_at_the_first_frame_s_hour(self) -> None:
        # 12:36 issue: frames 13:06 to 14:36, so 13Z — the plain rule's
        # 12Z would name an hour the window holds no frame of.
        self.assertEqual(
            observation_window_start(HKO, datetime(2026, 11, 12, 14, 36, tzinfo=UTC), 3, self.root),
            datetime(2026, 11, 12, 13, 0, tzinfo=UTC),
        )
        # 12:00 issue: frames 12:30 to 14:00, so 12Z.
        self.assertEqual(
            observation_window_start(HKO, datetime(2026, 11, 12, 14, 0, tzinfo=UTC), 3, self.root),
            datetime(2026, 11, 12, 12, 0, tzinfo=UTC),
        )
        # Every other feed keeps the plain rule.
        self.assertEqual(
            observation_window_start(source_spec("mrms"), datetime(2026, 11, 12, 14, 36, tzinfo=UTC), 3, self.root),
            datetime(2026, 11, 12, 12, 0, tzinfo=UTC),
        )

    def test_latest_resolves_through_a_range_read_of_the_head(self) -> None:
        data = LATE.read_bytes()

        def fetch_range(url: str, byte_range: ByteRange) -> bytes:
            self.assertEqual(url, hkonowcast.URL)
            return data[byte_range.start : byte_range.end + 1]

        with mock.patch("xuebuild.fetch.fetch_range", side_effect=fetch_range):
            self.assertEqual(resolve_run("latest", hours=3, model="hkonowcast").id, "2026111213")
            self.assertEqual(resolve_run("2026111213", hours=3, model="hkonowcast").id, "2026111213")
            # Only the newest issue is served: an earlier window cannot be fetched.
            with self.assertRaisesRegex(DownloadError, "has not fully landed"):
                resolve_run("2026111212", hours=3, model="hkonowcast")


class FetchTests(TempRoot, unittest.TestCase):
    root_prefix = "xue-hkonowcast-"

    def _fetch(self, run: str, path: Path = LATE) -> list[Path]:
        data = path.read_bytes()
        return hkonowcast.fetch_run(
            HKO,
            GfsRun(datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=UTC)),
            3,
            self.root,
            force=False,
            input_ids=None,
            fetch_bytes=lambda url: data,
        )

    def test_a_fetch_writes_the_series_the_csv_and_the_record(self) -> None:
        outputs = self._fetch("2026111213")
        directory = self.root / "hkonowcast.2026111213"
        self.assertEqual(outputs, [directory / "hkonowcast.2026111213.nc"])
        self.assertTrue(outputs[0].read_bytes().startswith(b"CDF\x01"))
        self.assertTrue((directory / "hkonowcast.202611121236.csv").is_file())
        record = json.loads((directory / "fetch.json").read_text(encoding="utf-8"))
        self.assertEqual((record["model"], record["run"], record["cadenceSeconds"]), ("hkonowcast", "2026111213", 360))
        self.assertEqual(record["issueTime"], "2026-11-12T12:36:00Z")
        self.assertEqual(
            [frame["slot"] for frame in record["frames"]],
            ["2026-11-12T13:06:00Z", "2026-11-12T13:36:00Z", "2026-11-12T14:06:00Z", "2026-11-12T14:36:00Z"],
        )
        self.assertEqual(record["grid"], {"width": 25, "height": 24})

    def test_an_issue_under_another_run_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "falls under run 2026111213, not 2026111212"):
            self._fetch("2026111212")

    def test_an_input_the_source_does_not_publish_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "publishes"):
            hkonowcast.fetch_run(
                HKO, GfsRun(datetime(2026, 11, 12, 13, tzinfo=UTC)), 3, self.root,
                force=False, input_ids=("cref",), fetch_bytes=lambda url: b"",
            )

    def test_the_feed_is_registered_under_the_source_s_family(self) -> None:
        self.assertIsInstance(fetcher(HKO), HkoNowcast)


@requires_gdal
class ConversionTests(ClassTempRoot, unittest.TestCase):
    """Both issues through the reference encoder, and the native encoder
    held to it byte for byte."""

    root_prefix = "xue-hkonowcast-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.builds = {}
        for name, path in (("early", EARLY), ("late", LATE)):
            issue = hkonowcast.parse_csv(path.read_text(encoding="utf-8"))
            run = GfsRun(hkonowcast.first_hour(issue.issue_time))
            inputs = cls.root / name / f"hkonowcast.{run.id}"
            hkonowcast.write_issue(issue, inputs / hkonowcast.series_name(run), "test")
            with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
                report = binconvert.convert_bin(
                    inputs,
                    cls.root / name / "out",
                    model="hkonowcast",
                    work_root=cls.root / name / "work",
                    manifest_path=cls.root / name / "out" / "manifest.json",
                )
            cls.builds[name] = (issue, inputs, report)

    def test_a_late_issue_is_a_six_minute_axis_from_the_next_hour(self) -> None:
        issue, _, report = self.builds["late"]
        self.assertEqual([bundle["variable"] for bundle in report["bundles"]], ["prate"])
        out = self.root / "late" / "out"
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("HKO-NOWCAST", "hongkong-prate"))
        self.assertEqual(manifest["runTime"], "2026-11-12T13:00:00Z")
        bundle = stored_bundle(out, "prate")
        # 13:06, 13:36, 14:06 and 14:36 from 13:00.
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 360, "firstFrameOffset": 1, "frameCount": 4, "frameStep": 5})
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (25, 24))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (113.031, 22.122))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.019, -0.018))
        variable = bundle.metadata["variables"][0]
        self.assertEqual(variable["unit"], "mm/h")
        self.assertEqual(variable["parameter"]["typeOfStatisticalProcessing"], 0)
        window = hkonowcast.regrid(issue)
        for frame, offset in enumerate((1, 6, 11, 16)):
            codes = np.asarray(bundle.decode_plane(1, offset)).reshape(window.rates.shape[1:])
            # North row first in the bundle; rain where the nowcast has rain.
            np.testing.assert_array_equal(codes > 0, window.rates[frame][::-1] > 0)

    def test_an_issue_on_the_hour_is_a_half_hourly_axis(self) -> None:
        out = self.root / "early" / "out"
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual(manifest["runTime"], "2026-11-12T12:00:00Z")
        bundle = stored_bundle(out, "prate")
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 1800, "firstFrameOffset": 1, "frameCount": 4, "frameStep": 1})

    @requires_native_source("hkonowcast")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        for name in ("early", "late"):
            with self.subTest(issue=name):
                _, inputs, report = self.builds[name]
                assert_native_matches(self, inputs, self.root / name / "out", report, self.root / name / "native", model="hkonowcast")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""The JMA source: the agency's precipitation nowcast over Japan, an
observation that is fetched — through the jma-radar tool — as one NetCDF
series per window.

Three things are new with it. A fetched observation whose frames arrive as
a series file (``SourceSpec.series_file``), read the way the CMA archive
file is, with the window's first hour as the run and the five-minute slots
as the axis (``cadence_seconds`` on the NetCDF path). A fetch that is an
external tool (``xuebuild/jmacli.py``): ``jma-radar window`` lists the
agency's ``targetTimes``, decodes the tiles and writes the series, and
``fetch.py`` turns its summary into the ``fetch.json`` the rolling publish
reads. And a source published under ``prate`` alone: the agency publishes
intensity classes, not a reflectivity, and the class representative rates
all sit inside the precipitation codebook.

``tests/fixtures/jma.2026091601.crop.nc`` is three frames of the 2026-09-16
01Z hour (01:05, 01:10 and 01:20; 01:15 left out, so the axis lists its
offsets) cropped to 200 x 150 cells over the Kii Peninsula and Shikoku
(135E to 137E, 34.5N to 33N) on a rainy morning: every class from 0 mm/h to
the 50-80 mm/h band is present, plus a corner of no data. 400 x 300 cells
of the published 0.005° grid.
"""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np

from xuebuild import binconvert, fetch, jmacli, native, observation, zstdcli
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    JMA_BBOX,
    JMA_FRAMES_DIRNAME,
    JMA_GRID_STEP,
    JMA_TARGET_TIMES_URL,
    _fetch_jma_run,
    _jma_run_is_complete,
    jma_frame_name,
    jma_window_slots,
    latest_jma_slot,
    latest_observation_slot,
    parse_jma_listing,
    resolve_run,
    window_summary,
)
from xuebuild.manifest import MODEL_CORE_BUNDLES, validate_bin_manifest
from xuebuild.model import GfsRun
from xuebuild.quantize import PROFILES
from xuebuild.sources import SOURCES, source_spec
from xuebuild.stac import _source_prose

FIXTURES = Path(__file__).parent / "fixtures"
SERIES = FIXTURES / "jma.2026091601.crop.nc"
JMA = source_spec("jma")

requires_gdal = unittest.skipUnless(
    shutil.which("gdalinfo") is not None and shutil.which("gdal_translate") is not None, "GDAL is not on PATH"
)


def stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def listing(*times: str, forecasts: tuple[tuple[str, str], ...] = (), elements: tuple[str, ...] = ("hrpns", "hrpns_nd")) -> str:
    """A ``targetTimes_N1`` document: analyses newest first, plus any
    forecast entries (basetime, validtime) the way ``N2`` spells them."""
    entries = [{"basetime": t, "validtime": t, "elements": list(elements)} for t in sorted(times, reverse=True)]
    entries += [{"basetime": b, "validtime": v, "elements": ["hrpns"]} for b, v in forecasts]
    return json.dumps(entries)


THREE_HOURS = tuple(
    f"20260915{hour:02d}{minute:02d}00" if hour < 24 else f"20260916{hour - 24:02d}{minute:02d}00"
    for hour in range(22, 26)
    for minute in range(0, 60, 5)
    if (hour, minute) >= (22, 10) and (hour, minute) <= (25, 10)
)


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_a_fetched_series_file_observation(self) -> None:
        self.assertTrue(JMA.observation and JMA.fetched and JMA.live and JMA.series_file)
        self.assertEqual((JMA.manifest_model, JMA.product, JMA.latest_filename), ("JMA-HRPNS", "japan-prate", "latest-jma.json"))
        self.assertEqual((JMA.window_hours, JMA.horizon_hours, JMA.cadence_seconds), (3, 3, 300))
        self.assertEqual((JMA.input_variable_ids, JMA.bundle_scalar_ids, JMA.core_bundle_ids), (("prate",),) * 3)
        self.assertEqual(MODEL_CORE_BUNDLES["JMA-HRPNS"], ("prate",))
        self.assertFalse(JMA.video)
        # The grid the fetch asks the tool for is the one a complete build wants.
        west, south, east, north = JMA_BBOX
        self.assertEqual(JMA.production_grid, (round((east - west) / JMA_GRID_STEP), round((north - south) / JMA_GRID_STEP)))

    def test_the_series_file_sources_are_the_two_netcdf_ones(self) -> None:
        self.assertEqual([spec.id for spec in SOURCES.values() if spec.series_file], ["cma", "jma"])
        # Both fetched: the CMA window out of its archive (tests/test_cma.py).
        self.assertTrue(source_spec("cma").fetched)

    def test_the_catalog_prose_names_the_agency_and_its_terms(self) -> None:
        prose = _source_prose(JMA)
        self.assertEqual(prose["providers"][0]["name"], "Japan Meteorological Agency")
        self.assertIn("気象庁", prose["description"])
        self.assertEqual(prose["links"][0]["rel"], "license")

    def test_every_class_representative_rate_has_a_code_of_its_own(self) -> None:
        """The agency's ten classes, at the representative rates jma-radar
        writes, must not collapse in the precipitation codebook — the
        reason the source ships prate rather than a wider registry."""
        rates = np.array([0.0, 0.5, 3.0, 7.5, 15.0, 25.0, 40.0, 65.0, 100.0])
        for profile, codebooks in PROFILES.items():
            codes = codebooks["prate"].quantize(rates)
            self.assertEqual(len(set(codes.tolist())), len(rates), profile)
            self.assertLess(int(codes.max()), codebooks["prate"].overflow_code, profile)


class ListingTests(unittest.TestCase):
    def test_analyses_are_the_entries_whose_times_agree(self) -> None:
        payload = listing("20260916010000", "20260916010500", forecasts=(("20260916010500", "20260916013500"),))
        self.assertEqual(parse_jma_listing(payload), [stamp("20260916010000"), stamp("20260916010500")])
        # Another element's analyses are not this product's.
        self.assertEqual(parse_jma_listing(listing("20260916010000", elements=("thns",))), [])
        self.assertEqual(parse_jma_listing('[{"basetime": 1}, "x", {"basetime": "bad", "validtime": "bad", "elements": ["hrpns"]}]'), [])
        with self.assertRaises(DownloadError):
            parse_jma_listing("not json")
        with self.assertRaises(DownloadError):
            parse_jma_listing('{"basetime": "20260916010000"}')

    def test_the_newest_analysis_ends_the_live_window(self) -> None:
        fetched: list[str] = []

        def fetch_text(url: str) -> str:
            fetched.append(url)
            return listing(*THREE_HOURS)

        self.assertEqual(latest_jma_slot(JMA, fetch=fetch_text), stamp("20260916011000"))
        self.assertEqual(fetched, [JMA_TARGET_TIMES_URL])
        with self.assertRaises(DownloadError):
            latest_jma_slot(JMA, fetch=lambda url: "[]")

    def test_a_window_is_the_listed_analyses_between_its_hour_and_its_end(self) -> None:
        slots = jma_window_slots(JMA, GfsRun(stamp("20260915230000")), 3, fetch=lambda url: listing(*THREE_HOURS))
        self.assertEqual((slots[0], slots[-1], len(slots)), (stamp("20260915230000"), stamp("20260916011000"), 27))
        # The window is inclusive of its end.
        slots = jma_window_slots(JMA, GfsRun(stamp("20260915230000")), 2, fetch=lambda url: listing(*THREE_HOURS))
        self.assertEqual(slots[-1], stamp("20260916010000"))

    def test_a_named_window_is_complete_when_the_listing_reaches_past_it(self) -> None:
        fetch_text = lambda url: listing(*THREE_HOURS)  # noqa: E731
        self.assertTrue(_jma_run_is_complete(JMA, GfsRun(stamp("20260915230000")), 2, fetch=fetch_text))
        # The live window's end is ahead of the newest frame.
        self.assertFalse(_jma_run_is_complete(JMA, GfsRun(stamp("20260915230000")), 3, fetch=fetch_text))
        # A window older than the listing cannot be fetched either.
        self.assertFalse(_jma_run_is_complete(JMA, GfsRun(stamp("20260915200000")), 1, fetch=fetch_text))
        self.assertFalse(_jma_run_is_complete(JMA, GfsRun(stamp("20260915230000")), 1, fetch=lambda url: "[]"))

    def test_resolve_run_latest_starts_two_hours_before_the_newest_frame(self) -> None:
        with mock.patch.object(fetch, "jma_analysis_times", return_value=parse_jma_listing(listing(*THREE_HOURS))):
            self.assertEqual(resolve_run("latest", hours=3, model="jma").id, "2026091523")
            self.assertEqual(latest_observation_slot(JMA), stamp("20260916011000"))
            # A named window: complete or refused.
            self.assertEqual(resolve_run("2026091523", hours=2, model="jma").id, "2026091523")
            with self.assertRaisesRegex(DownloadError, "not fully landed"):
                resolve_run("2026091523", hours=3, model="jma")
        with self.assertRaises(DownloadError):
            latest_observation_slot(source_spec("gfs"))


class ObservationCadenceTests(unittest.TestCase):
    """The NetCDF ingest on a source with a cadence: the window's hour is
    the run, the slots are the axis (the rule the MRMS frames follow)."""

    def _inspect(self, times: list[str], source=JMA) -> observation.ObservationSeries:
        epoch_seconds = [int(stamp(t).timestamp()) for t in times]
        info = {
            "size": [400, 300],
            "geoTransform": [135.0, 0.005, 0.0, 34.5, 0.0, -0.005],
            "metadata": {"": {"time#units": "seconds since 1970-01-01T00:00:00+00:00"}},
            "bands": [
                {
                    "band": index + 1,
                    "unit": "mm/h",
                    "scale": 0.5,
                    "offset": 0.0,
                    "noDataValue": 255,
                    "metadata": {"": {"NETCDF_DIM_time": str(seconds)}},
                }
                for index, seconds in enumerate(epoch_seconds)
            ],
        }
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(observation, "dataset_info", return_value=info),
        ):
            return observation.inspect_observation(Path("jma.nc"), source)

    def test_the_run_is_the_hour_of_the_first_slot(self) -> None:
        series = self._inspect(["20260916010500", "20260916011000", "20260916012000"])
        first = series.frames[0]["prate"]
        self.assertEqual(first.run_time, stamp("20260916010000"))
        self.assertEqual(series.lead_seconds, [300, 600, 1200])
        self.assertEqual(str(first.path), 'NETCDF:"jma.nc":prate')
        self.assertEqual(series.plane_source.fill_values, (255.0, 127.5))
        self.assertEqual(series.plane_source.fill_replacement, 0.0)

    def test_times_off_the_mark_snap_down_to_their_slot(self) -> None:
        series = self._inspect(["20260916010542", "20260916011001"])
        self.assertEqual(series.lead_seconds, [300, 600])
        # Two frames in one slot are one frame too many.
        with self.assertRaisesRegex(ConversionError, "strictly increasing"):
            self._inspect(["20260916010500", "20260916010542"])

    def test_a_source_without_a_cadence_keeps_its_first_frame_as_the_run(self) -> None:
        """The rule the archive files followed before the CMA source took a
        cadence of its own: no snapping, the first frame is the run."""
        import dataclasses

        archive_file = dataclasses.replace(source_spec("cma"), cadence_seconds=None, window_hours=None)
        info_times = ["20260916010500", "20260916011100"]
        epoch_seconds = [int(stamp(t).timestamp()) for t in info_times]
        info = {
            "metadata": {"": {"time#units": "seconds since 1970-01-01T00:00:00+00:00"}},
            "bands": [
                {"band": i + 1, "unit": "dBZ", "scale": 0.1, "offset": 0.0, "noDataValue": 32767, "metadata": {"": {"NETCDF_DIM_time": str(s)}}}
                for i, s in enumerate(epoch_seconds)
            ],
        }
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(observation, "dataset_info", return_value=info),
        ):
            series = observation.inspect_observation(Path("radar.nc"), archive_file)
        self.assertEqual(series.frames[0]["cref"].run_time, stamp("20260916010500"))
        self.assertEqual(series.lead_seconds, [0, 360])


class ToolTests(unittest.TestCase):
    def test_the_command_defaults_to_this_interpreter_and_takes_an_override(self) -> None:
        with mock.patch.dict(os.environ, {jmacli.COMMAND_VARIABLE: ""}):
            self.assertEqual(jmacli.command()[1:], ["-m", "jma_radar"])
        with mock.patch.dict(os.environ, {jmacli.COMMAND_VARIABLE: "uv run --quiet jma-radar"}):
            self.assertEqual(jmacli.command(), ["uv", "run", "--quiet", "jma-radar"])

    def test_a_missing_tool_says_how_to_install_it(self) -> None:
        with mock.patch.dict(os.environ, {jmacli.COMMAND_VARIABLE: "/nonexistent/jma-radar"}):
            with self.assertRaisesRegex(DownloadError, "install it with"):
                jmacli.version()
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="No module named jma_radar\n")
        with mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(DownloadError, "No module named jma_radar"):
                jmacli.version()

    def test_the_window_command_is_built_from_the_source_parameters(self) -> None:
        summary = {"frames": [{"basetime": "20260916010500", "validtime": "20260916010500", "path": None}], "grid": {}}
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(summary), stderr="")
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(subprocess, "run", return_value=completed) as run, mock.patch.dict(
                os.environ, {jmacli.COMMAND_VARIABLE: "jma-radar"}
            ):
                result = jmacli.fetch_window(
                    start="2026091601",
                    hours=3,
                    zoom=8,
                    step=0.01,
                    bbox=(121.0, 20.5, 149.0, 45.5),
                    method="max",
                    frames_dir=Path(root) / "frames",
                    output=Path(root) / "jma.2026091601" / "jma.2026091601.nc",
                    variable="prate",
                    concurrency=6,
                )
            self.assertEqual(result, summary)
            arguments = run.call_args.args[0]
            self.assertEqual(arguments[:2], ["jma-radar", "window"])
            for flag, value in (
                ("--start", "2026091601"),
                ("--hours", "3"),
                ("--zoom", "8"),
                ("--step", "0.01"),
                ("--bbox", "121,20.5,149,45.5"),
                ("--method", "max"),
                ("--variable", "prate"),
                ("--concurrency", "6"),
            ):
                self.assertEqual(arguments[arguments.index(flag) + 1], value, flag)
            self.assertIn("--json", arguments)
            # The directories the tool writes into exist before it runs.
            self.assertTrue((Path(root) / "frames").is_dir() and (Path(root) / "jma.2026091601").is_dir())

    def test_a_failed_or_silent_tool_is_a_download_error(self) -> None:
        arguments = dict(
            start="2026091601", hours=3, zoom=8, step=0.01, bbox=JMA_BBOX, method="max", variable="prate", concurrency=6
        )
        with tempfile.TemporaryDirectory() as root:
            paths = dict(frames_dir=Path(root) / "frames", output=Path(root) / "out.nc")
            for completed, reason in (
                (subprocess.CompletedProcess([], 1, stdout="", stderr="no analysis listed between ...\n"), "no analysis listed"),
                (subprocess.CompletedProcess([], 0, stdout="not json", stderr=""), "no JSON summary"),
                (subprocess.CompletedProcess([], 0, stdout="[]", stderr=""), "unexpected summary"),
            ):
                with mock.patch.object(subprocess, "run", return_value=completed):
                    with self.assertRaisesRegex(DownloadError, reason):
                        jmacli.fetch_window(**arguments, **paths)


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-jma-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run = GfsRun(stamp("20260916010000"))

    def _summary(self, *validtimes: str, write_output: Path | None = None) -> dict:
        frames_dir = self.root / JMA_FRAMES_DIRNAME / "ae1681567c"
        frames = []
        for validtime in validtimes:
            frame = frames_dir / f"hrpns_{validtime}.nc"
            frame.parent.mkdir(parents=True, exist_ok=True)
            frame.write_bytes(b"frame")
            frames.append({"basetime": validtime, "validtime": validtime, "path": str(frame), "observed_cells": 1})
        if write_output is not None:
            write_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(SERIES, write_output)
        return {"start": "2026091601", "hours": 3, "variable": "prate", "grid": {"nlat": 300, "nlon": 400}, "frames": frames}

    def test_the_fetch_writes_the_series_and_a_fetch_record(self) -> None:
        output = self.root / "jma.2026091601" / jma_frame_name(JMA, self.run)
        summary = self._summary("20260916010500", "20260916011000", write_output=output)
        with (
            mock.patch.object(jmacli, "version", return_value="0.2.0"),
            mock.patch.object(fetch, "jma_window_slots", return_value=[stamp("20260916010500"), stamp("20260916011000")]),
            mock.patch.object(jmacli, "fetch_window", return_value=summary) as window,
        ):
            paths = fetch.fetch_run(self.run, 3, self.root, model="jma")
        self.assertEqual(paths, [output])
        self.assertEqual(window.call_args.kwargs["frames_dir"], self.root / JMA_FRAMES_DIRNAME)
        self.assertEqual(window.call_args.kwargs["variable"], "prate")
        record = json.loads((output.parent / "fetch.json").read_text())
        self.assertEqual((record["model"], record["run"], record["cadenceSeconds"]), ("jma", "2026091601", 300))
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-09-16T01:05:00Z", "2026-09-16T01:10:00Z"])
        # What the rolling publish reads, the same shape as an MRMS window's.
        window_record = window_summary(output.parent)
        self.assertEqual((window_record["frameCount"], window_record["latestSlot"]), (2, "2026-09-16T01:10:00Z"))

    def test_an_empty_window_and_a_missing_series_are_errors(self) -> None:
        with (
            mock.patch.object(jmacli, "version", return_value="0.2.0"),
            mock.patch.object(fetch, "jma_window_slots", return_value=[stamp("20260916010500")]),
            mock.patch.object(fetch.time, "sleep"),
        ):
            with mock.patch.object(jmacli, "fetch_window", return_value=self._summary()):
                with self.assertRaisesRegex(DownloadError, "lists no analysis"):
                    _fetch_jma_run(JMA, self.run, 3, self.root, force=False, input_ids=None)
            with mock.patch.object(jmacli, "fetch_window", return_value=self._summary("20260916010500")):
                with self.assertRaisesRegex(DownloadError, "wrote no series"):
                    _fetch_jma_run(JMA, self.run, 3, self.root, force=False, input_ids=None)
        with self.assertRaisesRegex(DownloadError, "publishes"):
            _fetch_jma_run(JMA, self.run, 3, self.root, force=False, input_ids=("cref",))

    def test_a_forced_fetch_discards_the_window_s_cached_frames(self) -> None:
        output = self.root / "jma.2026091601" / jma_frame_name(JMA, self.run)
        summary = self._summary("20260916010500", "20260916011000", write_output=output)
        cached = [Path(frame["path"]) for frame in summary["frames"]]
        stale = cached[0].parent / "hrpns_20260916000000.nc"
        stale.write_bytes(b"outside the window")
        with (
            mock.patch.object(jmacli, "version", return_value="0.2.0"),
            mock.patch.object(fetch, "jma_window_slots", return_value=[stamp("20260916010500"), stamp("20260916011000")]),
            mock.patch.object(jmacli, "fetch_window", side_effect=lambda **kwargs: summary),
        ):
            # The tool would refetch what force removed; here it is mocked, so
            # the removal itself is what is checked.
            def refetch(**kwargs):
                self.assertFalse(any(path.exists() for path in cached))
                return summary

            with mock.patch.object(jmacli, "fetch_window", side_effect=refetch):
                _fetch_jma_run(JMA, self.run, 3, self.root, force=True, input_ids=None)
        self.assertTrue(stale.exists())

    def test_the_tool_is_asked_again_while_its_listing_lags_the_build_s(self) -> None:
        """The build read a listing reaching 01:10; the tool's copy, a CDN
        edge of its own, still ends at 01:05. It is asked again after a
        wait, and the window published is the one with 01:10 in it."""
        output = self.root / "jma.2026091601" / jma_frame_name(JMA, self.run)
        short = self._summary("20260916010500", write_output=output)
        full = self._summary("20260916010500", "20260916011000", write_output=output)
        with (
            mock.patch.object(jmacli, "version", return_value="0.2.0"),
            mock.patch.object(fetch, "jma_window_slots", return_value=[stamp("20260916010500"), stamp("20260916011000")]),
            mock.patch.object(jmacli, "fetch_window", side_effect=[short, short, full]) as window,
            mock.patch.object(fetch.time, "sleep") as sleep,
        ):
            _fetch_jma_run(JMA, self.run, 3, self.root, force=False, input_ids=None)
        self.assertEqual(window.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(fetch.JMA_LISTING_RETRY_SECONDS)] * 2)
        record = json.loads((output.parent / "fetch.json").read_text())
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-09-16T01:05:00Z", "2026-09-16T01:10:00Z"])

    def test_a_tool_that_never_catches_up_still_delivers_its_window(self) -> None:
        output = self.root / "jma.2026091601" / jma_frame_name(JMA, self.run)
        short = self._summary("20260916010500", write_output=output)
        with (
            mock.patch.object(jmacli, "version", return_value="0.2.0"),
            mock.patch.object(fetch, "jma_window_slots", return_value=[stamp("20260916010500"), stamp("20260916011000")]),
            mock.patch.object(jmacli, "fetch_window", return_value=short) as window,
            mock.patch.object(fetch.time, "sleep") as sleep,
            self.assertLogs(fetch.LOG.name, level="WARNING") as logs,
        ):
            _fetch_jma_run(JMA, self.run, 3, self.root, force=False, input_ids=None)
        self.assertEqual(window.call_count, 1 + fetch.JMA_LISTING_RETRIES)
        self.assertEqual(sleep.call_count, fetch.JMA_LISTING_RETRIES)
        self.assertTrue(any("still delivers" in line for line in logs.output))
        record = json.loads((output.parent / "fetch.json").read_text())
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-09-16T01:05:00Z"])

    def test_a_listing_behind_the_tool_s_does_not_hold_the_fetch_up(self) -> None:
        output = self.root / "jma.2026091601" / jma_frame_name(JMA, self.run)
        full = self._summary("20260916010500", "20260916011000", write_output=output)
        with (
            mock.patch.object(jmacli, "version", return_value="0.2.0"),
            mock.patch.object(fetch, "jma_window_slots", return_value=[stamp("20260916010500")]),
            mock.patch.object(jmacli, "fetch_window", return_value=full) as window,
            mock.patch.object(fetch.time, "sleep") as sleep,
        ):
            _fetch_jma_run(JMA, self.run, 3, self.root, force=False, input_ids=None)
        self.assertEqual((window.call_count, sleep.call_count), (1, 0))


@requires_gdal
class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-jma-"))
        inputs = cls.root / "jma.2026091601"
        inputs.mkdir()
        shutil.copy(SERIES, inputs / "jma.2026091601.nc")
        cls.inputs = inputs
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                inputs,
                cls.root / "out",
                model="jma",
                skip_video=True,
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_the_window_is_a_five_minute_axis_from_the_hour(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["prate"])
        self.assertEqual(self.report["videos"], [])
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("JMA-HRPNS", "japan-prate"))
        self.assertEqual(manifest["runTime"], "2026-09-16T01:00:00Z")
        self.assertEqual(manifest["forecastHours"], 1)
        validate_bin_manifest(manifest, expected_hours=1, require_core_variables=True)
        bundle = read_bundle(self.root / "out" / "prate.xue")
        # 01:05, 01:10 and 01:20 from the 01:00 run: offsets listed, since
        # 01:15 is not there. Three 128-cell tiles across four rows.
        self.assertEqual(
            bundle.metadata["time"],
            {"unitSeconds": 300, "firstFrameOffset": 1, "frameCount": 3, "frameOffsets": [1, 2, 4]},
        )
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (400, 300))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.005, -0.005))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (135.0025, 34.4975))
        variable = bundle.metadata["variables"][0]
        self.assertEqual(variable["unit"], "mm/h")
        self.assertEqual(variable["parameter"]["parameterCategory"], 1)
        # The gap at 01:15 ends a temporal group: two groups for three frames.
        self.assertEqual((bundle.frame_count, len(bundle.groups), bundle.tiles.count), (3, 2, 12))
        # The classes come through as their representative rates: decoding a
        # plane gives the codebook's nearest rate to each, and nothing else.
        codebook = PROFILES["quality"]["prate"]
        rates = codebook.decode(np.asarray(bundle.decode_plane(1, 1)).reshape(300, 400))
        representative = np.array([0.0, 0.5, 3.0, 7.5, 15.0, 25.0, 40.0, 65.0, 100.0])
        nearest = representative[np.abs(rates[:, :, None] - representative[None, None, :]).argmin(axis=2)]
        self.assertLess(float(np.abs(rates - nearest).max() / representative.max()), 0.05)
        self.assertGreater(len(set(np.round(nearest.ravel(), 1).tolist())), 6)
        self.assertGreater(float(nearest.max()), 40.0)
        for name in ("prate.half.xue", "prate.poster.bin"):
            self.assertTrue((self.root / "out" / name).is_file(), name)

    def test_a_complete_build_wants_the_production_grid(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "5600x5000 grid"):
                binconvert.convert_bin(
                    self.inputs, self.root / "complete", model="jma", skip_video=True, require_complete=True, expected_hours=3
                )

    def test_a_run_directory_holds_exactly_one_series(self) -> None:
        two = self.root / "two"
        two.mkdir()
        shutil.copy(SERIES, two / "a.nc")
        shutil.copy(SERIES, two / "b.nc")
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "exactly one NetCDF series"):
                binconvert.convert_bin(two, self.root / "two-out", model="jma", skip_video=True)

    @unittest.skipUnless(native.knows_source("jma"), f"the installed {native.DISTRIBUTION} wheel predates the JMA source")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI")
        subject = self.root / "native"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            report = native.convert_bin(self.inputs, subject, model="jma", skip_video=True, manifest_path=subject / "manifest.json")
        if report["zstdVersion"] != self.report["zstdVersion"]:
            self.skipTest("libzstd differs between the reference and the wheel")
        names = sorted(path.name for path in (self.root / "out").iterdir())
        self.assertEqual(sorted(path.name for path in subject.iterdir()), names)
        for name in names:
            with self.subTest(artifact=name):
                self.assertTrue(filecmp.cmp(self.root / "out" / name, subject / name, shallow=False), name)


if __name__ == "__main__":
    unittest.main()

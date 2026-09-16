"""The CMA source (``cma``, the source once called ``radar``): the
agency's level-3 composite reflectivity mosaic over China, an observation
that is fetched — through the cma-radar tool — out of the tool's own
archive, one Zarr store per UTC day, as one NetCDF series per window.

What is new with it against the JMA source it is shaped like: the fetch is
a read of an archive rather than a decode of tiles (``cma-radar
window``, ``xuebuild/cmacli.py``), so nothing is cached between rounds
and the newest frame is whatever the archive has written; a named window
is complete when the archive has moved past its end; the six-minute
cadence (``cadence_seconds`` 360, ``unitSeconds`` 360) on a source that
was, before the archive, a local file whose first frame was the run — a
new id for the new shape, so a wheel that knows ``radar`` is never taken
for one that knows ``cma``; and a showcase case that takes either a
window of the archive or a local file.

``tests/fixtures/cma.2026091609.crop.nc`` is three frames of the
2026-09-16 09Z hour (09:00, 09:06 and 09:18; 09:12 left out, so the axis
lists its offsets) cropped to 128 x 128 cells over Hubei and Hunan (112.5E
to 118.1E, 33.75N to 28.1N) on a convective afternoon, returns to 62.5 dBZ.
"""

from __future__ import annotations

import dataclasses
import filecmp
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from xuebuild import binconvert, fetch, native, observation, cmacli, zstdcli
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    CMA_ARCHIVE_VARIABLE,
    CMA_GRID_STEP,
    CMA_PRODUCT,
    CMA_ZOOM,
    _fetch_cma_run,
    cma_archive,
    _cma_run_is_complete,
    latest_observation_slot,
    latest_cma_slot,
    cma_archive_slots,
    cma_frame_name,
    cma_window_slots,
    resolve_run,
    window_summary,
)
from xuebuild.manifest import MODEL_CORE_BUNDLES, validate_bin_manifest
from xuebuild.model import GfsRun
from xuebuild.showcase import parse_case
from xuebuild.sources import SOURCES, source_spec
from xuebuild.stac import _source_prose

FIXTURES = Path(__file__).parent / "fixtures"
SERIES = FIXTURES / "cma.2026091609.crop.nc"
CMA = source_spec("cma")

requires_gdal = unittest.skipUnless(
    shutil.which("gdalinfo") is not None and shutil.which("gdal_translate") is not None, "GDAL is not on PATH"
)


def stamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def slots(start: datetime, end: datetime, *, missing: tuple[datetime, ...] = ()) -> list[str]:
    """Every six-minute slot from ``start`` through ``end`` inclusive, as
    the tool prints them, less the ones the archive never wrote."""
    out, slot = [], start
    while slot <= end:
        if slot not in missing:
            out.append(slot.strftime("%Y-%m-%dT%H:%M:%SZ"))
        slot += timedelta(minutes=6)
    return out


def archive(*written: str) -> mock.Mock:
    """A stand-in for ``cmacli.window``: an archive holding ``written``,
    answering the slots inside whatever window it is asked for."""
    held = sorted(stamp(text) for text in written)

    def window(*, source: str, start: str, hours: int, zoom: int, product: str, output: Path | None) -> dict:
        first = datetime.strptime(start, "%Y%m%d%H").replace(tzinfo=UTC)
        last = first + timedelta(hours=hours)
        inside = [slot for slot in held if first <= slot <= last]
        return {
            "frames": [slot.strftime("%Y-%m-%dT%H:%M:%SZ") for slot in inside],
            "slots": [{"time": slot.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": 1, "index": 0} for slot in inside],
            "grid": None,
            "output": None if output is None else str(output),
        }

    return mock.Mock(side_effect=window)


ARCHIVE = "s3://a-bucket/an-archive"

THREE_HOURS = slots(stamp("2026-09-15T22:12:00Z"), stamp("2026-09-16T01:06:00Z"), missing=(stamp("2026-09-16T00:30:00Z"),))


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_a_fetched_series_file_observation(self) -> None:
        self.assertTrue(CMA.observation and CMA.fetched and CMA.live and CMA.series_file)
        self.assertEqual((CMA.manifest_model, CMA.product, CMA.latest_filename), ("CMA-RADAR", "l3-mst-cref", "latest-cma.json"))
        self.assertEqual((CMA.window_hours, CMA.horizon_hours, CMA.cadence_seconds, CMA.cycle_hours), (3, 3, 360, 1))
        self.assertEqual((CMA.input_variable_ids, CMA.bundle_scalar_ids, CMA.core_bundle_ids), (("cref",),) * 3)
        self.assertEqual(MODEL_CORE_BUNDLES["CMA-RADAR"], ("cref",))
        self.assertFalse(CMA.video)
        self.assertIsNone(CMA.downsample)
        # The grid the archive keeps is the one a complete build wants: the
        # zoom-5 tile grid, 7 x 4 tiles of 256 cells over 67.5E-146.25E and
        # 11.25N-56.25N.
        self.assertEqual(CMA_GRID_STEP, 360 / (256 * 2**CMA_ZOOM))
        self.assertEqual(CMA.production_grid, (round((146.25 - 67.5) / CMA_GRID_STEP), round((56.25 - 11.25) / CMA_GRID_STEP)))
        self.assertEqual(CMA.production_grid, (7 * 256, 4 * 256))
        self.assertEqual(CMA_PRODUCT, "RADAR_L3_MST_CREF_GISJPG_Tiles_CR")

    def test_the_archive_is_named_by_the_environment_alone(self) -> None:
        """The location is private: no default, and an unset variable is
        the operator's error before the tool is asked anything."""
        with mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: ""}):
            with self.assertRaisesRegex(DownloadError, CMA_ARCHIVE_VARIABLE):
                cma_archive()
            with self.assertRaisesRegex(DownloadError, CMA_ARCHIVE_VARIABLE):
                cma_archive_slots(stamp("2026-09-16T09:00:00Z"), 1, window=mock.Mock())
        with mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: "s3://bucket/prefix/"}):
            self.assertEqual(cma_archive(), "s3://bucket/prefix")

    def test_the_series_file_sources_are_both_fetched_now(self) -> None:
        self.assertEqual([spec.id for spec in SOURCES.values() if spec.series_file], ["cma", "jma"])
        self.assertEqual([spec.id for spec in SOURCES.values() if spec.observation], ["cma", "mrms", "jma"])
        self.assertTrue(all(spec.fetched and spec.live for spec in SOURCES.values()))

    def test_the_catalog_prose_names_the_agency_and_the_archive(self) -> None:
        prose = _source_prose(CMA)
        self.assertEqual(prose["providers"][0]["name"], "China Meteorological Administration")
        # Public prose names the data and the agency, never the tooling
        # or the archive behind it.
        self.assertNotIn("cases only", prose["description"])
        for private in ("archive", "bucket", "cma-radar"):
            self.assertNotIn(private, prose["description"], private)


class ArchiveTests(unittest.TestCase):
    """The fetch functions against a stand-in archive."""

    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: ARCHIVE})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_slots_of_a_window_are_what_the_archive_wrote_in_it(self) -> None:
        window = archive(*THREE_HOURS)
        held = cma_archive_slots(stamp("2026-09-15T23:00:00Z"), 2, window=window)
        self.assertEqual((held[0], held[-1], len(held)), (stamp("2026-09-15T23:00:00Z"), stamp("2026-09-16T01:00:00Z"), 20))
        self.assertNotIn(stamp("2026-09-16T00:30:00Z"), held)
        self.assertEqual(
            window.call_args.kwargs,
            dict(source=ARCHIVE, start="2026091523", hours=2, zoom=CMA_ZOOM, product=CMA_PRODUCT, output=None),
        )
        # The same, keyed by a run.
        self.assertEqual(cma_window_slots(CMA, GfsRun(stamp("2026-09-15T23:00:00Z")), 2, window=window), held)
        # A slot the tool spells otherwise is an error, not a guess.
        bad = mock.Mock(return_value={"frames": ["20260916010000"], "slots": []})
        with self.assertRaisesRegex(DownloadError, "not YYYY-MM-DDTHH:MM:SSZ"):
            cma_archive_slots(stamp("2026-09-16T01:00:00Z"), 1, window=bad)

    def test_the_newest_written_slot_ends_the_live_window(self) -> None:
        window = archive(*THREE_HOURS)
        now = stamp("2026-09-16T01:40:00Z")
        self.assertEqual(latest_cma_slot(CMA, now=now, window=window), stamp("2026-09-16T01:06:00Z"))
        # The stores of the last day are asked: from 25 hours before this
        # hour, through the hour after it.
        self.assertEqual((window.call_args.kwargs["start"], window.call_args.kwargs["hours"]), ("2026091500", 26))
        with self.assertRaisesRegex(DownloadError, "no written slot"):
            latest_cma_slot(CMA, now=now, window=archive())

    def test_a_named_window_is_complete_when_the_archive_has_passed_its_end(self) -> None:
        window = archive(*THREE_HOURS)
        run = GfsRun(stamp("2026-09-15T23:00:00Z"))
        self.assertTrue(_cma_run_is_complete(CMA, run, 2, window=window))
        # The live window's end (02:00) is ahead of the newest slot (01:06).
        self.assertFalse(_cma_run_is_complete(CMA, run, 3, window=window))
        # A window whose end slot was never published still completes once
        # the archive has moved past it: the hour after the end is asked.
        gap = archive(*slots(stamp("2026-09-15T23:00:00Z"), stamp("2026-09-16T00:30:00Z"), missing=(stamp("2026-09-16T00:00:00Z"),)))
        self.assertTrue(_cma_run_is_complete(CMA, run, 1, window=gap))
        self.assertEqual(gap.call_args.kwargs["hours"], 2)
        self.assertFalse(_cma_run_is_complete(CMA, run, 1, window=archive()))

    def test_resolve_run_latest_starts_two_hours_before_the_newest_slot(self) -> None:
        with mock.patch.object(cmacli, "window", archive(*THREE_HOURS)):
            with mock.patch.object(fetch, "datetime", wraps=datetime) as clock:
                clock.now.return_value = stamp("2026-09-16T01:40:00Z")
                self.assertEqual(resolve_run("latest", hours=3, model="cma").id, "2026091523")
                self.assertEqual(latest_observation_slot(CMA), stamp("2026-09-16T01:06:00Z"))
            # A named window: complete or refused.
            self.assertEqual(resolve_run("2026091523", hours=2, model="cma").id, "2026091523")
            with self.assertRaisesRegex(DownloadError, "not fully landed"):
                resolve_run("2026091523", hours=3, model="cma")
            # Any hour starts a window.
            self.assertEqual(resolve_run("2026091522", hours=1, model="cma").id, "2026091522")


class ObservationCadenceTests(unittest.TestCase):
    """The NetCDF ingest on the source now that it has a cadence: the
    window's hour is the run and the six-minute slots the axis — the rule
    the JMA series and the MRMS frames follow, where the archive files
    before it took their first frame as the run."""

    def _inspect(self, times: list[str]) -> observation.ObservationSeries:
        info = {
            "metadata": {"": {"time#units": "seconds since 1970-01-01T00:00:00+00:00"}},
            "bands": [
                {
                    "band": index + 1,
                    "unit": "dBZ",
                    "scale": 0.1,
                    "offset": 0.0,
                    "noDataValue": 32767,
                    "metadata": {"": {"NETCDF_DIM_time": str(int(stamp(text).timestamp()))}},
                }
                for index, text in enumerate(times)
            ],
        }
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(observation, "dataset_info", return_value=info),
        ):
            return observation.inspect_observation(Path("cma.nc"), CMA)

    def test_the_run_is_the_hour_of_the_first_slot(self) -> None:
        series = self._inspect(["2026-09-16T09:06:00Z", "2026-09-16T09:12:00Z", "2026-09-16T09:24:00Z"])
        first = series.frames[0]["cref"]
        self.assertEqual(first.run_time, stamp("2026-09-16T09:00:00Z"))
        self.assertEqual(series.lead_seconds, [360, 720, 1440])
        self.assertEqual(series.plane_source.fill_values, (32767.0, 32767.0 * 0.1))
        self.assertEqual(series.plane_source.fill_replacement, 0.0)

    def test_a_series_across_midnight_counts_from_its_first_hour(self) -> None:
        series = self._inspect(["2026-09-15T23:54:00Z", "2026-09-16T00:00:00Z", "2026-09-16T00:06:00Z"])
        self.assertEqual(series.frames[0]["cref"].run_time, stamp("2026-09-15T23:00:00Z"))
        self.assertEqual(series.lead_seconds, [3240, 3600, 3960])


class ToolTests(unittest.TestCase):
    def test_the_command_defaults_to_this_interpreter_and_takes_an_override(self) -> None:
        with mock.patch.dict(os.environ, {cmacli.COMMAND_VARIABLE: ""}):
            # The script beside this interpreter when it is installed, else the bare name.
            command = cmacli.command()
            self.assertEqual(len(command), 1)
            self.assertTrue(command[0] == "cma-radar" or command[0].endswith("/cma-radar"))
        with mock.patch.dict(os.environ, {cmacli.COMMAND_VARIABLE: "uv run --quiet cma-radar"}):
            self.assertEqual(cmacli.command(), ["uv", "run", "--quiet", "cma-radar"])

    def test_a_missing_tool_says_how_to_install_it(self) -> None:
        with mock.patch.dict(os.environ, {cmacli.COMMAND_VARIABLE: "/nonexistent/cma-radar"}):
            with self.assertRaisesRegex(DownloadError, "XUE_CMA_RADAR"):
                cmacli.version()
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="No module named cma_radar\n")
        with mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(DownloadError, "No module named cma_radar"):
                cmacli.version()

    def test_the_window_command_is_built_from_the_source_parameters(self) -> None:
        summary = {"frames": ["2026-09-16T09:00:00Z"], "slots": [{"time": "2026-09-16T09:00:00Z", "status": 1, "index": 90}], "grid": {}, "output": "x"}
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(summary), stderr="")
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "cma.2026091609" / "cma.2026091609.nc"
            with mock.patch.object(subprocess, "run", return_value=completed) as run, mock.patch.dict(
                os.environ, {cmacli.COMMAND_VARIABLE: "cma-radar"}
            ):
                result = cmacli.window(source="s3://archive/radar", start="2026091609", hours=3, zoom=5, product="P", output=output)
                listed = cmacli.window(source="s3://archive/radar", start="2026091609", hours=3, zoom=5, product="P", output=None)
            self.assertEqual(result, summary)
            self.assertEqual(listed, summary)
            arguments = run.call_args_list[0].args[0]
            self.assertEqual(arguments[:2], ["cma-radar", "window"])
            for flag, value in (
                ("--source", "s3://archive/radar"),
                ("--start", "2026091609"),
                ("--hours", "3"),
                ("--zoom", "5"),
                ("--product", "P"),
                ("--out", str(output)),
            ):
                self.assertEqual(arguments[arguments.index(flag) + 1], value, flag)
            self.assertIn("--json", arguments)
            # A listing asks for no series and reads nothing but the index.
            self.assertNotIn("--out", run.call_args_list[1].args[0])
            # The directory the tool writes into exists before it runs.
            self.assertTrue(output.parent.is_dir())

    def test_a_failed_or_silent_tool_is_a_download_error(self) -> None:
        arguments = dict(source="s3://archive/radar", start="2026091609", hours=3, zoom=5, product="P", output=None)
        for completed, reason in (
            (subprocess.CompletedProcess([], 1, stdout="", stderr="no written slot between ...\n"), "no written slot"),
            (subprocess.CompletedProcess([], 0, stdout="not json", stderr=""), "no JSON summary"),
            (subprocess.CompletedProcess([], 0, stdout="[]", stderr=""), "unexpected summary"),
            (subprocess.CompletedProcess([], 0, stdout='{"frames": []}', stderr=""), "unexpected summary"),
        ):
            with mock.patch.object(subprocess, "run", return_value=completed):
                with self.assertRaisesRegex(DownloadError, reason):
                    cmacli.window(**arguments)


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-cma-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run = GfsRun(stamp("2026-09-16T09:00:00Z"))
        patcher = mock.patch.dict(os.environ, {CMA_ARCHIVE_VARIABLE: ARCHIVE})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _window(self, *times: str, write: bool = True) -> mock.Mock:
        def window(*, source: str, start: str, hours: int, zoom: int, product: str, output: Path | None) -> dict:
            if output is not None and write and times:
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(SERIES, output)
            return {
                "frames": list(times),
                "slots": [{"time": text, "status": 1, "index": index} for index, text in enumerate(times)],
                "grid": {"nlat": 1024, "nlon": 1792, "step": CMA_GRID_STEP},
                "output": None if output is None else str(output),
            }

        return mock.Mock(side_effect=window)

    def test_the_fetch_writes_the_series_and_a_fetch_record(self) -> None:
        output = self.root / "cma.2026091609" / cma_frame_name(CMA, self.run)
        window = self._window("2026-09-16T09:06:00Z", "2026-09-16T09:00:00Z", "2026-09-16T09:18:00Z")
        with mock.patch.object(cmacli, "version", return_value="0.2.0"), mock.patch.object(cmacli, "window", window):
            paths = fetch.fetch_run(self.run, 3, self.root, model="cma")
        self.assertEqual(paths, [output])
        self.assertEqual(window.call_args.kwargs, dict(source=ARCHIVE, start="2026091609", hours=3, zoom=CMA_ZOOM, product=CMA_PRODUCT, output=output))
        record = json.loads((output.parent / "fetch.json").read_text())
        self.assertEqual((record["model"], record["run"], record["cadenceSeconds"]), ("cma", "2026091609", 360))
        self.assertNotIn("archive", record)
        # In time order whatever the tool's order, each frame naming the series.
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-09-16T09:00:00Z", "2026-09-16T09:06:00Z", "2026-09-16T09:18:00Z"])
        self.assertEqual({frame["path"] for frame in record["frames"]}, {output.name})
        self.assertEqual(record["grid"]["nlon"], 1792)
        # What the rolling publish reads, the same shape as an MRMS window's.
        window_record = window_summary(output.parent)
        self.assertEqual((window_record["frameCount"], window_record["firstSlot"], window_record["latestSlot"]), (3, "2026-09-16T09:00:00Z", "2026-09-16T09:18:00Z"))

    def test_a_stale_series_is_replaced_and_every_round_reads_afresh(self) -> None:
        output = self.root / "cma.2026091609" / cma_frame_name(CMA, self.run)
        output.parent.mkdir(parents=True)
        output.write_bytes(b"the previous round's window")
        with mock.patch.object(cmacli, "version", return_value="0.2.0"):
            with mock.patch.object(cmacli, "window", self._window("2026-09-16T09:00:00Z")) as window:
                _fetch_cma_run(CMA, self.run, 3, self.root, force=False, input_ids=None)
                _fetch_cma_run(CMA, self.run, 3, self.root, force=True, input_ids=None)
        self.assertEqual(window.call_count, 2)
        self.assertTrue(filecmp.cmp(SERIES, output, shallow=False))

    def test_an_empty_window_and_a_missing_series_are_errors(self) -> None:
        with mock.patch.object(cmacli, "version", return_value="0.2.0"):
            with mock.patch.object(cmacli, "window", self._window()):
                with self.assertRaisesRegex(DownloadError, "no written slot"):
                    _fetch_cma_run(CMA, self.run, 3, self.root, force=False, input_ids=None)
            with mock.patch.object(cmacli, "window", self._window("2026-09-16T09:00:00Z", write=False)):
                with self.assertRaisesRegex(DownloadError, "wrote no series"):
                    _fetch_cma_run(CMA, self.run, 3, self.root, force=False, input_ids=None)
        with self.assertRaisesRegex(DownloadError, "publishes"):
            _fetch_cma_run(CMA, self.run, 3, self.root, force=False, input_ids=("prate",))


class ShowcaseTests(unittest.TestCase):
    def _payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": "demo-cma",
            "title": {locale: "Demo" for locale in _LOCALES},
            "summary": {locale: "Demo summary" for locale in _LOCALES},
            "model": "cma",
            "hours": 3,
            "bbox": [105.0, 14.0, 130.0, 34.0],
            "variables": ["cref"],
        }
        payload.update(overrides)
        return payload

    def test_a_case_names_a_window_of_the_archive_or_a_file(self) -> None:
        fetched = parse_case(self._payload(run="2026091609"))
        self.assertFalse(fetched.from_dataset)
        self.assertEqual(fetched.run, "2026091609")
        local = parse_case(self._payload(dataset="typhoon/series.nc", hours=212))
        self.assertTrue(local.from_dataset)
        self.assertEqual(local.dataset_path.name, "series.nc")
        from xuebuild.showcase import ShowcaseError

        with self.assertRaisesRegex(ShowcaseError, "no run to name"):
            parse_case(self._payload(run="2026091609", dataset="typhoon/series.nc"))
        with self.assertRaisesRegex(ShowcaseError, "run must be"):
            parse_case(self._payload())
        # MRMS is fetched but not a series file: a dataset is still refused.
        with self.assertRaisesRegex(ShowcaseError, "names a dataset"):
            parse_case(self._payload(model="mrms", dataset="x.nc"))


_LOCALES = ("zh", "zh-Hant", "en", "ja", "ko", "de", "fr", "es", "pt", "tr", "ru")


@requires_gdal
class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-cma-"))
        inputs = cls.root / "cma.2026091609"
        inputs.mkdir()
        shutil.copy(SERIES, inputs / "cma.2026091609.nc")
        cls.inputs = inputs
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                inputs,
                cls.root / "out",
                model="cma",
                skip_video=True,
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_the_window_is_a_six_minute_axis_from_the_hour(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["cref"])
        self.assertEqual(self.report["videos"], [])
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("CMA-RADAR", "l3-mst-cref"))
        self.assertEqual(manifest["runTime"], "2026-09-16T09:00:00Z")
        self.assertEqual(manifest["forecastHours"], 1)
        validate_bin_manifest(manifest, expected_hours=1, require_core_variables=True)
        bundle = read_bundle(self.root / "out" / "cref.xue")
        # 09:00, 09:06 and 09:18 from the 09:00 run: offsets listed, since
        # 09:12 is not there. Two 64-cell tiles across two rows.
        self.assertEqual(
            bundle.metadata["time"],
            {"unitSeconds": 360, "firstFrameOffset": 0, "frameCount": 3, "frameOffsets": [0, 1, 3]},
        )
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (128, 128))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (CMA_GRID_STEP, -CMA_GRID_STEP))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (112.52197265625, 33.72802734375))
        variable = bundle.metadata["variables"][0]
        self.assertEqual(variable["unit"], "dBZ")
        self.assertEqual((variable["parameter"]["parameterCategory"], variable["parameter"]["parameterNumber"]), (16, 5))
        # The gap at 09:12 ends a temporal group: two groups for three frames.
        self.assertEqual((bundle.frame_count, len(bundle.groups), bundle.tiles.count), (3, 2, 4))
        for name in ("cref.half.xue", "cref.poster.bin"):
            self.assertTrue((self.root / "out" / name).is_file(), name)

    def test_a_complete_build_wants_the_production_grid(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "1792x1024 grid"):
                binconvert.convert_bin(
                    self.inputs, self.root / "complete", model="cma", skip_video=True, require_complete=True, expected_hours=3
                )

    @unittest.skipUnless(native.knows_source("cma"), f"the installed {native.DISTRIBUTION} wheel predates the cma source")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI")
        subject = self.root / "native"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            report = native.convert_bin(self.inputs, subject, model="cma", skip_video=True, manifest_path=subject / "manifest.json")
        if report["zstdVersion"] != self.report["zstdVersion"]:
            self.skipTest("libzstd differs between the reference and the wheel")
        names = sorted(path.name for path in (self.root / "out").iterdir())
        self.assertEqual(sorted(path.name for path in subject.iterdir()), names)
        for name in names:
            with self.subTest(artifact=name):
                self.assertTrue(filecmp.cmp(self.root / "out" / name, subject / name, shallow=False), name)


class StacTests(unittest.TestCase):
    def test_a_source_stripped_of_its_feed_has_no_collection(self) -> None:
        """No registered source is without a feed any more; the rule is
        checked on a copy of the CMA source with its pointer taken off."""
        from xuebuild import stac

        archive_only = dataclasses.replace(CMA, latest_filename=None)
        self.assertFalse(archive_only.live)


if __name__ == "__main__":
    unittest.main()

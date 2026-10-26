"""The ARPEGE source: Météo-France's global model on its 0.25° GLOB025
grid (the GFS grid), from the surface package ``SP1`` of the data.gouv.fr
ARPEGE 0.25° dataset — four multi-field, CCSDS-packed GRIB2 objects per
cycle, one per range of steps, with no index. The fetch downloads the
packages the axis reaches and cuts each frame out of them by the records'
GRIB2 identities and valid times.

Nothing is new to the encoders: the records are matched under existing
identities and alternates (the run total as AIFS's WMO 0/1/52 in kg/m²,
the hourly maximum gust as ECMWF's 10 m alternate, the cloud cover as
AIFS's 0/6/1 on the ground surface, the terrain as the geometric height
0/3/6 in metres under the orography's alternate). The packages carry no precipitation
total at the analysis; the fetch writes the zero a run total is there, so
the converter differences the first step against it and the first hour's
rate is that hour's total rather than nothing. The terrain is in none of
the ``SP1`` packages: the fetch reads its one record out of the first
``SP2`` package by Range, walking the messages' heads.

``tests/fixtures/arpege.2026102512.f000.crop.grib2`` and ``f001`` are an
80 x 80 cell window from 2°E to 21.75°E and 30.25°N to 50°N (France's
Mediterranean coast, the Alps, Italy and the central Mediterranean) of
the analysis and the first step of the 2026-10-25 12Z cycle, every record
the source fetches in source order: seven at the analysis, the zero total
among them, the terrain last and no gust, and seven at the step.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from tests._support import FIXTURES, ParityCase, ReferenceBuild, assert_gdalinfo_agrees, requires_gdalinfo
from xuebuild import fetch, grib2, quantize
from xuebuild.binconvert import analysis_optional_ids, published_bundle_ids, video_variable_ids
from xuebuild.binformat import read_bundle
from xuebuild.errors import DownloadError
from xuebuild.fetch import (
    ARPEGE_PACKAGES,
    ARPEGE_STATIC_PACKAGE,
    _run_is_complete,
    arpege_package,
    arpege_package_url,
    model_object_url,
    resolve_run,
)
from xuebuild.idx import ByteRange
from xuebuild.model import GfsRun
from xuebuild.sources import source_spec
from xuebuild.stac import prose_document

FIXTURE_FRAMES = [FIXTURES / "arpege.2026102512.f000.crop.grib2", FIXTURES / "arpege.2026102512.f001.crop.grib2"]
ARPEGE = source_spec("arpege")
GFS = source_spec("gfs")
RUN = GfsRun(datetime(2026, 10, 25, 12, tzinfo=UTC))

#: The reference build every class here reads (tests/_support.py).
REFERENCE = ReferenceBuild("arpege", "2026102512", FIXTURE_FRAMES)


def tearDownModule() -> None:
    REFERENCE.cleanup()


class SourceRegistryTests(unittest.TestCase):
    def test_the_basic_set(self) -> None:
        self.assertEqual(published_bundle_ids(ARPEGE), ("tmp2m", "prate", "prmsl", "gust", "tcdc", "orog", "wind10m"))
        self.assertEqual(ARPEGE.input_variable_ids, ("tmp2m", "tp", "ugrd10m", "vgrd10m", "prmsl", "gust", "tcdc", "orog"))
        self.assertTrue(ARPEGE.accumulated_precipitation)
        # The total is written at the analysis by the fetch; only the gust
        # is absent there.
        self.assertEqual(ARPEGE.optional_at_analysis, ("gust",))
        self.assertEqual(analysis_optional_ids(ARPEGE, published_bundle_ids(ARPEGE)), ("prate", "gust"))
        self.assertEqual(ARPEGE.statistical_processes, (("prate", 0), ("gust", 2)))

    def test_the_axis_is_hourly_to_48_then_three_hourly_to_102(self) -> None:
        self.assertEqual(ARPEGE.steps, ((48, 1), (102, 3)))
        self.assertEqual(ARPEGE.cycle_hours, 6)
        hours = ARPEGE.forecast_hours(102)
        self.assertEqual(len(hours), 67)
        self.assertEqual(hours[:49], list(range(49)))
        self.assertEqual(hours[49:], list(range(51, 103, 3)))

    def test_the_grid_is_gfs_s(self) -> None:
        self.assertEqual(ARPEGE.production_grid, GFS.production_grid)
        self.assertEqual(ARPEGE.tile, GFS.tile)
        self.assertFalse(ARPEGE.video)
        self.assertEqual(video_variable_ids(ARPEGE), frozenset())
        prose = prose_document()["sources"]["arpege"]
        self.assertEqual(prose["license"], "etalab-2.0")
        self.assertIn("Météo-France", prose["description"])


class FetchTests(unittest.TestCase):
    def test_every_step_lies_in_one_package(self) -> None:
        for hour in ARPEGE.forecast_hours(102):
            first, last = arpege_package(hour)
            self.assertIn((first, last), ARPEGE_PACKAGES)
            self.assertTrue(first <= hour <= last)
        self.assertEqual({arpege_package(hour) for hour in ARPEGE.forecast_hours(102)}, set(ARPEGE_PACKAGES))
        with self.assertRaises(DownloadError):
            arpege_package(103)

    def test_the_package_urls(self) -> None:
        self.assertEqual(
            arpege_package_url(RUN, (49, 72)),
            "https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt/2026-10-25T12:00:00Z/arpege/025/SP1/"
            "arpege__025__SP1__049H072H__2026-10-25T12:00:00Z.grib2",
        )
        self.assertEqual(model_object_url(RUN, 51, "arpege"), arpege_package_url(RUN, (49, 72)))
        self.assertEqual(
            arpege_package_url(RUN, ARPEGE_PACKAGES[0], ARPEGE_STATIC_PACKAGE),
            "https://meteofrance-pnt.s3.rbx.io.cloud.ovh.net/pnt/2026-10-25T12:00:00Z/arpege/025/SP2/"
            "arpege__025__SP2__000H024H__2026-10-25T12:00:00Z.grib2",
        )

    def test_a_run_is_complete_with_every_package_its_axis_reaches(self) -> None:
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return True

        # The four SP1 packages and the SP2 package the terrain is read from.
        self.assertTrue(_run_is_complete(RUN, 102, "arpege", exists))
        self.assertEqual(len(probed), 5)
        probed.clear()
        self.assertTrue(_run_is_complete(RUN, 24, "arpege", exists))
        self.assertEqual(len(probed), 2)
        self.assertFalse(_run_is_complete(RUN, 102, "arpege", lambda url: "073H102H" not in url))
        self.assertFalse(_run_is_complete(RUN, 102, "arpege", lambda url: "/SP2/" not in url))

    def test_latest_walks_back_to_a_complete_cycle(self) -> None:
        now = datetime(2026, 10, 25, 10, 30, tzinfo=UTC)
        run = resolve_run("latest", hours=102, now=now, exists=lambda url: "T00:00:00Z" in url, model="arpege")
        self.assertEqual(run.id, "2026102500")

    def test_a_frame_is_cut_out_of_a_package_by_identity_and_valid_time(self) -> None:
        # Two frames joined into one file stand in for a package: the cut
        # must take each record of the step asked for, and only it.
        scratch = Path(tempfile.mkdtemp(prefix="xue-arpege-package-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        package = scratch / "package.grib2"
        package.write_bytes(b"".join(path.read_bytes() for path in FIXTURE_FRAMES))
        step = FIXTURE_FRAMES[1].read_bytes()
        spans = dict(zip((message.band for message in grib2.index_messages(FIXTURE_FRAMES[1])), grib2.message_spans(FIXTURE_FRAMES[1])))
        frames = grib2.inspect_grib_fast(FIXTURE_FRAMES[1], ARPEGE.input_variable_ids)
        records = fetch._arpege_records(RUN, 1, ARPEGE, ("gust", "tmp2m"), package)
        for variable_id, record in zip(("gust", "tmp2m"), records):
            offset, length = spans[frames[variable_id].band]
            self.assertEqual(record, step[offset : offset + length], variable_id)
        (analysis,) = fetch._arpege_records(RUN, 0, ARPEGE, ("tmp2m",), package)
        first_offset, first_length = grib2.message_spans(FIXTURE_FRAMES[0])[0]
        self.assertEqual(analysis, FIXTURE_FRAMES[0].read_bytes()[first_offset : first_offset + first_length])
        with self.assertRaises(DownloadError):
            fetch._arpege_records(RUN, 0, ARPEGE, ("gust",), package)
        doubled = scratch / "doubled.grib2"
        doubled.write_bytes(step + step)
        with self.assertRaises(DownloadError):
            fetch._arpege_records(RUN, 1, ARPEGE, ("tmp2m",), doubled)

    @unittest.skipUnless(shutil.which("grib_set"), "eccodes CLI")
    def test_the_analysis_frame_takes_a_zero_total(self) -> None:
        scratch = Path(tempfile.mkdtemp(prefix="xue-arpege-analysis-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        package = scratch / fetch.ARPEGE_PACKAGE_DIRNAME / fetch.arpege_package_name(RUN, (0, 24))
        package.parent.mkdir()
        package.write_bytes(b"".join(path.read_bytes() for path in FIXTURE_FRAMES))
        frame = scratch / "frame.grib2"
        # The terrain is read off another package (the test below).
        wanted = tuple(variable_id for variable_id in ARPEGE.input_variable_ids if variable_id != "orog")
        frame.write_bytes(fetch._download_arpege_payload(RUN, 0, ARPEGE, scratch, wanted))
        messages = grib2.index_messages(frame)
        frames = grib2.inspect_grib_fast(frame, wanted, optional_ids=("gust",))
        self.assertEqual(sorted(frames), sorted(("tmp2m", "tp", "ugrd10m", "vgrd10m", "prmsl", "tcdc")))
        self.assertEqual([frames[variable_id].band for variable_id in ("tmp2m", "tp", "ugrd10m", "vgrd10m", "prmsl", "tcdc")], list(range(1, 7)))
        tp = messages[frames["tp"].band - 1]
        self.assertEqual((tp.parameter_number, tp.statistical_process), (52, 1))
        self.assertEqual(tp.valid_time, RUN.time, "restamped to the analysis")

    def test_the_terrain_is_read_by_range_out_of_the_sp2_package(self) -> None:
        # The analysis and the step joined, the terrain last, stand in for
        # the SP2 object: the walk reads one head per message up to the
        # terrain's, then that record whole.
        analysis = FIXTURE_FRAMES[0].read_bytes()
        offset, length = grib2.message_spans(FIXTURE_FRAMES[0])[-1]
        terrain = analysis[offset : offset + length]
        body = FIXTURE_FRAMES[1].read_bytes() + analysis
        url = arpege_package_url(RUN, ARPEGE_PACKAGES[0], ARPEGE_STATIC_PACKAGE)
        asked: list[ByteRange] = []

        def fetch_range(requested: str, byte_range: ByteRange) -> bytes:
            self.assertEqual(requested, url)
            asked.append(byte_range)
            return body[byte_range.start : byte_range.end + 1]

        with (
            mock.patch.object(fetch, "remote_length", lambda requested: len(body)),
            mock.patch.object(fetch, "fetch_range", fetch_range),
        ):
            self.assertEqual(fetch._arpege_static_record(RUN, "orog"), terrain)
            # A head per message before the terrain's, its own, and the record.
            self.assertEqual(len(asked), len(grib2.message_spans(FIXTURE_FRAMES[1])) + len(grib2.message_spans(FIXTURE_FRAMES[0])) + 1)
            self.assertEqual((asked[-1].start, asked[-1].length), (len(body) - length, length))
            body = FIXTURE_FRAMES[1].read_bytes()
            with self.assertRaises(DownloadError):
                fetch._arpege_static_record(RUN, "orog")

    def test_message_spans_tile_the_file(self) -> None:
        spans = grib2.message_spans(FIXTURE_FRAMES[1])
        self.assertEqual(len(spans), len(grib2.index_messages(FIXTURE_FRAMES[1])))
        self.assertEqual(spans[0][0], 0)
        for (offset, length), (following, _) in zip(spans, spans[1:]):
            self.assertEqual(offset + length, following)
        self.assertEqual(sum(length for _, length in spans), FIXTURE_FRAMES[1].stat().st_size)


class MatcherTests(unittest.TestCase):
    def test_the_header_index_finds_every_input_in_source_order(self) -> None:
        for path, expected in zip(FIXTURE_FRAMES, (7, 7)):
            frames = grib2.inspect_grib_fast(path, ARPEGE.input_variable_ids, optional_ids=("gust", "orog"))
            self.assertEqual(len(frames), expected, path.name)
            present = [variable_id for variable_id in ARPEGE.input_variable_ids if variable_id in frames]
            self.assertEqual([frames[variable_id].band for variable_id in present], list(range(1, expected + 1)))

    def test_the_analysis_total_is_an_empty_accumulation(self) -> None:
        by_band = {message.band: message for message in grib2.index_messages(FIXTURE_FRAMES[0])}
        frames = grib2.inspect_grib_fast(FIXTURE_FRAMES[0], ARPEGE.input_variable_ids, optional_ids=("gust",))
        tp = by_band[frames["tp"].band]
        self.assertEqual((tp.parameter_number, tp.statistical_process), (52, 1))
        self.assertEqual(tp.valid_time, tp.reference_time)

    def test_the_terrain_is_the_geometric_height_in_metres(self) -> None:
        by_band = {message.band: message for message in grib2.index_messages(FIXTURE_FRAMES[0])}
        terrain = grib2.inspect_grib_fast(FIXTURE_FRAMES[0], ("orog",))["orog"]
        message = by_band[terrain.band]
        self.assertEqual((message.parameter_category, message.parameter_number, message.level_type), (3, 6, 1))
        self.assertEqual(terrain.unit, "m")

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        assert_gdalinfo_agrees(self, ((path, ARPEGE.input_variable_ids, ("gust", "orog")) for path in FIXTURE_FRAMES))


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.output = REFERENCE.run_directory
        _, cls.report = REFERENCE.get()

    def test_every_published_bundle_is_written(self) -> None:
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["model"], "ARPEGE")
        self.assertEqual(manifest["product"], "arpege")
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], list(published_bundle_ids(ARPEGE)))
        self.assertEqual(self.report["videos"], [])

    def test_the_crop_grid(self) -> None:
        grid = read_bundle(self.output / "tmp2m.xue").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (80, 80))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.25, -0.25))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (2.0, 50.0))

    def test_the_first_hour_s_rate_is_its_total(self) -> None:
        bundle = read_bundle(self.output / "prate.xue")
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 1, "frameCount": 1, "frameStep": 1})
        rates = quantize.PROFILES["quality"]["prate"].decode(bundle.decode_plane(1, 1))
        # The window's wettest cell has 8.3 mm in the first hour: differenced
        # against the zero analysis total, not zeroed for want of one.
        self.assertGreater(float(rates.max()), 7.0)
        self.assertLess(float(rates.max()), 10.0)

    def test_the_gust_starts_at_the_first_step(self) -> None:
        metadata = read_bundle(self.output / "gust.xue").metadata
        self.assertEqual(metadata["time"]["firstFrameOffset"], 1)
        self.assertEqual(metadata["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 2)

    def test_the_terrain_is_one_frame_in_metres(self) -> None:
        bundle = read_bundle(self.output / "orog.xue")
        self.assertEqual(bundle.metadata["time"]["frameCount"], 1)
        heights = quantize.PROFILES["quality"]["orog"].decode(bundle.decode_plane(1, 0))
        # The window holds the Mediterranean and the Alps: sea level within
        # a codebook step, and the model's Alps well above 2 km.
        self.assertLess(float(heights.min()), 40.0)
        self.assertGreater(float(heights.max()), 2000.0)
        self.assertLess(float(heights.max()), 4810.0)


class NativeParityTests(ParityCase, unittest.TestCase):
    """The same two frames through both encoders, byte for byte."""

    reference = REFERENCE
    expect = ("arpege.2026102512/prate.xue", "arpege.2026102512/wind10m.xue")


if __name__ == "__main__":
    unittest.main()

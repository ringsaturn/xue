"""The ICON-EU source: DWD's European nest of ICON at 0.0625°, a regular
latitude/longitude grid from 23.5°W to 62.5°E and 29.5°N to 70.5°N, so
nothing is resampled. DWD's open data server writes one bzip2-compressed,
CCSDS-packed GRIB2 field per parameter and step with no index; the fetch
names each by the registry's ``dwd_param``, decompresses, joins and
repacks one frame per step.

Nothing is new to the encoders: every record is matched under an
existing identity or alternate — the run-total ``TOT_PREC`` is AIFS's
WMO 0/1/52 accumulation in kg/m², ``VMAX_10M`` ECMWF's interval-maximum
gust on the 10 m surface, ``CLCT`` AIFS's 0/6/1 in percent on the ground
surface — and the 0.0625° step is a power of two the regional snap
passes through unchanged.

``tests/fixtures/iconeu.2026102512.f000.crop.grib2`` and ``f001`` are an
80 x 80 cell window over the western Alps and the Ligurian Sea (6.5–11.4°E,
43.1–48.0°N) of the analysis and the first step of the 2026-10-25 12Z
cycle, every record the source fetches in source order: six at the
analysis (no gust there) and seven at the step.
"""

from __future__ import annotations

import bz2
import json
import unittest
from datetime import UTC, datetime
from unittest import mock

from tests._support import FIXTURES, ParityCase, ReferenceBuild, assert_gdalinfo_agrees, requires_gdalinfo
from xuebuild import fetch, grib2, quantize
from xuebuild.binconvert import analysis_optional_ids, published_bundle_ids, video_variable_ids
from xuebuild.binformat import read_bundle
from xuebuild.errors import DownloadError
from xuebuild.fetch import _run_is_complete, icon_eu_object_url, model_object_url, resolve_run
from xuebuild.model import GfsRun
from xuebuild.sources import source_spec
from xuebuild.stac import prose_document
from xuebuild.variables import variable_spec

FIXTURE_FRAMES = [FIXTURES / "iconeu.2026102512.f000.crop.grib2", FIXTURES / "iconeu.2026102512.f001.crop.grib2"]
ICONEU = source_spec("iconeu")
RUN = GfsRun(datetime(2026, 10, 25, 12, tzinfo=UTC))

#: The reference build every class here reads (tests/_support.py).
REFERENCE = ReferenceBuild("iconeu", "2026102512", FIXTURE_FRAMES)


def tearDownModule() -> None:
    REFERENCE.cleanup()


class SourceRegistryTests(unittest.TestCase):
    def test_the_basic_set(self) -> None:
        self.assertEqual(published_bundle_ids(ICONEU), ("tmp2m", "prate", "prmsl", "gust", "tcdc", "wind10m"))
        self.assertEqual(ICONEU.input_variable_ids, ("tmp2m", "tp", "ugrd10m", "vgrd10m", "prmsl", "gust", "tcdc"))
        self.assertTrue(ICONEU.accumulated_precipitation)
        self.assertEqual(ICONEU.optional_at_analysis, ("gust",))
        self.assertEqual(analysis_optional_ids(ICONEU, published_bundle_ids(ICONEU)), ("prate", "gust"))
        self.assertEqual(ICONEU.statistical_processes, (("prate", 0), ("gust", 2)))

    def test_the_axis_is_hourly_to_78_then_three_hourly_to_120(self) -> None:
        self.assertEqual(ICONEU.steps, ((78, 1), (120, 3)))
        self.assertEqual(ICONEU.cycle_hours, 6)
        hours = ICONEU.forecast_hours(120)
        self.assertEqual(len(hours), 93)
        self.assertEqual(hours[:79], list(range(79)))
        self.assertEqual(hours[79:], list(range(81, 121, 3)))
        with self.assertRaises(DownloadError):
            ICONEU.forecast_hours(80)

    def test_the_grid_tile_video_and_catalog_prose(self) -> None:
        self.assertEqual(ICONEU.production_grid, (1377, 657))
        # The tile divides the grid exactly.
        self.assertEqual((1377 % ICONEU.tile[0], 657 % ICONEU.tile[1]), (0, 0))
        self.assertIsNone(ICONEU.regrid)
        self.assertEqual(ICONEU.variant_factors, (2,))
        self.assertFalse(ICONEU.video)
        self.assertEqual(video_variable_ids(ICONEU), frozenset())
        prose = prose_document()["sources"]["iconeu"]
        self.assertEqual(prose["license"], "CC-BY-4.0")
        self.assertIn("Deutscher Wetterdienst", prose["description"])

    def test_every_input_names_its_dwd_parameter(self) -> None:
        self.assertEqual(
            {variable_id: variable_spec(variable_id).dwd_param for variable_id in ICONEU.input_variable_ids},
            {
                "tmp2m": "T_2M",
                "tp": "TOT_PREC",
                "ugrd10m": "U_10M",
                "vgrd10m": "V_10M",
                "prmsl": "PMSL",
                "gust": "VMAX_10M",
                "tcdc": "CLCT",
            },
        )


class FetchTests(unittest.TestCase):
    def test_one_object_per_parameter_and_step(self) -> None:
        self.assertEqual(
            icon_eu_object_url(RUN, 81, "tp"),
            "https://opendata.dwd.de/weather/nwp/icon-eu/grib/12/tot_prec/"
            "icon-eu_europe_regular-lat-lon_single-level_2026102512_081_TOT_PREC.grib2.bz2",
        )
        self.assertEqual(model_object_url(RUN, 3, "iconeu"), icon_eu_object_url(RUN, 3, "tmp2m"))
        with self.assertRaises(DownloadError):
            icon_eu_object_url(RUN, 3, "cape")

    def test_a_run_is_complete_with_both_ends_of_every_input(self) -> None:
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return True

        self.assertTrue(_run_is_complete(RUN, 120, "iconeu", exists))
        # Six inputs at the analysis (no gust record there), seven at the end.
        self.assertEqual(len(probed), 13)
        self.assertFalse(any("_000_VMAX_10M" in url for url in probed))
        self.assertTrue(any("_120_VMAX_10M" in url for url in probed))
        self.assertFalse(_run_is_complete(RUN, 120, "iconeu", lambda url: "CLCT" not in url))

    def test_latest_walks_back_to_a_complete_cycle(self) -> None:
        now = datetime(2026, 10, 25, 14, 0, tzinfo=UTC)
        run = resolve_run("latest", hours=120, now=now, exists=lambda url: "/grib/06/" in url, model="iconeu")
        self.assertEqual(run.id, "2026102506")

    def test_a_frame_is_the_decompressed_fields_in_source_order(self) -> None:
        requested: list[str] = []

        def get_ok(url: str, **_: object) -> bytes:
            requested.append(url)
            return bz2.compress(f"<{url}>".encode())

        def fields(hour: int, *variable_ids: str) -> bytes:
            return b"".join(f"<{icon_eu_object_url(RUN, hour, variable_id)}>".encode() for variable_id in variable_ids)

        with mock.patch.object(fetch, "get_ok", get_ok):
            analysis = fetch._download_icon_eu_payload(RUN, 0, ICONEU)
            step = fetch._download_icon_eu_payload(RUN, 1, ICONEU, ("tmp2m", "gust"))
        # No gust at the analysis: its interval is empty there.
        self.assertEqual(analysis, fields(0, "tmp2m", "tp", "ugrd10m", "vgrd10m", "prmsl", "tcdc"))
        self.assertEqual(step, fields(1, "tmp2m", "gust"))
        self.assertEqual(len(requested), 8)


class MatcherTests(unittest.TestCase):
    def test_the_header_index_finds_every_input_in_source_order(self) -> None:
        for path, expected in zip(FIXTURE_FRAMES, (6, 7)):
            frames = grib2.inspect_grib_fast(path, ICONEU.input_variable_ids, optional_ids=("gust",))
            self.assertEqual(len(frames), expected, path.name)
            present = [variable_id for variable_id in ICONEU.input_variable_ids if variable_id in frames]
            self.assertEqual([frames[variable_id].band for variable_id in present], list(range(1, expected + 1)))
        step = grib2.inspect_grib_fast(FIXTURE_FRAMES[1], ICONEU.input_variable_ids)
        self.assertEqual(step["tp"].unit, "kg/(m^2*s)", "AIFS's millimetre accumulation")
        self.assertEqual(step["tcdc"].unit, "%")
        self.assertEqual(step["gust"].lead_seconds, 3600)

    def test_the_records_are_what_the_alternates_say(self) -> None:
        by_band = {message.band: message for message in grib2.index_messages(FIXTURE_FRAMES[1])}
        frames = grib2.inspect_grib_fast(FIXTURE_FRAMES[1], ICONEU.input_variable_ids)
        tp = by_band[frames["tp"].band]
        self.assertEqual((tp.parameter_category, tp.parameter_number, tp.level_type, tp.statistical_process), (1, 52, 1, 1))
        gust = by_band[frames["gust"].band]
        self.assertEqual((gust.parameter_number, gust.level_type, gust.level_value, gust.statistical_process), (22, 103, 10.0, 2))
        clct = by_band[frames["tcdc"].band]
        self.assertEqual((clct.parameter_category, clct.parameter_number, clct.level_type), (6, 1, 1))

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        assert_gdalinfo_agrees(self, ((path, ICONEU.input_variable_ids, ("gust",)) for path in FIXTURE_FRAMES))


class ConversionTests(unittest.TestCase):
    """The two-frame fixture through the reference pipeline: the module's
    reference build, which the parity case compares the wheel's against."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.output = REFERENCE.run_directory
        _, cls.report = REFERENCE.get()

    def test_every_published_bundle_is_written(self) -> None:
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["model"], "ICON-EU")
        self.assertEqual(manifest["product"], "iconeu")
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], list(published_bundle_ids(ICONEU)))
        self.assertEqual(self.report["videos"], [])

    def test_the_regional_grid_keeps_its_power_of_two_step(self) -> None:
        grid = read_bundle(self.output / "tmp2m.xue").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (80, 80))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.0625, -0.0625))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (6.5, 48.0))

    def test_the_rate_is_the_first_hour_s_total(self) -> None:
        bundle = read_bundle(self.output / "prate.xue")
        metadata = bundle.metadata
        self.assertEqual(metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 1, "frameCount": 1, "frameStep": 1})
        self.assertEqual(metadata["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 0)
        codes = bundle.decode_plane(1, 1)
        rates = quantize.PROFILES["quality"]["prate"].decode(codes)
        # The window's wettest cell has 9.7 mm in the first hour.
        self.assertGreater(float(rates.max()), 8.0)
        self.assertLess(float(rates.max()), 11.0)

    def test_the_gust_starts_at_the_first_step(self) -> None:
        metadata = read_bundle(self.output / "gust.xue").metadata
        self.assertEqual(metadata["time"]["firstFrameOffset"], 1)
        self.assertEqual(metadata["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 2)
        tmp2m = read_bundle(self.output / "tmp2m.xue").metadata
        self.assertEqual(tmp2m["time"], {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 1})


class NativeParityTests(ParityCase, unittest.TestCase):
    """The same two frames through both encoders, byte for byte."""

    reference = REFERENCE
    expect = ("iconeu.2026102512/prate.xue", "iconeu.2026102512/wind10m.xue")


if __name__ == "__main__":
    unittest.main()

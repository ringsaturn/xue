"""The ECMWF source aligned with GFS: the surface diagnostics, the vertical
velocity, the equivalent potential temperature and the ocean fields the open
data carries, each published under its GFS identity.

What is new with it. A record can arrive under a *whole* other identity at
another centre, not just another parameter number on the same surface
(``VariableSpec.grib2_alternates``): ECMWF's gust is an interval maximum on
the 10 m surface, its total cloud cover a local parameter at the ground in a
0–1 fraction, its CAPE the most-unstable parcel's, its skin temperature a
parameter of its own with no surface value. An ECMWF ``.index`` spells one
field differently along the axis (``10fg`` / ``10fg3``,
``VariableSpec.ecmwf_alternate_params``). A second file family of the ECMWF
cycle, the ``wave`` stream, read through the same ``CompanionFile``
mechanism as GFS-Wave. A scalar whose record is empty at the analysis
(``optional_at_analysis`` on a source that is not sflux) and whose series
therefore starts at the first step, the way a de-accumulated rate does.
The cycle's ensemble products (``ep``), one object for every step, read
through the same mechanism: three spreads on the deterministic run's axis
and six 24-hour precipitation probabilities, one window ending every twelve
hours from the 24th (``variable_step_hours``), each matched by one more rule
on the id CMA GRAPES GEPS's record already reads into.

``tests/fixtures/ecmwf.2026112612.f000.crop.grib2`` and ``f003`` are an 80 x
80 cell window of the 2026-11-26 12Z cycle over the Kara Sea (60–80E,
65–85N: sea ice, open water and land, so every bitmap and fill rule meets
real masked points), every record the source fetches in source order —
ninety at each frame, the analysis carrying the orography and the first
step the gust, the eight soil layers among them, the three spreads last —
from the ``oper``, ``wave`` and ``enfo`` ``ep`` objects as the fetcher
assembles them. The probabilities start at F024, past the fixture.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

import numpy as np

from tests._support import FIXTURES, ParityCase, ReferenceBuild, assert_gdalinfo_agrees, requires_gdalinfo, store_path, stored_bundle
from xuebuild import grib2
from xuebuild.binconvert import _variable_offsets
from xuebuild.discover import analysis_optional_ids, published_bundle_ids, store_dimensions
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    ECMWF_BASE_URLS,
    _frame_variable_ids,
    _run_is_complete,
    ecmwf_companion_object_url,
    ecmwf_object_url,
)
from xuebuild.idx import ecmwf_ensemble_selector, ecmwf_field_byte_range
from xuebuild.model import GfsRun
from xuebuild.quantize import PROFILES
from xuebuild.registry import document
from xuebuild.gdal import raster_expression
from xuebuild.sources import ECMWF_ENSEMBLE_PRODUCT_IDS, source_spec
from xuebuild.variables import variable_spec

FIXTURE_FRAMES = [FIXTURES / "ecmwf.2026112612.f000.crop.grib2", FIXTURES / "ecmwf.2026112612.f003.crop.grib2"]
ECMWF = source_spec("ecmwf")
GFS = source_spec("gfs")
SPREADS = ("prmslsd", "hgt500sd", "tmp850sd")
PROBABILITIES = ("apcp24gt1", "apcp24gt5", "apcp24gt10", "apcp24gt25", "apcp24gt50", "apcp24gt100")
ENSEMBLE = frozenset(ECMWF_ENSEMBLE_PRODUCT_IDS)
# The bundles this widening added to the ECMWF set, in publication order.
ADDED = ("gust", "tcdc", "cape", "dpt2m", "vvel850", "vvel700", "vvel500", "thetae850", "tmpsfc", "icetk", "htsgw", "perpw", "wave", "orog")

#: The reference build every class here reads (tests/_support.py).
REFERENCE = ReferenceBuild("ecmwf", "2026112612", FIXTURE_FRAMES)


def tearDownModule() -> None:
    REFERENCE.cleanup()


class SourceRegistryTests(unittest.TestCase):
    def test_ecmwf_publishes_what_the_open_data_carries_in_gfs_order(self) -> None:
        gfs = published_bundle_ids(GFS)
        # The ensemble's statistics sit beside their centres; GFS has none.
        ecmwf = tuple(bundle_id for bundle_id in published_bundle_ids(ECMWF) if bundle_id not in ENSEMBLE)
        # ECMWF's own type is the one field GFS has no record of; it sits where
        # GFS's derived one does.
        self.assertEqual(
            [bundle_id for bundle_id in gfs if bundle_id in ecmwf],
            [bundle_id for bundle_id in ecmwf if bundle_id != "ptypeec"],
        )
        self.assertEqual(ecmwf.index("ptypeec"), gfs.index("ptype"))
        for bundle_id in ADDED:
            self.assertIn(bundle_id, ecmwf)
        # Not in the open data: the layer cloud covers, the visibility, the
        # sea ice cover; and no apparent temperature record.
        for bundle_id in ("lcdc", "mcdc", "hcdc", "vis", "icec", "aptmp2m"):
            self.assertIn(bundle_id, gfs)
            self.assertNotIn(bundle_id, ecmwf)
        self.assertEqual(len(ecmwf), 79)
        published = published_bundle_ids(ECMWF)
        self.assertEqual(len(published), 88)
        self.assertEqual(published[published.index("prate") + 1 : published.index("prate") + 7], PROBABILITIES)
        for spread in SPREADS:
            self.assertEqual(published[published.index(spread) - 1], spread.removesuffix("sd"))

    def test_every_added_input_names_its_ecmwf_record(self) -> None:
        for variable_id in ECMWF.input_variable_ids:
            self.assertNotEqual(variable_spec(variable_id).ecmwf_param, "", variable_id)
        self.assertEqual(
            {variable_id: variable_spec(variable_id).ecmwf_param for variable_id in ("gust", "tcdc", "cape", "dpt2m", "tmpsfc", "icetk", "htsgw", "perpw", "dirpw")},
            {"gust": "10fg", "tcdc": "tcc", "cape": "mucape", "dpt2m": "2d", "tmpsfc": "skt", "icetk": "sithick", "htsgw": "swh", "perpw": "pp1d", "dirpw": "mwd"},
        )
        for level in (850, 700, 500):
            self.assertEqual(variable_spec(f"vvel{level}").ecmwf_param, "w")

    def test_the_gust_is_optional_at_the_analysis_and_starts_at_the_first_step(self) -> None:
        self.assertEqual(ECMWF.optional_at_analysis, ("gust",))
        scalars = published_bundle_ids(ECMWF)
        self.assertEqual(analysis_optional_ids(ECMWF, scalars), ("prate", *PROBABILITIES, "gust"))
        self.assertEqual(analysis_optional_ids(GFS, published_bundle_ids(GFS)), ())
        sflux = source_spec("sflux")
        self.assertEqual(analysis_optional_ids(sflux, published_bundle_ids(sflux)), ("prate",))

    def test_no_source_ships_video(self) -> None:
        for model in ("ecmwf", "sflux", "gfs", "hrrr"):
            self.assertFalse(source_spec(model).video, model)


class FetchTests(unittest.TestCase):
    def test_the_wave_stream_sits_beside_oper(self) -> None:
        run = GfsRun(datetime(2026, 9, 12, 12, tzinfo=UTC))
        self.assertEqual(
            ecmwf_object_url(run, 3),
            "https://storage.googleapis.com/ecmwf-open-data/20260912/12z/ifs/0p25/oper/20260912120000-3h-oper-fc.grib2",
        )
        self.assertEqual(
            ecmwf_companion_object_url(run, 3, "wave"),
            "https://storage.googleapis.com/ecmwf-open-data/20260912/12z/ifs/0p25/wave/20260912120000-3h-wave-fc.grib2",
        )
        self.assertTrue(
            ecmwf_companion_object_url(run, 240, "wave", base_url=ECMWF_BASE_URLS[1]).startswith(ECMWF_BASE_URLS[1])
        )
        with self.assertRaises(DownloadError):
            ecmwf_companion_object_url(run, 3, "ice")

    def test_a_run_is_complete_only_with_its_wave_frames(self) -> None:
        run = GfsRun(datetime(2026, 9, 12, 12, tzinfo=UTC))
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return "/wave/" not in url or "-0h-" in url

        self.assertFalse(_run_is_complete(run, 240, "ecmwf", exists))
        self.assertTrue(any("/wave/20260912120000-240h-wave-fc.grib2" in url for url in probed))
        self.assertTrue(_run_is_complete(run, 240, "ecmwf", lambda url: True))

    def test_the_gust_is_found_under_either_spelling(self) -> None:
        lines = [
            {"param": "2t", "levtype": "sfc", "_offset": 0, "_length": 10},
            {"param": "10fg3", "levtype": "sfc", "_offset": 10, "_length": 20},
            {"param": "t", "levtype": "pl", "levelist": "850", "_offset": 30, "_length": 5},
        ]
        text = "\n".join(json.dumps(line) for line in lines)
        gust = variable_spec("gust")
        found = ecmwf_field_byte_range(text, gust.ecmwf_param, alternate_params=gust.ecmwf_alternate_params)
        self.assertEqual((found.start, found.end), (10, 29))
        with self.assertRaisesRegex(DownloadError, "10fg"):
            ecmwf_field_byte_range(text, gust.ecmwf_param)
        # The primary spelling wins when present, and an ambiguous alternate
        # is still an error.
        both = text + "\n" + json.dumps({"param": "10fg", "levtype": "sfc", "_offset": 40, "_length": 1})
        self.assertEqual(ecmwf_field_byte_range(both, "10fg", alternate_params=("10fg3",)).start, 40)
        twice = text + "\n" + json.dumps({"param": "10fg3", "levtype": "sfc", "_offset": 50, "_length": 1})
        with self.assertRaises(DownloadError):
            ecmwf_field_byte_range(twice, "10fg", alternate_params=("10fg3",))


class MatcherTests(unittest.TestCase):
    def test_the_header_index_finds_every_input_in_source_order(self) -> None:
        optional = ECMWF.optional_at_analysis + ("orog",) + PROBABILITIES
        for path, expected in zip(FIXTURE_FRAMES, (90, 90)):
            frames = grib2.inspect_grib_fast(path, ECMWF.input_variable_ids, optional_ids=optional)
            self.assertEqual(len(frames), expected, path.name)
            present = [variable_id for variable_id in ECMWF.input_variable_ids if variable_id in frames]
            self.assertEqual([frames[variable_id].band for variable_id in present], list(range(1, expected + 1)))
        analysis, step = (grib2.inspect_grib_fast(path, ECMWF.input_variable_ids, optional_ids=optional) for path in FIXTURE_FRAMES)
        self.assertNotIn("gust", analysis)
        self.assertEqual(step["gust"].lead_seconds, 3 * 3600)
        # The orography is the analysis's alone on this source; ECMWF writes
        # no surface geopotential at a lead time.
        self.assertIn("orog", analysis)
        self.assertNotIn("orog", step)
        # The alternates' own units, the aliases' the registry's.
        self.assertEqual(step["tcdc"].unit, "-")
        self.assertEqual(step["gust"].unit, "m/s")
        self.assertEqual(step["cape"].unit, "J/kg")
        self.assertEqual(step["tmpsfc"].unit, "C")
        self.assertEqual(step["perpw"].unit, "s")
        self.assertEqual(step["dirpw"].unit, "Degree true")
        with self.assertRaisesRegex(ConversionError, "gust"):
            grib2.inspect_grib_fast(
                FIXTURE_FRAMES[0], tuple(i for i in ECMWF.input_variable_ids if i not in PROBABILITIES)
            )

    def test_the_records_are_what_the_alternates_say(self) -> None:
        by_band = {message.band: message for message in grib2.index_messages(FIXTURE_FRAMES[1])}
        frames = grib2.inspect_grib_fast(FIXTURE_FRAMES[1], ECMWF.input_variable_ids, optional_ids=("orog", *PROBABILITIES))
        gust = by_band[frames["gust"].band]
        self.assertEqual((gust.discipline, gust.parameter_category, gust.parameter_number), (0, 2, 22))
        self.assertEqual((gust.level_type, gust.level_value, gust.statistical_process), (103, 10.0, 2))
        tcc = by_band[frames["tcdc"].band]
        self.assertEqual((tcc.parameter_category, tcc.parameter_number, tcc.level_type, tcc.level_value), (6, 192, 1, None))
        cape = by_band[frames["cape"].band]
        self.assertEqual((cape.parameter_category, cape.parameter_number, cape.level_type), (7, 6, 17))
        skt = by_band[frames["tmpsfc"].band]
        self.assertEqual((skt.parameter_category, skt.parameter_number, skt.level_type, skt.level_value), (0, 17, 1, None))
        self.assertEqual(by_band[frames["perpw"].band].parameter_number, 34)
        self.assertEqual(by_band[frames["dirpw"].band].parameter_number, 14)
        self.assertEqual(by_band[frames["htsgw"].band].parameter_number, 3)
        # ECMWF's spreads: code 2, the sea-level pressure as pressure.
        for spread, triple in (("prmslsd", (0, 3, 0)), ("hgt500sd", (0, 3, 5)), ("tmp850sd", (0, 0, 0))):
            message = by_band[frames[spread].band]
            self.assertEqual((message.discipline, message.parameter_category, message.parameter_number), triple)
            self.assertEqual(message.template, 2)
            self.assertEqual(message.ensemble.derived_forecast, 2)

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        optional = ECMWF.optional_at_analysis + ("orog",) + PROBABILITIES
        assert_gdalinfo_agrees(self, ((path, ECMWF.input_variable_ids, optional) for path in FIXTURE_FRAMES))


class ConversionTests(unittest.TestCase):
    """The two-frame fixture through the reference pipeline: the module's
    reference build, which the parity case compares the wheel's against."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.output = REFERENCE.run_directory
        _, cls.report = REFERENCE.get()

    def _plane(self, variable_id: str, offset: int, member: int = 1) -> tuple[np.ndarray, dict]:
        bundle = stored_bundle(self.output, variable_id)
        return bundle.decode_plane(member, offset), bundle.metadata

    def test_every_published_bundle_is_written_and_no_video(self) -> None:
        manifest = json.loads((self.output / "manifest.json").read_text())
        # The probabilities start at F024, past the fixture's two frames:
        # left out with a warning.
        self.assertEqual(
            [bundle["variable"] for bundle in manifest["bundles"]],
            [bundle_id for bundle_id in published_bundle_ids(ECMWF) if bundle_id not in PROBABILITIES],
        )
        self.assertFalse(any("video" in bundle for bundle in manifest["bundles"]))
        self.assertEqual(self.report["videos"], [])
        self.assertEqual(list(self.output.glob("*.h264*")), [])

    def test_the_gust_series_starts_at_the_first_step(self) -> None:
        codes, metadata = self._plane("gust", 3)
        self.assertEqual(metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 3, "frameCount": 1, "frameStep": 1})
        self.assertGreater(int(codes.max()), 0)
        _, prate = self._plane("prate", 3)
        self.assertEqual(prate["time"]["firstFrameOffset"], 3)
        _, tmp2m = self._plane("tmp2m", 0)
        self.assertEqual(tmp2m["time"], {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 3})
        # The identity written is GFS's — the ground surface, not the 10 m
        # one — marked as the maximum over the step it is, the way the
        # de-accumulated rate is marked as a mean; an instantaneous field
        # carries no such key.
        parameter = metadata["variables"][0]["parameter"]
        self.assertEqual((parameter["typeOfFirstFixedSurface"], parameter["scaledValueOfFirstFixedSurface"]), (1, 0))
        self.assertEqual(parameter["typeOfStatisticalProcessing"], 2)
        self.assertEqual(prate["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 0)
        self.assertNotIn("typeOfStatisticalProcessing", tmp2m["variables"][0]["parameter"])
        self.assertEqual(ECMWF.statistical_processes, (("prate", 0), ("gust", 2)))
        self.assertEqual(source_spec("sflux").statistical_processes, (("prate", 0),))
        self.assertEqual(GFS.statistical_processes, ())

    def test_the_cloud_fraction_reaches_the_codebook_in_percent(self) -> None:
        codes, metadata = self._plane("tcdc", 0)
        quantization = metadata["variables"][0]["quantization"]
        self.assertEqual(quantization["scale"], 0.5)
        self.assertEqual(int(codes.max()), 200, "an overcast cell is 100 %")
        self.assertEqual(int(codes.min()), 0)
        self.assertEqual(metadata["variables"][0]["parameter"]["parameterNumber"], 1, "0/6/1, never the local 192")

    def test_land_is_the_bottom_of_the_wave_and_ice_codebooks(self) -> None:
        fills = {}
        for variable_id in ("htsgw", "perpw", "icetk"):
            codes, metadata = self._plane(variable_id, 0)
            self.assertNotIn(255, codes, f"{variable_id} carries no nodata code")
            fills[variable_id] = float(np.mean(codes == 0))
        # A third of the window is land, and the ice edge masks more of the
        # wave fields; the skin temperature covers everything.
        self.assertGreater(fills["htsgw"], 0.25)
        self.assertGreater(fills["perpw"], 0.25)
        self.assertGreater(fills["icetk"], 0.25)
        skin, _ = self._plane("tmpsfc", 0)
        self.assertLess(float(np.mean(skin == 0)), 0.01)
        u, _ = self._plane("wave", 0, 1)
        v, _ = self._plane("wave", 0, 2)
        self.assertGreater(float(np.mean((u == 127) & (v == 127))), 0.25, "land is (0, 0) in the wave vector")
        self.assertGreater(int(u.max()), 127)


    def test_the_spreads_publish_the_registry_identity_on_the_run_axis(self) -> None:
        for spread, ceiling in (("prmslsd", 5.0), ("hgt500sd", 10.0), ("tmp850sd", 2.0)):
            codes, metadata = self._plane(spread, 3)
            self.assertEqual(metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 3})
            variable = metadata["variables"][0]
            self.assertEqual(variable["ensemble"], {"derivedForecast": 4}, spread)
            quantization = variable["quantization"]
            values = quantization["offset"] + codes.astype(float) * quantization["scale"]
            self.assertGreater(float(values.max()), 0.0, spread)
            self.assertLess(float(values.max()), ceiling, spread)
        parameter = self._plane("prmslsd", 0)[1]["variables"][0]["parameter"]
        self.assertEqual((parameter["parameterCategory"], parameter["parameterNumber"]), (3, 1), "0/3/1, never ECMWF's 0/3/0")
        tmp850sd = self._plane("tmp850sd", 0)[1]["variables"][0]
        self.assertEqual(tmp850sd["unit"], variable_spec("tmp850").output_unit)


class EnsembleProductsTests(unittest.TestCase):
    """The ``ep`` family: one object per cycle, records picked by statistic
    and step, the probabilities on a twelve-hourly axis of their own."""

    RUN = GfsRun(datetime(2026, 11, 26, 12, tzinfo=UTC))

    def test_one_object_serves_every_step(self) -> None:
        expected = "https://storage.googleapis.com/ecmwf-open-data/20261126/12z/ifs/0p25/enfo/20261126120000-240h-enfo-ep.grib2"
        for hour in (0, 3, 150, 240):
            self.assertEqual(ecmwf_companion_object_url(self.RUN, hour, "ep"), expected)
        self.assertEqual(ECMWF.companion_of("apcp24gt10").id, "ep")
        self.assertEqual(ECMWF.companion_of("tmp850sd").id, "ep")

    def test_a_run_waits_for_its_ensemble_products(self) -> None:
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return "/enfo/" not in url

        self.assertFalse(_run_is_complete(self.RUN, 240, "ecmwf", exists))
        self.assertEqual(sum("-enfo-ep.grib2" in url for url in probed), 1)

    def test_records_are_picked_by_statistic_and_step(self) -> None:
        self.assertEqual(ecmwf_ensemble_selector(variable_spec("prmslsd"), 48), ("es", "48"))
        self.assertEqual(ecmwf_ensemble_selector(variable_spec("apcp24gt10"), 48), ("ep", "24-48"))
        self.assertEqual(ecmwf_ensemble_selector(variable_spec("prmsl"), 48), (None, None))
        self.assertEqual(
            {variable_id: variable_spec(variable_id).ecmwf_param for variable_id in ECMWF_ENSEMBLE_PRODUCT_IDS},
            {
                "prmslsd": "msl",
                "hgt500sd": "gh",
                "tmp850sd": "t",
                **{f"apcp24gt{mm}": f"tpg{mm}" for mm in (1, 5, 10, 25, 50, 100)},
            },
        )
        lines = [
            {"param": "msl", "type": "em", "step": "48", "levtype": "sfc", "_offset": 0, "_length": 10},
            {"param": "msl", "type": "es", "step": "48", "levtype": "sfc", "_offset": 10, "_length": 10},
            {"param": "msl", "type": "es", "step": "51", "levtype": "sfc", "_offset": 20, "_length": 10},
            {"param": "tpg10", "type": "ep", "step": "12-36", "levtype": "sfc", "_offset": 30, "_length": 10},
            {"param": "tpg10", "type": "ep", "step": "24-48", "levtype": "sfc", "_offset": 40, "_length": 10},
        ]
        text = "\n".join(json.dumps(line) for line in lines)
        self.assertEqual(ecmwf_field_byte_range(text, "msl", record_type="es", step="48").start, 10)
        self.assertEqual(ecmwf_field_byte_range(text, "tpg10", record_type="ep", step="24-48").start, 40)
        with self.assertRaisesRegex(DownloadError, "msl"):
            ecmwf_field_byte_range(text, "msl")
        with self.assertRaisesRegex(DownloadError, "step 36"):
            ecmwf_field_byte_range(text, "tpg10", record_type="ep", step="36")

    def test_the_850_hpa_temperature_spread_is_registered_like_cma_s_spreads(self) -> None:
        spec = variable_spec("tmp850sd")
        self.assertEqual(spec.parameter_metadata(), variable_spec("tmp850").parameter_metadata())
        self.assertEqual(spec.grib2_ensemble.metadata(), {"derivedForecast": 4})
        self.assertEqual(spec.output_unit, variable_spec("tmp850").output_unit)
        for profile, codebooks in PROFILES.items():
            self.assertEqual(
                codebooks["tmp850sd"].metadata(),
                {"type": "linear", "offset": 0.0, "scale": 0.05, "minimumCode": 0, "maximumCode": 250, "nodataCode": 255},
                profile,
            )
        # A departure in either unit is the same number: no offset.
        self.assertEqual(raster_expression(spec, "C"), raster_expression(spec, "K"))
        source = document()["sources"]["ecmwf"]
        self.assertEqual(source["variableStepHours"], [[variable_id, 12] for variable_id in PROBABILITIES])
        self.assertEqual(source["variableFirstHours"], [[variable_id, 24] for variable_id in PROBABILITIES])
        self.assertEqual(document()["sources"]["gfs"]["variableStepHours"], [])

    def test_a_frame_asks_for_the_probabilities_on_their_cadence_alone(self) -> None:
        hours = [hour for hour in (0, 3, 21, 24, 27, 36, 48, 144, 150, 156, 162, 234, 240) if "apcp24gt10" in _frame_variable_ids(ECMWF, hour)]
        self.assertEqual(hours, [24, 36, 48, 144, 156, 240])
        for hour in (0, 3, 150, 240):
            self.assertIn("tmp850sd", _frame_variable_ids(ECMWF, hour))

    def test_the_probabilities_take_an_axis_of_their_own(self) -> None:
        self.assertEqual(store_dimensions(ECMWF, "apcp24gt10")[0], "time_apcp24gt10")
        self.assertEqual(store_dimensions(ECMWF, "prmslsd")[0], "time")
        axis = [*range(0, 145, 3), *range(150, 241, 6)]
        offsets = _variable_offsets(ECMWF, axis, ("tmp2m", "prate", "apcp24gt10", "prmslsd"), ("tmp2m", "prate", "apcp24gt10", "prmslsd"), 3600)
        self.assertEqual(offsets["apcp24gt10"], list(range(24, 241, 12)))
        self.assertEqual(offsets["prmslsd"], axis)
        self.assertEqual(offsets["prate"], axis[1:])
        # A build that ends before the first window has nothing to publish.
        self.assertEqual(_variable_offsets(ECMWF, [0, 3, 6], ("apcp24gt10",), ("apcp24gt10",), 3600)["apcp24gt10"], [])


class NativeParityTests(ParityCase, unittest.TestCase):
    """The same two frames through both encoders, byte for byte — every
    alternate identity, the fraction unit, the analysis-less gust axis, the
    bitmap fills and the wave vector, on real ECMWF records."""

    reference = REFERENCE
    expect = (
        store_path("ecmwf.2026112612", "gust"),
        store_path("ecmwf.2026112612", "wave", "uwave"),
        store_path("ecmwf.2026112612", "tmp850sd"),
    )


if __name__ == "__main__":
    unittest.main()

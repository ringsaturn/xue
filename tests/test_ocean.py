"""The ocean fields: the surface (skin) temperature, sea ice cover and
thickness from pgrb2, and the significant wave height, primary wave period
and direction from the cycle's GFS-Wave file.

Like the surface diagnostics before them, they add nothing to the container
and everything to the registries: a GRIB2 identity (the first ones in the
oceanographic discipline), a linear codebook, a record matcher, a bundle id.
`tests/fixtures/ocean-registry.json` is the committed golden that holds the
Python and Rust encoders to one set of numbers: this module regenerates and
compares it, and the Rust encoder's unit tests read the same file.

Two things are new with the wave fields, and both are checked here against
the real records in the GRIB fixture: a source reading a second file family
of the same cycle (``sources.CompanionFile``), and a record that does not
cover its grid — land arrives as GDAL's GRIB nodata value and becomes the
bottom of the codebook, since the format carries no bitmap.
"""

from __future__ import annotations

import json
import math
import unittest
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from xuebuild import grib2
from xuebuild.binconvert import (
    VECTOR_BUNDLES,
    VIDEO_VARIABLE_IDS,
    GridInfo,
    _convert_units,
    _extract_planes,
    _grid_info,
    _snap_global_longitudes,
    published_bundle_ids,
)
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import _run_is_complete, companion_object_url, object_url, wave_object_url
from xuebuild.gdal import _band_matches, raster_expression
from xuebuild.grib2 import MessageInfo, _matches
from xuebuild.model import GfsRun, SourceFrame
from xuebuild.quantize import PROFILES
from xuebuild.sources import source_spec
from xuebuild.variables import OCEAN_VARIABLE_IDS, variable_spec

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "gfs.2026081406.f000.crop.grib2"
REGISTRY = Path(__file__).resolve().parent / "fixtures" / "ocean-registry.json"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
WAVE_IDS = ("htsgw", "perpw", "dirpw")
GDAL_GRIB_NODATA = 9999.0


def registry_entry(variable_id: str) -> dict:
    """The registry as the implementations must agree it is."""
    spec = variable_spec(variable_id)
    return {
        "label": spec.label,
        "unit": spec.output_unit,
        "parameter": spec.parameter_metadata(),
        "quality": PROFILES["quality"][variable_id].metadata(),
        "compact": PROFILES["compact"][variable_id].metadata(),
    }


class RegistryTests(unittest.TestCase):
    def test_the_committed_registry_still_describes_this_encoder(self) -> None:
        expected = json.loads(REGISTRY.read_text(encoding="utf-8"))
        actual = {variable_id: registry_entry(variable_id) for variable_id in OCEAN_VARIABLE_IDS}
        self.assertEqual(
            actual,
            expected,
            "the ocean registry moved; the Rust encoder reads the same fixture, so "
            "regenerate it deliberately and change both",
        )

    def test_balanced_is_quality_except_for_ice_cover(self) -> None:
        # Ice concentration is read in tenths, so production takes the same
        # 1 % step cloud cover does.
        for variable_id in OCEAN_VARIABLE_IDS:
            expected = "compact" if variable_id == "icec" else "quality"
            self.assertEqual(
                PROFILES["balanced"][variable_id].metadata(),
                PROFILES[expected][variable_id].metadata(),
                variable_id,
            )

    def test_the_code_space_is_spent(self) -> None:
        for variable_id in OCEAN_VARIABLE_IDS:
            quality = PROFILES["quality"][variable_id]
            compact = PROFILES["compact"][variable_id]
            with self.subTest(variable=variable_id):
                self.assertEqual(compact.minimum, quality.minimum)
                self.assertEqual(compact.step, quality.step * 2)
                self.assertLessEqual(quality.maximum_code, 254)
                self.assertGreaterEqual(quality.maximum_code, 200)
                # The bottom of the codebook is what a bitmap-masked point
                # becomes, so it must be the value the registry says it is.
                self.assertEqual(quality.minimum, float(variable_spec(variable_id).value_range[0]))
        # Every profile of the direction codebook stops short of 360, which
        # would alias 0.
        for profile in ("quality", "compact", "balanced"):
            self.assertLess(PROFILES[profile]["dirpw"].maximum, 360.0, profile)
        # The skin temperature shares the 2 m temperature's floor and step
        # and runs seventeen degrees further.
        self.assertEqual((PROFILES["quality"]["tmpsfc"].minimum, PROFILES["quality"]["tmpsfc"].step), (-60.0, 0.5))
        self.assertEqual(PROFILES["quality"]["tmpsfc"].maximum, 67.0)

    def test_each_variable_is_registered_where_its_record_lives(self) -> None:
        skin = variable_spec("tmpsfc")
        self.assertEqual((skin.grib2_discipline, skin.grib2_category, skin.grib2_number), (0, 0, 0))
        self.assertEqual((skin.grib2_level_type, skin.grib2_level_value), (1, 0.0))
        self.assertEqual(skin.index_field, ":TMP:surface:")
        self.assertEqual(skin.fill_values, (), "the skin temperature covers land and sea alike")
        for variable_id, number, unit in (("icec", 0, "%"), ("icetk", 1, "m")):
            spec = variable_spec(variable_id)
            self.assertEqual((spec.grib2_discipline, spec.grib2_category, spec.grib2_number), (10, 2, number))
            self.assertEqual((spec.grib2_level_type, spec.grib2_level_value, spec.output_unit), (1, 0.0, unit))
            self.assertEqual(spec.fill_values, ())
        for variable_id, number in (("htsgw", 3), ("perpw", 11), ("dirpw", 10)):
            spec = variable_spec(variable_id)
            self.assertEqual((spec.grib2_discipline, spec.grib2_category, spec.grib2_number), (10, 0, number))
            self.assertEqual(spec.grib2_level_type, 1)
            self.assertIsNone(spec.grib2_level_value, "WAVEWATCH III writes the surface with value 1; either is accepted")
            self.assertEqual(spec.fill_values, (GDAL_GRIB_NODATA,), "the wave records carry a bitmap")
            self.assertEqual(spec.index_field, f":{spec.grib_element}:surface:")
        for variable_id in OCEAN_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            self.assertIsNone(spec.grib2_statistical, f"{variable_id} is an instantaneous product")
            self.assertEqual(spec.ecmwf_param, "", f"{variable_id} is not fetched from ECMWF yet")

    def test_gfs_publishes_them_and_the_other_sources_do_not_yet(self) -> None:
        gfs = source_spec("gfs")
        published = published_bundle_ids(gfs)
        for variable_id in OCEAN_VARIABLE_IDS:
            self.assertIn(variable_id, published)
            self.assertIn(variable_id, gfs.input_variable_ids)
            self.assertNotIn(variable_id, VECTOR_BUNDLES)
            self.assertNotIn(variable_id, VIDEO_VARIABLE_IDS)
        for source in (source_spec("ecmwf"), source_spec("sflux"), source_spec("radar")):
            self.assertEqual(source.companion_files, ())
            for variable_id in OCEAN_VARIABLE_IDS:
                self.assertNotIn(variable_id, published_bundle_ids(source), source.id)

    def test_the_wave_fields_come_from_the_companion_family(self) -> None:
        gfs = source_spec("gfs")
        (wave,) = gfs.companion_files
        self.assertEqual((wave.id, wave.variable_ids), ("wave", WAVE_IDS))
        for variable_id in WAVE_IDS:
            self.assertIs(gfs.companion_of(variable_id), wave)
        for variable_id in ("tmpsfc", "icec", "icetk", "tmp2m", "prate"):
            self.assertIsNone(gfs.companion_of(variable_id))
        # Assembly order: every primary record, then the companion's, so the
        # stated input order is the order the records sit in the frame.
        primary = gfs.primary_input_ids()
        self.assertEqual(gfs.input_variable_ids, primary + WAVE_IDS)
        self.assertEqual(len(primary), 37)


class FetchTests(unittest.TestCase):
    def test_the_wave_object_sits_beside_atmos(self) -> None:
        run = GfsRun(datetime(2026, 8, 14, 6, tzinfo=UTC))
        self.assertTrue(object_url(run, 0).endswith("/gfs.20260814/06/atmos/gfs.t06z.pgrb2.0p25.f000"))
        self.assertTrue(wave_object_url(run, 0).endswith("/gfs.20260814/06/wave/gridded/gfswave.t06z.global.0p25.f000.grib2"))
        self.assertTrue(wave_object_url(run, 240).endswith("gfswave.t06z.global.0p25.f240.grib2"))
        self.assertEqual(companion_object_url(run, 6, "wave"), wave_object_url(run, 6))
        with self.assertRaises(DownloadError):
            companion_object_url(run, 6, "ice")

    def test_a_run_is_complete_only_with_its_wave_frames(self) -> None:
        run = GfsRun(datetime(2026, 8, 14, 6, tzinfo=UTC))
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return "gfswave" not in url or not url.endswith("f240.grib2")

        # The wave f240 lands on its own schedule (occasionally twenty
        # minutes after the pgrb2 f240); until it does the cycle is not
        # complete for this source.
        self.assertFalse(_run_is_complete(run, 240, "gfs", exists))
        self.assertEqual(
            probed,
            [object_url(run, 0), object_url(run, 240), wave_object_url(run, 0), wave_object_url(run, 240)],
        )
        self.assertTrue(_run_is_complete(run, 120, "gfs", exists))
        # sflux reads one family and probes only it.
        probed.clear()
        self.assertTrue(_run_is_complete(run, 240, "sflux", exists))
        self.assertEqual(len(probed), 2)
        self.assertFalse(any("gfswave" in url for url in probed))


class MatcherTests(unittest.TestCase):
    def test_the_header_index_matches_the_records_as_written(self) -> None:
        def message(discipline: int, category: int, number: int, level_type: int, level_value: float | None) -> MessageInfo:
            return MessageInfo(
                band=1,
                discipline=discipline,
                parameter_category=category,
                parameter_number=number,
                level_type=level_type,
                level_value=level_value,
                reference_time=EPOCH,
                valid_time=EPOCH,
                statistical_process=None,
            )

        self.assertTrue(_matches(variable_spec("tmpsfc"), message(0, 0, 0, 1, 0.0)))
        self.assertFalse(_matches(variable_spec("tmpsfc"), message(0, 0, 0, 103, 2.0)), "the 2 m temperature")
        self.assertFalse(_matches(variable_spec("tmpsfc"), message(0, 0, 0, 100, 85000.0)), "an isobaric temperature")
        self.assertTrue(_matches(variable_spec("icec"), message(10, 2, 0, 1, 0.0)))
        self.assertFalse(_matches(variable_spec("icec"), message(0, 2, 0, 1, 0.0)), "0/2/0 is the wind direction")
        self.assertTrue(_matches(variable_spec("icetk"), message(10, 2, 1, 1, 0.0)))
        # The wave surface value: 1 as WAVEWATCH III writes it, 0 as GRIB2
        # would, none at all — all the same water surface.
        for level_value in (1.0, 0.0, None):
            self.assertTrue(_matches(variable_spec("htsgw"), message(10, 0, 3, 1, level_value)), level_value)
            self.assertTrue(_matches(variable_spec("perpw"), message(10, 0, 11, 1, level_value)), level_value)
            self.assertTrue(_matches(variable_spec("dirpw"), message(10, 0, 10, 1, level_value)), level_value)
        self.assertFalse(_matches(variable_spec("htsgw"), message(10, 0, 5, 1, 1.0)), "the wind-wave height")
        self.assertFalse(_matches(variable_spec("htsgw"), message(10, 0, 3, 241, 1.0)), "a swell partition")

    def test_gdal_bands_match_on_element_and_surface(self) -> None:
        self.assertTrue(_band_matches("tmpsfc", {"GRIB_ELEMENT": "TMP", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertTrue(_band_matches("tmpsfc", {"GRIB_ELEMENT": "TMP"}, '0[-] SFC="Ground or water surface"'))
        self.assertFalse(_band_matches("tmpsfc", {"GRIB_ELEMENT": "TMP", "GRIB_SHORT_NAME": "2-HTGL"}, ""))
        self.assertFalse(
            _band_matches("tmpsfc", {"GRIB_ELEMENT": "TMP", "GRIB_SHORT_NAME": "85000-ISBL"}, '85000[Pa] ISBL="Isobaric surface"'),
            "an isobaric level is a surface too, in prose",
        )
        self.assertFalse(_band_matches("tmp2m", {"GRIB_ELEMENT": "TMP", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        for variable_id, element in (("icec", "ICEC"), ("icetk", "ICETK")):
            self.assertTrue(_band_matches(variable_id, {"GRIB_ELEMENT": element, "GRIB_SHORT_NAME": "0-SFC"}, ""))
            self.assertFalse(_band_matches(variable_id, {"GRIB_ELEMENT": "ICETMP", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        for variable_id, element in (("htsgw", "HTSGW"), ("perpw", "PERPW"), ("dirpw", "DIRPW")):
            self.assertTrue(_band_matches(variable_id, {"GRIB_ELEMENT": element, "GRIB_SHORT_NAME": "1-SFC"}, ""))
            self.assertTrue(_band_matches(variable_id, {"GRIB_ELEMENT": element, "GRIB_SHORT_NAME": "0-SFC"}, ""))
            self.assertTrue(_band_matches(variable_id, {"GRIB_ELEMENT": element}, '1[-] SFC="Ground or water surface"'))
            self.assertFalse(_band_matches(variable_id, {"GRIB_ELEMENT": element, "GRIB_SHORT_NAME": "1-RESERVED(241)"}, ""))
            self.assertFalse(_band_matches(variable_id, {"GRIB_ELEMENT": "WVHGT", "GRIB_SHORT_NAME": "1-SFC"}, ""))

    def test_units_are_accepted_as_gdal_reports_them(self) -> None:
        for variable_id in OCEAN_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            with self.subTest(variable=variable_id):
                raster_expression(variable_id, spec.gdal_unit)
                raster_expression(variable_id, f"[{spec.gdal_unit}]")
        self.assertEqual(raster_expression("tmpsfc", "[C]"), "maximum(-60,minimum(67,A))")
        self.assertEqual(raster_expression("tmpsfc", "K"), "maximum(-60,minimum(67,A-273.15))")
        self.assertEqual(raster_expression("icec", "[Proportion]"), "maximum(0,minimum(100,A*100))")
        self.assertEqual(raster_expression("icetk", "[m]"), "maximum(0,minimum(5.08,A))")
        self.assertEqual(raster_expression("htsgw", "[m]"), "maximum(0,minimum(25.4,A))")
        self.assertEqual(raster_expression("perpw", "[s]"), "maximum(0,minimum(25.4,A))")
        self.assertEqual(raster_expression("dirpw", "[Degree true]"), "A")
        for variable_id, unit in (("icec", "%"), ("icetk", "cm"), ("htsgw", "ft"), ("perpw", "Hz"), ("dirpw", "rad"), ("tmpsfc", "m")):
            with self.assertRaises(ConversionError):
                raster_expression(variable_id, unit)

    def test_the_fixture_carries_every_ocean_record(self) -> None:
        # The GRIB fixture is recut from the whole GFS frame — pgrb2 records
        # then the wave file's — so both matchers find the six in it.
        gfs = source_spec("gfs")
        fast = grib2.inspect_grib_fast(FIXTURE, gfs.input_variable_ids)
        for variable_id, band in zip(OCEAN_VARIABLE_IDS, range(35, 41), strict=True):
            self.assertEqual(fast[variable_id].band, band, variable_id)
            self.assertEqual(fast[variable_id].unit, variable_spec(variable_id).gdal_unit)


class ConversionTests(unittest.TestCase):
    def frame(self, variable_id: str, unit: str) -> SourceFrame:
        return SourceFrame(FIXTURE, 1, variable_id, EPOCH, EPOCH, 0, unit)

    def test_units_reach_the_codebook(self) -> None:
        skin = _convert_units(self.frame("tmpsfc", "K"), np.array([273.15, 300.0]))
        np.testing.assert_allclose(skin, [0.0, 26.85])
        np.testing.assert_array_equal(_convert_units(self.frame("tmpsfc", "C"), np.array([12.5])), [12.5])
        np.testing.assert_allclose(_convert_units(self.frame("icec", "Proportion"), np.array([0.0, 0.85, 1.0])), [0.0, 85.0, 100.0])
        np.testing.assert_array_equal(_convert_units(self.frame("icetk", "m"), np.array([2.5])), [2.5])
        np.testing.assert_array_equal(_convert_units(self.frame("htsgw", "m"), np.array([3.25])), [3.25])
        np.testing.assert_array_equal(_convert_units(self.frame("perpw", "s"), np.array([11.0])), [11.0])

    def test_a_direction_of_360_is_the_codebook_zero(self) -> None:
        direction = _convert_units(self.frame("dirpw", "Degree true"), np.array([0.0, 359.37, 360.0, 1.35]))
        np.testing.assert_allclose(direction, [0.0, 359.37, 0.0, 1.35])
        codes = PROFILES["quality"]["dirpw"].quantize(direction)
        # 359.37 clamps to the top code (358.5°), a degree short of the wrap,
        # rather than to a code that would read as 360 = 0.
        self.assertEqual(codes.tolist(), [0, 239, 0, 1])

    def test_land_becomes_the_bottom_of_the_wave_codebooks(self) -> None:
        gfs = source_spec("gfs")
        grid = _grid_info(FIXTURE)
        frames = grib2.inspect_grib_fast(FIXTURE, gfs.input_variable_ids)
        import tempfile

        with tempfile.TemporaryDirectory() as work:
            planes = _extract_planes({v: frames[v] for v in ("tmp2m", *OCEAN_VARIABLE_IDS)}, grid, Path(work))
        for variable_id in OCEAN_VARIABLE_IDS:
            self.assertTrue(np.isfinite(planes[variable_id]).all(), variable_id)
            self.assertLess(planes[variable_id].max(), GDAL_GRIB_NODATA, f"{variable_id} still carries the nodata value")
        # The window is 118–138E, 18–38N: about a sixth of it is land, where
        # every wave field is 0 — and the same cells in every wave field.
        land = planes["htsgw"] == 0.0
        self.assertGreater(land.mean(), 0.1)
        self.assertLess(land.mean(), 0.3)
        np.testing.assert_array_equal(planes["perpw"] == 0.0, land)
        np.testing.assert_array_equal(planes["dirpw"][land], 0.0)
        # Where there is water there are waves: the period is never 0 at sea.
        self.assertGreater(planes["perpw"][~land].min(), 0.5)
        self.assertLess(planes["htsgw"][~land].max(), 25.4)
        # The skin temperature has no bitmap: land and sea alike, August.
        self.assertGreater(planes["tmpsfc"].min(), 15.0)
        self.assertLess(planes["tmpsfc"].max(), 45.0)
        # Nothing frozen in the South China Sea.
        np.testing.assert_array_equal(planes["icec"], 0.0)
        np.testing.assert_array_equal(planes["icetk"], 0.0)


class GlobalGridTests(unittest.TestCase):
    """WAVEWATCH III writes the last longitude of the 0.25° grid as
    359.750016°, and GDAL derives the step from it; the encoder must still
    see the same grid the pgrb2 record of the same cycle describes."""

    WAVE_STEP = 0.2500000111188325
    WAVE_ORIGIN = -180.12500000555943

    def wave_grid(self) -> GridInfo:
        return GridInfo(
            width=1440,
            height=721,
            first_longitude=self.WAVE_ORIGIN + self.WAVE_STEP / 2,
            first_latitude=90.0,
            longitude_step=self.WAVE_STEP,
            latitude_step=-0.25,
        )

    def test_the_wave_grid_snaps_to_the_pgrb2_grid(self) -> None:
        raw = self.wave_grid()
        self.assertFalse(raw.wraps, "as GDAL reports it, the grid does not close")
        snapped = _snap_global_longitudes(raw)
        self.assertEqual((snapped.longitude_step, snapped.first_longitude), (0.25, -180.0))
        self.assertTrue(snapped.wraps)
        self.assertEqual(snapped.metadata(), _snap_global_longitudes(GridInfo(1440, 721, -180.0, 90.0, 0.25, -0.25)).metadata())

    def test_exact_grids_pass_through_unchanged(self) -> None:
        for grid in (
            GridInfo(1440, 721, -180.0, 90.0, 0.25, -0.25),
            # The sflux Gaussian grid before its roll: first center at 0.
            GridInfo(3072, 1536, 0.0, 89.91, 0.1171875, -0.117),
            # A regional grid is not global however its step is spelt.
            GridInfo(80, 80, 118.0, 38.0, self.WAVE_STEP, -0.25),
            GridInfo(80, 80, 118.0, 38.0, 0.25, -0.25),
        ):
            with self.subTest(grid=grid):
                snapped = _snap_global_longitudes(grid)
                self.assertEqual(snapped.longitude_step, grid.longitude_step)
                self.assertEqual(snapped.first_longitude, grid.first_longitude)

    def test_an_origin_off_the_step_grid_keeps_its_step_only(self) -> None:
        # A global grid whose first center sits between two multiples of its
        # step (a staggered grid) gets the exact step and keeps its origin.
        grid = GridInfo(1440, 721, -179.9, 90.0, self.WAVE_STEP, -0.25)
        snapped = _snap_global_longitudes(grid)
        self.assertEqual(snapped.longitude_step, 0.25)
        self.assertEqual(snapped.first_longitude, -179.9)

    def test_a_grid_more_than_a_thousandth_of_a_cell_short_is_not_global(self) -> None:
        grid = GridInfo(1440, 721, -180.0, 90.0, 0.25 * (1 - 2e-3), -0.25)
        self.assertIs(_snap_global_longitudes(grid), grid)
        self.assertFalse(math.isclose(grid.width * grid.longitude_step, 360.0))


if __name__ == "__main__":
    unittest.main()

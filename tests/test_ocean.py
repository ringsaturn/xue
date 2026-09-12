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

The wave vector (``wave``, the ``uwave`` / ``vwave`` pair) is the first
vector bundle derived from two scalars that are published themselves: the
height laid along the direction of travel, so the frontend draws the sea
the way it draws the wind. Its derivation is checked here for convention
and for the round trip back through the codebook.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from xuebuild import grib2
from xuebuild.assemble import bundle_group_matrix, group_needs_eccodes
from xuebuild.binconvert import (
    DERIVED_VECTORS,
    VECTOR_BUNDLES,
    VIDEO_VARIABLE_IDS,
    WAVE_BUNDLE_ID,
    GridInfo,
    _convert_units,
    _extract_planes,
    _grid_info,
    _snap_global_longitudes,
    bundle_input_ids,
    derive_vector,
    derive_wave_vector,
    published_bundle_ids,
    vector_input_ids,
)
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    _repack_grid_simple,
    _run_is_complete,
    companion_object_url,
    object_url,
    wave_object_url,
)
from xuebuild.gdal import _band_matches, raster_expression
from xuebuild.grib2 import MessageInfo, _matches
from xuebuild.model import GfsRun, SourceFrame
from xuebuild.quantize import PROFILES
from xuebuild.sources import source_spec
from xuebuild.variables import OCEAN_VARIABLE_IDS, WAVE_VECTOR_COMPONENT_IDS, variable_spec

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
        actual = {
            variable_id: registry_entry(variable_id)
            for variable_id in OCEAN_VARIABLE_IDS + WAVE_VECTOR_COMPONENT_IDS
        }
        self.assertEqual(
            actual,
            expected,
            "the ocean registry moved; the Rust encoder reads the same fixture, so "
            "regenerate it deliberately and change both",
        )

    def test_balanced_is_quality_except_for_ice_cover(self) -> None:
        # Ice concentration is read in tenths, so production takes the same
        # 1 % step cloud cover does.
        for variable_id in OCEAN_VARIABLE_IDS + WAVE_VECTOR_COMPONENT_IDS:
            expected = "compact" if variable_id == "icec" else "quality"
            self.assertEqual(
                PROFILES["balanced"][variable_id].metadata(),
                PROFILES[expected][variable_id].metadata(),
                variable_id,
            )

    def test_the_code_space_is_spent(self) -> None:
        for variable_id in OCEAN_VARIABLE_IDS + WAVE_VECTOR_COMPONENT_IDS:
            quality = PROFILES["quality"][variable_id]
            compact = PROFILES["compact"][variable_id]
            with self.subTest(variable=variable_id):
                self.assertEqual(compact.step, quality.step * 2)
                self.assertLessEqual(quality.maximum_code, 254)
                self.assertGreaterEqual(quality.maximum_code, 200)
                if variable_id in WAVE_VECTOR_COMPONENT_IDS:
                    # Symmetric, and 0 on the grid in both profiles: land
                    # is (0, 0) exactly, so the compact codebook gives up a
                    # code at each end rather than the middle.
                    self.assertEqual(compact.minimum, quality.minimum + quality.step)
                    for book in (quality, compact):
                        self.assertEqual(-book.minimum, book.maximum)
                        self.assertEqual(book.quantize(np.array([0.0])).tolist(), [book.maximum_code // 2])
                    continue
                self.assertEqual(compact.minimum, quality.minimum)
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

    def test_the_wave_vector_components_are_derived_and_local(self) -> None:
        # Never matched against a record — the converter derives them — so
        # the matching fields stay empty; a Xue-local pair of numbers in the
        # waves category on the same water surface as the inputs, with no
        # surface value declared, like the inputs themselves.
        for variable_id, number in zip(WAVE_VECTOR_COMPONENT_IDS, (250, 251), strict=True):
            spec = variable_spec(variable_id)
            self.assertEqual((spec.grib_element, spec.index_field, spec.gdal_unit), ("", "", ""))
            self.assertEqual((spec.grib2_discipline, spec.grib2_category, spec.grib2_number), (10, 0, number))
            self.assertEqual(spec.grib2_level_type, 1)
            self.assertIsNone(spec.grib2_level_value)
            self.assertEqual(spec.output_unit, "m")
            self.assertEqual(spec.fill_values, ())
            with self.assertRaises(ConversionError):
                _band_matches(variable_id, {"GRIB_ELEMENT": "HTSGW", "GRIB_SHORT_NAME": "1-SFC"}, "")
        # Symmetric over the height codebook's own coverage, so no sea the
        # height field holds clamps in the vector, and land is the middle
        # code of both components.
        height = PROFILES["quality"]["htsgw"]
        for variable_id in WAVE_VECTOR_COMPONENT_IDS:
            for profile in ("quality", "balanced"):
                book = PROFILES[profile][variable_id]
                self.assertEqual((book.minimum, book.maximum), (-height.maximum, height.maximum), profile)
                self.assertEqual(book.quantize(np.array([0.0, -0.0])).tolist(), [127, 127], profile)
            self.assertEqual(PROFILES["compact"][variable_id].quantize(np.array([0.0, -0.0])).tolist(), [63, 63])
        self.assertEqual(PROFILES["quality"]["uwave"].step, 2 * height.step)

    def test_gfs_publishes_them_and_the_other_sources_do_not_yet(self) -> None:
        gfs = source_spec("gfs")
        published = published_bundle_ids(gfs)
        for variable_id in OCEAN_VARIABLE_IDS:
            self.assertIn(variable_id, published)
            self.assertIn(variable_id, gfs.input_variable_ids)
            self.assertNotIn(variable_id, VECTOR_BUNDLES)
            self.assertNotIn(variable_id, VIDEO_VARIABLE_IDS)
        # The wave vector is the last bundle GFS publishes: a vector, derived
        # from two of the scalars above, which stay published beside it — a
        # reader who wants the height to a tenth or the direction to a
        # degree and a half has them.
        self.assertEqual(published[-1], WAVE_BUNDLE_ID)
        self.assertEqual(VECTOR_BUNDLES[WAVE_BUNDLE_ID], WAVE_VECTOR_COMPONENT_IDS)
        self.assertEqual(DERIVED_VECTORS[WAVE_BUNDLE_ID], ("htsgw", "dirpw"))
        self.assertEqual(vector_input_ids(WAVE_BUNDLE_ID), ("htsgw", "dirpw"))
        self.assertEqual(bundle_input_ids(gfs, WAVE_BUNDLE_ID), ("htsgw", "dirpw"))
        self.assertNotIn(WAVE_BUNDLE_ID, VIDEO_VARIABLE_IDS)
        for source in (source_spec("ecmwf"), source_spec("sflux"), source_spec("radar")):
            self.assertEqual(source.companion_files, ())
            for variable_id in OCEAN_VARIABLE_IDS + (WAVE_BUNDLE_ID,):
                self.assertNotIn(variable_id, published_bundle_ids(source), source.id)

    def test_the_wave_fields_come_from_the_companion_family(self) -> None:
        gfs = source_spec("gfs")
        (wave,) = gfs.companion_files
        self.assertEqual((wave.id, wave.variable_ids), ("wave", WAVE_IDS))
        # WAVEWATCH III packs its records as JPEG 2000, which the wheel's
        # GDAL cannot read: the fetcher repacks them, and the build job of
        # any bundle from the family installs grib_set.
        self.assertTrue(wave.repack)
        self.assertTrue(group_needs_eccodes(gfs, ("tmp2m", "htsgw")))
        self.assertTrue(group_needs_eccodes(gfs, ("tmp2m", WAVE_BUNDLE_ID)), "the wave vector reads the family too")
        self.assertFalse(group_needs_eccodes(gfs, ("tmp2m", "tmpsfc", "icec")))
        flags = {entry["group"]: entry["eccodes"] for entry in bundle_group_matrix(gfs, 100)}
        self.assertEqual([group for group, needs in flags.items() if needs], [*WAVE_IDS, WAVE_BUNDLE_ID])
        self.assertFalse(any(entry["eccodes"] for entry in bundle_group_matrix(source_spec("sflux"), 100)))
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


class RepackTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("grib_set"), "eccodes CLI")
    def test_repacking_rewrites_every_message_as_grid_simple(self) -> None:
        # The plumbing only: the fixture's messages were written by GDAL
        # (grid_ieee and grid_complex with GDAL's own scale choices), which
        # `grib_set -r` does not carry over faithfully, so the values are
        # not compared here. The product this is for — WAVEWATCH III's JPEG
        # 2000 records, 10/12/16 bits at a decimal scale of 2 — repacks to
        # the same integers; that was verified on a live frame by hand, and
        # the byte-identity of the two encoders on a repacked frame with it.
        repacked = _repack_grid_simple(FIXTURE.read_bytes(), "fixture")
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "repacked.grib2"
            path.write_bytes(repacked)
            self.assertEqual(len(grib2.index_messages(path)), 40, "every message survives, in order")
            listing = subprocess.run(
                ["grib_ls", "-p", "packingType", str(path)], capture_output=True, text=True, check=True
            ).stdout
            self.assertEqual(listing.count("grid_simple"), 40)
        with self.assertRaises(DownloadError):
            _repack_grid_simple(b"not a grib message", "nowhere")


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


class WaveVectorTests(unittest.TestCase):
    """The wave vector: the significant height along the direction of
    travel, in the wind's convention, so the frontend's vector path — the
    magnitude shader, the particles, the probe's ``atan2(-u, -v)`` — reads
    it without knowing it is not a wind."""

    def test_the_components_follow_the_wind_convention(self) -> None:
        # Degrees true the waves come *from*: from the north they travel
        # south (v negative), from the east they travel west (u negative).
        u, v = derive_wave_vector(
            {"htsgw": np.array([2.0, 2.0, 2.0, 2.0, 3.0]), "dirpw": np.array([0.0, 90.0, 180.0, 270.0, 45.0])},
            WAVE_BUNDLE_ID,
        )
        np.testing.assert_allclose(u, [0.0, -2.0, 0.0, 2.0, -3 * math.sqrt(0.5)], atol=1e-12)
        np.testing.assert_allclose(v, [-2.0, 0.0, 2.0, 0.0, -3 * math.sqrt(0.5)], atol=1e-12)
        # Back to what the probe shows: the magnitude is the height, the
        # direction the one the record carried.
        np.testing.assert_allclose(np.hypot(u, v), [2.0, 2.0, 2.0, 2.0, 3.0])
        np.testing.assert_allclose(np.degrees(np.arctan2(-u, -v)) % 360.0, [0.0, 90.0, 180.0, 270.0, 45.0], atol=1e-9)
        # derive_vector dispatches the wave bundle here and the flux
        # bundles to their own derivation.
        same_u, same_v = derive_vector({"htsgw": np.array([1.0]), "dirpw": np.array([30.0])}, WAVE_BUNDLE_ID)
        np.testing.assert_array_equal((same_u, same_v), derive_wave_vector({"htsgw": np.array([1.0]), "dirpw": np.array([30.0])}, WAVE_BUNDLE_ID))

    def test_land_is_the_middle_code_of_both_components(self) -> None:
        # Land is 0 m from 0° in the inputs (the bitmap's fill), so the
        # vector there is (0, 0) — code 127 in both — and a renderer's
        # magnitude palette paints nothing, as the height's bottom code does.
        gfs = source_spec("gfs")
        grid = _grid_info(FIXTURE)
        frames = grib2.inspect_grib_fast(FIXTURE, gfs.input_variable_ids)
        with tempfile.TemporaryDirectory() as work:
            planes = _extract_planes({v: frames[v] for v in ("htsgw", "dirpw")}, grid, Path(work))
        planes = {variable_id: _convert_units(frames[variable_id], plane) for variable_id, plane in planes.items()}
        u, v = derive_vector(planes, WAVE_BUNDLE_ID)
        land = planes["htsgw"] == 0.0
        book = PROFILES["balanced"]["uwave"]
        codes_u, codes_v = book.quantize(u), book.quantize(v)
        np.testing.assert_array_equal(codes_u[land], 127)
        np.testing.assert_array_equal(codes_v[land], 127)
        # At sea the pair reconstructs the height within the codebook's
        # error budget, and nothing clamps: the height's own range is the
        # vector's.
        self.assertFalse(np.isin(codes_u, (0, 254)).any())
        self.assertFalse(np.isin(codes_v, (0, 254)).any())
        height = np.hypot(book.decode(codes_u), book.decode(codes_v))
        self.assertLess(np.abs(height - planes["htsgw"])[~land].max(), book.step * math.sqrt(0.5) + 1e-9)
        self.assertGreater(np.count_nonzero(codes_u != 127), 0)


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

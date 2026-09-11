"""The surface diagnostics: wind gust, total cloud cover and convective
available potential energy.

Like the pressure family and the upper-air fills before them, these add
nothing to the container and everything to the registries: a GRIB2 identity,
a linear codebook, a record matcher, a bundle id. `tests/fixtures/surface-registry.json`
is the committed golden that holds the three implementations to one set of
numbers: this module regenerates and compares it, the Rust encoder's unit
tests and the frontend's vitest read the same file, so a codebook can only
move in all three at once.

Registration is not publication: no source lists these yet, and the tests
here say so, because publishing one means widening a source's input list and
recutting the GRIB fixture the parity test builds from.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path

from xuebuild.binconvert import VECTOR_BUNDLES, VIDEO_VARIABLE_IDS, published_bundle_ids
from xuebuild.errors import ConversionError
from xuebuild.gdal import _band_matches, raster_expression
from xuebuild.grib2 import MessageInfo, _matches
from xuebuild.quantize import PROFILES, SURFACE_VARIABLE_IDS
from xuebuild.sources import SOURCES
from xuebuild.variables import variable_spec

REGISTRY = Path(__file__).resolve().parent / "fixtures" / "surface-registry.json"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def registry_entry(variable_id: str) -> dict:
    """The registry as the three implementations must agree it is."""
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
        actual = {variable_id: registry_entry(variable_id) for variable_id in SURFACE_VARIABLE_IDS}
        self.assertEqual(
            actual,
            expected,
            "the surface registry moved; the Rust encoder and the frontend read the same "
            "fixture, so regenerate it deliberately and change all three",
        )

    def test_balanced_is_quality_except_for_cloud_cover(self) -> None:
        # Cloud cover is relative humidity's kind of field — small-scale
        # structure everywhere, read in tens of percent — and takes the same
        # 1 % step in production.
        for variable_id in SURFACE_VARIABLE_IDS:
            expected = "compact" if variable_id == "tcdc" else "quality"
            self.assertEqual(
                PROFILES["balanced"][variable_id].metadata(),
                PROFILES[expected][variable_id].metadata(),
                variable_id,
            )

    def test_the_code_space_is_spent_and_the_profiles_cover_the_same_range(self) -> None:
        for variable_id in SURFACE_VARIABLE_IDS:
            quality = PROFILES["quality"][variable_id]
            compact = PROFILES["compact"][variable_id]
            with self.subTest(variable=variable_id):
                self.assertEqual(quality.minimum, 0.0, "every surface diagnostic is one-sided")
                self.assertEqual(compact.minimum, quality.minimum)
                self.assertEqual(compact.maximum, quality.maximum)
                self.assertEqual(compact.step, quality.step * 2)
                self.assertLessEqual(quality.maximum_code, 254)
                self.assertGreaterEqual(quality.maximum_code, 200)

    def test_each_variable_is_registered_where_a_pgrb2_record_lives(self) -> None:
        gust = variable_spec("gust")
        self.assertEqual((gust.grib2_category, gust.grib2_number, gust.grib2_level_type), (2, 22, 1))
        self.assertEqual(gust.index_field, ":GUST:surface:")
        cape = variable_spec("cape")
        self.assertEqual((cape.grib2_category, cape.grib2_number, cape.grib2_level_type), (7, 6, 1))
        self.assertEqual(cape.index_field, ":CAPE:surface:")
        tcdc = variable_spec("tcdc")
        self.assertEqual((tcdc.grib2_category, tcdc.grib2_number, tcdc.grib2_level_type), (6, 1, 10))
        self.assertIsNone(tcdc.grib2_level_value, "the entire atmosphere carries no surface value")
        self.assertEqual(tcdc.excluded_index_phrases, ("ave fcst",))
        for variable_id in SURFACE_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            self.assertIsNone(spec.grib2_statistical, f"{variable_id} is an instantaneous product")
            self.assertEqual(spec.ecmwf_param, "", f"{variable_id} has no ECMWF matching rule yet")

    def test_no_source_publishes_them_yet(self) -> None:
        for source in SOURCES.values():
            published = published_bundle_ids(source)
            for variable_id in SURFACE_VARIABLE_IDS:
                self.assertNotIn(variable_id, published, source.id)
                self.assertNotIn(variable_id, source.input_variable_ids, source.id)
                self.assertNotIn(variable_id, VECTOR_BUNDLES)
                self.assertNotIn(variable_id, VIDEO_VARIABLE_IDS)


class MatcherTests(unittest.TestCase):
    def test_the_header_index_accepts_only_the_instantaneous_record(self) -> None:
        def message(category: int, number: int, level_type: int, level_value: float | None, statistical: int | None) -> MessageInfo:
            return MessageInfo(
                band=1,
                discipline=0,
                parameter_category=category,
                parameter_number=number,
                level_type=level_type,
                level_value=level_value,
                reference_time=EPOCH,
                valid_time=EPOCH,
                statistical_process=statistical,
            )

        self.assertTrue(_matches(variable_spec("gust"), message(2, 22, 1, 0.0, None)))
        self.assertFalse(_matches(variable_spec("gust"), message(2, 22, 103, 10.0, None)), "the 10 m gust is another field")
        self.assertTrue(_matches(variable_spec("cape"), message(7, 6, 1, 0.0, None)))
        self.assertFalse(_matches(variable_spec("cape"), message(7, 6, 108, 18000.0, None)), "mixed-layer CAPE")
        self.assertTrue(_matches(variable_spec("tcdc"), message(6, 1, 10, None, None)))
        self.assertFalse(_matches(variable_spec("tcdc"), message(6, 1, 10, None, 0)), "the interval average")
        self.assertFalse(_matches(variable_spec("tcdc"), message(6, 1, 214, None, None)), "the low cloud layer")

    def test_gdal_bands_match_on_element_and_surface(self) -> None:
        self.assertTrue(_band_matches("gust", {"GRIB_ELEMENT": "GUST", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertTrue(_band_matches("gust", {"GRIB_ELEMENT": "GUST"}, '0[-] SFC="Ground or water surface"'))
        self.assertFalse(_band_matches("gust", {"GRIB_ELEMENT": "GUST", "GRIB_SHORT_NAME": "10-HTGL"}, ""))
        self.assertFalse(_band_matches("gust", {"GRIB_ELEMENT": "UGRD", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertTrue(_band_matches("cape", {"GRIB_ELEMENT": "CAPE", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertFalse(_band_matches("cape", {"GRIB_ELEMENT": "CAPE", "GRIB_SHORT_NAME": "18000-0-SPDL"}, ""))
        self.assertTrue(_band_matches("tcdc", {"GRIB_ELEMENT": "TCDC", "GRIB_SHORT_NAME": "0-EATM"}, ""))
        self.assertTrue(_band_matches("tcdc", {"GRIB_ELEMENT": "TCDC"}, '0[-] EATM="Entire atmosphere"'))
        self.assertFalse(_band_matches("tcdc", {"GRIB_ELEMENT": "TCDC", "GRIB_SHORT_NAME": "0-LCY"}, "low cloud layer"))
        self.assertFalse(_band_matches("tcdc", {"GRIB_ELEMENT": "CAPE", "GRIB_SHORT_NAME": "0-EATM"}, ""))

    def test_units_are_accepted_as_gdal_reports_them(self) -> None:
        for variable_id in SURFACE_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            with self.subTest(variable=variable_id):
                raster_expression(variable_id, spec.gdal_unit)
                raster_expression(variable_id, f"[{spec.gdal_unit}]")
        self.assertEqual(raster_expression("gust", "[m/s]"), "maximum(0,minimum(127,A))")
        self.assertEqual(raster_expression("tcdc", "[%]"), "maximum(0,minimum(100,A))")
        self.assertEqual(raster_expression("cape", "[J/kg]"), "maximum(0,minimum(6350,A))")
        for variable_id, unit in (("gust", "km/h"), ("tcdc", "kg/kg"), ("cape", "m^2/s^2")):
            with self.assertRaises(ConversionError):
                raster_expression(variable_id, unit)


if __name__ == "__main__":
    unittest.main()

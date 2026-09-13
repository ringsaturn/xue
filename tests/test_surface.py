"""The surface diagnostics: wind gust, total / low / middle / high cloud
cover, convective available potential energy, visibility, 2 m dew point and
2 m apparent temperature.

Like the pressure family and the upper-air fills before them, these add
nothing to the container and everything to the registries: a GRIB2 identity,
a linear codebook, a record matcher, a bundle id. `tests/fixtures/surface-registry.json`
is the committed golden that holds the three implementations to one set of
numbers: this module regenerates and compares it, the Rust encoder's unit
tests and the frontend's vitest read the same file, so a codebook can only
move in all three at once.

GFS publishes them all; sflux and ECMWF none yet (ECMWF carries neighbours
rather than equivalents of most — ``10fg`` is an interval maximum, ``tcc``
a fraction — and needs its own matching rules first).
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
from xuebuild.sources import source_spec
from xuebuild.variables import variable_spec

REGISTRY = Path(__file__).resolve().parent / "fixtures" / "surface-registry.json"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
CLOUD_COVER_IDS = ("tcdc", "lcdc", "mcdc", "hcdc")


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
        # 1 % step in production, the layers with the total.
        for variable_id in SURFACE_VARIABLE_IDS:
            expected = "compact" if variable_id in CLOUD_COVER_IDS else "quality"
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
                if variable_id not in ("dpt2m", "aptmp2m"):
                    self.assertEqual(quality.minimum, 0.0, "one-sided, like every quantity but a temperature")
                self.assertEqual(compact.minimum, quality.minimum)
                self.assertEqual(compact.maximum, quality.maximum)
                self.assertEqual(compact.step, quality.step * 2)
                self.assertLessEqual(quality.maximum_code, 254)
                # The apparent temperature spends a whole degree over 150 K
                # rather than half a degree over less.
                self.assertGreaterEqual(quality.maximum_code, 150 if variable_id == "aptmp2m" else 200)

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
        for layer, number, surface in (("lcdc", 3, 214), ("mcdc", 4, 224), ("hcdc", 5, 234)):
            spec = variable_spec(layer)
            self.assertEqual((spec.grib2_category, spec.grib2_number, spec.grib2_level_type), (6, number, surface))
            self.assertIsNone(spec.grib2_level_value, "a cloud layer surface carries no value")
            self.assertEqual(spec.excluded_index_phrases, ("ave fcst",))
        vis = variable_spec("vis")
        self.assertEqual((vis.grib2_category, vis.grib2_number, vis.grib2_level_type, vis.output_unit), (19, 0, 1, "km"))
        for two_metre, number in (("dpt2m", 6), ("aptmp2m", 21)):
            spec = variable_spec(two_metre)
            self.assertEqual((spec.grib2_category, spec.grib2_number, spec.grib2_level_type, spec.grib2_level_value), (0, number, 103, 2.0))
            self.assertEqual(spec.index_field, f":{spec.grib_element}:2 m above ground:")
        # ECMWF open data carries the dew point under the same identity, and
        # the gust, the total cloud cover and the CAPE as neighbours accepted
        # through a whole alternate identity each; the layer cloud covers,
        # the visibility and the apparent temperature it does not carry.
        ecmwf_params = {"dpt2m": "2d", "gust": "10fg", "tcdc": "tcc", "cape": "mucape"}
        for variable_id in SURFACE_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            self.assertIsNone(spec.grib2_statistical, f"{variable_id} is an instantaneous product")
            self.assertEqual(spec.ecmwf_param, ecmwf_params.get(variable_id, ""), variable_id)
            self.assertEqual(bool(spec.grib2_alternates), variable_id in ("gust", "tcdc", "cape"), variable_id)
        self.assertEqual(variable_spec("gust").ecmwf_alternate_params, ("10fg3",))
        gust, tcdc, cape = (variable_spec(variable_id).grib2_alternates[0] for variable_id in ("gust", "tcdc", "cape"))
        self.assertEqual((gust.triple, gust.level_type, gust.level_value, gust.statistical), ((0, 2, 22), 103, 10.0, 2))
        self.assertEqual((tcdc.triple, tcdc.level_type, tcdc.level_value, tcdc.gdal_unit), ((0, 6, 192), 1, None, "-"))
        self.assertEqual((cape.triple, cape.level_type, cape.level_value), ((0, 7, 6), 17, None))

    def test_gfs_publishes_them_all_and_ecmwf_what_the_open_data_carries(self) -> None:
        gfs = published_bundle_ids(source_spec("gfs"))
        for variable_id in SURFACE_VARIABLE_IDS:
            self.assertIn(variable_id, gfs)
            self.assertNotIn(variable_id, VECTOR_BUNDLES)
            self.assertNotIn(variable_id, VIDEO_VARIABLE_IDS)
        ecmwf = published_bundle_ids(source_spec("ecmwf"))
        for variable_id in SURFACE_VARIABLE_IDS:
            self.assertEqual(variable_id in ecmwf, variable_id in ("gust", "tcdc", "cape", "dpt2m"), variable_id)
        for source in (source_spec("sflux"), source_spec("radar")):
            published = published_bundle_ids(source)
            for variable_id in SURFACE_VARIABLE_IDS:
                self.assertNotIn(variable_id, published, source.id)


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
        self.assertTrue(_band_matches("vis", {"GRIB_ELEMENT": "VIS", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertTrue(_band_matches("dpt2m", {"GRIB_ELEMENT": "DPT", "GRIB_SHORT_NAME": "2-HTGL"}, ""))
        self.assertTrue(_band_matches("aptmp2m", {"GRIB_ELEMENT": "APTMP", "GRIB_COMMENT": "2 m above ground"}, ""))
        self.assertFalse(_band_matches("dpt2m", {"GRIB_ELEMENT": "TMP", "GRIB_SHORT_NAME": "2-HTGL"}, ""))
        self.assertFalse(_band_matches("tmp2m", {"GRIB_ELEMENT": "DPT", "GRIB_SHORT_NAME": "2-HTGL"}, ""))
        for layer, element, short_name, phrase in (
            ("lcdc", "LCDC", "0-LCY", "Low cloud level"),
            ("mcdc", "MCDC", "0-MCY", "Middle cloud level"),
            ("hcdc", "HCDC", "0-HCY", "High cloud level"),
        ):
            self.assertTrue(_band_matches(layer, {"GRIB_ELEMENT": element, "GRIB_SHORT_NAME": short_name}, ""))
            self.assertTrue(_band_matches(layer, {"GRIB_ELEMENT": element}, f'0[-] {short_name[2:]}="{phrase}"'))
            self.assertFalse(_band_matches(layer, {"GRIB_ELEMENT": "TCDC", "GRIB_SHORT_NAME": short_name}, ""))
            self.assertFalse(_band_matches(layer, {"GRIB_ELEMENT": element, "GRIB_SHORT_NAME": "0-EATM"}, ""))
        self.assertTrue(_band_matches("gust", {"GRIB_ELEMENT": "GUST", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertTrue(_band_matches("gust", {"GRIB_ELEMENT": "GUST"}, '0[-] SFC="Ground or water surface"'))
        # ECMWF's interval-maximum gust sits on the 10 m surface.
        self.assertTrue(_band_matches("gust", {"GRIB_ELEMENT": "GUST", "GRIB_SHORT_NAME": "10-HTGL"}, ""))
        self.assertFalse(_band_matches("gust", {"GRIB_ELEMENT": "GUST", "GRIB_SHORT_NAME": "2-HTGL"}, ""))
        self.assertFalse(_band_matches("gust", {"GRIB_ELEMENT": "UGRD", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertFalse(_band_matches("gust", {"GRIB_ELEMENT": "UGRD", "GRIB_SHORT_NAME": "10-HTGL"}, ""))
        self.assertTrue(_band_matches("cape", {"GRIB_ELEMENT": "CAPE", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertFalse(_band_matches("cape", {"GRIB_ELEMENT": "CAPE", "GRIB_SHORT_NAME": "18000-0-SPDL"}, ""))
        # ECMWF's most-unstable CAPE departs from surface type 17, which GDAL
        # gives no short name and describes by its name.
        self.assertTrue(
            _band_matches(
                "cape",
                {"GRIB_ELEMENT": "CAPE", "GRIB_SHORT_NAME": "0-"},
                '0[-] ="Departure level of the most unstable parcel of air (MUDL)"',
            )
        )
        self.assertFalse(_band_matches("cape", {"GRIB_ELEMENT": "CAPE", "GRIB_SHORT_NAME": "0-"}, ""))
        self.assertTrue(_band_matches("tcdc", {"GRIB_ELEMENT": "TCDC", "GRIB_SHORT_NAME": "0-EATM"}, ""))
        self.assertTrue(_band_matches("tcdc", {"GRIB_ELEMENT": "TCDC"}, '0[-] EATM="Entire atmosphere"'))
        self.assertFalse(_band_matches("tcdc", {"GRIB_ELEMENT": "TCDC", "GRIB_SHORT_NAME": "0-LCY"}, "low cloud layer"))
        self.assertFalse(_band_matches("tcdc", {"GRIB_ELEMENT": "CAPE", "GRIB_SHORT_NAME": "0-EATM"}, ""))
        # ECMWF's tcc is the local 0/6/192 GDAL does not know, at the surface.
        ecmwf_tcc = {"GRIB_ELEMENT": "unknown", "GRIB_SHORT_NAME": "0-SFC", "GRIB_COMMENT": "(prodType 0, cat 6, subcat 192) [-]"}
        self.assertTrue(_band_matches("tcdc", ecmwf_tcc, '0[-] SFC="Ground or water surface"'))
        self.assertFalse(_band_matches("tcdc", {**ecmwf_tcc, "GRIB_COMMENT": "(prodType 0, cat 1, subcat 193) [-]"}, ""))
        self.assertFalse(_band_matches("tp", ecmwf_tcc, '0[-] SFC="Ground or water surface"'))
        self.assertEqual(raster_expression("tcdc", "-"), "maximum(0,minimum(100,A*100))")
        self.assertEqual(raster_expression("tcdc", "%"), "maximum(0,minimum(100,A))")

    def test_units_are_accepted_as_gdal_reports_them(self) -> None:
        for variable_id in SURFACE_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            with self.subTest(variable=variable_id):
                raster_expression(variable_id, spec.gdal_unit)
                raster_expression(variable_id, f"[{spec.gdal_unit}]")
        self.assertEqual(raster_expression("gust", "[m/s]"), "maximum(0,minimum(127,A))")
        self.assertEqual(raster_expression("tcdc", "[%]"), "maximum(0,minimum(100,A))")
        self.assertEqual(raster_expression("cape", "[J/kg]"), "maximum(0,minimum(6350,A))")
        self.assertEqual(raster_expression("vis", "[m]"), "A/1000")
        self.assertEqual(raster_expression("dpt2m", "[C]"), "maximum(-70,minimum(40,A))")
        self.assertEqual(raster_expression("dpt2m", "K"), "maximum(-70,minimum(40,A-273.15))")
        self.assertEqual(raster_expression("aptmp2m", "[C]"), "maximum(-90,minimum(60,A))")
        self.assertEqual(raster_expression("lcdc", "[%]"), raster_expression("tcdc", "[%]"))
        for variable_id, unit in (("gust", "km/h"), ("tcdc", "kg/kg"), ("cape", "m^2/s^2"), ("vis", "km"), ("dpt2m", "%")):
            with self.assertRaises(ConversionError):
                raster_expression(variable_id, unit)


if __name__ == "__main__":
    unittest.main()

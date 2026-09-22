"""The surface diagnostics: wind gust, total / low / middle / high cloud
cover, convective available potential energy and inhibition, visibility, 2 m
dew point and apparent temperature, precipitable water, planetary boundary
layer height and the categorical precipitation type.

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

import numpy as np

from xuebuild.binconvert import (
    PTYPE_CODES,
    VECTOR_BUNDLES,
    VIDEO_VARIABLE_IDS,
    derive_ptype,
    published_bundle_ids,
)
from xuebuild.errors import ConversionError
from xuebuild.gdal import _band_matches, raster_expression
from xuebuild.grib2 import MessageInfo, _matches
from xuebuild.quantize import PROFILES, SURFACE_VARIABLE_IDS
from xuebuild.sources import source_spec
from xuebuild.variables import variable_spec

REGISTRY = Path(__file__).resolve().parent / "fixtures" / "surface-registry.json"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
CLOUD_COVER_IDS = ("tcdc", "lcdc", "mcdc", "hcdc")
# Fields whose codebook is not one-sided: a temperature runs below zero (the
# dew point, the apparent temperature) and convective inhibition is the
# negative half of CAPE.
NEGATIVE_OFFSET_IDS = ("dpt2m", "aptmp2m", "cin")
# The one categorical codebook: integer class codes with no error budget, the
# same book in every profile.
CATEGORICAL_IDS = ("ptype",)


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
                if variable_id not in NEGATIVE_OFFSET_IDS:
                    self.assertEqual(
                        quality.minimum, 0.0, "one-sided, like every quantity but a temperature and CIN"
                    )
                self.assertEqual(compact.minimum, quality.minimum)
                self.assertEqual(compact.maximum, quality.maximum)
                if variable_id in CATEGORICAL_IDS:
                    # The one class codebook: the same book in every profile,
                    # since there is no resolution to trade.
                    self.assertEqual(compact.step, quality.step)
                    self.assertEqual(quality.maximum_code, 8)
                    continue
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
        # Precipitable water sits on NCEP's local "entire atmosphere
        # (considered as a single layer)" surface (type 200), which carries
        # no value like the WMO type 10 the total cloud cover uses; the
        # inhibition and the boundary layer height on the ground surface,
        # the latter under NCEP's local 0/3/196 rather than the WMO 0/3/18.
        pwat = variable_spec("pwat")
        self.assertEqual(
            (pwat.grib2_category, pwat.grib2_number, pwat.grib2_level_type, pwat.grib2_level_value),
            (1, 3, 200, None),
        )
        self.assertEqual(pwat.index_field, ":PWAT:entire atmosphere (considered as a single layer):")
        cin = variable_spec("cin")
        self.assertEqual((cin.grib2_category, cin.grib2_number, cin.grib2_level_type), (7, 7, 1))
        hpbl = variable_spec("hpbl")
        self.assertEqual((hpbl.grib2_category, hpbl.grib2_number, hpbl.grib2_level_type), (3, 196, 1))
        # The four categorical flags and the derived type they combine into.
        for flag, number in (("crain", 192), ("cfrzr", 193), ("cicep", 194), ("csnow", 195)):
            spec = variable_spec(flag)
            self.assertEqual((spec.grib2_category, spec.grib2_number, spec.grib2_level_type), (1, number, 1))
            self.assertEqual(spec.index_field, f":{spec.grib_element}:surface:")
            # pgrb2 carries an interval average of each flag beside the
            # instantaneous record from f001 on; the fetch must take the
            # instantaneous one, as prate and the cloud covers do.
            self.assertEqual(spec.excluded_index_phrases, ("ave fcst",))
        ptype = variable_spec("ptype")
        self.assertEqual((ptype.grib2_category, ptype.grib2_number, ptype.grib2_level_type), (1, 19, 1))
        self.assertEqual(ptype.grib_element, "", "the type is derived, not a record")
        # ECMWF open data carries the dew point under the same identity, and
        # the gust, the total cloud cover and the CAPE as neighbours accepted
        # through a whole alternate identity each; the layer cloud covers,
        # the visibility and the apparent temperature IFS open data does not
        # carry. AIFS open data carries the layers (``lcc`` / ``mcc`` /
        # ``hcc``) on surfaces of its own and the total under the WMO 0/6/1
        # from the ground surface up, each an alternate too.
        ecmwf_params = {"dpt2m": "2d", "gust": "10fg", "tcdc": "tcc", "cape": "mucape", "lcdc": "lcc", "mcdc": "mcc", "hcdc": "hcc"}
        with_alternates = ("gust", "tcdc", "cape", "lcdc", "mcdc", "hcdc")
        for variable_id in SURFACE_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            self.assertIsNone(spec.grib2_statistical, f"{variable_id} is an instantaneous product")
            self.assertEqual(spec.ecmwf_param, ecmwf_params.get(variable_id, ""), variable_id)
            self.assertEqual(bool(spec.grib2_alternates), variable_id in with_alternates, variable_id)
        self.assertEqual(variable_spec("gust").ecmwf_alternate_params, ("10fg3",))
        gust, tcdc, cape = (variable_spec(variable_id).grib2_alternates[0] for variable_id in ("gust", "tcdc", "cape"))
        self.assertEqual((gust.triple, gust.level_type, gust.level_value, gust.statistical), ((0, 2, 22), 103, 10.0, 2))
        self.assertEqual((tcdc.triple, tcdc.level_type, tcdc.level_value, tcdc.gdal_unit), ((0, 6, 192), 1, None, "-"))
        self.assertEqual((cape.triple, cape.level_type, cape.level_value), ((0, 7, 6), 17, None))
        aifs_tcdc = variable_spec("tcdc").grib2_alternates[1]
        self.assertEqual((aifs_tcdc.triple, aifs_tcdc.level_type, aifs_tcdc.level_value, aifs_tcdc.gdal_unit), ((0, 6, 1), 1, None, ""))
        for layer, number, surface, value in (("lcdc", 3, 1, None), ("mcdc", 4, 100, 80000.0), ("hcdc", 5, 100, 45000.0)):
            (alternate,) = variable_spec(layer).grib2_alternates
            self.assertEqual((alternate.triple, alternate.level_type, alternate.level_value, alternate.statistical), ((0, 6, number), surface, value, None))

    def test_gfs_publishes_them_all_and_ecmwf_what_the_open_data_carries(self) -> None:
        gfs = published_bundle_ids(source_spec("gfs"))
        for variable_id in SURFACE_VARIABLE_IDS:
            self.assertIn(variable_id, gfs)
            self.assertNotIn(variable_id, VECTOR_BUNDLES)
            self.assertNotIn(variable_id, VIDEO_VARIABLE_IDS)
        self.assertIn("wind100m", gfs)
        ecmwf = published_bundle_ids(source_spec("ecmwf"))
        for variable_id in SURFACE_VARIABLE_IDS:
            self.assertEqual(variable_id in ecmwf, variable_id in ("gust", "tcdc", "cape", "dpt2m"), variable_id)
        for source in (source_spec("sflux"), source_spec("cma")):
            published = published_bundle_ids(source)
            for variable_id in SURFACE_VARIABLE_IDS:
                self.assertNotIn(variable_id, published, source.id)


class PrecipitationTypeTests(unittest.TestCase):
    """The derived categorical field: four 0/1 flags into WMO code table
    4.201's own values, in the fixed precedence both encoders share."""

    def _flags(self, *, crain: float = 0.0, cfrzr: float = 0.0, cicep: float = 0.0, csnow: float = 0.0) -> dict:
        return {
            "crain": np.array([[crain]]),
            "cfrzr": np.array([[cfrzr]]),
            "cicep": np.array([[cicep]]),
            "csnow": np.array([[csnow]]),
        }

    def test_each_flag_maps_to_its_code_table_value(self) -> None:
        for variable_id, expected in (("crain", 1.0), ("cfrzr", 3.0), ("cicep", 8.0), ("csnow", 5.0)):
            with self.subTest(flag=variable_id):
                codes = derive_ptype(self._flags(**{variable_id: 1.0}), "ptype")
                np.testing.assert_array_equal(codes, [[expected]])

    def test_no_flag_is_no_precipitation(self) -> None:
        np.testing.assert_array_equal(derive_ptype(self._flags(), "ptype"), [[0.0]])

    def test_snow_wins_a_point_snow_ice_pellets_freezing_rain_and_rain_all_claim(self) -> None:
        # The flags are mutually exclusive in the model's own output; this
        # pins the defensive precedence the two encoders must share.
        codes = derive_ptype(self._flags(crain=1.0, cfrzr=1.0, cicep=1.0, csnow=1.0), "ptype")
        np.testing.assert_array_equal(codes, [[5.0]])
        self.assertEqual([code for _, code in PTYPE_CODES], [1.0, 3.0, 8.0, 5.0])


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
        # Precipitable water on NCEP's local type 200; CIN rejects the
        # mixed-layer variants on the "pressure difference from ground"
        # surface; the flags and the 100 m wind pair are their own records.
        self.assertTrue(_matches(variable_spec("pwat"), message(1, 3, 200, 0.0, None)))
        self.assertFalse(_matches(variable_spec("pwat"), message(1, 3, 1, 0.0, None)), "the total cloud cover's surface")
        self.assertTrue(_matches(variable_spec("cin"), message(7, 7, 1, 0.0, None)))
        self.assertFalse(_matches(variable_spec("cin"), message(7, 7, 108, 18000.0, None)), "the 180–0 mb CIN")
        self.assertTrue(_matches(variable_spec("hpbl"), message(3, 196, 1, 0.0, None)))
        for flag, number in (("crain", 192), ("cfrzr", 193), ("cicep", 194), ("csnow", 195)):
            self.assertTrue(_matches(variable_spec(flag), message(1, number, 1, 0.0, None)))
        self.assertTrue(_matches(variable_spec("ugrd100m"), message(2, 2, 103, 100.0, None)))
        self.assertFalse(_matches(variable_spec("ugrd10m"), message(2, 2, 103, 100.0, None)), "the 100 m wind")

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
        # The new surface diagnostics: the precipitable water on the entire
        # atmosphere (GDAL spells type 200 like the WMO type 10), the
        # inhibition and boundary layer height on the ground surface, the
        # categorical flags, and the 100 m wind pair (which the 10 m matcher
        # must not accept and vice versa).
        self.assertTrue(_band_matches("pwat", {"GRIB_ELEMENT": "PWAT", "GRIB_SHORT_NAME": "0-EATM"}, ""))
        self.assertTrue(_band_matches("pwat", {"GRIB_ELEMENT": "PWAT"}, '0[-] EATM="Entire atmosphere (considered as a single layer)"'))
        self.assertFalse(_band_matches("pwat", {"GRIB_ELEMENT": "TCDC", "GRIB_SHORT_NAME": "0-EATM"}, ""))
        self.assertTrue(_band_matches("cin", {"GRIB_ELEMENT": "CIN", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertTrue(_band_matches("hpbl", {"GRIB_ELEMENT": "HPBL", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertTrue(_band_matches("csnow", {"GRIB_ELEMENT": "CSNOW", "GRIB_SHORT_NAME": "0-SFC"}, ""))
        self.assertTrue(_band_matches("crain", {"GRIB_ELEMENT": "CRAIN"}, '0[-] SFC="Ground or water surface"'))
        self.assertTrue(_band_matches("ugrd100m", {"GRIB_ELEMENT": "UGRD", "GRIB_SHORT_NAME": "100-HTGL"}, ""))
        self.assertTrue(_band_matches("ugrd100m", {"GRIB_ELEMENT": "UGRD"}, "100[Pa] HTGL=100 m above ground"))
        self.assertFalse(_band_matches("ugrd100m", {"GRIB_ELEMENT": "UGRD", "GRIB_SHORT_NAME": "10-HTGL"}, ""))
        self.assertFalse(_band_matches("ugrd10m", {"GRIB_ELEMENT": "UGRD", "GRIB_SHORT_NAME": "100-HTGL"}, ""))
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
        self.assertEqual(raster_expression("cin", "[J/kg]"), "maximum(-1016,minimum(0,A))")
        self.assertEqual(raster_expression("pwat", "[kg/(m^2)]"), "maximum(0,minimum(127,A))")
        self.assertEqual(raster_expression("pwat", "kg m**-2"), "maximum(0,minimum(127,A))")
        self.assertEqual(raster_expression("hpbl", "[m]"), "maximum(0,minimum(5080,A))")
        self.assertEqual(raster_expression("ptype", "[1]"), "maximum(0,minimum(8,A))")
        self.assertEqual(raster_expression("crain", "(Code table 4.222)"), "maximum(0,minimum(1,A))")
        for variable_id, unit in (("gust", "km/h"), ("tcdc", "kg/kg"), ("cape", "m^2/s^2"), ("vis", "km"), ("dpt2m", "%")):
            with self.assertRaises(ConversionError):
                raster_expression(variable_id, unit)


if __name__ == "__main__":
    unittest.main()

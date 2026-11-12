"""The GDAL band-matching rules of the registry document (docs/registry.md
§match): one case per rule, on synthetic band metadata as GDAL describes
it, through the evaluator in ``gdal.py``."""

from __future__ import annotations

import unittest

from xuebuild import rules
from xuebuild.gdal import _band_matches, raster_expression, rule_matches
from xuebuild.variables import variable_spec


def band(**metadata: str) -> dict[str, str]:
    return dict(metadata)


class SurfaceRuleTests(unittest.TestCase):
    def test_one_of_the_elements_on_the_sfc_surface(self) -> None:
        rule = rules.surface("TMP", "SKINT")
        spec = variable_spec("tmpsfc")
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="TMP", GRIB_SHORT_NAME="0-SFC"), ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="SKINT", GRIB_SHORT_NAME="0-SFC"), ""))
        # The GFS-Wave surface value of 1 comes out 1-SFC.
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="TMP", GRIB_SHORT_NAME="1-SFC"), ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="TMP"), '0[-] SFC="Ground or water surface"'))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="tmp", GRIB_LEVEL="ground or water surface"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="DPT", GRIB_SHORT_NAME="0-SFC"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="TMP", GRIB_SHORT_NAME="2-HTGL"), ""))
        # "Isobaric surface" is a surface in prose, not the ground.
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="TMP", GRIB_SHORT_NAME="85000-ISBL"), 'ISBL="Isobaric surface"'))


class HeightRuleTests(unittest.TestCase):
    def test_the_element_at_its_height_above_ground(self) -> None:
        rule = rules.height("UGRD", 80)
        spec = variable_spec("ugrd80m")
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="UGRD", GRIB_SHORT_NAME="80-HTGL"), ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="UGRD", GRIB_SHORT_NAME="80-M-HTGL"), ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="UGRD"), "80[m] HTGL=80 m above ground"))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="UGRD", GRIB_COMMENT="80.0 meters above ground"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="UGRD", GRIB_SHORT_NAME="100-HTGL"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="UGRD"), "180 m above ground"))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="VGRD", GRIB_SHORT_NAME="80-HTGL"), ""))
        two = rules.height("TMP", 2)
        self.assertTrue(rule_matches(two, variable_spec("tmp2m"), band(GRIB_ELEMENT="TMP", GRIB_SHORT_NAME="2-HTGL"), ""))
        self.assertFalse(rule_matches(two, variable_spec("tmp2m"), band(GRIB_ELEMENT="TMP"), "12 m above ground"))


class EntireAtmosphereRuleTests(unittest.TestCase):
    def test_the_element_on_the_entire_atmosphere(self) -> None:
        rule = rules.entire_atmosphere("PWAT")
        spec = variable_spec("pwat")
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="PWAT", GRIB_SHORT_NAME="0-EATM"), ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="PWAT"), 'EATM="Entire atmosphere (considered as a single layer)"'))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="TCDC", GRIB_SHORT_NAME="0-EATM"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="PWAT", GRIB_SHORT_NAME="0-SFC"), ""))


class MeanSeaLevelRuleTests(unittest.TestCase):
    def test_one_of_the_elements_on_mean_sea_level(self) -> None:
        rule = rules.mean_sea_level("PRMSL", "PRES", "MSLMA")
        spec = variable_spec("prmsl")
        for element in ("PRMSL", "PRES", "MSLMA"):
            self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT=element, GRIB_SHORT_NAME="0-MSL"), ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="PRMSL"), '0[-] MSL="Mean sea level"'))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="MSLET", GRIB_SHORT_NAME="0-MSL"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="PRES", GRIB_SHORT_NAME="0-SFC"), ""))


class IsobaricRuleTests(unittest.TestCase):
    def test_the_element_on_this_level_and_no_other(self) -> None:
        rule = rules.isobaric("HGT", 500)
        spec = variable_spec("hgt500")
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="HGT", GRIB_SHORT_NAME="50000-ISBL"), ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="HGT", GRIB_SHORT_NAME="500-ISBL"), ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="HGT"), '50000[Pa] ISBL="Isobaric surface"'))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="HGT"), "HGT at 500 mb"))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="HGT"), "500 hPa"))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="HGT", GRIB_SHORT_NAME="100000-ISBL"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="HGT"), "HGT at 1500 mb"))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="HGT"), '150000[Pa] ISBL="Isobaric surface"'))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="TMP", GRIB_SHORT_NAME="50000-ISBL"), ""))


class CloudLayerRuleTests(unittest.TestCase):
    def test_the_element_on_its_layer_or_named_as_the_layer(self) -> None:
        rule = rules.cloud_layer("MCDC", "0-MCY", "middle cloud", "medium cloud")
        spec = variable_spec("mcdc")
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="MCDC", GRIB_SHORT_NAME="0-MCY"), ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="MCDC", GRIB_SHORT_NAME="80000-45000-ISBL", GRIB_COMMENT="Medium cloud cover [%]"), ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="MCDC"), 'MCY="Middle cloud layer"'))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="MCDC", GRIB_SHORT_NAME="0-EATM"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="TCDC", GRIB_SHORT_NAME="0-MCY"), ""))


class TemplateRuleTests(unittest.TestCase):
    @staticmethod
    def soil(discipline: str, assembled: str, template: str = "0") -> dict[str, str]:
        return band(GRIB_ELEMENT="TSOIL", GRIB_DISCIPLINE=discipline, GRIB_PDS_PDTN=template, GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES=assembled)

    def test_the_assembled_template_against_the_identity_or_an_alternate(self) -> None:
        rule = rules.template()
        gfs_tsoil2 = self.soil("2(Land_Surface)", "0 2 2 0 81 0 0 1 0 106 2 10 106 2 40")
        ecmwf_sot1 = self.soil("2(Land_Surface)", "3 18 2 255 161 0 0 1 0 151 0 0 151 0 1")
        self.assertTrue(rule_matches(rule, variable_spec("tsoil2"), gfs_tsoil2, ""))
        self.assertFalse(rule_matches(rule, variable_spec("tsoil1"), gfs_tsoil2, ""))
        self.assertTrue(rule_matches(rule, variable_spec("tsoil1"), ecmwf_sot1, ""))
        self.assertFalse(rule_matches(rule, variable_spec("tsoil2"), ecmwf_sot1, ""))
        # Another discipline, another template, no template at all.
        self.assertFalse(rule_matches(rule, variable_spec("tsoil2"), self.soil("0(Meteorological)", "0 2 2 0 81 0 0 1 0 106 2 10 106 2 40"), ""))
        self.assertFalse(rule_matches(rule, variable_spec("tsoil2"), self.soil("2(Land_Surface)", "0 2 2 0 81 0 0 1 0 106 2 10 106 2 40", "8"), ""))
        self.assertFalse(rule_matches(rule, variable_spec("tsoil2"), band(GRIB_ELEMENT="TSOIL", GRIB_SHORT_NAME="0.1-0.4-DBLL"), ""))


class TemplateAerosolRuleTests(unittest.TestCase):
    def test_template_4_48_s_parameter_and_aerosol_identity_whole(self) -> None:
        rule = rules.template_aerosol()
        dust = band(
            GRIB_ELEMENT="AOTK",
            GRIB_DISCIPLINE="0(Meteorological)",
            GRIB_PDS_PDTN="48",
            GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES="20 102 62001 0 6 20 0 0 7 9 545 9 565 2 0 96 0 0 1 3 10 0 0 255 0 0",
        )
        self.assertTrue(rule_matches(rule, variable_spec("aoddust"), dust, ""))
        self.assertFalse(rule_matches(rule, variable_spec("aod"), dust, ""))
        self.assertFalse(rule_matches(rule, variable_spec("pm10dust"), dust, ""))
        self.assertFalse(rule_matches(rule, variable_spec("aoddust"), {**dust, "GRIB_PDS_PDTN": "0"}, ""))
        self.assertFalse(rule_matches(rule, variable_spec("aoddust"), {k: v for k, v in dust.items() if k != "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES"}, ""))
        # A variable with no aerosol block matches no aerosol record.
        self.assertFalse(rule_matches(rule, variable_spec("tmp2m"), dust, ""))


class TemplateEnsembleRuleTests(unittest.TestCase):
    """CMA GRAPES GEPS's products, with the assembled values GDAL 3.13
    reports for them (one record of each template the source reads)."""

    METEOROLOGICAL = "0(Meteorological)"
    HGT_MEAN = "3 5 4 128 128 0 0 1 24 100 0 50000 255 -127 -2147483647 0 31"
    HGT_SPREAD = "3 5 4 128 128 0 0 1 24 100 0 50000 255 -127 -2147483647 4 31"
    PRMSL_MEAN = "3 1 4 128 128 0 0 1 24 101 -127 -2147483647 255 -127 -2147483647 0 31"
    TMP2M_Q25 = "0 0 4 128 128 0 0 1 24 103 0 2 255 -127 -2147483647 25"
    APCP_Q50_6H = "1 8 4 128 128 0 0 1 18 1 -127 -2147483647 255 -127 -2147483647 50 2026 11 11 12 0 0 1 0 1 2 1 6 255 0"
    APCP_Q50_24H = "1 8 4 128 128 0 0 1 0 1 -127 -2147483647 255 -127 -2147483647 50 2026 11 11 12 0 0 1 0 1 2 1 24 255 0"
    # CMA writes the threshold into both limits; type 3 reads the lower.
    PROB_10MM_24H = (
        "1 8 4 128 128 0 0 1 0 1 -127 -2147483647 255 -127 -2147483647 3 6 3 2 1000 2 1000 2026 11 11 12 0 0 1 0 1 2 1 24 255 0"
    )
    PROB_10MM_6H = (
        "1 8 4 128 128 0 0 1 18 1 -127 -2147483647 255 -127 -2147483647 3 5 3 2 1000 2 1000 2026 11 11 12 0 0 1 0 1 2 1 6 255 0"
    )

    @classmethod
    def product(cls, template: int, assembled: str, element: str = "HGT", short_name: str = "50000-ISBL") -> dict[str, str]:
        return band(
            GRIB_ELEMENT=element,
            GRIB_SHORT_NAME=short_name,
            GRIB_DISCIPLINE=cls.METEOROLOGICAL,
            GRIB_PDS_PDTN=str(template),
            GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES=assembled,
        )

    def test_the_mean_and_the_spread_are_told_apart_by_the_template(self) -> None:
        mean, spread = self.product(2, self.HGT_MEAN), self.product(2, self.HGT_SPREAD)
        self.assertTrue(_band_matches("hgt500", mean, ""))
        self.assertFalse(_band_matches("hgt500", spread, ""))
        self.assertTrue(_band_matches("hgt500sd", spread, ""))
        self.assertFalse(_band_matches("hgt500sd", mean, ""))
        # The isobaric rule alone would take both: an ensemble template is
        # read by a template-ensemble rule or by none.
        self.assertFalse(rule_matches(rules.isobaric("HGT", 500), variable_spec("hgt500"), mean, ""))
        self.assertFalse(_band_matches("hgt850", mean, ""))
        prmsl = self.product(2, self.PRMSL_MEAN, "PRMSL", "0-MSL")
        self.assertTrue(_band_matches("prmsl", prmsl, ""))
        self.assertFalse(_band_matches("prmslsd", prmsl, ""))

    def test_a_percentile_and_its_interval(self) -> None:
        q25 = self.product(6, self.TMP2M_Q25, "TMP25", "2-HTGL")
        self.assertTrue(_band_matches("tmp2mq25", q25, ""))
        self.assertFalse(_band_matches("tmp2mq75", q25, ""))
        self.assertFalse(_band_matches("tmp2m", q25, ""))
        six = self.product(10, self.APCP_Q50_6H, "APCP50", "0-SFC")
        day = self.product(10, self.APCP_Q50_24H, "APCP50", "0-SFC")
        self.assertTrue(_band_matches("apcp", six, ""))
        # The run-long window beside it is the same percentile of the same
        # parameter; its length is what refuses it.
        self.assertFalse(_band_matches("apcp", day, ""))
        self.assertFalse(_band_matches("apcpq25", six, ""))
        # An instantaneous percentile's template is not the interval's.
        self.assertFalse(_band_matches("apcp", {**six, "GRIB_PDS_PDTN": "6"}, ""))

    def test_a_probability_reads_the_limit_its_type_uses(self) -> None:
        day = self.product(9, self.PROB_10MM_24H, "ProbAPCP24", "0-SFC")
        self.assertTrue(_band_matches("apcp24gt10", day, ""))
        self.assertFalse(_band_matches("apcp24gt5", day, ""))
        self.assertFalse(_band_matches("apcp24gt10", self.product(9, self.PROB_10MM_6H, "ProbAPCP06", "0-SFC"), ""))
        self.assertFalse(_band_matches("apcp", day, ""))
        # Unassembled, or assembled short, is no record.
        self.assertFalse(_band_matches("apcp24gt10", {k: v for k, v in day.items() if k != "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES"}, ""))
        self.assertFalse(_band_matches("apcp24gt10", {**day, "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": self.PROB_10MM_24H.rsplit(" ", 4)[0]}, ""))

    def test_a_single_forecast_is_not_an_ensemble_product(self) -> None:
        plain = self.product(0, "0 0 2 0 96 0 0 1 24 103 0 2 255 0 0", "TMP", "2-HTGL")
        self.assertTrue(_band_matches("tmp2m", plain, ""))
        self.assertFalse(_band_matches("tmp2mq25", plain, ""))


class TemplateRankRuleTests(unittest.TestCase):
    @staticmethod
    def swell(rank: int, assembled: bool = True, number: int = 8) -> dict[str, str]:
        metadata = band(GRIB_ELEMENT="SWELL", GRIB_SHORT_NAME=f"{rank}-RESERVED(241)")
        if assembled:
            metadata |= band(
                GRIB_DISCIPLINE="10(Oceanographic_Products)",
                GRIB_PDS_PDTN="0",
                GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES=f"0 {number} 2 0 11 0 0 1 0 241 0 {rank} 255 0 0",
            )
        return metadata

    def test_the_rank_off_the_template_or_the_short_name(self) -> None:
        rule = rules.template_rank()
        spec = variable_spec("swhgt")
        for assembled in (True, False):
            self.assertTrue(rule_matches(rule, spec, self.swell(1, assembled), ""))
            self.assertFalse(rule_matches(rule, spec, self.swell(2, assembled), ""))
        self.assertFalse(rule_matches(rule, spec, self.swell(1, number=9), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="SWELL", GRIB_SHORT_NAME="1-SFC"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="WVHGT", GRIB_SHORT_NAME="1-RESERVED(241)"), ""))


class TemplateMrmsRuleTests(unittest.TestCase):
    def test_the_product_on_the_mrms_discipline_at_its_level(self) -> None:
        rule = rules.template_mrms()
        composite = band(GRIB_ELEMENT="MergedReflectivityQCComposite", GRIB_SHORT_NAME="500-GPML", GRIB_DISCIPLINE="209")
        rate = band(GRIB_ELEMENT="PrecipRate", GRIB_SHORT_NAME="0-GPML", GRIB_DISCIPLINE="209")
        level = band(
            GRIB_ELEMENT="MergedReflectivityQC",
            GRIB_SHORT_NAME="3000-GPML",
            GRIB_DISCIPLINE="209",
            GRIB_PDS_PDTN="0",
            GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES="9 0 8 0 97 0 0 0 0 102 0 3000 255 1 0",
        )
        self.assertTrue(rule_matches(rule, variable_spec("cref"), composite, ""))
        self.assertTrue(rule_matches(rule, variable_spec("prate"), rate, ""))
        self.assertFalse(rule_matches(rule, variable_spec("prate"), composite, ""))
        self.assertTrue(rule_matches(rule, variable_spec("refl3000"), level, ""))
        self.assertFalse(rule_matches(rule, variable_spec("refl2500"), level, ""))
        self.assertFalse(rule_matches(rule, variable_spec("refl3000"), {**level, "GRIB_DISCIPLINE": "0"}, ""))
        # The short name decides where the driver did not assemble the template.
        self.assertTrue(rule_matches(rule, variable_spec("refl3000"), band(GRIB_ELEMENT="MergedReflectivityQC", GRIB_SHORT_NAME="3000-GPML", GRIB_DISCIPLINE="209"), ""))
        # A variable with no MRMS product matches nothing.
        self.assertFalse(rule_matches(rule, variable_spec("tmp2m"), composite, ""))


class TotalPrecipitationRuleTests(unittest.TestCase):
    def test_the_local_parameter_or_the_tprate_record_on_the_surface(self) -> None:
        rule = rules.total_precipitation()
        spec = variable_spec("tp")
        ecmwf = band(GRIB_ELEMENT="unknown", GRIB_SHORT_NAME="0-SFC", GRIB_COMMENT="(prodType 0, cat 1, subcat 193) [-]")
        aifs = band(GRIB_ELEMENT="TPRATE", GRIB_SHORT_NAME="0-SFC", GRIB_COMMENT="Total precipitation rate [kg/(m^2*s)]")
        self.assertTrue(rule_matches(rule, spec, ecmwf, ""))
        self.assertTrue(rule_matches(rule, spec, aifs, ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="unknown", GRIB_COMMENT="(prodType 0, cat 1, subcat 193) [-]"), 'SFC="Ground or water surface"'))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="unknown", GRIB_SHORT_NAME="0-SFC", GRIB_COMMENT="(prodType 0, cat 6, subcat 192) [-]"), ""))
        self.assertFalse(rule_matches(rule, spec, {**aifs, "GRIB_SHORT_NAME": "0-EATM"}, ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="PRATE", GRIB_SHORT_NAME="0-SFC"), ""))


class LocalParameterRuleTests(unittest.TestCase):
    def test_the_unknown_element_with_the_triple_in_its_comment(self) -> None:
        rule = rules.local_parameter(6, 192)
        spec = variable_spec("tcdc")
        tcc = band(GRIB_ELEMENT="unknown", GRIB_SHORT_NAME="0-SFC", GRIB_COMMENT="(prodType 0, cat 6, subcat 192) [-]")
        self.assertTrue(rule_matches(rule, spec, tcc, ""))
        self.assertTrue(rule_matches(rule, spec, {**tcc, "GRIB_SHORT_NAME": ""}, '0[-] SFC="Ground or water surface"'))
        self.assertFalse(rule_matches(rule, spec, {**tcc, "GRIB_COMMENT": "(prodType 0, cat 1, subcat 193) [-]"}, ""))
        self.assertFalse(rule_matches(rule, spec, {**tcc, "GRIB_ELEMENT": "TCDC"}, ""))
        self.assertFalse(rule_matches(rule, spec, {**tcc, "GRIB_SHORT_NAME": "0-EATM"}, ""))


class PhraseRuleTests(unittest.TestCase):
    def test_the_element_described_by_one_of_the_phrases(self) -> None:
        rule = rules.phrase("CAPE", "most unstable", "mudl")
        spec = variable_spec("cape")
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="CAPE", GRIB_SHORT_NAME="0-"), '0[-] ="Level of the most unstable parcel"'))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="CAPE", GRIB_COMMENT="MUDL CAPE"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="CAPE", GRIB_SHORT_NAME="0-"), ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="CIN"), "most unstable"))


class GeopotentialSurfaceRuleTests(unittest.TestCase):
    def test_the_geopotential_on_the_ground_surface(self) -> None:
        rule = rules.geopotential_surface("GP", "Z")
        spec = variable_spec("orog")
        gp = band(GRIB_ELEMENT="GP", GRIB_SHORT_NAME="0-SFC", GRIB_COMMENT="Geopotential [(m^2)/(s^2)]")
        self.assertTrue(rule_matches(rule, spec, gp, ""))
        self.assertTrue(rule_matches(rule, spec, band(GRIB_ELEMENT="Z", GRIB_COMMENT="Geopotential"), 'SFC="Ground or water surface"'))
        self.assertFalse(rule_matches(rule, spec, {**gp, "GRIB_COMMENT": "Pressure [Pa]"}, ""))
        self.assertFalse(rule_matches(rule, spec, {**gp, "GRIB_SHORT_NAME": "50000-ISBL"}, ""))
        self.assertFalse(rule_matches(rule, spec, band(GRIB_ELEMENT="HGT", GRIB_SHORT_NAME="0-SFC", GRIB_COMMENT="Geopotential height"), ""))


class BandMatchingTests(unittest.TestCase):
    def test_a_band_is_the_record_when_any_rule_accepts_it(self) -> None:
        # gust: pgrb2's surface record or ECMWF's 10 m interval maximum.
        self.assertTrue(_band_matches("gust", band(GRIB_ELEMENT="GUST", GRIB_SHORT_NAME="0-SFC"), ""))
        self.assertTrue(_band_matches("gust", band(GRIB_ELEMENT="GUST", GRIB_SHORT_NAME="10-HTGL"), ""))
        self.assertFalse(_band_matches("gust", band(GRIB_ELEMENT="GUST", GRIB_SHORT_NAME="2-HTGL"), ""))
        # orog: NCEP's HGT on the surface or ECMWF's geopotential.
        self.assertTrue(_band_matches("orog", band(GRIB_ELEMENT="HGT", GRIB_SHORT_NAME="0-SFC"), ""))
        self.assertTrue(_band_matches("orog", band(GRIB_ELEMENT="GP", GRIB_SHORT_NAME="0-SFC", GRIB_COMMENT="Geopotential"), ""))
        self.assertFalse(_band_matches("orog", band(GRIB_ELEMENT="HGT", GRIB_SHORT_NAME="50000-ISBL"), ""))

    def test_a_field_no_band_carries_matches_nothing(self) -> None:
        for variable_id in ("ptype", "thetae850", "uqflx850", "ir104", "clw250"):
            with self.subTest(variable=variable_id):
                self.assertEqual(variable_spec(variable_id).gdal_rules, ())
                self.assertFalse(_band_matches(variable_id, band(GRIB_ELEMENT="APCP", GRIB_SHORT_NAME="0-SFC"), ""))

    def test_the_default_rules_follow_the_identity(self) -> None:
        self.assertEqual(variable_spec("vis").gdal_rules, (rules.surface("VIS"),))
        self.assertEqual(variable_spec("dpt2m").gdal_rules, (rules.height("DPT", 2),))
        self.assertEqual(variable_spec("pwat").gdal_rules, (rules.entire_atmosphere("PWAT"),))
        self.assertEqual(variable_spec("rh700").gdal_rules, (rules.isobaric("RH", 700),))
        self.assertEqual(variable_spec("soilw3").gdal_rules, (rules.template(),))
        self.assertEqual(variable_spec("swper").gdal_rules, (rules.template_rank(),))
        self.assertEqual(variable_spec("refl500").gdal_rules, (rules.template_mrms(),))
        self.assertEqual(variable_spec("pm25").gdal_rules, (rules.template_aerosol(),))

    def test_the_raster_expression_is_the_rule_s_arithmetic_alone(self) -> None:
        self.assertEqual(raster_expression(variable_spec("tmp2m"), "K"), "A-273.15")
        self.assertEqual(raster_expression(variable_spec("tmp2m"), "[C]"), "A")
        self.assertEqual(raster_expression(variable_spec("prate"), "[kg/(m^2 s)]"), "A*3600")
        self.assertEqual(raster_expression(variable_spec("tcdc"), "-"), "A*100")
        self.assertEqual(raster_expression(variable_spec("orog"), "(m^2)/(s^2)"), "A/9.80665")


if __name__ == "__main__":
    unittest.main()

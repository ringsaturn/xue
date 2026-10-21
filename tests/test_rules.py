"""The unit rules of the registry document (docs/registry.md §conversion):
each rule's accepted spellings, the spelling it refuses, the raster
expression the inspection paths compare and the arithmetic the converter
applies."""

from __future__ import annotations

import unittest

import numpy as np

from xuebuild import rules
from xuebuild.errors import ConversionError
from xuebuild.rules import Conversion, GdalRule, accepted_units, accepts, compact_unit, convert, expression, temperature_unit
from xuebuild.variables import VARIABLES


def converted(conversion: Conversion, unit: str, *values: float) -> list[float]:
    return list(convert(conversion, unit, np.array(values, dtype=np.float64)))


class CompactionTests(unittest.TestCase):
    def test_spellings_are_compared_after_compaction(self) -> None:
        self.assertEqual(compact_unit(" [kg/(m^2 s)] "), "kg/m^2s")
        self.assertEqual(compact_unit("W m**-2"), "wm-2")
        self.assertEqual(compact_unit("kg/m²"), "kg/m^2")
        self.assertEqual(compact_unit(""), "")

    def test_a_temperature_drops_the_degree_sign_and_the_word(self) -> None:
        self.assertEqual(temperature_unit("K"), "K")
        self.assertEqual(temperature_unit("[C]"), "C")
        self.assertEqual(temperature_unit("°C"), "C")
        self.assertEqual(temperature_unit("degrees Fahrenheit"), "F")
        self.assertEqual(temperature_unit("degF"), "F")
        with self.assertRaisesRegex(ConversionError, "unsupported temperature unit"):
            temperature_unit("rankine")


class ConversionRuleTests(unittest.TestCase):
    """One case per rule: the spellings it accepts and what each gives, a
    spelling it refuses, and the arithmetic on a plane."""

    def assertRule(
        self,
        conversion: Conversion,
        accepted: dict[str, str],
        refused: tuple[str, ...],
        arithmetic: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        for unit, expected in accepted.items():
            with self.subTest(unit=unit):
                self.assertTrue(accepts(conversion, unit))
                self.assertEqual(expression(conversion, unit), expected)
        for unit in refused:
            with self.subTest(refused=unit):
                self.assertFalse(accepts(conversion, unit))
                with self.assertRaises(ConversionError):
                    expression(conversion, unit)
        for unit, (value, result) in (arithmetic or {}).items():
            with self.subTest(arithmetic=unit):
                self.assertAlmostEqual(converted(conversion, unit, value)[0], result, places=9)

    def test_none_without_spellings_takes_any_unit(self) -> None:
        self.assertIsNone(accepted_units(rules.NONE))
        self.assertRule(rules.NONE, {"": "A", "K": "A", "anything": "A"}, ())
        self.assertEqual(converted(rules.NONE, "K", 1.5), [1.5])

    def test_none_with_spellings_is_identity_under_exactly_those(self) -> None:
        self.assertRule(rules.SPEED, {"m/s": "A", "[m/s]": "A", "m s-1": "A", "mps": "A"}, ("km/h", "kt"), {"m/s": (3.0, 3.0)})
        self.assertRule(rules.RADIATIVE_FLUX, {"W/(m^2)": "A", "W m-2": "A", "W/m²": "A"}, ("J/m^2",))
        self.assertRule(rules.ENERGY_PER_MASS, {"J/kg": "A", "J kg-1": "A"}, ("m^2/s^2",))
        self.assertRule(rules.COLUMN_MASS, {"kg/(m^2)": "A", "kg m**-2": "A", "mm": "A"}, ("m",))
        self.assertRule(rules.METRES, {"m": "A", "[m]": "A", "metres": "A"}, ("cm", "ft", "km"))
        self.assertRule(rules.SECONDS, {"s": "A", "[s]": "A", "seconds": "A"}, ("Hz",))
        self.assertRule(rules.REFLECTIVITY, {"dB": "A", "dBZ": "A"}, ("mm/hr", "mm"))
        self.assertRule(rules.VERTICAL_VELOCITY, {"Pa/s": "A", "Pa s-1": "A"}, ("m/s",))
        self.assertRule(rules.GEOPOTENTIAL_METRES, {"gpm": "A", "[gpm]": "A", "m": "A"}, ("dam", "m2/s2"))
        self.assertRule(rules.PERCENT, {"%": "A", "[%]": "A"}, ("kg/kg", "1"))
        self.assertRule(rules.DIMENSIONLESS, {"[Numeric]": "A", "Numeric": "A", "-": "A", "": "A"}, ("kg/m^3",))
        self.assertRule(rules.PARTICULATE, {"[10^-6g/m^3]": "A", "ug/m^3": "A", "µg/m³": "A"}, ("kg/m^3",))
        self.assertRule(rules.SOIL_MOISTURE, {"[Fraction]": "A", "m^3/m^3": "A", "Proportion": "A"}, ("%", "kg/m^2"))
        self.assertRule(rules.MASS_RATE, {"[kg/(m^2 s)]": "A", "kg m-2 s-1": "A"}, ("mm", "mm/h"))

    def test_celsius(self) -> None:
        self.assertRule(
            rules.CELSIUS,
            {"K": "A-273.15", "kelvin": "A-273.15", "[C]": "A", "degC": "A", "F": "(A-32)*5/9", "degF": "(A-32)*5/9"},
            ("rankine", "m"),
            {"K": (273.15, 0.0), "C": (20.0, 20.0), "F": (212.0, 100.0)},
        )
        # Fahrenheit is converted in the written order: subtract, times 5, over 9.
        self.assertEqual(converted(rules.CELSIUS, "F", 32.0, 212.0), [0.0, 100.0])

    def test_soil_temperature_is_celsius_after_the_converter_s_read_through(self) -> None:
        self.assertRule(rules.SOIL_TEMPERATURE, {"K": "A-273.15", "[C]": "A"}, ("m",), {"K": (300.0, 300.0 - 273.15)})

    def test_precipitation_rate(self) -> None:
        self.assertRule(
            rules.PRECIPITATION_RATE,
            {"[kg/(m^2 s)]": "A*3600", "kg m-2 s-1": "A*3600", "mm/hr": "A", "[mm/h]": "A"},
            ("mm", "m"),
            {"kg/(m^2 s)": (1.0 / 3600, 1.0), "mm/hr": (2.5, 2.5)},
        )

    def test_precipitation_accumulation(self) -> None:
        self.assertRule(
            rules.PRECIPITATION_ACCUMULATION,
            {"-": "A*1000", "m": "A*1000", "": "A*1000", "kg/(m^2*s)": "A", "kg m**-2": "A", "kg/m^2": "A", "mm": "A"},
            ("in", "mm/h"),
            {"-": (0.001, 1.0), "mm": (3.0, 3.0)},
        )

    def test_pascal_to_hectopascal(self) -> None:
        self.assertRule(rules.PASCAL_TO_HECTOPASCAL, {"Pa": "A/100", "[Pa]": "A/100"}, ("hPa", "mb"), {"Pa": (101325.0, 1013.25)})

    def test_metre_to_kilometre(self) -> None:
        self.assertRule(rules.METRE_TO_KILOMETRE, {"m": "A/1000", "metres": "A/1000"}, ("km", "ft"), {"m": (24135.0, 24.135)})

    def test_mass_ratio_to_g_per_kg(self) -> None:
        self.assertRule(rules.MASS_RATIO_TO_G_PER_KG, {"kg/kg": "A*1000", "[kg/kg]": "A*1000"}, ("g/kg", "%"), {"kg/kg": (0.012, 12.0)})

    def test_proportion_to_percent(self) -> None:
        self.assertRule(
            rules.PROPORTION_TO_PERCENT,
            {"Proportion": "A*100", "fraction": "A*100", "1": "A*100", "-": "A*100", "": "A*100"},
            ("%",),
            {"Proportion": (0.25, 25.0)},
        )

    def test_cloud_cover_is_percent_or_a_fraction_scaled_up(self) -> None:
        self.assertRule(
            rules.CLOUD_COVER,
            {"%": "A", "[%]": "A", "-": "A*100", "1": "A*100", "fraction": "A*100", "(0 - 1)": "A*100"},
            ("kg/kg", "octa"),
            {"%": (40.0, 40.0), "-": (0.4, 40.0)},
        )

    def test_degrees_true_is_reduced_modulo_360(self) -> None:
        self.assertRule(rules.DEGREES_TRUE, {"Degree true": "A", "[Degree true]": "A", "deg": "A", "degrees": "A"}, ("rad",))
        self.assertEqual(converted(rules.DEGREES_TRUE, "Degree true", 360.0, 45.0, 725.0), [0.0, 45.0, 5.0])

    def test_geopotential_to_metres(self) -> None:
        self.assertRule(
            rules.GEOPOTENTIAL_TO_METRES,
            {"gpm": "A", "m": "A", "(m^2)/(s^2)": "A/9.80665", "m**2 s**-2": "A/9.80665", "m²/s²": "A/9.80665"},
            ("ft", "dam"),
            {"gpm": (100.0, 100.0), "(m^2)/(s^2)": (980.665, 100.0)},
        )

    def test_code_table_accepts_the_table_s_own_name(self) -> None:
        flag = rules.code_table("4.222")
        self.assertRule(flag, {"0=no; 1=yes": "A", "(Code table 4.222)": "A", "1": "A", "-": "A", "": "A"}, ("4.201", "%"))
        self.assertEqual(accepted_units(flag), ("1", "-", "", "4.222"))
        self.assertEqual(converted(flag, "0=no; 1=yes", 1.0), [1.0])
        self.assertRule(rules.code_table("4.201"), {"[1]": "A", "4.201": "A"}, ("4.222",))


class DocumentShapeTests(unittest.TestCase):
    def test_a_conversion_writes_only_the_keys_it_has(self) -> None:
        self.assertEqual(rules.NONE.document(), {"rule": "none"})
        self.assertEqual(rules.SPEED.document(), {"rule": "none", "acceptedUnits": ["m/s", "m/sec", "ms-1", "ms^-1", "mps"]})
        self.assertEqual(rules.CELSIUS.document(), {"rule": "celsius"})
        self.assertEqual(rules.code_table("4.222").document(), {"rule": "code-table", "table": "4.222"})

    def test_a_conversion_is_one_of_the_closed_set(self) -> None:
        with self.assertRaises(ValueError):
            Conversion("fahrenheit")
        with self.assertRaises(ValueError):
            Conversion("celsius", accepted_units=("k",))
        with self.assertRaises(ValueError):
            Conversion("code-table")

    def test_a_gdal_rule_writes_its_own_parameters(self) -> None:
        self.assertEqual(rules.surface("TMP", "SKINT").document(), {"rule": "surface", "elements": ["TMP", "SKINT"]})
        self.assertEqual(rules.height("TMP", 2).document(), {"rule": "height", "element": "TMP", "metres": 2})
        self.assertEqual(rules.entire_atmosphere("TCDC").document(), {"rule": "entire-atmosphere", "element": "TCDC"})
        self.assertEqual(rules.mean_sea_level("PRMSL").document(), {"rule": "mean-sea-level", "elements": ["PRMSL"]})
        self.assertEqual(rules.isobaric("HGT", 500).document(), {"rule": "isobaric", "element": "HGT", "hpa": 500})
        self.assertEqual(
            rules.cloud_layer("LCDC", "0-LCY", "low cloud").document(),
            {"rule": "cloud-layer", "element": "LCDC", "shortName": "0-LCY", "phrases": ["low cloud"]},
        )
        self.assertEqual(rules.local_parameter(6, 192).document(), {"rule": "local-parameter", "category": 6, "number": 192})
        self.assertEqual(rules.phrase("CAPE", "mudl").document(), {"rule": "phrase", "element": "CAPE", "phrases": ["mudl"]})
        self.assertEqual(rules.geopotential_surface("GP").document(), {"rule": "geopotential-surface", "elements": ["GP"]})
        for rule in (rules.template(), rules.template_aerosol(), rules.template_rank(), rules.template_mrms(), rules.total_precipitation()):
            self.assertEqual(rule.document(), {"rule": rule.rule})
        with self.assertRaises(ValueError):
            GdalRule("surface-ish")

    def test_every_registered_variable_names_rules_out_of_the_closed_sets(self) -> None:
        for variable_id, spec in VARIABLES.items():
            with self.subTest(variable=variable_id):
                self.assertIn(spec.conversion.rule, rules.CONVERSION_RULES)
                for rule in spec.gdal_rules or ():
                    self.assertIn(rule.rule, rules.MATCH_RULES)
                self.assertIn(spec.predictor, rules.PREDICTORS)
                self.assertIn(spec.decimation, rules.DECIMATIONS)
                self.assertTrue(set(spec.companions) <= rules.COMPANIONS)

    def test_the_declared_gdal_unit_is_one_the_variable_s_rule_accepts(self) -> None:
        for variable_id, spec in VARIABLES.items():
            if not spec.gdal_rules:
                continue
            with self.subTest(variable=variable_id):
                expression(spec.conversion, spec.gdal_unit)
                for alternate in spec.grib2_alternates:
                    if alternate.gdal_unit:
                        expression(spec.conversion, alternate.gdal_unit)


if __name__ == "__main__":
    unittest.main()

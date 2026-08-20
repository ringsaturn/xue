import unittest

from xue.errors import ConversionError
from xue.gdal import celsius_expression, normalize_unit, precipitation_expression


class GdalTests(unittest.TestCase):
    def test_temperature_unit_normalization(self) -> None:
        self.assertEqual(normalize_unit("K"), "K")
        self.assertEqual(normalize_unit("[C]"), "C")
        self.assertEqual(normalize_unit("degrees Fahrenheit"), "F")
        self.assertIn("A-273.15", celsius_expression("kelvin"))
        self.assertIn("(A-32)*5/9", celsius_expression("degF"))

    def test_unsupported_unit_is_explicit(self) -> None:
        with self.assertRaisesRegex(ConversionError, "unsupported temperature unit"):
            normalize_unit("rankine")

    def test_precipitation_rate_converts_to_millimetres_per_hour(self) -> None:
        self.assertIn("A*3600", precipitation_expression("[kg/(m^2 s)]"))
        with self.assertRaisesRegex(ConversionError, "precipitation rate unit"):
            precipitation_expression("mm")


if __name__ == "__main__":
    unittest.main()

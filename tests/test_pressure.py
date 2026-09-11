"""The pressure family: sea level pressure and isobaric geopotential height.

Nine variables that add nothing to the container — no version bump, no new
metadata key — and everything to the registries: a GRIB2 identity, a linear
codebook, a record matcher, a bundle id. What holds them together across the
three implementations is `tests/fixtures/pressure-registry.json`, a committed
golden this module regenerates and compares; the Rust encoder's unit tests and
the frontend's vitest read the same file, so a number can only move in all
three at once.

The rule with no other home is **half-code alignment**: every standard
contour value must land exactly halfway between two codes. It is what keeps
the shader's `fract` test from lighting up a whole flat plateau where a
contour coincides with a code value, and nothing but a test states it — the
container neither knows nor cares.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from xuebuild.errors import ConversionError
from xuebuild.gdal import _band_matches, height_expression, pressure_expression, raster_expression
from xuebuild.quantize import (
    CONTOUR_INTERVALS,
    EMPHASIS_CONTOURS,
    EMPHASIS_INTERVALS,
    PRESSURE_VARIABLE_IDS,
    PROFILES,
)
from xuebuild.variables import HEIGHT_LEVELS_HPA, height_variable_id, variable_spec

REGISTRY = Path(__file__).resolve().parent / "fixtures" / "pressure-registry.json"


def registry_entry(variable_id: str) -> dict:
    """The registry as the three implementations must agree it is."""
    spec = variable_spec(variable_id)
    entry: dict = {
        "label": spec.label,
        "unit": spec.output_unit,
        "parameter": spec.parameter_metadata(),
        "contourInterval": CONTOUR_INTERVALS[variable_id],
        "quality": PROFILES["quality"][variable_id].metadata(),
        "compact": PROFILES["compact"][variable_id].metadata(),
    }
    if variable_id in EMPHASIS_INTERVALS:
        entry["emphasisInterval"] = EMPHASIS_INTERVALS[variable_id]
    if variable_id in EMPHASIS_CONTOURS:
        entry["emphasisContours"] = list(EMPHASIS_CONTOURS[variable_id])
    return entry


class RegistryTests(unittest.TestCase):
    def test_the_committed_registry_still_describes_this_encoder(self) -> None:
        expected = json.loads(REGISTRY.read_text(encoding="utf-8"))
        actual = {variable_id: registry_entry(variable_id) for variable_id in PRESSURE_VARIABLE_IDS}
        self.assertEqual(
            actual,
            expected,
            "the pressure registry moved; the Rust encoder and the frontend read the same "
            "fixture, so regenerate it deliberately and change all three",
        )

    def test_the_levels_are_registered_once_each_and_in_order(self) -> None:
        heights = [height_variable_id(level) for level in HEIGHT_LEVELS_HPA]
        self.assertEqual(list(PRESSURE_VARIABLE_IDS), ["prmsl", *heights])
        self.assertEqual(len(set(PRESSURE_VARIABLE_IDS)), len(PRESSURE_VARIABLE_IDS))

    def test_the_surface_pressure_is_stated_three_ways_and_they_agree(self) -> None:
        for level in HEIGHT_LEVELS_HPA:
            spec = variable_spec(height_variable_id(level))
            with self.subTest(level=level):
                self.assertEqual(spec.index_field, f":HGT:{level} mb:")
                self.assertEqual(spec.grib2_level_value, level * 100.0)
                parameter = spec.parameter_metadata()
                self.assertEqual(parameter["scaleFactorOfFirstFixedSurface"], 0)
                self.assertEqual(parameter["scaledValueOfFirstFixedSurface"], level * 100)


class CodebookTests(unittest.TestCase):
    def test_every_contour_lands_half_a_code_off(self) -> None:
        """The rule the whole contour rendering rests on.

        Checked over the codebook's entire span rather than at a couple of
        sample contours, plus the emphasised lines, in the quality profile
        production actually publishes."""
        for variable_id in PRESSURE_VARIABLE_IDS:
            codebook = PROFILES["quality"][variable_id]
            intervals = [CONTOUR_INTERVALS[variable_id]]
            if variable_id in EMPHASIS_INTERVALS:
                intervals.append(EMPHASIS_INTERVALS[variable_id])
            contours: list[float] = list(EMPHASIS_CONTOURS.get(variable_id, ()))
            for interval in intervals:
                first = int(np.ceil(codebook.minimum / interval))
                last = int(np.floor(codebook.maximum / interval))
                contours += [interval * step for step in range(first, last + 1)]
            self.assertGreater(len(contours), 8, variable_id)
            for contour in contours:
                offset = (contour - codebook.minimum) / codebook.step
                with self.subTest(variable=variable_id, contour=contour):
                    self.assertAlmostEqual(offset % 1.0, 0.5, places=9)

    def test_the_step_is_a_fraction_of_the_contour_interval(self) -> None:
        for variable_id in PRESSURE_VARIABLE_IDS:
            codebook = PROFILES["quality"][variable_id]
            ratio = CONTOUR_INTERVALS[variable_id] / codebook.step
            with self.subTest(variable=variable_id):
                self.assertAlmostEqual(ratio, round(ratio), places=9, msg="step must divide the interval")
                self.assertGreaterEqual(ratio, 3.0)

    def test_the_code_space_is_spent_and_the_profiles_cover_the_same_range(self) -> None:
        for variable_id in PRESSURE_VARIABLE_IDS:
            quality = PROFILES["quality"][variable_id]
            compact = PROFILES["compact"][variable_id]
            with self.subTest(variable=variable_id):
                self.assertEqual(quality.maximum_code, 254)
                self.assertEqual(compact.maximum_code, 127)
                self.assertEqual(compact.step, quality.step * 2)
                self.assertEqual(compact.maximum, quality.maximum)
                # balanced is the production profile and must be quality here.
                self.assertEqual(PROFILES["balanced"][variable_id], quality)

    def test_quantization_round_trips_inside_the_range_and_clamps_outside(self) -> None:
        for variable_id in PRESSURE_VARIABLE_IDS:
            codebook = PROFILES["quality"][variable_id]
            step = codebook.step
            values = np.array(
                [
                    codebook.minimum,
                    codebook.minimum + step / 2,  # round-half-up lands on code 1
                    (codebook.minimum + codebook.maximum) / 2,
                    codebook.maximum,
                    codebook.minimum - 10 * step,
                    codebook.maximum + 10 * step,
                ]
            )
            codes = codebook.quantize(values)
            with self.subTest(variable=variable_id):
                self.assertEqual(codes[0], 0)
                self.assertEqual(codes[1], 1)
                self.assertEqual(codes[3], 254)
                self.assertEqual(codes[4], 0, "below the range clamps")
                self.assertEqual(codes[5], 254, "above the range clamps")
                decoded = codebook.decode(codes[:4])
                self.assertLessEqual(float(np.abs(decoded - values[:4]).max()), step / 2 + 1e-9)

    def test_the_coverage_holds_the_measured_gfs_envelope(self) -> None:
        """Global minima and maxima measured over six extreme GFS f000
        analyses (a record Southern Ocean cyclone, two heat domes, a
        Siberian-high winter, a peak subtropical-high summer, one ordinary
        run). Both ends must stay inside the codebook: a run that clamps
        flattens the extreme core, and the Antarctic ice sheet's
        extrapolated heights are exactly where that would happen."""
        envelope = {
            "prmsl": (902.3, 1104.0),
            "hgt1000": (-807.0, 667.0),
            "hgt925": (-194.0, 1238.0),
            "hgt850": (464.0, 1879.0),
            "hgt700": (1963.0, 3309.0),
            "hgt500": (4335.0, 6008.0),
            "hgt300": (7631.0, 9852.0),
            "hgt250": (8777.0, 11135.0),
            "hgt200": (10156.0, 12643.0),
        }
        self.assertEqual(sorted(envelope), sorted(PRESSURE_VARIABLE_IDS))
        for variable_id, (low, high) in envelope.items():
            codebook = PROFILES["quality"][variable_id]
            with self.subTest(variable=variable_id):
                self.assertLess(codebook.minimum, low)
                self.assertGreater(codebook.maximum, high)


class RecordMatchingTests(unittest.TestCase):
    def test_pressure_converts_pascals_and_refuses_anything_else(self) -> None:
        self.assertEqual(pressure_expression("[Pa]"), "A/100")
        self.assertEqual(raster_expression("prmsl", "Pa"), "A/100")
        with self.assertRaisesRegex(ConversionError, "unsupported pressure unit"):
            pressure_expression("hPa")

    def test_height_takes_geopotential_metres_as_is(self) -> None:
        self.assertEqual(height_expression("[gpm]"), "A")
        self.assertEqual(raster_expression("hgt500", "m"), "A")
        with self.assertRaisesRegex(ConversionError, "geopotential height unit"):
            height_expression("dam")

    def test_the_declared_gdal_units_are_the_ones_the_matchers_accept(self) -> None:
        for variable_id in PRESSURE_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            with self.subTest(variable=variable_id):
                raster_expression(variable_id, spec.gdal_unit)

    def test_mean_sea_level_pressure_matches_its_own_record(self) -> None:
        self.assertTrue(
            _band_matches("prmsl", {"GRIB_ELEMENT": "PRMSL", "GRIB_SHORT_NAME": "0-MSL"}, "")
        )
        # MSLET is NCEP's other sea level reduction and a different quantity.
        self.assertFalse(
            _band_matches("prmsl", {"GRIB_ELEMENT": "MSLET", "GRIB_SHORT_NAME": "0-MSL"}, "")
        )

    def test_a_level_matches_only_itself(self) -> None:
        # Both spellings of the surface: pascals, which is what GRIB2 stores
        # and GDAL reports, and hectopascals, which is how the level is
        # written in an .idx phrase and in human-facing descriptions.
        for level in HEIGHT_LEVELS_HPA:
            variable_id = height_variable_id(level)
            for short_name in (f"{level}-ISBL", f"{level * 100}-ISBL"):
                metadata = {"GRIB_ELEMENT": "HGT", "GRIB_SHORT_NAME": short_name}
                with self.subTest(level=level, short_name=short_name):
                    self.assertTrue(_band_matches(variable_id, metadata, ""))
                    for other in HEIGHT_LEVELS_HPA:
                        if other != level:
                            self.assertFalse(_band_matches(height_variable_id(other), metadata, ""))

    def test_the_bands_a_real_gfs_file_reports_match(self) -> None:
        """The shape GDAL 3.x actually hands back for a pgrb2 HGT record.

        The level is in pascals in both the short name and the description,
        which no hectopascal-only matcher would have hit — every isobaric
        level would have been reported missing from a complete file."""
        for level in HEIGHT_LEVELS_HPA:
            metadata = {
                "GRIB_ELEMENT": "HGT",
                "GRIB_SHORT_NAME": f"{level * 100}-ISBL",
                "GRIB_COMMENT": "Geopotential height [gpm]",
                "GRIB_UNIT": "[gpm]",
            }
            description = f'{level * 100}[Pa] ISBL="Isobaric surface"'
            with self.subTest(level=level):
                self.assertTrue(_band_matches(height_variable_id(level), metadata, description))
                # The description alone, without the short name, is enough.
                self.assertTrue(
                    _band_matches(
                        height_variable_id(level), {"GRIB_ELEMENT": "HGT"}, description
                    )
                )

    def test_the_phrase_fallback_does_not_match_a_longer_number(self) -> None:
        # A driver that reports no ISBL short name still has to name the
        # level; "1500 mb" must not satisfy the 500 hPa matcher, and neither
        # must the 1000 hPa surface written in pascals (100000 Pa) satisfy
        # the 1000 Pa-suffixed prefix of it.
        self.assertTrue(_band_matches("hgt500", {"GRIB_ELEMENT": "HGT"}, "HGT at 500 mb"))
        self.assertFalse(_band_matches("hgt500", {"GRIB_ELEMENT": "HGT"}, "HGT at 1500 mb"))
        self.assertFalse(
            _band_matches("hgt500", {"GRIB_ELEMENT": "HGT"}, '150000[Pa] ISBL="Isobaric surface"')
        )


if __name__ == "__main__":
    unittest.main()

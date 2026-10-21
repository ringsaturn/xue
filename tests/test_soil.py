"""The soil layers: the land surface model's temperature and volumetric
water content in four layers counted from the surface, on GFS and ECMWF,
and the top two on AIFS.

Like the ocean fields before them, they add nothing to the container and
everything to the registries: a GRIB2 identity per layer (the registry's
first in GRIB2's land surface discipline, 2), a linear codebook, a record
matcher, a bundle id. `tests/fixtures/soil-registry.json` is the committed
golden that holds the Python and Rust encoders to one set of numbers: this
module regenerates and compares it, and the Rust encoder's unit tests read
the same file.

Three things are particular to them, and each is checked here against the
real records the GRIB fixtures carry:

- A layer is identified by its index, not its depth: GFS cuts the soil at
  0–10, 10–40, 40–100 and 100–200 cm on the "depth below land surface"
  surface (106), the IFS at 0–7, 7–28, 28–100 and 100–289 cm on its "soil
  level" surface (151, layer n carrying n − 1). The GFS record is the
  registered identity; ECMWF's is an alternate, and the ECMWF fetch turns
  it back into the ``levelist`` of the open data ``.index``.
- Neither field exists over water. GFS leaves both to a bitmap there, and
  AIFS its water content; the masked points become the bottom of each
  codebook, one step below the least real value, so code 0 is "no soil".
- GDAL's GRIB driver labels the soil temperature Celsius while handing it
  back in kelvin (its tables do not mark these parameters for conversion),
  so the converter reads the unit off the values
  (``binconvert.soil_temperature_unit``).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from tests._support import FIXTURES, registry_entry
from xuebuild import grib2
from xuebuild.binconvert import (
    SOIL_KELVIN_THRESHOLD,
    VECTOR_BUNDLES,
    VIDEO_VARIABLE_IDS,
    _convert_units,
    _extract_planes,
    _grid_info,
    published_bundle_ids,
    soil_temperature_unit,
)
from xuebuild.errors import ConversionError
from xuebuild.gdal import _band_matches, inspect_grib_multi, raster_expression
from xuebuild.grib2 import MessageInfo, _matches
from xuebuild.idx import ecmwf_field_byte_range, ecmwf_level_selector
from xuebuild.model import SourceFrame
from xuebuild.quantize import PROFILES
from xuebuild.sources import source_spec
from xuebuild.variables import (
    ECMWF_SOIL_LEVEL_TYPE,
    SOIL_LAYERS,
    SOIL_MOISTURE_IDS,
    SOIL_TEMPERATURE_IDS,
    SOIL_VARIABLE_IDS,
    SURFACE_TEMPERATURE_IDS,
    soil_layer,
    variable_spec,
)

GFS_FIXTURE = FIXTURES / "gfs.2026081406.f000.crop.grib2"
ECMWF_FIXTURES = (FIXTURES / "ecmwf.2026091212.f000.crop.grib2", FIXTURES / "ecmwf.2026091212.f003.crop.grib2")
AIFS_FIXTURES = (FIXTURES / "aifs.2026091700.f000.crop.grib2", FIXTURES / "aifs.2026091700.f006.crop.grib2")
REGISTRY = FIXTURES / "soil-registry.json"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
GDAL_GRIB_NODATA = 9999.0
GFS_LAYER_TOPS_M = (0.0, 0.1, 0.4, 1.0)
AIFS_SOIL_IDS = ("tsoil1", "tsoil2", "soilw1", "soilw2")


class RegistryTests(unittest.TestCase):
    def test_the_committed_registry_still_describes_this_encoder(self) -> None:
        expected = json.loads(REGISTRY.read_text(encoding="utf-8"))
        actual = {variable_id: registry_entry(variable_id) for variable_id in SOIL_VARIABLE_IDS}
        self.assertEqual(
            actual,
            expected,
            "the soil registry moved; the Rust encoder reads the same fixture, so "
            "regenerate it deliberately and change both",
        )

    def test_the_ids_are_layer_indices(self) -> None:
        self.assertEqual(SOIL_LAYERS, (1, 2, 3, 4))
        self.assertEqual(SOIL_TEMPERATURE_IDS, ("tsoil1", "tsoil2", "tsoil3", "tsoil4"))
        self.assertEqual(SOIL_MOISTURE_IDS, ("soilw1", "soilw2", "soilw3", "soilw4"))
        self.assertEqual(SOIL_VARIABLE_IDS, SOIL_TEMPERATURE_IDS + SOIL_MOISTURE_IDS)
        for layer in SOIL_LAYERS:
            self.assertEqual(soil_layer(f"tsoil{layer}"), layer)
            self.assertEqual(soil_layer(f"soilw{layer}"), layer)
        for other in ("tsoil5", "tsoil", "soilw0", "tmpsfc", "tmp2m"):
            self.assertIsNone(soil_layer(other), other)
        # The Celsius rule covers the soil temperature, not the water.
        for variable_id in SOIL_TEMPERATURE_IDS:
            self.assertIn(variable_id, SURFACE_TEMPERATURE_IDS)
        for variable_id in SOIL_MOISTURE_IDS:
            self.assertNotIn(variable_id, SURFACE_TEMPERATURE_IDS)

    def test_balanced_is_quality(self) -> None:
        # Smooth fields: production keeps the fine step.
        for variable_id in SOIL_VARIABLE_IDS:
            self.assertEqual(
                PROFILES["balanced"][variable_id].metadata(), PROFILES["quality"][variable_id].metadata(), variable_id
            )

    def test_code_zero_is_no_soil(self) -> None:
        for variable_id in SOIL_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            quality = PROFILES["quality"][variable_id]
            compact = PROFILES["compact"][variable_id]
            with self.subTest(variable=variable_id):
                self.assertEqual(compact.step, quality.step * 2)
                self.assertEqual(compact.minimum, quality.minimum)
                self.assertEqual(compact.maximum, quality.maximum)
                # The bottom of the codebook is what a bitmap-masked point
                # becomes, so it must be the value the registry says it is.
                self.assertEqual(quality.minimum, float(spec.value_range[0]))
                self.assertEqual(quality.maximum, float(spec.value_range[1]))
                self.assertEqual(spec.fill_values, (GDAL_GRIB_NODATA,))
                for book in (quality, compact):
                    self.assertLessEqual(book.maximum_code, 254)
                    self.assertEqual(book.quantize(np.array([spec.value_range[0]])).tolist(), [0])
        temperature, moisture = PROFILES["quality"]["tsoil1"], PROFILES["quality"]["soilw1"]
        self.assertEqual((temperature.minimum, temperature.maximum, temperature.step), (-61.0, 66.0, 0.5))
        self.assertEqual(temperature.maximum_code, 254)
        self.assertEqual((moisture.minimum, moisture.maximum, moisture.step), (-0.01, 1.0, 0.005))
        self.assertEqual(moisture.maximum_code, 202)
        for profile in ("quality", "compact"):
            temperature, moisture = PROFILES[profile]["tsoil1"], PROFILES[profile]["soilw1"]
            # The least soil temperature a real analysis holds (-57 °C, the
            # top layer under the Antarctic plateau) and a bone-dry soil are
            # values, not "no soil"; 0 °C and 0 m³/m³ are codes of their own.
            self.assertGreater(temperature.quantize(np.array([-57.3])).tolist()[0], 0, profile)
            self.assertGreater(moisture.quantize(np.array([0.0])).tolist()[0], 0, profile)
            self.assertEqual(temperature.decode(temperature.quantize(np.array([0.0]))).tolist(), [0.0], profile)
            self.assertAlmostEqual(moisture.decode(moisture.quantize(np.array([0.0])))[0], 0.0, places=12)
            # A desert's top layer (58 °C) and a saturated peat (0.9) are
            # inside the range.
            self.assertLess(temperature.quantize(np.array([58.4])).tolist()[0], temperature.maximum_code, profile)
            self.assertLess(moisture.quantize(np.array([0.9])).tolist()[0], moisture.maximum_code, profile)


class IdentityTests(unittest.TestCase):
    def test_the_gfs_record_is_the_identity(self) -> None:
        for layer, top in zip(SOIL_LAYERS, GFS_LAYER_TOPS_M, strict=True):
            temperature, moisture = variable_spec(f"tsoil{layer}"), variable_spec(f"soilw{layer}")
            self.assertEqual(
                (temperature.grib2_discipline, temperature.grib2_category, temperature.grib2_number), (2, 0, 2)
            )
            self.assertEqual((moisture.grib2_discipline, moisture.grib2_category, moisture.grib2_number), (2, 0, 192))
            for spec in (temperature, moisture):
                self.assertEqual((spec.grib2_level_type, spec.grib2_level_value), (106, top))
                self.assertIsNone(spec.grib2_statistical)
                self.assertEqual(spec.grib2_aliases, ())
            self.assertEqual((temperature.output_unit, temperature.gdal_unit), ("°C", "C"))
            self.assertEqual((moisture.output_unit, moisture.gdal_unit), ("m³/m³", "Fraction"))
            phrase = ("0-0.1", "0.1-0.4", "0.4-1", "1-2")[layer - 1]
            self.assertEqual(temperature.index_field, f":TSOIL:{phrase} m below ground:")
            self.assertEqual(moisture.index_field, f":SOILW:{phrase} m below ground:")
        # The metadata block writes each top with the least scale factor.
        self.assertEqual(
            [
                (block["scaleFactorOfFirstFixedSurface"], block["scaledValueOfFirstFixedSurface"])
                for block in (variable_spec(f"tsoil{layer}").parameter_metadata() for layer in SOIL_LAYERS)
            ],
            [(0, 0), (1, 1), (1, 4), (0, 1)],
        )

    def test_ecmwf_writes_the_wmo_parameters_on_its_soil_levels(self) -> None:
        for layer in SOIL_LAYERS:
            temperature, moisture = variable_spec(f"tsoil{layer}"), variable_spec(f"soilw{layer}")
            self.assertEqual((temperature.ecmwf_param, moisture.ecmwf_param), ("sot", "vsw"))
            (sot,) = temperature.grib2_alternates
            (vsw,) = moisture.grib2_alternates
            self.assertEqual((sot.triple, sot.level_type, sot.level_value, sot.statistical), ((2, 3, 18), 151, layer - 1.0, None))
            self.assertEqual((vsw.triple, vsw.level_type, vsw.level_value, vsw.statistical), ((2, 0, 25), 151, layer - 1.0, None))
            self.assertEqual((sot.gdal_unit, vsw.gdal_unit), ("", "m^3/m^3"))
            self.assertEqual(ECMWF_SOIL_LEVEL_TYPE, 151)
            self.assertEqual(ecmwf_level_selector(temperature), ("sol", str(layer)))
            self.assertEqual(ecmwf_level_selector(moisture), ("sol", str(layer)))
        # Nothing else is a soil level.
        self.assertEqual(ecmwf_level_selector(variable_spec("tmpsfc")), ("sfc", None))
        self.assertEqual(ecmwf_level_selector(variable_spec("tmp850")), ("pl", "850"))

    def test_the_ecmwf_index_names_each_layer_once(self) -> None:
        lines = [
            {"param": "sot", "levtype": "sol", "levelist": str(layer), "_offset": 100 * layer, "_length": 10}
            for layer in SOIL_LAYERS
        ] + [
            {"param": "vsw", "levtype": "sol", "levelist": str(layer), "_offset": 1000 + 100 * layer, "_length": 10}
            for layer in SOIL_LAYERS
        ] + [{"param": "skt", "levtype": "sfc", "_offset": 5000, "_length": 10}]
        text = "\n".join(json.dumps(line) for line in lines)
        for layer in SOIL_LAYERS:
            for variable_id, base in ((f"tsoil{layer}", 0), (f"soilw{layer}", 1000)):
                spec = variable_spec(variable_id)
                levtype, levelist = ecmwf_level_selector(spec)
                found = ecmwf_field_byte_range(text, spec.ecmwf_param, levtype=levtype, levelist=levelist)
                self.assertEqual((found.start, found.end), (base + 100 * layer, base + 100 * layer + 9), variable_id)


class SourceTests(unittest.TestCase):
    def test_gfs_and_ecmwf_publish_four_layers_and_aifs_two(self) -> None:
        for model, expected in (("gfs", SOIL_VARIABLE_IDS), ("ecmwf", SOIL_VARIABLE_IDS), ("aifs", AIFS_SOIL_IDS)):
            source = source_spec(model)
            published = published_bundle_ids(source)
            with self.subTest(model=model):
                self.assertEqual(tuple(b for b in published if b in SOIL_VARIABLE_IDS), expected)
                self.assertEqual(tuple(v for v in source.input_variable_ids if v in SOIL_VARIABLE_IDS), expected)
                # Together, temperature then water, after the ocean fields
                # and before the static orography.
                start = published.index(expected[0])
                self.assertEqual(published[start : start + len(expected)], expected)
                self.assertEqual(published[start + len(expected)], "orog")
                # Primary-file records, read at every frame from the
                # analysis on.
                for variable_id in expected:
                    self.assertIsNone(source.companion_of(variable_id), variable_id)
                    self.assertNotIn(variable_id, source.optional_at_analysis)
                    self.assertNotIn(variable_id, VECTOR_BUNDLES)
                    self.assertNotIn(variable_id, VIDEO_VARIABLE_IDS)
                    self.assertFalse(variable_spec(variable_id).static, variable_id)
        for model in ("hrrr", "sflux", "ifshres", "cfs", "gefsaero"):
            published = published_bundle_ids(source_spec(model))
            for variable_id in SOIL_VARIABLE_IDS:
                self.assertNotIn(variable_id, published, model)


class MatcherTests(unittest.TestCase):
    @staticmethod
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

    def test_the_header_index_matches_one_layer_per_variable(self) -> None:
        message = self.message
        for layer, top in zip(SOIL_LAYERS, GFS_LAYER_TOPS_M, strict=True):
            temperature, moisture = variable_spec(f"tsoil{layer}"), variable_spec(f"soilw{layer}")
            self.assertTrue(_matches(temperature, message(2, 0, 2, 106, top)))
            self.assertTrue(_matches(moisture, message(2, 0, 192, 106, top)))
            self.assertTrue(_matches(temperature, message(2, 3, 18, 151, layer - 1.0)))
            self.assertTrue(_matches(moisture, message(2, 0, 25, 151, layer - 1.0)))
            for other in SOIL_LAYERS:
                if other != layer:
                    self.assertFalse(_matches(temperature, message(2, 3, 18, 151, other - 1.0)), (layer, other))
                    self.assertFalse(_matches(moisture, message(2, 0, 192, 106, GFS_LAYER_TOPS_M[other - 1])))
            # The liquid water (SOILL, 2/3/192) and the ECMWF temperature on
            # the GFS surface are other fields.
            self.assertFalse(_matches(moisture, message(2, 3, 192, 106, top)))
            self.assertFalse(_matches(temperature, message(2, 3, 18, 106, top)))
            self.assertFalse(_matches(temperature, message(2, 0, 192, 106, top)), "the water is not the temperature")

    def test_gdal_bands_match_on_the_assembled_template(self) -> None:
        def band(discipline: str, values: str, pdtn: str = "0") -> dict[str, str]:
            return {
                "GRIB_DISCIPLINE": discipline,
                "GRIB_PDS_PDTN": pdtn,
                "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": values,
            }

        # As GDAL reports the records the fixtures carry.
        gfs_tsoil2 = band("2(Land_Surface)", "0 2 2 0 81 0 0 1 0 106 2 10 106 2 40")
        gfs_soilw1 = band("2(Land_Surface)", "0 192 2 0 81 0 0 1 0 106 2 0 106 2 10")
        ecmwf_sot1 = band("2(Land_Surface)", "3 18 2 255 161 0 0 1 0 151 0 0 151 0 1")
        aifs_vsw2 = band("2(Land_Surface)", "0 25 2 1 5 65535 255 1 0 151 0 1 151 0 2")
        self.assertTrue(_band_matches("tsoil2", gfs_tsoil2, ""))
        self.assertFalse(_band_matches("tsoil1", gfs_tsoil2, ""))
        self.assertFalse(_band_matches("soilw2", gfs_tsoil2, ""))
        self.assertTrue(_band_matches("soilw1", gfs_soilw1, ""))
        self.assertTrue(_band_matches("tsoil1", ecmwf_sot1, ""))
        self.assertFalse(_band_matches("tsoil2", ecmwf_sot1, ""))
        self.assertTrue(_band_matches("soilw2", aifs_vsw2, ""))
        self.assertFalse(_band_matches("soilw1", aifs_vsw2, ""))
        # Another discipline, another template, or no template at all.
        self.assertFalse(_band_matches("tsoil2", band("0(Meteorological)", "0 2 2 0 81 0 0 1 0 106 2 10 106 2 40"), ""))
        self.assertFalse(_band_matches("tsoil2", band("2(Land_Surface)", "0 2 2 0 81 0 0 1 0 106 2 10 106 2 40", "8"), ""))
        self.assertFalse(_band_matches("tsoil2", {"GRIB_ELEMENT": "TSOIL", "GRIB_SHORT_NAME": "0.1-0.4-DBLL"}, ""))

    def test_units_are_accepted_as_gdal_reports_them(self) -> None:
        for variable_id in SOIL_TEMPERATURE_IDS:
            self.assertEqual(raster_expression(variable_spec(variable_id), "[C]"), "A")
            self.assertEqual(raster_expression(variable_spec(variable_id), "K"), "A-273.15")
        for variable_id in SOIL_MOISTURE_IDS:
            for unit in ("[Fraction]", "m^3/m^3", "[m^3/m^3]", "Proportion"):
                self.assertEqual(raster_expression(variable_spec(variable_id), unit), "A", unit)
        for variable_id, unit in (("tsoil1", "m"), ("soilw1", "%"), ("soilw1", "kg/m^2")):
            with self.assertRaises(ConversionError):
                raster_expression(variable_spec(variable_id), unit)

    def test_the_fixtures_carry_every_layer_and_both_matchers_agree(self) -> None:
        cases = (
            ("gfs", (GFS_FIXTURE,), SOIL_VARIABLE_IDS, ((72, 73, 74, 75, 76, 77, 78, 79),)),
            ("ecmwf", ECMWF_FIXTURES, SOIL_VARIABLE_IDS, ((56, 57, 58, 59, 60, 61, 62, 63),) * 2),
            # The analysis carries the orography before them, the step not.
            ("aifs", AIFS_FIXTURES, AIFS_SOIL_IDS, ((30, 31, 32, 33), (29, 30, 31, 32))),
        )
        for model, paths, variable_ids, bands_per_path in cases:
            source = source_spec(model)
            optional = source.optional_at_analysis + ("orog",)
            for path, bands in zip(paths, bands_per_path, strict=True):
                with self.subTest(fixture=path.name):
                    fast = grib2.inspect_grib_fast(path, source.input_variable_ids, optional_ids=optional)
                    reference = inspect_grib_multi(path, source.input_variable_ids, optional_ids=optional)
                    for variable_id, band in zip(variable_ids, bands, strict=True):
                        self.assertEqual(fast[variable_id].band, band, variable_id)
                        self.assertEqual(reference[variable_id].band, band, variable_id)
                        self.assertEqual(
                            raster_expression(variable_spec(variable_id), fast[variable_id].unit),
                            raster_expression(variable_spec(variable_id), reference[variable_id].unit),
                            variable_id,
                        )
                    for variable_id in variable_ids:
                        if variable_id in SOIL_MOISTURE_IDS:
                            expected = "Fraction" if model == "gfs" else "m^3/m^3"
                            self.assertEqual(fast[variable_id].unit, expected, variable_id)
                        else:
                            self.assertEqual(fast[variable_id].unit, "C", variable_id)


class ConversionTests(unittest.TestCase):
    def frame(self, variable_id: str, unit: str) -> SourceFrame:
        return SourceFrame(GFS_FIXTURE, 1, variable_id, EPOCH, EPOCH, 0, unit)

    def test_a_soil_temperature_labelled_celsius_in_kelvin_is_converted(self) -> None:
        # GDAL's label is C; the values are kelvin, and the fill (the bottom
        # of the codebook) sits among them.
        kelvin = _convert_units(self.frame("tsoil1", "C"), np.array([-61.0, 273.15, 300.0]))
        np.testing.assert_allclose(kelvin, [-61.0 - 273.15, 0.0, 26.85])
        self.assertEqual(PROFILES["quality"]["tsoil1"].quantize(kelvin).tolist()[0], 0, "the fill stays no soil")
        # Celsius that really is Celsius stays, the fill with it.
        np.testing.assert_array_equal(_convert_units(self.frame("tsoil1", "C"), np.array([-61.0, 12.5, 58.0])), [-61.0, 12.5, 58.0])
        np.testing.assert_allclose(_convert_units(self.frame("tsoil3", "K"), np.array([273.15])), [0.0])
        # An all-water plane is the fill alone, below the threshold either way.
        np.testing.assert_array_equal(_convert_units(self.frame("tsoil2", "C"), np.full(3, -61.0)), [-61.0] * 3)
        self.assertEqual(soil_temperature_unit("C", np.array([SOIL_KELVIN_THRESHOLD])), "C")
        self.assertEqual(soil_temperature_unit("C", np.array([SOIL_KELVIN_THRESHOLD + 0.01])), "K")
        self.assertEqual(soil_temperature_unit("K", np.array([12.0])), "K")
        self.assertEqual(soil_temperature_unit("C", np.array([])), "C")
        # The rule is the soil temperature's alone: a skin temperature
        # labelled Celsius is Celsius whatever it holds.
        np.testing.assert_array_equal(_convert_units(self.frame("tmpsfc", "C"), np.array([300.0])), [300.0])

    def test_the_water_content_is_already_in_its_unit(self) -> None:
        for unit in ("Fraction", "m^3/m^3"):
            np.testing.assert_array_equal(_convert_units(self.frame("soilw1", unit), np.array([0.0, 0.35, 1.0])), [0.0, 0.35, 1.0])

    def test_water_becomes_no_soil_on_the_gfs_fixture(self) -> None:
        gfs = source_spec("gfs")
        grid = _grid_info(GFS_FIXTURE)
        frames = grib2.inspect_grib_fast(GFS_FIXTURE, gfs.input_variable_ids)
        with tempfile.TemporaryDirectory() as work:
            planes = _extract_planes({v: frames[v] for v in ("tmp2m", *SOIL_VARIABLE_IDS)}, grid, Path(work))
        # The window is 118–138E, 18–38N, about a sixth of it land: the same
        # cells carry soil in every layer and both fields.
        land = planes["tsoil1"] > -61.0
        self.assertGreater(land.mean(), 0.1)
        self.assertLess(land.mean(), 0.3)
        for variable_id in SOIL_VARIABLE_IDS:
            plane = planes[variable_id]
            self.assertTrue(np.isfinite(plane).all(), variable_id)
            np.testing.assert_array_equal(plane > float(variable_spec(variable_id).value_range[0]), land, variable_id)
            self.assertEqual(PROFILES["balanced"][variable_id].quantize(plane[~land]).tolist(), [0] * int((~land).sum()))
        # August in East Asia, in Celsius: the kelvin GDAL handed back was
        # converted, and nothing clamps.
        for variable_id in SOIL_TEMPERATURE_IDS:
            self.assertGreater(planes[variable_id][land].min(), 5.0, variable_id)
            self.assertLess(planes[variable_id][land].max(), 45.0, variable_id)
        for variable_id in SOIL_MOISTURE_IDS:
            self.assertGreater(planes[variable_id][land].min(), 0.0, variable_id)
            self.assertLess(planes[variable_id][land].max(), 0.6, variable_id)
        # Deeper is steadier: the top layer swings furthest.
        spread = [float(np.ptp(planes[variable_id][land])) for variable_id in SOIL_TEMPERATURE_IDS]
        self.assertEqual(spread[0], max(spread))


if __name__ == "__main__":
    unittest.main()

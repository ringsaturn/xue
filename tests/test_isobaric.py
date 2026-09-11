"""The upper-air fills: temperature, relative and specific humidity, the wind
and the water vapour flux on the eight standard isobaric surfaces.

Like the pressure family before them (`tests/test_pressure.py`), these add
nothing to the container — no version bump, no new metadata key — and
everything to the registries: a GRIB2 identity, a linear codebook, a record
matcher, a bundle id. `tests/fixtures/isobaric-registry.json` is the committed
golden that holds the three implementations to one set of numbers: this module
regenerates and compares it, the Rust encoder's unit tests and the frontend's
vitest read the same file, so a codebook can only move in all three at once.

The one rule with no other home is the **vapour flux derivation**: `q·V/g`
with `q` in g/kg, `V` in m/s and standard gravity, in that operation order,
which is what keeps the Python and native encoders byte-identical on a field
neither of them reads from a record.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import numpy as np

from xuebuild.binconvert import (
    VECTOR_BUNDLES,
    VIDEO_VARIABLE_IDS,
    bundle_input_ids,
    derive_vapour_flux,
    published_bundle_ids,
    vapour_flux_level,
    vector_input_ids,
)
from xuebuild.errors import ConversionError
from xuebuild.gdal import _band_matches, raster_expression
from xuebuild.manifest import BIN_BUNDLE_VARIABLES
from xuebuild.quantize import ISOBARIC_VARIABLE_IDS, PROFILES
from xuebuild.sources import source_spec
from xuebuild.variables import (
    ISOBARIC_FAMILIES,
    ISOBARIC_FAMILY_FIRST_ID,
    ISOBARIC_LEVELS_HPA,
    STANDARD_GRAVITY,
    isobaric_variable,
    isobaric_variable_id,
    variable_spec,
)

REGISTRY = Path(__file__).resolve().parent / "fixtures" / "isobaric-registry.json"


def registry_entry(variable_id: str) -> dict:
    """The registry as the three implementations must agree it is."""
    spec = variable_spec(variable_id)
    return {
        "numericId": spec.numeric_id,
        "label": spec.label,
        "unit": spec.output_unit,
        "parameter": spec.parameter_metadata(),
        "quality": PROFILES["quality"][variable_id].metadata(),
        "compact": PROFILES["compact"][variable_id].metadata(),
    }


class RegistryTests(unittest.TestCase):
    def test_the_committed_registry_still_describes_this_encoder(self) -> None:
        expected = json.loads(REGISTRY.read_text(encoding="utf-8"))
        actual = {variable_id: registry_entry(variable_id) for variable_id in ISOBARIC_VARIABLE_IDS}
        self.assertEqual(
            actual,
            expected,
            "the isobaric registry moved; the Rust encoder and the frontend read the same "
            "fixture, so regenerate it deliberately and change all three",
        )

    def test_balanced_is_quality_except_for_relative_humidity(self) -> None:
        for variable_id in ISOBARIC_VARIABLE_IDS:
            expected = "compact" if variable_id.startswith("rh") else "quality"
            self.assertEqual(
                PROFILES["balanced"][variable_id].metadata(),
                PROFILES[expected][variable_id].metadata(),
                variable_id,
            )

    def test_every_family_is_registered_at_every_level_with_contiguous_ids(self) -> None:
        for family in ISOBARIC_FAMILIES:
            first = ISOBARIC_FAMILY_FIRST_ID[family]
            for index, level in enumerate(ISOBARIC_LEVELS_HPA):
                variable_id = isobaric_variable_id(family, level)
                spec = variable_spec(variable_id)
                self.assertEqual(spec.numeric_id, first + index, variable_id)
                self.assertEqual(spec.grib2_level_type, 100, variable_id)
                self.assertEqual(spec.grib2_level_value, level * 100.0, variable_id)
                self.assertEqual(isobaric_variable(variable_id), (family, level))
        numeric_ids = [variable_spec(variable_id).numeric_id for variable_id in ISOBARIC_VARIABLE_IDS]
        self.assertEqual(len(numeric_ids), len(set(numeric_ids)))
        self.assertIsNone(isobaric_variable("tmp550"), "not a registered level")
        self.assertIsNone(isobaric_variable("tmp2m"))
        self.assertIsNone(isobaric_variable("prmsl"))

    def test_the_manifest_orders_every_scalar_before_every_vector(self) -> None:
        vectors = [variable for variable in BIN_BUNDLE_VARIABLES if variable in VECTOR_BUNDLES]
        scalars = [variable for variable in BIN_BUNDLE_VARIABLES if variable not in VECTOR_BUNDLES]
        self.assertEqual(list(BIN_BUNDLE_VARIABLES), scalars + vectors)
        for family in ("tmp", "rh", "spfh"):
            self.assertTrue(all(isobaric_variable_id(family, level) in scalars for level in ISOBARIC_LEVELS_HPA))
        self.assertEqual(vectors[0], "wind10m")
        for level in ISOBARIC_LEVELS_HPA:
            self.assertIn(f"wind{level}", vectors)
            self.assertIn(f"qflux{level}", vectors)
        # The flux components never appear as bundles of their own.
        self.assertFalse(any(variable.startswith(("uqflx", "vqflx", "ugrd", "vgrd")) for variable in BIN_BUNDLE_VARIABLES))


class VectorBundleTests(unittest.TestCase):
    def test_a_vector_bundle_names_its_inputs(self) -> None:
        self.assertEqual(vector_input_ids("wind10m"), ("ugrd10m", "vgrd10m"))
        self.assertEqual(vector_input_ids("wind850"), ("ugrd850", "vgrd850"))
        self.assertEqual(vector_input_ids("qflux850"), ("spfh850", "ugrd850", "vgrd850"))
        self.assertEqual(VECTOR_BUNDLES["qflux850"], ("uqflx850", "vqflx850"))
        self.assertEqual(vapour_flux_level("qflux700"), 700)
        self.assertIsNone(vapour_flux_level("wind700"))
        self.assertIsNone(vapour_flux_level("qflux550"))

    def test_gfs_publishes_the_first_launch_set(self) -> None:
        gfs = source_spec("gfs")
        published = published_bundle_ids(gfs)
        for bundle_id in ("tmp850", "tmp500", "rh850", "rh700", "wind10m", "wind850", "qflux850"):
            self.assertIn(bundle_id, published)
        self.assertNotIn("spfh850", published, "the specific humidity is an input only")
        self.assertEqual(bundle_input_ids(gfs, "qflux850"), ("spfh850", "ugrd850", "vgrd850"))
        # A listed vector bundle without its inputs is not published.
        self.assertEqual(published_bundle_ids(source_spec("radar")), ("cref",))
        self.assertEqual(published_bundle_ids(source_spec("ecmwf"))[-1], "wind10m")
        self.assertEqual(published_bundle_ids(source_spec("sflux"))[-1], "wind10m")
        # The manifest order is the published order.
        self.assertEqual(
            list(published),
            [variable for variable in BIN_BUNDLE_VARIABLES if variable in published],
        )

    def test_only_the_surface_fields_get_a_video_companion(self) -> None:
        self.assertEqual(set(VIDEO_VARIABLE_IDS), {"tmp2m", "prate", "dswrf", "cref"})

    def test_the_vapour_flux_is_q_v_over_g(self) -> None:
        q = np.array([10.0, 0.0, 20.0])  # g/kg
        u = np.array([9.80665, 5.0, -9.80665])
        v = np.array([0.0, 5.0, 19.6133])
        flux_u, flux_v = derive_vapour_flux({"spfh850": q, "ugrd850": u, "vgrd850": v}, "qflux850")
        np.testing.assert_allclose(flux_u, [10.0, 0.0, -20.0])
        np.testing.assert_allclose(flux_v, [0.0, 0.0, 40.0])
        # The exact operation order the native encoder reproduces.
        self.assertEqual(flux_u[0], q[0] * u[0] / STANDARD_GRAVITY)


class MatcherTests(unittest.TestCase):
    def test_each_family_matches_its_own_element_on_its_own_surface(self) -> None:
        for variable_id, element in (
            ("tmp850", "TMP"),
            ("rh700", "RH"),
            ("spfh850", "SPFH"),
            ("ugrd850", "UGRD"),
            ("vgrd500", "VGRD"),
            ("hgt500", "HGT"),
        ):
            level = isobaric_variable(variable_id)[1]
            pascals = {"GRIB_ELEMENT": element, "GRIB_SHORT_NAME": f"{level * 100}-ISBL"}
            self.assertTrue(_band_matches(variable_id, pascals, f'{level * 100}[Pa] ISBL="Isobaric surface"'))
            other_element = {"GRIB_ELEMENT": "HGT" if element != "HGT" else "TMP", "GRIB_SHORT_NAME": f"{level * 100}-ISBL"}
            self.assertFalse(_band_matches(variable_id, other_element, ""))
            other_level = {"GRIB_ELEMENT": element, "GRIB_SHORT_NAME": "100000-ISBL"}
            self.assertFalse(_band_matches(variable_id, other_level, ""))

    def test_a_derived_component_is_never_matched(self) -> None:
        with self.assertRaises(ConversionError):
            _band_matches("uqflx850", {"GRIB_ELEMENT": "SPFH"}, "")

    def test_units_follow_the_family(self) -> None:
        self.assertEqual(raster_expression("tmp850", "[C]"), raster_expression("tmp2m", "[C]"))
        self.assertEqual(raster_expression("tmp850", "K"), "maximum(-60,minimum(50,A-273.15))")
        self.assertEqual(raster_expression("rh700", "[%]"), "maximum(0,minimum(100,A))")
        self.assertEqual(raster_expression("spfh850", "[kg/kg]"), "A*1000")
        self.assertEqual(raster_expression("ugrd850", "[m/s]"), raster_expression("ugrd10m", "[m/s]"))
        for variable_id, unit in (("rh700", "kg/kg"), ("spfh850", "g/kg"), ("spfh850", "%")):
            with self.assertRaises(ConversionError):
                raster_expression(variable_id, unit)


if __name__ == "__main__":
    unittest.main()

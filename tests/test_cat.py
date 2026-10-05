"""The clear-air turbulence bundles: Ellrod TI1 projected onto EDR.

``cat300`` / ``cat250`` / ``cat200`` are the first derivation that reads a
cell's neighbours rather than the cell alone, so on top of the arithmetic
these cases hold the geometry: centred differences on the sphere, the
wrapping longitude, the rows the index is not defined on, and a crop that is
cut after the derivation, never before it.
"""

from __future__ import annotations

import math
import unittest
from unittest import mock

import numpy as np

from xuebuild.binconvert import (
    CAT_EDR_LOG_MEAN,
    CAT_EDR_LOG_STD,
    CAT_EDR_NIL,
    CAT_SHEAR_LAYERS,
    DERIVED_SCALARS,
    EARTH_RADIUS_M,
    RAW_VARIABLE_IDS,
    CropWindow,
    GridInfo,
    _derive_cat_plane,
    bundle_input_ids,
    cat_level,
    derive_cat,
    ellrod_ti1,
    published_bundle_ids,
    uncropped_latitudes,
)
from xuebuild.errors import ConversionError
from xuebuild.quantize import PROFILES, QUALITY_CAT
from xuebuild.sources import source_spec
from xuebuild.variables import CAT_LEVELS_HPA, variable_spec

RAD = math.pi / 180.0


def _planes(level: int, rows: int, columns: int, u: np.ndarray, v: np.ndarray) -> dict[str, np.ndarray]:
    """The inputs of ``cat<level>``: ``(u, v)`` on the surface and below,
    the wind above 10 m/s stronger eastward, and the height above 1000 m
    higher — a vertical shear of exactly 0.01 s^-1 everywhere."""
    above, below = CAT_SHEAR_LAYERS[level]
    z = np.full((rows, columns), 9000.0)
    planes = {f"ugrd{below}": u, f"vgrd{below}": v, f"hgt{below}": z}
    planes.update({f"ugrd{above}": u + 10.0, f"vgrd{above}": v.copy(), f"hgt{above}": z + 1000.0})
    planes.setdefault(f"ugrd{level}", u)
    planes.setdefault(f"vgrd{level}", v)
    return planes


class GeometryTests(unittest.TestCase):
    rows, columns = 9, 12
    latitudes = 4.0 - 1.0 * np.arange(9, dtype=np.float64)  # 4 .. -4

    def test_pure_stretching_is_the_zonal_gradient_over_dx(self) -> None:
        j = np.arange(self.columns, dtype=np.float64)
        u = np.tile(2.0 * j, (self.rows, 1))
        v = np.zeros_like(u)
        ti1, valid = ellrod_ti1(_planes(300, self.rows, self.columns, u, v), 300, self.latitudes, 1.0, -1.0, False)
        dx = EARTH_RADIUS_M * np.cos(self.latitudes * RAD) * (1.0 * RAD)
        expected = 0.01 * (2.0 / dx)
        np.testing.assert_allclose(ti1[1:-1, 1:-1], np.broadcast_to(expected[1:-1, None], (7, 10)), rtol=1e-12)
        # Not wrapping: the end columns have no centred difference.
        self.assertFalse(valid[:, 0].any() or valid[:, -1].any())
        self.assertEqual(ti1[4, 0], 0.0)
        self.assertFalse(valid[0].any() or valid[-1].any())

    def test_pure_shear_is_the_meridional_gradient_over_dy(self) -> None:
        r = np.arange(self.rows, dtype=np.float64)
        u = np.tile(-3.0 * r[:, None], (1, self.columns))  # eastward wind grows northward
        v = np.zeros_like(u)
        ti1, _valid = ellrod_ti1(_planes(250, self.rows, self.columns, u, v), 250, self.latitudes, 1.0, -1.0, False)
        dy = EARTH_RADIUS_M * (1.0 * RAD)
        # du/dy = (u[r-1] - u[r+1]) / 2dy = 3 / dy: shearing deformation.
        np.testing.assert_allclose(ti1[1:-1, 1:-1], 0.01 * 3.0 / dy, rtol=1e-12)

    def test_the_vertical_shear_divides_by_the_layer_depth(self) -> None:
        j = np.arange(self.columns, dtype=np.float64)
        u = np.tile(j, (self.rows, 1))
        planes = _planes(200, self.rows, self.columns, u, np.zeros_like(u))
        planes["vgrd200"] = planes["vgrd200"] + 30.0  # |dV| = hypot(10, 30)
        planes["hgt200"] = planes["hgt250"] + 2000.0
        ti1, _ = ellrod_ti1(planes, 200, self.latitudes, 1.0, -1.0, False)
        dx = EARTH_RADIUS_M * math.cos(0.0) * RAD
        self.assertAlmostEqual(ti1[4, 5], math.sqrt(10.0**2 + 30.0**2) / 2000.0 * (1.0 / dx), delta=1e-20)

    def test_the_first_and_last_columns_are_neighbours_on_a_wrapping_grid(self) -> None:
        rng = np.random.default_rng(32)
        u = rng.normal(size=(self.rows, self.columns))
        v = rng.normal(size=(self.rows, self.columns))
        ti1, valid = ellrod_ti1(_planes(300, self.rows, self.columns, u, v), 300, self.latitudes, 30.0, -1.0, True)
        self.assertTrue(valid[1:-1].all())
        r = 3
        two_dx = 2.0 * (EARTH_RADIUS_M * math.cos(self.latitudes[r] * RAD) * (30.0 * RAD))
        two_dy = 2.0 * (EARTH_RADIUS_M * (1.0 * RAD))
        dudx = (u[r, 1] - u[r, -1]) / two_dx
        dvdx = (v[r, 1] - v[r, -1]) / two_dx
        dudy = (u[r - 1, 0] - u[r + 1, 0]) / two_dy
        dvdy = (v[r - 1, 0] - v[r + 1, 0]) / two_dy
        dst, dsh = dudx - dvdy, dvdx + dudy
        # Bit-exact: the documented operation order is the contract.
        self.assertEqual(ti1[r, 0], 0.01 * math.sqrt(dst * dst + dsh * dsh))

    def test_the_polar_rows_are_not_computed(self) -> None:
        latitudes = 88.0 - 1.0 * np.arange(8, dtype=np.float64)  # 88 .. 81
        u = np.tile(np.arange(12, dtype=np.float64), (8, 1))
        ti1, valid = ellrod_ti1(_planes(300, 8, 12, u, np.zeros_like(u)), 300, latitudes, 30.0, -1.0, True)
        self.assertEqual(valid[:, 5].tolist(), [False, False, False, True, True, True, True, False])
        self.assertTrue((ti1[:3] == 0.0).all())

    def test_a_south_to_north_grid_is_refused(self) -> None:
        u = np.zeros((self.rows, self.columns))
        with self.assertRaises(ConversionError):
            ellrod_ti1(_planes(300, self.rows, self.columns, u, u), 300, self.latitudes[::-1], 1.0, 1.0, True)


class ProjectionTests(unittest.TestCase):
    rows, columns = 5, 8
    latitudes = np.array([2.0, 1.0, 0.0, -1.0, -2.0])

    def _field(self) -> tuple[dict[str, np.ndarray], float, np.ndarray]:
        """A field whose TI1 is one value on every defined cell, that value,
        and the mask."""
        u = np.tile(np.arange(self.columns, dtype=np.float64), (self.rows, 1))
        planes = _planes(300, self.rows, self.columns, u, np.zeros_like(u))
        ti1, valid = ellrod_ti1(planes, 300, self.latitudes, 1.0, -1.0, False)
        return planes, float(ti1[2, 3]), valid

    def _edr(self, planes: dict[str, np.ndarray], mean: float, std: float) -> np.ndarray:
        return derive_cat(planes, "cat300", self.latitudes, 1.0, -1.0, False, (mean, std))

    def test_edr_is_the_lognormal_projection_of_ti1(self) -> None:
        planes, ti1, valid = self._field()
        std = 1.2
        # Two standard deviations above the fit's mean: moderate, kept.
        mean = math.log(ti1) - 2.0 * std
        edr = self._edr(planes, mean, std)
        b = CAT_EDR_LOG_STD / std
        a = CAT_EDR_LOG_MEAN - b * mean
        self.assertEqual(edr[2, 3], math.exp(a + b * math.log(ti1)))
        self.assertAlmostEqual(edr[2, 3], math.exp(CAT_EDR_LOG_MEAN + 2.0 * CAT_EDR_LOG_STD))
        self.assertTrue((edr[~valid] == 0.0).all())
        # Calm air floors rather than taking the logarithm of zero.
        calm = self._edr(_planes(300, self.rows, self.columns, np.zeros((5, 8)), np.zeros((5, 8))), mean, std)
        self.assertTrue(np.isfinite(calm).all())
        self.assertEqual(calm.max(), 0.0)

    def test_nil_turbulence_is_written_as_zero(self) -> None:
        planes, ti1, valid = self._field()
        std = 1.2
        # At the fit's mean the EDR is the climatological median, 0.076: nil.
        at_mean = self._edr(planes, math.log(ti1), std)
        self.assertLess(math.exp(CAT_EDR_LOG_MEAN), CAT_EDR_NIL)
        self.assertTrue((at_mean == 0.0).all())
        # The threshold itself: with std = CAT_EDR_LOG_STD and
        # mean = CAT_EDR_LOG_MEAN the projection is exp(ln TI1) (b = 1,
        # a = 0). exp never returns 0.10 exactly, so the cells tried are the
        # TI1 values whose EDR is the nearest double at or above 0.10 (kept,
        # code 20) and the nearest one below it (nil).
        def projected(t: float) -> float:
            return float(np.exp(np.log(t)))

        lowest_kept = CAT_EDR_NIL
        while projected(lowest_kept) >= CAT_EDR_NIL:
            lowest_kept = float(np.nextafter(lowest_kept, 0.0))
        highest_nil = lowest_kept
        while projected(lowest_kept) < CAT_EDR_NIL:
            lowest_kept = float(np.nextafter(lowest_kept, 1.0))
        self.assertLess(projected(lowest_kept) - CAT_EDR_NIL, 1e-16)
        self.assertLess(CAT_EDR_NIL - projected(highest_nil), 1e-16)
        ti1_plane = np.zeros((self.rows, self.columns))
        ti1_plane[2, 2], ti1_plane[2, 3], ti1_plane[2, 4] = highest_nil, lowest_kept, 0.3
        with mock.patch("xuebuild.binconvert.ellrod_ti1", return_value=(ti1_plane, valid)):
            edr = self._edr(planes, CAT_EDR_LOG_MEAN, CAT_EDR_LOG_STD)
        self.assertEqual(edr[2, 2], 0.0)
        self.assertEqual(edr[2, 3], projected(lowest_kept))
        self.assertEqual(int(QUALITY_CAT.quantize(edr)[2, 3]), 20)
        self.assertAlmostEqual(edr[2, 4], 0.3)
        # 0.10 itself, had it come out of exp, would keep its code too.
        self.assertEqual(int(QUALITY_CAT.quantize(np.array([CAT_EDR_NIL]))[0]), 20)

    def test_a_crop_is_cut_after_the_derivation(self) -> None:
        rows, columns = 13, 24
        grid = GridInfo(width=columns, height=rows, first_longitude=-180.0, first_latitude=60.0,
                        longitude_step=15.0, latitude_step=-10.0)
        rng = np.random.default_rng(7)
        planes = {variable_id: rng.normal(size=rows * columns) * 20.0 for variable_id in DERIVED_SCALARS["cat250"]}
        planes["hgt200"] = planes["hgt200"] + 12000.0
        planes["hgt300"] = planes["hgt300"] + 9000.0
        calibration = {250: (-17.0, 1.1)}
        whole = _derive_cat_plane(planes, "cat250", grid, calibration).reshape(rows, columns)
        # A window across the antimeridian, touching the grid's first row.
        crop = CropWindow(source_width=columns, source_height=rows, row_start=0, column_start=20, width=8, height=5)
        cropped_grid = GridInfo(width=8, height=5, first_longitude=120.0, first_latitude=60.0,
                                longitude_step=15.0, latitude_step=-10.0, crop=crop)
        np.testing.assert_array_equal(uncropped_latitudes(cropped_grid), uncropped_latitudes(grid))
        cut = _derive_cat_plane(planes, "cat250", cropped_grid, calibration).reshape(5, 8)
        np.testing.assert_array_equal(cut, crop.take(whole))
        with self.assertRaises(ConversionError):
            _derive_cat_plane(planes, "cat250", grid, {300: (-17.0, 1.1)})


class RegistryTests(unittest.TestCase):
    def test_the_bundles_and_their_inputs(self) -> None:
        self.assertEqual(CAT_LEVELS_HPA, (300, 250, 200))
        self.assertEqual(
            DERIVED_SCALARS["cat250"],
            ("ugrd200", "vgrd200", "hgt200", "ugrd300", "vgrd300", "hgt300", "ugrd250", "vgrd250"),
        )
        self.assertEqual(cat_level("cat200"), 200)
        self.assertIsNone(cat_level("cape"))
        spec = variable_spec("cat300")
        self.assertEqual(spec.parameter_metadata()["parameterCategory"], 19)
        self.assertEqual(spec.parameter_metadata()["parameterNumber"], 29)
        self.assertEqual(PROFILES["quality"]["cat300"].metadata()["maximumCode"], 127)
        # Stacked RAW: chaining measured 50-54 % larger on real runs.
        self.assertLessEqual({"cat300", "cat250", "cat200"}, RAW_VARIABLE_IDS)

    def test_gfs_and_ecmwf_publish_and_calibrate_all_three(self) -> None:
        for model in ("gfs", "ecmwf"):
            source = source_spec(model)
            published = published_bundle_ids(source)
            self.assertEqual([b for b in published if b.startswith("cat")], ["cat300", "cat250", "cat200"])
            self.assertEqual(tuple(level for level, _mean, _std in source.cat_calibration), CAT_LEVELS_HPA)
            for bundle_id in ("cat300", "cat250", "cat200"):
                for input_id in bundle_input_ids(source, bundle_id):
                    self.assertIn(input_id, source.input_variable_ids, f"{model} {bundle_id}")
        # Other sources do not have the jet-level winds and heights.
        self.assertFalse(any(b.startswith("cat") for b in published_bundle_ids(source_spec("aifs"))))


if __name__ == "__main__":
    unittest.main()

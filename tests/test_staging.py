"""Staging the CAMEL emissivity months DEBRA reads
(xuebuild/satellite/staging.py).

The granule fetch (Earthdata, behind ``shachen.io.emissivity.
fetch_emissivity``) is never exercised: the tests inject a fetch that
returns a synthetic CAM5K30EM-shaped granule written here, so what is
held is the part that is this module's — the region table, the crop on
a north-to-south granule, the file's attributes and layout, and that
``xuebuild.satellite.ancillary`` reads the result back as a staged
month. The NetCDF half needs the ``staging`` group (xarray + netCDF4) and
the read-back needs GDAL; both skip without.
"""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import TempRoot, requires_gdal
from xuebuild.errors import ConversionError, XueError
from xuebuild.satellite import ancillary, staging
from xuebuild.satellite.staging import MARGIN_DEG, REGIONS, Region


def _native(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


XARRAY = _native("xarray")
NETCDF = XARRAY and _native("netCDF4")
requires_xarray = unittest.skipUnless(XARRAY, "xarray is not installed (uv sync --group staging)")
requires_netcdf = unittest.skipUnless(NETCDF, "the staging group (xarray + netCDF4) is not installed")

#: CAM5K30EM's hinge-point wavelengths, as its ``comment`` attribute lists
#: them; the loader interpolates the DEBRA band centres between them.
HINGES = (3.6, 4.3, 5.0, 5.8, 7.6, 8.3, 9.3, 10.8, 12.1, 14.3, 15.0, 16.0, 17.0)
#: A regional box for the crop tests; the table itself holds the globe.
GOBI = Region("gobi", (80.0, 30.0, 146.0, 55.0), "Gobi and Taklamakan through the North China Plain to Japan")


def descending_global(step: float, variables: tuple[str, ...] = ("emis_tir_86",)):
    """A global grid at ``step`` with latitude stored north to south, as
    CAMEL does, each variable the latitude in degrees (so a value says
    where it came from) and NaN on one 'water' row at 32.5° N."""
    import xarray as xr  # noqa: PLC0415

    latitudes = np.arange(90 - step / 2, -90, -step, dtype=np.float32)
    longitudes = np.arange(-180 + step / 2, 180, step, dtype=np.float32)
    plane = np.repeat(latitudes[:, None], longitudes.size, axis=1).astype(np.float32)
    plane[np.isclose(latitudes, 32.5)] = np.nan
    return xr.Dataset(
        {name: (("latitude", "longitude"), plane.copy()) for name in variables},
        coords={"latitude": latitudes, "longitude": longitudes},
    )


def write_granule(path: Path, step: float = 1.0) -> Path:
    """A CAM5K30EM-shaped granule: ``camel_emis`` on (latitude,
    longitude, spectra), north to south, the hinge wavelengths in the
    variable's comment and no coordinate for them, as the real file."""
    import xarray as xr  # noqa: PLC0415

    latitudes = np.arange(90 - step / 2, -90, -step, dtype=np.float32)
    longitudes = np.arange(-180 + step / 2, 180, step, dtype=np.float32)
    # Emissivity rises with latitude by 0.001 per degree and with hinge
    # index by 0.01, so a cell's value says where and which hinge.
    cube = 0.8 + latitudes[:, None, None] * 0.001 + np.arange(len(HINGES))[None, None, :] * 0.01
    cube = np.broadcast_to(cube, (latitudes.size, longitudes.size, len(HINGES))).astype(np.float32).copy()
    cube[np.isclose(latitudes, 32.5)] = np.nan
    dataset = xr.Dataset(
        {
            "camel_emis": (
                ("latitude", "longitude", "spectra"),
                cube,
                {"comment": "Emissivity at " + ", ".join(f"{wl:.1f}" for wl in HINGES) + " micron", "units": "none"},
            )
        },
        coords={
            "latitude": ("latitude", latitudes, {"units": "degrees north"}),
            "longitude": ("longitude", longitudes, {"units": "degrees east"}),
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_netcdf(path)
    return path


class RegionTableTest(unittest.TestCase):
    def test_the_globe_is_the_one_region(self) -> None:
        self.assertEqual(sorted(REGIONS), ["global"])
        self.assertEqual(REGIONS["global"].bbox, (-180.0, -90.0, 180.0, 90.0))
        self.assertEqual(MARGIN_DEG, 1.0)

    def test_no_region_crosses_the_antimeridian(self) -> None:
        for region in REGIONS.values():
            lon_min, lat_min, lon_max, lat_max = region.bbox
            self.assertLess(lon_min, lon_max, region.key)
            self.assertLess(lat_min, lat_max, region.key)
            self.assertGreaterEqual(lon_min, -180.0, region.key)
            self.assertLessEqual(lon_max, 180.0, region.key)
            self.assertEqual(region.key, region.key.lower())
            self.assertTrue(region.description)

    def test_regions_are_frozen(self) -> None:
        with self.assertRaises(AttributeError):
            REGIONS["global"].bbox = (0.0, 0.0, 1.0, 1.0)  # type: ignore[misc]


@requires_xarray
class SubsetTest(unittest.TestCase):
    def test_crop_is_ascending_and_covers_the_box_with_margin(self) -> None:
        cropped = staging.subset(descending_global(0.5), GOBI)
        latitudes = cropped["latitude"].values
        longitudes = cropped["longitude"].values
        self.assertTrue(np.all(np.diff(latitudes) > 0))
        self.assertTrue(np.all(np.diff(longitudes) > 0))
        # Within one cell of [79, 147] x [29, 56].
        self.assertLessEqual(abs(latitudes[0] - 29.0), 0.5)
        self.assertLessEqual(abs(latitudes[-1] - 56.0), 0.5)
        self.assertLessEqual(abs(longitudes[0] - 79.0), 0.5)
        self.assertLessEqual(abs(longitudes[-1] - 147.0), 0.5)
        self.assertTrue(np.all(latitudes >= 29.0) and np.all(latitudes <= 56.0))
        self.assertTrue(np.all(longitudes >= 79.0) and np.all(longitudes <= 147.0))
        # The rows were reordered with their values, not relabelled.
        values = cropped["emis_tir_86"].values
        finite = np.isfinite(values[:, 0])
        np.testing.assert_allclose(values[finite, 0], latitudes[finite])
        self.assertTrue(np.all(np.isnan(values[np.isclose(latitudes, 32.5)])))

    def test_global_box_keeps_the_whole_granule_ascending(self) -> None:
        granule = descending_global(0.5)
        cropped = staging.subset(granule, REGIONS["global"])
        self.assertEqual(dict(cropped.sizes), dict(granule.sizes))
        self.assertTrue(np.all(np.diff(cropped["latitude"].values) > 0))
        self.assertTrue(np.all(np.diff(cropped["longitude"].values) > 0))
        self.assertTrue(ancillary.is_global(cropped["longitude"].values))

    def test_empty_box_is_an_error(self) -> None:
        dataset = descending_global(0.5).sel(latitude=slice(10, -10))
        with self.assertRaises(ConversionError):
            staging.subset(dataset, GOBI)


class MonthsTest(unittest.TestCase):
    def test_source_month_comes_from_the_granule_name(self) -> None:
        asked = date(2026, 9, 1)
        self.assertEqual(staging.granule_month(Path("x/CAM5K30EM_202309.nc"), asked), date(2023, 9, 1))
        self.assertEqual(staging.granule_month(Path("CAM5K30EM_emis_202109_V003.nc"), asked), date(2021, 9, 1))
        with self.assertLogs(staging.LOG, level="WARNING"):
            self.assertEqual(staging.granule_month(Path("renamed.nc"), asked), asked)

    def test_default_months_are_this_month_and_the_next(self) -> None:
        self.assertEqual(staging.default_months(date(2026, 12, 15)), [date(2026, 12, 1), date(2027, 1, 1)])
        self.assertEqual(staging.default_months(date(2026, 1, 31)), [date(2026, 1, 1), date(2026, 2, 1)])
        today = datetime.now(UTC).date()
        following = (today.replace(day=28) + timedelta(days=7)).replace(day=1)
        self.assertEqual(staging.default_months(), [today.replace(day=1), following])

    def test_month_spelling(self) -> None:
        self.assertEqual(staging.parse_month("2026-09"), date(2026, 9, 1))
        for bad in ("202609", "2026-13", "2026-9", "2026-09-01"):
            with self.assertRaises(XueError):
                staging.parse_month(bad)


@requires_netcdf
class StageMonthTest(TempRoot, unittest.TestCase):
    root_prefix = "xue-staging-"

    def setUp(self) -> None:
        super().setUp()
        self.granule = write_granule(self.root / "granules" / "CAM5K30EM_202309.nc")
        self.fetched: list[tuple[date, Path]] = []

    def fetch(self, month: date, granules: Path) -> Path:
        self.fetched.append((month, granules))
        return self.granule

    def test_writes_the_staged_layout(self) -> None:
        import xarray as xr  # noqa: PLC0415

        path = staging.stage_month(GOBI, date(2026, 9, 1), self.root / "ancillary", fetch=self.fetch)
        self.assertEqual(path, self.root / "ancillary" / "camel" / "gobi" / "202609.nc")
        self.assertEqual(self.fetched, [(date(2026, 9, 1), self.root / "ancillary" / "camel-granules")])
        with xr.open_dataset(path) as staged:
            self.assertEqual(
                list(staged.data_vars), ["emis_swir_39", "emis_wv_62", "emis_tir_86", "emis_tir_104", "emis_tir_123"]
            )
            for name, variable in staged.data_vars.items():
                self.assertEqual(variable.dims, ("latitude", "longitude"), name)
                self.assertEqual(variable.dtype, np.float32, name)
                self.assertIn("band_center_um", variable.attrs, name)
                self.assertTrue(variable.encoding.get("zlib"), name)
            self.assertEqual(staged.attrs["region"], "gobi")
            self.assertEqual(staged.attrs["month"], "2026-09")
            self.assertEqual(staged.attrs["source_month"], "2023-09")
            self.assertEqual(staged.attrs["source"], staging.SOURCE)
            np.testing.assert_array_equal(staged.attrs["bbox"], [80.0, 30.0, 146.0, 55.0])
            self.assertEqual(staged.attrs["margin_deg"], 1.0)
            self.assertEqual(staged["latitude"].attrs["units"], "degrees north")
            latitudes = staged["latitude"].values
            self.assertTrue(np.all(np.diff(latitudes) > 0))
            self.assertAlmostEqual(float(latitudes[0]), 29.5)
            self.assertAlmostEqual(float(latitudes[-1]), 55.5)
            self.assertAlmostEqual(float(staged["longitude"].values[0]), 79.5)
            self.assertAlmostEqual(float(staged["longitude"].values[-1]), 146.5)
            # 10.33 µm lies between the 9.3 and 10.8 hinges (indices 6 and 7).
            row = staged["emis_tir_104"].sel(latitude=40.5).values
            expected = 0.8 + 40.5 * 0.001 + (6 + (10.33 - 9.3) / (10.8 - 9.3)) * 0.01
            np.testing.assert_allclose(row, expected, rtol=1e-5)
            self.assertTrue(np.all(np.isnan(staged["emis_tir_86"].sel(latitude=32.5).values)))
        self.assertEqual(ancillary.staged_months(self.root / "ancillary" / "camel"), {"gobi": ["202609"]})

    def test_overwrites_a_staged_month(self) -> None:
        root = self.root / "ancillary"
        path = staging.stage_month(GOBI, date(2026, 9, 1), root, fetch=self.fetch)
        before = path.stat().st_mtime_ns
        path.write_bytes(b"a bad subset")
        self.assertEqual(staging.stage_month(GOBI, date(2026, 9, 1), root, fetch=self.fetch), path)
        self.assertGreater(path.stat().st_size, 1000)
        self.assertGreaterEqual(path.stat().st_mtime_ns, before)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["202609.nc"])

    def test_stage_every_region_of_every_month(self) -> None:
        written = staging.stage(self.root / "ancillary", months=[date(2026, 9, 1), date(2026, 10, 1)], fetch=self.fetch)
        self.assertEqual(
            [str(path.relative_to(self.root / "ancillary")) for path in written],
            [f"camel/global/{month}.nc" for month in ("202609", "202610")],
        )
        self.assertEqual(ancillary.staged_months(self.root / "ancillary" / "camel"), {"global": ["202609", "202610"]})

    def test_a_granule_month_falls_back_to_the_asked_month(self) -> None:
        import xarray as xr  # noqa: PLC0415

        renamed = self.root / "granules" / "renamed.nc"
        renamed.write_bytes(self.granule.read_bytes())
        path = staging.stage_month(GOBI, date(2026, 9, 1), self.root / "ancillary", fetch=lambda month, granules: renamed)
        with xr.open_dataset(path) as staged:
            self.assertEqual(staged.attrs["source_month"], "2026-09")

    def test_cli(self) -> None:
        # basicConfig would leave a stderr handler on the root logger for
        # every later test; assertLogs attaches its own.
        with (
            mock.patch.object(staging, "_fetch_granule", self.fetch),
            mock.patch.object(staging.logging, "basicConfig"),
            self.assertLogs(staging.LOG, level="INFO") as logs,
        ):
            code = staging.main(["--root", str(self.root / "ancillary"), "--region", "global", "--month", "2026-11"])
        self.assertEqual(code, 0)
        self.assertEqual(ancillary.staged_months(self.root / "ancillary" / "camel"), {"global": ["202611"]})
        self.assertTrue(any("served by the 2023-09 granule" in line for line in logs.output), logs.output)
        self.assertEqual(staging.main(["--root", str(self.root), "--month", "2026-13"]), 2)

    @requires_gdal
    def test_read_back_as_a_staged_month(self) -> None:
        path = staging.stage_month(GOBI, date(2026, 9, 1), self.root / "ancillary", fetch=self.fetch)
        staged = ancillary.read_staged_emissivity(path)
        self.assertEqual((staged.region, staged.month, staged.source_month), ("gobi", "2026-09", "2023-09"))
        field = staged.fields["emis_tir_86"]
        self.assertEqual(field.values.shape, (27, 68))
        # North first, as every LatLonField, with the values following their rows.
        self.assertAlmostEqual(float(field.latitudes[0]), 55.5)
        self.assertAlmostEqual(float(field.longitudes[0]), 79.5)
        expected = 0.8 + 55.5 * 0.001 + (5 + (8.44 - 8.3) / (9.3 - 8.3)) * 0.01
        self.assertAlmostEqual(float(field.values[0, 0]), expected, places=5)
        self.assertTrue(np.all(np.isnan(field.values[np.isclose(field.latitudes, 32.5)])))
        by_slot = ancillary.staged_emissivity(self.root / "ancillary" / "camel", datetime(2026, 9, 14, 3, tzinfo=UTC))
        self.assertEqual([region.path for region in by_slot], [path])


if __name__ == "__main__":
    unittest.main()

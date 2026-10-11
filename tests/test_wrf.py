"""The WRF series tool (``xue wrf-series``, ``xuebuild/wrf/``): a Recast
WOOF run's ``wrfout`` files as the CF series the ``woof`` source converts.

Most cases run on a synthetic wrfout — a 6 by 5 mass-point Lambert nest at
12 km with three levels and three hourly outputs — written here with
netCDF4, so they need the ``wrf`` group and skip without it. Two cases
look at the real Fuji run under ``~/Downloads`` when it is there.
"""

from __future__ import annotations

import importlib.util
import math
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from tests._support import FIXTURES, requires_gdal
from xuebuild.errors import DownloadError, XueError
from xuebuild.observation import accepted_series_units
from xuebuild.reproject import LambertConformal
from xuebuild.variables import variable_spec

NETCDF = importlib.util.find_spec("netCDF4") is not None
REAL_RUN = Path(os.environ.get("XUE_WOOF_RUN", Path.home() / "Downloads" / "run_20294738511688ee"))

START = datetime(2026, 10, 10, 6, tzinfo=UTC)
NX, NY, NZ = 6, 5, 3
DX = 12000.0
PROJECTION = LambertConformal(
    radius=6_370_000.0, standard_parallel_1=30.0, standard_parallel_2=60.0, latitude_of_origin=35.0, central_meridian=139.0
)
CENTER = (139.0, 35.0)
#: w-level heights, so the mass levels sit at 1, 4 and 8 km: one in each
#: cloud layer.
Z_W = np.array([0.0, 2000.0, 6000.0, 10000.0])
CLDFRA_COLUMN = np.array([0.2, 0.5, 0.0])


def _coordinates() -> tuple[np.ndarray, np.ndarray]:
    xc, yc = PROJECTION.forward(*CENTER)
    xlat = np.zeros((NY, NX))
    xlong = np.zeros((NY, NX))
    for j in range(NY):
        for i in range(NX):
            x = xc + (i - (NX - 1) / 2) * DX
            y = yc + (j - (NY - 1) / 2) * DX
            xlong[j, i], xlat[j, i] = PROJECTION.inverse(x, y)
    return xlat, xlong


def write_wrfout(directory: Path, hours: range, *, latitude_shift: float = 0.0) -> None:
    """A run directory with ``experiment.toml`` and one wrfout per hour of
    ``hours`` for domain d02; ``latitude_shift`` moves XLAT off the grid the
    header declares."""
    import netCDF4

    (directory / "experiment.toml").write_text('[experiment]\nname = "synthetic"\n')
    (directory / "events.jsonl").write_text('{"command": ["/venvs/gpuwm-9.9.9-py3/bin/python3"]}\n')
    wrfout = directory / "run" / "wrfout"
    wrfout.mkdir(parents=True)
    xlat, xlong = _coordinates()
    alpha = PROJECTION.cone_constant * np.radians(xlong - PROJECTION.central_meridian)
    for hour in hours:
        valid = START + timedelta(hours=hour)
        with netCDF4.Dataset(wrfout / f"wrfout_d02_{valid:%Y-%m-%d_%H_%M_%S}", "w") as ds:
            ds.setncatts(
                {
                    "MAP_PROJ": 1, "GRID_ID": 2, "DX": DX, "DY": DX,
                    "TRUELAT1": 30.0, "TRUELAT2": 60.0, "STAND_LON": 139.0,
                    "MOAD_CEN_LAT": 35.0, "CEN_LAT": CENTER[1], "CEN_LON": CENTER[0],
                    "SIMULATION_START_DATE": START.strftime("%Y-%m-%d_%H:%M:%S"),
                }
            )  # fmt: skip
            for name, size in (("Time", None), ("DateStrLen", 19), ("west_east", NX), ("south_north", NY),
                               ("bottom_top", NZ), ("west_east_stag", NX + 1), ("south_north_stag", NY + 1),
                               ("bottom_top_stag", NZ + 1)):  # fmt: skip
                ds.createDimension(name, size)
            times = ds.createVariable("Times", "S1", ("Time", "DateStrLen"))
            times[0] = np.frombuffer(valid.strftime("%Y-%m-%d_%H:%M:%S").encode("ascii"), dtype="S1")

            def put(name: str, values: np.ndarray, dims: tuple[str, ...]) -> None:
                var = ds.createVariable(name, "f4", ("Time", *dims))
                var[0] = values

            surface = ("south_north", "west_east")
            column = np.arange(NX, dtype=float)[None, :] * np.ones((NY, 1))
            put("T2", 283.15 + 0.5 * column, surface)
            put("Q2", np.full((NY, NX), 0.01), surface)
            put("PSFC", np.full((NY, NX), 100000.0), surface)
            put("U10", np.full((NY, NX), 3.0), surface)
            put("V10", np.zeros((NY, NX)), surface)
            put("TSK", np.full((NY, NX), 290.15), surface)
            put("HGT", 100.0 + 10.0 * column, surface)
            put("PBLH", np.full((NY, NX), 800.0), surface)
            put("SWDOWN", np.full((NY, NX), 400.0), surface)
            put("RAINNC", np.full((NY, NX), float(hour * hour)), surface)
            put("RAINC", np.full((NY, NX), 0.5 * hour), surface)
            put("RAINSH", np.zeros((NY, NX)), surface)
            put("PHB", np.broadcast_to((Z_W * 9.81)[:, None, None], (NZ + 1, NY, NX)), ("bottom_top_stag", *surface))
            put("PH", np.zeros((NZ + 1, NY, NX)), ("bottom_top_stag", *surface))
            put("U", np.broadcast_to(np.arange(NX + 1, dtype=float), (NZ, NY, NX + 1)), ("bottom_top", "south_north", "west_east_stag"))
            put("V", np.broadcast_to(np.arange(NY + 1, dtype=float)[:, None], (NZ, NY + 1, NX)), ("bottom_top", "south_north_stag", "west_east"))
            cldfra = np.broadcast_to(CLDFRA_COLUMN[:, None, None], (NZ, NY, NX)).copy()
            cldfra[2, 0, 0] = 0.4
            put("CLDFRA", cldfra, ("bottom_top", *surface))
            put("COSALPHA", np.cos(alpha), surface)
            put("SINALPHA", np.sin(alpha), surface)
            put("XLAT", xlat + latitude_shift, surface)
            put("XLONG", xlong, surface)


@unittest.skipUnless(NETCDF, "netCDF4 is not installed (uv sync --group wrf)")
class DerivationTests(unittest.TestCase):
    def test_destagger_averages_neighbours(self) -> None:
        from xuebuild.wrf.derive import destagger

        u = np.arange(5, dtype=float)[None, :] * np.ones((2, 1))
        np.testing.assert_allclose(destagger(u, axis=1), np.array([[0.5, 1.5, 2.5, 3.5]] * 2))
        np.testing.assert_allclose(destagger(u.T, axis=0), np.array([[0.5, 1.5, 2.5, 3.5]] * 2).T)

    def test_rotation_turns_grid_wind_earth_relative(self) -> None:
        from xuebuild.wrf.derive import rotate_to_earth

        alpha = math.radians(30.0)
        u, v = rotate_to_earth(np.array([2.0]), np.array([0.0]), np.array([math.cos(alpha)]), np.array([math.sin(alpha)]))
        self.assertAlmostEqual(float(u[0]), 2.0 * math.cos(alpha))
        self.assertAlmostEqual(float(v[0]), 2.0 * math.sin(alpha))

    def test_dew_point_matches_bolton_by_hand(self) -> None:
        from xuebuild.wrf.derive import dew_point

        # q = 0.01, p = 1000 hPa: e = 0.01·1000/0.632 = 15.8228 hPa, then
        # Bolton eq. 10 inverted.
        e = 0.01 * 1000.0 / 0.632
        ln_e = math.log(e / 6.112)
        expected = 243.5 * ln_e / (17.67 - ln_e)
        self.assertAlmostEqual(expected, 13.8537, delta=1e-4)
        self.assertAlmostEqual(float(dew_point(np.array([0.01]), np.array([100000.0]))[0]), expected, places=9)

    def test_cloud_layers_take_the_maximum_and_the_total_random_overlap(self) -> None:
        from xuebuild.wrf.derive import cloud_covers, mass_heights

        z = mass_heights(np.zeros((4, 1, 1)), (Z_W * 9.81)[:, None, None])
        np.testing.assert_allclose(z[:, 0, 0], [1000.0, 4000.0, 8000.0])
        covers = cloud_covers(CLDFRA_COLUMN[:, None, None], z)
        self.assertAlmostEqual(float(covers["lcdc"][0, 0]), 20.0)
        self.assertAlmostEqual(float(covers["mcdc"][0, 0]), 50.0)
        self.assertAlmostEqual(float(covers["hcdc"][0, 0]), 0.0)
        self.assertAlmostEqual(float(covers["tcdc"][0, 0]), 100.0 * (1.0 - 0.8 * 0.5))


@unittest.skipUnless(NETCDF, "netCDF4 is not installed (uv sync --group wrf)")
class SyntheticRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-wrf-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.run_dir = self.root / "run"
        self.run_dir.mkdir()
        write_wrfout(self.run_dir, range(3))

    def test_target_grid_is_inscribed_and_bilinear_is_exact_on_a_plane(self) -> None:
        from xuebuild.wrf.reader import open_run, read_frame
        from xuebuild.wrf.regrid import build_sampler, inscribed_rectangle, place_grid

        run = open_run(self.run_dir, "d02")
        _, fields = read_frame(run.files[0])
        mass = place_grid(run.grid, fields["XLAT"], fields["XLONG"])
        west, south, east, north = inscribed_rectangle(mass)
        xlat, xlong = fields["XLAT"], fields["XLONG"]
        self.assertAlmostEqual(west, float(xlong[:, 0].max()), places=4)
        self.assertAlmostEqual(east, float(xlong[:, -1].min()), places=4)
        self.assertAlmostEqual(south, float(xlat[0, :].max()), places=4)
        self.assertAlmostEqual(north, float(xlat[-1, :].min()), places=4)
        sampler = build_sampler(mass, 0.05)
        self.assertTrue(np.all(sampler.longitudes > west) and np.all(sampler.longitudes < east))
        self.assertTrue(np.all(sampler.latitudes > south) and np.all(sampler.latitudes < north))
        self.assertTrue(np.all(np.diff(sampler.latitudes) > 0), "latitudes ascend like the ifshres series")
        self.assertTrue(np.all(sampler.column + sampler.fx <= NX - 1) and np.all(sampler.row + sampler.fy <= NY - 1))
        # A plane linear in the mass column is reproduced exactly at the
        # fractional column each center lands on.
        plane = np.arange(NX, dtype=float)[None, :] * np.ones((NY, 1))
        np.testing.assert_allclose(sampler.take(plane), sampler.column + sampler.fx, atol=1e-9)

    def test_placement_is_held_to_xlat_xlong(self) -> None:
        from xuebuild.wrf.reader import open_run, read_frame
        from xuebuild.wrf.regrid import place_grid

        shifted = self.root / "shifted"
        shifted.mkdir()
        write_wrfout(shifted, range(2), latitude_shift=0.001)
        run = open_run(shifted, "d02")
        _, fields = read_frame(run.files[0])
        with self.assertRaisesRegex(XueError, "misses XLAT/XLONG"):
            place_grid(run.grid, fields["XLAT"], fields["XLONG"])

    def test_convert_run_writes_the_series_contract(self) -> None:
        import netCDF4

        from xuebuild.wrf import VARIABLE_IDS, convert_run

        out = self.root / "out"
        summaries = convert_run(self.run_dir, "d02", out, step=0.05, command="xue wrf-series test")
        self.assertEqual([summary.variable_id for summary in summaries], list(VARIABLE_IDS))
        for variable_id in VARIABLE_IDS:
            with netCDF4.Dataset(out / f"woof.2026101006.{variable_id}.nc") as ds:
                self.assertEqual(ds.Conventions, "CF-1.10")
                self.assertEqual(ds.forecast_reference_time, "2026-10-10T06:00:00Z")
                self.assertIn("gpuwm 9.9.9", ds.source)
                self.assertTrue(ds.dimensions["time"].isunlimited())
                self.assertEqual(ds["time"].units, "hours since 2026-10-10 06:00:00")
                np.testing.assert_array_equal(ds["time"][:], [1.0, 2.0])
                np.testing.assert_array_equal(ds["step"][:], [1.0, 2.0])
                self.assertEqual(float(ds["forecast_reference_time"][...]), START.timestamp())
                self.assertEqual(ds["latitude"].units, "degrees_north")
                self.assertEqual(ds["longitude"].units, "degrees_east")
                self.assertTrue(np.all(np.diff(ds["latitude"][:]) > 0))
                data = ds[variable_id]
                self.assertEqual(data.dimensions, ("time", "latitude", "longitude"))
                self.assertEqual(data.dtype, np.float32)
                self.assertTrue(math.isnan(float(data._FillValue)))
                self.assertTrue(data.filters()["zlib"])
                self.assertIn(data.units, accepted_series_units(variable_spec(variable_id)))
                values = np.asarray(data[:], dtype=np.float64)
                self.assertFalse(np.isnan(values).any())
                if variable_id == "apcp":
                    # Totals h² + h/2: hour 1 falls 1.5, hour 2 falls 3.5.
                    np.testing.assert_allclose(values[0], 1.5, atol=1e-5)
                    np.testing.assert_allclose(values[1], 3.5, atol=1e-5)
                elif variable_id == "dpt2m":
                    np.testing.assert_allclose(values, 13.853, atol=1e-3)
                elif variable_id == "tmpsfc":
                    np.testing.assert_allclose(values, 17.0, atol=1e-5)
                elif variable_id == "hcdc":
                    self.assertGreater(float(values.max()), 0.0)
                    self.assertEqual(float(values.min()), 0.0)
        with netCDF4.Dataset(out / "woof.2026101006.ugrd10m.nc") as u, netCDF4.Dataset(out / "woof.2026101006.vgrd10m.nc") as v:
            np.testing.assert_allclose(np.hypot(u["ugrd10m"][:], v["vgrd10m"][:]), 3.0, atol=1e-5)
        with self.assertRaisesRegex(XueError, "--force"):
            convert_run(self.run_dir, "d02", out, step=0.05)

    def test_refuses_a_missing_hour_and_a_run_without_its_analysis(self) -> None:
        from xuebuild.wrf.reader import open_run

        (self.run_dir / "run" / "wrfout" / "wrfout_d02_2026-10-10_07_00_00").unlink()
        with self.assertRaisesRegex(XueError, "not hourly"):
            open_run(self.run_dir, "d02")
        (self.run_dir / "run" / "wrfout" / "wrfout_d02_2026-10-10_06_00_00").unlink()
        with self.assertRaisesRegex(XueError, "simulation start"):
            open_run(self.run_dir, "d02")
        with self.assertRaisesRegex(XueError, "no wrfout_d03"):
            open_run(self.run_dir, "d03")

    @requires_gdal
    def test_the_converter_reads_the_series(self) -> None:
        from xuebuild.observation import inspect_observation
        from xuebuild.sources import source_spec
        from xuebuild.wrf import convert_run

        try:
            source = source_spec("woof")
        except DownloadError:
            self.skipTest("the woof source is not registered yet (xuebuild/sources.py)")
        out = self.root / "out"
        convert_run(self.run_dir, "d02", out, step=0.05)
        series = inspect_observation(out, source)
        self.assertEqual(series.lead_seconds, [3600, 7200])
        self.assertEqual(series.frames[0]["tmp2m"].run_time, START)


@unittest.skipUnless(NETCDF and (REAL_RUN / "run" / "wrfout").is_dir(), "the Fuji run is not under ~/Downloads")
class FujiRunTests(unittest.TestCase):
    def test_d04_lands_on_the_79_by_61_grid(self) -> None:
        from xuebuild.wrf.reader import open_run, read_frame
        from xuebuild.wrf.regrid import build_sampler, place_grid

        run = open_run(REAL_RUN, "d04")
        self.assertEqual((run.grid.nx, run.grid.ny, run.grid.dx), (72, 68, 500.0))
        _, fields = read_frame(run.files[0])
        sampler = build_sampler(place_grid(run.grid, fields["XLAT"], fields["XLONG"]), 0.005)
        self.assertEqual((sampler.width, sampler.height), (79, 61))
        self.assertAlmostEqual(float(sampler.longitudes[0]), 138.540)
        self.assertAlmostEqual(float(sampler.longitudes[-1]), 138.930)
        self.assertAlmostEqual(float(sampler.latitudes[0]), 35.220)
        self.assertAlmostEqual(float(sampler.latitudes[-1]), 35.520)


@unittest.skipUnless(NETCDF, "netCDF4 is not installed (uv sync --group wrf)")
class FixtureTests(unittest.TestCase):
    def test_fixture_is_a_cut_of_the_tool_output(self) -> None:
        import netCDF4

        from xuebuild.wrf import VARIABLE_IDS

        directory = FIXTURES / "woof.2026101006"
        for variable_id in VARIABLE_IDS:
            with netCDF4.Dataset(directory / f"woof.2026101006.{variable_id}.nc") as ds:
                data = ds[variable_id]
                self.assertEqual(data.shape, (2, 16, 20))
                self.assertEqual(ds.forecast_reference_time, "2026-10-10T06:00:00Z")
                self.assertIn(data.units, accepted_series_units(variable_spec(variable_id)))
                np.testing.assert_array_equal(ds["time"][:], [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()

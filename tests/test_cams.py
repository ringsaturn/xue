"""The cams source: the Copernicus Atmosphere Monitoring Service's global
aerosol analyses at ECMWF, three-hourly on the 0.4° regular grid, from the
Atmosphere Data Store.

A series-file forecast with no feed, like ERA5: ``xue cams-series --start``
names a window's first hour as the run, and the tool (``xuebuild/cams/``)
turns the store's NetCDF — every 00Z and 12Z cycle's analysis and its +3,
+6 and +9 h steps, laid out (forecast_period, forecast_reference_time, …) —
into one CF series per variable on one three-hourly axis, PM2.5 in µg/m³
where CAMS has kg m**-3. The bundles are GEFS-Aerosols' own quantities.

``tests/fixtures/cams.2026100900/`` is hours 0–6 of the window from
2026-10-09 00Z over a 4 x 3 cell box over southern Sumatra, written by the
tool with the command in ``tests/fixtures/README.md``.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import ClassTempRoot, FIXTURES, assert_native_matches, requires_gdal, requires_native_source
from xuebuild import binconvert, observation
from xuebuild.binconvert import published_bundle_ids
from xuebuild.binformat import read_bundle
from xuebuild.cams import series as cams_series
from xuebuild.cams.series import CamsError
from xuebuild.errors import DownloadError
from xuebuild.fetch import resolve_run
from xuebuild.manifest import validate_bin_manifest
from xuebuild.quantize import PROFILES
from xuebuild.showcase import parse_case
from xuebuild.sources import SOURCES, source_spec
from xuebuild.stac import _source_prose

SERIES_DIR = FIXTURES / "cams.2026100900"
SPEC = source_spec("cams")
RUN_TIME = datetime(2026, 10, 9, 0, tzinfo=UTC)
WIDTH, HEIGHT = 4, 3
VARIABLES = ("aod", "aodorg", "pm25")


def localized(text: str) -> dict[str, str]:
    from xuebuild.showcase import LOCALES

    return {locale: text for locale in LOCALES}


def _netcdf4():
    try:
        import netCDF4  # noqa: PLC0415 - the era5 / wrf groups
    except ImportError as exc:  # pragma: no cover - CI syncs the wrf group
        raise unittest.SkipTest(f"netCDF4 is not installed ({exc})") from exc
    return netCDF4


class AdsDownload:
    """A synthetic Data Store answer: one file of every cycle's leads,
    zipped the way the store sends ``netcdf_zip``, the short names and
    units CAMS writes."""

    LATITUDES = np.array([0.0, -0.4, -0.8])
    LONGITUDES = np.array([104.0, 104.4, 104.8, 105.2])

    def __init__(self, root: Path, *, cycles: int = 2, leads: tuple[int, ...] = (0, 3, 6, 9), start: datetime = RUN_TIME) -> None:
        self.cycles = [start + timedelta(hours=12 * index) for index in range(cycles)]
        self.leads = leads
        shape = (len(leads), cycles, self.LATITUDES.size, self.LONGITUDES.size)
        ramp = np.arange(np.prod(shape), dtype=np.float32).reshape(shape) / 100
        self.values = {
            "aod550": ("~", 0.5 + ramp),
            "omaod550": ("~", 0.2 + ramp),
            "pm2p5": ("kg m**-3", (20 + ramp) * 1e-9),
        }
        netcdf4 = _netcdf4()
        path = root / "data_sfc.nc"
        with netcdf4.Dataset(path, "w", format="NETCDF4") as dataset:
            dataset.createDimension("forecast_period", len(leads))
            dataset.createDimension("forecast_reference_time", cycles)
            dataset.createDimension("latitude", self.LATITUDES.size)
            dataset.createDimension("longitude", self.LONGITUDES.size)
            period = dataset.createVariable("forecast_period", "f8", ("forecast_period",))
            period.units = "hours"
            period[:] = leads
            reference = dataset.createVariable("forecast_reference_time", "i8", ("forecast_reference_time",))
            reference.units = "seconds since 1970-01-01"
            reference[:] = [int(cycle.timestamp()) for cycle in self.cycles]
            dataset.createVariable("latitude", "f8", ("latitude",))[:] = self.LATITUDES
            dataset.createVariable("longitude", "f8", ("longitude",))[:] = self.LONGITUDES
            for short, (unit, values) in self.values.items():
                variable = dataset.createVariable(short, "f4", ("forecast_period", "forecast_reference_time", "latitude", "longitude"))
                variable.units = unit
                variable[:] = values
        self.zip = root / "download.zip"
        with zipfile.ZipFile(self.zip, "w") as archive:
            archive.write(path, path.name)
        path.unlink()


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_a_series_file_forecast_with_no_feed(self) -> None:
        self.assertEqual((SPEC.manifest_model, SPEC.product), ("CAMS", "global-composition-analysis"))
        self.assertIsNone(SPEC.latest_filename)
        self.assertTrue(SPEC.series_file)
        self.assertFalse(SPEC.observation or SPEC.live or SPEC.fetched)
        self.assertIsNone(SPEC.regrid)
        self.assertFalse(SPEC.video)
        self.assertEqual(SPEC.variant_factors, ())
        self.assertEqual(SPEC.production_grid, (900, 451))
        with self.assertRaisesRegex(DownloadError, "not fetched"):
            resolve_run("latest", hours=24, model="cams")
        self.assertEqual(list(SOURCES).index("cams"), list(SOURCES).index("era5") + 1)

    def test_the_axis_is_three_hourly_from_the_window_s_first_hour(self) -> None:
        self.assertEqual((SPEC.steps, SPEC.first_hour, SPEC.horizon_hours, SPEC.cycle_hours), (((1440, 3),), 0, 1440, 3))
        self.assertEqual(SPEC.forecast_hours(9), [0, 3, 6, 9])
        with self.assertRaises(DownloadError):
            SPEC.forecast_hours(1443)

    def test_three_aerosol_bundles_of_gefs_aerosols_quantities(self) -> None:
        self.assertEqual(SPEC.input_variable_ids, VARIABLES)
        self.assertEqual(published_bundle_ids(SPEC), VARIABLES)
        self.assertEqual(SPEC.core_bundle_ids, ("aod",))
        self.assertEqual(SPEC.statistical_processes, ())
        self.assertTrue(set(VARIABLES) <= set(published_bundle_ids(source_spec("gefsaero"))))
        self.assertEqual(cams_series.VARIABLE_IDS, VARIABLES)

    def test_the_catalog_prose_names_cams_and_the_licence(self) -> None:
        prose = _source_prose(SPEC)
        self.assertEqual(prose["license"], "CC-BY-4.0")
        self.assertIn("Contains modified Copernicus Atmosphere Monitoring Service information {year}", prose["description"])
        self.assertEqual({link["rel"] for link in prose["links"]}, {"license", "cite-as"})
        self.assertIn("Copernicus Atmosphere Monitoring Service", prose["providers"][0]["name"])


class CaseDefinitionTests(unittest.TestCase):
    def payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": "cams-demo",
            "title": localized("Demo"),
            "summary": localized("Demo summary"),
            "model": "cams",
            "dataset": "cams/demo/",
            "hours": 120,
            "bbox": [95.0, -8.0, 118.0, 8.0],
            "variables": ["aodorg", "aod", "pm25"],
        }
        payload.update(overrides)
        return payload

    def test_a_case_names_the_dataset_directory_and_no_run(self) -> None:
        spec = parse_case(self.payload())
        self.assertTrue(spec.from_dataset)
        self.assertEqual(spec.run, "")
        with self.assertRaises(Exception):
            parse_case(self.payload(run="2026100900"))
        with self.assertRaises(Exception):
            parse_case(self.payload(hours=121))  # off the three-hourly axis
        parse_case(self.payload(hours=1440))


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="xue-cams-")
        self.root = Path(self.scratch.name)

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_the_cycles_become_one_three_hourly_series_per_variable(self) -> None:
        download = AdsDownload(self.root)
        summaries = cams_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=21)
        self.assertEqual([summary.variable_id for summary in summaries], list(VARIABLES))
        netcdf4 = _netcdf4()
        for variable_id, short, factor in (("aod", "aod550", 1.0), ("aodorg", "omaod550", 1.0), ("pm25", "pm2p5", 1e9)):
            with self.subTest(variable=variable_id), netcdf4.Dataset(self.root / "out" / f"cams.2026100900.{variable_id}.nc") as dataset:
                self.assertEqual(dataset["time"].units, "hours since 2026-10-09 00:00:00")
                self.assertEqual(list(dataset["time"][:]), [0, 3, 6, 9, 12, 15, 18, 21])
                self.assertEqual(dataset[variable_id].units, "µg/m³" if variable_id == "pm25" else "1")
                # Latitudes ascend in the series; the store's run north first.
                self.assertEqual(list(dataset["latitude"][:]), [-0.8, -0.4, 0.0])
                values = download.values[short][1].astype(np.float64) * factor
                # Frame 5 (+15 h) is the 12Z cycle's +3 h step.
                np.testing.assert_allclose(dataset[variable_id][5], values[1, 1, ::-1], rtol=1e-5)
                np.testing.assert_allclose(dataset[variable_id][0], values[0, 0, ::-1], rtol=1e-5)

    def test_the_newer_cycle_wins_an_hour_two_reach(self) -> None:
        # With a +12 h step the 00Z cycle reaches 12Z, where the 12Z cycle's
        # analysis stands.
        download = AdsDownload(self.root, leads=(0, 3, 6, 9, 12))
        cams_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=12)
        netcdf4 = _netcdf4()
        with netcdf4.Dataset(self.root / "out" / "cams.2026100900.aod.nc") as dataset:
            np.testing.assert_allclose(dataset["aod"][4], download.values["aod550"][1][0, 1, ::-1], rtol=1e-6)

    def test_the_box_keeps_the_cells_whose_centres_lie_inside(self) -> None:
        download = AdsDownload(self.root)
        summaries = cams_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=3, bbox=(104.3, -0.5, 104.9, 0.1))
        self.assertEqual({(summary.width, summary.height, summary.frames) for summary in summaries}, {(2, 2, 2)})

    def test_the_window_starts_on_the_three_hourly_axis(self) -> None:
        download = AdsDownload(self.root)
        cams_series.convert(download.zip, self.root / "out", start=RUN_TIME + timedelta(hours=6), hours=6)
        self.assertTrue((self.root / "out" / "cams.2026100906.pm25.nc").is_file())
        with self.assertRaisesRegex(CamsError, "3-hourly axis"):
            cams_series.convert(download.zip, self.root / "out", start=RUN_TIME + timedelta(hours=1), hours=3)

    def test_a_frame_the_download_lacks_is_refused(self) -> None:
        download = AdsDownload(self.root)
        with self.assertRaisesRegex(CamsError, r"lacks aod550 at \+24 h"):
            cams_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=24)

    def test_a_unit_other_than_cams_s_is_refused(self) -> None:
        download = AdsDownload(self.root)
        with zipfile.ZipFile(download.zip) as archive:
            archive.extract("data_sfc.nc", self.root)
        netcdf4 = _netcdf4()
        with netcdf4.Dataset(self.root / "data_sfc.nc", "a") as dataset:
            dataset["pm2p5"].units = "ug m**-3"
        with self.assertRaisesRegex(CamsError, "pm2p5 is in ug"):
            cams_series.convert(self.root / "data_sfc.nc", self.root / "out", start=RUN_TIME, hours=3)

    def test_existing_series_are_kept_without_force(self) -> None:
        download = AdsDownload(self.root)
        cams_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=3)
        with self.assertRaisesRegex(CamsError, "--force"):
            cams_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=3)
        cams_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=3, force=True)


@requires_gdal
class SeriesTests(unittest.TestCase):
    def test_the_run_is_the_epoch_of_the_time_axis_and_starts_at_the_analysis(self) -> None:
        series = observation.inspect_observation(SERIES_DIR, SPEC)
        self.assertEqual(series.frames[0]["aod"].run_time, RUN_TIME)
        self.assertEqual(series.lead_seconds, [0, 10800, 21600])

    def test_every_input_is_a_series_of_its_own(self) -> None:
        files = observation.series_files(SERIES_DIR, SPEC.input_variable_ids)
        self.assertEqual({variable_id: path.name for variable_id, path in files.items()}, {v: f"cams.2026100900.{v}.nc" for v in VARIABLES})


@requires_gdal
class ConversionTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-cams-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                SERIES_DIR,
                cls.root / "out",
                model="cams",
                skip_video=True,
                work_root=cls.root / "work",
                run_id="2026100900",
                manifest_path=cls.root / "out" / "manifest.json",
                container=True, zarr=False,
            )

    def bundle(self, name: str):
        return read_bundle(self.root / "out" / f"{name}.xue")

    def test_the_run_publishes_every_bundle_the_source_declares(self) -> None:
        self.assertEqual(tuple(bundle["variable"] for bundle in self.report["bundles"]), VARIABLES)
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("CAMS", "global-composition-analysis"))
        self.assertEqual(manifest["runTime"], "2026-10-09T00:00:00Z")
        validate_bin_manifest(manifest, expected_hours=6, require_core_variables=True)
        self.assertFalse((self.root / "out" / "aod.half.xue").exists())

    def test_every_bundle_steps_three_hourly_from_the_analysis(self) -> None:
        for name in VARIABLES:
            with self.subTest(bundle=name):
                self.assertEqual(
                    self.bundle(name).metadata["time"],
                    {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 3, "frameStep": 3},
                )

    def test_the_values_are_the_series(self) -> None:
        netcdf4 = _netcdf4()
        for name in VARIABLES:
            codebook = PROFILES["quality"][name]
            with netcdf4.Dataset(SERIES_DIR / f"cams.2026100900.{name}.nc") as dataset:
                values = np.asarray(dataset[name][:], dtype=np.float64)[:, ::-1]  # north first in the bundle
            # decode_plane is addressed by the frame's hour, three apart here.
            for frame, hour in ((0, 0), (2, 6)):
                with self.subTest(bundle=name, hour=hour):
                    # The aerosol codebooks are log1p steps, so the bound is
                    # the codebook's own round trip, not half a linear step.
                    codes = np.asarray(self.bundle(name).decode_plane(1, hour)).reshape(HEIGHT, WIDTH)
                    np.testing.assert_array_equal(codes, codebook.quantize(values[frame]))

    @requires_native_source("cams")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, SERIES_DIR, self.root / "out", self.report, self.root / "native", model="cams")


if __name__ == "__main__":
    unittest.main()

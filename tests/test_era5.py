"""The era5 source: ECMWF's ERA5 reanalysis on single levels, hourly on the
0.25° regular grid, from the Copernicus Climate Data Store.

A series-file forecast with no feed, like the EFAS water balance: a
reanalysis has no cycle, so ``xue era5-series --start`` names a window's
first hour as the run, and the tool (``xuebuild/era5/``) turns the Data
Store's NetCDF — one file, or the zip of one file per step type it sends
when the instantaneous fields and the hourly gust maximum share a request —
into one CF series per variable, in degrees Celsius where ERA5 has kelvin.
Every hour is an analysis, so the axis starts at f000. The gust is
``fg10``, the maximum over the hour ending at the frame, declared a maximum
(``typeOfStatisticalProcessing`` 2) like ECMWF's open-data gust.

``tests/fixtures/era5.2026100800/`` is hours 0–2 of the window from
2026-10-08 00Z over a 5 x 4 cell box at the Mongolia–China border west of
Arxan, written by the tool with the command in ``tests/fixtures/README.md``.
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
from xuebuild.era5 import series as era5_series
from xuebuild.era5.series import Era5Error
from xuebuild.errors import DownloadError
from xuebuild.fetch import resolve_run
from xuebuild.manifest import validate_bin_manifest
from xuebuild.quantize import PROFILES
from xuebuild.showcase import parse_case
from xuebuild.sources import SOURCES, source_spec
from xuebuild.stac import _source_prose

SERIES_DIR = FIXTURES / "era5.2026100800"
SPEC = source_spec("era5")
RUN_TIME = datetime(2026, 10, 8, 0, tzinfo=UTC)
WIDTH, HEIGHT = 5, 4
INPUTS = ("tmp2m", "dpt2m", "ugrd10m", "vgrd10m", "gust")
BUNDLES = ("tmp2m", "dpt2m", "gust", "wind10m")


def localized(text: str) -> dict[str, str]:
    from xuebuild.showcase import LOCALES

    return {locale: text for locale in LOCALES}


def _netcdf4():
    try:
        import netCDF4  # noqa: PLC0415 - the era5 / wrf groups
    except ImportError as exc:  # pragma: no cover - CI syncs the wrf group
        raise unittest.SkipTest(f"netCDF4 is not installed ({exc})") from exc
    return netCDF4


def write_cds_file(path: Path, variables: dict[str, tuple[str, np.ndarray]], times: list[datetime], latitudes, longitudes) -> None:
    """One file the way the Data Store writes it: ``valid_time`` in seconds
    since 1970, latitudes north first, the ERA5 short names and units."""
    netcdf4 = _netcdf4()
    with netcdf4.Dataset(path, "w", format="NETCDF4") as dataset:
        dataset.createDimension("valid_time", len(times))
        dataset.createDimension("latitude", len(latitudes))
        dataset.createDimension("longitude", len(longitudes))
        valid_time = dataset.createVariable("valid_time", "i8", ("valid_time",))
        valid_time.setncatts({"units": "seconds since 1970-01-01", "calendar": "proleptic_gregorian"})
        valid_time[:] = [int(time.timestamp()) for time in times]
        dataset.createVariable("latitude", "f8", ("latitude",))[:] = latitudes
        dataset.createVariable("longitude", "f8", ("longitude",))[:] = longitudes
        for short, (unit, values) in variables.items():
            variable = dataset.createVariable(short, "f4", ("valid_time", "latitude", "longitude"))
            variable.units = unit
            variable[:] = values


class CdsDownload:
    """A synthetic Data Store answer: the instantaneous file and the gust
    maximum file, zipped the way the store sends a mixed request."""

    LATITUDES = np.array([47.5, 47.25, 47.0])
    LONGITUDES = np.array([119.0, 119.25, 119.5, 119.75])

    def __init__(self, root: Path, *, hours: int = 3, start: datetime = RUN_TIME, kelvin: float = 280.0) -> None:
        self.times = [start + timedelta(hours=hour) for hour in range(hours)]
        shape = (hours, self.LATITUDES.size, self.LONGITUDES.size)
        ramp = np.arange(np.prod(shape), dtype=np.float32).reshape(shape) / 10
        self.values = {
            "t2m": ("K", kelvin + ramp),
            "d2m": ("K", kelvin - 5 + ramp),
            "u10": ("m s**-1", 1 + ramp),
            "v10": ("m s**-1", -1 - ramp),
            "fg10": ("m s**-1", 5 + ramp),
        }
        instant = root / "data_stream-oper_stepType-instant.nc"
        maximum = root / "data_stream-oper_stepType-max.nc"
        write_cds_file(instant, {k: v for k, v in self.values.items() if k != "fg10"}, self.times, self.LATITUDES, self.LONGITUDES)
        write_cds_file(maximum, {"fg10": self.values["fg10"]}, self.times, self.LATITUDES, self.LONGITUDES)
        self.zip = root / "download.zip"
        with zipfile.ZipFile(self.zip, "w") as archive:
            archive.write(instant, instant.name)
            archive.write(maximum, maximum.name)
        instant.unlink()
        maximum.unlink()


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_a_series_file_forecast_with_no_feed(self) -> None:
        self.assertEqual((SPEC.manifest_model, SPEC.product), ("ERA5", "reanalysis-single-levels"))
        self.assertIsNone(SPEC.latest_filename)
        self.assertTrue(SPEC.series_file)
        self.assertFalse(SPEC.observation or SPEC.live or SPEC.fetched)
        self.assertIsNone(SPEC.open_meteo)
        self.assertIsNone(SPEC.regrid)
        self.assertFalse(SPEC.video)
        self.assertEqual(SPEC.variant_factors, ())
        self.assertEqual(SPEC.production_grid, (1440, 721))
        with self.assertRaisesRegex(DownloadError, "not fetched"):
            resolve_run("latest", hours=24, model="era5")
        self.assertEqual(list(SOURCES).index("era5"), list(SOURCES).index("efaswb") + 1)

    def test_the_axis_is_hourly_from_the_window_s_first_hour(self) -> None:
        self.assertEqual((SPEC.steps, SPEC.first_hour, SPEC.horizon_hours, SPEC.cycle_hours), (((240, 1),), 0, 240, 1))
        self.assertEqual(SPEC.forecast_hours(3), [0, 1, 2, 3])
        with self.assertRaises(DownloadError):
            SPEC.forecast_hours(241)

    def test_four_bundles_the_gust_declared_a_maximum(self) -> None:
        self.assertEqual(SPEC.input_variable_ids, INPUTS)
        self.assertEqual(published_bundle_ids(SPEC), BUNDLES)
        self.assertEqual(SPEC.core_bundle_ids, ("tmp2m",))
        self.assertEqual(SPEC.statistical_processes, (("gust", 2),))
        self.assertEqual(SPEC.optional_at_analysis, ())
        self.assertEqual(era5_series.VARIABLE_IDS, INPUTS)

    def test_the_catalog_prose_names_c3s_and_the_licence(self) -> None:
        prose = _source_prose(SPEC)
        self.assertEqual(prose["license"], "CC-BY-4.0")
        self.assertIn("Contains modified Copernicus Climate Change Service information {year}", prose["description"])
        self.assertEqual({link["rel"] for link in prose["links"]}, {"license", "cite-as"})
        self.assertIn("Copernicus Climate Change Service", prose["providers"][0]["name"])


class CaseDefinitionTests(unittest.TestCase):
    def payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": "era5-demo",
            "title": localized("Demo"),
            "summary": localized("Demo summary"),
            "model": "era5",
            "dataset": "era5/demo/",
            "hours": 192,
            "bbox": [117.0, 45.5, 122.0, 48.5],
            "variables": ["wind10m", "gust", "tmp2m", "dpt2m"],
        }
        payload.update(overrides)
        return payload

    def test_a_case_names_the_dataset_directory_and_no_run(self) -> None:
        spec = parse_case(self.payload())
        self.assertTrue(spec.from_dataset)
        self.assertEqual(spec.run, "")
        with self.assertRaises(Exception):
            parse_case(self.payload(run="2026100800"))
        with self.assertRaises(Exception):
            parse_case(self.payload(hours=241))
        parse_case(self.payload(hours=240))


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="xue-era5-")
        self.root = Path(self.scratch.name)

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_the_zip_becomes_one_series_per_variable_in_celsius(self) -> None:
        download = CdsDownload(self.root)
        summaries = era5_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=2)
        self.assertEqual([summary.variable_id for summary in summaries], list(INPUTS))
        netcdf4 = _netcdf4()
        for variable_id, short in (("tmp2m", "t2m"), ("dpt2m", "d2m"), ("ugrd10m", "u10"), ("gust", "fg10")):
            with self.subTest(variable=variable_id), netcdf4.Dataset(self.root / "out" / f"era5.2026100800.{variable_id}.nc") as dataset:
                self.assertEqual(dataset["time"].units, "hours since 2026-10-08 00:00:00")
                self.assertEqual(list(dataset["time"][:]), [0, 1, 2])
                self.assertEqual(dataset[variable_id].units, "degC" if variable_id in ("tmp2m", "dpt2m") else "m s-1")
                # Latitudes ascend in the series; the store's run north first.
                self.assertEqual(list(dataset["latitude"][:]), [47.0, 47.25, 47.5])
                expected = download.values[short][1][:, ::-1].astype(np.float64)
                if variable_id in ("tmp2m", "dpt2m"):
                    expected = expected - 273.15
                np.testing.assert_allclose(dataset[variable_id][:], expected, atol=1e-4)

    def test_the_box_keeps_the_cells_whose_centres_lie_inside(self) -> None:
        download = CdsDownload(self.root)
        summaries = era5_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=1, bbox=(119.2, 47.0, 119.6, 47.3))
        self.assertEqual({(summary.width, summary.height, summary.frames) for summary in summaries}, {(2, 2, 2)})

    def test_the_window_may_start_after_the_download_does(self) -> None:
        download = CdsDownload(self.root, hours=5)
        era5_series.convert(download.zip, self.root / "out", start=RUN_TIME + timedelta(hours=2), hours=2)
        self.assertTrue((self.root / "out" / "era5.2026100802.gust.nc").is_file())

    def test_an_hour_the_download_lacks_is_refused(self) -> None:
        download = CdsDownload(self.root, hours=3)
        with self.assertRaisesRegex(Era5Error, r"lacks t2m at \+3 h"):
            era5_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=3)

    def test_a_download_without_the_gust_is_refused(self) -> None:
        CdsDownload(self.root)
        with zipfile.ZipFile(self.root / "download.zip") as archive:
            archive.extract("data_stream-oper_stepType-instant.nc", self.root)
        with self.assertRaisesRegex(Era5Error, "lacks fg10"):
            era5_series.convert(self.root / "data_stream-oper_stepType-instant.nc", self.root / "out", start=RUN_TIME, hours=1)

    def test_existing_series_are_kept_without_force(self) -> None:
        download = CdsDownload(self.root)
        era5_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=1)
        with self.assertRaisesRegex(Era5Error, "--force"):
            era5_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=1)
        era5_series.convert(download.zip, self.root / "out", start=RUN_TIME, hours=1, force=True)


@requires_gdal
class SeriesTests(unittest.TestCase):
    def test_the_run_is_the_epoch_of_the_time_axis_and_starts_at_the_analysis(self) -> None:
        series = observation.inspect_observation(SERIES_DIR, SPEC)
        self.assertEqual(series.frames[0]["tmp2m"].run_time, RUN_TIME)
        self.assertEqual(series.lead_seconds, [0, 3600, 7200])

    def test_every_input_is_a_series_of_its_own(self) -> None:
        files = observation.series_files(SERIES_DIR, SPEC.input_variable_ids)
        self.assertEqual({variable_id: path.name for variable_id, path in files.items()}, {v: f"era5.2026100800.{v}.nc" for v in INPUTS})


@requires_gdal
class ConversionTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-era5-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                SERIES_DIR,
                cls.root / "out",
                model="era5",
                skip_video=True,
                work_root=cls.root / "work",
                run_id="2026100800",
                manifest_path=cls.root / "out" / "manifest.json",
                container=True, zarr=False,
            )

    def bundle(self, name: str):
        return read_bundle(self.root / "out" / f"{name}.xue")

    def test_the_run_publishes_every_bundle_the_source_declares(self) -> None:
        self.assertEqual(tuple(bundle["variable"] for bundle in self.report["bundles"]), BUNDLES)
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("ERA5", "reanalysis-single-levels"))
        self.assertEqual(manifest["runTime"], "2026-10-08T00:00:00Z")
        validate_bin_manifest(manifest, expected_hours=2, require_core_variables=True)
        self.assertFalse((self.root / "out" / "tmp2m.half.xue").exists())

    def test_every_bundle_starts_at_the_analysis(self) -> None:
        for name in BUNDLES:
            with self.subTest(bundle=name):
                self.assertEqual(
                    self.bundle(name).metadata["time"],
                    {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 3, "frameStep": 1},
                )

    def test_the_gust_alone_is_declared_a_maximum(self) -> None:
        processes = {
            name: self.bundle(name).metadata["variables"][0]["parameter"].get("typeOfStatisticalProcessing")
            for name in ("tmp2m", "dpt2m", "gust")
        }
        self.assertEqual(processes, {"tmp2m": None, "dpt2m": None, "gust": 2})

    def test_the_values_are_the_series(self) -> None:
        netcdf4 = _netcdf4()
        for name in ("tmp2m", "dpt2m", "gust"):
            codebook = PROFILES["quality"][name]
            with netcdf4.Dataset(SERIES_DIR / f"era5.2026100800.{name}.nc") as dataset:
                values = np.asarray(dataset[name][:], dtype=np.float64)[:, ::-1]  # north first in the bundle
            for hour in (0, 2):
                with self.subTest(bundle=name, hour=hour):
                    decoded = codebook.decode(np.asarray(self.bundle(name).decode_plane(1, hour)).reshape(HEIGHT, WIDTH))
                    source = np.clip(values[hour], codebook.minimum, codebook.maximum)
                    self.assertLessEqual(float(np.abs(decoded - source).max()), codebook.metadata()["scale"] / 2 + 1e-6)

    @requires_native_source("era5")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, SERIES_DIR, self.root / "out", self.report, self.root / "native", model="era5")


if __name__ == "__main__":
    unittest.main()

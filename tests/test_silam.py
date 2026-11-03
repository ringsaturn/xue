"""The silam source: SILAM v6.1, the Finnish Meteorological Institute's
global air-quality forecast at 0.2°, from FMI's AWS open data bucket.

A fetched series-file forecast like ``cmadust``, with the fetch in this
repository (``xuebuild/silam.py``): the bucket folder must hold every
field's day files through the horizon; each day file is fetched whole, read
with netCDF4 and its wanted hours taken by valid time; each variable's run
becomes one classic NetCDF series, latitudes ascending as the file has
them and the 180° column moved to the west edge, which the converters turn
north-up.

``tests/fixtures/silam.2026110300/`` is hours 1–3 of the 2026-11-03 00Z
run over a 5 x 4 cell box over Beijing (116.0–116.8°E, 39.6–40.2°N), cut
from the fetched series with the script in ``tests/fixtures/README.md``.
The box holds PM2.5 of 42 to 223 µg/m³ and PM10 of 155 to 419 µg/m³.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import ClassTempRoot, FIXTURES, TempRoot, assert_native_matches, requires_gdal, requires_native_source, stored_bundle
from xuebuild import binconvert, cmadust, feeds, fetch, observation, silam
from xuebuild.binconvert import published_bundle_ids
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import parse_run, resolve_run
from xuebuild.manifest import validate_bin_manifest
from xuebuild.model import GfsRun
from xuebuild.observation import series_unit_accepted
from xuebuild.quantize import PROFILES
from xuebuild.sources import source_spec
from xuebuild.stac import _source_prose
from xuebuild.variables import variable_spec

SERIES_DIR = FIXTURES / "silam.2026110300"
SPEC = source_spec("silam")
RUN = GfsRun(datetime(2026, 11, 3, tzinfo=UTC))
WIDTH, HEIGHT = 5, 4
BUNDLES = ("pm25", "pm10")
FILL = np.float32(-1.7448305e09)
MISSING = np.float32(-9.99999e14)
#: The file's time unit: a reference two days before the folder's date.
REFERENCE = datetime(2026, 11, 1, tzinfo=UTC)

#: A small grid the fetch is held to in place of the 1800 x 897 one: the
#: file's columns start one step east of the series', as the 0.2° grid's do.
TINY = {"FILE_WEST": 0.2, "GRID_WEST": 0.0, "GRID_SOUTH": 39.6, "GRID_WIDTH": WIDTH, "GRID_HEIGHT": HEIGHT}


def _netcdf4():
    try:
        import netCDF4  # noqa: PLC0415 - the silam / wrf groups
    except ImportError as exc:
        raise unittest.SkipTest(f"netCDF4 is not installed ({exc})") from exc
    return netCDF4


def day_file(
    variable_id: str,
    day: int,
    *,
    run: datetime = RUN.time,
    unit: str = "ug/m3",
    south: float = TINY["GRID_SOUTH"],
    time_units: str = f"seconds since {REFERENCE:%Y-%m-%d %H:%M:%S} UTC",
    fill_at: tuple[int, int, int] | None = None,
) -> tuple[bytes, np.ndarray]:
    """A day file laid out as FMI writes one (``time``, ``lat``, ``lon``,
    24 hourly frames from +1 h of the day, float32 coordinates widened to
    double, a fill and a missing value), and the planes as written."""
    netcdf4 = _netcdf4()
    hours = np.arange(day * 24 + 1, day * 24 + 25)
    rows, columns = np.meshgrid(np.arange(HEIGHT), np.arange(WIDTH), indexing="ij")
    planes = np.stack([(rows * 10 + columns + hour * 100).astype(np.float32) for hour in hours])
    if variable_id == "pm10":
        planes = planes * np.float32(2)
    if fill_at is not None:
        planes[fill_at] = FILL
        planes[fill_at[0], fill_at[1], (fill_at[2] + 1) % WIDTH] = MISSING
    name = silam.FILE_VARIABLES[variable_id]
    with tempfile.TemporaryDirectory(prefix="xue-silam-") as scratch:
        path = Path(scratch) / "day.nc"
        with netcdf4.Dataset(path, "w", format="NETCDF4") as dataset:
            dataset.createDimension("time", None)
            dataset.createDimension("lat", HEIGHT)
            dataset.createDimension("lon", WIDTH)
            time = dataset.createVariable("time", "f8", ("time",))
            time.units = time_units
            time.cell_methods = "time: mean"
            time[:] = [((run + timedelta(hours=int(hour))) - REFERENCE).total_seconds() for hour in hours]
            lat = dataset.createVariable("lat", "f4", ("lat",))
            lat[:] = np.float32(south + 0.2 * np.arange(HEIGHT))
            lon = dataset.createVariable("lon", "f4", ("lon",))
            lon[:] = np.float32(TINY["FILE_WEST"] + 0.2 * np.arange(WIDTH))
            variable = dataset.createVariable(name, "f4", ("time", "lat", "lon"), zlib=True, fill_value=FILL)
            variable.missing_value = MISSING
            variable.units = unit
            variable.cell_methods = "hybrid: mean time: mean"
            variable[:] = planes
        return path.read_bytes(), planes


class Bucket:
    """The bucket's listing and objects in memory, recorded."""

    def __init__(self, files: dict[str, bytes], *, extra: tuple[str, ...] = ()) -> None:
        self.files = files
        self.extra = extra
        self.listings: list[str] = []
        self.gets: list[str] = []

    def listing(self, url: str) -> str:
        self.listings.append(url)
        keys = [*self.files, *self.extra, f"global/{RUN.time:%Y%m%d}/silam_glob_v6_1_{RUN.time:%Y%m%d}_O3_d0.nc"]
        contents = "".join(f"<Contents><Key>{key}</Key><Size>1</Size></Contents>" for key in keys)
        return (
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Name>fmi-opendata-silam-surface-netcdf</Name><IsTruncated>false</IsTruncated>{contents}</ListBucketResult>"
        )

    def get(self, url: str) -> bytes:
        self.gets.append(url)
        return self.files[url.removeprefix(f"{silam.BASE_URL}/")]


def bucket_for(variables: tuple[str, ...] = BUNDLES, days: tuple[int, ...] = (0,)) -> tuple[Bucket, dict[str, dict[int, np.ndarray]]]:
    files: dict[str, bytes] = {}
    planes: dict[str, dict[int, np.ndarray]] = {}
    for variable_id in variables:
        planes[variable_id] = {}
        for day in days:
            payload, written = day_file(variable_id, day)
            files[silam.file_key(RUN, variable_id, day)] = payload
            planes[variable_id][day] = written
    return Bucket(files), planes


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_a_fetched_series_file_forecast(self) -> None:
        self.assertEqual((SPEC.manifest_model, SPEC.product, SPEC.latest_filename), ("SILAM", "silam-glob02", "latest-silam.json"))
        self.assertTrue(SPEC.series_file and SPEC.fetched and SPEC.live)
        self.assertFalse(SPEC.observation)
        self.assertIsNone(SPEC.open_meteo)
        self.assertIsNone(SPEC.regrid)
        self.assertFalse(SPEC.video)
        self.assertEqual(SPEC.variant_factors, (2,))

    def test_the_axis_is_hourly_from_1_to_72_then_three_hourly_to_120(self) -> None:
        self.assertEqual((SPEC.steps, SPEC.first_hour, SPEC.horizon_hours, SPEC.cycle_hours), (((72, 1), (120, 3)), 1, 120, 24))
        hours = SPEC.forecast_hours(120)
        self.assertEqual(len(hours), 88)
        self.assertEqual(hours[:2], [1, 2])
        self.assertEqual(hours[70:74], [71, 72, 75, 78])
        with self.assertRaises(DownloadError):
            SPEC.forecast_hours(73)
        with self.assertRaises(DownloadError):
            parse_run("2026110312", "silam")

    def test_it_publishes_the_surface_particulates(self) -> None:
        self.assertEqual(SPEC.input_variable_ids, BUNDLES)
        self.assertEqual(published_bundle_ids(SPEC), BUNDLES)
        self.assertEqual(SPEC.core_bundle_ids, ("pm25",))
        # An hourly mean at every frame, the three-hourly ones too: not a
        # statistic over the step, so none is declared.
        self.assertEqual(SPEC.statistical_processes, ())
        self.assertEqual(set(silam.FILE_VARIABLES), set(BUNDLES))

    def test_the_grid_is_the_model_s_own_from_180_west(self) -> None:
        self.assertEqual(SPEC.production_grid, (silam.GRID_WIDTH, silam.GRID_HEIGHT))
        self.assertEqual(SPEC.production_grid, (1800, 897))
        latitudes, longitudes = silam.grid_coordinates()
        self.assertEqual((latitudes[0], latitudes[-1], longitudes[0], longitudes[-1]), (-89.6, 89.6, -180.0, 179.8))
        file_latitudes, file_longitudes = silam.file_coordinates()
        np.testing.assert_array_equal(file_latitudes, latitudes)
        self.assertEqual((file_longitudes[0], file_longitudes[-1]), (-179.8, 180.0))
        np.testing.assert_allclose(np.roll(file_longitudes, 1)[1:], longitudes[1:])
        self.assertEqual(SPEC.tile, (60, 69))
        self.assertEqual((1800 % 60, 897 % 69), (0, 0))

    def test_the_catalog_prose_names_fmi_aws_and_cc_by(self) -> None:
        prose = _source_prose(SPEC)
        self.assertEqual(prose["license"], "CC-BY-4.0")
        self.assertIn("Finnish Meteorological Institute", prose["description"])
        self.assertIn("CC BY 4.0", prose["description"])
        self.assertEqual([provider["name"] for provider in prose["providers"][:2]], ["Finnish Meteorological Institute", "Amazon Web Services"])
        (licence,) = [link for link in prose["links"] if link["rel"] == "license"]
        self.assertIn("Creative Commons Attribution 4.0", licence["title"])


class VariableTests(unittest.TestCase):
    def test_the_series_unit_is_one_the_converter_reads(self) -> None:
        for variable_id in BUNDLES:
            self.assertTrue(series_unit_accepted(variable_spec(variable_id), silam.SERIES_UNIT), variable_id)


class NamingTests(unittest.TestCase):
    def test_a_key_names_the_folder_date_the_field_and_the_day(self) -> None:
        self.assertEqual(silam.file_key(RUN, "pm25", 0), "global/20261103/silam_glob_v6_1_20261103_PM25_d0.nc")
        self.assertEqual(silam.file_key(RUN, "pm10", 4), "global/20261103/silam_glob_v6_1_20261103_PM10_d4.nc")
        self.assertEqual(
            silam.file_url(silam.file_key(RUN, "pm25", 0)),
            "https://fmi-opendata-silam-surface-netcdf.s3.amazonaws.com/global/20261103/silam_glob_v6_1_20261103_PM25_d0.nc",
        )

    def test_a_lead_hour_falls_in_its_day(self) -> None:
        self.assertEqual([silam.day_of(hour) for hour in (1, 24, 25, 48, 72, 75, 96, 97, 120)], [0, 0, 1, 1, 2, 3, 3, 4, 4])
        self.assertEqual(silam.days_for(SPEC.forecast_hours(120)), [0, 1, 2, 3, 4])
        self.assertEqual(silam.days_for(SPEC.forecast_hours(3)), [0])
        for hour in (0, 121):
            with self.assertRaises(ValueError):
                silam.day_of(hour)


class ListingTests(unittest.TestCase):
    def test_the_listing_names_the_run_s_day_files(self) -> None:
        bucket, _ = bucket_for(days=(0,))
        self.assertTrue(silam.run_is_complete(SPEC, RUN, 3, fetch=bucket.listing))
        self.assertFalse(silam.run_is_complete(SPEC, RUN, 25, fetch=bucket.listing))
        self.assertEqual(
            silam.missing_files(SPEC, RUN, 25, silam.list_run(RUN, fetch=bucket.listing)),
            ["global/20261103/silam_glob_v6_1_20261103_PM25_d1.nc", "global/20261103/silam_glob_v6_1_20261103_PM10_d1.nc"],
        )
        self.assertTrue(bucket.listings[0].startswith(f"{silam.BASE_URL}/?list-type=2&prefix=global%2F20261103%2F"))

    def test_a_page_that_is_not_a_listing_is_an_error_not_an_empty_run(self) -> None:
        with self.assertRaisesRegex(DownloadError, "not XML"):
            silam.list_run(RUN, fetch=lambda url: "<html")
        with self.assertRaisesRegex(DownloadError, "not an S3 listing"):
            silam.list_run(RUN, fetch=lambda url: "<html></html>")

    def test_resolve_run_latest_walks_back_over_the_daily_cycles(self) -> None:
        now = datetime(2026, 11, 3, 4, 0, tzinfo=UTC)
        with mock.patch.object(
            silam, "run_is_complete", side_effect=lambda spec, run, hours, **kwargs: run.id == "2026110200"
        ) as probe:
            run = resolve_run("latest", hours=120, now=now, model="silam")
        self.assertEqual(run.id, "2026110200")
        self.assertEqual([call.args[1].id for call in probe.call_args_list], ["2026110300", "2026110200"])
        with mock.patch.object(silam, "run_is_complete", return_value=False):
            with self.assertRaisesRegex(DownloadError, "incomplete"):
                resolve_run("2026110300", hours=120, model="silam")


class DayFileTests(unittest.TestCase):
    def read(self, payload: bytes, hours: list[int], **options: object) -> dict[int, np.ndarray]:
        with mock.patch.multiple(silam, **TINY):
            return silam.read_day(payload, RUN, "pm25", hours, label="day", **options)  # type: ignore[arg-type]

    def test_the_wanted_hours_are_taken_by_valid_time_the_fill_nan_and_the_east_column_rolled_west(self) -> None:
        payload, planes = day_file("pm25", 0, fill_at=(1, 2, 3))
        read = self.read(payload, [2, 3])
        self.assertEqual(sorted(read), [2, 3])
        expected = planes[1].copy()
        expected[2, 3] = np.nan
        expected[2, 4] = np.nan
        np.testing.assert_array_equal(read[2], np.roll(expected, 1, axis=-1))
        np.testing.assert_array_equal(read[3], np.roll(planes[2], 1, axis=-1))
        self.assertEqual(read[2].dtype, np.float32)
        unrolled = self.read(payload, [3], roll=False)
        np.testing.assert_array_equal(unrolled[3], planes[2])

    def test_a_file_off_the_product_s_grid_time_or_unit_is_refused(self) -> None:
        for payload, hours, message in (
            (day_file("pm25", 0, south=39.8)[0], [2], "lat is not the product's"),
            (day_file("pm25", 1)[0], [2], "no frame valid at 2026-11-03T02:00Z"),
            (day_file("pm25", 0, run=datetime(2026, 11, 2, tzinfo=UTC))[0], [2], r"\+2 h of run 2026110300"),
            (day_file("pm25", 0, unit="kg/m3")[0], [2], "PM25 is in kg/m3, not ug/m3"),
            (day_file("pm25", 0, time_units="hours since 2026-11-01")[0], [2], "not seconds since a UTC instant"),
            (day_file("pm10", 0)[0], [2], "carries no PM25"),
            (b"<html>busy</html>", [2], "not a NetCDF-4 file"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(DownloadError, message):
                self.read(payload, hours)


class FetchTests(TempRoot, unittest.TestCase):
    root_prefix = "xue-silam-"

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.multiple(silam, **TINY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_fetch(self, bucket: Bucket, hours: int = 3, **options: object) -> list[Path]:
        return silam.fetch_run(
            SPEC,
            RUN,
            hours,
            self.root,
            force=bool(options.pop("force", False)),
            input_ids=options.pop("input_ids", None),  # type: ignore[arg-type]
            fetch_text=bucket.listing,
            fetch_bytes=bucket.get,
        )

    def test_the_fetch_writes_one_series_per_variable_and_a_record(self) -> None:
        bucket, planes = bucket_for(days=(0, 1))
        paths = self.run_fetch(bucket, hours=26)
        destination = self.root / "silam.2026110300"
        self.assertEqual(paths, [destination / "silam.2026110300.pm25.nc", destination / "silam.2026110300.pm10.nc"])
        for variable_id in BUNDLES:
            data = (destination / f"silam.2026110300.{variable_id}.nc").read_bytes()
            header = cmadust.parse_header(data)
            self.assertEqual(header.attributes["forecast_reference_time"], "2026-11-03T00:00:00Z")
            self.assertEqual(header.attributes["institution"], "Finnish Meteorological Institute")
            self.assertEqual(header.variables[variable_id].attributes["units"], "ug/m3")
            self.assertEqual(header.variables[variable_id].attributes["long_name"], silam.LONG_NAMES[variable_id])
            read = lambda url, span: data[span.start : span.end + 1]  # noqa: E731
            np.testing.assert_array_equal(cmadust.read_variable("s", header, "time", read), list(range(1, 27)))
            longitudes = cmadust.read_variable("s", header, "longitude", read)
            self.assertEqual((longitudes[0], longitudes[-1]), (0.0, 0.8))
            series = cmadust.read_variable("s", header, variable_id, read)
            for index, hour in enumerate(range(1, 27)):
                day, offset = divmod(hour - 1, 24)
                np.testing.assert_array_equal(series[index], np.roll(planes[variable_id][day][offset], 1, axis=-1), f"{variable_id} +{hour} h")
        record = json.loads((destination / "fetch.json").read_text())
        self.assertEqual((record["model"], record["run"], record["hours"], record["frames"]), ("silam", "2026110300", 26, 26))
        # One GET per field and day.
        self.assertEqual(len(bucket.gets), 4)

    def test_a_day_file_the_folder_lacks_is_an_incomplete_run(self) -> None:
        bucket, _ = bucket_for(days=(0,))
        with self.assertRaisesRegex(DownloadError, r"incomplete: the bucket holds no global/20261103/silam_glob_v6_1_20261103_PM25_d1\.nc"):
            self.run_fetch(bucket, hours=25)
        self.assertEqual(bucket.gets, [])

    def test_a_job_fetches_only_its_own_bundles_inputs_and_keeps_what_is_on_disk(self) -> None:
        bucket, _ = bucket_for(("pm10",))
        paths = self.run_fetch(bucket, input_ids=("pm10",))
        self.assertEqual([path.name for path in paths], ["silam.2026110300.pm10.nc"])
        self.assertEqual(len(bucket.gets), 1)
        bucket.gets.clear()
        self.run_fetch(bucket, input_ids=("pm10",))
        self.assertEqual(bucket.gets, [])
        self.run_fetch(bucket, input_ids=("pm10",), force=True)
        self.assertEqual(len(bucket.gets), 1)
        with self.assertRaisesRegex(DownloadError, "publishes"):
            self.run_fetch(bucket, input_ids=("pm10dust",))

    def test_the_dispatch_reaches_the_module(self) -> None:
        with mock.patch.object(silam, "fetch_run", return_value=[]) as fetch_run:
            fetch.fetch_run(RUN, 3, self.root, model="silam", input_ids=("pm25",))
        self.assertEqual(fetch_run.call_args.kwargs["input_ids"], ("pm25",))
        self.assertIs(fetch_run.call_args.kwargs["fetch_bytes"], feeds.fetch_silam_file)
        self.assertIs(fetch_run.call_args.kwargs["fetch_text"], fetch.fetch_text)


@requires_gdal
class SeriesTests(unittest.TestCase):
    def test_the_run_is_the_epoch_of_the_time_axis(self) -> None:
        series = observation.inspect_observation(SERIES_DIR, SPEC)
        self.assertEqual(series.frames[0]["pm25"].run_time, RUN.time)
        self.assertEqual(series.lead_seconds, [3600, 7200, 10800])

    def test_every_input_is_a_series_of_its_own(self) -> None:
        files = observation.series_files(SERIES_DIR, SPEC.input_variable_ids)
        self.assertEqual({v: path.name for v, path in files.items()}, {v: f"silam.2026110300.{v}.nc" for v in BUNDLES})


@requires_gdal
class ConversionTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-silam-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                SERIES_DIR,
                cls.root / "out",
                model="silam",
                work_root=cls.root / "work",
                run_id="2026110300",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    def bundle(self, name: str):
        return stored_bundle(self.root / "out", name)

    def series(self, name: str) -> np.ndarray:
        data = (SERIES_DIR / f"silam.2026110300.{name}.nc").read_bytes()
        header = cmadust.parse_header(data)
        values = cmadust.read_variable("fixture", header, name, lambda url, span: data[span.start : span.end + 1])
        return np.asarray(values, dtype=np.float64)[:, ::-1]  # north first in the bundle

    def test_the_run_publishes_every_bundle_the_source_declares(self) -> None:
        self.assertEqual(tuple(bundle["variable"] for bundle in self.report["bundles"]), BUNDLES)
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("SILAM", "silam-glob02"))
        self.assertEqual(manifest["runTime"], "2026-11-03T00:00:00Z")
        validate_bin_manifest(manifest, expected_hours=3, require_core_variables=True)

    def test_the_grid_is_the_fixture_s_box_north_up(self) -> None:
        grid = self.bundle("pm25").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (WIDTH, HEIGHT))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (116.0, 40.2))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.2, -0.2))
        self.assertEqual(grid["rowOrder"], "north-to-south")
        self.assertFalse(grid["wrapLongitude"])

    def test_the_particulates_carry_their_total_aerosol_identity(self) -> None:
        for name, number in (("pm25", 193), ("pm10", 192)):
            variable = self.bundle(name).metadata["variables"][0]
            self.assertEqual(variable["unit"], "µg/m³")
            self.assertEqual((variable["parameter"]["parameterCategory"], variable["parameter"]["parameterNumber"]), (13, number))
            self.assertEqual(variable["aerosol"]["aerosolType"], 62000)

    def test_the_values_are_the_series(self) -> None:
        for name in BUNDLES:
            codebook = PROFILES["quality"][name]
            values = self.series(name)
            for index in (0, 2):
                with self.subTest(bundle=name, frame=index):
                    codes = np.asarray(self.bundle(name).decode_plane(1, index)).reshape(HEIGHT, WIDTH)
                    np.testing.assert_array_equal(codes, codebook.quantize(values[index]))

    def test_the_box_is_on_the_scale(self) -> None:
        pm25, pm10 = self.series("pm25"), self.series("pm10")
        self.assertGreater(float(pm25.min()), 30.0)
        self.assertLess(float(pm10.max()), 500.0)
        self.assertTrue(np.all(pm10 >= pm25))

    def test_a_complete_build_wants_the_whole_published_grid(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "1800x897 grid"):
                binconvert.convert_bin(
                    SERIES_DIR,
                    self.root / "complete",
                    model="silam",
                    require_complete=True,
                    expected_hours=3,
                )

    @requires_native_source("silam")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, SERIES_DIR, self.root / "out", self.report, self.root / "native", model="silam")


if __name__ == "__main__":
    unittest.main()

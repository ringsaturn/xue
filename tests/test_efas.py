"""The efas and efaswb sources: Copernicus EFAS v5 river discharge, the
HRES forecast and the water balance, on the EFAS 1/60° grid over Europe.

Both are series-file forecasts with no feed, like the WOOF nest: the Early
Warning Data Store publishes forecasts thirty days late, so a run is
requested by hand, converted by ``xue efas-series`` (``xuebuild/efas/``)
into one CF series per variable and reached only through showcase cases
naming the directory. The axis is six-hourly from f006 to f120, since
``dis06`` is the mean over the six hours ending at the frame (declared a
mean, ``typeOfStatisticalProcessing`` 0). Three bundles: the discharge on a
log codebook, its return-period class against EFAS v5.1's thresholds
(``disrp``, a class codebook 0–5 whose code 0 is "not evaluated", produced
by ``xue``) and the static upstream area repeated on every frame
(``upstream``, produced by ``xue``). The series carry no NaN: a cell off
the rivers, or one EFAS does not classify, is the declared fill -9999,
which every river variable names in ``fill_values`` and both encoders fold
to the bottom of the range, as the soil layers' 9999 is folded.

``tests/fixtures/efas.2024102712/`` and ``efaswb.2024102600/`` are 21 x 15
cell windows over the Rambla del Poyo and the lower Turia, every frame of
the 2024-10-27 12Z HRES cycle and of the water balance from 2024-10-26
00Z, written by the tool with the commands in ``tests/fixtures/README.md``.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import UTC, datetime, timedelta
from unittest import mock

import numpy as np

from tests._support import ClassTempRoot, FIXTURES, assert_native_matches, registry_entry, requires_gdal, requires_native_source
from xuebuild import binconvert, observation
from xuebuild.binconvert import published_bundle_ids
from xuebuild.binformat import read_bundle
from xuebuild.efas import series as efas_series
from xuebuild.efas.series import Message, Window, return_period_class
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import resolve_run
from xuebuild.manifest import validate_bin_manifest
from xuebuild.quantize import PROFILES, PrecipitationCodebook, TemperatureCodebook
from xuebuild.showcase import parse_case
from xuebuild.sources import SOURCES, source_spec
from xuebuild.stac import _source_prose
from xuebuild.model import PlaneSource
from xuebuild.variables import EFAS_SERIES_FILL, RIVER_VARIABLE_IDS, variable_spec

RIVER_REGISTRY = FIXTURES / "river-registry.json"
BUNDLES = ("dis06", "disrp", "upstream")
HOURS = list(range(6, 121, 6))
WIDTH, HEIGHT = 21, 15
#: The cell standing for the SAIH gauge where the Rambla del Poyo crosses
#: the A-3 (196 km² upstream), as (row from the north, column) in the
#: fixtures' window.
POYO_A3 = (4, 9)
RUNS = {
    "efas": (FIXTURES / "efas.2024102712", "2024102712", datetime(2024, 10, 27, 12, tzinfo=UTC)),
    "efaswb": (FIXTURES / "efaswb.2024102600", "2024102600", datetime(2024, 10, 26, 0, tzinfo=UTC)),
}


def localized(text: str) -> dict[str, str]:
    from xuebuild.showcase import LOCALES

    return {locale: text for locale in LOCALES}


class SourceRegistryTests(unittest.TestCase):
    def test_both_are_series_file_forecasts_with_no_feed(self) -> None:
        expected = {"efas": ("EFAS-v5", "hres", 12), "efaswb": ("EFAS-v5-WB", "water_balance", 6)}
        for source_id, (model, product, cycle_hours) in expected.items():
            spec = source_spec(source_id)
            with self.subTest(source=source_id):
                self.assertEqual((spec.manifest_model, spec.product, spec.cycle_hours), (model, product, cycle_hours))
                self.assertIsNone(spec.latest_filename)
                self.assertTrue(spec.series_file)
                self.assertFalse(spec.observation or spec.live or spec.fetched)
                self.assertIsNone(spec.window_hours)
                self.assertIsNone(spec.open_meteo)
                self.assertIsNone(spec.regrid)
                self.assertFalse(spec.video)
                self.assertEqual(spec.production_grid, (4530, 2970))
                self.assertEqual(spec.tile, (64, 64))
                self.assertEqual(spec.variant_factors, ())
                self.assertEqual(spec.companion_files, ())
                with self.assertRaisesRegex(DownloadError, "not fetched"):
                    resolve_run("latest", hours=120, model=source_id)
        self.assertLess(list(SOURCES).index("woof"), list(SOURCES).index("efas"))

    def test_the_axis_is_six_hourly_from_the_first_mean(self) -> None:
        for source_id in RUNS:
            spec = source_spec(source_id)
            with self.subTest(source=source_id):
                self.assertEqual((spec.steps, spec.first_hour, spec.horizon_hours), (((120, 6),), 6, 120))
                self.assertEqual(spec.forecast_hours(120), HOURS)
                self.assertEqual(spec.forecast_hours(12), [6, 12])
                for off_axis in (0, 9, 126):
                    with self.assertRaises(DownloadError):
                        spec.forecast_hours(off_axis)

    def test_three_bundles_the_discharge_declared_a_mean(self) -> None:
        self.assertEqual(RIVER_VARIABLE_IDS, BUNDLES)
        for source_id in RUNS:
            spec = source_spec(source_id)
            with self.subTest(source=source_id):
                self.assertEqual(published_bundle_ids(spec), BUNDLES)
                self.assertEqual(spec.input_variable_ids, BUNDLES)
                self.assertEqual(spec.core_bundle_ids, ("dis06",))
                self.assertEqual(spec.statistical_processes, (("dis06", 0),))
                self.assertEqual(spec.optional_at_analysis, ())
                self.assertFalse(spec.accumulated_precipitation or spec.averaged_precipitation or spec.interval_precipitation)

    def test_the_identities(self) -> None:
        dis06, disrp, upstream = (variable_spec(variable_id) for variable_id in BUNDLES)
        self.assertEqual((dis06.label, dis06.output_unit, dis06.value_range), ("River discharge, 6 h mean", "m³/s", (0, 100000)))
        self.assertEqual(
            (dis06.grib2_discipline, dis06.grib2_category, dis06.grib2_number, dis06.grib2_level_type, dis06.grib2_level_value),
            (1, 0, 7, 1, None),
        )
        self.assertEqual(dis06.grib2_statistical, 0)
        self.assertIsNone(dis06.producer_id)
        self.assertEqual((disrp.label, disrp.output_unit, disrp.value_range), ("Return-period class", "1", (0, 5)))
        self.assertEqual((disrp.grib2_discipline, disrp.grib2_category, disrp.grib2_number, disrp.grib2_level_type), (1, 192, 1, 1))
        self.assertEqual((upstream.label, upstream.output_unit, upstream.value_range), ("Upstream area", "km²", (0, 10000000)))
        self.assertEqual((upstream.grib2_discipline, upstream.grib2_category, upstream.grib2_number, upstream.grib2_level_type), (1, 192, 2, 1))
        self.assertEqual((disrp.producer_id, upstream.producer_id), ("xue", "xue"))
        for variable_id in BUNDLES:
            self.assertEqual(observation.accepted_series_units(variable_spec(variable_id)), (variable_spec(variable_id).output_unit,))
            # The series' fill is declared, and lands on the bottom of the
            # range: no river is zero discharge and zero area, an
            # unevaluated cell class 0.
            self.assertEqual(variable_spec(variable_id).fill_values, (EFAS_SERIES_FILL,))
            self.assertEqual(variable_spec(variable_id).value_range[0], 0)
        self.assertEqual(EFAS_SERIES_FILL, -9999.0)

    def test_the_codebooks(self) -> None:
        for profile in PROFILES:
            with self.subTest(profile=profile):
                dis06, disrp, upstream = (PROFILES[profile][variable_id] for variable_id in BUNDLES)
                self.assertIsInstance(dis06, PrecipitationCodebook)
                self.assertEqual((dis06.trace, dis06.scale, dis06.maximum), (0.1, 10.0, 100000.0))
                self.assertIsInstance(upstream, PrecipitationCodebook)
                self.assertEqual((upstream.trace, upstream.scale, upstream.maximum), (1.0, 100.0, 1.0e7))
                self.assertEqual(disrp, TemperatureCodebook(minimum=0.0, maximum=5.0, step=1.0, name="disrp"))
                self.assertEqual(disrp.maximum_code, 5)
                compact = profile == "compact"
                for book in (dis06, upstream):
                    self.assertEqual((book.maximum_code, book.overflow_code, book.nodata_code), (125, 126, 127) if compact else (253, 254, 255))
        self.assertEqual(PROFILES["balanced"]["dis06"], PROFILES["quality"]["dis06"])
        # The class codes are the classes, and the fill is code 0.
        np.testing.assert_array_equal(PROFILES["quality"]["disrp"].quantize(np.arange(6.0)), np.arange(6))
        fold = PlaneSource(fill_values=variable_spec("disrp").fill_values, fill_replacement=0.0)
        self.assertEqual(int(PROFILES["quality"]["disrp"].quantize(fold.apply_fill(np.array([EFAS_SERIES_FILL])))[0]), 0)

    def test_the_committed_river_registry_still_describes_this_encoder(self) -> None:
        pinned = json.loads(RIVER_REGISTRY.read_text(encoding="utf-8"))
        actual = {variable_id: registry_entry(variable_id) for variable_id in RIVER_VARIABLE_IDS}
        self.assertEqual(
            pinned,
            actual,
            "the river registry moved; the Rust encoder and the frontend read the same fixture, so "
            "regenerate tests/fixtures/river-registry.json deliberately (tests/fixtures/README.md) "
            "and change every implementation together",
        )
        self.assertEqual(list(pinned), list(RIVER_VARIABLE_IDS))

    def test_the_catalog_prose_carries_the_licence_credit_and_disclaimer(self) -> None:
        for source_id in RUNS:
            prose = _source_prose(source_spec(source_id))
            with self.subTest(source=source_id):
                self.assertIn("EFAS v5", prose["title"])
                self.assertIn("Contains modified Copernicus Emergency Management Service information 2024", prose["description"])
                self.assertIn("EFAS data are for information only and do not constitute a flood warning", prose["description"])
                self.assertIn("showcase cases only", prose["description"])
                self.assertEqual(prose["license"], "other")
                self.assertEqual([link["title"] for link in prose["links"]], ["CEMS-FLOODS datasets licence"])
                self.assertEqual(
                    [provider["name"] for provider in prose["providers"]],
                    ["ECMWF / Copernicus Emergency Management Service", "Xue"],
                )


class ClassRuleTests(unittest.TestCase):
    """The return-period class ``xue efas-series`` derives, on synthetic
    cells: thresholds 2, 3, 5, 9 m³/s, upstream 100 km²."""

    THRESHOLDS = tuple(np.full(6, value) for value in (2.0, 3.0, 5.0, 9.0))

    def classes(self, discharge: list[float], upstream: float | list[float] = 100.0, thresholds=None) -> np.ndarray:
        return return_period_class(
            np.asarray(discharge, dtype=np.float64),
            thresholds if thresholds is not None else tuple(level[: len(discharge)] for level in self.THRESHOLDS),
            np.broadcast_to(np.asarray(upstream, dtype=np.float64), (len(discharge),)),
        )

    def test_each_threshold_reached_is_a_class(self) -> None:
        # At or above a level counts: the class steps on the threshold itself.
        np.testing.assert_array_equal(self.classes([0.0, 1.99, 2.0, 3.0, 8.99, 9.0]), [1, 1, 2, 3, 4, 5])
        self.assertEqual(self.classes([1.0e5]).dtype, np.float32)

    def test_a_cell_efas_does_not_evaluate_has_no_class(self) -> None:
        # Upstream 50 km² or less, a 1.5-year level of 1 m³/s or less, or no
        # river at all.
        np.testing.assert_array_equal(np.isnan(self.classes([10.0, 10.0], upstream=[50.0, 50.01])), [True, False])
        low = (np.array([1.0, 1.01]), np.array([3.0, 3.0]), np.array([5.0, 5.0]), np.array([9.0, 9.0]))
        np.testing.assert_array_equal(np.isnan(self.classes([10.0, 10.0], thresholds=low)), [True, False])
        self.assertTrue(np.isnan(self.classes([np.nan])).all())
        self.assertTrue(np.isnan(self.classes([10.0], upstream=np.nan)).all())
        missing = tuple(np.array([np.nan]) if index == 2 else level[:1] for index, level in enumerate(self.THRESHOLDS))
        self.assertTrue(np.isnan(self.classes([10.0], thresholds=missing)).all())

    def test_the_class_needs_all_four_levels(self) -> None:
        with self.assertRaises(efas_series.EfasError):
            return_period_class(np.zeros(1), self.THRESHOLDS[:3], np.full(1, 100.0))


class WindowTests(unittest.TestCase):
    def test_the_grid_and_a_bbox_cut(self) -> None:
        whole = Window()
        self.assertEqual((whole.width, whole.height), (4530, 2970))
        self.assertAlmostEqual(float(whole.longitudes[0]), -25.25 + 1 / 120)
        self.assertAlmostEqual(float(whole.latitudes[-1]), 72.25 - 1 / 120)
        self.assertLess(float(whole.latitudes[0]), float(whole.latitudes[-1]), "the series' latitudes ascend")
        window = Window.from_bbox((-0.75, 39.3, -0.4, 39.55))
        self.assertEqual((window.row_start, window.column_start, window.width, window.height), (1962, 1470, WIDTH, HEIGHT))
        self.assertTrue((window.longitudes >= -0.75).all() and (window.longitudes <= -0.4).all())
        plane = np.arange(4530 * 2970, dtype=np.float64).reshape(2970, 4530)
        cut = window.take(plane)
        self.assertEqual(cut.shape, (HEIGHT, WIDTH))
        # North row first in the GRIB, south row first in the series.
        self.assertEqual(cut[-1, 0], plane[1962, 1470])
        with self.assertRaises(efas_series.EfasError):
            Window.from_bbox((10.0, 10.0, 11.0, 11.0))
        with self.assertRaises(efas_series.EfasError):
            Window.from_bbox((1.0, 39.0, 0.0, 40.0))


class FrameTimeTests(unittest.TestCase):
    """How a message's valid time is found: a forecast's own reference and
    validity, the water balance's validity or, where its dates are not
    real, its place in the file counted from ``--start``."""

    CYCLE = datetime(2024, 10, 27, 12, tzinfo=UTC)

    def message(self, index: int, reference: datetime | None, valid: datetime | None) -> Message:
        return Message(index, reference, valid, 2, np.empty(0))

    def test_a_forecast_is_its_cycle_and_the_ends_of_its_intervals(self) -> None:
        messages = [self.message(index, self.CYCLE, self.CYCLE + timedelta(hours=6 * (index + 1))) for index in range(3)]
        run, valid = efas_series._frame_times(messages, "efas", None)
        self.assertEqual(run, self.CYCLE)
        self.assertEqual([(time - run) / timedelta(hours=1) for time in valid], [6, 12, 18])
        self.assertEqual(efas_series._frame_times(messages, "efas", self.CYCLE)[0], self.CYCLE)
        with self.assertRaisesRegex(efas_series.EfasError, "not the forecast's cycle"):
            efas_series._frame_times(messages, "efas", self.CYCLE + timedelta(hours=12))
        mixed = [*messages, self.message(3, self.CYCLE + timedelta(hours=12), self.CYCLE + timedelta(hours=24))]
        with self.assertRaisesRegex(efas_series.EfasError, "reference times"):
            efas_series._frame_times(mixed, "efas", None)

    def test_the_water_balance_is_named_by_its_start(self) -> None:
        start = datetime(2024, 10, 26, tzinfo=UTC)
        undated = [self.message(index, None, None) for index in range(3)]
        with self.assertRaisesRegex(efas_series.EfasError, "--start"):
            efas_series._frame_times(undated, "efaswb", None)
        run, valid = efas_series._frame_times(undated, "efaswb", start)
        self.assertEqual(run, start)
        self.assertEqual(valid, [start + timedelta(hours=hours) for hours in (6, 12, 18)])
        dated = [self.message(index, None, start + timedelta(hours=6 * (index + 1))) for index in range(3)]
        self.assertEqual(efas_series._frame_times(dated, "efaswb", start)[1], valid)


class CaseDefinitionTests(unittest.TestCase):
    def payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": "valencia-demo",
            "title": localized("Demo"),
            "summary": localized("Demo summary"),
            "model": "efas",
            "dataset": "efas/valencia-2024-efas/",
            "hours": 120,
            "bbox": [-1.5, 38.8, 0.5, 40.2],
            "variables": ["disrp", "dis06", "upstream"],
            "defaultVariable": "disrp",
        }
        payload.update(overrides)
        return payload

    def test_a_case_names_the_dataset_directory_and_no_run(self) -> None:
        spec = parse_case(self.payload())
        self.assertTrue(spec.from_dataset)
        self.assertEqual(spec.variables, BUNDLES)
        parse_case(self.payload(model="efaswb", dataset="efas/valencia-2024-efaswb/", hours=12))
        for bad in ({"run": "2024102712"}, {"dataset": ""}, {"hours": 126}, {"hours": 9}):
            with self.subTest(bad=bad), self.assertRaises(Exception):
                parse_case(self.payload(**bad))


@requires_gdal
class SeriesTests(unittest.TestCase):
    def test_the_run_is_the_epoch_and_the_axis_starts_at_f006(self) -> None:
        for source_id, (directory, _, run_time) in RUNS.items():
            with self.subTest(source=source_id):
                series = observation.inspect_observation(directory, source_spec(source_id))
                self.assertEqual(series.frames[0]["dis06"].run_time, run_time)
                self.assertEqual(series.lead_seconds, [hour * 3600 for hour in HOURS])
                self.assertEqual(series.producers, {"disrp": ("xue", "1"), "upstream": ("xue", "1")})
                # The fill is the file's declared -9999, not a NaN.
                for variable_id in BUNDLES:
                    plane_source = series.plane_sources[variable_id]
                    self.assertFalse(plane_source.fill_nan)
                    self.assertEqual(set(plane_source.fill_values), {EFAS_SERIES_FILL})
                files = observation.series_files(directory, BUNDLES)
                self.assertEqual(
                    {variable_id: path.name for variable_id, path in files.items()},
                    {variable_id: f"{source_id}.{run_time:%Y%m%d%H}.{variable_id}.nc" for variable_id in BUNDLES},
                )


@requires_gdal
class ConversionTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-efas-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.reports = {}
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            for source_id, (directory, run_id, _) in RUNS.items():
                cls.reports[source_id] = binconvert.convert_bin(
                    directory,
                    cls.root / source_id,
                    model=source_id,
                    skip_video=True,
                    work_root=cls.root / f"work-{source_id}",
                    run_id=run_id,
                    manifest_path=cls.root / source_id / "manifest.json",
                    container=True,
                    zarr=False,
                )

    def bundle(self, source_id: str, name: str):
        return read_bundle(self.root / source_id / f"{name}.xue")

    def plane(self, source_id: str, name: str, hour: int) -> np.ndarray:
        return np.asarray(self.bundle(source_id, name).decode_plane(1, hour)).reshape(HEIGHT, WIDTH)

    def test_the_run_publishes_the_three_bundles(self) -> None:
        for source_id, (_, _, run_time) in RUNS.items():
            spec = source_spec(source_id)
            with self.subTest(source=source_id):
                report = self.reports[source_id]
                self.assertEqual(tuple(bundle["variable"] for bundle in report["bundles"]), BUNDLES)
                self.assertEqual(report["videos"], [])
                manifest = json.loads((self.root / source_id / "manifest.json").read_text())
                self.assertEqual((manifest["model"], manifest["product"]), (spec.manifest_model, spec.product))
                self.assertEqual(manifest["runTime"], run_time.strftime("%Y-%m-%dT%H:%M:%SZ"))
                self.assertEqual(manifest["forecastHours"], 120)
                validate_bin_manifest(manifest, expected_hours=120, require_core_variables=True)
                self.assertFalse((self.root / source_id / "dis06.half.xue").exists(), "no ladder")

    def test_the_time_axis_and_the_grid(self) -> None:
        for source_id in RUNS:
            for name in BUNDLES:
                with self.subTest(source=source_id, bundle=name):
                    metadata = self.bundle(source_id, name).metadata
                    self.assertEqual(metadata["time"]["firstFrameOffset"] * metadata["time"]["unitSeconds"], 6 * 3600)
                    self.assertEqual(metadata["time"]["frameStep"] * metadata["time"]["unitSeconds"], 6 * 3600)
                    self.assertEqual(metadata["time"]["frameCount"], 20)
                    grid = metadata["grid"]
                    self.assertEqual((grid["width"], grid["height"], grid["rowOrder"]), (WIDTH, HEIGHT, "north-to-south"))
                    self.assertAlmostEqual(grid["longitudeStep"], 1 / 60, places=9)
                    self.assertAlmostEqual(grid["latitudeStep"], -1 / 60, places=9)
                    self.assertAlmostEqual(grid["firstLongitude"], -0.75 + 1 / 120, places=9)
                    self.assertAlmostEqual(grid["firstLatitude"], 39.55 - 1 / 120, places=9)
                    self.assertFalse(grid["wrapLongitude"])

    def test_the_identities_and_codebooks_in_the_metadata(self) -> None:
        for source_id in RUNS:
            with self.subTest(source=source_id):
                variables = {name: self.bundle(source_id, name).metadata["variables"][0] for name in BUNDLES}
                self.assertEqual(variables["dis06"]["parameter"]["typeOfStatisticalProcessing"], 0)
                self.assertNotIn("producer", variables["dis06"])
                for name in ("disrp", "upstream"):
                    self.assertEqual(variables[name]["producer"], {"id": "xue", "version": "1"})
                    self.assertNotIn("typeOfStatisticalProcessing", variables[name]["parameter"])
                for name, variable in variables.items():
                    self.assertEqual(variable["unit"], variable_spec(name).output_unit)
                    self.assertEqual(variable["quantization"], PROFILES["quality"][name].metadata())

    def test_the_poyo_a3_cell_passes_its_twenty_year_level_in_the_forecast_alone(self) -> None:
        row, column = POYO_A3
        upstream = PROFILES["quality"]["upstream"].decode(self.plane("efas", "upstream", 6))[row, column]
        self.assertAlmostEqual(float(upstream), 196.3, delta=196.3 * 0.03)
        forecast = [int(self.plane("efas", "disrp", hour)[row, column]) for hour in HOURS]
        # The six-hour mean ending 30 October 00Z (f060) is over the 20-year
        # level; the frames either side of it over the 5-year level.
        self.assertEqual(forecast, [1] * 8 + [4, 5, 4] + [1] * 9)
        discharge = PROFILES["quality"]["dis06"].decode(self.plane("efas", "dis06", 60))[row, column]
        self.assertAlmostEqual(float(discharge), 30.1, delta=30.1 * 0.04)
        # The water balance barely responds on the coastal ravines.
        self.assertEqual([int(self.plane("efaswb", "disrp", hour)[row, column]) for hour in HOURS], [1] * 20)

    def test_a_cell_efas_does_not_evaluate_is_class_zero(self) -> None:
        upstream = PROFILES["quality"]["upstream"].decode(self.plane("efas", "upstream", 60))
        classes = self.plane("efas", "disrp", 60)
        small = upstream < 45.0  # well under 50 km², whatever the log step
        self.assertTrue(small.any(), "the fixture carries small catchments")
        self.assertFalse(classes[small].any())
        self.assertTrue((classes > 0).any())

    def test_the_values_are_the_series(self) -> None:
        import netCDF4  # noqa: PLC0415 - the efas / wrf groups

        directory, run_id, _ = RUNS["efas"]
        for name in BUNDLES:
            codebook = PROFILES["quality"][name]
            with netCDF4.Dataset(directory / f"efas.{run_id}.{name}.nc") as dataset:
                values = np.ma.filled(dataset[name][:], np.nan)[:, ::-1]  # south first in the series
            for index, hour in enumerate((6, 60, 120)):
                with self.subTest(bundle=name, hour=hour):
                    source = values[HOURS.index(hour)]
                    # A cell off the rivers is NaN in the series and the
                    # codebook bottom in the bundle: the format has no bitmap.
                    expected = codebook.quantize(np.nan_to_num(source, nan=float(variable_spec(name).value_range[0])))
                    np.testing.assert_array_equal(self.plane("efas", name, hour), expected)
        # The upstream area is the same plane on every frame.
        first = self.plane("efas", "upstream", 6)
        for hour in HOURS:
            np.testing.assert_array_equal(self.plane("efas", "upstream", hour), first)

    def test_a_complete_run_cannot_be_required_of_a_source_with_no_feed(self) -> None:
        directory, _, _ = RUNS["efas"]
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "no complete run to require"):
                binconvert.convert_bin(
                    directory,
                    self.root / "complete",
                    model="efas",
                    skip_video=True,
                    require_complete=True,
                    expected_hours=120,
                    container=True,
                    zarr=False,
                )

    @requires_native_source("efas")
    def test_the_native_encoder_writes_the_same_bytes_for_the_forecast(self) -> None:
        directory, _, _ = RUNS["efas"]
        assert_native_matches(self, directory, self.root / "efas", self.reports["efas"], self.root / "native-efas", model="efas")

    @requires_native_source("efaswb")
    def test_the_native_encoder_writes_the_same_bytes_for_the_water_balance(self) -> None:
        directory, _, _ = RUNS["efaswb"]
        assert_native_matches(
            self, directory, self.root / "efaswb", self.reports["efaswb"], self.root / "native-efaswb", model="efaswb"
        )


if __name__ == "__main__":
    unittest.main()

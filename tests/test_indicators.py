"""The agriculture indicators: daily features against analytic answers,
the in-season state machine, the crop calendar, the weights file and the
HTTP Range store reader against the container's own decoder."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np

from tests.prepare_indicators_golden import (
    GRID,
    HOURS,
    PLANES,
    PROFILE,
    REGION,
    REGION_BOUNDS,
    RUN,
    RUN_TIME,
    STORE_ROOT,
    WEIGHTS,
    region_weights,
    serve,
)
from tests.prepare_web_fixture import write_v2_bundle
from xuebuild import binformat, zarrstore
from xuebuild.binconvert import build_metadata
from xuebuild.indicators import calendar, season, store
from xuebuild.indicators import weights as weights_module
from xuebuild.indicators.features import Series, daily_features, frame_steps, summarize
from xuebuild.indicators.regions import REGION_IDS, REGIONS, region
from xuebuild.indicators.schema import IndicatorsError
from xuebuild.quantize import PROFILES, codebook_from_metadata

HOUR = 3600


def constant(value: float, frames: int, shape: tuple[int, int] = (2, 3)) -> np.ndarray:
    return np.full((frames, *shape), value, dtype=np.float64)


def uniform(shape: tuple[int, int] = (2, 3)) -> np.ndarray:
    return np.full(shape, 1.0 / (shape[0] * shape[1]))


def hourly(hours: int) -> list[int]:
    return [hour * HOUR for hour in range(hours + 1)]


# 06Z is local midnight at UTC−6, so a run there starts on a day boundary.
MIDNIGHT_RUN = datetime(2026, 7, 10, 6, tzinfo=UTC)


def features(
    temperature: np.ndarray,
    precipitation: np.ndarray,
    *,
    offsets: list[int] | None = None,
    precipitation_offsets: list[int] | None = None,
    weights: np.ndarray | None = None,
    run_time: datetime = MIDNIGHT_RUN,
    utc_offset: int = -6,
    expected: list[int] | None = None,
) -> list[dict]:
    offsets = offsets if offsets is not None else hourly(temperature.shape[0] - 1)
    return daily_features(
        Series(run_time, offsets, temperature),
        Series(run_time, precipitation_offsets if precipitation_offsets is not None else offsets, precipitation),
        weights if weights is not None else uniform(temperature.shape[1:]),
        utc_offset,
        expected,
    )


class FeatureTests(unittest.TestCase):
    def test_a_constant_mild_field(self) -> None:
        days = features(constant(20.0, 49), constant(0.0, 49))
        self.assertEqual([day["date"] for day in days], ["2026-07-10", "2026-07-11", "2026-07-12"])
        first = days[0]
        self.assertEqual(
            {key: first[key] for key in first if key != "date"},
            {
                "t2m_mean": 20.0,
                "t2m_max": 20.0,
                "t2m_min": 20.0,
                "precip": 0.0,
                "dry_frac": 1.0,
                "hot30_frac": 0.0,
                "hot35_frac": 0.0,
                "gdd": 10.0,
                "complete": True,
            },
        )
        # The last day holds only the closing midnight frame.
        self.assertEqual([day["complete"] for day in days], [True, True, False])

    def test_heat_thresholds_and_the_degree_day_cap(self) -> None:
        hot = features(constant(32.0, 25), constant(0.0, 25))[0]
        self.assertEqual((hot["hot30_frac"], hot["hot35_frac"], hot["gdd"]), (1.0, 0.0, 20.0))
        hotter = features(constant(36.0, 25), constant(0.0, 25))[0]
        self.assertEqual((hotter["hot30_frac"], hotter["hot35_frac"], hotter["gdd"]), (1.0, 1.0, 20.0))
        cold = features(constant(4.0, 25), constant(0.0, 25))[0]
        self.assertEqual(cold["gdd"], 0.0)

    def test_fractions_are_crop_area_weights(self) -> None:
        temperature = np.empty((25, 1, 2))
        temperature[:, 0, 0], temperature[:, 0, 1] = 31.0, 29.0
        day = features(temperature, constant(0.0, 25, (1, 2)), weights=np.array([[0.25, 0.75]]))[0]
        self.assertEqual(day["hot30_frac"], 0.25)
        self.assertEqual(day["t2m_mean"], 29.5)

    def test_the_maximum_is_per_cell_before_weighting(self) -> None:
        # Two cells peaking at different hours: the weighted field never
        # exceeds 25, each cell's own maximum is 30.
        temperature = np.full((25, 1, 2), 20.0)
        temperature[3, 0, 0] = 30.0
        temperature[15, 0, 1] = 30.0
        day = features(temperature, constant(0.0, 25, (1, 2)), weights=np.array([[0.5, 0.5]]))[0]
        self.assertEqual(day["t2m_max"], 30.0)
        self.assertEqual(day["t2m_min"], 20.0)

    def test_precipitation_totals_and_the_dry_day_threshold(self) -> None:
        wet = features(constant(20.0, 25), constant(0.5, 25))[0]
        self.assertEqual((wet["precip"], wet["dry_frac"]), (12.0, 0.0))
        drizzle = features(constant(20.0, 25), constant(0.03, 25))[0]
        self.assertEqual((drizzle["precip"], drizzle["dry_frac"]), (0.72, 1.0))

    def test_a_frame_at_local_midnight_opens_the_new_day(self) -> None:
        run = datetime(2026, 7, 10, 0, tzinfo=UTC)  # 18:00 local on the 9th
        temperature = np.where(np.arange(13)[:, None, None] < 6, 10.0, 20.0) * np.ones((13, 2, 3))
        days = features(temperature, constant(0.0, 13), run_time=run)
        self.assertEqual([(day["date"], day["t2m_mean"]) for day in days], [("2026-07-09", 10.0), ("2026-07-10", 20.0)])
        # The run starts inside the 9th: that day is not whole.
        self.assertFalse(days[0]["complete"])

    def test_a_step_across_the_local_day_boundary_is_split_by_time(self) -> None:
        # UTC−3: 03Z is local midnight, so the 00Z–06Z step is half on each day.
        run = datetime(2026, 1, 10, 0, tzinfo=UTC)
        offsets = [0, 6 * HOUR, 12 * HOUR]
        rate = np.zeros((3, 2, 3))
        rate[1] = 1.0
        days = features(constant(25.0, 3), rate, offsets=offsets, run_time=run, utc_offset=-3)
        self.assertEqual([(day["date"], day["precip"]) for day in days], [("2026-01-09", 3.0), ("2026-01-10", 3.0)])

    def test_a_missing_frame_leaves_its_day_incomplete(self) -> None:
        expected = hourly(72)
        temperature_offsets = [offset for offset in expected if offset != 30 * HOUR]
        precipitation_offsets = [offset for offset in expected if offset != 50 * HOUR]
        days = daily_features(
            Series(MIDNIGHT_RUN, temperature_offsets, constant(20.0, len(temperature_offsets))),
            Series(MIDNIGHT_RUN, precipitation_offsets, constant(0.1, len(precipitation_offsets))),
            uniform(),
            -6,
            expected,
        )
        self.assertEqual([day["complete"] for day in days], [True, False, False, False])
        # The step over the gap still counts its precipitation.
        self.assertEqual(days[2]["precip"], 2.4)
        days = daily_features(
            Series(MIDNIGHT_RUN, temperature_offsets, constant(20.0, len(temperature_offsets))),
            Series(MIDNIGHT_RUN, precipitation_offsets, constant(0.1, len(precipitation_offsets))),
            uniform(),
            -6,
        )
        # Taking the temperature axis as the source's, the missing hour of
        # temperature cannot show; the precipitation one still does.
        self.assertEqual([day["complete"] for day in days], [True, True, False, False])

    def test_the_last_day_needs_the_frame_that_closes_it(self) -> None:
        # Six-hourly at UTC−3: the day ending 03Z is closed by the 06Z step.
        run = datetime(2026, 1, 10, 0, tzinfo=UTC)
        offsets = [hour * HOUR for hour in range(0, 31, 6)]
        days = features(constant(25.0, 6), constant(0.0, 6), offsets=offsets, run_time=run, utc_offset=-3)
        self.assertEqual(
            [(day["date"], day["complete"]) for day in days],
            [("2026-01-09", False), ("2026-01-10", True), ("2026-01-11", False)],
        )
        days = features(
            constant(25.0, 5), constant(0.0, 5), offsets=offsets[:5], run_time=run, utc_offset=-3, expected=offsets
        )
        self.assertEqual([day["complete"] for day in days], [False, False])

    def test_frame_steps_and_the_summary(self) -> None:
        self.assertEqual(frame_steps([hour * HOUR for hour in [*range(121), *range(123, 241, 3)]]), [[120, 1], [240, 3]])
        days = features(constant(21.0, 24 * 20 + 1), constant(0.1, 24 * 20 + 1))
        summary = summarize(days)
        self.assertEqual(summary, {"days": 14, "precip_sum": 33.6, "gdd_sum": 154.0, "hot35_days": 0.0})


def line(run: str, region_id: str, days: list[tuple[str, float]], *, complete: bool = True) -> dict:
    """A GFS line carrying only ``(date, precip)`` days of one region."""
    return {
        "source": "gfs",
        "run": run,
        "regions": {
            region_id: {
                "days": [
                    {
                        "date": day,
                        "t2m_mean": 22.0,
                        "t2m_max": 30.0,
                        "t2m_min": 15.0,
                        "precip": precip,
                        "dry_frac": 1.0 if precip < 1 else 0.0,
                        "hot30_frac": 0.0,
                        "hot35_frac": 0.5,
                        "gdd": 12.0,
                        "complete": complete,
                    }
                    for day, precip in days
                ],
                "summary": {},
            }
        },
    }


class CalendarTests(unittest.TestCase):
    def test_season_ids(self) -> None:
        self.assertEqual(calendar.season_of("br-mt", date(2027, 1, 10)), "2026-27")
        self.assertEqual(calendar.season_of("br-mt", date(2026, 10, 1)), "2026-27")
        self.assertEqual(calendar.season_of("br-mt", date(2027, 3, 31)), "2026-27")
        self.assertIsNone(calendar.season_of("br-mt", date(2026, 9, 30)))
        self.assertEqual(calendar.season_of("ar-sf", date(2027, 4, 30)), "2026-27")
        self.assertEqual(calendar.season_of("us-ia", date(2027, 7, 1)), "2027")
        self.assertIsNone(calendar.season_of("us-ia", date(2027, 10, 1)))
        self.assertEqual(calendar.season_bounds("br-rs", "2026-27"), (date(2026, 10, 1), date(2027, 3, 31)))

    def test_flowering_windows(self) -> None:
        self.assertTrue(calendar.in_window("br-rs", date(2027, 2, 28)))
        self.assertFalse(calendar.in_window("br-rs", date(2026, 12, 14)))
        self.assertTrue(calendar.in_window("br-mt", date(2027, 1, 31)))
        self.assertFalse(calendar.in_window("ar-cb", date(2027, 3, 1)))
        self.assertTrue(calendar.in_window("us-il", date(2027, 8, 31), "flowering"))
        self.assertTrue(calendar.in_window("us-il", date(2027, 9, 30), "season"))

    def test_every_region_has_a_calendar_and_an_offset(self) -> None:
        self.assertEqual(len(REGIONS), 16)
        self.assertEqual(set(calendar.CALENDARS), set(REGION_IDS))
        self.assertEqual({entry.utc_offset_hours for entry in REGIONS if entry.country == "US"}, {-6})
        self.assertEqual({entry.utc_offset_hours for entry in REGIONS if entry.country != "US"}, {-3})
        for entry in REGIONS:
            if entry.parent is not None:
                self.assertTrue(set(entry.subdivisions) <= set(region(entry.parent).subdivisions))


class SeasonTests(unittest.TestCase):
    def seasons(self, lines: list[dict], previous: dict | None = None) -> dict:
        return season.build_seasons(lines, previous or {}, weights_version="w1")

    def test_consecutive_dry_days(self) -> None:
        # us-ia days open at 06Z; each daily run at 06Z supplies its own day.
        precip = [0.0, 0.2, 5.0, 0.0, 0.0, 0.9]
        lines = [
            line(f"202607{10 + index:02d}06", "us-ia", [(f"2026-07-{10 + index:02d}", value)])
            for index, value in enumerate(precip)
        ]
        payload = self.seasons(lines)[("us-ia", "2026")]
        self.assertEqual((payload["cdd_current"], payload["cdd_max"]), (3, 3))
        self.assertEqual(payload["days_counted"], 6)
        self.assertEqual(payload["precip_total"], 6.1)
        self.assertEqual(payload["hot35_days_window"], 3.0)
        self.assertEqual(payload["throughRun"], "2026071506")

    def test_a_missing_day_ends_a_dry_spell(self) -> None:
        lines = [
            line("2026071006", "us-ia", [("2026-07-10", 0.0)]),
            line("2026071106", "us-ia", [("2026-07-11", 0.0)]),
            line("2026071306", "us-ia", [("2026-07-13", 0.0)]),
        ]
        payload = self.seasons(lines)[("us-ia", "2026")]
        self.assertEqual((payload["cdd_current"], payload["cdd_max"]), (1, 2))

    def test_the_newest_run_before_the_day_wins_and_a_missing_cycle_is_partial(self) -> None:
        lines = [
            line("2026071000", "us-ia", [("2026-07-10", 3.0), ("2026-07-11", 3.0)]),
            line("2026071006", "us-ia", [("2026-07-10", 0.0)]),
            # 2026-07-11's own cycle (11 06Z) is missing; 12 06Z has reached it.
            line("2026071206", "us-ia", [("2026-07-12", 0.0)]),
        ]
        daily = self.seasons(lines)[("us-ia", "2026")]["daily"]
        self.assertEqual(
            [(day["date"], day["run"], day["lead_hours"], day["partial"]) for day in daily],
            [("2026-07-10", "2026071006", 0, False), ("2026-07-11", "2026071000", 30, True), ("2026-07-12", "2026071206", 0, False)],
        )

    def test_a_day_waits_for_its_cycle_and_skips_incomplete_days(self) -> None:
        lines = [line("2026071006", "us-ia", [("2026-07-10", 0.0), ("2026-07-11", 0.0)])]
        self.assertEqual([day["date"] for day in self.seasons(lines)[("us-ia", "2026")]["daily"]], ["2026-07-10"])
        self.assertEqual(self.seasons([line("2026071006", "us-ia", [("2026-07-10", 0.0)], complete=False)]), {})

    def test_a_season_across_the_new_year_is_one_file(self) -> None:
        # br-mt days open at 03Z; the 00Z cycle is three hours before.
        lines = [
            line("2026123100", "br-mt", [("2026-12-31", 0.0)]),
            line("2027010100", "br-mt", [("2027-01-01", 2.0)]),
        ]
        files = self.seasons(lines)
        self.assertEqual(list(files), [("br-mt", "2026-27")])
        self.assertEqual(season.season_path("br-mt", "2026-27"), "season/br-mt.2026-27.json")
        self.assertEqual([day["lead_hours"] for day in files[("br-mt", "2026-27")]["daily"]], [3, 3])

    def test_out_of_season_days_are_not_counted(self) -> None:
        self.assertEqual(self.seasons([line("2026100106", "us-ia", [("2026-10-01", 0.0)])]), {})

    def test_the_previous_file_carries_days_the_lines_no_longer_hold(self) -> None:
        first = self.seasons([line("2026071006", "us-ia", [("2026-07-10", 0.0)])])
        second = self.seasons([line("2026071106", "us-ia", [("2026-07-11", 4.0)])], first)
        payload = second[("us-ia", "2026")]
        self.assertEqual([day["date"] for day in payload["daily"]], ["2026-07-10", "2026-07-11"])
        self.assertEqual(payload["throughRun"], "2026071106")
        # Rebuilding from the same inputs gives the same file.
        self.assertEqual(self.seasons([line("2026071106", "us-ia", [("2026-07-11", 4.0)])], first), second)


def _importable(name: str) -> bool:
    import importlib.util  # noqa: PLC0415

    return importlib.util.find_spec(name) is not None


class WeightsTests(unittest.TestCase):
    def test_the_fixture_loads(self) -> None:
        loaded = weights_module.load(WEIGHTS)
        self.assertEqual((loaded.grid, loaded.shape, loaded.version), ("mini", (GRID.height, GRID.width), "fixture-1"))
        rows, cols, values = loaded[REGION]
        self.assertEqual((rows, cols), (slice(REGION_BOUNDS[0], REGION_BOUNDS[1]), slice(REGION_BOUNDS[2], REGION_BOUNDS[3])))
        self.assertAlmostEqual(float(values.sum()), 1.0, places=12)
        self.assertEqual(values[0, 0], 0.0)
        expected = region_weights()[rows, cols].astype(np.float64)
        np.testing.assert_allclose(values, expected / expected.sum(), rtol=0, atol=1e-15)
        self.assertRegex(loaded.crc32, r"^[0-9a-f]{8}$")

    def test_it_reads_over_http_with_one_index_range_and_only_the_box(self) -> None:
        with serve(WEIGHTS.parent) as (base_url, requests):
            loaded = weights_module.load(f"{base_url}{WEIGHTS.name}")
        self.assertEqual(loaded.crc32, weights_module.load(WEIGHTS).crc32)
        shard = [header for path, header in requests if path.endswith("weights/c/0/0/0")]
        # The index as a suffix range, then the box's chunks: four of the
        # shard's twelve, close enough to merge into one range.
        self.assertEqual(shard[0], f"bytes=-{16 * 12 + 4}")
        self.assertEqual(len(shard), 2)

    def test_the_grid_must_match_the_store(self) -> None:
        loaded = weights_module.load(WEIGHTS)
        grid = GRID.metadata()
        weights_module.check_grid(loaded, grid)
        with self.assertRaises(IndicatorsError):
            weights_module.check_grid(loaded, {**grid, "firstLongitude": -109.5})
        with self.assertRaises(IndicatorsError):
            weights_module.check_grid(loaded, {**grid, "width": 31})

    def edited(self, root: Path, change) -> Path:
        copy = root / "weights.zarr"
        shutil.copytree(WEIGHTS, copy)
        document = copy / "zarr.json"
        payload = json.loads(document.read_text())
        change(payload)
        document.write_text(json.dumps(payload))
        return copy

    def test_a_malformed_store_is_refused(self) -> None:
        def shrink(payload: dict) -> None:
            payload["attributes"]["regions"][REGION]["rows"][1] -= 1

        def outside(payload: dict) -> None:
            payload["attributes"]["regions"][REGION]["cols"] = [25, 31]

        def renamed(payload: dict) -> None:
            regions = payload["attributes"]["regions"]
            regions["us-il"] = regions.pop(REGION)

        cases = {"sum to": shrink, "inside the grid": outside, "different regions": renamed}
        for message, change in cases.items():
            with self.subTest(message), tempfile.TemporaryDirectory() as scratch:
                with self.assertRaisesRegex(IndicatorsError, message):
                    weights_module.load(self.edited(Path(scratch), change))
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(scratch) / "weights.zarr"
            shutil.copytree(WEIGHTS, copy)
            shard = copy / "weights" / "c" / "0" / "0" / "0"
            data = bytearray(shard.read_bytes())
            data[-6] ^= 0xFF
            shard.write_bytes(bytes(data))
            with self.assertRaisesRegex(IndicatorsError, "CRC-32C"):
                weights_module.load(copy)

    @unittest.skipUnless(_importable("xarray") and _importable("zarr"), "xarray and zarr are not installed")
    def test_xarray_opens_the_store(self) -> None:
        import xarray  # noqa: PLC0415

        dataset = xarray.open_zarr(WEIGHTS, consolidated=False)
        self.assertEqual(dict(dataset["weights"].sizes), {"region": 1, "lat": GRID.height, "lon": GRID.width})
        self.assertEqual([str(name) for name in dataset["region"].values], [REGION])
        self.assertAlmostEqual(float(dataset["weights"].sel(region=REGION).sum()), 1.0, places=5)


class CodebookTests(unittest.TestCase):
    def test_every_profile_codebook_round_trips_through_its_metadata(self) -> None:
        for profile in PROFILES.values():
            for name, codebook in profile.items():
                rebuilt = codebook_from_metadata(codebook.metadata(), name=name)
                top = getattr(codebook, "overflow_code", None) or codebook.maximum_code
                codes = np.arange(top + 1, dtype=np.uint8)
                np.testing.assert_array_equal(rebuilt.decode(codes), codebook.decode(codes), err_msg=name)


class StoreReaderTests(unittest.TestCase):
    """The reader against the container's decoder: bundles written the way
    the fixture's are, exported both as the profile's default store and as a
    delta-chain store with its index at the start, served over HTTP."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = Path(tempfile.mkdtemp(prefix="xue-indicators-store-"))
        cls.bundles = {}
        for variable_id, builder in PLANES.items():
            path = cls.scratch / f"{variable_id}.xue"
            codebook = PROFILES[PROFILE][variable_id]
            write_v2_bundle(
                path,
                build_metadata(RUN_TIME, HOURS, GRID, PROFILE, (variable_id,)),
                GRID,
                (variable_id,),
                HOURS,
                {hour: {variable_id: codebook.quantize(builder(hour).ravel())} for hour in HOURS},
                level=3,
                tile=(8, 8),
            )
            cls.bundles[variable_id] = binformat.read_bundle(path)
            zarrstore.export_bundle(path, cls.scratch / f"{variable_id}.zarr")
            zarrstore.export_bundle(path, cls.scratch / f"{variable_id}.delta.zarr", delta=True, index_location="start")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.scratch)

    def open(self, base_url: str, variable_id: str, store_name: str) -> store.StoreArray:
        manifest = {
            "model": "GFS",
            "runTime": "2026-08-15T06:00:00Z",
            "forecastHours": HOURS[-1],
            "bundles": [{"variable": variable_id, "zarr": {"path": store_name, "byteLength": 1, "crc32": "00000000"}}],
        }
        run = store.RunRef("gfs", RUN, RUN_TIME, "manifest.json", None, manifest, base_url)
        # The descriptor's ?v= is appended to every store URL; the server ignores it.
        return store.open_array(run, variable_id)

    def expected_codes(self, variable_id: str, rows: slice, cols: slice) -> np.ndarray:
        bundle = self.bundles[variable_id]
        return np.stack(
            [
                bundle.decode_plane(1, hour).reshape(GRID.height, GRID.width)[rows, cols]
                for hour in bundle.frame_offsets
            ]
        )

    def test_windows_read_back_the_bundles_codes(self) -> None:
        windows = {
            "region": (slice(REGION_BOUNDS[0], REGION_BOUNDS[1]), slice(REGION_BOUNDS[2], REGION_BOUNDS[3])),
            "corner": (slice(15, 20), slice(25, 30)),
            "cell": (slice(0, 1), slice(0, 1)),
        }
        with serve(self.scratch.parent) as (base_url, _requests):
            base = f"{base_url}{self.scratch.name}/"
            for variable_id in PLANES:
                for store_name in (f"{variable_id}.zarr", f"{variable_id}.delta.zarr"):
                    with self.subTest(store=store_name):
                        array = self.open(base, variable_id, store_name)
                        codes = store.read_codes(array, windows)
                        for name, (rows, cols) in windows.items():
                            np.testing.assert_array_equal(codes[name], self.expected_codes(variable_id, rows, cols))
                        values = store.read_values(array, {"region": windows["region"]})["region"]
                        codebook = PROFILES[PROFILE][variable_id]
                        np.testing.assert_array_equal(values, codebook.decode(codes["region"]))

    def test_ranges_are_one_index_read_and_merged_per_time_chunk(self) -> None:
        rows, cols = slice(REGION_BOUNDS[0], REGION_BOUNDS[1]), slice(REGION_BOUNDS[2], REGION_BOUNDS[3])
        with serve(self.scratch.parent) as (base_url, requests):
            array = self.open(f"{base_url}{self.scratch.name}/", "tmp2m", "tmp2m.zarr")
            requests.clear()
            store.read_codes(array, {"region": (rows, cols)})
        shard = [header for path, header in requests if path.endswith("c/0/0/0")]
        self.assertEqual(shard[0], f"bytes=-{array.geometry.shard_index_bytes}")
        self.assertEqual(sum(header.startswith("bytes=-") for header in shard), 1)
        # Four tiles in two tile rows, a few hundred bytes apart: one range per time chunk.
        self.assertEqual(len(shard) - 1, array.geometry.time_chunks)

    def test_a_corrupt_shard_index_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            shutil.copytree(self.scratch / "tmp2m.zarr", root / "tmp2m.zarr")
            shard = root / "tmp2m.zarr" / "tmp2m" / "c" / "0" / "0" / "0"
            data = bytearray(shard.read_bytes())
            data[-10] ^= 0xFF
            shard.write_bytes(bytes(data))
            with serve(root) as (base_url, _requests):
                array = self.open(base_url, "tmp2m", "tmp2m.zarr")
                with self.assertRaisesRegex(store.StoreError, "CRC-32C"):
                    store.read_codes(array, {"cell": (slice(0, 1), slice(0, 1))})

    def test_the_live_pointer_resolves_to_the_fixture_run(self) -> None:
        with serve(STORE_ROOT) as (base_url, requests):
            run = store.resolve_latest("gfs", base_url)
            with self.assertRaisesRegex(store.StoreError, "HTTP 404"):
                store.resolve_latest("ecmwf", base_url)
        self.assertEqual((run.run, run.manifest_path), (RUN, f"gfs.{RUN}/manifest.json"))
        self.assertEqual(requests[0], ("latest.json", None))
        self.assertTrue(run.manifest_crc32)
        pointer = json.loads((STORE_ROOT / "latest.json").read_text())
        self.assertEqual(run.manifest_crc32, pointer["manifestCrc32"])


if __name__ == "__main__":
    unittest.main()

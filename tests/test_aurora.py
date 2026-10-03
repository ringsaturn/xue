"""The NOAA SWPC OVATION aurora source: registry, grid parsing, the frame
cache and the window series, and the fetch that writes them.

The NetCDF half needs the optional ``aurora`` group (xarray + netCDF4) the
same way the CMA tests need ``cma``; the tests that touch it skip without
it. Everything the live feed's JSON is turned into — the parse, the slot
snap, the cache listing — runs with NumPy alone.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xuebuild import aurora
from xuebuild.errors import DownloadError
from xuebuild.fetch import _fetch_aurora_run, latest_aurora_slot
from xuebuild.model import GfsRun
from xuebuild.sources import MODEL_CORE_BUNDLES, SOURCES, source_spec
from xuebuild.stac import _source_prose

AURORA = source_spec("aurora")


def _native(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


NETCDF = _native("netCDF4") and _native("xarray")

#: A synthetic OVATION document: the real shape, values a ramp so a plane is
#: distinguishable from its neighbour.
def ovation_payload(stamp: str = "2026-10-03T04:41:00Z", offset: float = 0.0) -> str:
    coordinates = []
    for lon in range(360):
        for lat in range(-90, 91):
            probability = float((lon + lat + 90) % 40) + offset
            coordinates.append([lon, lat, probability])
    return json.dumps({"Observation Time": "2026-10-03T03:12:00Z", "Forecast Time": stamp, "Data Format": "[Longitude, Latitude, Aurora]", "coordinates": coordinates})


class RegistryTests(unittest.TestCase):
    def test_the_source_is_the_fetched_observation_it_claims_to_be(self) -> None:
        self.assertEqual((AURORA.id, AURORA.manifest_model, AURORA.product), ("aurora", "SWPC-AURORA", "ovation-aurora-1p00"))
        self.assertEqual(AURORA.latest_filename, "latest-aurora.json")
        self.assertEqual((AURORA.window_hours, AURORA.horizon_hours, AURORA.cadence_seconds), (3, 3, 300))
        self.assertEqual((AURORA.input_variable_ids, AURORA.bundle_scalar_ids, AURORA.core_bundle_ids), (("aurora",),) * 3)
        self.assertEqual(MODEL_CORE_BUNDLES["SWPC-AURORA"], ("aurora",))
        self.assertTrue(AURORA.observation and AURORA.series_file and AURORA.fetched and AURORA.live)
        self.assertFalse(AURORA.video)

    def test_the_grid_is_the_ovation_grid(self) -> None:
        self.assertEqual(AURORA.production_grid, aurora.GRID_SHAPE[::-1])
        width, height = AURORA.production_grid
        self.assertEqual((width, height), (360, 181))
        tile_width, tile_height = AURORA.tile
        self.assertGreaterEqual(width, tile_width)
        self.assertGreaterEqual(height, tile_height)

    def test_the_catalog_prose_names_the_agency_and_a_probability(self) -> None:
        prose = _source_prose(AURORA)
        self.assertEqual(prose["providers"][0]["name"], "NOAA Space Weather Prediction Center")
        self.assertIn("probability", prose["description"].lower())
        self.assertEqual(prose["links"][0]["rel"], "license")


class ParseTests(unittest.TestCase):
    def test_a_grid_is_read_off_lat_lon_and_snapped(self) -> None:
        frame = aurora.parse_ovation(ovation_payload())
        self.assertEqual(frame.valid_time, datetime(2026, 10, 3, 4, 40, tzinfo=UTC))
        self.assertEqual(frame.values.shape, aurora.GRID_SHAPE)
        # Row 0 is 90S, so cell (lon=0, lat=-90) is at [0, 0].
        self.assertEqual(frame.values[0, 0], 0.0)
        self.assertEqual(frame.values[-1, 0], float((0 + 90 + 90) % 40))

    def test_a_partial_grid_is_refused(self) -> None:
        payload = json.loads(ovation_payload())
        payload["coordinates"].pop()
        with self.assertRaisesRegex(DownloadError, "missing 1 of"):
            aurora.parse_ovation(json.dumps(payload))

    def test_a_document_without_a_forecast_time_is_refused(self) -> None:
        payload = json.loads(ovation_payload())
        del payload["Forecast Time"]
        with self.assertRaises(DownloadError):
            aurora.parse_ovation(json.dumps(payload))

    def test_snap_slot_takes_the_five_minute_mark_below(self) -> None:
        self.assertEqual(aurora.snap_slot(datetime(2026, 10, 3, 4, 41, 59, tzinfo=UTC)), datetime(2026, 10, 3, 4, 40, tzinfo=UTC))
        self.assertEqual(aurora.snap_slot(datetime(2026, 10, 3, 4, 40, tzinfo=UTC)), datetime(2026, 10, 3, 4, 40, tzinfo=UTC))
        self.assertEqual(aurora.snap_slot(datetime(2026, 10, 3, 4, 44, 59, tzinfo=UTC)), datetime(2026, 10, 3, 4, 40, tzinfo=UTC))

    def test_a_frame_name_and_its_time_round_trip(self) -> None:
        moment = datetime(2026, 10, 3, 4, 40, tzinfo=UTC)
        name = aurora.frame_name(moment)
        self.assertNotIn(":", name)
        self.assertEqual(aurora.frame_time(Path(name)), moment)


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-aurora-"))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.frames_dir = self.root / aurora.FRAMES_DIRNAME

    def _touch(self, moment: datetime) -> Path:
        variable_dir = self.frames_dir / aurora.VARIABLE_DIR
        variable_dir.mkdir(parents=True, exist_ok=True)
        path = variable_dir / aurora.frame_name(moment)
        path.write_bytes(b"")
        return path

    def test_the_cache_lists_oldest_first_and_windows_by_the_run(self) -> None:
        for minute in (5, 0, 10):
            self._touch(datetime(2026, 10, 3, 4, minute, tzinfo=UTC))
        self.assertEqual([slot for slot, _ in aurora.cached_frames(self.frames_dir)], [
            datetime(2026, 10, 3, 4, 0, tzinfo=UTC),
            datetime(2026, 10, 3, 4, 5, tzinfo=UTC),
            datetime(2026, 10, 3, 4, 10, tzinfo=UTC),
        ])
        window = aurora.window_frames(self.frames_dir, datetime(2026, 10, 3, 4, 0, tzinfo=UTC), 3)
        self.assertEqual(len(window), 3)
        narrow = aurora.window_frames(self.frames_dir, datetime(2026, 10, 3, 4, 0, tzinfo=UTC), 0)
        self.assertEqual([slot for slot, _ in narrow], [datetime(2026, 10, 3, 4, 0, tzinfo=UTC)])

    def test_latest_slot_falls_back_to_the_grid_just_parsed(self) -> None:
        frame = aurora.parse_ovation(ovation_payload())
        self.assertEqual(aurora.latest_slot(self.frames_dir, fallback=frame.valid_time), frame.valid_time)
        with self.assertRaises(DownloadError):
            aurora.latest_slot(self.frames_dir)


@unittest.skipUnless(NETCDF, "the aurora group (xarray + netCDF4) is not installed")
class SeriesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-aurora-"))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.frames_dir = self.root / aurora.FRAMES_DIRNAME

    def test_a_frame_and_a_window_are_written_and_read_back(self) -> None:
        frame = aurora.parse_ovation(ovation_payload())
        written = aurora.write_frame(self.frames_dir, frame)
        self.assertTrue(written.is_file())
        aurora.write_frame(self.frames_dir, aurora.OvationFrame(frame.valid_time + timedelta(minutes=5), frame.values + 1.0))
        output = self.root / "aurora.2026100304.nc"
        start = frame.valid_time.replace(minute=0, second=0, microsecond=0)
        slots = aurora.write_window(self.frames_dir, start, 3, output)
        self.assertEqual(slots, [frame.valid_time, frame.valid_time + timedelta(minutes=5)])

        from xuebuild.observation import inspect_observation

        observation = inspect_observation(output, AURORA)
        self.assertEqual(len(observation.frames), 2)
        first = observation.frames[0]["aurora"]
        self.assertEqual(first.run_time, start)
        self.assertEqual(first.valid_time, frame.valid_time)
        self.assertEqual(first.unit, "%")

    def test_a_window_with_nothing_cached_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "holds no frame"):
            aurora.write_window(self.frames_dir, datetime(2026, 10, 3, 4, 0, tzinfo=UTC), 3, self.root / "x.nc")


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-aurora-"))
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.run = GfsRun(datetime(2026, 10, 3, 4, 0, tzinfo=UTC))

    def test_latest_slot_reads_the_grid_s_forecast_time(self) -> None:
        slot = latest_aurora_slot(AURORA, fetch=lambda url: ovation_payload("2026-10-03T04:41:00Z"))
        self.assertEqual(slot, datetime(2026, 10, 3, 4, 40, tzinfo=UTC))

    @unittest.skipUnless(NETCDF, "the aurora group (xarray + netCDF4) is not installed")
    def test_a_fetch_writes_the_frame_and_the_window_and_grows_with_it(self) -> None:
        payloads = iter([ovation_payload("2026-10-03T04:41:00Z"), ovation_payload("2026-10-03T04:46:00Z", offset=1.0)])
        outputs = _fetch_aurora_run(AURORA, self.run, 3, self.root, force=False, input_ids=None, fetch=lambda url: next(payloads))
        self.assertEqual(outputs, [self.root / f"aurora.{self.run.id}" / f"aurora.{self.run.id}.nc"])
        self.assertTrue(outputs[0].is_file())
        record = json.loads((self.root / f"aurora.{self.run.id}" / "fetch.json").read_text(encoding="utf-8"))
        self.assertEqual(record["model"], "aurora")
        self.assertEqual(record["cadenceSeconds"], 300)
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-10-03T04:40:00Z"])
        # A second round adds its frame; the window holds both and the cache
        # is never cleared (force would rebuild the series, not drop history).
        _fetch_aurora_run(AURORA, self.run, 3, self.root, force=True, input_ids=None, fetch=lambda url: next(payloads))
        record = json.loads((self.root / f"aurora.{self.run.id}" / "fetch.json").read_text(encoding="utf-8"))
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-10-03T04:40:00Z", "2026-10-03T04:45:00Z"])

    def test_an_input_the_source_does_not_publish_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "publishes"):
            _fetch_aurora_run(AURORA, self.run, 3, self.root, force=False, input_ids=("cref",), fetch=lambda url: ovation_payload())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""The CWA radar composite over Taiwan: registry, the slot paths, the
document parse, the datum shift, the newest slot, the fetch through the
frame cache, and the series through both encoders.

``tests/fixtures/cwa.202611271120.crop.xml`` and
``cwa.202611271130.crop.xml`` are two real ``compref_mosaic.xml`` history
documents of the agency's open data bucket (19:20 and 19:30 Taiwan time on
2026-11-27, 11:20 and 11:30 UTC), cut to the 40 x 40 points of the showers
northeast of the island (25.5–25.9875°N, 122.6–123.0875°E on TWD67) with
the header kept (``tests/fixtures/README.md``). The parse runs on NumPy
alone; the cache and the series need the optional ``cwa`` group (xarray +
netCDF4), the way the aurora tests need theirs.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import (
    ClassTempRoot,
    FIXTURES,
    TempRoot,
    assert_native_matches,
    requires_gdal,
    requires_native_source,
    stored_bundle,
)
from xuebuild import binconvert, cwaradar
from xuebuild.errors import DownloadError
from xuebuild.feeds import CwaRadar, fetcher
from xuebuild.fetch import observation_window_start
from xuebuild.model import GfsRun
from xuebuild.sources import source_spec
from xuebuild.stac import _source_prose

CWA = source_spec("cwa")
EARLY = FIXTURES / "cwa.202611271120.crop.xml"
LATE = FIXTURES / "cwa.202611271130.crop.xml"
EARLY_TIME = datetime(2026, 11, 27, 11, 20, tzinfo=UTC)
LATE_TIME = datetime(2026, 11, 27, 11, 30, tzinfo=UTC)
RUN = GfsRun(datetime(2026, 11, 27, 11, 0, tzinfo=UTC))


def _installed(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


NETCDF = _installed("netCDF4") and _installed("xarray")


def _with_fields(text: str, **fields: str) -> str:
    for name, value in fields.items():
        text, count = re.subn(rf"<{name}>[^<]*</{name}>", f"<{name}>{value}</{name}>", text)
        assert count == 1, name
    return text


def _with_content(text: str, content: str) -> str:
    start, end = text.index("<content>") + len("<content>"), text.index("</content>")
    return text[:start] + content + text[end:]


def _full(slot: datetime) -> bytes:
    """A document on the published grid for ``slot``: the fixture's header,
    every point no echo but a ramp of echoes along the first row."""
    height, width = cwaradar.GRID_SHAPE
    text = _with_fields(
        EARLY.read_text(encoding="utf-8"),
        GridDimensionX=str(width),
        GridDimensionY=str(height),
        StartPointLongitude="115.0",
        StartPointLatitude="18.0",
        DateTime=slot.astimezone(cwaradar.TAIWAN).isoformat(),
    )
    values = np.full(height * width, -99.0)
    values[:60] = np.arange(60, dtype=np.float64)
    values[-1] = -999.0
    return _with_content(text, ",".join(f"{value:.3E}" for value in values)).encode("utf-8")


class Bucket:
    """The history prefix as a dict of slot to document; a slot it lacks is
    a 404. Every request is recorded."""

    def __init__(self, documents: dict[datetime, bytes]) -> None:
        self.documents = {cwaradar.history_url(slot): payload for slot, payload in documents.items()}
        self.requests: list[str] = []

    def get(self, url: str) -> bytes | None:
        self.requests.append(url)
        return self.documents.get(url)

    def exists(self, url: str) -> bool:
        self.requests.append(url)
        return url in self.documents


class RegistryTests(unittest.TestCase):
    def test_the_source_is_a_fetched_window_of_reflectivity(self) -> None:
        self.assertEqual((CWA.window_hours, CWA.cadence_seconds, CWA.cycle_hours), (24, 600, 1))
        # No half tier: a small grid, and every store object is a write per round.
        self.assertEqual(CWA.variant_factors, ())
        self.assertEqual((CWA.input_variable_ids, CWA.bundle_scalar_ids, CWA.core_bundle_ids), (("cref",),) * 3)
        self.assertTrue(CWA.observation and CWA.series_file and CWA.fetched and CWA.live)
        self.assertEqual((CWA.manifest_model, CWA.product), ("CWA-RADAR", "taiwan-cref"))

    def test_the_grid_is_the_agency_s_grid(self) -> None:
        self.assertEqual(CWA.production_grid, cwaradar.GRID_SHAPE[::-1])

    def test_the_catalog_prose_carries_the_required_attribution(self) -> None:
        prose = _source_prose(CWA)
        self.assertEqual(prose["providers"][0]["name"], "Central Weather Administration")
        self.assertIn("TWD67", prose["description"])
        self.assertEqual(prose["links"][0]["rel"], "license")
        self.assertIn("Central Weather Administration", prose["links"][0]["title"])

    def test_the_feed_is_registered_under_the_source_s_family(self) -> None:
        self.assertIsInstance(fetcher(CWA), CwaRadar)


class PathTests(unittest.TestCase):
    def test_a_slot_s_path_is_named_in_taiwan_time(self) -> None:
        self.assertEqual(
            cwaradar.history_url(EARLY_TIME),
            "https://cwaopendata.s3.ap-northeast-1.amazonaws.com/history/Observation/202611271920compref_mosaic.xml",
        )
        # 20:00 UTC is the next day in Taiwan.
        self.assertTrue(cwaradar.history_url(datetime(2026, 11, 27, 20, 0, tzinfo=UTC)).endswith("/202611280400compref_mosaic.xml"))

    def test_floor_slot_takes_the_ten_minute_mark_at_or_before(self) -> None:
        self.assertEqual(cwaradar.floor_slot(datetime(2026, 11, 27, 11, 29, 59, tzinfo=UTC)), EARLY_TIME)
        self.assertEqual(cwaradar.floor_slot(LATE_TIME), LATE_TIME)

    def test_a_window_s_slots_run_from_the_hour_to_the_newest(self) -> None:
        slots = cwaradar.window_slots(RUN, 3, datetime(2026, 11, 27, 11, 37, tzinfo=UTC))
        self.assertEqual(slots, [RUN.time + timedelta(minutes=10 * index) for index in range(4)])
        self.assertEqual(len(cwaradar.window_slots(RUN, 3, datetime(2026, 11, 28, tzinfo=UTC))), 19)


class ParseTests(unittest.TestCase):
    def test_a_document_is_read_in_utc_with_its_sentinels_missing(self) -> None:
        mosaic = cwaradar.parse(EARLY.read_bytes())
        self.assertEqual(mosaic.valid_time, EARLY_TIME)
        self.assertEqual(mosaic.values.shape, (40, 40))
        self.assertEqual((mosaic.first_longitude, mosaic.first_latitude, mosaic.step), (122.6, 25.5, 0.0125))
        text = EARLY.read_text(encoding="utf-8")
        raw = np.array(text[text.index("<content>") + 9 : text.index("</content>")].split(","), dtype=float)
        raw = raw.reshape(40, 40)
        np.testing.assert_array_equal(np.isnan(mosaic.values), np.isin(raw, (-99.0, -999.0)))
        self.assertTrue((raw > 0).any(), "the fixture holds echoes")
        np.testing.assert_array_equal(mosaic.values[raw > -90], raw[raw > -90])

    def test_the_axes_are_moved_onto_wgs84_by_one_offset(self) -> None:
        mosaic = cwaradar.parse(EARLY.read_bytes())
        self.assertEqual(mosaic.longitudes[0], 122.6081)
        self.assertEqual(mosaic.latitudes[0], 25.4982)
        self.assertEqual(mosaic.latitudes[-1], 25.9857)
        np.testing.assert_allclose(np.diff(mosaic.longitudes), 0.0125, atol=1e-12)

    def test_a_short_document_is_refused(self) -> None:
        text = EARLY.read_text(encoding="utf-8")
        content = text[text.index("<content>") + 9 : text.index("</content>")]
        with self.assertRaisesRegex(DownloadError, "1599 values"):
            cwaradar.parse(_with_content(text, content.rsplit(",", 1)[0]))

    def test_a_document_that_is_not_xml_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "not XML"):
            cwaradar.parse(b"<Error><Code>NoSuchKey</Code>")

    def test_a_time_off_the_ten_minute_marks_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "ten-minute"):
            cwaradar.parse_time("2026-11-27T19:25:00+08:00")

    def test_a_time_without_an_offset_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "offset"):
            cwaradar.parse_time("2026-11-27T19:20:00")

    def test_another_unit_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "not dBZ"):
            cwaradar.parse(_with_fields(EARLY.read_text(encoding="utf-8"), Reflectivity="mm"))

    def test_the_published_grid_passes_and_a_crop_does_not(self) -> None:
        cwaradar.check_grid(cwaradar.parse(_full(EARLY_TIME)))
        with self.assertRaisesRegex(DownloadError, "grid moved"):
            cwaradar.check_grid(cwaradar.parse(EARLY.read_bytes()))

    def test_a_frame_name_and_its_time_round_trip(self) -> None:
        name = cwaradar.frame_name(EARLY_TIME)
        self.assertEqual(name, "cwa_20261127112000.nc")
        self.assertEqual(cwaradar.frame_time(Path(name)), EARLY_TIME)


class LatestTests(unittest.TestCase):
    def test_the_newest_slot_is_probed_back_from_the_clock(self) -> None:
        bucket = Bucket({EARLY_TIME: b"", LATE_TIME: b""})
        # 11:46: 11:40 is not written yet, 11:30 is.
        slot = cwaradar.latest_slot(now=datetime(2026, 11, 27, 11, 46, tzinfo=UTC), exists=bucket.exists)
        self.assertEqual(slot, LATE_TIME)
        self.assertEqual(bucket.requests, [cwaradar.history_url(LATE_TIME + timedelta(minutes=10)), cwaradar.history_url(LATE_TIME)])

    def test_an_hour_without_a_mosaic_is_an_error(self) -> None:
        with self.assertRaisesRegex(DownloadError, "no CWA radar mosaic"):
            cwaradar.latest_slot(now=datetime(2026, 11, 27, 13, 0, tzinfo=UTC), exists=Bucket({EARLY_TIME: b""}).exists)

    def test_a_window_starts_by_the_plain_rule(self) -> None:
        # The history reaches a whole window back from a cold cache.
        newest = datetime(2026, 11, 27, 11, 30, tzinfo=UTC)
        self.assertEqual(observation_window_start(CWA, newest, 3, Path("/nonexistent")), datetime(2026, 11, 27, 9, 0, tzinfo=UTC))


@unittest.skipUnless(NETCDF, "the cwa group (xarray + netCDF4) is not installed")
class FetchTests(TempRoot, unittest.TestCase):
    root_prefix = "xue-cwa-"

    def _fetch(self, bucket: Bucket, now: datetime, *, force: bool = False) -> dict:
        cwaradar.fetch_run(CWA, RUN, 3, self.root, force=force, input_ids=None, fetch_or_none=bucket.get, now=now)
        return json.loads((self.root / f"cwa.{RUN.id}" / "fetch.json").read_text(encoding="utf-8"))

    def test_a_fetch_reads_every_slot_the_cache_lacks_and_skips_the_404s(self) -> None:
        slots = [RUN.time + timedelta(minutes=10 * index) for index in range(5)]
        # 11:10 never written: a gap in the axis, not an error.
        bucket = Bucket({slot: _full(slot) for slot in slots if slot.minute != 10})
        record = self._fetch(bucket, datetime(2026, 11, 27, 11, 45, tzinfo=UTC))
        self.assertEqual((record["model"], record["cadenceSeconds"]), ("cwa", 600))
        self.assertEqual(record["grid"], {"width": 921, "height": 881})
        self.assertEqual(
            [frame["slot"] for frame in record["frames"]],
            ["2026-11-27T11:00:00Z", "2026-11-27T11:20:00Z", "2026-11-27T11:30:00Z", "2026-11-27T11:40:00Z"],
        )
        self.assertEqual(bucket.requests, [cwaradar.history_url(slot) for slot in slots])
        values, latitudes, longitudes = cwaradar.read_frame(
            self.root / cwaradar.FRAMES_DIRNAME / cwaradar.VARIABLE_DIR / cwaradar.frame_name(EARLY_TIME)
        )
        self.assertEqual(values.shape, cwaradar.GRID_SHAPE)
        self.assertEqual((longitudes[0], latitudes[0]), (115.0081, 17.9982))
        np.testing.assert_allclose(values[0, :60], np.arange(60), atol=0.05)
        self.assertTrue(np.isnan(values[0, 60:]).all() and np.isnan(values[-1, -1]))

        # The next round asks only for what the cache lacks: the gap and
        # the new slot.
        bucket.documents[cwaradar.history_url(slots[-1] + timedelta(minutes=10))] = _full(slots[-1] + timedelta(minutes=10))
        bucket.requests.clear()
        record = self._fetch(bucket, datetime(2026, 11, 27, 11, 57, tzinfo=UTC))
        self.assertEqual(bucket.requests, [cwaradar.history_url(slots[1]), cwaradar.history_url(slots[-1] + timedelta(minutes=10))])
        self.assertEqual(len(record["frames"]), 5)

    def test_force_reads_the_cached_slots_again(self) -> None:
        bucket = Bucket({RUN.time: _full(RUN.time)})
        self._fetch(bucket, RUN.time)
        bucket.requests.clear()
        self._fetch(bucket, RUN.time, force=True)
        self.assertEqual(bucket.requests, [cwaradar.history_url(RUN.time)])

    def test_a_document_under_another_slot_s_path_is_refused(self) -> None:
        bucket = Bucket({RUN.time: _full(EARLY_TIME)})
        with self.assertRaisesRegex(DownloadError, "holds the mosaic of"):
            self._fetch(bucket, RUN.time)

    def test_a_crop_is_refused_by_the_fetch(self) -> None:
        bucket = Bucket({EARLY_TIME: EARLY.read_bytes()})
        with self.assertRaisesRegex(DownloadError, "grid moved"):
            self._fetch(bucket, EARLY_TIME)

    def test_a_window_with_no_mosaic_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "holds no frame"):
            self._fetch(Bucket({}), LATE_TIME)

    def test_an_input_the_source_does_not_publish_is_refused(self) -> None:
        with self.assertRaisesRegex(DownloadError, "publishes"):
            cwaradar.fetch_run(CWA, RUN, 3, self.root, force=False, input_ids=("prate",), fetch_or_none=lambda url: None)


@requires_gdal
@unittest.skipUnless(NETCDF, "the cwa group (xarray + netCDF4) is not installed")
class ConversionTests(ClassTempRoot, unittest.TestCase):
    """The two cropped mosaics as one window through the reference encoder,
    and the native encoder held to it byte for byte."""

    root_prefix = "xue-cwa-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        frames_dir = cls.root / cwaradar.FRAMES_DIRNAME
        cls.mosaics = [cwaradar.parse(path.read_bytes()) for path in (EARLY, LATE)]
        for mosaic in cls.mosaics:
            cwaradar.write_frame(frames_dir, mosaic)
        cls.inputs = cls.root / f"cwa.{RUN.id}"
        cwaradar.write_window(frames_dir, RUN.time, 3, cls.inputs / cwaradar.series_name(RUN))
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                cls.inputs,
                cls.root / "out",
                model="cwa",
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    def test_the_window_is_a_ten_minute_axis_on_wgs84(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["cref"])
        out = self.root / "out"
        manifest = json.loads((out / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("CWA-RADAR", "taiwan-cref"))
        self.assertEqual(manifest["runTime"], "2026-11-27T11:00:00Z")
        bundle = stored_bundle(out, "cref")
        # 11:20 and 11:30 from 11:00.
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 600, "firstFrameOffset": 2, "frameCount": 2, "frameStep": 1})
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (40, 40))
        self.assertAlmostEqual(grid["firstLongitude"], 122.6081, places=9)
        self.assertAlmostEqual(grid["firstLatitude"], 25.9857, places=9)
        self.assertAlmostEqual(grid["longitudeStep"], 0.0125, places=12)
        self.assertAlmostEqual(grid["latitudeStep"], -0.0125, places=12)
        self.assertEqual(bundle.metadata["variables"][0]["unit"], "dBZ")
        for frame, offset in enumerate((2, 3)):
            codes = np.asarray(bundle.decode_plane(1, offset)).reshape(40, 40)
            values = self.mosaics[frame].values[::-1]
            # North row first in the bundle; an echo where the mosaic has one
            # of at least half a codebook step (the bottom code is no echo,
            # no radar and anything that rounds to 0 dBZ alike).
            np.testing.assert_array_equal(codes > 0, np.nan_to_num(values, nan=-1.0) >= 0.25)

    @requires_native_source("cwa")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, self.inputs, self.root / "out", self.report, self.root / "native", model="cwa")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

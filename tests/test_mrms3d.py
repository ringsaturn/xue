"""The MRMS reflectivity volume: the radar mosaic's 33 constant-altitude
levels (500 m to 19 km above mean sea level), published as one bundle.

Three things are new with it. A published cadence coarser than the
bucket's (``SourceSpec.object_cadence_seconds``): the levels are scanned
every two minutes and published every ten, so a frame is the scan stamped
in ``[mark, mark + 120 s)`` and the scans between the marks serve no slot.
A bundle kind of its own (``VOLUME_BUNDLES``): many variables, each read
straight off its own record, written as one bundle in level order. And
record matching that tells levels apart: the 33 records share an element
and differ only in the height of their fixed surface.

``tests/fixtures/mrms3d.2026100423.t0030.crop.grib2`` and ``t0040`` are a
300 x 140 cell window (81.75W to 78.75W, 32N to 30.6N, the Georgia and
South Carolina coast) of the 2026-10-04 23:30:40 and 23:40:38 scans, the 33
levels concatenated in level order the way the fetcher assembles a frame:
a line of storms whose echoes reach 17 km, below the lowest beams over the
sea at the low levels (-999) and clear air above the tops (-99).
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import struct
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import (
    ClassTempRoot,
    FIXTURES,
    TempRoot,
    assert_gdalinfo_agrees,
    assert_native_matches,
    requires_gdalinfo,
    requires_native_source,
)
from xuebuild import binconvert, binformat, fetch, grib2, stac
from xuebuild.binconvert import (
    RAW_VARIABLE_IDS,
    VOLUME_BUNDLES,
    bundle_input_ids,
    bundle_variable_ids,
    published_bundle_ids,
)
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    MRMS_BASE_URL,
    MrmsObject,
    _download_mrms_frame,
    _mrms_run_is_complete,
    latest_mrms_slot,
    latest_observation_slot,
    mrms_frame_name,
    mrms_object_slot,
    mrms_window_frames,
    resolve_run,
)
from xuebuild.gdal import _band_matches, raster_expression
from xuebuild.model import GfsRun
from xuebuild.quantize import PROFILES
from xuebuild.sources import SOURCES, Downsample, source_spec
from xuebuild.variables import REFLECTIVITY_LEVELS_M, REFLECTIVITY_VARIABLE_IDS, reflectivity_level, variable_spec

FRAMES = (FIXTURES / "mrms3d.2026100423.t0030.crop.grib2", FIXTURES / "mrms3d.2026100423.t0040.crop.grib2")
STAMPS = ("233040", "234038")
MRMS3D = source_spec("mrms3d")
RUN = GfsRun(datetime(2026, 10, 4, 23, tzinfo=UTC))
DAY = "20261004"


def product(level_m: int) -> str:
    return f"MergedReflectivityQC_{level_m / 1000:05.2f}"


def level_key(level_m: int, day: str, time: str) -> str:
    return f"CONUS/{product(level_m)}/{day}/MRMS_{product(level_m)}_{day}-{time}.grib2.gz"


def listing(*keys: str) -> str:
    """A ``ListObjectsV2`` page the way the bucket answers one."""
    contents = "".join(f"<Contents><Key>{key}</Key><Size>1</Size></Contents>" for key in keys)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>noaa-mrms-pds</Name>'
        f"<KeyCount>{len(keys)}</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated>"
        f"{contents}</ListBucketResult>"
    )


def bucket(times: dict[int, list[str]] | list[str], day: str = DAY):
    """A ``fetch_text`` listing the given HHMMSS stamps for every level of
    ``day`` (one list for all levels, or one per level by metres)."""

    def fetch_text(url: str) -> str:
        if day.replace("-", "") not in url:
            return listing()
        for level in REFLECTIVITY_LEVELS_M:
            if f"{product(level)}%2F" in url:
                stamps = times if isinstance(times, list) else times.get(level, [])
                return listing(*(level_key(level, day, stamp) for stamp in stamps))
        return listing()

    return fetch_text


def split_messages(path: Path) -> list[bytes]:
    """The GRIB messages of one file, by the indicator section's length."""
    data = path.read_bytes()
    messages = []
    offset = 0
    while offset < len(data):
        length = struct.unpack_from(">Q", data, offset + 8)[0]
        messages.append(data[offset : offset + length])
        offset += length
    return messages


class RegistryTests(unittest.TestCase):
    def test_the_source_is_a_ten_minute_volume_of_two_minute_scans(self) -> None:
        self.assertTrue(MRMS3D.observation and MRMS3D.fetched and MRMS3D.live)
        self.assertTrue(MRMS3D.mrms)
        self.assertEqual((MRMS3D.window_hours, MRMS3D.cycle_hours), (3, 1))
        self.assertEqual((MRMS3D.cadence_seconds, MRMS3D.object_cadence_seconds), (600, 120))
        self.assertEqual(MRMS3D.downsample, Downsample(factor=5))
        self.assertEqual((MRMS3D.production_grid, MRMS3D.tile, MRMS3D.variant_factors), ((1400, 700), (50, 50), (2,)))
        self.assertEqual(MRMS3D.fetch_concurrency, 8)
        self.assertFalse(MRMS3D.video or MRMS3D.accumulated_precipitation)
        self.assertEqual((MRMS3D.steps, MRMS3D.bundle_scalar_ids, MRMS3D.bundle_vector_ids, MRMS3D.bundle_composite_ids), ((), (), (), ()))
        self.assertEqual((MRMS3D.bundle_volume_ids, MRMS3D.core_bundle_ids), (("refl3d",), ("refl3d",)))
        self.assertEqual(MRMS3D.input_variable_ids, REFLECTIVITY_VARIABLE_IDS)
        # The fetch dispatch keys off the products, not the id: the composite
        # mosaic and the volume are MRMS, the JMA nowcast (whose prate is an
        # MRMS product's quantity too) is a series file and is not.
        self.assertEqual([source.id for source in SOURCES.values() if source.mrms], ["mrms", "mrms3d"])
        self.assertIsNone(source_spec("mrms").object_cadence_seconds)

    def test_the_levels_are_a_family_in_bundle_order(self) -> None:
        self.assertEqual(len(REFLECTIVITY_LEVELS_M), 33)
        self.assertEqual(REFLECTIVITY_LEVELS_M[:3], (500, 750, 1000))
        self.assertEqual(REFLECTIVITY_LEVELS_M[-3:], (17000, 18000, 19000))
        self.assertEqual(list(REFLECTIVITY_LEVELS_M), sorted(REFLECTIVITY_LEVELS_M))
        self.assertEqual(REFLECTIVITY_VARIABLE_IDS[0], "refl500")
        self.assertEqual(REFLECTIVITY_VARIABLE_IDS[-1], "refl19000")
        self.assertEqual((reflectivity_level("refl3000"), reflectivity_level("refl3250"), reflectivity_level("cref")), (3000, None, None))
        for level, label, name in (
            (500, "Radar reflectivity at 0.5 km MSL", "MergedReflectivityQC_00.50"),
            (750, "Radar reflectivity at 0.75 km MSL", "MergedReflectivityQC_00.75"),
            (3000, "Radar reflectivity at 3 km MSL", "MergedReflectivityQC_03.00"),
            (19000, "Radar reflectivity at 19 km MSL", "MergedReflectivityQC_19.00"),
        ):
            spec = variable_spec(f"refl{level}")
            self.assertEqual((spec.label, spec.mrms_product, spec.output_unit, spec.value_range), (label, name, "dBZ", (0, 80)))
            self.assertEqual((spec.grib_element, spec.gdal_unit, spec.fill_values, spec.index_field), ("MergedReflectivityQC", "dBZ", (-999.0, -99.0), ""))
            self.assertEqual(
                spec.parameter_metadata(),
                {
                    "discipline": 209,
                    "parameterCategory": 9,
                    "parameterNumber": 0,
                    "typeOfFirstFixedSurface": 102,
                    "scaleFactorOfFirstFixedSurface": 0,
                    "scaledValueOfFirstFixedSurface": level,
                },
            )

    def test_the_volume_bundle_is_its_levels(self) -> None:
        self.assertEqual(VOLUME_BUNDLES, {"refl3d": REFLECTIVITY_VARIABLE_IDS})
        self.assertEqual(bundle_variable_ids("refl3d"), REFLECTIVITY_VARIABLE_IDS)
        self.assertEqual(bundle_input_ids(MRMS3D, "refl3d"), REFLECTIVITY_VARIABLE_IDS)
        self.assertEqual(published_bundle_ids(MRMS3D), ("refl3d",))
        # A volume listed without every level fetched does not ship.
        import dataclasses

        short = dataclasses.replace(MRMS3D, input_variable_ids=REFLECTIVITY_VARIABLE_IDS[:-1])
        self.assertEqual(published_bundle_ids(short), ())

    def test_the_codebooks_and_predictor_are_the_composites(self) -> None:
        for profile, books in PROFILES.items():
            for variable_id in REFLECTIVITY_VARIABLE_IDS:
                self.assertEqual(books[variable_id], books["cref"], f"{profile} {variable_id}")
        self.assertTrue(set(REFLECTIVITY_VARIABLE_IDS) <= RAW_VARIABLE_IDS)
        for variable_id in ("refl500", "refl19000"):
            self.assertEqual(raster_expression(variable_id, "dBZ"), "maximum(0,minimum(80,A))")
        with self.assertRaises(ConversionError):
            raster_expression("refl500", "mm/hr")


class SlotRuleTests(unittest.TestCase):
    def item(self, level: int, stamp: str) -> MrmsObject:
        return MrmsObject(level_key(level, DAY, stamp), datetime.strptime(DAY + stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC))

    def test_only_the_scan_after_each_mark_serves_a_slot(self) -> None:
        mark = datetime(2026, 10, 4, 23, 30, tzinfo=UTC)
        self.assertEqual(mrms_object_slot(MRMS3D, self.item(500, "233040")), mark)
        self.assertEqual(mrms_object_slot(MRMS3D, self.item(500, "233000")), mark)
        self.assertEqual(mrms_object_slot(MRMS3D, self.item(500, "233159")), mark)
        for stamp in ("233200", "233241", "233440", "233641", "233840", "232959"):
            self.assertIsNone(mrms_object_slot(MRMS3D, self.item(500, stamp)), stamp)
        self.assertEqual(mrms_object_slot(MRMS3D, self.item(500, "234038")), mark + timedelta(minutes=10))
        # The composite mosaic keeps every two-minute slot, as it always did.
        mrms = source_spec("mrms")
        self.assertEqual(mrms_object_slot(mrms, self.item(500, "233241")), datetime(2026, 10, 4, 23, 32, tzinfo=UTC))

    def test_a_window_is_the_marks_every_level_has_from_one_scan(self) -> None:
        every_scan = [f"23{minute:02d}40" for minute in range(0, 60, 2)]
        times: dict[int, list[str]] = {level: list(every_scan) for level in REFLECTIVITY_LEVELS_M}
        # The 23:50 scan of one level never landed: its next scan, 23:52,
        # is not at a mark, so 23:50 is a gap rather than a frame stitched
        # from two scans.
        times[7000].remove("235040")
        # A reissue of the 23:20 scan in the same two minutes wins, as on
        # the composite.
        times[500].append("232059")
        frames = mrms_window_frames(MRMS3D, RUN, 1, fetch=bucket(times))
        self.assertEqual(
            [slot.strftime("%H:%M") for slot in frames], ["23:00", "23:10", "23:20", "23:30", "23:40"]
        )
        for slot, objects in frames.items():
            self.assertEqual(tuple(objects), REFLECTIVITY_VARIABLE_IDS)
            observed = {item.observed for variable_id, item in objects.items() if variable_id != "refl500"}
            self.assertEqual(observed, {slot + timedelta(seconds=40)}, slot)
        self.assertEqual(frames[datetime(2026, 10, 4, 23, 20, tzinfo=UTC)]["refl500"].observed.second, 59)
        # Narrowed to the levels a group fetches, the gap is that level's alone.
        only = mrms_window_frames(MRMS3D, RUN, 1, ("refl500", "refl1000"), fetch=bucket(times))
        self.assertEqual(len(only), 6)
        self.assertEqual(list(next(iter(only.values()))), ["refl500", "refl1000"])

    def test_the_frame_name_is_the_marks_offset(self) -> None:
        self.assertEqual(
            mrms_frame_name(MRMS3D, RUN, datetime(2026, 10, 4, 23, 30, tzinfo=UTC)), "mrms3d.2026100423.t0030.grib2"
        )

    def test_the_live_window_ends_at_the_newest_common_mark(self) -> None:
        now = datetime(2026, 10, 4, 23, 55, tzinfo=UTC)
        scans = [f"23{minute:02d}40" for minute in range(0, 54, 2)]
        # The 23:52 scan is newer than the 23:50 mark but serves no slot.
        self.assertEqual(latest_mrms_slot(MRMS3D, now=now, fetch=bucket(scans)), datetime(2026, 10, 4, 23, 50, tzinfo=UTC))
        times = {level: list(scans) for level in REFLECTIVITY_LEVELS_M}
        times[19000].remove("235040")
        self.assertEqual(latest_mrms_slot(MRMS3D, now=now, fetch=bucket(times)), datetime(2026, 10, 4, 23, 40, tzinfo=UTC))
        with mock.patch("xuebuild.fetch.fetch_text", bucket(scans)):
            self.assertEqual(latest_observation_slot(MRMS3D, now=now), datetime(2026, 10, 4, 23, 50, tzinfo=UTC))
            self.assertEqual(resolve_run("latest", hours=1, now=now, model="mrms3d").id, "2026100423")
            self.assertEqual(resolve_run("latest", hours=3, now=now, model="mrms3d").id, "2026100421")
        with self.assertRaisesRegex(DownloadError, "no frame of every product"):
            latest_mrms_slot(MRMS3D, now=now, fetch=bucket(["235240"]))

    def test_a_window_is_complete_once_every_level_has_moved_past_it(self) -> None:
        run = GfsRun(datetime(2026, 10, 4, 22, tzinfo=UTC))
        times = {level: ["220040", "230040"] for level in REFLECTIVITY_LEVELS_M}
        self.assertTrue(_mrms_run_is_complete(MRMS3D, run, 1, fetch=bucket(times)))
        times[11000] = ["220040", "225840"]
        self.assertFalse(_mrms_run_is_complete(MRMS3D, run, 1, fetch=bucket(times)))
        # Any scan past the end proves it, not only one at a mark.
        times[11000] = ["220040", "230240"]
        self.assertTrue(_mrms_run_is_complete(MRMS3D, run, 1, fetch=bucket(times)))
        with mock.patch("xuebuild.fetch.fetch_text", bucket(times)):
            self.assertEqual(resolve_run("2026100422", hours=1, model="mrms3d").id, "2026100422")


class RecordMatchingTests(unittest.TestCase):
    def metadata(self, level: int, *, assembled: bool = True) -> dict[str, str]:
        record = {
            "GRIB_ELEMENT": "MergedReflectivityQC",
            "GRIB_SHORT_NAME": f"{level}-GPML",
            "GRIB_DISCIPLINE": "209",
            "GRIB_UNIT": "[dBZ]",
            "GRIB_COMMENT": "3D Reflectivty Mosaic - 33 CAPPIS (500-19000m) [dBZ]",
        }
        if assembled:
            record["GRIB_PDS_PDTN"] = "0"
            record["GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES"] = f"9 0 8 0 97 0 0 0 0 102 0 {level} 255 1 0"
        return record

    def test_each_level_matches_its_own_record_alone(self) -> None:
        for assembled in (True, False):
            for level in (500, 3000, 19000):
                record = self.metadata(level, assembled=assembled)
                description = f'{level}[m] GPML="Specific altitude above mean sea level"'
                matches = [variable_id for variable_id in REFLECTIVITY_VARIABLE_IDS if _band_matches(variable_id, record, description)]
                self.assertEqual(matches, [f"refl{level}"], (assembled, level))
                # The composite is another element; the volume's 500 m level
                # is not the composite though both sit on 500 m.
                self.assertFalse(_band_matches("cref", record, description))
        record = self.metadata(3000)
        self.assertFalse(_band_matches("refl3000", {**record, "GRIB_DISCIPLINE": "0"}, ""))
        # Another parameter on the same surface is not the reflectivity.
        other = {**record, "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": "9 1 8 0 97 0 0 0 0 102 0 3000 255 1 0"}
        self.assertFalse(_band_matches("refl3000", other, ""))
        composite = {
            "GRIB_ELEMENT": "MergedReflectivityQCComposite",
            "GRIB_SHORT_NAME": "500-GPML",
            "GRIB_DISCIPLINE": "209",
            "GRIB_PDS_PDTN": "0",
            "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": "10 0 8 0 97 0 0 0 0 102 0 500 255 1 0",
        }
        self.assertTrue(_band_matches("cref", composite, ""))
        self.assertFalse(_band_matches("refl500", composite, ""))
        # The composite's level is checked too now: a composite stamped on
        # another surface is not the registered alternate.
        self.assertFalse(
            _band_matches("cref", {**composite, "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": "10 0 8 0 97 0 0 0 0 102 0 1000 255 1 0"}, "")
        )

    def test_every_level_is_found_in_the_fixture_in_order(self) -> None:
        for path, stamp in zip(FRAMES, STAMPS):
            frames = grib2.inspect_grib_fast(path, MRMS3D.input_variable_ids)
            self.assertEqual(tuple(frames), REFLECTIVITY_VARIABLE_IDS)
            self.assertEqual([frame.band for frame in frames.values()], list(range(1, 34)))
            self.assertEqual({frame.unit for frame in frames.values()}, {"dBZ"})
            # One scan: every level carries the scan's own time.
            observed = datetime.strptime(DAY + stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
            self.assertEqual({frame.valid_time for frame in frames.values()}, {observed})
            self.assertEqual({frame.lead_seconds for frame in frames.values()}, {0})

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        assert_gdalinfo_agrees(self, [(path, MRMS3D.input_variable_ids, ()) for path in FRAMES])


class DownloadTests(TempRoot, unittest.TestCase):
    """The fixture's messages served back as the bucket's gzipped objects."""

    root_prefix = "xue-mrms3d-fetch-"

    def setUp(self) -> None:
        super().setUp()
        self.objects: dict[str, bytes] = {}
        for path, stamp in zip(FRAMES, STAMPS):
            messages = split_messages(path)
            self.assertEqual(len(messages), 33)
            for level, message in zip(REFLECTIVITY_LEVELS_M, messages):
                self.objects[level_key(level, DAY, stamp)] = message
        self.requested: list[str] = []

    def request(self, url: str, **_: object) -> object:
        key = url.removeprefix(f"{MRMS_BASE_URL}/")
        self.requested.append(key)
        body = gzip.compress(self.objects[key])

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def read(self):
                return body

        return Response()

    @requires_gdalinfo
    def test_a_window_is_fetched_one_scan_per_mark(self) -> None:
        # Every two-minute scan is listed; only the two at a mark are fetched.
        scans = ["232840", "233040", "233240", "233440", "233640", "233840", "234038", "234240"]
        with (
            mock.patch("xuebuild.fetch._request", self.request),
            mock.patch("xuebuild.fetch.fetch_text", bucket(scans)),
            mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}),
        ):
            paths = fetch.fetch_run(RUN, 1, self.root, model="mrms3d", input_ids=None)
        self.assertEqual([path.name for path in paths], ["mrms3d.2026100423.t0030.grib2", "mrms3d.2026100423.t0040.grib2"])
        for path, fixture in zip(paths, FRAMES):
            self.assertEqual(path.read_bytes(), fixture.read_bytes())
        self.assertEqual(len(self.requested), 66)
        record = json.loads((self.root / "mrms3d.2026100423" / "fetch.json").read_text())
        self.assertEqual((record["model"], record["cadenceSeconds"]), ("mrms3d", 600))
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-10-04T23:30:00Z", "2026-10-04T23:40:00Z"])
        self.assertEqual(list(record["frames"][0]["objects"]), list(REFLECTIVITY_VARIABLE_IDS))
        self.assertEqual(record["frames"][1]["objects"]["refl19000"]["observed"], "2026-10-04T23:40:38Z")
        # Reused when readable: one gdalinfo pass checks all 33 levels.
        slot = datetime(2026, 10, 4, 23, 30, tzinfo=UTC)
        objects = {
            variable_id: MrmsObject(level_key(level, DAY, "233040"), slot + timedelta(seconds=40))
            for variable_id, level in zip(REFLECTIVITY_VARIABLE_IDS, REFLECTIVITY_LEVELS_M)
        }
        with (
            mock.patch("xuebuild.fetch._request", mock.Mock(side_effect=AssertionError("no download"))),
            mock.patch("xuebuild.gdal.dataset_info", wraps=__import__("xuebuild.gdal", fromlist=["dataset_info"]).dataset_info) as info,
            mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}),
        ):
            self.assertEqual(_download_mrms_frame(MRMS3D, RUN, slot, objects, self.root / "mrms3d.2026100423", force=False), paths[0])
        self.assertEqual(info.call_count, 1)


@requires_gdalinfo
class ConversionTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-mrms3d-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        inputs = cls.root / "mrms3d.2026100423"
        inputs.mkdir()
        for path in FRAMES:
            shutil.copy(path, inputs / path.name.replace(".crop", ""))
        cls.inputs = inputs
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                inputs,
                cls.root / "out",
                model="mrms3d",
                skip_video=True,
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    def test_one_volume_bundle_on_a_ten_minute_axis(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["refl3d"])
        self.assertEqual((self.report["posters"], self.report["videos"]), ([], []))
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"], manifest["runTime"]), ("NOAA-MRMS3D", "conus-refl3d", "2026-10-04T23:00:00Z"))
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], ["refl3d"])
        self.assertNotIn("poster", manifest["bundles"][0])
        self.assertNotIn("video", manifest["bundles"][0])
        self.assertEqual(sorted(path.name for path in (self.root / "out").iterdir()), ["manifest.json", "refl3d.half.xue", "refl3d.xue"])
        bundle = read_bundle(self.root / "out" / "refl3d.xue")
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 600, "firstFrameOffset": 3, "frameCount": 2, "frameStep": 1})
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (60, 28))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.05, -0.05))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (-81.725, 31.975))
        # Two tiles of 50 across the 60-cell side, one temporal group.
        self.assertEqual((bundle.frame_count, len(bundle.groups), bundle.tiles.count), (2, 1, 2))

    def test_the_bundle_carries_every_level_in_order_with_its_identity(self) -> None:
        bundle = read_bundle(self.root / "out" / "refl3d.xue")
        variables = bundle.metadata["variables"]
        self.assertEqual([variable["id"] for variable in variables], list(REFLECTIVITY_VARIABLE_IDS))
        self.assertEqual([variable["numericId"] for variable in variables], list(range(1, 34)))
        for variable, level in zip(variables, REFLECTIVITY_LEVELS_M):
            self.assertEqual(variable["parameter"], variable_spec(f"refl{level}").parameter_metadata())
            self.assertEqual(variable["unit"], "dBZ")
            self.assertEqual(variable["quantization"], PROFILES["quality"]["cref"].metadata())
        self.assertEqual(variables[10]["label"], "Radar reflectivity at 3 km MSL")
        self.assertEqual({entry.predictor for entry in bundle.variable_entries}, {binformat.PREDICTOR_RAW})
        # The storm is in the codes: echo near the ground over a fraction of
        # the window (the beams overshoot the low levels offshore), echo
        # aloft over most of it, tops near 17 km and nothing at 19.
        maxima = {}
        for numeric_id, variable_id in enumerate(REFLECTIVITY_VARIABLE_IDS, start=1):
            codes = np.asarray(bundle.decode_plane(numeric_id, 3))
            maxima[variable_id] = codes.max() / 2
        self.assertGreater(maxima["refl500"], 50)
        self.assertGreater(maxima["refl8000"], 35)
        self.assertGreater(maxima["refl15000"], 15)
        self.assertEqual(maxima["refl19000"], 0)
        low = np.asarray(bundle.decode_plane(1, 3))
        aloft = np.asarray(bundle.decode_plane(REFLECTIVITY_VARIABLE_IDS.index("refl5000") + 1, 3))
        self.assertGreater((aloft > 0).mean(), 2 * (low > 0).mean())

    def test_the_half_variant(self) -> None:
        [variant] = self.report["variants"]
        self.assertEqual((variant["width"], variant["height"]), (30, 14))
        half = read_bundle(self.root / "out" / "refl3d.half.xue")
        self.assertEqual(len(half.metadata["variables"]), 33)
        self.assertEqual(half.tiles.count, 2)
        full = read_bundle(self.root / "out" / "refl3d.xue")
        for numeric_id in (1, 21):
            np.testing.assert_array_equal(
                np.asarray(half.decode_plane(numeric_id, 4)).reshape(14, 30),
                np.asarray(full.decode_plane(numeric_id, 4)).reshape(28, 60)[::2, ::2],
            )

    def test_the_catalog_lists_every_level(self) -> None:
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        variables = stac._variables_of(manifest, ["time", "latitude", "longitude"])
        self.assertEqual(list(variables), list(REFLECTIVITY_VARIABLE_IDS))
        self.assertEqual(variables["refl3000"]["xue:bundle"], "refl3d")
        self.assertEqual(variables["refl3000"]["description"], "Radar reflectivity at 3 km MSL")
        self.assertEqual(stac._bundle_title("refl3d"), "Radar reflectivity volume (33 CAPPI levels, 0.5–19 km MSL)")

    def test_a_complete_build_wants_the_production_grid(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "1400x700 grid"):
                binconvert.convert_bin(
                    self.inputs, self.root / "complete", model="mrms3d", skip_video=True, require_complete=True, expected_hours=3
                )

    @requires_native_source("mrms3d", "the MRMS reflectivity volume")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, self.inputs, self.root / "out", self.report, self.root / "native", model="mrms3d")


if __name__ == "__main__":
    unittest.main()

"""The MRMS source: the NOAA radar mosaic over the contiguous United States,
an observation that is fetched.

Four things are new with it. An observation source with a bucket
(``SourceSpec.window_hours``): a run is a window starting at the run's
hour, its frames are listed off the bucket rather than computed (the
composite is stamped a jittered forty seconds past each two-minute mark),
and each is one whole gzipped GRIB per product. A time axis snapped to the
product's cadence (``cadence_seconds``): the frame at 00:02:41 is the frame
at 00:02, and the window's first hour is the run time. A source published
coarser than it arrives (``Downsample``): every 2 x 2 block of the 0.01°
grid becomes its maximum on the 0.02° grid the bundles carry, and the grid
itself is described on its round step rather than the hair-off one GDAL
derives. And two registry alternates: the MRMS-local composite reflectivity
(209/10/0 on a 500 m surface) under ``cref`` and the radar precipitation
rate (209/6/1, already in mm/h) under ``prate``, with the product's
out-of-coverage and no-echo sentinels folded to the codebook bottom.

``tests/fixtures/mrms.2026091300.t0000.crop.grib2`` and ``t0002`` are a
160 x 160 cell window of the 2026-09-13 00:00:42 and 00:02:42 composites
with the 00:00 and 00:02 rates appended, the way the fetcher assembles a
frame, on the James Bay shore at the edge of the Canadian coverage: about a
quarter of the window is outside radar coverage, half inside it with no
echo, and a quarter a rain band.
"""

from __future__ import annotations

import filecmp
import gzip
import json
import math
import os
import shutil
import struct
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from xuebuild import binconvert, fetch, grib2, native, zstdcli
from xuebuild.binconvert import (
    BlockReduction,
    GridInfo,
    _extract_planes,
    _grid_info,
    _snap_axis,
    _snap_observation_frames,
    _snap_regional_steps,
    crop_grid,
    published_bundle_ids,
)
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError, ManifestError
from xuebuild.fetch import (
    MRMS_BASE_URL,
    MrmsObject,
    _download_mrms_frame,
    _fetch_mrms_run,
    _mrms_run_is_complete,
    list_mrms_objects,
    mrms_frame_name,
    mrms_object_url,
    mrms_product_prefix,
    mrms_window_frames,
    parse_mrms_listing,
    resolve_run,
)
from xuebuild.gdal import _band_matches, inspect_grib_multi, precipitation_rate_is_mm_per_hour, raster_expression
from xuebuild.manifest import MODEL_CORE_BUNDLES, build_bin_manifest, validate_bin_manifest
from xuebuild.model import GfsRun, SourceFrame
from xuebuild.sources import SOURCES, Downsample, source_spec
from xuebuild.variables import variable_spec

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FRAMES = (FIXTURES / "mrms.2026091300.t0000.crop.grib2", FIXTURES / "mrms.2026091300.t0002.crop.grib2")
MRMS = source_spec("mrms")
RUN = GfsRun(datetime(2026, 9, 13, 0, tzinfo=UTC))

requires_gdalinfo = unittest.skipUnless(shutil.which("gdalinfo") is not None, "gdalinfo is not on PATH")


def listing(*keys: str, truncated: bool = False) -> str:
    """A ``ListObjectsV2`` page the way the bucket answers one."""
    contents = "".join(
        f"<Contents><Key>{key}</Key><LastModified>2026-09-13T00:01:37.000Z</LastModified>"
        f"<Size>1553104</Size></Contents>"
        for key in keys
    )
    token = "<NextContinuationToken>abc/def+ghi=</NextContinuationToken>" if truncated else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>noaa-mrms-pds</Name>'
        f"<KeyCount>{len(keys)}</KeyCount><MaxKeys>1000</MaxKeys>"
        f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>{token}{contents}</ListBucketResult>"
    )


def cref_key(day: str, time: str) -> str:
    return f"CONUS/MergedReflectivityQCComposite_00.50/{day}/MRMS_MergedReflectivityQCComposite_00.50_{day}-{time}.grib2.gz"


def prate_key(day: str, time: str) -> str:
    return f"CONUS/PrecipRate_00.00/{day}/MRMS_PrecipRate_00.00_{day}-{time}.grib2.gz"


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


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_a_fetched_observation(self) -> None:
        self.assertEqual((MRMS.manifest_model, MRMS.product), ("NOAA-MRMS", "conus-cref"))
        self.assertTrue(MRMS.observation)
        self.assertTrue(MRMS.fetched)
        self.assertFalse(MRMS.live)
        self.assertIsNone(MRMS.latest_filename)
        self.assertEqual((MRMS.window_hours, MRMS.cadence_seconds), (3, 120))
        self.assertEqual(MRMS.horizon_hours, 3)
        self.assertEqual(MRMS.cycle_hours, 1)
        self.assertEqual(MRMS.downsample, Downsample(factor=2))
        self.assertIsNone(MRMS.regrid)
        self.assertEqual(MRMS.production_grid, (3500, 1750))
        self.assertFalse(MRMS.video)
        with self.assertRaisesRegex(DownloadError, "publishes no forecast axis"):
            MRMS.forecast_hours(3)
        # The local-file observation is still not fetched, and a forecast is.
        radar = source_spec("radar")
        self.assertTrue(radar.observation)
        self.assertFalse(radar.fetched)
        self.assertIsNone(radar.window_hours)
        self.assertTrue(source_spec("hrrr").fetched)
        for model in ("gfs", "ecmwf", "sflux", "hrrr", "radar"):
            self.assertIsNone(source_spec(model).downsample, model)
            self.assertIsNone(source_spec(model).cadence_seconds, model)

    def test_published_bundles_and_the_core_set(self) -> None:
        self.assertEqual(published_bundle_ids(MRMS), ("cref", "prate"))
        self.assertEqual(MRMS.core_bundle_ids, ("cref",))
        self.assertEqual(MODEL_CORE_BUNDLES["NOAA-MRMS"], ("cref",))
        self.assertEqual(MODEL_CORE_BUNDLES["CMA-RADAR"], ("cref",))
        for model in ("GFS", "ECMWF", "GFS-SFLUX", "HRRR"):
            self.assertEqual(MODEL_CORE_BUNDLES[model], ("tmp2m", "prate"), model)
        # A source's core set is a subset of what it publishes, or a
        # complete build could never validate.
        for source in SOURCES.values():
            for bundle_id in source.core_bundle_ids:
                self.assertIn(bundle_id, published_bundle_ids(source), source.id)

    def test_the_products_are_registered_on_the_variables(self) -> None:
        self.assertEqual(variable_spec("cref").mrms_product, "MergedReflectivityQCComposite_00.50")
        self.assertEqual(variable_spec("prate").mrms_product, "PrecipRate_00.00")
        self.assertEqual(variable_spec("tmp2m").mrms_product, "")


class ManifestTests(unittest.TestCase):
    def test_a_live_manifest_needs_the_reflectivity_and_nothing_else(self) -> None:
        run_time = datetime(2026, 9, 13, 0, tzinfo=UTC)
        cref = {"variable": "cref", "path": "mrms.2026091300/cref.xue", "byteLength": 1, "crc32": "00000000"}
        prate = {"variable": "prate", "path": "mrms.2026091300/prate.xue", "byteLength": 1, "crc32": "00000000"}
        payload = build_bin_manifest(run_time, bundles=[cref], expected_hours=3, model="NOAA-MRMS", product="conus-cref")
        validate_bin_manifest(payload, expected_hours=3)
        with self.assertRaisesRegex(ManifestError, "missing the required cref bundle"):
            build_bin_manifest(run_time, bundles=[prate], expected_hours=3, model="NOAA-MRMS", product="conus-cref")
        build_bin_manifest(
            run_time, bundles=[prate], expected_hours=3, model="NOAA-MRMS", product="conus-cref", require_core_variables=False
        )
        # A forecast still needs its pair.
        with self.assertRaisesRegex(ManifestError, "missing the required tmp2m bundle"):
            build_bin_manifest(run_time, bundles=[prate], expected_hours=3, model="HRRR", product="wrfsfc")


class ListingTests(unittest.TestCase):
    def test_keys_are_parsed_and_snapped_to_their_slot(self) -> None:
        objects, token = parse_mrms_listing(
            listing(cref_key("20260913", "000042"), cref_key("20260913", "000241"), "CONUS/MergedReflectivityQCComposite_00.50/20260913/README")
        )
        self.assertIsNone(token)
        self.assertEqual([item.observed.strftime("%H:%M:%S") for item in objects], ["00:00:42", "00:02:41"])
        self.assertEqual([item.slot(120).strftime("%H:%M:%S") for item in objects], ["00:00:00", "00:02:00"])
        # The rate is stamped on the mark and lands on the same slot.
        rate, _ = parse_mrms_listing(listing(prate_key("20260913", "000200")))
        self.assertEqual(rate[0].slot(120), objects[1].slot(120))
        _, token = parse_mrms_listing(listing(cref_key("20260913", "000042"), truncated=True))
        self.assertEqual(token, "abc/def+ghi=")
        with self.assertRaisesRegex(DownloadError, "not XML"):
            parse_mrms_listing("<not xml")

    def test_a_day_is_listed_with_the_continuation_followed(self) -> None:
        pages = [
            listing(cref_key("20260913", "000042"), truncated=True),
            listing(cref_key("20260913", "000241")),
        ]
        urls: list[str] = []

        def fetch_text(url: str) -> str:
            urls.append(url)
            return pages.pop(0)

        objects = list_mrms_objects("MergedReflectivityQCComposite_00.50", datetime(2026, 9, 13, 5, tzinfo=UTC), fetch=fetch_text)
        self.assertEqual(len(objects), 2)
        self.assertEqual(len(urls), 2)
        self.assertTrue(urls[0].startswith(f"{MRMS_BASE_URL}/?list-type=2&prefix="))
        self.assertIn("CONUS%2FMergedReflectivityQCComposite_00.50%2F20260913%2F", urls[0])
        self.assertIn("continuation-token=abc%2Fdef%2Bghi%3D", urls[1])
        self.assertEqual(mrms_product_prefix("PrecipRate_00.00", datetime(2026, 9, 13, 23, 59, tzinfo=UTC)), "CONUS/PrecipRate_00.00/20260913/")
        self.assertEqual(mrms_object_url(cref_key("20260913", "000042")), f"{MRMS_BASE_URL}/{cref_key('20260913', '000042')}")

    def test_a_window_is_the_slots_every_product_has(self) -> None:
        def fetch_text(url: str) -> str:
            if "MergedReflectivityQCComposite" in url and "20260913" in url:
                # A reissue in the 00:02 slot, a missing 00:04, one past the window.
                return listing(
                    cref_key("20260913", "000042"),
                    cref_key("20260913", "000241"),
                    cref_key("20260913", "000259"),
                    cref_key("20260913", "000641"),
                    cref_key("20260913", "030040"),
                    cref_key("20260913", "030241"),
                )
            if "PrecipRate" in url and "20260913" in url:
                # The rate has the 00:04 the composite lacks and lacks 00:06.
                return listing(
                    prate_key("20260913", "000000"),
                    prate_key("20260913", "000200"),
                    prate_key("20260913", "000400"),
                    prate_key("20260913", "030000"),
                )
            return listing()

        frames = mrms_window_frames(MRMS, RUN, 3, fetch=fetch_text)
        self.assertEqual([slot.strftime("%H:%M") for slot in frames], ["00:00", "00:02", "03:00"])
        self.assertEqual(list(frames[datetime(2026, 9, 13, 0, 2, tzinfo=UTC)]), ["cref", "prate"])
        self.assertEqual(frames[datetime(2026, 9, 13, 0, 2, tzinfo=UTC)]["cref"].observed.second, 59)
        # Narrowed to one product, its own slots stand.
        only = mrms_window_frames(MRMS, RUN, 3, ("cref",), fetch=fetch_text)
        self.assertEqual([slot.strftime("%H:%M") for slot in only], ["00:00", "00:02", "00:06", "03:00"])
        # A window crossing midnight lists both days.
        listed: list[str] = []

        def two_days(url: str) -> str:
            listed.append(url)
            return listing()

        self.assertEqual(mrms_window_frames(MRMS, GfsRun(datetime(2026, 9, 12, 22, tzinfo=UTC)), 3, fetch=two_days), {})
        self.assertEqual(sum("20260912" in url for url in listed), 2)
        self.assertEqual(sum("20260913" in url for url in listed), 2)

    def test_the_frame_name_is_the_slot_offset(self) -> None:
        self.assertEqual(mrms_frame_name(MRMS, RUN, datetime(2026, 9, 13, 0, 2, tzinfo=UTC)), "mrms.2026091300.t0002.grib2")
        self.assertEqual(mrms_frame_name(MRMS, RUN, datetime(2026, 9, 13, 2, 58, tzinfo=UTC)), "mrms.2026091300.t0258.grib2")
        self.assertEqual(mrms_frame_name(MRMS, RUN, datetime(2026, 9, 14, 3, 0, tzinfo=UTC)), "mrms.2026091300.t2700.grib2")
        with self.assertRaises(DownloadError):
            mrms_frame_name(MRMS, RUN, datetime(2026, 9, 12, 23, 58, tzinfo=UTC))

    def test_a_window_is_complete_once_the_bucket_has_moved_past_it(self) -> None:
        latest = {"cref": "030040", "prate": "030000"}

        def fetch_text(url: str) -> str:
            if "20260913" not in url:
                return listing()
            if "MergedReflectivityQCComposite" in url:
                return listing(cref_key("20260913", "000042"), cref_key("20260913", latest["cref"]))
            return listing(prate_key("20260913", "000000"), prate_key("20260913", latest["prate"]))

        with mock.patch("xuebuild.fetch.fetch_text", fetch_text):
            self.assertTrue(_mrms_run_is_complete(MRMS, RUN, 3))
            self.assertEqual(resolve_run("2026091300", hours=3, model="mrms").id, "2026091300")
            latest["prate"] = "025800"
            self.assertFalse(_mrms_run_is_complete(MRMS, RUN, 3))
            with self.assertRaisesRegex(DownloadError, "has not fully landed"):
                resolve_run("2026091300", hours=3, model="mrms")
        with self.assertRaisesRegex(DownloadError, "no live feed yet"):
            resolve_run("latest", hours=3, model="mrms")
        with self.assertRaisesRegex(DownloadError, "at least an hour"):
            resolve_run("2026091300", hours=0, model="mrms")
        with self.assertRaisesRegex(DownloadError, "read from a local file"):
            resolve_run("2026091300", hours=3, model="radar")


class DownloadTests(unittest.TestCase):
    """The fixture's messages served back as the bucket's gzipped objects."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-mrms-fetch-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        cref0, prate0 = split_messages(FRAMES[0])
        cref2, prate2 = split_messages(FRAMES[1])
        self.objects = {
            cref_key("20260913", "000042"): cref0,
            prate_key("20260913", "000000"): prate0,
            cref_key("20260913", "000242"): cref2,
            prate_key("20260913", "000200"): prate2,
        }

    def request(self, url: str, **_: object) -> object:
        key = url.removeprefix(f"{MRMS_BASE_URL}/")
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

    def test_a_frame_is_the_products_gunzipped_in_input_order(self) -> None:
        slot = datetime(2026, 9, 13, 0, 2, tzinfo=UTC)
        objects = {
            "cref": MrmsObject(cref_key("20260913", "000242"), datetime(2026, 9, 13, 0, 2, 42, tzinfo=UTC)),
            "prate": MrmsObject(prate_key("20260913", "000200"), slot),
        }
        with mock.patch("xuebuild.fetch._request", self.request):
            path = _download_mrms_frame(MRMS, RUN, slot, objects, self.root, force=False)
        self.assertEqual(path.name, "mrms.2026091300.t0002.grib2")
        self.assertEqual(path.read_bytes(), FRAMES[1].read_bytes())
        # Reused when readable, replaced on request.
        with mock.patch("xuebuild.fetch._request", mock.Mock(side_effect=AssertionError("no download"))):
            self.assertEqual(_download_mrms_frame(MRMS, RUN, slot, objects, self.root, force=False), path)
        with mock.patch("xuebuild.fetch._request", self.request):
            _download_mrms_frame(MRMS, RUN, slot, objects, self.root, force=True)

    def test_a_window_is_fetched_and_recorded(self) -> None:
        def fetch_text(url: str) -> str:
            if "MergedReflectivityQCComposite" in url and "20260913" in url:
                return listing(cref_key("20260913", "000042"), cref_key("20260913", "000242"))
            if "PrecipRate" in url and "20260913" in url:
                return listing(prate_key("20260913", "000000"), prate_key("20260913", "000200"))
            return listing()

        with mock.patch("xuebuild.fetch._request", self.request), mock.patch("xuebuild.fetch.fetch_text", fetch_text):
            paths = fetch.fetch_run(RUN, 3, self.root, model="mrms")
        self.assertEqual([path.name for path in paths], ["mrms.2026091300.t0000.grib2", "mrms.2026091300.t0002.grib2"])
        for path, fixture in zip(paths, FRAMES):
            self.assertEqual(path.read_bytes(), fixture.read_bytes())
        record = json.loads((self.root / "mrms.2026091300" / "fetch.json").read_text())
        self.assertEqual((record["run"], record["hours"], record["cadenceSeconds"]), ("2026091300", 3, 120))
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-09-13T00:00:00Z", "2026-09-13T00:02:00Z"])
        self.assertEqual(record["frames"][1]["objects"]["cref"]["observed"], "2026-09-13T00:02:42Z")
        self.assertEqual(record["frames"][1]["objects"]["prate"]["key"], prate_key("20260913", "000200"))
        with mock.patch("xuebuild.fetch.fetch_text", lambda url: listing()), self.assertRaisesRegex(DownloadError, "no frames"):
            _fetch_mrms_run(MRMS, RUN, 3, self.root, force=False, input_ids=None)


class RecordMatchingTests(unittest.TestCase):
    def test_the_alternates_are_matched_and_never_written(self) -> None:
        cref = variable_spec("cref")
        self.assertEqual(len(cref.grib2_alternates), 1)
        alternate = cref.grib2_alternates[0]
        self.assertEqual((alternate.triple, alternate.level_type, alternate.level_value, alternate.gdal_unit), ((209, 10, 0), 102, 500.0, "dBZ"))
        self.assertEqual(cref.fill_values, (-999.0, -99.0))
        self.assertEqual(cref.parameter_metadata()["discipline"], 0)
        self.assertEqual(cref.parameter_metadata()["typeOfFirstFixedSurface"], 10)
        prate = variable_spec("prate")
        alternate = prate.grib2_alternates[0]
        self.assertEqual((alternate.triple, alternate.level_type, alternate.level_value, alternate.gdal_unit), ((209, 6, 1), 102, 0.0, "mm/hr"))
        self.assertEqual(prate.fill_values, (-3.0,))
        self.assertEqual(prate.parameter_metadata()["parameterCategory"], 1)

    def test_the_rate_unit_decides_the_scaling(self) -> None:
        self.assertTrue(precipitation_rate_is_mm_per_hour("[mm/hr]"))
        self.assertTrue(precipitation_rate_is_mm_per_hour("mm/h"))
        self.assertFalse(precipitation_rate_is_mm_per_hour("kg/(m^2 s)"))
        self.assertEqual(raster_expression("prate", "mm/hr"), "maximum(0,minimum(50,A))")
        self.assertEqual(raster_expression("prate", "kg/(m^2 s)"), "maximum(0,minimum(50,A*3600))")
        with self.assertRaises(ConversionError):
            raster_expression("prate", "mm")
        frame = SourceFrame(FRAMES[0], 2, "prate", RUN.time, RUN.time, 0, "mm/hr")
        np.testing.assert_array_equal(binconvert._convert_units(frame, np.array([0.0, 2.5])), [0.0, 2.5])
        frame = SourceFrame(FRAMES[0], 2, "prate", RUN.time, RUN.time, 0, "kg/(m^2 s)")
        np.testing.assert_array_equal(binconvert._convert_units(frame, np.array([1.0 / 3600])), [1.0])

    def test_gdal_names_the_records_by_product(self) -> None:
        composite = {"GRIB_ELEMENT": "MergedReflectivityQCComposite", "GRIB_SHORT_NAME": "500-GPML", "GRIB_DISCIPLINE": "209", "GRIB_COMMENT": "Composite Reflectivity Mosaic (optimal method) [dBZ]"}
        rate = {"GRIB_ELEMENT": "PrecipRate", "GRIB_SHORT_NAME": "0-GPML", "GRIB_DISCIPLINE": "209", "GRIB_COMMENT": "Radar Precipitation Rate [mm/hr]"}
        description = '500[m] GPML="Specific altitude above mean sea level"'
        self.assertTrue(_band_matches("cref", composite, description))
        self.assertFalse(_band_matches("prate", composite, description))
        self.assertTrue(_band_matches("prate", rate, description))
        self.assertFalse(_band_matches("cref", rate, description))
        self.assertFalse(_band_matches("prate_ave", rate, description))
        self.assertFalse(_band_matches("tcdc", composite, description))
        # The element alone is not enough: the discipline says MRMS.
        self.assertFalse(_band_matches("cref", {**composite, "GRIB_DISCIPLINE": "0"}, description))

    def test_every_product_is_found_in_the_fixture_both_ways(self) -> None:
        for path, minute in zip(FRAMES, (0, 2)):
            fast = grib2.inspect_grib_fast(path, MRMS.input_variable_ids)
            self.assertEqual(tuple(fast), ("cref", "prate"))
            self.assertEqual([frame.band for frame in fast.values()], [1, 2])
            self.assertEqual((fast["cref"].unit, fast["prate"].unit), ("dBZ", "mm/hr"))
            # Each product is its own observation: a zero forecast time from
            # its own reference time, the composite's jittered past the mark.
            self.assertEqual(fast["cref"].valid_time, datetime(2026, 9, 13, 0, minute, 42, tzinfo=UTC))
            self.assertEqual(fast["prate"].valid_time, datetime(2026, 9, 13, 0, minute, tzinfo=UTC))
            self.assertEqual({frame.lead_seconds for frame in fast.values()}, {0})
            if shutil.which("gdalinfo") is None:
                continue
            with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
                slow = inspect_grib_multi(path, MRMS.input_variable_ids)
            for variable_id, frame in fast.items():
                self.assertEqual(frame, slow[variable_id], variable_id)


class AxisTests(unittest.TestCase):
    def frames(self, *observed: tuple[str, str]) -> list[dict[str, SourceFrame]]:
        per_file = []
        for path, (cref_time, prate_time) in zip(FRAMES * 3, observed):
            per_file.append(
                {
                    variable_id: SourceFrame(path, band, variable_id, when, when, 0, unit)
                    for variable_id, band, unit, when in (
                        ("cref", 1, "dBZ", datetime.fromisoformat(cref_time).replace(tzinfo=UTC)),
                        ("prate", 2, "mm/hr", datetime.fromisoformat(prate_time).replace(tzinfo=UTC)),
                    )
                }
            )
        return per_file

    def test_observation_times_snap_down_to_the_slot_and_the_hour_is_the_run(self) -> None:
        snapped = _snap_observation_frames(
            self.frames(("2026-09-13T00:02:41", "2026-09-13T00:02:00"), ("2026-09-13T00:00:42", "2026-09-13T00:00:00"), ("2026-09-13T02:58:37", "2026-09-13T02:58:00")),
            120,
        )
        self.assertEqual([frames["cref"].lead_seconds for frames in snapped], [120, 0, 2 * 3600 + 58 * 60])
        self.assertEqual({frame.run_time for frames in snapped for frame in frames.values()}, {RUN.time})
        self.assertEqual(snapped[0]["prate"].valid_time, datetime(2026, 9, 13, 0, 2, tzinfo=UTC))
        self.assertEqual(snapped[0]["cref"].valid_time, snapped[0]["prate"].valid_time)
        # A window whose first hour has no frame starts at the hour it does.
        later = _snap_observation_frames(self.frames(("2026-09-13T01:04:41", "2026-09-13T01:04:00")), 120)
        self.assertEqual(later[0]["cref"].run_time, datetime(2026, 9, 13, 1, tzinfo=UTC))
        self.assertEqual(later[0]["cref"].lead_seconds, 240)
        with self.assertRaisesRegex(ConversionError, "disagree on the observation slot"):
            _snap_observation_frames(self.frames(("2026-09-13T00:02:41", "2026-09-13T00:04:00")), 120)

    def test_two_files_in_one_slot_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            duplicate = Path(scratch) / "mrms.2026091300.t0000.again.grib2"
            shutil.copy(FRAMES[0], duplicate)
            with self.assertRaisesRegex(ConversionError, "duplicate lead times"):
                binconvert._prepare_frames_all([FRAMES[0], duplicate], MRMS.input_variable_ids, cadence_seconds=120)
        per_file = binconvert._prepare_frames_all(list(FRAMES), MRMS.input_variable_ids, cadence_seconds=120)
        self.assertEqual([frames["cref"].lead_seconds for frames in per_file], [0, 120])
        self.assertEqual(binconvert.axis_unit_seconds([0, 120, 240]), 120)


class GridTests(unittest.TestCase):
    def test_regional_steps_snap_to_their_thousandth(self) -> None:
        step = 0.0099999997142449
        raw = GridInfo(7000, 3500, -129.99999999985712 + step / 2, 54.9999999998571 - 0.0099999997142041 / 2, step, -0.0099999997142041)
        full = _snap_regional_steps(raw)
        self.assertEqual((full.longitude_step, full.latitude_step), (0.01, -0.01))
        self.assertEqual((full.first_longitude, full.first_latitude), (-129.995, 54.995))
        # The GRIB writer re-rounds a crop's last coordinate: a hair further off, still on 0.01.
        crop_step = 0.009999993710692
        crop = _snap_regional_steps(GridInfo(160, 160, -77.60000099685537 + crop_step / 2, 51.795, crop_step, -0.01))
        self.assertEqual((crop.longitude_step, crop.first_longitude), (0.01, -77.595))
        # 30 thousandths is the double nearest 0.03, as a literal is.
        self.assertEqual(_snap_axis(0.03 + 1e-10, -100.0, 100), (0.03, -100.0))
        self.assertEqual(_snap_axis(0.03 + 1e-10, -100.0, 100)[0], 0.03)
        # Not near a thousandth — the radar mosaic's power-of-two tiles, the
        # Gaussian grid, a step a whole thousandth off — passes through.
        for grid in (
            GridInfo(512, 512, 100.0, 40.0, 360 / 65536, -360 / 65536),
            GridInfo(3072, 1536, -180.0, 89.91, 0.1171875, -0.117),
            GridInfo(100, 100, -100.0, 40.0, 0.011, -0.011),
        ):
            self.assertEqual(_snap_regional_steps(grid), grid)
        # A global grid is the global rule's business, which runs first and
        # leaves it wrapping; a wrapping grid passes through here untouched.
        wave = GridInfo(1440, 721, -180.0, 90.0, 0.25, -0.25)
        self.assertEqual(_snap_regional_steps(wave), wave)
        # An origin off the half-step grid keeps its step only.
        self.assertEqual(_snap_axis(0.0099999997, -129.9, 7000), (0.01, -129.9))

    def test_the_block_maximum(self) -> None:
        reduction = BlockReduction(factor=2, source_width=4, source_height=2)
        self.assertEqual(reduction.source_shape, (2, 4))
        plane = np.array([[0.0, 5.5, -1.0, np.nan], [3.0, 1.0, 2.0, 0.0]])
        reduced = reduction.take(plane)
        self.assertEqual(reduced.shape, (1, 2))
        self.assertEqual(reduced[0, 0], 5.5)
        self.assertTrue(np.isnan(reduced[0, 1]))
        with self.assertRaises(ConversionError):
            reduction.take(np.zeros((2, 5)))
        grid = binconvert._downsample_grid(GridInfo(4, 2, -129.995, 54.995, 0.01, -0.01), Downsample(2), Path("x"))
        self.assertEqual((grid.width, grid.height), (2, 1))
        self.assertEqual((grid.first_longitude, grid.first_latitude), (-129.99, 54.99))
        self.assertEqual((grid.longitude_step, grid.latitude_step), (0.02, -0.02))
        self.assertEqual(grid.source_shape, (2, 4))
        self.assertEqual(grid.downsample, reduction)
        self.assertIsNone(grid.decimated().downsample)
        with self.assertRaisesRegex(ConversionError, "does not divide"):
            binconvert._downsample_grid(GridInfo(5, 2, -129.995, 54.995, 0.01, -0.01), Downsample(2), Path("x"))
        with self.assertRaisesRegex(ConversionError, "global grid"):
            binconvert._downsample_grid(GridInfo(1440, 720, -180.0, 90.0, 0.25, -0.25), Downsample(2), Path("x"))


@requires_gdalinfo
class FixtureGridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.grid = _grid_info(FRAMES[0], MRMS)

    def test_the_fixture_is_thinned_onto_its_round_step(self) -> None:
        self.assertEqual((self.grid.width, self.grid.height), (80, 80))
        self.assertEqual((self.grid.longitude_step, self.grid.latitude_step), (0.02, -0.02))
        self.assertEqual((self.grid.first_longitude, self.grid.first_latitude), (-77.59, 51.79))
        self.assertEqual(self.grid.source_shape, (160, 160))
        self.assertFalse(self.grid.wraps)
        self.assertEqual(self.grid.column_roll, 0)
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            # Without the source's thinning, the same file is the 0.01° grid.
            raw = _grid_info(FRAMES[0])
        self.assertEqual((raw.width, raw.longitude_step, raw.first_longitude), (160, 0.01, -77.595))

    def test_the_sentinels_are_the_codebook_bottom_and_the_block_keeps_its_maximum(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}), tempfile.TemporaryDirectory() as work:
            frames = inspect_grib_multi(FRAMES[0], MRMS.input_variable_ids)
            planes = _extract_planes(frames, self.grid, Path(work))
            raw_grid = _grid_info(FRAMES[0])
            raw = _extract_planes(frames, raw_grid, Path(work))
        cref = planes["cref"].reshape(80, 80)
        prate = planes["prate"].reshape(80, 80)
        self.assertEqual(cref.shape, (80, 80))
        # -999 and -99 are gone; what remains is either no echo or a real
        # return, and the raw record's weak negative returns are kept as
        # they are for the codebook to clamp.
        self.assertTrue((cref >= -30).all())
        self.assertGreater((cref > 0).sum(), 1500)
        self.assertGreater((cref == 0).sum(), 3000)
        self.assertTrue((prate >= 0).all())
        self.assertGreater(prate.max(), 5.0)
        # The thinned plane is the block maximum of the raw one, whose
        # sentinels were folded first.
        raw_cref = raw["cref"].reshape(160, 160)
        self.assertTrue((raw_cref >= -30).all())
        np.testing.assert_array_equal(cref, raw_cref.reshape(80, 2, 80, 2).max(axis=(1, 3)))
        raw_prate = raw["prate"].reshape(160, 160)
        np.testing.assert_array_equal(prate, raw_prate.reshape(80, 2, 80, 2).max(axis=(1, 3)))

    def test_a_crop_is_cut_from_the_thinned_grid(self) -> None:
        cropped = crop_grid(self.grid, (-77.0, 51.0, -76.5, 51.5))
        self.assertEqual(cropped.downsample, self.grid.downsample)
        self.assertEqual(cropped.source_shape, (160, 160))
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}), tempfile.TemporaryDirectory() as work:
            frames = inspect_grib_multi(FRAMES[0], ("cref",))
            whole = _extract_planes(frames, self.grid, Path(work))["cref"].reshape(80, 80)
            window = _extract_planes(frames, cropped, Path(work))["cref"].reshape(cropped.height, cropped.width)
        crop = cropped.crop
        np.testing.assert_array_equal(
            window, whole[crop.row_start : crop.row_start + crop.height, crop.column_start : crop.column_start + crop.width]
        )


@requires_gdalinfo
class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-mrms-"))
        inputs = cls.root / "mrms.2026091300"
        inputs.mkdir()
        for path in FRAMES:
            shutil.copy(path, inputs / path.name.replace(".crop", ""))
        cls.inputs = inputs
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                inputs,
                cls.root / "out",
                model="mrms",
                skip_video=True,
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_the_window_is_a_two_minute_axis_on_the_thinned_grid(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["cref", "prate"])
        self.assertEqual(self.report["videos"], [])
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("NOAA-MRMS", "conus-cref"))
        self.assertEqual(manifest["runTime"], "2026-09-13T00:00:00Z")
        self.assertEqual(manifest["forecastHours"], 1)
        bundle = read_bundle(self.root / "out" / "cref.xue")
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 120, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 1})
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (80, 80))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.02, -0.02))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (-77.59, 51.79))
        # The identity written is the mosaic's, never the MRMS-local one.
        parameter = bundle.metadata["variables"][0]["parameter"]
        self.assertEqual((parameter["discipline"], parameter["parameterCategory"], parameter["parameterNumber"]), (0, 16, 5))
        self.assertEqual(bundle.metadata["variables"][0]["unit"], "dBZ")
        rate = read_bundle(self.root / "out" / "prate.xue")
        self.assertEqual(rate.metadata["variables"][0]["parameter"]["parameterCategory"], 1)
        self.assertEqual(rate.metadata["variables"][0]["unit"], "mm/h")
        self.assertEqual(rate.metadata["time"]["unitSeconds"], 120)
        # Two frames, one temporal group, a 2 x 2 tile grid on the 80-cell side.
        self.assertEqual((bundle.frame_count, len(bundle.groups), bundle.tiles.count), (2, 1, 4))
        codes = np.asarray(bundle.decode_plane(1, 0)).reshape(80, 80)
        self.assertGreater(codes.max(), 60)
        self.assertGreater((codes == 0).sum(), 3000)
        for name in ("cref.half.xue", "cref.poster.bin", "prate.half.xue", "prate.poster.bin"):
            self.assertTrue((self.root / "out" / name).is_file(), name)

    def test_a_complete_build_wants_the_production_grid(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "3500x1750 grid"):
                binconvert.convert_bin(
                    self.inputs, self.root / "complete", model="mrms", skip_video=True, require_complete=True, expected_hours=3
                )

    @unittest.skipUnless(native.knows_source("mrms"), f"the installed {native.DISTRIBUTION} wheel predates the MRMS source")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI")
        subject = self.root / "native"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            report = native.convert_bin(self.inputs, subject, model="mrms", skip_video=True, manifest_path=subject / "manifest.json")
        if report["zstdVersion"] != self.report["zstdVersion"]:
            self.skipTest("libzstd differs between the reference and the wheel")
        names = sorted(path.name for path in (self.root / "out").iterdir())
        self.assertEqual(sorted(path.name for path in subject.iterdir()), names)
        for name in names:
            with self.subTest(artifact=name):
                self.assertTrue(filecmp.cmp(self.root / "out" / name, subject / name, shallow=False), name)


if __name__ == "__main__":
    unittest.main()

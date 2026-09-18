"""The GOES-East and GOES-West sources: NOAA's CMIPF product through the
satellite stage.

The shape is tests/test_satellite.py's: the fixture is eight 220 x 220
windows cut from real GOES-19 files (tests/prepare_goes_fixture.py — the
four Dust RGB channels of two consecutive scans, the Venezuelan coast at
66–62°W, 7–11°N), listed by a stand-in for the bucket under the product's
own keys; the reader, the completeness rule, the download, the warp, the
producer with the ABI stretches, the series, the ingest and the conversion
on the production grid run over them, and the native encoder is held
byte-identical when the installed wheel knows the source.
"""

from __future__ import annotations

import dataclasses
import filecmp
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from tests.test_satellite import bucket, requires_gdal
from xuebuild import binconvert, native, observation, zstdcli
from xuebuild.binformat import read_bundle
from xuebuild.errors import DownloadError
from xuebuild.fetch import (
    _fetch_satellite_run,
    _satellite_run_is_complete,
    latest_observation_slot,
    latest_satellite_slot,
    resolve_run,
    satellite_grid,
    window_summary,
)
from xuebuild.manifest import validate_bin_manifest
from xuebuild.model import GfsRun
from xuebuild.quantize import PROFILES
from xuebuild.satellite import GOES_EAST, GOES_WEST, HIMAWARI, PLATFORMS, assemble
from xuebuild.satellite import fetch as satellite_fetch
from xuebuild.satellite.producers import PRODUCERS
from xuebuild.satellite.projector import TargetGrid
from xuebuild.satellite.readers import CMIPFReader, parse_cmipf_key, reader_for
from xuebuild.sources import SatelliteBand, source_spec
from xuebuild.stac import _source_prose
from xuebuild.variables import DUST_RGB_BUNDLE_ID, DUST_RGB_COMPONENT_IDS

FIXTURES = Path(__file__).parent / "fixtures" / "goes"
EAST = source_spec("goeseast")
WEST = source_spec("goeswest")
IR104 = GOES_EAST.channel("ir104")
CHANNELS = tuple(GOES_EAST.channel(channel_id) for channel_id in EAST.input_variable_ids)
DUST = PRODUCERS[DUST_RGB_BUNDLE_ID]
#: The fixture's two scans: 15:10:20 and 15:20:20 UTC on 2026-09-17 (day
#: 260), slots 15:10 and 15:20.
SLOT_1510 = datetime(2026, 9, 17, 15, 10, tzinfo=UTC)
SLOT_1520 = datetime(2026, 9, 17, 15, 20, tzinfo=UTC)
HOUR = datetime(2026, 9, 17, 15, tzinfo=UTC)
#: The fixture windows' footprint at the published step, for the tests
#: that warp: 150 x 150 cells around the Venezuelan coast.
TILE_GRID = TargetGrid(west=-67.0, south=6.0, east=-61.0, north=12.0, step=0.04)


def fixture_keys() -> dict[str, Path]:
    """The fixture files under the keys the bucket lists them by."""
    keys: dict[str, Path] = {}
    for path in sorted(FIXTURES.glob("*.nc")):
        parsed = parse_cmipf_key(path.name)
        assert parsed is not None, path.name
        keys[f"{GOES_EAST.prefix}/{parsed.start:%Y/%j/%H}/{path.name}"] = path
    return keys


def counting(listing: Callable[[str], str]) -> tuple[Callable[[str], str], list[str]]:
    """A listing that records the URLs it was asked for."""
    asked: list[str] = []

    def fetch(url: str) -> str:
        asked.append(url)
        return listing(url)

    return fetch, asked


class RegistryTests(unittest.TestCase):
    def test_the_sources_are_the_himawari_one_on_their_own_disks(self) -> None:
        himawari = source_spec("himawari")
        for spec, platform, model, pointer in ((EAST, "goeseast", "GOES-EAST", "latest-goeseast.json"), (WEST, "goeswest", "GOES-WEST", "latest-goeswest.json")):
            with self.subTest(source=spec.id):
                self.assertTrue(spec.observation and spec.series_file and spec.fetched and spec.live)
                self.assertEqual((spec.platform, spec.manifest_model, spec.latest_filename, spec.product), (platform, model, pointer, "abi-fldk-0p04"))
                self.assertEqual(spec.input_variable_ids, himawari.input_variable_ids)
                self.assertEqual((spec.bundle_scalar_ids, spec.bundle_composite_ids, spec.core_bundle_ids), (himawari.bundle_scalar_ids, himawari.bundle_composite_ids, himawari.core_bundle_ids))
                self.assertEqual(binconvert.published_bundle_ids(spec), ("ir104", "dustrgb"))
                self.assertEqual((spec.grid_step, spec.cadence_seconds, spec.window_hours, spec.production_grid, spec.tile), (0.04, 600, 6, (3000, 3000), (64, 64)))
                self.assertFalse(spec.video)

    def test_the_bands_are_the_abi_s(self) -> None:
        for spec, number in ((EAST, 273), (WEST, 272)):
            with self.subTest(source=spec.id):
                self.assertEqual([band_id for band_id, _ in spec.bands], ["ir086", "ir104", "ir112", "ir123"])
                bands = dict(spec.bands)
                self.assertEqual(bands["ir104"], SatelliteBand(satellite_series=0, satellite_number=number, instrument_type=617, central_wavenumber=96618))
                self.assertEqual([bands[band_id].central_wavenumber for band_id in ("ir086", "ir104", "ir112", "ir123")], [117647, 96618, 89286, 81301])
                # An ABI window and its AHI counterpart share the id and differ
                # in the band block alone.
                self.assertNotEqual(bands["ir104"], dict(source_spec("himawari").bands)["ir104"])

    def test_the_east_disk_is_on_negative_longitudes_and_the_west_crosses_the_antimeridian(self) -> None:
        self.assertEqual(GOES_EAST.region, (-135.2, -60.0, -15.2, 60.0))
        self.assertEqual(GOES_WEST.region, (163.0, -60.0, 283.0, 60.0))
        self.assertEqual(HIMAWARI.region, (80.7, -60.0, 200.7, 60.0))
        east = satellite_grid(EAST)
        self.assertEqual((east.width, east.height, east.first_longitude, east.first_latitude), (3000, 3000, -135.18, 59.98))
        west = satellite_grid(WEST)
        self.assertEqual((west.width, west.height, west.first_longitude, west.first_latitude), (3000, 3000, 163.02, 59.98))
        # The west edge is always inside -180 … 180 and the east edge past
        # 180 when the disk crosses it: the shape Himawari's grid takes.
        for platform in (HIMAWARI, GOES_EAST, GOES_WEST):
            west_edge, _, east_edge, _ = platform.region
            self.assertGreaterEqual(west_edge, -180.0)
            self.assertLess(west_edge, 180.0)
            self.assertEqual(round(east_edge - west_edge, 6), 120.0)

    def test_the_platforms_read_the_cmipf_product(self) -> None:
        for platform, bucket_name in ((GOES_EAST, "noaa-goes19"), (GOES_WEST, "noaa-goes18")):
            self.assertEqual((platform.reader, platform.bucket, platform.prefix, platform.tile_count), ("cmipf", bucket_name, "ABI-L2-CMIPF", 1))
            self.assertIsInstance(reader_for(platform), CMIPFReader)
            self.assertEqual(platform.channel("ir104").band, 13)
            self.assertEqual([platform.channel(channel_id).band for channel_id in EAST.input_variable_ids], [11, 13, 14, 15])

    def test_the_catalog_prose_names_noaa_and_the_recipe(self) -> None:
        for spec, spacecraft in ((EAST, "GOES-19"), (WEST, "GOES-18")):
            prose = _source_prose(spec)
            self.assertEqual(prose["title"], f"{spacecraft} ABI full disk")
            self.assertEqual(prose["providers"][0]["name"], "NOAA NESDIS")
            self.assertEqual(prose["providers"][1]["roles"], ["host"])
            self.assertEqual(prose["providers"][2]["name"], "shachen")
            self.assertIn("endorse", prose["description"])
            self.assertEqual([link["rel"] for link in prose["links"]], ["license", "cite-as", "describedby"])


class KeyTests(unittest.TestCase):
    def test_a_cmipf_key_parses_to_its_fields(self) -> None:
        parsed = parse_cmipf_key("ABI-L2-CMIPF/2026/260/15/OR_ABI-L2-CMIPF-M6C13_G19_s20262601510206_e20262601519526_c20262601519573.nc")
        assert parsed is not None
        self.assertEqual((parsed.mode, parsed.channel, parsed.spacecraft), (6, 13, 19))
        self.assertEqual(parsed.start, datetime(2026, 9, 17, 15, 10, 20, tzinfo=UTC))
        self.assertEqual(parsed.end, datetime(2026, 9, 17, 15, 19, 52, tzinfo=UTC))
        self.assertEqual(parsed.created, datetime(2026, 9, 17, 15, 19, 57, tzinfo=UTC))
        self.assertIsNone(parse_cmipf_key("ABI-L2-MCMIPF/2026/260/15/OR_ABI-L2-MCMIPF-M6_G19_s20262601510206_e20262601519526_c20262601520000.nc"))
        self.assertIsNone(parse_cmipf_key("OR_HFD-020-B12-M1C13-T020_GH9_s20262600300000_c20262600308160.nc"))

    def test_a_scan_s_slot_is_its_start_floored_to_the_cadence(self) -> None:
        reader = CMIPFReader()
        self.assertEqual(reader.slot_of(GOES_EAST, datetime(2026, 9, 17, 15, 10, 20, tzinfo=UTC)), SLOT_1510)
        self.assertEqual(reader.slot_of(GOES_EAST, datetime(2026, 9, 17, 15, 19, 59, tzinfo=UTC)), SLOT_1510)
        self.assertEqual(reader.slot_of(GOES_EAST, SLOT_1520), SLOT_1520)
        self.assertEqual(reader.hour_prefix(GOES_EAST, SLOT_1510), "ABI-L2-CMIPF/2026/260/15/")
        self.assertEqual(reader.hour_prefix(GOES_WEST, datetime(2026, 1, 1, 0, 5, tzinfo=UTC)), "ABI-L2-CMIPF/2026/001/00/")


class ListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = fixture_keys()
        self.listing, self.download = bucket(self.keys)
        self.reader = CMIPFReader()

    def test_a_day_s_hours_are_its_directories_and_a_slot_is_one_file(self) -> None:
        day = datetime(2026, 9, 17, tzinfo=UTC)
        self.assertEqual(self.reader.list_hours(GOES_EAST, day, fetch=self.listing), [HOUR])
        self.assertEqual(self.reader.list_hours(GOES_EAST, day - timedelta(days=1), fetch=self.listing), [])
        self.assertEqual(self.reader.list_slots(GOES_EAST, day, fetch=self.listing), [SLOT_1510, SLOT_1520])
        objects = self.reader.list_slot(GOES_EAST, IR104, SLOT_1510, fetch=self.listing)
        self.assertEqual(len(objects), 1)
        self.assertEqual((objects[0].tile, objects[0].key.rsplit("/", 1)[-1][:28]), (1, "OR_ABI-L2-CMIPF-M6C13_G19_s2"))
        self.assertTrue(satellite_fetch.slot_is_complete(GOES_EAST, objects))
        # Another channel's file, or a slot the hour lacks, is not this slot.
        self.assertEqual(self.reader.list_slot(GOES_EAST, GOES_EAST.channel("wv062"), SLOT_1510, fetch=self.listing), [])
        self.assertEqual(self.reader.list_slot(GOES_EAST, IR104, datetime(2026, 9, 17, 15, 30, tzinfo=UTC), fetch=self.listing), [])
        self.assertFalse(satellite_fetch.slot_is_complete(GOES_EAST, []))

    def test_a_reissued_file_wins_and_any_scan_mode_is_listed(self) -> None:
        reissued = dict(self.keys)
        key = next(k for k in self.keys if "C13_" in k and "_s20262601510206" in k)
        later = key.replace("_c20262601519573", "_c20262601530000").replace("-M6C13_", "-M3C13_")
        reissued[later] = self.keys[key]
        listing, _ = bucket(reissued)
        objects = self.reader.list_slot(GOES_EAST, IR104, SLOT_1510, fetch=listing)
        self.assertEqual([item.key for item in objects], [later])

    def test_the_newest_slots_cost_a_day_listing_and_an_hour(self) -> None:
        now = datetime(2026, 9, 17, 15, 35, tzinfo=UTC)
        fetch, asked = counting(self.listing)
        self.assertEqual(self.reader.recent_slots(GOES_EAST, now, limit=3, fetch=fetch), [SLOT_1520, SLOT_1510])
        # One listing of the day's hour directories, one of the hour, and
        # yesterday's directory only because the hour held fewer than asked.
        self.assertEqual(len(asked), 3)
        fetch, asked = counting(self.listing)
        self.assertEqual(self.reader.recent_slots(GOES_EAST, now, limit=1, fetch=fetch), [SLOT_1520])
        self.assertEqual(len(asked), 2)
        # A slot after now is not recent yet.
        self.assertEqual(self.reader.recent_slots(GOES_EAST, datetime(2026, 9, 17, 15, 15, tzinfo=UTC), limit=3, fetch=self.listing), [SLOT_1510])

    def test_the_newest_complete_slot_ends_the_live_window(self) -> None:
        now = datetime(2026, 9, 17, 15, 35, tzinfo=UTC)
        fetch, asked = counting(self.listing)
        self.assertEqual(satellite_fetch.latest_slot(GOES_EAST, IR104, now=now, fetch=fetch), SLOT_1520)
        # The day, the hour, yesterday's day, and the slot's own hour again
        # for its file: four requests a round.
        self.assertLessEqual(len(asked), 4)
        # A slot another channel has but this one lacks is passed over.
        partial = {k: v for k, v in self.keys.items() if not ("C13_" in k and "_s20262601520206" in k)}
        listing, _ = bucket(partial)
        self.assertEqual(satellite_fetch.latest_slot(GOES_EAST, IR104, now=now, fetch=listing), SLOT_1510)
        self.assertEqual(satellite_fetch.latest_slot(GOES_EAST, IR104, now=datetime(2026, 9, 18, 0, 5, tzinfo=UTC), fetch=self.listing), SLOT_1520)
        with self.assertRaisesRegex(DownloadError, "no complete ir104 slot"):
            satellite_fetch.latest_slot(GOES_EAST, IR104, now=datetime(2026, 9, 20, tzinfo=UTC), fetch=self.listing)

    def test_the_source_dispatches_to_the_platform(self) -> None:
        now = datetime(2026, 9, 17, 15, 35, tzinfo=UTC)
        self.assertEqual(latest_satellite_slot(EAST, now=now, fetch=self.listing), SLOT_1520)
        with mock.patch("xuebuild.satellite.readers._fetch_text", self.listing):
            self.assertEqual(latest_observation_slot(EAST, now=now), SLOT_1520)
            run = resolve_run("latest", hours=3, now=now, model="goeseast")
            self.assertEqual(run.id, "2026091713")
            self.assertEqual(resolve_run("latest", hours=EAST.window_hours, now=now, model="goeseast").id, "2026091710")
            self.assertFalse(_satellite_run_is_complete(EAST, GfsRun(HOUR), 3, now=now))
            self.assertTrue(_satellite_run_is_complete(EAST, GfsRun(datetime(2026, 9, 17, 12, tzinfo=UTC)), 3, now=now))
            with self.assertRaisesRegex(DownloadError, "has not fully landed"):
                resolve_run("2026091715", hours=3, now=now, model="goeseast")
        # GOES-West is the same reader on its own bucket, which the
        # stand-in does not hold.
        listing, _ = bucket({})
        with self.assertRaisesRegex(DownloadError, "GOES-18 lists no complete"):
            satellite_fetch.latest_slot(GOES_WEST, GOES_WEST.channel("ir104"), now=now, fetch=listing)


@requires_gdal
class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-goes-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.keys = fixture_keys()
        self.listing, self.download = bucket(self.keys)
        self.downloads: list[str] = []

    def download_counting(self, url: str) -> bytes:
        self.downloads.append(url)
        return self.download(url)

    def fetch_window(self, *, force: bool = False, channels: tuple = (IR104,), producers: tuple = (), platform=GOES_EAST, grid: TargetGrid = TILE_GRID):
        return satellite_fetch.fetch_window(
            platform,
            channels,
            HOUR,
            1,
            grid=grid,
            raw_root=self.root,
            destination=self.root / "goeseast.2026091715",
            series_stem="goeseast.2026091715",
            units={channel.id: "K" for channel in channels},
            producers=producers,
            force=force,
            fetch=self.listing,
            download=self.download_counting,
        )

    def test_a_slot_is_one_file_warped_once_and_read_from_the_cache_after(self) -> None:
        window = self.fetch_window()
        self.assertEqual([(item.slot, item.tiles) for item in window.slots], [(SLOT_1510, 1), (SLOT_1520, 1)])
        self.assertEqual(len(self.downloads), 2)
        self.assertEqual(window.series["ir104"], self.root / "goeseast.2026091715" / "goeseast.2026091715.ir104.nc")
        frames = sorted(path.name for path in (self.root / "goeseast-frames" / "ir104").iterdir())
        self.assertEqual(frames, ["ir104_20260917151000.json", "ir104_20260917151000.tif", "ir104_20260917152000.json", "ir104_20260917152000.tif"])
        packing = json.loads((self.root / "goeseast-frames" / "ir104" / "ir104_20260917151000.json").read_text())
        self.assertAlmostEqual(packing["scale"], 0.06145332, places=8)
        self.assertAlmostEqual(packing["offset"], 89.62, places=5)
        self.assertEqual((packing["unit"], packing["tiles"], len(packing["keys"])), ("K", 1, 1))
        self.assertFalse((self.root / "goeseast.2026091715" / "tiles").exists())
        again = self.fetch_window()
        self.assertEqual([item.tiles for item in again.slots], [0, 0])
        self.assertEqual(len(self.downloads), 2)
        forced = self.fetch_window(force=True)
        self.assertEqual(len(self.downloads), 4)
        self.assertTrue(filecmp.cmp(window.series["ir104"], forced.series["ir104"], shallow=False))

    def test_a_frame_is_the_file_on_the_target_grid(self) -> None:
        window = self.fetch_window()
        info = json.loads(subprocess.run(["gdalinfo", "-json", "-stats", str(window.slots[0].frames["ir104"])], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(info["size"], [150, 150])
        self.assertEqual(info["geoTransform"][:2], [-67.0, 0.04])
        band = info["bands"][0]
        self.assertEqual(band["noDataValue"], -32767.0)
        statistics = band["metadata"][""]
        minimum = float(statistics["STATISTICS_MINIMUM"]) * band["scale"] + band["offset"]
        maximum = float(statistics["STATISTICS_MAXIMUM"]) * band["scale"] + band["offset"]
        # Cold cloud tops and the Caribbean in September, in kelvin.
        self.assertLess(minimum, 240.0)
        self.assertGreater(maximum, 290.0)
        self.assertLess(maximum, 310.0)
        # The fixture window covers 66.07–61.77°W, 7.16–11.26°N (columns
        # 23–130, rows 18–121 of this grid); outside it the frame is the fill.
        plane = assemble.read_frame(window.slots[0].frames["ir104"], TILE_GRID)
        self.assertTrue(np.isfinite(plane[50:120, 40:110]).all())
        self.assertFalse(np.isfinite(plane[:, :20]).any())
        self.assertFalse(np.isfinite(plane[:15, :]).any())

    def test_the_dust_rgb_takes_the_abi_stretches(self) -> None:
        from shachen.constants import DUST_RGB, DUST_RGB_ABI  # noqa: PLC0415
        from shachen.dustrgb import dust_rgb  # noqa: PLC0415
        import xarray as xr  # noqa: PLC0415

        window = self.fetch_window(channels=CHANNELS, producers=(DUST,))
        self.assertEqual([(item.slot, item.tiles) for item in window.slots], [(SLOT_1510, 4), (SLOT_1520, 4)])
        self.assertEqual(list(window.series), ["ir086", "ir104", "ir112", "ir123", "dustr", "dustg", "dustb"])
        planes = {channel.id: assemble.read_frame(window.slots[0].frames[channel.id], TILE_GRID) for channel in CHANNELS}
        scene = xr.Dataset({name: xr.DataArray(planes[channel_id], dims=("y", "x")) for name, channel_id in (("bt_tir_86", "ir086"), ("bt_tir_104", "ir104"), ("bt_tir_112", "ir112"), ("bt_tir_123", "ir123"))})
        abi = np.asarray(dust_rgb(scene, DUST_RGB_ABI).values)
        seviri = np.asarray(dust_rgb(scene, DUST_RGB).values)
        covered = np.isfinite(planes["ir104"])
        self.assertGreater(int(covered.sum()), 4000)
        for index, gun_id in enumerate(DUST_RGB_COMPONENT_IDS):
            actual = assemble.read_frame(window.slots[0].frames[gun_id], TILE_GRID)
            self.assertEqual(np.isfinite(actual).tolist(), covered.tolist())
            np.testing.assert_allclose(actual[covered], abi[..., index][covered], atol=0.00005 + 1e-12)
        # The two published stretch sets disagree on this scene, so the
        # choice by instrument is not dead code.
        self.assertFalse(np.allclose(abi[covered], seviri[covered], atol=0.001))
        gun = window.slots[0].frames["dustr"]
        self.assertEqual(json.loads(assemble.packing_path(gun).read_text(encoding="utf-8"))["producer"], {"id": "shachen", "version": DUST.version})

    def test_the_source_fetch_writes_the_series_and_a_fetch_record(self) -> None:
        written = _fetch_satellite_run(EAST, GfsRun(HOUR), 3, self.root, force=False, input_ids=None, fetch=self.listing, download=self.download)
        run_dir = self.root / "goeseast.2026091715"
        self.assertEqual(written, [run_dir / f"goeseast.2026091715.{variable_id}.nc" for variable_id in ("ir086", "ir104", "ir112", "ir123", "dustr", "dustg", "dustb")])
        record = json.loads((run_dir / "fetch.json").read_text(encoding="utf-8"))
        self.assertEqual((record["model"], record["run"], record["hours"], record["cadenceSeconds"]), ("goeseast", "2026091715", 3, 600))
        self.assertEqual(record["platform"], "GOES-19")
        self.assertEqual(record["grid"], {"step": 0.04, "width": 3000, "height": 3000, "firstLongitude": -135.18, "firstLatitude": 59.98})
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-09-17T15:10:00Z", "2026-09-17T15:20:00Z"])
        self.assertEqual([frame["tilesFetched"] for frame in record["frames"]], [4, 4])
        summary = window_summary(run_dir)
        self.assertEqual((summary["frameCount"], summary["latestSlot"]), (2, "2026-09-17T15:20:00Z"))
        series = observation.inspect_observation(run_dir, EAST, ("ir104", *DUST_RGB_COMPONENT_IDS))
        self.assertEqual(series.lead_seconds, [600, 1200])
        self.assertEqual(series.producers["dustr"], ("shachen", DUST.version))


@requires_gdal
class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-goes-convert-"))
        listing, download = bucket(fixture_keys())
        _fetch_satellite_run(EAST, GfsRun(HOUR), 3, cls.root, force=False, input_ids=None, fetch=listing, download=download)
        cls.inputs = cls.root / "goeseast.2026091715"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                cls.inputs,
                cls.root / "out",
                model="goeseast",
                skip_video=True,
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
                require_complete=True,
                expected_hours=3,
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_the_bundles_are_the_east_disk_with_the_abi_band(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["ir104", "dustrgb"])
        manifest = json.loads((self.root / "out" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["model"], manifest["product"]), ("GOES-EAST", "abi-fldk-0p04"))
        self.assertEqual(manifest["runTime"], "2026-09-17T15:00:00Z")
        validate_bin_manifest(manifest, expected_hours=1, require_core_variables=True)
        bundle = read_bundle(self.root / "out" / "ir104.xue")
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 600, "firstFrameOffset": 1, "frameCount": 2, "frameStep": 1})
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"], grid["firstLongitude"], grid["firstLatitude"]), (3000, 3000, -135.18, 59.98))
        self.assertFalse(grid["wrapLongitude"])
        variable = bundle.metadata["variables"][0]
        self.assertEqual(variable["band"], GOES_EAST.band("ir104").metadata())
        self.assertEqual(variable["band"]["satelliteNumber"], 273)
        composite = read_bundle(self.root / "out" / "dustrgb.xue")
        self.assertEqual([(v["numericId"], v["id"]) for v in composite.metadata["variables"]], [(1, "dustr"), (2, "dustg"), (3, "dustb")])
        self.assertEqual(composite.metadata["variables"][0]["producer"], {"id": "shachen", "version": DUST.version})
        self.assertEqual(composite.metadata["grid"], grid)

    def test_the_planes_are_kelvin_with_the_disk_at_the_bottom(self) -> None:
        bundle = read_bundle(self.root / "out" / "ir104.xue")
        codes = np.asarray(bundle.decode_plane(1, 1)).reshape(3000, 3000)
        # The fixture covers 66.07–61.77°W, 7.16–11.26°N: columns 1728–1836,
        # rows 1218–1321 of the disk grid; everything else is the fill.
        covered = codes[1225:1315, 1735:1830]
        self.assertGreater(float((covered > 0).mean()), 0.99)
        self.assertEqual(int(codes[:1200].max()), 0)
        self.assertEqual(int(codes[:, :1700].max()), 0)
        self.assertEqual(int(codes[:, 1850:].max()), 0)
        kelvin = PROFILES["quality"]["ir104"].decode(covered)
        self.assertLess(float(kelvin.min()), 240.0)
        self.assertGreater(float(kelvin.max()), 290.0)
        guns = [np.asarray(read_bundle(self.root / "out" / "dustrgb.xue").decode_plane(number, 1)).reshape(3000, 3000) for number in (1, 2, 3)]
        for gun in guns:
            self.assertEqual((gun == 0).tolist() == (codes == 0).tolist(), True)
            self.assertGreaterEqual(int(gun[codes > 0].min()), 1)
            self.assertLessEqual(int(gun[codes > 0].max()), 251)

    def test_the_ladder_is_three_rungs_in_ascending_factor_order(self) -> None:
        """The satellite ladder: half, quarter and eighth per bundle, in
        that order, each decimated from the full plane and cut with the
        source tile over its factor."""
        manifest = json.loads((self.root / "out" / "manifest.json").read_text(encoding="utf-8"))
        rungs = [("half", 2, 1500, 32), ("quarter", 4, 750, 16), ("eighth", 8, 375, 8)]
        for entry in manifest["bundles"]:
            bundle_id = entry["variable"]
            with self.subTest(bundle=bundle_id):
                self.assertEqual([variant["path"] for variant in entry["variants"]], [f"{bundle_id}.{tier}.xue" for tier, _, _, _ in rungs])
                self.assertEqual([variant["width"] for variant in entry["variants"]], [side for _, _, side, _ in rungs])
                full = read_bundle(self.root / "out" / f"{bundle_id}.xue")
                for tier, factor, side, tile in rungs:
                    rung = read_bundle(self.root / "out" / f"{bundle_id}.{tier}.xue")
                    self.assertEqual((rung.width, rung.height, rung.tiles.tile_width, rung.tiles.tile_height), (side, side, tile, tile))
                    self.assertEqual(rung.metadata["time"], full.metadata["time"])
                    offset = full.frame_offsets[-1]
                    expected = np.asarray(full.decode_plane(1, offset)).reshape(3000, 3000)[::factor, ::factor]
                    np.testing.assert_array_equal(np.asarray(rung.decode_plane(1, offset)).reshape(side, side), expected)
        self.assertEqual([Path(variant["output"]).name for variant in self.report["variants"]], [f"{b}.{t}.xue" for b in ("ir104", "dustrgb") for t, _, _, _ in rungs])

    @unittest.skipUnless(native.knows_source("goeseast"), f"the installed {native.DISTRIBUTION} wheel predates the goeseast source")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI")
        subject = self.root / "native"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            report = native.convert_bin(
                self.inputs, subject, model="goeseast", skip_video=True, manifest_path=subject / "manifest.json", require_complete=True, expected_hours=3
            )
        if report["zstdVersion"] != self.report["zstdVersion"]:
            self.skipTest("libzstd differs between the reference and the wheel")
        names = sorted(path.name for path in (self.root / "out").iterdir())
        self.assertEqual(sorted(path.name for path in subject.iterdir()), names)
        for name in names:
            with self.subTest(artifact=name):
                self.assertTrue(filecmp.cmp(self.root / "out" / name, subject / name, shallow=False), name)


if __name__ == "__main__":
    unittest.main()

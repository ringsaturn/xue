"""The satellite source: Himawari-9's 10.4 µm infrared window, an
observation fetched from NOAA's bucket as ISatSS tiles and reprojected in
the fetch stage.

What is new with it. A source named by its orbital role, whose spacecraft
and channel are the optional ``band`` block beside the variable's
parameter (``SourceSpec.bands``, written by both encoders). A fetch stage
of its own (``xuebuild/satellite/``): a platform registry, a reader that
lists and mosaics a slot's tiles, a projector that warps the mosaic onto
the plate carrée grid the bundles carry, a frame cache so a slot is
warped once, and a NetCDF series GDAL itself writes — after which the
source is a ``series_file`` observation like the JMA nowcast, and both
encoders read it unchanged. A published grid that crosses the
antimeridian (80.7°E to 200.7°E).

``tests/fixtures/himawari/`` is four real tiles: T020 and T021 of the
03:00 and 03:10 UTC scans of 2026-09-17, the western Pacific south of Japan.
The bucket is stood in for by a listing and a download built from them.
"""

from __future__ import annotations

import dataclasses
import filecmp
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from xuebuild import binconvert, native, observation, zstdcli
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
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
from xuebuild.satellite import HIMAWARI, PLATFORMS, platform
from xuebuild.satellite import assemble
from xuebuild.satellite import fetch as satellite_fetch
from xuebuild.satellite.platforms import ABI_CHANNELS, AHI_CHANNELS, GOES_EAST
from xuebuild.satellite.projector import GdalWarpProjector, TargetGrid
from xuebuild.satellite.readers import ISatSSReader, parse_isatss_key
from xuebuild.sources import SOURCES, SatelliteBand, source_spec
from xuebuild.stac import _source_prose
from xuebuild.variables import SATELLITE_VARIABLE_IDS, variable_spec

FIXTURES = Path(__file__).parent / "fixtures"
TILES = FIXTURES / "himawari"
REGISTRY = FIXTURES / "satellite-registry.json"
SPEC = source_spec("himawari")
IR104 = HIMAWARI.channel("ir104")
#: The fixture's two slots.
SLOT_0300 = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)
SLOT_0310 = datetime(2026, 9, 17, 3, 10, tzinfo=UTC)
#: The fixture tiles' footprint at the published step, for the tests that
#: warp: 600 x 325 cells.
TILE_GRID = TargetGrid(west=140.0, south=20.0, east=164.0, north=33.0, step=0.04)
#: The platform as the fixture sees it: a slot is two tiles.
TWO_TILES = dataclasses.replace(HIMAWARI, tile_count=2)

requires_gdal = unittest.skipUnless(
    all(shutil.which(command) for command in ("gdalinfo", "gdal_translate", "gdalwarp", "gdalbuildvrt")),
    "GDAL is not on PATH",
)


def fixture_keys() -> dict[str, Path]:
    """The fixture tiles under the keys the bucket lists them by."""
    keys: dict[str, Path] = {}
    for path in sorted(TILES.glob("*.nc")):
        parsed = parse_isatss_key(path.name)
        assert parsed is not None, path.name
        keys[f"{HIMAWARI.prefix}/{parsed.start:%Y/%m/%d/%H%M}/{path.name}"] = path
    return keys


def bucket(keys: dict[str, Path], *, sizes: dict[str, int] | None = None) -> tuple[Callable[[str], str], Callable[[str], bytes]]:
    """A stand-in for the bucket: an S3 ``list-type=2`` listing over
    ``keys`` (with ``delimiter`` honoured) and a download of them."""

    def listing(url: str) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        prefix = query["prefix"][0]
        delimiter = query.get("delimiter", [None])[0]
        body = ['<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated>']
        seen: set[str] = set()
        for key, path in keys.items():
            if not key.startswith(prefix):
                continue
            if delimiter:
                head = key[len(prefix) :].split(delimiter)[0] + delimiter
                if head not in seen:
                    seen.add(head)
                    body.append(f"<CommonPrefixes><Prefix>{prefix}{head}</Prefix></CommonPrefixes>")
            else:
                size = (sizes or {}).get(key, path.stat().st_size)
                body.append(f"<Contents><Key>{key}</Key><Size>{size}</Size></Contents>")
        return "".join(body) + "</ListBucketResult>"

    def download(url: str) -> bytes:
        return keys[url.split(".amazonaws.com/", 1)[1]].read_bytes()

    return listing, download


class RegistryTests(unittest.TestCase):
    def test_the_committed_registry_still_describes_this_encoder(self) -> None:
        expected = json.loads(REGISTRY.read_text(encoding="utf-8"))
        actual = {}
        for variable_id in SATELLITE_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            actual[variable_id] = {
                "label": spec.label,
                "unit": spec.output_unit,
                "parameter": spec.parameter_metadata(),
                "band": {"himawari": HIMAWARI.band(variable_id).metadata()},
                "quality": PROFILES["quality"][variable_id].metadata(),
                "compact": PROFILES["compact"][variable_id].metadata(),
            }
        self.assertEqual(
            actual,
            expected,
            "the satellite registry moved; the Rust encoder and the frontend read the same "
            "fixture, so regenerate it deliberately and change all three",
        )

    def test_brightness_temperature_is_the_top_of_atmosphere_parameter(self) -> None:
        parameter = variable_spec("ir104").parameter_metadata()
        self.assertEqual((parameter["discipline"], parameter["parameterCategory"], parameter["parameterNumber"]), (0, 4, 4))
        self.assertEqual(parameter["typeOfFirstFixedSurface"], 8)
        self.assertIsNone(parameter["scaledValueOfFirstFixedSurface"])

    def test_the_code_space_is_spent_and_the_bottom_is_the_fill(self) -> None:
        for variable_id in SATELLITE_VARIABLE_IDS:
            quality = PROFILES["quality"][variable_id]
            compact = PROFILES["compact"][variable_id]
            self.assertEqual(compact.step, quality.step * 2)
            self.assertEqual(compact.minimum, quality.minimum)
            self.assertEqual(quality.maximum_code, 253)
            self.assertEqual(compact.maximum_code, 126)
            self.assertEqual(quality.minimum, float(variable_spec(variable_id).value_range[0]))
            self.assertEqual(PROFILES["balanced"][variable_id].metadata(), quality.metadata())
        # A cold overshooting top and a hot desert both stay inside.
        codes = PROFILES["quality"]["ir104"].quantize(np.array([180.0, 185.0, 330.0, 331.8]))
        self.assertEqual(codes.tolist(), [0, 8, 250, 253])

    def test_the_source_is_a_satellite_series_file_observation(self) -> None:
        self.assertTrue(SPEC.observation and SPEC.series_file and SPEC.fetched and SPEC.live)
        self.assertEqual((SPEC.platform, SPEC.grid_step, SPEC.cadence_seconds, SPEC.window_hours), ("himawari", 0.04, 600, 3))
        self.assertEqual((SPEC.input_variable_ids, SPEC.bundle_scalar_ids, SPEC.core_bundle_ids), (("ir104",),) * 3)
        self.assertEqual((SPEC.manifest_model, SPEC.latest_filename), ("HIMAWARI", "latest-himawari.json"))
        self.assertEqual([spec.id for spec in SOURCES.values() if spec.platform], ["himawari"])

    def test_the_source_band_is_the_platform_s(self) -> None:
        self.assertEqual(SPEC.bands, (("ir104", HIMAWARI.band("ir104")),))
        band = SPEC.bands[0][1]
        self.assertEqual(band, SatelliteBand(satellite_series=0, satellite_number=174, instrument_type=297, central_wavenumber=96086))
        self.assertEqual(round(1e6 / band.central_wavenumber, 2), 10.41)

    def test_the_production_grid_is_the_platform_region_at_the_step(self) -> None:
        grid = satellite_grid(SPEC)
        self.assertEqual((grid.width, grid.height), SPEC.production_grid)
        self.assertEqual((grid.west, grid.south, grid.east, grid.north), (80.7, -60.0, 200.7, 60.0))
        self.assertEqual((grid.first_longitude, grid.first_latitude), (80.72, 59.98))
        # The disk runs past the antimeridian; the encoders' crop_grid and the
        # shell take such a grid in its own copy of the world.
        self.assertGreater(grid.east, 180.0)
        with self.assertRaisesRegex(DownloadError, "not the production grid"):
            satellite_grid(dataclasses.replace(SPEC, grid_step=0.05))

    def test_the_catalog_prose_names_the_agency_and_the_host(self) -> None:
        prose = _source_prose(SPEC)
        self.assertEqual(prose["providers"][0]["name"], "Japan Meteorological Agency")
        self.assertEqual(prose["providers"][1]["roles"], ["host"])
        self.assertIn("endorses", prose["description"])
        self.assertEqual(prose["links"][0]["rel"], "license")


class PlatformTests(unittest.TestCase):
    def test_a_channel_id_is_the_nominal_wavelength_and_instrument_neutral(self) -> None:
        self.assertEqual(HIMAWARI.channel("ir104").band, 13)
        self.assertEqual(GOES_EAST.channel("ir104").band, 13)
        self.assertEqual(HIMAWARI.channel("wv062").band, 8)
        self.assertEqual(HIMAWARI.channel("vis064").band, 3)
        self.assertEqual(GOES_EAST.channel("vis064").band, 2)
        for channels in (AHI_CHANNELS, ABI_CHANNELS):
            self.assertEqual(len(channels), 16)
            self.assertEqual(len({channel.id for channel in channels}), 16)
            self.assertEqual([channel.band for channel in channels], list(range(1, 17)))
        # Every id names a wavelength the channel's centre rounds to: the
        # solar bands in hundredths of a micron (vis064 is 0.64 µm), the
        # thermal bands in tenths (ir104 is 10.4 µm, ir039 is 3.9 µm).
        for channel in AHI_CHANNELS + ABI_CHANNELS:
            family, digits = re.match(r"([a-z]+)(\d+)", channel.id).groups()
            scale = 100 if family in ("vis", "nir") else 10
            self.assertAlmostEqual(int(digits) / scale, channel.wavelength_um, delta=0.1, msg=channel.id)

    def test_the_band_block_is_the_spacecraft_and_the_centre_wave_number(self) -> None:
        self.assertEqual(HIMAWARI.band("ir104").metadata()["scaledValueOfCentralWaveNumber"], 96086)
        self.assertEqual(GOES_EAST.band("ir104").metadata()["scaledValueOfCentralWaveNumber"], 96618)
        self.assertEqual(GOES_EAST.band("ir104").satellite_number, 273)
        self.assertEqual(HIMAWARI.bands(("ir104", "wv062"))[1][0], "wv062")
        with self.assertRaises(KeyError):
            HIMAWARI.band("ir999")

    def test_the_registry_is_keyed_by_orbital_role(self) -> None:
        self.assertEqual(sorted(PLATFORMS), ["goeseast", "goeswest", "himawari"])
        self.assertIs(platform("himawari"), HIMAWARI)
        self.assertEqual(HIMAWARI.region, (80.7, -60.0, 200.7, 60.0))
        self.assertEqual(GOES_EAST.region, (-135.2, -60.0, -15.2, 60.0))
        with self.assertRaises(KeyError):
            platform("meteosat")


class KeyTests(unittest.TestCase):
    def test_an_isatss_key_parses_to_its_fields(self) -> None:
        key = "AHI-L2-FLDK-ISatSS/2026/09/17/0300/OR_HFD-020-B12-M1C13-T044_GH9_s20262600300000_c20262600308160.nc"
        parsed = parse_isatss_key(key)
        assert parsed is not None
        self.assertEqual((parsed.resolution, parsed.bits, parsed.channel, parsed.tile, parsed.spacecraft), (20, 12, 13, 44, "GH9"))
        self.assertEqual(parsed.start, SLOT_0300)
        self.assertEqual(parsed.created, datetime(2026, 9, 17, 3, 8, 16, tzinfo=UTC))
        self.assertIsNone(parse_isatss_key("AHI-L2-FLDK-ISatSS/2026/09/17/0300/something-else.nc"))

    def test_the_listing_prefix_spells_each_channel_s_own_bit_depth(self) -> None:
        reader = ISatSSReader()
        prefix = reader.slot_prefix(HIMAWARI, IR104, SLOT_0300)
        self.assertEqual(prefix, "AHI-L2-FLDK-ISatSS/2026/09/17/0300/OR_HFD-020-B12-M1C13-")
        self.assertTrue(reader.slot_prefix(HIMAWARI, HIMAWARI.channel("ir039"), SLOT_0300).endswith("OR_HFD-020-B14-M1C07-"))
        self.assertTrue(reader.slot_prefix(HIMAWARI, HIMAWARI.channel("vis064"), SLOT_0300).endswith("OR_HFD-005-B11-M1C03-"))
        self.assertTrue(reader.slot_prefix(HIMAWARI, HIMAWARI.channel("vis047"), SLOT_0300).endswith("OR_HFD-010-B11-M1C01-"))


class ListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = fixture_keys()
        self.listing, self.download = bucket(self.keys)
        self.reader = ISatSSReader()

    def test_a_day_s_slots_are_its_directories(self) -> None:
        day = datetime(2026, 9, 17, tzinfo=UTC)
        self.assertEqual(self.reader.list_slots(HIMAWARI, day, fetch=self.listing), [SLOT_0300, SLOT_0310])
        self.assertEqual(self.reader.list_slots(HIMAWARI, day - timedelta(days=1), fetch=self.listing), [])

    def test_a_slot_s_tiles_are_listed_in_tile_order_and_a_reissue_wins(self) -> None:
        objects = self.reader.list_slot(HIMAWARI, IR104, SLOT_0300, fetch=self.listing)
        self.assertEqual([item.tile for item in objects], [20, 21])
        reissued = dict(self.keys)
        key = next(k for k in self.keys if "0300/" in k and "-T020_" in k)
        later = key.replace("_c20262600308160", "_c20262600309000")
        reissued[later] = self.keys[key]
        listing, _ = bucket(reissued)
        objects = self.reader.list_slot(HIMAWARI, IR104, SLOT_0300, fetch=listing)
        self.assertEqual([item.tile for item in objects], [20, 21])
        self.assertEqual(objects[0].key, later)
        # Another channel's tiles under the same directory are not this slot.
        self.assertEqual(self.reader.list_slot(HIMAWARI, HIMAWARI.channel("wv062"), SLOT_0300, fetch=self.listing), [])

    def test_the_newest_complete_slot_ends_the_live_window(self) -> None:
        now = datetime(2026, 9, 17, 3, 30, tzinfo=UTC)
        self.assertEqual(satellite_fetch.latest_slot(TWO_TILES, IR104, now=now, fetch=self.listing), SLOT_0310)
        # A slot whose tiles are still landing is passed over for the one
        # before it.
        partial = {k: v for k, v in self.keys.items() if not ("0310/" in k and "-T021_" in k)}
        listing, _ = bucket(partial)
        self.assertEqual(satellite_fetch.latest_slot(TWO_TILES, IR104, now=now, fetch=listing), SLOT_0300)
        # Yesterday's directory is asked when today's is empty; two empty
        # days is a feed that is down.
        self.assertEqual(
            satellite_fetch.latest_slot(TWO_TILES, IR104, now=datetime(2026, 9, 18, 0, 5, tzinfo=UTC), fetch=self.listing),
            SLOT_0310,
        )
        with self.assertRaisesRegex(DownloadError, "no complete ir104 slot"):
            satellite_fetch.latest_slot(TWO_TILES, IR104, now=datetime(2026, 9, 20, tzinfo=UTC), fetch=self.listing)

    def test_the_source_dispatches_to_the_platform(self) -> None:
        with mock.patch.dict(PLATFORMS, {"himawari": TWO_TILES}):
            now = datetime(2026, 9, 17, 3, 30, tzinfo=UTC)
            self.assertEqual(latest_satellite_slot(SPEC, now=now, fetch=self.listing), SLOT_0310)
            with mock.patch("xuebuild.satellite.readers._fetch_text", self.listing):
                self.assertEqual(latest_observation_slot(SPEC, now=now), SLOT_0310)
                # The live window is the three hours ending with the newest
                # slot's hour; a named window must have fully landed.
                run = resolve_run("latest", hours=3, now=now, model="himawari")
                self.assertEqual(run.id, "2026091701")
                self.assertFalse(_satellite_run_is_complete(SPEC, GfsRun(SLOT_0300), 3))
                self.assertTrue(_satellite_run_is_complete(SPEC, GfsRun(datetime(2026, 9, 17, 0, tzinfo=UTC)), 3))
                with self.assertRaisesRegex(DownloadError, "has not fully landed"):
                    resolve_run("2026091703", hours=3, now=now, model="himawari")

    def test_a_window_is_every_slot_from_the_hour_through_its_end(self) -> None:
        slots = satellite_fetch.window_slots(HIMAWARI, SLOT_0300, 3)
        self.assertEqual(len(slots), 19)
        self.assertEqual((slots[0], slots[-1]), (SLOT_0300, SLOT_0300 + timedelta(hours=3)))


@requires_gdal
class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-himawari-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.keys = fixture_keys()
        self.listing, self.download = bucket(self.keys)
        self.downloads: list[str] = []

    def download_counting(self, url: str) -> bytes:
        self.downloads.append(url)
        return self.download(url)

    def fetch_window(self, *, force: bool = False, listing: Callable[[str], str] | None = None, grid: TargetGrid = TILE_GRID):
        return satellite_fetch.fetch_window(
            TWO_TILES,
            IR104,
            SLOT_0300,
            1,
            grid=grid,
            raw_root=self.root,
            destination=self.root / "himawari.2026091703",
            series_name="himawari.2026091703.nc",
            unit="K",
            force=force,
            fetch=listing or self.listing,
            download=self.download_counting,
        )

    def test_a_slot_is_warped_once_and_read_from_the_cache_after(self) -> None:
        window = self.fetch_window()
        self.assertEqual([(frame.slot, frame.tiles) for frame in window.frames], [(SLOT_0300, 2), (SLOT_0310, 2)])
        self.assertEqual(len(self.downloads), 4)
        frames = sorted(path.name for path in (self.root / "himawari-frames" / "ir104").iterdir())
        self.assertEqual(frames, ["ir104_20260917030000.tif", "ir104_20260917031000.tif"])
        self.assertFalse((self.root / "himawari.2026091703" / "tiles").exists())
        # The tiles are gone, the frames stay, and the next round downloads
        # nothing.
        again = self.fetch_window()
        self.assertEqual([frame.tiles for frame in again.frames], [0, 0])
        self.assertEqual(len(self.downloads), 4)
        # Forced, the frames are warped again from fresh tiles.
        forced = self.fetch_window(force=True)
        self.assertEqual([frame.tiles for frame in forced.frames], [2, 2])
        self.assertEqual(len(self.downloads), 8)
        self.assertTrue(filecmp.cmp(window.series, forced.series, shallow=False), "a frame is a pure function of its tiles")

    def test_a_frame_is_the_tiles_on_the_target_grid(self) -> None:
        window = self.fetch_window()
        info = json.loads(subprocess.run(["gdalinfo", "-json", "-stats", str(window.frames[0].path)], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(info["size"], [600, 325])
        self.assertEqual(info["geoTransform"][:2], [140.0, 0.04])
        band = info["bands"][0]
        self.assertEqual((band["noDataValue"], band["scale"], band["offset"]), (-32767.0, 0.064208984375, 69.0))
        statistics = band["metadata"][""]
        minimum = float(statistics["STATISTICS_MINIMUM"]) * 0.064208984375 + 69
        maximum = float(statistics["STATISTICS_MAXIMUM"]) * 0.064208984375 + 69
        # Cold cloud tops and clear tropical sea, in kelvin.
        self.assertLess(minimum, 200.0)
        self.assertGreater(maximum, 295.0)
        self.assertLess(maximum, 310.0)

    def test_an_incomplete_slot_is_left_out_and_an_empty_window_is_an_error(self) -> None:
        partial = {k: v for k, v in self.keys.items() if not ("0310/" in k and "-T021_" in k)}
        listing, _ = bucket(partial)
        window = self.fetch_window(listing=listing)
        self.assertEqual([frame.slot for frame in window.frames], [SLOT_0300])
        self.assertEqual(len(self.downloads), 2)
        with self.assertRaisesRegex(DownloadError, "no complete ir104 slot"):
            self.fetch_window(listing=bucket({})[0], force=True)

    def test_the_series_is_what_the_observation_ingest_reads(self) -> None:
        window = self.fetch_window()
        series = observation.inspect_observation(window.series, SPEC)
        self.assertEqual(series.lead_seconds, [0, 600])
        frame = series.frames[0]["ir104"]
        self.assertEqual((frame.run_time, frame.valid_time, frame.unit), (SLOT_0300, SLOT_0300, "K"))
        self.assertEqual(series.plane_source.fill_replacement, 180)
        self.assertEqual(series.plane_source.fill_values[0], -32767.0)
        self.assertTrue(series.plane_source.unscale)

    def test_the_source_fetch_writes_the_series_and_a_fetch_record(self) -> None:
        with mock.patch.dict(PLATFORMS, {"himawari": TWO_TILES}):
            written = _fetch_satellite_run(
                SPEC, GfsRun(SLOT_0300), 3, self.root, force=False, input_ids=None, fetch=self.listing, download=self.download
            )
        self.assertEqual(written, [self.root / "himawari.2026091703" / "himawari.2026091703.nc"])
        record = json.loads((self.root / "himawari.2026091703" / "fetch.json").read_text(encoding="utf-8"))
        self.assertEqual((record["model"], record["run"], record["hours"], record["cadenceSeconds"]), ("himawari", "2026091703", 3, 600))
        self.assertEqual(record["platform"], "Himawari-9")
        self.assertEqual(record["grid"], {"step": 0.04, "width": 3000, "height": 3000, "firstLongitude": 80.72, "firstLatitude": 59.98})
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-09-17T03:00:00Z", "2026-09-17T03:10:00Z"])
        self.assertEqual([frame["tilesFetched"] for frame in record["frames"]], [2, 2])
        summary = window_summary(self.root / "himawari.2026091703")
        self.assertEqual((summary["frameCount"], summary["latestSlot"]), (2, "2026-09-17T03:10:00Z"))
        with self.assertRaisesRegex(DownloadError, "publishes"):
            _fetch_satellite_run(SPEC, GfsRun(SLOT_0300), 3, self.root, force=False, input_ids=("cref",), fetch=self.listing)

    def test_the_projector_refuses_a_grid_that_is_not_whole_cells(self) -> None:
        with self.assertRaisesRegex(ConversionError, "whole number"):
            TargetGrid(west=140.0, south=20.0, east=164.01, north=33.0, step=0.04)
        with self.assertRaisesRegex(ConversionError, "unsupported resampling"):
            GdalWarpProjector().to_grid(self.root / "x.vrt", TILE_GRID, nodata=-32767, resampling="lanczos", out=self.root / "x.tif")
        self.assertEqual(assemble.frame_path(self.root, IR104, SLOT_0310), self.root / "ir104" / "ir104_20260917031000.tif")


@requires_gdal
class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-himawari-convert-"))
        listing, download = bucket(fixture_keys())
        with mock.patch.dict(PLATFORMS, {"himawari": TWO_TILES}):
            _fetch_satellite_run(SPEC, GfsRun(SLOT_0300), 3, cls.root, force=False, input_ids=None, fetch=listing, download=download)
        cls.inputs = cls.root / "himawari.2026091703"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                cls.inputs,
                cls.root / "out",
                model="himawari",
                skip_video=True,
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
                require_complete=True,
                expected_hours=3,
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_the_bundle_is_the_disk_grid_with_the_band_beside_the_parameter(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["ir104"])
        manifest = json.loads((self.root / "out" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["model"], manifest["product"]), ("HIMAWARI", "ahi-fldk-0p04"))
        self.assertEqual(manifest["runTime"], "2026-09-17T03:00:00Z")
        validate_bin_manifest(manifest, expected_hours=1, require_core_variables=True)
        bundle = read_bundle(self.root / "out" / "ir104.xue")
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 1})
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (3000, 3000))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (80.72, 59.98))
        self.assertFalse(grid["wrapLongitude"])
        variable = bundle.metadata["variables"][0]
        self.assertEqual(list(variable), ["numericId", "id", "label", "unit", "parameter", "band", "quantization"])
        self.assertEqual(variable["unit"], "K")
        self.assertEqual(variable["band"], HIMAWARI.band("ir104").metadata())
        self.assertEqual(variable["parameter"]["typeOfFirstFixedSurface"], 8)
        self.assertEqual(bundle.tiles.count, 47 * 47)

    def test_the_planes_are_kelvin_with_the_uncovered_disk_at_the_bottom(self) -> None:
        bundle = read_bundle(self.root / "out" / "ir104.xue")
        codes = np.asarray(bundle.decode_plane(1, 0)).reshape(3000, 3000)
        kelvin = PROFILES["quality"]["ir104"].decode(codes)
        # The two tiles cover 140.7–162.9°E, 20.7–32.5°N: columns 1500–2055,
        # rows 688–983 of the disk grid; everything else is the fill.
        covered = codes[700:970, 1520:2040]
        self.assertGreater(float((covered > 0).mean()), 0.99)
        self.assertEqual(int(codes[:600].max()), 0)
        self.assertEqual(int(codes[:, :1400].max()), 0)
        self.assertEqual(int(codes[:, 2300:].max()), 0)
        inside = kelvin[700:970, 1520:2040][covered > 0]
        self.assertLess(float(inside.min()), 200.0)
        self.assertGreater(float(inside.max()), 295.0)
        for name in ("ir104.half.xue", "ir104.poster.bin"):
            self.assertTrue((self.root / "out" / name).is_file(), name)

    def test_a_crop_past_the_antimeridian_reads_the_disk_s_eastern_columns(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            report = binconvert.convert_bin(
                self.inputs,
                self.root / "crop",
                model="himawari",
                skip_video=True,
                skip_variants=True,
                work_root=self.root / "crop-work",
                bbox=(-175.0, 0.0, -160.0, 10.0),
            )
        self.assertEqual([bundle["variable"] for bundle in report["bundles"]], ["ir104"])
        grid = read_bundle(self.root / "crop" / "ir104.xue").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (376, 252))
        self.assertEqual(grid["firstLongitude"], -175.0)
        self.assertFalse(grid["wrapLongitude"])

    @unittest.skipUnless(native.knows_source("himawari"), f"the installed {native.DISTRIBUTION} wheel predates the himawari source")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI")
        subject = self.root / "native"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            report = native.convert_bin(
                self.inputs, subject, model="himawari", skip_video=True, manifest_path=subject / "manifest.json", require_complete=True, expected_hours=3
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

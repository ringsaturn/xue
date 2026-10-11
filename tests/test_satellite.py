"""The satellite source: Himawari-9's infrared windows, an observation
fetched from NOAA's bucket as ISatSS tiles and reprojected in the fetch
stage, and the Dust RGB composed from four of them there.

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

What came with the composite. A window of several channels, each warped
and cached per slot; a producer (``xuebuild/satellite/producers.py``) run
per slot on the warped channels, its outputs cached as frames of their
own; one series file per variable, the produced ones stamped with the
producer's id and version; an observation ingest that reads several
variables with their own packing; a composite bundle of three variables
whose ``producer`` block sits beside a local-use ``parameter``.

``tests/fixtures/himawari/`` is forty real tiles: T020 and T021 of the
03:00 and 03:10 UTC scans of 2026-09-17 in bands 7, 8, 11, 13, 14 and 15
(3.9, 6.2, 8.6, 10.4, 11.2 and 12.3 µm), the western Pacific south of
Japan, untouched; and the same tiles in bands 1–4 (0.47, 0.51, 0.64 and
0.86 µm, the true colour composite's inputs) cut to their north-west
corner (``tests/prepare_himawari_fixture.py``), so the composite is
defined over a sixteenth of each tile. The bucket is stood in for by a
listing and a download built from them. The DEBRA producer's ancillary
fields are staged from ``tests/fixtures/debra/`` (:func:`stage_ancillary`);
its own tests are in ``tests/test_debra.py``.
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

from tests._support import ClassTempRoot, FIXTURES, TempRoot, assert_native_matches, requires_gdal_warp, requires_native_source, requires_shachen
from xuebuild import binconvert, observation
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
from xuebuild.satellite import HIMAWARI, METEOSAT, PLATFORMS, platform
from xuebuild.satellite import assemble, readers
from xuebuild.satellite import fetch as satellite_fetch
from xuebuild.satellite.platforms import ABI_CHANNELS, AHI_CHANNELS, GOES_EAST
from xuebuild.satellite import ancillary
from xuebuild.satellite.producers import (
    PRODUCERS,
    TRUE_COLOR_CLOUD_EXPONENT,
    TRUE_COLOR_CLOUD_TEMPERATURE_K,
    TRUE_COLOR_CURVE_IN,
    TRUE_COLOR_CURVE_OUT,
    TRUE_COLOR_HYBRID_GREEN,
    TRUE_COLOR_LOW_CLOUD_DIFFERENCE_K,
    TRUE_COLOR_LOW_CLOUD_WHITE,
    TRUE_COLOR_NIGHT_GROUND,
    TRUE_COLOR_TERMINATOR_DEG,
    TRUE_COLOR_VERSION,
    TRUE_COLOR_ZENITH_LIMIT_DEG,
    DustRGBProducer,
    TrueColorProducer,
    true_color_day_weight,
)
from xuebuild.satellite.projector import GdalWarpProjector, TargetGrid
from xuebuild.satellite.readers import ISatSSReader, parse_isatss_key
from xuebuild.sources import SOURCES, SatelliteBand, source_spec
from xuebuild.stac import _source_prose
from xuebuild.variables import (
    DUST_CF_BUNDLE_ID,
    ZHOUYE_BUNDLE_ID,
    DUST_RGB_BUNDLE_ID,
    DUST_RGB_COMPONENT_IDS,
    SATELLITE_CHANNEL_IDS,
    SATELLITE_INFRARED_IDS,
    SATELLITE_REFLECTANCE_IDS,
    SATELLITE_VARIABLE_IDS,
    TRUE_COLOR_BUNDLE_ID,
    TRUE_COLOR_COMPONENT_IDS,
    VARIABLES,
    variable_spec,
)

TILES = FIXTURES / "himawari"
DEBRA_FIXTURES = FIXTURES / "debra"
REGISTRY = FIXTURES / "satellite-registry.json"
SPEC = source_spec("himawari")
IR104 = HIMAWARI.channel("ir104")
#: The ten channels the source fetches, in source order: six infrared
#: windows, then the four solar bands.
CHANNELS = tuple(HIMAWARI.channel(channel_id) for channel_id in SPEC.input_variable_ids)
INFRARED = tuple(channel for channel in CHANNELS if channel.kind == "bt")
SOLAR = tuple(channel for channel in CHANNELS if channel.kind == "reflectance")
#: The channels and the variables every satellite source's fetch writes,
#: in series order: the channels, the Dust RGB's guns, the confidences,
#: the true colour's guns.
SERIES_IDS = (
    SPEC.input_variable_ids
    + DUST_RGB_COMPONENT_IDS
    + (DUST_CF_BUNDLE_ID, ZHOUYE_BUNDLE_ID)
    + TRUE_COLOR_COMPONENT_IDS
)
DUST = PRODUCERS[DUST_RGB_BUNDLE_ID]
DEBRA = PRODUCERS[DUST_CF_BUNDLE_ID]
TRUECOLOR = PRODUCERS[TRUE_COLOR_BUNDLE_ID]
#: The fixture's two slots.
SLOT_0300 = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)
SLOT_0310 = datetime(2026, 9, 17, 3, 10, tzinfo=UTC)
#: The fixture tiles' footprint at the published step, for the tests that
#: warp: 600 x 325 cells.
TILE_GRID = TargetGrid(west=140.0, south=20.0, east=164.0, north=33.0, step=0.04)
#: The platform as the fixture sees it: a slot is two tiles.
TWO_TILES = dataclasses.replace(HIMAWARI, tile_count=2)


def stage_ancillary(raw_root: Path, slots: tuple[datetime, ...], *, skin: str = "gfs.tmpsfc.pacific.grib2") -> Path:
    """What the DEBRA producer reads beside the channels, staged under the
    raw root the way `make pull-r2-ancillary` and a previous round leave
    it: one CAMEL month of one region (the operational pipeline's gobi
    subset cut to a box of water inside the fixture tiles' grid) and, for
    each slot, the GFS skin-temperature record its first candidate cycle
    would be fetched as, already cached — so no test touches the GFS
    bucket and the fixture record (a 2026-09-14 field; the producer reads
    the field, not its stamp) stands in for whatever cycle a slot asks."""
    root = raw_root / "ancillary"
    camel = root / "camel" / "gobi"
    camel.mkdir(parents=True, exist_ok=True)
    shutil.copy(DEBRA_FIXTURES / "camel.gobi.202609.pacific.nc", camel / "202609.nc")
    gfs = root / "gfs"
    gfs.mkdir(parents=True, exist_ok=True)
    for slot in slots:
        cycle, forecast_hour = ancillary.skin_temperature_candidates(slot)[0]
        shutil.copy(DEBRA_FIXTURES / skin, gfs / ancillary.skin_temperature_name(cycle, forecast_hour))
    return root


def fixture_keys() -> dict[str, Path]:
    """The fixture tiles under the keys the bucket lists them by."""
    keys: dict[str, Path] = {}
    for path in sorted(TILES.glob("*.nc")):
        parsed = parse_isatss_key(path.name)
        assert parsed is not None, path.name
        keys[f"{HIMAWARI.prefix}/{parsed.start:%Y/%m/%d/%H%M}/{path.name}"] = path
    return keys


def registry_document() -> dict[str, object]:
    """The satellite registry as this encoder describes it: every channel
    and gun with its label, unit, parameter, band (channels, per platform)
    or producer id (guns), and codebooks; then each composite bundle's
    components in order. tests/fixtures/satellite-registry.json is this."""
    variables: dict[str, object] = {}
    for variable_id in SATELLITE_VARIABLE_IDS:
        spec = variable_spec(variable_id)
        entry: dict[str, object] = {
            "label": spec.label,
            "unit": spec.output_unit,
            "parameter": spec.parameter_metadata(),
        }
        if spec.producer_id is not None:
            entry["producer"] = {"id": spec.producer_id}
        else:
            entry["band"] = {"himawari": HIMAWARI.band(variable_id).metadata()}
        entry["quality"] = PROFILES["quality"][variable_id].metadata()
        entry["compact"] = PROFILES["compact"][variable_id].metadata()
        variables[variable_id] = entry
    return {"variables": variables, "bundles": {bundle_id: list(ids) for bundle_id, ids in binconvert.COMPOSITE_BUNDLES.items()}}


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
        self.assertEqual(registry_document(), expected,
            "the satellite registry moved; the Rust encoder and the frontend read the same "
            "fixture, so regenerate it deliberately (tests/prepare_satellite_registry.py) and change all three",
        )

    def test_brightness_temperature_is_the_top_of_atmosphere_parameter(self) -> None:
        parameter = variable_spec("ir104").parameter_metadata()
        self.assertEqual((parameter["discipline"], parameter["parameterCategory"], parameter["parameterNumber"]), (0, 4, 4))
        self.assertEqual(parameter["typeOfFirstFixedSurface"], 8)
        self.assertIsNone(parameter["scaledValueOfFirstFixedSurface"])

    def test_the_code_space_is_spent_and_the_bottom_is_the_fill(self) -> None:
        self.assertEqual(SATELLITE_CHANNEL_IDS, SATELLITE_INFRARED_IDS + SATELLITE_REFLECTANCE_IDS)
        for variable_id in SATELLITE_CHANNEL_IDS:
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
        # A reflectance is a number in 0–1 that a sunlit cloud takes past 1:
        # the satellite-image albedo parameter with the band beside it, and
        # a codebook whose code 0 is the fill, a hundredth below zero, so
        # the night side's 0.0 is a whole code on both profiles.
        for variable_id in SATELLITE_REFLECTANCE_IDS:
            spec = variable_spec(variable_id)
            parameter = spec.parameter_metadata()
            self.assertEqual((parameter["discipline"], parameter["parameterCategory"], parameter["parameterNumber"], parameter["typeOfFirstFixedSurface"]), (3, 0, 1, 8))
            self.assertEqual((spec.output_unit, spec.value_range, spec.producer_id), ("1", (-0.01, 1.255), None))
        codes = PROFILES["quality"]["vis064"].quantize(np.array([-0.01, 0.0, 0.5, 1.255, 1.4]))
        self.assertEqual(codes.tolist(), [0, 2, 102, 253, 253])
        self.assertEqual(PROFILES["compact"]["vis064"].quantize(np.array([-0.01, 0.0, 1.25])).tolist(), [0, 1, 126])

    def test_a_gun_is_a_local_use_parameter_with_the_producer_registered(self) -> None:
        """A composite has no GRIB2 parameter: each gun takes a local-use
        number (discipline 3, category 192) that means nothing without the
        producer, so the producer id is on the registry row and the block
        beside the parameter (written from the series' stamp) is what a
        reader keys on. One codebook for every profile, with code 0 one
        step below zero: "no data" for all three guns at once, so that
        black stays a value."""
        for number, gun_id in enumerate(DUST_RGB_COMPONENT_IDS, start=1):
            spec = variable_spec(gun_id)
            parameter = spec.parameter_metadata()
            self.assertEqual((parameter["discipline"], parameter["parameterCategory"], parameter["parameterNumber"]), (3, 192, number))
            self.assertEqual(parameter["typeOfFirstFixedSurface"], 8)
            self.assertEqual((spec.producer_id, spec.output_unit, spec.value_range), ("shachen", "1", (-0.004, 1)))
            for profile in PROFILES:
                codebook = PROFILES[profile][gun_id]
                self.assertEqual(
                    codebook.metadata(),
                    {"type": "linear", "offset": -0.004, "scale": 0.004, "minimumCode": 0, "maximumCode": 251, "nodataCode": 255},
                )
        self.assertIsNone(variable_spec("ir104").producer_id)
        codes = PROFILES["quality"]["dustr"].quantize(np.array([-0.004, 0.0, 0.5, 1.0]))
        self.assertEqual(codes.tolist(), [0, 1, 126, 251])
        self.assertEqual(PROFILES["quality"]["dustr"].decode(np.array([1, 251], dtype=np.uint8)).tolist(), [0.0, 1.0])
        self.assertEqual(
            binconvert.COMPOSITE_BUNDLES,
            {
                "dustrgb": ("dustr", "dustg", "dustb"),
                "dustcf": ("dustcf",),
                "zhouye": ("zhouye",),
                "truecolor": ("truer", "trueg", "trueb"),
            },
        )
        self.assertEqual(binconvert.bundle_variable_ids("dustrgb"), DUST_RGB_COMPONENT_IDS)
        self.assertEqual(binconvert.bundle_variable_ids("dustcf"), ("dustcf",))
        self.assertEqual(binconvert.bundle_variable_ids("ir104"), ("ir104",))
        # The confidence is the next local-use number under the same
        # producer, with the guns' codebook: code 0 is no data, 0.0 is code 1.
        spec = variable_spec("dustcf")
        parameter = spec.parameter_metadata()
        self.assertEqual((parameter["discipline"], parameter["parameterCategory"], parameter["parameterNumber"], parameter["typeOfFirstFixedSurface"]), (3, 192, 4, 8))
        self.assertEqual((spec.producer_id, spec.output_unit, spec.value_range), ("shachen", "1", (-0.004, 1)))
        for profile in PROFILES:
            self.assertEqual(PROFILES[profile]["dustcf"].metadata(), PROFILES[profile]["dustr"].metadata())
        # The true colour guns: this pipeline's own recipe, so the producer
        # is `xue`, the next three local-use numbers, the guns' codebook.
        for number, gun_id in zip((7, 8, 9), TRUE_COLOR_COMPONENT_IDS):
            spec = variable_spec(gun_id)
            parameter = spec.parameter_metadata()
            self.assertEqual((parameter["discipline"], parameter["parameterCategory"], parameter["parameterNumber"], parameter["typeOfFirstFixedSurface"]), (3, 192, number, 8))
            self.assertEqual((spec.producer_id, spec.output_unit, spec.value_range), ("xue", "1", (-0.004, 1)))
            for profile in PROFILES:
                self.assertEqual(PROFILES[profile][gun_id].metadata(), PROFILES[profile]["dustr"].metadata())
        self.assertEqual(binconvert.bundle_variable_ids("truecolor"), TRUE_COLOR_COMPONENT_IDS)
        self.assertEqual(SATELLITE_VARIABLE_IDS[-3:], TRUE_COLOR_COMPONENT_IDS)
        # No night microphysics composite: the true colour carries the night
        # side, and local numbers 10–12 are left unused.
        self.assertNotIn("nightmicro", binconvert.COMPOSITE_BUNDLES)
        for gun_id in ("nightr", "nightg", "nightb"):
            self.assertNotIn(gun_id, VARIABLES)
        self.assertFalse(
            [spec.id for spec in VARIABLES.values() if spec.grib2_discipline == 3 and spec.grib2_category == 192 and spec.grib2_number in (10, 11, 12)]
        )

    def test_the_source_is_a_satellite_series_file_observation(self) -> None:
        self.assertTrue(SPEC.observation and SPEC.series_file and SPEC.fetched and SPEC.live)
        self.assertEqual((SPEC.platform, SPEC.grid_step, SPEC.cadence_seconds, SPEC.window_hours), ("himawari", 0.04, 600, 6))
        self.assertEqual(SPEC.input_variable_ids, ("ir039", "wv062", "ir086", "ir104", "ir112", "ir123", "vis047", "vis051", "vis064", "nir086"))
        self.assertEqual(SPEC.input_variable_ids, SATELLITE_CHANNEL_IDS)
        self.assertEqual((SPEC.bundle_scalar_ids, SPEC.bundle_composite_ids, SPEC.core_bundle_ids), (("ir104",), ("dustrgb", "dustcf", "zhouye", "truecolor"), ("ir104",)))
        self.assertEqual(binconvert.published_bundle_ids(SPEC), ("ir104", "dustrgb", "dustcf", "zhouye", "truecolor"))
        # A composite's inputs, for the fetch, are the channels its
        # producer reads; a source that fetched fewer would not publish it.
        self.assertEqual(binconvert.bundle_input_ids(SPEC, "dustrgb"), ("ir086", "ir104", "ir112", "ir123"))
        self.assertEqual(binconvert.bundle_input_ids(SPEC, "dustcf"), ("ir039", "wv062", "ir086", "ir104", "ir123"))
        self.assertEqual(binconvert.bundle_input_ids(SPEC, "truecolor"), ("vis047", "vis051", "vis064", "nir086", "ir039", "ir104"))
        fewer = dataclasses.replace(SPEC, input_variable_ids=("ir104",))
        self.assertEqual(binconvert.published_bundle_ids(fewer), ("ir104",))
        dust_only = dataclasses.replace(SPEC, input_variable_ids=("ir086", "ir104", "ir112", "ir123"))
        self.assertEqual(binconvert.published_bundle_ids(dust_only), ("ir104", "dustrgb"))
        windows_only = dataclasses.replace(SPEC, input_variable_ids=SATELLITE_INFRARED_IDS)
        self.assertEqual(binconvert.published_bundle_ids(windows_only), ("ir104", "dustrgb", "dustcf", "zhouye"))
        solar_only = dataclasses.replace(SPEC, input_variable_ids=("ir104", *SATELLITE_REFLECTANCE_IDS))
        self.assertEqual(binconvert.published_bundle_ids(solar_only), ("ir104",))
        self.assertEqual([spec.id for spec in SOURCES.values() if spec.platform], ["himawari", "himawarijp", "goeseast", "goeswest", "meteosat"])

    def test_the_source_band_is_the_platform_s(self) -> None:
        self.assertEqual([band_id for band_id, _ in SPEC.bands], list(SATELLITE_CHANNEL_IDS))
        band = dict(SPEC.bands)["ir104"]
        self.assertEqual(band, SatelliteBand(satellite_series=0, satellite_number=174, instrument_type=297, central_wavenumber=96086))
        self.assertEqual(round(1e6 / band.central_wavenumber, 2), 10.41)
        self.assertEqual(dict(SPEC.bands)["ir123"].central_wavenumber, 80772)
        # The solar bands by the AHI's own central wavelengths.
        self.assertEqual([dict(SPEC.bands)[band_id].central_wavenumber for band_id in SATELLITE_REFLECTANCE_IDS], [2124811, 1960784, 1564602, 1167270])

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
        self.assertEqual(sorted(PLATFORMS), ["goeseast", "goeswest", "himawari", "meteosat", "meteosatiodc"])
        self.assertIs(platform("himawari"), HIMAWARI)
        self.assertEqual(HIMAWARI.region, (80.7, -60.0, 200.7, 60.0))
        self.assertEqual(GOES_EAST.region, (-135.2, -60.0, -15.2, 60.0))
        with self.assertRaises(KeyError):
            platform("mtg")


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

    def test_the_rapid_scan_directories_between_the_full_disks_are_not_slots(self) -> None:
        # The prefix carries the target-area rapid scans (OR_HR3-*) under
        # two-and-a-half-minute directories of their own, four between
        # each pair of full disks. They are not slots: a listing that took
        # them would have latest_slot look at the newest three of them
        # and find no full disk, as the live rounds did at every minute
        # but the one right after a full-disk directory appeared.
        keys = dict(self.keys)
        for hhmm in ("0302", "0304", "0307", "0309", "0312", "0314"):
            keys[f"{HIMAWARI.prefix}/2026/09/17/{hhmm}/OR_HR3-020-B12-M1C13-T001_GH9_s2026260{hhmm}160_c2026260{hhmm}410.nc"] = self.keys[
                next(iter(self.keys))
            ]
        listing, _ = bucket(keys)
        day = datetime(2026, 9, 17, tzinfo=UTC)
        self.assertEqual(self.reader.list_slots(HIMAWARI, day, fetch=listing), [SLOT_0300, SLOT_0310])
        now = datetime(2026, 9, 17, 3, 15, tzinfo=UTC)
        self.assertEqual(self.reader.recent_slots(HIMAWARI, now, limit=3, fetch=listing), [SLOT_0310, SLOT_0300])
        self.assertEqual(satellite_fetch.latest_slot(TWO_TILES, IR104, now=now, fetch=listing), SLOT_0310)

    def test_a_slot_s_tiles_are_listed_in_tile_order_and_a_reissue_wins(self) -> None:
        objects = self.reader.list_slot(HIMAWARI, IR104, SLOT_0300, fetch=self.listing)
        self.assertEqual([item.tile for item in objects], [20, 21])
        reissued = dict(self.keys)
        key = next(k for k in self.keys if "0300/" in k and "-M1C13-T020_" in k)
        later = key.replace("_c20262600308160", "_c20262600309000")
        reissued[later] = self.keys[key]
        listing, _ = bucket(reissued)
        objects = self.reader.list_slot(HIMAWARI, IR104, SLOT_0300, fetch=listing)
        self.assertEqual([item.tile for item in objects], [20, 21])
        self.assertEqual(objects[0].key, later)
        # Another channel's tiles under the same directory are its own slot:
        # the 0.64 µm band's two, at their 0.5 km resolution in the name.
        solar = self.reader.list_slot(HIMAWARI, HIMAWARI.channel("vis064"), SLOT_0300, fetch=self.listing)
        self.assertEqual([item.tile for item in solar], [20, 21])
        self.assertIn("OR_HFD-005-B11-M1C03-", solar[0].key)
        self.assertEqual(self.reader.list_slot(HIMAWARI, HIMAWARI.channel("nir161"), SLOT_0300, fetch=self.listing), [])

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
                # The live window is the hours ending with the newest slot's
                # hour (six by default); a named window must have fully landed.
                run = resolve_run("latest", hours=3, now=now, model="himawari")
                self.assertEqual(run.id, "2026091701")
                self.assertEqual(resolve_run("latest", hours=SPEC.window_hours, now=now, model="himawari").id, "2026091622")
                self.assertFalse(_satellite_run_is_complete(SPEC, GfsRun(SLOT_0300), 3, now=now))
                self.assertTrue(_satellite_run_is_complete(SPEC, GfsRun(datetime(2026, 9, 17, 0, tzinfo=UTC)), 3, now=now))
                with self.assertRaisesRegex(DownloadError, "has not fully landed"):
                    resolve_run("2026091703", hours=3, now=now, model="himawari")

    def test_a_window_is_every_slot_from_the_hour_through_its_end(self) -> None:
        slots = satellite_fetch.window_slots(HIMAWARI, SLOT_0300, 3)
        self.assertEqual(len(slots), 19)
        self.assertEqual((slots[0], slots[-1]), (SLOT_0300, SLOT_0300 + timedelta(hours=3)))


@requires_gdal_warp
def warp_window(
    root: Path,
    ancillary: Path,
    listing: Callable[[str], str],
    download: Callable[[str], bytes],
    *,
    force: bool = False,
    grid: TargetGrid = TILE_GRID,
    channels: tuple = (IR104,),
    producers: tuple = (),
    series_concurrency: int = satellite_fetch.SERIES_CONCURRENCY,
):
    """The fixture's one-hour window fetched into ``root`` on two tiles."""
    return satellite_fetch.fetch_window(
        TWO_TILES,
        channels,
        SLOT_0300,
        1,
        grid=grid,
        raw_root=root,
        destination=root / "himawari.2026091703",
        series_stem="himawari.2026091703",
        units={channel.id: VARIABLES[channel.id].output_unit for channel in channels},
        producers=producers,
        ancillary_root=ancillary,
        force=force,
        fetch=listing,
        download=download,
        series_concurrency=series_concurrency,
    )


class FetchTests(TempRoot, unittest.TestCase):
    root_prefix = "xue-himawari-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        # The ten channels warped once for the class: the first round most
        # of these tests start from, and the slow part of each. A test that
        # starts from it takes a copy (from_seed), since the tests change
        # what they read.
        cls.seed = Path(tempfile.mkdtemp(prefix="xue-himawari-seed-"))
        cls.addClassCleanup(shutil.rmtree, cls.seed, True)
        listing, download = bucket(fixture_keys())
        cls.seed_downloads: list[str] = []

        def download_counting(url: str) -> bytes:
            cls.seed_downloads.append(url)
            return download(url)

        ancillary = stage_ancillary(cls.seed, (SLOT_0300, SLOT_0310))
        cls.seed_window = warp_window(cls.seed, ancillary, listing, download_counting, channels=CHANNELS)

    def from_seed(self) -> None:
        """The class's warped ten-channel window, as this test's raw root."""
        shutil.copytree(self.seed, self.root, dirs_exist_ok=True)

    def setUp(self) -> None:
        super().setUp()
        self.keys = fixture_keys()
        self.listing, self.download = bucket(self.keys)
        self.downloads: list[str] = []
        self.ancillary = stage_ancillary(self.root, (SLOT_0300, SLOT_0310))

    def download_counting(self, url: str) -> bytes:
        self.downloads.append(url)
        return self.download(url)

    def fetch_window(
        self,
        *,
        force: bool = False,
        listing: Callable[[str], str] | None = None,
        grid: TargetGrid = TILE_GRID,
        channels: tuple = (IR104,),
        producers: tuple = (),
    ):
        return warp_window(
            self.root,
            self.ancillary,
            listing or self.listing,
            self.download_counting,
            force=force,
            grid=grid,
            channels=channels,
            producers=producers,
        )

    def test_concurrent_series_equal_the_serial_ones(self) -> None:
        """Series are stacked on a pool; what each file holds and the order
        of the mapping do not depend on how many run at once, and no
        scratch file survives."""
        self.from_seed()
        kwargs = dict(channels=CHANNELS)

        def content(path: Path) -> list:
            # What a series holds — each band's checksum, values' metadata
            # and the time axis — rather than its bytes: the HDF5 object
            # headers carry the second the file was written.
            info = json.loads(
                subprocess.run(["gdalinfo", "-json", "-checksum", str(path)], check=True, capture_output=True, text=True).stdout
            )
            return [(band["checksum"], band.get("metadata", {})) for band in info["bands"]]

        serial = warp_window(self.root, self.ancillary, self.listing, self.download_counting, series_concurrency=1, **kwargs)
        serial_content = {key: content(path) for key, path in serial.series.items()}
        parallel = warp_window(self.root, self.ancillary, self.listing, self.download_counting, series_concurrency=4, **kwargs)
        self.assertEqual(list(parallel.series), [channel.id for channel in CHANNELS])
        self.assertEqual(list(parallel.series), list(serial.series))
        for key, path in parallel.series.items():
            self.assertEqual(content(path), serial_content[key], key)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir() if p.suffix != ".nc"), [])

    def test_a_failed_series_leaves_no_partial_file(self) -> None:
        self.from_seed()
        real = assemble.run_command

        def failing(arguments, **kwargs):
            if "ir104" in kwargs.get("description", ""):
                raise ConversionError("boom")
            return real(arguments, **kwargs)

        with mock.patch.object(assemble, "run_command", failing):
            with self.assertRaisesRegex(ConversionError, "boom"):
                warp_window(self.root, self.ancillary, self.listing, self.download_counting, channels=CHANNELS)
        directory = self.root / "himawari.2026091703"
        self.assertEqual([p.name for p in directory.iterdir() if ".part" in p.name or p.suffix == ".vrt"], [])

    def test_a_slot_is_warped_once_and_read_from_the_cache_after(self) -> None:
        window = self.fetch_window()
        self.assertEqual([(item.slot, item.tiles) for item in window.slots], [(SLOT_0300, 2), (SLOT_0310, 2)])
        self.assertEqual(list(window.series), ["ir104"])
        self.assertEqual(window.series["ir104"], self.root / "himawari.2026091703" / "himawari.2026091703.ir104.nc")
        self.assertEqual(len(self.downloads), 4)
        frames = sorted(path.name for path in (self.root / "himawari-frames" / "ir104").iterdir())
        self.assertEqual(
            frames,
            [
                "ir104_20260917030000.json",
                "ir104_20260917030000.tif",
                "ir104_20260917031000.json",
                "ir104_20260917031000.tif",
            ],
        )
        self.assertFalse((self.root / "himawari.2026091703" / "tiles").exists())
        # The packing beside each frame is the source's, so the series never
        # depends on what a given GDAL's warp carries through to the band.
        packing = json.loads((self.root / "himawari-frames" / "ir104" / "ir104_20260917030000.json").read_text())
        self.assertEqual((packing["scale"], packing["offset"], packing["unit"]), (0.064208984375, 69.0, "kelvin"))
        self.assertEqual((packing["tiles"], len(packing["keys"]), packing["projector"]), (2, 2, "gdalwarp"))
        self.assertEqual(assemble.frame_packing(window.slots[0].frames["ir104"]), assemble.Packing(0.064208984375, 69.0, "kelvin"))
        # The tiles are gone, the frames stay, and the next round downloads
        # nothing.
        again = self.fetch_window()
        self.assertEqual([item.tiles for item in again.slots], [0, 0])
        self.assertEqual(len(self.downloads), 4)
        # Forced, the frames are warped again from fresh tiles.
        forced = self.fetch_window(force=True)
        self.assertEqual([item.tiles for item in forced.slots], [2, 2])
        self.assertEqual(len(self.downloads), 8)
        self.assertTrue(filecmp.cmp(window.series["ir104"], forced.series["ir104"], shallow=False), "a frame is a pure function of its tiles")

    @requires_shachen
    def test_a_window_of_six_channels_composes_the_dust_rgb_per_slot(self) -> None:
        """Every channel of a slot is warped and cached on its own; the
        producer then runs once on the slot's planes and its three guns
        are cached as frames beside them, so the next round reads
        everything back and composes nothing. A slot one channel lacks is
        left out whole, and every series carries the same axis."""
        # The class's window is the first round: every tile of both slots
        # fetched once and warped. The producer then runs on what it cached.
        self.assertEqual([(item.slot, item.tiles) for item in self.seed_window.slots], [(SLOT_0300, 20), (SLOT_0310, 20)])
        self.assertEqual(len(self.seed_downloads), 40)
        self.from_seed()
        window = self.fetch_window(channels=INFRARED, producers=(DUST,))
        self.assertEqual(self.downloads, [])
        self.assertEqual(list(window.series), ["ir039", "wv062", "ir086", "ir104", "ir112", "ir123", "dustr", "dustg", "dustb"])
        self.assertEqual(list(window.slots[0].frames), list(window.series))
        # The seed's frame cache holds the solar bands too; this window's
        # variables are a subset of it.
        self.assertTrue(set(window.series) <= {path.name for path in (self.root / "himawari-frames").iterdir()})
        gun = window.slots[0].frames["dustg"]
        self.assertEqual(gun, self.root / "himawari-frames" / "dustg" / "dustg_20260917030000.tif")
        packing = json.loads(assemble.packing_path(gun).read_text(encoding="utf-8"))
        self.assertEqual((packing["scale"], packing["offset"], packing["unit"]), (0.0001, 0.0, "1"))
        self.assertEqual(packing["producer"], {"id": "shachen", "version": DUST.version})
        self.assertEqual(packing["inputs"], [f"{channel_id}_20260917030000.tif" for channel_id in DUST.inputs])
        self.assertNotIn("ancillary", packing)
        self.assertEqual(assemble.frame_packing(gun).producer, ("shachen", DUST.version))
        # The guns are shachen's, cell for cell: the frame holds them to four
        # decimals, and the cells the disk does not cover are no data in all
        # three.
        planes = {channel.id: assemble.read_frame(window.slots[0].frames[channel.id], TILE_GRID) for channel in INFRARED}
        expected = DUST.run(HIMAWARI, planes, {}, slot=SLOT_0300, grid=TILE_GRID)
        covered = np.isfinite(planes["ir104"])
        self.assertGreater(float(covered.mean()), 0.5)
        for gun_id in DUST_RGB_COMPONENT_IDS:
            actual = assemble.read_frame(window.slots[0].frames[gun_id], TILE_GRID)
            self.assertEqual(np.isfinite(actual).tolist(), covered.tolist())
            np.testing.assert_allclose(actual[covered], expected[gun_id][covered], atol=0.00005 + 1e-12)
            self.assertGreaterEqual(float(actual[covered].min()), 0.0)
            self.assertLessEqual(float(actual[covered].max()), 1.0)
        # Composed once: the next round reads the cache and runs no producer.
        with mock.patch.object(DustRGBProducer, "run", side_effect=AssertionError("recomposed")):
            again = self.fetch_window(channels=INFRARED, producers=(DUST,))
        self.assertEqual([item.tiles for item in again.slots], [0, 0])
        for variable_id, path in window.series.items():
            self.assertTrue(filecmp.cmp(path, again.series[variable_id], shallow=False), variable_id)
        # A slot one channel lacks is left out of every series.
        partial = {k: v for k, v in self.keys.items() if not ("0310/" in k and "-M1C15-T021_" in k)}
        listing, _ = bucket(partial)
        shutil.rmtree(self.root / "himawari-frames")
        short = self.fetch_window(channels=INFRARED, producers=(DUST,), listing=listing)
        self.assertEqual([item.slot for item in short.slots], [SLOT_0300])
        series = observation.inspect_observation(self.root / "himawari.2026091703", SPEC, ("ir104", *DUST_RGB_COMPONENT_IDS))
        self.assertEqual(series.lead_seconds, [0])
        # A producer whose channels the window does not fetch is refused
        # before anything is downloaded.
        with self.assertRaisesRegex(ConversionError, "does not fetch"):
            self.fetch_window(channels=(IR104,), producers=(DUST,))

    def test_a_window_of_solar_bands_composes_the_true_colour_per_slot(self) -> None:
        """The true colour producer's day side: this pipeline's own recipe
        on the four solar bands, in its fixed order — the reflectances
        clipped at 0, divided by the cosine of the solar zenith floored at
        85°, the hybrid green, the brightness curve — held here to the
        same steps written out, and defined exactly where every band has
        data under the noon sun (the day weight is 1 there, so the
        infrared windows the night side reads change nothing). The solar
        bands' frames are block means (the bands are four and eight times
        finer than the grid), which the sidecar records."""
        self.from_seed()
        channels = tuple(HIMAWARI.channel(channel_id) for channel_id in TRUECOLOR.inputs)
        window = self.fetch_window(channels=channels, producers=(TRUECOLOR,))
        self.assertEqual(self.downloads, [])
        self.assertEqual(list(window.series), ["vis047", "vis051", "vis064", "nir086", "ir039", "ir104", "truer", "trueg", "trueb"])
        for channel in SOLAR:
            sidecar = json.loads(assemble.packing_path(window.slots[0].frames[channel.id]).read_text(encoding="utf-8"))
            self.assertEqual((sidecar["scale"], sidecar["offset"], sidecar["unit"], sidecar["resampling"]), (0.00048828125, 0.0, "1", "average"))
        infrared_frame = self.root / "himawari-frames" / "ir104" / "ir104_20260917030000.tif"
        self.assertEqual(json.loads(assemble.packing_path(infrared_frame).read_text(encoding="utf-8"))["resampling"], "bilinear")
        gun = window.slots[0].frames["trueg"]
        packing = json.loads(assemble.packing_path(gun).read_text(encoding="utf-8"))
        self.assertEqual(TRUE_COLOR_VERSION, "2")
        self.assertEqual(packing["producer"], {"id": "xue", "version": "2"})
        self.assertEqual(packing["inputs"], [f"{channel_id}_20260917030000.tif" for channel_id in TRUECOLOR.inputs])
        self.assertNotIn("ancillary", packing)
        # The recipe, step by step, against the producer's frames.
        from pyorbital.astronomy import cos_zen  # noqa: PLC0415

        planes = {channel.id: assemble.read_frame(window.slots[0].frames[channel.id], TILE_GRID) for channel in SOLAR}
        # The solar tiles are a sixteenth of the infrared ones: a corner,
        # which the 1 km and 0.5 km bands' block means edge within a cell
        # of each other; the picture is defined where all four are.
        covered = np.all([np.isfinite(plane) for plane in planes.values()], axis=0)
        for channel_id, plane in planes.items():
            self.assertLess(int(np.isfinite(plane).sum()) - int(covered.sum()), int(covered.sum()) // 50, channel_id)
        self.assertGreater(int(covered.sum()), 2000)
        self.assertLess(float(covered.mean()), 0.2)
        lons = TILE_GRID.first_longitude + np.arange(TILE_GRID.width) * TILE_GRID.step
        lats = TILE_GRID.first_latitude - np.arange(TILE_GRID.height) * TILE_GRID.step
        lon2d, lat2d = np.meshgrid(lons, lats)
        cosine = cos_zen(SLOT_0300.replace(tzinfo=None), lon2d, lat2d)
        # Noon over the western Pacific: the sun is high everywhere here,
        # well inside the day side of the terminator.
        self.assertGreater(float(cosine.min()), np.cos(np.radians(TRUE_COLOR_TERMINATOR_DEG[0])))
        divisor = np.maximum(cosine, np.cos(np.radians(TRUE_COLOR_ZENITH_LIMIT_DEG)))
        normalized = {channel_id: np.maximum(plane, 0.0) / divisor for channel_id, plane in planes.items()}
        green = TRUE_COLOR_HYBRID_GREEN[0] * normalized["vis051"] + TRUE_COLOR_HYBRID_GREEN[1] * normalized["nir086"]
        for gun_id, source in zip(TRUE_COLOR_COMPONENT_IDS, (normalized["vis064"], green, normalized["vis047"])):
            actual = assemble.read_frame(window.slots[0].frames[gun_id], TILE_GRID)
            self.assertEqual(np.isfinite(actual).tolist(), covered.tolist(), gun_id)
            expected = np.clip(np.interp(source[covered], TRUE_COLOR_CURVE_IN, TRUE_COLOR_CURVE_OUT), 0.0, 1.0)
            np.testing.assert_allclose(actual[covered], expected, atol=0.00005 + 1e-12)
            self.assertGreaterEqual(float(actual[covered].min()), 0.0)
            self.assertLessEqual(float(actual[covered].max()), 1.0)
        # A picture, not a flat field: sea and cloud both in the corner.
        red = assemble.read_frame(window.slots[0].frames["truer"], TILE_GRID)
        self.assertGreater(float(np.nanstd(red)), 0.05)
        # The green is the hybrid, not the band: it differs where the
        # near-infrared does.
        self.assertFalse(np.allclose(green[covered], normalized["vis051"][covered], atol=0.001))
        # Composed once.
        with mock.patch.object(TrueColorProducer, "run", side_effect=AssertionError("recomposed")):
            again = self.fetch_window(channels=channels, producers=(TRUECOLOR,))
        self.assertEqual([item.tiles for item in again.slots], [0, 0])
        # The ABI has no green band: its recipe reads three and blends one;
        # every imager's night side reads the same two windows.
        self.assertEqual(TRUECOLOR.inputs_for(GOES_EAST), ("vis047", "vis064", "nir086", "ir039", "ir104"))
        self.assertEqual(TRUECOLOR.inputs_for(HIMAWARI), ("vis047", "vis051", "vis064", "nir086", "ir039", "ir104"))
        # The FCI has a green band too: Meteosat reads the AHI's six.
        self.assertEqual(TRUECOLOR.inputs_for(METEOSAT), ("vis047", "vis051", "vis064", "nir086", "ir039", "ir104"))
        with self.assertRaisesRegex(ConversionError, "needs"):
            TRUECOLOR.run(GOES_EAST, {"vis047": planes["vis047"]}, {}, slot=SLOT_0300, grid=TILE_GRID)

    def true_color_planes(self, *, ir104: float, ir039: float, reflectance: float = 0.2) -> dict[str, np.ndarray]:
        """Flat input planes on the tile grid: every solar band one
        reflectance, the two windows one brightness temperature each."""
        shape = (TILE_GRID.height, TILE_GRID.width)
        planes = {channel_id: np.full(shape, reflectance) for channel_id in ("vis047", "vis051", "vis064", "nir086")}
        planes["ir104"] = np.full(shape, ir104)
        planes["ir039"] = np.full(shape, ir039)
        return planes

    def test_the_true_colour_night_side_is_white_cloud_over_a_dark_ground(self) -> None:
        """At midnight over the tile (15:00 UTC) the day weight is 0
        everywhere: the picture is the night side alone, defined wherever
        both windows are and no longer no data — the ground constant on a
        clear warm cell, a cold cloud top whiter than a warm one, low cloud
        lit by the 10.4 − 3.9 µm difference on a cell too warm for the
        infrared ramp, and the reflectances, missing or not, unread."""
        midnight = SLOT_0300 + timedelta(hours=12)
        # The fixture's own windows: every cell they cover is a value.
        self.from_seed()
        window = self.fetch_window(channels=tuple(HIMAWARI.channel(c) for c in TRUECOLOR.inputs), producers=(TRUECOLOR,))
        planes = {channel_id: assemble.read_frame(window.slots[0].frames[channel_id], TILE_GRID) for channel_id in TRUECOLOR.inputs}
        windows = np.isfinite(planes["ir104"]) & np.isfinite(planes["ir039"])
        self.assertGreater(float(windows.mean()), 0.5)
        night = TRUECOLOR.run(HIMAWARI, planes, {}, slot=midnight, grid=TILE_GRID)
        warm, cold = TRUE_COLOR_CLOUD_TEMPERATURE_K
        high = np.clip((warm - planes["ir104"]) / (warm - cold), 0.0, 1.0) ** TRUE_COLOR_CLOUD_EXPONENT
        start, end = TRUE_COLOR_LOW_CLOUD_DIFFERENCE_K
        low = TRUE_COLOR_LOW_CLOUD_WHITE * np.clip((planes["ir104"] - planes["ir039"] - start) / (end - start), 0.0, 1.0)
        whiteness = np.maximum(high, low)
        for gun_id, ground in zip(TRUE_COLOR_COMPONENT_IDS, TRUE_COLOR_NIGHT_GROUND):
            self.assertEqual(np.isfinite(night[gun_id]).tolist(), windows.tolist(), gun_id)
            np.testing.assert_allclose(night[gun_id][windows], (ground + (1.0 - ground) * whiteness)[windows], atol=1e-12)
            self.assertGreaterEqual(float(night[gun_id][windows].min()), ground - 1e-12)
        # The slot's deep convection reads white, not a flat field.
        self.assertGreater(float(np.nanstd(night["truer"])), 0.05)
        self.assertGreater(float(np.nanmax(night["truer"])), 0.75)
        # Night needs no reflectance: without them it is the same picture.
        dark = {channel_id: (np.full_like(plane, np.nan) if channel_id.startswith(("vis", "nir")) else plane) for channel_id, plane in planes.items()}
        unlit = TRUECOLOR.run(HIMAWARI, dark, {}, slot=midnight, grid=TILE_GRID)
        for gun_id in TRUE_COLOR_COMPONENT_IDS:
            np.testing.assert_array_equal(unlit[gun_id], night[gun_id])
        # A clear warm cell is the ground, opaque: never code 0.
        clear = TRUECOLOR.run(HIMAWARI, self.true_color_planes(ir104=295.0, ir039=295.0), {}, slot=midnight, grid=TILE_GRID)
        for gun_id, ground in zip(TRUE_COLOR_COMPONENT_IDS, TRUE_COLOR_NIGHT_GROUND):
            np.testing.assert_allclose(clear[gun_id], ground, atol=1e-12)
        self.assertGreater(min(TRUE_COLOR_NIGHT_GROUND), 0.0)
        # The same tint on every imager: the disks meet without a seam.
        for platform in (GOES_EAST, METEOSAT):
            inputs = {channel_id: plane for channel_id, plane in self.true_color_planes(ir104=295.0, ir039=295.0).items()}
            other = TRUECOLOR.run(platform, inputs, {}, slot=midnight, grid=TILE_GRID)
            for gun_id in TRUE_COLOR_COMPONENT_IDS:
                np.testing.assert_array_equal(other[gun_id], clear[gun_id])
        # Colder is whiter, and a cold top is white in all three guns.
        tops = {
            temperature: TRUECOLOR.run(HIMAWARI, self.true_color_planes(ir104=temperature, ir039=temperature), {}, slot=midnight, grid=TILE_GRID)
            for temperature in (270.0, 250.0, 230.0, 210.0)
        }
        reds = [float(tops[temperature]["truer"][0, 0]) for temperature in (270.0, 250.0, 230.0, 210.0)]
        self.assertEqual(reds, sorted(reds))
        self.assertLess(reds[0], reds[1])
        for gun_id in TRUE_COLOR_COMPONENT_IDS:
            np.testing.assert_allclose(tops[210.0][gun_id], 1.0, atol=1e-12)
        # Fog: as warm as the ground at 10.4 µm, 8 K colder at 3.9 µm —
        # lit to the low cloud's softer grey; 1 K is clear.
        fog = TRUECOLOR.run(HIMAWARI, self.true_color_planes(ir104=288.0, ir039=280.0), {}, slot=midnight, grid=TILE_GRID)
        haze = TRUECOLOR.run(HIMAWARI, self.true_color_planes(ir104=288.0, ir039=287.0), {}, slot=midnight, grid=TILE_GRID)
        for gun_id, ground in zip(TRUE_COLOR_COMPONENT_IDS, TRUE_COLOR_NIGHT_GROUND):
            np.testing.assert_allclose(fog[gun_id], ground + (1.0 - ground) * TRUE_COLOR_LOW_CLOUD_WHITE, atol=1e-12)
            np.testing.assert_allclose(haze[gun_id], ground, atol=1e-12)
        self.assertGreater(float(fog["truer"][0, 0]), float(clear["truer"][0, 0]))
        # A night window missing is no data at night.
        holed = dict(planes, ir039=np.where(np.arange(TILE_GRID.width) < 100, np.nan, planes["ir039"]))
        partial = TRUECOLOR.run(HIMAWARI, holed, {}, slot=midnight, grid=TILE_GRID)
        for gun_id in TRUE_COLOR_COMPONENT_IDS:
            self.assertTrue(np.all(np.isnan(partial[gun_id][:, :100])), gun_id)
            np.testing.assert_array_equal(partial[gun_id][:, 100:], night[gun_id][:, 100:])

    def test_the_true_colour_blends_across_the_terminator(self) -> None:
        """The day weight is 1 to 80° of solar zenith and 0 from 90°,
        linear and monotone between; across the band each gun is
        ``a × day + (1 − a) × night`` of the two sides, so the picture
        runs continuously from the day side into the night side. Full day
        keeps its picture without the night windows; a twilight cell
        needs both sides' inputs."""
        np.testing.assert_allclose(true_color_day_weight(np.array([0.0, 80.0, 82.5, 85.0, 90.0, 120.0, 180.0])), [1.0, 1.0, 0.75, 0.5, 0.0, 0.0, 0.0], atol=1e-12)
        sweep = true_color_day_weight(np.linspace(70.0, 100.0, 3001))
        self.assertTrue(np.all(np.diff(sweep) <= 0.0))
        self.assertLessEqual(float(np.abs(np.diff(sweep)).max()), 0.001 + 1e-12)
        self.assertEqual(TRUE_COLOR_TERMINATOR_DEG, (80.0, 90.0))
        from pyorbital.astronomy import cos_zen  # noqa: PLC0415

        lons = TILE_GRID.first_longitude + np.arange(TILE_GRID.width) * TILE_GRID.step
        lats = TILE_GRID.first_latitude - np.arange(TILE_GRID.height) * TILE_GRID.step
        lon2d, lat2d = np.meshgrid(lons, lats)

        def weight_at(slot: datetime) -> np.ndarray:
            cosine = cos_zen(slot.replace(tzinfo=None), lon2d, lat2d)
            return true_color_day_weight(np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0))))

        # The first ten-minute slot of the evening whose terminator band
        # crosses the tile.
        dusk = next(
            slot
            for slot in (SLOT_0300 + timedelta(minutes=10 * step) for step in range(1, 72))
            if float(((weight_at(slot) > 0.0) & (weight_at(slot) < 1.0)).mean()) > 0.2
        )
        weight = weight_at(dusk)
        planes = self.true_color_planes(ir104=240.0, ir039=240.0, reflectance=0.3)
        blended = TRUECOLOR.run(HIMAWARI, planes, {}, slot=dusk, grid=TILE_GRID)
        noon = TRUECOLOR.run(HIMAWARI, planes, {}, slot=SLOT_0300, grid=TILE_GRID)
        night = TRUECOLOR.run(HIMAWARI, planes, {}, slot=SLOT_0300 + timedelta(hours=12), grid=TILE_GRID)
        cosine = cos_zen(dusk.replace(tzinfo=None), lon2d, lat2d)
        divisor = np.maximum(cosine, np.cos(np.radians(TRUE_COLOR_ZENITH_LIMIT_DEG)))
        normalized = 0.3 / divisor
        green = TRUE_COLOR_HYBRID_GREEN[0] * normalized + TRUE_COLOR_HYBRID_GREEN[1] * normalized
        for gun_id, source in zip(TRUE_COLOR_COMPONENT_IDS, (normalized, green, normalized)):
            day = np.clip(np.interp(source, TRUE_COLOR_CURVE_IN, TRUE_COLOR_CURVE_OUT), 0.0, 1.0)
            expected = weight * day + (1.0 - weight) * night[gun_id]
            np.testing.assert_allclose(blended[gun_id], expected, atol=1e-12)
            self.assertTrue(np.all(np.isfinite(blended[gun_id])), gun_id)
        # Where the weight is 1 the day picture is the noon recipe's, where
        # it is 0 the night side's.
        full_day, full_night = weight >= 1.0, weight <= 0.0
        if full_night.any():
            np.testing.assert_array_equal(blended["truer"][full_night], night["truer"][full_night])
        # Continuous: neighbouring cells across the band differ by little.
        self.assertLess(float(np.abs(np.diff(blended["truer"], axis=1)).max()), 0.05)
        self.assertGreater(float(noon["truer"][0, 0]), 0.0)
        # Missing inputs: in full day the night windows are not needed, in
        # twilight both sides' are.
        no_windows = dict(planes, ir104=np.full_like(planes["ir104"], np.nan), ir039=np.full_like(planes["ir039"], np.nan))
        day_only = TRUECOLOR.run(HIMAWARI, no_windows, {}, slot=SLOT_0300, grid=TILE_GRID)
        for gun_id in TRUE_COLOR_COMPONENT_IDS:
            np.testing.assert_array_equal(day_only[gun_id], noon[gun_id])
        twilight = TRUECOLOR.run(HIMAWARI, no_windows, {}, slot=dusk, grid=TILE_GRID)
        band = (weight > 0.0) & (weight < 1.0)
        self.assertTrue(np.all(np.isnan(twilight["truer"][band])))
        if full_day.any():
            np.testing.assert_array_equal(twilight["truer"][full_day], blended["truer"][full_day])
        no_red = dict(planes, vis064=np.full_like(planes["vis064"], np.nan))
        self.assertTrue(np.all(np.isnan(TRUECOLOR.run(HIMAWARI, no_red, {}, slot=dusk, grid=TILE_GRID)["trueg"][band])))

    def test_a_frame_is_the_tiles_on_the_target_grid(self) -> None:
        self.from_seed()
        window = self.fetch_window()
        info = json.loads(subprocess.run(["gdalinfo", "-json", "-stats", str(window.slots[0].frames["ir104"])], check=True, capture_output=True, text=True).stdout)
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
        self.assertEqual([item.slot for item in window.slots], [SLOT_0300])
        self.assertEqual(len(self.downloads), 2)
        with self.assertRaisesRegex(DownloadError, "no complete ir104 slot"):
            self.fetch_window(listing=bucket({})[0], force=True)

    def test_a_frame_cached_before_sidecars_is_read_off_its_band(self) -> None:
        self.from_seed()
        window = self.fetch_window()
        frame = window.slots[0].frames["ir104"]
        sidecar = assemble.packing_path(frame)
        sidecar.unlink()
        # With the sidecar gone the band's own scale and offset stand in
        # (GDAL 3.13 carries them through the warp; the unit may be missing
        # on another version, which is what the sidecar exists for).
        packing = assemble.frame_packing(frame)
        self.assertEqual((packing.scale, packing.offset), (0.064208984375, 69.0))
        sidecar.write_text('{"scale": 1}', encoding="utf-8")
        with self.assertRaisesRegex(ConversionError, "not a packing sidecar"):
            assemble.frame_packing(frame)

    @requires_shachen
    def test_the_series_is_what_the_observation_ingest_reads(self) -> None:
        self.from_seed()
        window = self.fetch_window(channels=INFRARED, producers=(DUST,))
        wanted = ("ir104", *DUST_RGB_COMPONENT_IDS)
        # The run directory, one file per variable, resolved by id; or the
        # one file, for a source with one variable.
        series = observation.inspect_observation(self.root / "himawari.2026091703", SPEC, wanted)
        self.assertEqual(list(series.datasets), list(wanted))
        self.assertEqual(series.datasets["dustr"], observation.netcdf_dataset(window.series["dustr"], "dustr"))
        self.assertEqual(series.lead_seconds, [0, 600])
        self.assertEqual([list(frames) for frames in series.frames], [list(wanted)] * 2)
        frame = series.frames[0]["ir104"]
        self.assertEqual((frame.run_time, frame.valid_time, frame.unit), (SLOT_0300, SLOT_0300, "K"))
        self.assertEqual(series.plane_sources["ir104"].fill_replacement, 180)
        self.assertEqual(series.plane_sources["ir104"].fill_values[0], -32767.0)
        self.assertTrue(series.plane_sources["ir104"].unscale)
        # Each variable's own packing and fill: a gun's fill lands one code
        # below zero, and its producer stamp is read back.
        gun = series.plane_sources["dustg"]
        self.assertEqual((gun.fill_replacement, gun.fill_values[0], gun.unscale), (-0.004, -32767.0, True))
        self.assertAlmostEqual(gun.fill_values[1], -3.2767)
        self.assertEqual(series.producers, {gun_id: ("shachen", DUST.version) for gun_id in DUST_RGB_COMPONENT_IDS})
        self.assertEqual(series.frames[1]["dustb"].unit, "1")
        alone = observation.inspect_observation(window.series["ir104"], SPEC, ("ir104",))
        self.assertEqual((list(alone.datasets), alone.lead_seconds, alone.producers), (["ir104"], [0, 600], {}))
        self.assertEqual(observation.series_files(self.root / "himawari.2026091703", ("ir112",)), {"ir112": window.series["ir112"]})
        # A gun the series does not stamp (or stamps as another producer's)
        # is refused.
        unstamped_dir = self.root / "unstamped"
        for slot in window.slots:
            frame = slot.frames["dustr"]
            copy = unstamped_dir / "dustr" / frame.name
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(frame, copy)
            assemble.write_packing(copy, assemble.Packing(0.0001, 0.0, "1"))
        unstamped = unstamped_dir / "x.dustr.nc"
        assemble.write_series(
            [assemble.Frame(slot=slot.slot, path=unstamped_dir / "dustr" / slot.frames["dustr"].name) for slot in window.slots],
            variable=assemble.SeriesVariable(id="dustr", unit="1", long_name="unstamped"),
            platform=HIMAWARI, grid=TILE_GRID, run_time=SLOT_0300, out=unstamped,
        )
        with self.assertRaisesRegex(ConversionError, "produced by <nothing>"):
            observation.inspect_observation(unstamped, SPEC, ("dustr",))
        with self.assertRaisesRegex(ConversionError, "exactly one series file for prate"):
            observation.series_files(self.root / "himawari.2026091703", ("ir086", "prate"))
        with self.assertRaisesRegex(ConversionError, "holds 0"):
            observation.series_files(self.root, ("ir086",))

    @requires_shachen
    def test_the_source_fetch_writes_the_series_and_a_fetch_record(self) -> None:
        with mock.patch.dict(PLATFORMS, {"himawari": TWO_TILES}):
            written = _fetch_satellite_run(
                SPEC, GfsRun(SLOT_0300), 3, self.root, force=False, input_ids=None, fetch=self.listing, download=self.download
            )
        run_dir = self.root / "himawari.2026091703"
        self.assertEqual(written, [run_dir / f"himawari.2026091703.{variable_id}.nc" for variable_id in SERIES_IDS])
        record = json.loads((run_dir / "fetch.json").read_text(encoding="utf-8"))
        self.assertEqual((record["model"], record["run"], record["hours"], record["cadenceSeconds"]), ("himawari", "2026091703", 3, 600))
        self.assertEqual(record["platform"], "Himawari-9")
        self.assertEqual(record["grid"], {"step": 0.04, "width": 3000, "height": 3000, "firstLongitude": 80.72, "firstLatitude": 59.98})
        self.assertEqual(record["series"], {variable_id: path.name for variable_id, path in zip(SERIES_IDS, written)})
        self.assertEqual(
            record["producers"],
            [
                {"bundle": "dustrgb", "bundles": ["dustrgb"], "id": "shachen", "version": DUST.version, "inputs": ["ir086", "ir104", "ir112", "ir123"]},
                {"bundle": "dustcf", "bundles": ["dustcf", "zhouye"], "id": "shachen", "version": DEBRA.version, "inputs": ["ir039", "wv062", "ir086", "ir104", "ir123"]},
                {"bundle": "truecolor", "bundles": ["truecolor"], "id": "xue", "version": TRUE_COLOR_VERSION, "inputs": ["vis047", "vis051", "vis064", "nir086", "ir039", "ir104"]},
            ],
        )
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-09-17T03:00:00Z", "2026-09-17T03:10:00Z"])
        self.assertEqual([frame["tilesFetched"] for frame in record["frames"]], [20, 20])
        self.assertEqual(record["frames"][0]["frames"]["dustr"], "dustr_20260917030000.tif")
        self.assertEqual(record["frames"][0]["frames"]["dustcf"], "dustcf_20260917030000.tif")
        # The confidence's frames name the ancillary they were computed
        # against: the month's staged emissivity and the slot's GFS record.
        sidecar = json.loads(assemble.packing_path(self.root / "himawari-frames" / "dustcf" / "dustcf_20260917030000.tif").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["ancillary"], {"camel": "camel", "skin": "gfs_tmpsfc_2026091700_f003.grib2"})
        self.assertEqual(sidecar["producer"], {"id": "shachen", "version": DEBRA.version})
        summary = window_summary(run_dir)
        self.assertEqual((summary["frameCount"], summary["latestSlot"]), (2, "2026-09-17T03:10:00Z"))
        with self.assertRaisesRegex(DownloadError, "publishes"):
            _fetch_satellite_run(SPEC, GfsRun(SLOT_0300), 3, self.root, force=False, input_ids=("cref",), fetch=self.listing)
        # Asked for one channel, the fetch composes nothing and writes that
        # channel's series alone; asked for the Dust RGB's four, it composes
        # that and neither the confidence nor the true colour.
        shutil.rmtree(run_dir)
        with mock.patch.dict(PLATFORMS, {"himawari": TWO_TILES}):
            alone = _fetch_satellite_run(
                SPEC, GfsRun(SLOT_0300), 3, self.root, force=False, input_ids=("ir104",), fetch=self.listing, download=self.download
            )
        self.assertEqual(alone, [run_dir / "himawari.2026091703.ir104.nc"])
        self.assertEqual(json.loads((run_dir / "fetch.json").read_text(encoding="utf-8"))["producers"], [])
        shutil.rmtree(run_dir)
        with mock.patch.dict(PLATFORMS, {"himawari": TWO_TILES}):
            dust = _fetch_satellite_run(
                SPEC, GfsRun(SLOT_0300), 3, self.root, force=False, input_ids=DUST.inputs, fetch=self.listing, download=self.download
            )
        self.assertEqual([path.name.split(".")[-2] for path in dust], ["ir086", "ir104", "ir112", "ir123", "dustr", "dustg", "dustb"])
        self.assertEqual([entry["bundle"] for entry in json.loads((run_dir / "fetch.json").read_text(encoding="utf-8"))["producers"]], ["dustrgb"])

    def test_a_scan_segment_the_product_wrote_as_zero_kelvin_is_no_data(self) -> None:
        """ISatSS writes a segment the instrument never delivered as 0 K,
        not as its fill (tile T036 of the 13:50Z scan of 2026-09-17, four
        fifths of it missing). Left as a value, bilinear resampling smears a
        cold edge into the neighbouring cells and the codebook folds the
        rest to its bottom; the reader's lookup table makes it no data."""
        tile = TILES / "partial" / "OR_HFD-020-B12-M1C13-T036_GH9_s20262601350000_c20262601358150.nc"
        files = readers.SlotFiles(slot=datetime(2026, 9, 17, 13, 50, tzinfo=UTC), paths=(tile,))
        grid = TargetGrid(west=80.0, south=0.0, east=110.0, north=30.0, step=0.04)
        reader = ISatSSReader()
        valid: dict[str, float] = {}
        minimum: dict[str, float] = {}
        for name, floor in (("raw", None), ("masked", IR104.missing_below)):
            vrt = reader.open(files, self.root / name, missing_below=floor)
            out = self.root / name / "frame.tif"
            GdalWarpProjector().to_grid(vrt, grid, nodata=assemble.NODATA, resampling="bilinear", out=out)
            info = json.loads(
                subprocess.run(
                    ["gdalinfo", "--config", "GDAL_PAM_ENABLED", "NO", "-json", "-stats", str(out)],
                    check=True, capture_output=True, text=True,
                ).stdout
            )["bands"][0]["metadata"][""]
            valid[name] = float(info["STATISTICS_VALID_PERCENT"])
            minimum[name] = float(info["STATISTICS_MINIMUM"]) * 0.064208984375 + 69
        self.assertEqual(IR104.missing_below, 100.0)
        self.assertIsNone(HIMAWARI.channel("vis064").missing_below)
        self.assertLess(minimum["raw"], 1.0)
        self.assertGreater(minimum["masked"], 180.0)
        self.assertLess(valid["masked"], valid["raw"] / 3)
        self.assertGreater(valid["masked"], 1.0)

    def test_the_projector_refuses_a_grid_that_is_not_whole_cells(self) -> None:
        with self.assertRaisesRegex(ConversionError, "whole number"):
            TargetGrid(west=140.0, south=20.0, east=164.01, north=33.0, step=0.04)
        with self.assertRaisesRegex(ConversionError, "unsupported resampling"):
            GdalWarpProjector().to_grid(self.root / "x.vrt", TILE_GRID, nodata=-32767, resampling="lanczos", out=self.root / "x.tif")
        self.assertEqual(assemble.frame_path(self.root, IR104, SLOT_0310), self.root / "ir104" / "ir104_20260917031000.tif")


@requires_gdal_warp
@requires_shachen
class ConversionTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-himawari-convert-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        listing, download = bucket(fixture_keys())
        stage_ancillary(cls.root, (SLOT_0300, SLOT_0310))
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
                container=True, zarr=False,
            )

    def test_the_bundle_is_the_disk_grid_with_the_band_beside_the_parameter(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["ir104", "dustrgb", "dustcf", "zhouye", "truecolor"])
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

    def test_the_composite_is_three_guns_with_the_producer_beside_the_parameter(self) -> None:
        """The Dust RGB bundle: the three guns as variables 1, 2 and 3 in
        bundle order, each a local-use parameter with the producer block
        beside it (the registered id, the version the series was stamped
        with) and no band; the same axis and grid as the channel; the
        three-rung ladder and no poster or video."""
        manifest = json.loads((self.root / "out" / "manifest.json").read_text(encoding="utf-8"))
        entry = next(bundle for bundle in manifest["bundles"] if bundle["variable"] == "dustrgb")
        self.assertNotIn("poster", entry)
        self.assertNotIn("video", entry)
        self.assertEqual(len(entry["variants"]), 3)
        self.assertFalse((self.root / "out" / "dustrgb.poster.bin").exists())
        bundle = read_bundle(self.root / "out" / "dustrgb.xue")
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 1})
        self.assertEqual(bundle.metadata["grid"], read_bundle(self.root / "out" / "ir104.xue").metadata["grid"])
        variables = bundle.metadata["variables"]
        self.assertEqual([(variable["numericId"], variable["id"]) for variable in variables], [(1, "dustr"), (2, "dustg"), (3, "dustb")])
        for number, variable in enumerate(variables, start=1):
            self.assertEqual(list(variable), ["numericId", "id", "label", "unit", "parameter", "producer", "quantization"])
            self.assertEqual(variable["unit"], "1")
            parameter = variable["parameter"]
            self.assertEqual((parameter["discipline"], parameter["parameterCategory"], parameter["parameterNumber"]), (3, 192, number))
            self.assertEqual(parameter["typeOfFirstFixedSurface"], 8)
            self.assertEqual(variable["producer"], {"id": "shachen", "version": DUST.version})
            self.assertEqual(variable["quantization"], PROFILES["quality"]["dustr"].metadata())
        self.assertEqual(bundle.tiles.count, 47 * 47)
        # Code 0 is no data in every gun exactly where the channel has none,
        # and inside the disk every gun is a value in 1..251.
        ir104 = np.asarray(read_bundle(self.root / "out" / "ir104.xue").decode_plane(1, 0)).reshape(3000, 3000)
        guns = [np.asarray(bundle.decode_plane(number, 0)).reshape(3000, 3000) for number in (1, 2, 3)]
        for codes in guns:
            self.assertEqual((codes == 0).tolist() == (ir104 == 0).tolist(), True)
            inside = codes[ir104 > 0]
            self.assertGreaterEqual(int(inside.min()), 1)
            self.assertLessEqual(int(inside.max()), 251)
        # The guns are shachen's: one covered cell recomputed from the
        # channel frames the fetch cached agrees to a code.
        frames_dir = self.root / "himawari-frames"
        planes = {
            channel_id: assemble.read_frame(frames_dir / channel_id / f"{channel_id}_20260917030000.tif", satellite_grid(SPEC))
            for channel_id in DUST.inputs
        }
        expected = DUST.run(HIMAWARI, planes, {}, slot=SLOT_0300, grid=satellite_grid(SPEC))
        row, column = 800, 1800
        self.assertTrue(np.isfinite(planes["ir104"][row, column]))
        for codes, gun_id in zip(guns, DUST_RGB_COMPONENT_IDS):
            decoded = PROFILES["quality"][gun_id].decode(codes[row : row + 1, column : column + 1])[0, 0]
            self.assertAlmostEqual(decoded, float(expected[gun_id][row, column]), delta=0.0021, msg=gun_id)
        validate_bin_manifest(manifest, expected_hours=1, require_core_variables=True)

    def test_the_true_colour_is_three_guns_under_this_pipeline_s_producer(self) -> None:
        """The true colour bundle: the three guns as variables 1, 2 and 3,
        the producer's local numbers 7, 8 and 9 with the producer block
        `xue` at the recipe's version 2, the channel's axis and grid, the
        three-rung ladder; code 0 wherever the 10.4 µm channel has none
        (no picture by day or night without the disk), a value elsewhere."""
        manifest = json.loads((self.root / "out" / "manifest.json").read_text(encoding="utf-8"))
        entry = next(bundle for bundle in manifest["bundles"] if bundle["variable"] == "truecolor")
        self.assertNotIn("poster", entry)
        self.assertEqual(len(entry["variants"]), 3)
        bundle = read_bundle(self.root / "out" / "truecolor.xue")
        self.assertEqual(bundle.metadata["grid"], read_bundle(self.root / "out" / "ir104.xue").metadata["grid"])
        variables = bundle.metadata["variables"]
        self.assertEqual([(variable["numericId"], variable["id"]) for variable in variables], [(1, "truer"), (2, "trueg"), (3, "trueb")])
        for number, variable in zip((7, 8, 9), variables):
            parameter = variable["parameter"]
            self.assertEqual((parameter["discipline"], parameter["parameterCategory"], parameter["parameterNumber"], parameter["typeOfFirstFixedSurface"]), (3, 192, number, 8))
            self.assertEqual(variable["producer"], {"id": "xue", "version": "2"})
            self.assertEqual(variable["quantization"], PROFILES["quality"]["dustr"].metadata())
        ir104 = np.asarray(read_bundle(self.root / "out" / "ir104.xue").decode_plane(1, 0)).reshape(3000, 3000)
        for number in (1, 2, 3):
            codes = np.asarray(bundle.decode_plane(number, 0)).reshape(3000, 3000)
            self.assertTrue(np.all(codes[ir104 == 0] == 0))
            self.assertGreater(int((codes > 0).sum()), 0)
            self.assertLessEqual(int(codes.max()), 251)

    def test_the_confidence_is_one_variable_with_the_producer_beside_the_parameter(self) -> None:
        """The DEBRA bundle: one variable, a local-use parameter (the
        producer's number 4) with the producer block beside it and no
        band, on the channel's axis and grid, the guns' codebook, the
        three-rung ladder and no poster or video; code 0 wherever the
        channel has none and over land no emissivity is staged for, and a
        value in 1..251 elsewhere inside the disk."""
        manifest = json.loads((self.root / "out" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], ["ir104", "dustrgb", "dustcf", "zhouye", "truecolor"])
        entry = next(bundle for bundle in manifest["bundles"] if bundle["variable"] == "dustcf")
        self.assertNotIn("poster", entry)
        self.assertNotIn("video", entry)
        self.assertEqual(len(entry["variants"]), 3)
        bundle = read_bundle(self.root / "out" / "dustcf.xue")
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 1})
        self.assertEqual(bundle.metadata["grid"], read_bundle(self.root / "out" / "ir104.xue").metadata["grid"])
        (variable,) = bundle.metadata["variables"]
        self.assertEqual((variable["numericId"], variable["id"], variable["unit"]), (1, "dustcf", "1"))
        self.assertEqual(list(variable), ["numericId", "id", "label", "unit", "parameter", "producer", "quantization"])
        parameter = variable["parameter"]
        self.assertEqual((parameter["discipline"], parameter["parameterCategory"], parameter["parameterNumber"], parameter["typeOfFirstFixedSurface"]), (3, 192, 4, 8))
        self.assertEqual(variable["producer"], {"id": "shachen", "version": DEBRA.version})
        self.assertEqual(variable["quantization"], PROFILES["quality"]["dustr"].metadata())
        ir104 = np.asarray(read_bundle(self.root / "out" / "ir104.xue").decode_plane(1, 0)).reshape(3000, 3000)
        codes = np.asarray(bundle.decode_plane(1, 0)).reshape(3000, 3000)
        self.assertTrue((codes[ir104 == 0] == 0).all())
        inside = codes[ir104 > 0]
        self.assertGreater(int((inside > 0).sum()), 1000)
        self.assertLessEqual(int(inside.max()), 251)
        # The fixture's tiles are water south of Japan: nearly every covered
        # cell is defined, water needing no staged emissivity; the islands
        # (Ogasawara, Iwo) are land no staged file reaches, and no data.
        self.assertLess(float((inside == 0).mean()), 0.01)
        validate_bin_manifest(manifest, expected_hours=1, require_core_variables=True)

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

    def test_the_ladder_is_half_quarter_and_eighth_in_that_order(self) -> None:
        """A satellite source publishes three rungs (``variant_factors`` 2,
        4, 8): each bundle's ``variants`` run half, quarter, eighth — the
        manifest's ascending-factor, descending-width order — every rung
        decimated from the full plane (so the eighth is every eighth row
        and column, and equally the quarter's every second), cut with the
        source tile over its factor, and reported per bundle in the same
        order; the GFS-shaped sources still publish the half alone."""
        manifest = json.loads((self.root / "out" / "manifest.json").read_text(encoding="utf-8"))
        rungs = [("half", 2, 1500, 32), ("quarter", 4, 750, 16), ("eighth", 8, 375, 8)]
        for entry in manifest["bundles"]:
            bundle_id = entry["variable"]
            with self.subTest(bundle=bundle_id):
                self.assertEqual([variant["path"] for variant in entry["variants"]], [f"{bundle_id}.{tier}.xue" for tier, _, _, _ in rungs])
                self.assertEqual([(variant["width"], variant["height"]) for variant in entry["variants"]], [(side, side) for _, _, side, _ in rungs])
                self.assertTrue(all(variant["bandwidth"] > 0 for variant in entry["variants"]))
                self.assertGreater(entry["variants"][0]["byteLength"], entry["variants"][1]["byteLength"])
                self.assertGreater(entry["variants"][1]["byteLength"], entry["variants"][2]["byteLength"])
                full = read_bundle(self.root / "out" / f"{bundle_id}.xue")
                for tier, factor, side, tile in rungs:
                    rung = read_bundle(self.root / "out" / f"{bundle_id}.{tier}.xue")
                    grid = rung.metadata["grid"]
                    self.assertEqual((grid["width"], grid["height"]), (side, side))
                    self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.04 * factor, -0.04 * factor))
                    self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (80.72, 59.98))
                    self.assertEqual((rung.tiles.tile_width, rung.tiles.tile_height), (tile, tile))
                    # 1500 / 32, 750 / 16 and 375 / 8 are all 46.875: the
                    # same 47 x 47 tiles as the full tier, so tile n covers
                    # the same ground at every rung.
                    self.assertEqual(rung.tiles.count, full.tiles.count)
                    self.assertEqual(rung.metadata["time"], full.metadata["time"])
                    self.assertEqual(rung.metadata["variables"], full.metadata["variables"])
                    for numeric_id in full.variable_ids:
                        for offset in full.frame_offsets:
                            expected = np.asarray(full.decode_plane(numeric_id, offset)).reshape(3000, 3000)[::factor, ::factor]
                            np.testing.assert_array_equal(np.asarray(rung.decode_plane(numeric_id, offset)).reshape(side, side), expected)
        # The build report lists the rungs per bundle, ascending, in the
        # bundles' own order.
        self.assertEqual(
            [Path(variant["output"]).name for variant in self.report["variants"]],
            [f"{bundle_id}.{tier}.xue" for bundle_id in ("ir104", "dustrgb", "dustcf", "zhouye", "truecolor") for tier, _, _, _ in rungs],
        )
        self.assertEqual(sorted(path.name for path in (self.root / "out").glob("*.xue")), sorted(f"{b}{s}.xue" for b in ("ir104", "dustrgb", "dustcf", "zhouye", "truecolor") for s in ("", ".half", ".quarter", ".eighth")))
        self.assertEqual(source_spec("gfs").variant_factors, (2,))

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
                container=True, zarr=False,
            )
        self.assertEqual([bundle["variable"] for bundle in report["bundles"]], ["ir104", "dustrgb", "dustcf", "zhouye", "truecolor"])
        grid = read_bundle(self.root / "crop" / "ir104.xue").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (376, 252))
        self.assertEqual(grid["firstLongitude"], -175.0)
        self.assertFalse(grid["wrapLongitude"])
        self.assertEqual(read_bundle(self.root / "crop" / "dustrgb.xue").metadata["grid"], grid)

    def test_a_restricted_build_reads_only_the_bundles_it_was_asked_for(self) -> None:
        """``--bundles ir104`` reads the channel's series alone — the guns'
        files may as well not exist — and ``--bundles dustrgb`` the guns'
        alone, so a build never quantizes a channel nothing publishes."""
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with mock.patch.object(observation, "dataset_info", wraps=observation.dataset_info) as inspected:
                report = binconvert.convert_bin(
                    self.inputs, self.root / "only-ir", model="himawari", skip_video=True, skip_variants=True,
                    work_root=self.root / "only-ir-work", bundle_ids=("ir104",),
                    container=True, zarr=False,
                )
            self.assertEqual([bundle["variable"] for bundle in report["bundles"]], ["ir104"])
            self.assertEqual([str(call.args[0]) for call in inspected.call_args_list], [str(observation.netcdf_dataset(self.inputs / "himawari.2026091703.ir104.nc", "ir104"))])
            report = binconvert.convert_bin(
                self.inputs, self.root / "only-dust", model="himawari", skip_video=True, skip_variants=True,
                work_root=self.root / "only-dust-work", bundle_ids=("dustrgb",),
                container=True, zarr=False,
            )
        self.assertEqual([bundle["variable"] for bundle in report["bundles"]], ["dustrgb"])
        self.assertTrue(filecmp.cmp(self.root / "only-dust" / "dustrgb.xue", self.root / "out" / "dustrgb.xue", shallow=False))

    @requires_native_source("himawari", "the himawari source's bundle set")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, self.inputs, self.root / "out", self.report, self.root / "native", model="himawari", require_complete=True, expected_hours=3)


#: The Japan box: the himawari platform on a region of its own at 0.01°.
JAPAN = source_spec("himawarijp")
JAPAN_REGION = (122.0, 24.0, 148.0, 46.0)
#: Boxes inside the fixture's two tiles: one only T020 reaches into, one
#: that also needs T019 (west of the sub-satellite longitude), which the
#: fixture does not hold.
T020_ONLY = (141.0, 25.0, 148.0, 32.0)
NEEDS_T019 = (139.0, 25.0, 148.0, 32.0)


class JapanSourceTests(unittest.TestCase):
    def test_the_source_is_the_platform_on_a_box_of_its_own(self) -> None:
        self.assertTrue(JAPAN.observation and JAPAN.series_file and JAPAN.fetched and JAPAN.live)
        self.assertEqual((JAPAN.manifest_model, JAPAN.product, JAPAN.latest_filename), ("HIMAWARI-JP", "ahi-japan-0p01", "latest-himawarijp.json"))
        self.assertEqual((JAPAN.platform, JAPAN.grid_step, JAPAN.region, JAPAN.cadence_seconds, JAPAN.window_hours), ("himawari", 0.01, JAPAN_REGION, 600, 3))
        self.assertEqual((JAPAN.production_grid, JAPAN.tile, JAPAN.variant_factors), ((2600, 2200), (64, 64), (2, 4, 8)))
        # The Dust RGB's windows, the true colour's bands and night
        # windows, no confidence.
        self.assertEqual(binconvert.published_bundle_ids(JAPAN), ("ir104", "dustrgb", "truecolor"))
        self.assertEqual(binconvert.bundle_input_ids(JAPAN, "dustrgb"), ("ir086", "ir104", "ir112", "ir123"))
        self.assertEqual(binconvert.bundle_input_ids(JAPAN, "truecolor"), ("vis047", "vis051", "vis064", "nir086", "ir039", "ir104"))
        self.assertEqual(JAPAN.input_variable_ids, ("ir039", "ir086", "ir104", "ir112", "ir123", "vis047", "vis051", "vis064", "nir086"))
        self.assertEqual(set(JAPAN.input_variable_ids), {*binconvert.bundle_input_ids(JAPAN, "dustrgb"), *binconvert.bundle_input_ids(JAPAN, "truecolor")})
        # Its bands are the disk's, in the disk's order: same spacecraft,
        # same channels.
        self.assertTrue(set(JAPAN.bands) <= set(SPEC.bands))
        self.assertEqual(list(JAPAN.input_variable_ids), [channel_id for channel_id in SPEC.input_variable_ids if channel_id in JAPAN.input_variable_ids])
        # Every full-disk source takes its platform's region.
        self.assertEqual([spec.id for spec in SOURCES.values() if spec.region is not None], ["himawarijp"])

    def test_the_grid_is_the_box_at_the_step(self) -> None:
        grid = satellite_grid(JAPAN)
        self.assertEqual((grid.west, grid.south, grid.east, grid.north, grid.step), (*JAPAN_REGION, 0.01))
        self.assertEqual((grid.width, grid.height), JAPAN.production_grid)
        # Whole degrees on a 0.01° step: the JMA nowcast's 0.005° cells nest
        # in these two to one.
        self.assertEqual((grid.first_longitude, grid.first_latitude), (122.005, 45.995))
        with self.assertRaisesRegex(DownloadError, "the source's region at 0.01° is 2600 x 2300"):
            satellite_grid(dataclasses.replace(JAPAN, region=(122.0, 23.0, 148.0, 46.0)))
        # A box that is not whole cells is no grid at all.
        with self.assertRaisesRegex(ConversionError, "not a whole number"):
            satellite_grid(dataclasses.replace(JAPAN, region=(122.0, 24.0, 148.005, 46.0)))

    def test_the_catalog_prose_names_the_agency_and_the_box(self) -> None:
        prose = _source_prose(JAPAN)
        self.assertEqual(prose["providers"][0]["name"], "Japan Meteorological Agency")
        self.assertEqual(prose["providers"][1]["roles"], ["host"])
        self.assertIn("endorses", prose["description"])
        self.assertIn("24–46°N, 122–148°E", prose["description"])


class TileFootprintTests(unittest.TestCase):
    @requires_gdal_warp
    def test_the_footprint_is_the_tile_s_own_georeference(self) -> None:
        """The layout table and the scan-angle arithmetic agree with what
        the fixture tiles say about themselves: their row and column
        offsets on the disk, and the corners GDAL reads off their
        geostationary georeference."""
        for path in sorted(TILES.glob("OR_HFD-020-B12-M1C13-*_s20262600300000_*.nc")):
            parsed = parse_isatss_key(path.name)
            assert parsed is not None
            info = json.loads(
                subprocess.run(["gdalinfo", "-json", f'NETCDF:"{path}":Sectorized_CMI'], check=True, capture_output=True, text=True).stdout
            )
            metadata = info["metadata"][""]
            self.assertEqual(
                readers.isatss_tile_position(parsed.tile),
                (int(metadata["NC_GLOBAL#tile_row_offset"]) // 550, int(metadata["NC_GLOBAL#tile_column_offset"]) // 550),
            )
            corners = info["wgs84Extent"]["coordinates"][0]
            longitudes = [point[0] for point in corners]
            latitudes = [point[1] for point in corners]
            footprint = readers.isatss_tile_footprint(HIMAWARI, parsed.tile)
            assert footprint is not None
            np.testing.assert_allclose(footprint, (min(longitudes), min(latitudes), max(longitudes), max(latitudes)), atol=0.01)

    def test_the_layout_is_eighty_eight_tiles_without_the_corners(self) -> None:
        self.assertEqual(sum(count for _, count in readers.ISATSS_TILE_ROWS), HIMAWARI.tile_count)
        self.assertEqual(readers.isatss_tile_position(1), (0, 2))
        self.assertEqual(readers.isatss_tile_position(20), (2, 5))
        self.assertEqual(readers.isatss_tile_position(88), (9, 7))
        with self.assertRaises(ValueError):
            readers.isatss_tile_position(89)

    def test_a_region_needs_only_the_tiles_reaching_into_it(self) -> None:
        reader = ISatSSReader()
        # Japan: six tiles of 88, two rows north of the sub-satellite point,
        # three columns from 106°E to 156°E.
        self.assertEqual(reader.tiles_covering(HIMAWARI, JAPAN_REGION), frozenset({9, 10, 11, 18, 19, 20}))
        self.assertEqual(reader.tiles_covering(HIMAWARI, T020_ONLY), frozenset({20}))
        self.assertEqual(reader.tiles_covering(HIMAWARI, NEEDS_T019), frozenset({19, 20}))
        # The whole disk reaches every tile; a box spelled on negative
        # longitudes finds the eastern limb past the antimeridian.
        self.assertEqual(reader.tiles_covering(HIMAWARI, HIMAWARI.region), frozenset(range(1, 89)))
        self.assertEqual(reader.tiles_covering(HIMAWARI, (-170.0, -10.0, -160.0, 0.0)), frozenset({44, 54}))
        # A single-file product has nothing to narrow.
        self.assertIsNone(readers.CMIPFReader().tiles_covering(GOES_EAST, (-100.0, 20.0, -80.0, 40.0)))

    def test_a_slot_is_complete_when_the_region_s_tiles_are_there(self) -> None:
        listing, _ = bucket(fixture_keys())
        objects = ISatSSReader().list_slot(HIMAWARI, IR104, SLOT_0300, fetch=listing)
        self.assertEqual([item.tile for item in objects], [20, 21])
        # Two tiles of 88 are no disk, but all a box inside T020 needs.
        self.assertFalse(satellite_fetch.slot_is_complete(HIMAWARI, objects))
        self.assertTrue(satellite_fetch.slot_is_complete(HIMAWARI, objects, region=T020_ONLY))
        self.assertFalse(satellite_fetch.slot_is_complete(HIMAWARI, objects, region=NEEDS_T019))
        self.assertEqual([item.tile for item in satellite_fetch.covering_objects(HIMAWARI, objects, region=T020_ONLY)], [20])
        self.assertEqual(satellite_fetch.covering_objects(HIMAWARI, objects), objects)


class JapanFetchTests(TempRoot, unittest.TestCase):
    root_prefix = "xue-himawarijp-"

    def setUp(self) -> None:
        super().setUp()
        self.listing, download = bucket(fixture_keys())
        self.downloads: list[str] = []

        def counting(url: str) -> bytes:
            self.downloads.append(url)
            return download(url)

        self.download = counting

    def fetch_window(self, region: tuple[float, float, float, float]):
        return satellite_fetch.fetch_window(
            HIMAWARI,
            (IR104,),
            SLOT_0300,
            1,
            grid=TargetGrid(*region, step=0.04),
            raw_root=self.root,
            destination=self.root / "himawarijp.2026091703",
            series_stem="himawarijp.2026091703",
            units={"ir104": "K"},
            region=region,
            source_id="himawarijp",
            fetch=self.listing,
            download=self.download,
        )

    @requires_gdal_warp
    def test_a_regional_window_fetches_its_tiles_alone_and_caches_apart(self) -> None:
        window = self.fetch_window(T020_ONLY)
        self.assertEqual([(item.slot, item.tiles) for item in window.slots], [(SLOT_0300, 1), (SLOT_0310, 1)])
        self.assertEqual(len(self.downloads), 2)
        self.assertTrue(all("-T020_" in url for url in self.downloads))
        # The frames are the source's, not the platform's: the disk caches
        # the same slot on another grid under himawari-frames.
        self.assertEqual(sorted(path.name for path in self.root.iterdir() if path.name.endswith("-frames")), ["himawarijp-frames"])
        frame = window.slots[0].frames["ir104"]
        self.assertEqual(frame.parent.parent, self.root / "himawarijp-frames")
        self.assertEqual(json.loads(assemble.packing_path(frame).read_text(encoding="utf-8"))["tiles"], 1)
        info = json.loads(subprocess.run(["gdalinfo", "-json", str(frame)], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(info["size"], [175, 175])
        self.assertEqual(info["geoTransform"][:2], [141.0, 0.04])

    def test_a_region_whose_tiles_have_not_all_landed_is_no_slot(self) -> None:
        with self.assertRaisesRegex(DownloadError, "no complete ir104 slot"):
            self.fetch_window(NEEDS_T019)
        self.assertEqual(self.downloads, [])


@requires_shachen
class JapanConversionTests(ClassTempRoot, unittest.TestCase):
    """The Japan source end to end on the fixture: of the six tiles its box
    needs the fixture holds T020 alone, so the reader is told that is all
    the box needs; the frames are warped onto the whole 0.01° grid, the
    Dust RGB and the true colour composed per slot, and the window built through both encoders."""

    root_prefix = "xue-himawarijp-convert-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        listing, download = bucket(fixture_keys())
        with mock.patch.object(ISatSSReader, "tiles_covering", lambda self, platform, region: frozenset({20})):
            cls.written = _fetch_satellite_run(JAPAN, GfsRun(SLOT_0300), 3, cls.root, force=False, input_ids=None, fetch=listing, download=download)
        cls.inputs = cls.root / "himawarijp.2026091703"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                cls.inputs,
                cls.root / "out",
                model="himawarijp",
                skip_video=True,
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
                require_complete=True,
                expected_hours=3,
                container=True, zarr=False,
            )

    def test_the_bundles_are_the_box_with_the_disk_s_band(self) -> None:
        self.assertEqual([path.name.split(".")[-2] for path in self.written], [*JAPAN.input_variable_ids, *DUST_RGB_COMPONENT_IDS, *TRUE_COLOR_COMPONENT_IDS])
        record = json.loads((self.inputs / "fetch.json").read_text(encoding="utf-8"))
        self.assertEqual(record["grid"], {"step": 0.01, "width": 2600, "height": 2200, "firstLongitude": 122.005, "firstLatitude": 45.995})
        self.assertEqual([frame["tilesFetched"] for frame in record["frames"]], [9, 9])
        self.assertTrue((self.root / "himawarijp-frames" / "dustr").is_dir())
        self.assertFalse((self.root / "himawari-frames").exists())
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["ir104", "dustrgb", "truecolor"])
        manifest = json.loads((self.root / "out" / "manifest.json").read_text(encoding="utf-8"))
        validate_bin_manifest(manifest, expected_hours=1, require_core_variables=True)
        self.assertEqual((manifest["model"], manifest["product"]), ("HIMAWARI-JP", "ahi-japan-0p01"))
        bundle = read_bundle(self.root / "out" / "ir104.xue")
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"], grid["firstLongitude"], grid["firstLatitude"]), (2600, 2200, 122.005, 45.995))
        self.assertEqual(bundle.metadata["variables"][0]["band"], HIMAWARI.band("ir104").metadata())
        self.assertEqual(bundle.tiles.count, 41 * 35)

    @requires_native_source("himawarijp")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, self.inputs, self.root / "out", self.report, self.root / "native", model="himawarijp", require_complete=True, expected_hours=3)


if __name__ == "__main__":
    unittest.main()

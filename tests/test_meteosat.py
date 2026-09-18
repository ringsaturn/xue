"""The Meteosat source: EUMETSAT's MTG FCI Level 1c product through the
satellite stage, hourly.

The shape is tests/test_goes.py's, with three things of its own. The
files come from the EUMETSAT Data Store rather than a bucket, so the
stand-in here is the store's OpenSearch (a trimmed real response,
tests/fixtures/meteosat/search.json, and products written from it) and a
token-bearing download. The fixture is one real chunk file (chunk 20 of a
full-disk cycle from EUMETSAT's public MTG test data, the spectrally
representative FDHSI cycle of May 2022 — simulated 2017 radiances in the
operational format, rows 2645–2784 of the 5568-row grid, the strip 0 to
2.9°S across the disk), which stands for every chunk of every slot: the
reader's chunk selection is narrowed to it, since forty chunks of a real
cycle are 900 MB. And the source publishes the hour's cycle alone, so the
window's slots are hourly while the store lists one every ten minutes.
"""

from __future__ import annotations

import filecmp
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np

from tests.test_satellite import requires_gdal
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
from xuebuild.satellite import HIMAWARI, METEOSAT, METEOSAT_IODC, PLATFORMS, assemble, eumetsat
from xuebuild.satellite import fetch as satellite_fetch
from xuebuild.satellite.producers import PRODUCERS
from xuebuild.satellite.projector import TargetGrid
from xuebuild.satellite.readers import (
    FCI_BT_SCALE,
    FCI_GROUPS,
    FCIReader,
    SlotFiles,
    SlotObject,
    brightness_temperature,
    elevation_angle,
    parse_fci_chunk,
    reader_for,
)
from xuebuild.sources import SOURCES, SatelliteBand, source_spec
from xuebuild.stac import _source_prose
from xuebuild.variables import DUST_RGB_BUNDLE_ID, DUST_RGB_COMPONENT_IDS

FIXTURES = Path(__file__).parent / "fixtures" / "meteosat"
CHUNK = next(iter(sorted(FIXTURES.glob("*.nc"))))
SPEC = source_spec("meteosat")
IR104 = METEOSAT.channel("ir104")
CHANNELS = tuple(METEOSAT.channel(channel_id) for channel_id in SPEC.input_variable_ids)
DUST = PRODUCERS[DUST_RGB_BUNDLE_ID]
DAY = datetime(2026, 9, 17, tzinfo=UTC)
#: The stand-in store's cycles: the hourly ones the source takes, and the
#: ten-minute ones between that it must pass over.
CYCLES = tuple(DAY + timedelta(hours=12) + timedelta(minutes=minutes) for minutes in (0, 10, 20, 60, 70))
SLOT_1200 = CYCLES[0]
SLOT_1300 = CYCLES[3]
#: The fixture strip's footprint at the published step, for the tests
#: that warp: 0–3°S across 10°W–10°E.
TILE_GRID = TargetGrid(west=-10.0, south=-4.0, east=10.0, north=1.0, step=0.04)

requires_hdf5plugin = unittest.skipUnless(
    __import__("importlib").util.find_spec("hdf5plugin") is not None, "hdf5plugin is not installed (uv sync --group satellite)"
)


def product_id(start: datetime) -> str:
    end = start + timedelta(minutes=9, seconds=28)
    return (
        "W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--x-x---x_C_EUMT_"
        f"{start + timedelta(minutes=13):%Y%m%d%H%M%S}_IDPFI_OPE_{start:%Y%m%d%H%M%S}_{end:%Y%m%d%H%M%S}_N__O_0093_0000"
    )


def chunk_name(start: datetime, chunk: int) -> str:
    """A chunk file's name as the store lists it for a cycle."""
    created = start + timedelta(minutes=4, seconds=chunk)
    chunk_start = start + timedelta(seconds=14 * chunk)
    return (
        "W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--CHK-BODY---NC4E_C_EUMT_"
        f"{created:%Y%m%d%H%M%S}_IDPFI_OPE_{chunk_start:%Y%m%d%H%M%S}_{chunk_start + timedelta(seconds=11):%Y%m%d%H%M%S}"
        f"_N__O_0093_{chunk:04d}.nc"
    )


def entry_href(start: datetime, name: str) -> str:
    return (
        "https://api.eumetsat.int/data/download/1.0.0/collections/EO%3AEUM%3ADAT%3A0662/products/"
        f"{urllib.parse.quote(product_id(start), safe='')}/entry?name={urllib.parse.quote(name, safe='')}"
    )


def product_feature(start: datetime, *, chunks: int = 40, published: datetime | None = None) -> dict[str, object]:
    """One product of the collection in the store's OpenSearch shape."""
    end = start + timedelta(minutes=9, seconds=28)
    updated = published or end + timedelta(minutes=5)
    entries = [{"type": "Link", "href": entry_href(start, chunk_name(start, chunk)), "mediaType": "application/x-netcdf", "title": chunk_name(start, chunk)} for chunk in range(1, chunks + 1)]
    trail = chunk_name(start, 40).replace("CHK-BODY", "CHK-TRAIL").replace("_0040.nc", "_0041.nc")
    entries.append({"type": "Link", "href": entry_href(start, trail), "mediaType": "application/x-netcdf", "title": trail})
    entries.append({"type": "Link", "href": entry_href(start, "EOPMetadata.xml"), "mediaType": "application/xml", "title": "EOPMetadata.xml"})
    return {
        "type": "Feature",
        "id": product_id(start),
        "properties": {
            "identifier": product_id(start),
            "parentIdentifier": "EO:EUM:DAT:0662",
            "date": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
            "updated": f"{updated:%Y-%m-%dT%H:%M:%S.000Z}",
            "links": {"sip-entries": entries},
        },
    }


def store(features: list[dict[str, object]]) -> Callable[[str], str]:
    """A stand-in for the Data Store's search: the products sensed in the
    query's interval, one page."""

    def fetch(url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        assert parsed.netloc == "api.eumetsat.int" and parsed.path == "/data/search-products/1.0.0/os", url
        query = urllib.parse.parse_qs(parsed.query)
        assert query["pi"] == ["EO:EUM:DAT:0662"], query
        start = datetime.strptime(query["dtstart"][0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        end = datetime.strptime(query["dtend"][0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        hits = []
        for feature in features:
            sensed = datetime.strptime(str(feature["properties"]["date"]).split("/")[0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)  # type: ignore[index]
            if start <= sensed <= end:
                hits.append(feature)
        hits.sort(key=lambda feature: str(feature["properties"]["date"]), reverse=True)  # type: ignore[index]
        return json.dumps({"type": "FeatureCollection", "totalResults": len(hits), "itemsPerPage": 100, "startIndex": 0, "features": hits})

    return fetch


def counting(listing: Callable[[str], str]) -> tuple[Callable[[str], str], list[str]]:
    asked: list[str] = []

    def fetch(url: str) -> str:
        asked.append(url)
        return listing(url)

    return fetch, asked


def chunk_download(downloads: list[str] | None = None) -> Callable[[str], bytes]:
    """A download that answers every chunk-20 link with the fixture chunk
    and refuses any other file: the reader must ask for nothing else."""

    def download(url: str) -> bytes:
        if downloads is not None:
            downloads.append(url)
        name = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["name"][0])
        parsed = parse_fci_chunk(name)
        if parsed is None or parsed.chunk != 20:
            raise AssertionError(f"the reader asked for {name}")
        return CHUNK.read_bytes()

    return download


class RegistryTests(unittest.TestCase):
    def test_the_source_is_the_himawari_one_hourly_on_its_own_disk(self) -> None:
        himawari = source_spec("himawari")
        self.assertTrue(SPEC.observation and SPEC.series_file and SPEC.fetched and SPEC.live)
        self.assertEqual((SPEC.platform, SPEC.manifest_model, SPEC.latest_filename, SPEC.product), ("meteosat", "METEOSAT", "latest-meteosat.json", "fci-fldk-0p04"))
        # FCI has no 11.2 µm window: three channels feed the composite.
        self.assertEqual(SPEC.input_variable_ids, ("ir086", "ir104", "ir123"))
        self.assertEqual((SPEC.bundle_scalar_ids, SPEC.bundle_composite_ids, SPEC.core_bundle_ids), (himawari.bundle_scalar_ids, himawari.bundle_composite_ids, himawari.core_bundle_ids))
        self.assertEqual(binconvert.published_bundle_ids(SPEC), ("ir104", "dustrgb"))
        self.assertEqual(binconvert.bundle_input_ids(SPEC, "dustrgb"), ("ir086", "ir104", "ir123"))
        self.assertEqual(binconvert.bundle_input_ids(himawari, "dustrgb"), ("ir086", "ir104", "ir112", "ir123"))
        # The cycle on the hour alone, a day of them.
        self.assertEqual((SPEC.grid_step, SPEC.cadence_seconds, SPEC.window_hours, SPEC.production_grid, SPEC.tile), (0.04, 3600, 24, (3000, 3000), (64, 64)))
        self.assertEqual(METEOSAT.cadence_seconds, 600)
        self.assertFalse(SPEC.video)
        self.assertEqual([spec.id for spec in SOURCES.values() if spec.platform], ["himawari", "goeseast", "goeswest", "meteosat"])

    def test_the_bands_are_the_fci_s(self) -> None:
        self.assertEqual([band_id for band_id, _ in SPEC.bands], ["ir086", "ir104", "ir123"])
        bands = dict(SPEC.bands)
        self.assertEqual(bands["ir104"], SatelliteBand(satellite_series=0, satellite_number=71, instrument_type=210, central_wavenumber=95238))
        self.assertEqual([bands[band_id].central_wavenumber for band_id in ("ir086", "ir104", "ir123")], [114943, 95238, 81301])
        self.assertNotEqual(bands["ir104"], dict(source_spec("himawari").bands)["ir104"])

    def test_the_platforms_are_registered_with_their_wmo_numbers(self) -> None:
        self.assertEqual(sorted(PLATFORMS), ["goeseast", "goeswest", "himawari", "meteosat", "meteosatiodc"])
        self.assertEqual((METEOSAT.spacecraft, METEOSAT.satellite_number, METEOSAT.instrument, METEOSAT.instrument_type), ("Meteosat-12", 71, "FCI", 210))
        self.assertEqual((METEOSAT.reader, METEOSAT.bucket, METEOSAT.prefix, METEOSAT.tile_count, METEOSAT.sweep_axis), ("fci", "api.eumetsat.int", "EO:EUM:DAT:0662", 40, "y"))
        self.assertEqual((METEOSAT_IODC.spacecraft, METEOSAT_IODC.satellite_number, METEOSAT_IODC.instrument, METEOSAT_IODC.instrument_type, METEOSAT_IODC.sub_longitude), ("Meteosat-9", 56, "SEVIRI", 207, 45.5))
        self.assertIsInstance(reader_for(METEOSAT), FCIReader)
        with self.assertRaisesRegex(DownloadError, "no reader is implemented for 'seviri'"):
            reader_for(METEOSAT_IODC)
        # The FCI channel table maps the instrument's groups to the shared ids.
        self.assertEqual([channel.band for channel in CHANNELS], [12, 14, 15])
        self.assertEqual([FCI_GROUPS[channel.band - 1] for channel in CHANNELS], ["ir_87", "ir_105", "ir_123"])
        self.assertEqual(len(METEOSAT.channels), 16)
        self.assertEqual([channel.id for channel in METEOSAT.channels if channel.kind == "bt"], ["ir039", "wv062", "wv073", "ir086", "ir096", "ir104", "ir123", "ir133"])
        self.assertNotIn("ir112", {channel.id for channel in METEOSAT.channels})

    def test_the_disk_is_centred_on_the_prime_meridian(self) -> None:
        self.assertEqual(METEOSAT.region, (-60.0, -60.0, 60.0, 60.0))
        grid = satellite_grid(SPEC)
        self.assertEqual((grid.width, grid.height, grid.first_longitude, grid.first_latitude), (3000, 3000, -59.98, 59.98))

    def test_the_producer_reads_three_windows_on_fci(self) -> None:
        self.assertEqual(DUST.inputs_for(METEOSAT), ("ir086", "ir104", "ir123"))
        self.assertEqual(DUST.inputs_for(HIMAWARI), ("ir086", "ir104", "ir112", "ir123"))
        self.assertEqual(DUST.inputs_for(METEOSAT_IODC), ("ir086", "ir104", "ir123"))

    def test_the_catalog_prose_names_eumetsat_the_licence_and_the_hourly_rule(self) -> None:
        prose = _source_prose(SPEC)
        self.assertEqual(prose["title"], "Meteosat-12 FCI full disk, hourly")
        self.assertEqual(prose["license"], "CC-BY-4.0")
        self.assertEqual(prose["providers"][0]["name"], "EUMETSAT")
        self.assertEqual(prose["providers"][1]["name"], "shachen")
        self.assertIn("Contains modified EUMETSAT Meteosat data", prose["description"])
        self.assertIn("on each hour", prose["description"])
        self.assertEqual([link["rel"] for link in prose["links"]], ["license", "cite-as", "describedby"])


class ClientTests(unittest.TestCase):
    def test_a_search_is_one_anonymous_query_per_page(self) -> None:
        url = eumetsat.search_url("EO:EUM:DAT:0662", SLOT_1200, SLOT_1300)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual((query["pi"], query["dtstart"], query["dtend"], query["format"]), (["EO:EUM:DAT:0662"], ["2026-09-17T12:00:00Z"], ["2026-09-17T13:00:00Z"], ["json"]))

    def test_the_store_s_own_response_parses(self) -> None:
        products, total = eumetsat.parse_products((FIXTURES / "search.json").read_text(encoding="utf-8"), "EO:EUM:DAT:0662")
        self.assertEqual((total, len(products)), (2, 2))
        cycle = products[0]
        self.assertEqual(cycle.start, datetime(2026, 9, 17, 15, 20, 7, tzinfo=UTC))
        self.assertEqual(cycle.end, datetime(2026, 9, 17, 15, 29, 35, tzinfo=UTC))
        self.assertEqual(cycle.updated, datetime(2026, 9, 17, 15, 34, 41, 435000, tzinfo=UTC))
        self.assertTrue(cycle.id.startswith("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--x-x---x_C_EUMT_20260917152320"))
        self.assertEqual(len(cycle.entries), 61)
        chunks = [parse_fci_chunk(entry.name) for entry in cycle.entries]
        self.assertEqual(sorted(parsed.chunk for parsed in chunks if parsed is not None), list(range(1, 41)))
        self.assertTrue(all(entry.href.startswith("https://api.eumetsat.int/data/download/1.0.0/collections/EO%3AEUM%3ADAT%3A0662/products/") for entry in cycle.entries))
        # The other product came first in the store's page and sorts by
        # sensing start here.
        self.assertEqual(products[1].start, datetime(2026, 9, 17, 15, 10, 7, tzinfo=UTC))
        self.assertEqual(eumetsat.search("EO:EUM:DAT:0662", SLOT_1200, SLOT_1300, fetch=lambda url: (FIXTURES / "search.json").read_text(encoding="utf-8"))[0].start, products[1].start)

    def test_credentials_come_from_the_environment_or_the_error_names_them(self) -> None:
        self.assertEqual(eumetsat.credentials({"EUMETSAT_CONSUMER_KEY": "k", "EUMETSAT_CONSUMER_SECRET": "s"}), ("k", "s"))
        self.assertFalse(eumetsat.has_credentials({}))
        with self.assertRaisesRegex(DownloadError, "EUMETSAT_CONSUMER_KEY and EUMETSAT_CONSUMER_SECRET"):
            eumetsat.credentials({"EUMETSAT_CONSUMER_KEY": "k"})
        with mock.patch.dict(os.environ, {"EUMETSAT_CONSUMER_KEY": "", "EUMETSAT_CONSUMER_SECRET": ""}):
            with self.assertRaisesRegex(DownloadError, "registered account"):
                eumetsat.download_bytes("https://api.eumetsat.int/data/download/x", tokens=eumetsat.TokenCache())

    def test_a_token_is_minted_once_renewed_when_refused_and_a_rate_limit_waited_out(self) -> None:
        calls: list[tuple[str, dict[str, str]]] = []
        answers: list[object] = []

        class Response(io.BytesIO):
            status = 200

        def opener(request, timeout):  # noqa: ANN001
            calls.append((request.full_url, dict(request.header_items())))
            if request.full_url == eumetsat.TOKEN_URL:
                self.assertEqual(request.get_header("Authorization"), "Basic azpz")
                self.assertEqual(request.data, b"grant_type=client_credentials")
                return Response(json.dumps({"access_token": f"tok{sum(1 for url, _ in calls if url == eumetsat.TOKEN_URL)}", "expires_in": 3600}).encode())
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return Response(answer)

        waits: list[float] = []
        tokens = eumetsat.TokenCache()
        retry_at = int((datetime.now(UTC) + timedelta(seconds=7)).timestamp() * 1000)
        answers[:] = [
            urllib.error.HTTPError("u", 401, "unauthorized", {}, io.BytesIO(b"")),
            urllib.error.HTTPError("u", 429, "slow down", {}, io.BytesIO(json.dumps({"message": {"retryAfter": retry_at}}).encode())),
            b"chunk bytes",
        ]
        with mock.patch.dict(os.environ, {"EUMETSAT_CONSUMER_KEY": "k", "EUMETSAT_CONSUMER_SECRET": "s"}):
            body = eumetsat.download_bytes("https://api.eumetsat.int/data/download/x", opener=opener, tokens=tokens, sleep=waits.append)
        self.assertEqual(body, b"chunk bytes")
        # Two tokens: the first refused, the second carried through the
        # rate limit and the success.
        self.assertEqual([url for url, _ in calls].count(eumetsat.TOKEN_URL), 2)
        bearers = [headers.get("Authorization") for url, headers in calls if url != eumetsat.TOKEN_URL]
        self.assertEqual(bearers, ["Bearer tok1", "Bearer tok2", "Bearer tok2"])
        self.assertEqual(len(waits), 1)
        self.assertGreater(waits[0], 3.0)
        self.assertLessEqual(waits[0], 7.0)
        # The token is reused while it lives.
        answers[:] = [b"more"]
        with mock.patch.dict(os.environ, {"EUMETSAT_CONSUMER_KEY": "k", "EUMETSAT_CONSUMER_SECRET": "s"}):
            eumetsat.download_bytes("https://api.eumetsat.int/data/download/y", opener=opener, tokens=tokens, sleep=waits.append)
        self.assertEqual([url for url, _ in calls].count(eumetsat.TOKEN_URL), 2)
        self.assertIsNone(eumetsat.retry_after_seconds(b"not json"))
        self.assertEqual(eumetsat.retry_after_seconds(json.dumps({"message": {"retryAfter": 0}}).encode()), 0.0)


class KeyTests(unittest.TestCase):
    def test_a_chunk_name_parses_to_its_fields(self) -> None:
        parsed = parse_fci_chunk("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--CHK-BODY---NC4E_C_EUMT_20260917152435_IDPFI_OPE_20260917152007_20260917152018_N__O_0093_0001.nc")
        assert parsed is not None
        self.assertEqual((parsed.cycle, parsed.chunk), (93, 1))
        self.assertEqual(parsed.start, datetime(2026, 9, 17, 15, 20, 7, tzinfo=UTC))
        self.assertEqual(parsed.end, datetime(2026, 9, 17, 15, 20, 18, tzinfo=UTC))
        self.assertEqual(parsed.created, datetime(2026, 9, 17, 15, 24, 35, tzinfo=UTC))
        fixture = parse_fci_chunk(CHUNK.name)
        assert fixture is not None
        self.assertEqual((fixture.cycle, fixture.chunk), (1, 20))
        self.assertEqual(fixture.start, datetime(2017, 9, 20, 0, 4, 26, tzinfo=UTC))
        # The trailer, the metadata and a product id are not chunks.
        self.assertIsNone(parse_fci_chunk("W_XX-EUMETSAT-Darmstadt,IMG+SAT,MTI1+FCI-1C-RRAD-FDHSI-FD--CHK-TRAIL---NC4E_C_EUMT_20260917153100_IDPFI_OPE_20260917152007_20260917152935_N__O_0093_0041.nc"))
        self.assertIsNone(parse_fci_chunk("EOPMetadata.xml"))
        self.assertIsNone(parse_fci_chunk(product_id(SLOT_1200)))

    def test_a_cycle_s_slot_is_its_start_floored_to_the_cadence(self) -> None:
        reader = FCIReader()
        self.assertEqual(reader.slot_of(METEOSAT, datetime(2026, 9, 17, 15, 20, 7, tzinfo=UTC)), datetime(2026, 9, 17, 15, 20, tzinfo=UTC))
        self.assertEqual(reader.slot_of(METEOSAT, datetime(2026, 9, 17, 15, 29, 59, tzinfo=UTC)), datetime(2026, 9, 17, 15, 20, tzinfo=UTC))
        self.assertTrue(satellite_fetch.on_cadence(SLOT_1200, 3600))
        self.assertFalse(satellite_fetch.on_cadence(CYCLES[1], 3600))
        self.assertEqual(satellite_fetch.window_slots(METEOSAT, SLOT_1200, 2, cadence_seconds=3600), [SLOT_1200, SLOT_1300, SLOT_1300 + timedelta(hours=1)])
        self.assertEqual(len(satellite_fetch.window_slots(METEOSAT, SLOT_1200, 1)), 7)
        with self.assertRaisesRegex(ConversionError, "cannot publish it every 900 s"):
            satellite_fetch.window_slots(METEOSAT, SLOT_1200, 1, cadence_seconds=900)


class GeometryTests(unittest.TestCase):
    def test_the_needed_chunks_are_the_rows_inside_sixty_degrees(self) -> None:
        # 60° of latitude on the sub-satellite meridian is seen 0.1402 rad
        # up from the disk's centre (the disk's edge is 0.1556); the rows
        # past it are the outermost two chunks at either end.
        angle = elevation_angle(60.0, height=35786400.0, semi_major=6378137.0, semi_minor=6356752.314245)
        self.assertAlmostEqual(angle, 0.14023, places=4)
        self.assertAlmostEqual(elevation_angle(0.0, height=35786400.0, semi_major=6378137.0, semi_minor=6356752.314245), 0.0)
        self.assertEqual(FCIReader().needed_chunks(METEOSAT), set(range(2, 40)))

    def test_the_planck_conversion_is_the_product_s(self) -> None:
        # The ir_105 coefficients of the fixture, on a mid-range radiance.
        planck = {"constant_c1": 1.19104279e-05, "constant_c2": 1.43877518, "coefficient_a": 0.999000013, "coefficient_b": 0.364399999, "coefficient_wavenumber": 949.973022}
        temperature = brightness_temperature(np.array([95.0986]), planck)
        effective = 1.43877518 * 949.973022 / np.log1p(1.19104279e-05 * 949.973022**3 / 95.0986)
        self.assertAlmostEqual(float(temperature[0]), (effective - 0.364399999) / 0.999000013, places=9)
        self.assertGreater(float(temperature[0]), 291.0)
        self.assertLess(float(temperature[0]), 292.0)


class ListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.features = [product_feature(start) for start in CYCLES]
        self.listing = store(self.features)
        self.reader = FCIReader()

    def test_the_day_s_cycles_and_a_slot_s_chunks(self) -> None:
        self.assertEqual(self.reader.list_slots(METEOSAT, DAY, fetch=self.listing), list(CYCLES))
        self.assertEqual(self.reader.list_slots(METEOSAT, DAY - timedelta(days=1), fetch=self.listing), [])
        objects = self.reader.list_slot(METEOSAT, IR104, SLOT_1200, fetch=self.listing)
        self.assertEqual([item.tile for item in objects], list(range(1, 41)))
        self.assertTrue(all(item.url.startswith("https://api.eumetsat.int/data/download/") and item.key.endswith(".nc") for item in objects))
        self.assertTrue(satellite_fetch.slot_is_complete(METEOSAT, objects))
        # Every channel is in every chunk: the listing is the same.
        self.assertEqual(self.reader.list_slot(METEOSAT, METEOSAT.channel("ir086"), SLOT_1200, fetch=self.listing), objects)
        self.assertEqual(self.reader.list_slot(METEOSAT, IR104, SLOT_1200 + timedelta(minutes=30), fetch=self.listing), [])
        self.assertFalse(satellite_fetch.slot_is_complete(METEOSAT, self.reader.list_slot(METEOSAT, IR104, SLOT_1200, fetch=store([product_feature(SLOT_1200, chunks=39)]))))

    def test_a_reissued_product_wins(self) -> None:
        later = product_feature(SLOT_1200, published=SLOT_1200 + timedelta(hours=2))
        later["properties"]["identifier"] = product_id(SLOT_1200) + "_REISSUE"  # type: ignore[index]
        listing = store(self.features + [later])
        products = self.reader.products(METEOSAT, SLOT_1200, SLOT_1200 + timedelta(minutes=9), fetch=listing)
        self.assertEqual(products[SLOT_1200].id, product_id(SLOT_1200) + "_REISSUE")

    def test_the_newest_slots_are_one_search(self) -> None:
        now = SLOT_1300 + timedelta(minutes=25)
        fetch, asked = counting(self.listing)
        self.assertEqual(self.reader.recent_slots(METEOSAT, now, limit=3, fetch=fetch), [CYCLES[4], CYCLES[3], CYCLES[2]])
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.reader.recent_slots(METEOSAT, SLOT_1200 + timedelta(minutes=15), limit=3, fetch=self.listing), [CYCLES[1], CYCLES[0]])

    def test_the_source_takes_the_hourly_cycle_alone(self) -> None:
        now = SLOT_1300 + timedelta(minutes=25)
        fetch, asked = counting(self.listing)
        # The store's newest cycle is 13:10; the source's newest is 13:00.
        self.assertEqual(satellite_fetch.latest_slot(METEOSAT, IR104, now=now, fetch=fetch), CYCLES[4])
        self.assertEqual(satellite_fetch.latest_slot(METEOSAT, IR104, now=now, fetch=fetch, cadence_seconds=3600), SLOT_1300)
        self.assertLessEqual(len(asked), 4)
        self.assertEqual(latest_satellite_slot(SPEC, now=now, fetch=self.listing), SLOT_1300)
        self.assertEqual(latest_satellite_slot(SPEC, now=SLOT_1300 - timedelta(minutes=1), fetch=self.listing), SLOT_1200)
        with mock.patch("xuebuild.satellite.eumetsat._fetch_text", self.listing):
            self.assertEqual(latest_observation_slot(SPEC, now=now), SLOT_1300)
            run = resolve_run("latest", hours=3, now=now, model="meteosat")
            # Two whole hours and the one in progress; a day the same way.
            self.assertEqual(run.id, "2026091711")
            self.assertEqual(resolve_run("latest", hours=SPEC.window_hours, now=now, model="meteosat").id, "2026091614")
            self.assertTrue(_satellite_run_is_complete(SPEC, GfsRun(SLOT_1200), 1, now=now))
            self.assertFalse(_satellite_run_is_complete(SPEC, GfsRun(SLOT_1200), 2, now=now))
        with self.assertRaisesRegex(DownloadError, "Meteosat-12 lists no complete ir104 slot"):
            satellite_fetch.latest_slot(METEOSAT, IR104, now=DAY + timedelta(days=3), fetch=self.listing, cadence_seconds=3600)

    def test_a_download_takes_the_needed_chunks_only_and_wants_them_all_listed(self) -> None:
        objects = self.reader.list_slot(METEOSAT, IR104, SLOT_1200, fetch=self.listing)
        with self.assertRaisesRegex(DownloadError, r"lists no chunk \[2, 3"):
            self.reader.download(METEOSAT, objects[:1] + objects[39:], Path(tempfile.mkdtemp(prefix="xue-fci-")), download=lambda url: b"x")
        with self.assertRaisesRegex(DownloadError, "nothing to download"):
            self.reader.download(METEOSAT, [], Path(tempfile.mkdtemp(prefix="xue-fci-")), download=lambda url: b"x")


@requires_gdal
@requires_hdf5plugin
class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-meteosat-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.listing = store([product_feature(start) for start in CYCLES])
        self.downloads: list[str] = []
        self.download = chunk_download(self.downloads)
        # The fixture is chunk 20; the reader's selection is narrowed to it.
        patcher = mock.patch.object(FCIReader, "needed_chunks", lambda self, platform: {20})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fetch_window(self, *, force: bool = False, channels: tuple = (IR104,), producers: tuple = (), grid: TargetGrid = TILE_GRID, hours: int = 1):
        return satellite_fetch.fetch_window(
            METEOSAT,
            channels,
            SLOT_1200,
            hours,
            grid=grid,
            raw_root=self.root,
            destination=self.root / "meteosat.2026091712",
            series_stem="meteosat.2026091712",
            units={channel.id: "K" for channel in channels},
            producers=producers,
            cadence_seconds=3600,
            force=force,
            fetch=self.listing,
            download=self.download,
        )

    def test_a_slot_is_its_chunk_read_once_for_every_channel(self) -> None:
        window = self.fetch_window(channels=CHANNELS)
        self.assertEqual([(item.slot, item.tiles) for item in window.slots], [(SLOT_1200, 120), (SLOT_1300, 120)])
        # One download per slot serves the three channels.
        self.assertEqual(len(self.downloads), 2)
        self.assertEqual(sorted(window.series), ["ir086", "ir104", "ir123"])
        frames = sorted(path.name for path in (self.root / "meteosat-frames" / "ir104").iterdir())
        self.assertEqual(frames, ["ir104_20260917120000.json", "ir104_20260917120000.tif", "ir104_20260917130000.json", "ir104_20260917130000.tif"])
        packing = json.loads((self.root / "meteosat-frames" / "ir104" / "ir104_20260917120000.json").read_text())
        self.assertEqual((packing["scale"], packing["offset"], packing["unit"], packing["tiles"], len(packing["keys"])), (FCI_BT_SCALE, 100.0, "K", 40, 40))
        self.assertFalse((self.root / "meteosat.2026091712" / "tiles").exists())
        again = self.fetch_window(channels=CHANNELS)
        self.assertEqual([item.tiles for item in again.slots], [0, 0])
        self.assertEqual(len(self.downloads), 2)

    def test_a_frame_is_the_strip_in_kelvin_on_the_target_grid(self) -> None:
        window = self.fetch_window()
        frame = window.slots[0].frames["ir104"]
        info = json.loads(subprocess.run(["gdalinfo", "-json", "-stats", str(frame)], check=True, capture_output=True, text=True).stdout)
        self.assertEqual(info["size"], [500, 125])
        self.assertEqual(info["geoTransform"][:2], [-10.0, 0.04])
        band = info["bands"][0]
        self.assertEqual(band["noDataValue"], -32767.0)
        plane = assemble.read_frame(frame, TILE_GRID)
        covered = np.isfinite(plane)
        # The chunk is rows 2645–2784: a strip of constant scan angle from
        # the equator down to 2.5°S near the sub-satellite meridian (2.9°S
        # out at the disk's edge), across the whole width; north of it and
        # south of it nothing.
        rows = np.where(covered.any(axis=1))[0]
        self.assertEqual((int(rows.min()), int(rows.max())), (25, 88))
        self.assertTrue(covered[30:85, :].all())
        self.assertFalse(covered[:24, :].any())
        self.assertFalse(covered[90:, :].any())
        self.assertGreater(float(np.nanmin(plane)), 200.0)
        self.assertLess(float(np.nanmax(plane)), 340.0)
        # Read back through GDAL's own unscaling, the same numbers.
        unscaled = subprocess.run(["gdal_translate", "-q", "-of", "ENVI", "-ot", "Float64", "-unscale", str(frame), str(self.root / "unscaled.bin")], check=True)
        self.assertEqual(unscaled.returncode, 0)
        values = np.fromfile(self.root / "unscaled.bin", dtype="<f8").reshape(125, 500)
        np.testing.assert_allclose(values[covered], plane[covered], atol=1e-9)

    def test_the_dust_rgb_reads_the_ten_micron_window_for_green(self) -> None:
        from shachen.constants import DUST_RGB  # noqa: PLC0415
        from shachen.dustrgb import dust_rgb  # noqa: PLC0415
        import xarray as xr  # noqa: PLC0415

        window = self.fetch_window(channels=CHANNELS, producers=(DUST,))
        self.assertEqual(list(window.series), ["ir086", "ir104", "ir123", "dustr", "dustg", "dustb"])
        planes = {channel.id: assemble.read_frame(window.slots[0].frames[channel.id], TILE_GRID) for channel in CHANNELS}
        scene = xr.Dataset({name: xr.DataArray(planes[channel_id], dims=("y", "x")) for name, channel_id in (("bt_tir_86", "ir086"), ("bt_tir_104", "ir104"), ("bt_tir_112", "ir104"), ("bt_tir_123", "ir123"))})
        expected = np.asarray(dust_rgb(scene, DUST_RGB).values)
        covered = np.isfinite(planes["ir104"])
        self.assertGreater(int(covered.sum()), 30000)
        for index, gun_id in enumerate(DUST_RGB_COMPONENT_IDS):
            actual = assemble.read_frame(window.slots[0].frames[gun_id], TILE_GRID)
            self.assertEqual(np.isfinite(actual).tolist(), covered.tolist())
            np.testing.assert_allclose(actual[covered], expected[..., index][covered], atol=0.00005 + 1e-12)
        # The three windows differ, so the green gun is not flat.
        self.assertGreater(float(np.nanstd(assemble.read_frame(window.slots[0].frames["dustg"], TILE_GRID))), 0.01)
        gun = window.slots[0].frames["dustr"]
        self.assertEqual(json.loads(assemble.packing_path(gun).read_text(encoding="utf-8"))["producer"], {"id": "shachen", "version": DUST.version})

    def test_the_source_fetch_writes_hourly_series_and_a_fetch_record(self) -> None:
        written = _fetch_satellite_run(SPEC, GfsRun(SLOT_1200), 1, self.root, force=False, input_ids=None, fetch=self.listing, download=self.download)
        run_dir = self.root / "meteosat.2026091712"
        self.assertEqual(written, [run_dir / f"meteosat.2026091712.{variable_id}.nc" for variable_id in ("ir086", "ir104", "ir123", "dustr", "dustg", "dustb")])
        record = json.loads((run_dir / "fetch.json").read_text(encoding="utf-8"))
        self.assertEqual((record["model"], record["run"], record["hours"], record["cadenceSeconds"]), ("meteosat", "2026091712", 1, 3600))
        self.assertEqual(record["platform"], "Meteosat-12")
        self.assertEqual(record["grid"], {"step": 0.04, "width": 3000, "height": 3000, "firstLongitude": -59.98, "firstLatitude": 59.98})
        self.assertEqual([frame["slot"] for frame in record["frames"]], ["2026-09-17T12:00:00Z", "2026-09-17T13:00:00Z"])
        self.assertEqual(record["producers"][0]["inputs"], ["ir086", "ir104", "ir123"])
        summary = window_summary(run_dir)
        self.assertEqual((summary["frameCount"], summary["latestSlot"]), (2, "2026-09-17T13:00:00Z"))
        series = observation.inspect_observation(run_dir, SPEC, ("ir104", *DUST_RGB_COMPONENT_IDS))
        self.assertEqual(series.lead_seconds, [0, 3600])
        self.assertEqual(series.producers["dustr"], ("shachen", DUST.version))

    def test_without_credentials_the_download_names_them(self) -> None:
        listing = self.listing
        with mock.patch.dict(os.environ, {"EUMETSAT_CONSUMER_KEY": "", "EUMETSAT_CONSUMER_SECRET": ""}):
            with self.assertRaisesRegex(DownloadError, "EUMETSAT_CONSUMER_KEY"):
                satellite_fetch.fetch_frame(
                    METEOSAT, IR104, SLOT_1200, grid=TILE_GRID, frames_dir=self.root / "frames", tiles_dir=self.root / "tiles", fetch=listing
                )


@requires_gdal
@requires_hdf5plugin
class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-meteosat-convert-"))
        cls.patcher = mock.patch.object(FCIReader, "needed_chunks", lambda self, platform: {20})
        cls.patcher.start()
        _fetch_satellite_run(SPEC, GfsRun(SLOT_1200), 1, cls.root, force=False, input_ids=None, fetch=store([product_feature(start) for start in CYCLES]), download=chunk_download())
        cls.inputs = cls.root / "meteosat.2026091712"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                cls.inputs,
                cls.root / "out",
                model="meteosat",
                skip_video=True,
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
                require_complete=True,
                expected_hours=1,
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.patcher.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_the_bundles_are_hourly_with_the_fci_band(self) -> None:
        self.assertEqual([bundle["variable"] for bundle in self.report["bundles"]], ["ir104", "dustrgb"])
        manifest = json.loads((self.root / "out" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((manifest["model"], manifest["product"]), ("METEOSAT", "fci-fldk-0p04"))
        self.assertEqual(manifest["runTime"], "2026-09-17T12:00:00Z")
        validate_bin_manifest(manifest, expected_hours=1, require_core_variables=True)
        bundle = read_bundle(self.root / "out" / "ir104.xue")
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 1})
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"], grid["firstLongitude"], grid["firstLatitude"]), (3000, 3000, -59.98, 59.98))
        variable = bundle.metadata["variables"][0]
        self.assertEqual(variable["band"], METEOSAT.band("ir104").metadata())
        self.assertEqual((variable["band"]["satelliteNumber"], variable["band"]["instrumentType"]), (71, 210))
        composite = read_bundle(self.root / "out" / "dustrgb.xue")
        self.assertEqual([(v["numericId"], v["id"]) for v in composite.metadata["variables"]], [(1, "dustr"), (2, "dustg"), (3, "dustb")])
        self.assertEqual(composite.metadata["variables"][0]["producer"], {"id": "shachen", "version": DUST.version})
        self.assertEqual(composite.metadata["time"], bundle.metadata["time"])

    def test_the_planes_are_the_strip_in_kelvin(self) -> None:
        bundle = read_bundle(self.root / "out" / "ir104.xue")
        codes = np.asarray(bundle.decode_plane(1, 0)).reshape(3000, 3000)
        # The strip is the equator to 2.5°S at the sub-satellite meridian
        # (2.9°S at the disk's edge): rows 1500–1572 of the disk grid across
        # every column, with the test cycle's own five per cent of missing
        # pixels; everything else is the fill.
        covered = codes[1505:1560, :]
        self.assertGreater(float((covered > 0).mean()), 0.9)
        self.assertEqual(int(codes[:1495].max()), 0)
        self.assertEqual(int(codes[1580:].max()), 0)
        kelvin = PROFILES["quality"]["ir104"].decode(covered)
        self.assertGreater(float(kelvin.min()), 200.0)
        self.assertLess(float(kelvin.max()), 340.0)
        guns = [np.asarray(read_bundle(self.root / "out" / "dustrgb.xue").decode_plane(number, 0)).reshape(3000, 3000) for number in (1, 2, 3)]
        for gun in guns:
            self.assertEqual((gun == 0).tolist() == (codes == 0).tolist(), True)
            self.assertGreaterEqual(int(gun[codes > 0].min()), 1)
            self.assertLessEqual(int(gun[codes > 0].max()), 251)

    @unittest.skipUnless(native.knows_source("meteosat"), f"the installed {native.DISTRIBUTION} wheel predates the meteosat source")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI")
        subject = self.root / "native"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            report = native.convert_bin(
                self.inputs, subject, model="meteosat", skip_video=True, manifest_path=subject / "manifest.json", require_complete=True, expected_hours=1
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

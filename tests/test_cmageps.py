"""The cmageps source: CMA GRAPES GEPS, the China Meteorological
Administration's global ensemble, from the WMO WIS2 global caches.

CMA publishes no members, only ensemble products: one GRIB2 file per lead
time with the mean and spread (template 4.2), the 0/25/50/75/100th
percentiles (4.6, and 4.10 over an interval) and exceedance probabilities,
JPEG2000 packed and with no index. The fetch (``xuebuild/cmageps.py``)
walks each file's message heads over Range, takes the ensemble's centre of
every input — the mean where CMA writes one, the median elsewhere, the
precipitation over the six hours ending at the step — and rewrites each
record to the plain template the registry's identities match (4.2 and 4.6
to 4.0, 4.10 to 4.8); the frame is then repacked to grid_simple. The
instantaneous 10 m gust is published on the ground-surface identity GFS's
instantaneous gust carries. Nothing is new to the encoders: the
precipitation total is the registry's ``apcp`` (an interval total, divided
by the interval as Open-Meteo's is), every other record an existing
identity.

``tests/fixtures/cmageps.2026111012.f000.crop.grib2`` and ``f006`` are an
80 x 80 cell window from 130°E to 149.75°E and 49.875°N to 30.125°N (Japan
and the Pacific east of it) of the analysis and the first six-hourly step
of the 2026-11-10 12Z cycle, every record the fetcher writes in source
order: five at the analysis (the precipitation, the gust and the cloud
cover are not read there) and eight at the step.
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests._support import FIXTURES, ParityCase, ReferenceBuild, assert_gdalinfo_agrees, requires_gdalinfo, store_path, stored_bundle
from xuebuild import cmageps, grib2, quantize
from xuebuild.assemble import group_needs_eccodes
from xuebuild.discover import analysis_optional_ids, published_bundle_ids
from xuebuild.errors import DownloadError
from xuebuild.feeds import FrameFeed, fetcher
from xuebuild.idx import ByteRange
from xuebuild.model import GfsRun
from xuebuild.sources import source_spec
from xuebuild.stac import _PROSE
from xuebuild.wis2 import CacheObject

FIXTURE_FRAMES = [FIXTURES / "cmageps.2026111012.f000.crop.grib2", FIXTURES / "cmageps.2026111012.f006.crop.grib2"]
SPEC = source_spec("cmageps")
RUN = GfsRun(datetime(2026, 11, 10, 12, tzinfo=UTC))
INPUTS = ("hgt500", "prmsl", "apcp", "tmp2m", "tmp850", "gust", "cape", "tcdc")
BUNDLES = ("tmp2m", "prate", "prmsl", "gust", "tcdc", "cape", "tmp850", "hgt500")

#: The reference build every class here reads (tests/_support.py).
REFERENCE = ReferenceBuild("cmageps", "2026111012", FIXTURE_FRAMES)


def tearDownModule() -> None:
    REFERENCE.cleanup()


# -- A product file's heads, written independently of the module -----------

MISSING_LEVEL = (0xFF, 0xFFFFFFFF)


def _message(
    template: int,
    triple: tuple[int, int, int],
    level: tuple[int, int, int],
    forecast_hours: int,
    extra: bytes = b"",
    payload: int = 64,
) -> bytes:
    """One GRIB2 message whose sections 0, 1 and 4 are real and whose data
    is ``payload`` bytes of padding in a section 7: the head is all the
    walk reads."""
    discipline, category, number = triple
    level_type, scale, scaled = level
    section1 = struct.pack(">IBHHBBBHBBBBBBB", 21, 1, 38, 0, 4, 0, 1, 2026, 11, 10, 12, 0, 0, 0, 3)
    body = struct.pack(
        ">HHBBBBBHBBiBBIBBI",
        0,
        template,
        category,
        number,
        4,
        128,
        128,
        0,
        0,
        1,
        forecast_hours,
        level_type,
        scale,
        scaled,
        255,
        0xFF,
        0xFFFFFFFF,
    )
    section4 = struct.pack(">IB", 5 + len(body) + len(extra), 4) + body + extra
    section7 = struct.pack(">IB", 5 + payload, 7) + b"\0" * payload
    total = 16 + len(section1) + len(section4) + len(section7) + 4
    return b"GRIB\0\0" + bytes([discipline, 2]) + struct.pack(">Q", total) + section1 + section4 + section7 + b"7777"


def _mean(triple, level, hour, derived=0):
    return _message(2, triple, level, hour, bytes([derived, 31]))


def _percentile(triple, level, hour, percentile):
    return _message(6, triple, level, hour, bytes([percentile]))


def _interval(triple, level, start, end, percentile, statistical=1):
    ending = RUN.time + timedelta(hours=end)
    extra = (
        bytes([percentile])
        + struct.pack(">HBBBBB", ending.year, ending.month, ending.day, ending.hour, 0, 0)
        + struct.pack(">BIBBBIBI", 1, 0, statistical, 2, 1, end - start, 255, 0)
    )
    return _message(10, triple, level, start, extra)


def _probability(triple, level, hour):
    return _message(5, triple, level, hour, bytes(13))


SURFACE = (1,) + MISSING_LEVEL
TEN_METRES = (103, 0, 10)

#: A +24 h file: every kind of record the real one carries, the centre of
#: each input among them, in the real file's order.
PRODUCTS_AT_24 = [
    ("hgt500", _mean((0, 3, 5), (100, 0, 50000), 24)),
    (None, _mean((0, 3, 5), (100, 0, 50000), 24, derived=4)),
    ("prmsl", _mean((0, 3, 1), (101,) + MISSING_LEVEL, 24)),
    (None, _mean((0, 3, 1), (101,) + MISSING_LEVEL, 24, derived=4)),
    (None, _interval((0, 1, 8), SURFACE, 0, 24, 50)),
    (None, _probability((0, 2, 22), TEN_METRES, 24)),
    (None, _interval((0, 1, 8), SURFACE, 18, 24, 25)),
    ("apcp", _interval((0, 1, 8), SURFACE, 18, 24, 50)),
    (None, _interval((0, 1, 29), SURFACE, 18, 24, 50)),
    (None, _percentile((0, 0, 0), (103, 0, 2), 24, 25)),
    ("tmp2m", _percentile((0, 0, 0), (103, 0, 2), 24, 50)),
    ("tmp850", _percentile((0, 0, 0), (100, 0, 85000), 24, 50)),
    (None, _percentile((0, 2, 1), (103, 0, 10), 24, 50)),
    ("gust", _percentile((0, 2, 22), TEN_METRES, 24, 50)),
    ("cape", _percentile((0, 7, 6), SURFACE, 24, 50)),
    ("tcdc", _percentile((0, 6, 1), SURFACE, 24, 50)),
    (None, _percentile((0, 2, 25), (100, 0, 25000), 24, 50)),
]


def _file(records):
    body = b"".join(message for _, message in records)
    offsets, offset = {}, 0
    for variable_id, message in records:
        if variable_id is not None:
            offsets[variable_id] = offset
        offset += len(message)
    return body, offsets


class Reader:
    """Range reads of in-memory files, recorded."""

    def __init__(self, files: dict[str, bytes], broken: tuple[str, ...] = ()) -> None:
        self.files = files
        self.broken = broken
        self.asked: list[tuple[str, int, int]] = []

    def __call__(self, url: str, byte_range: ByteRange) -> bytes:
        self.asked.append((url, byte_range.start, byte_range.end))
        if url in self.broken:
            raise DownloadError(f"{url}: 503")
        return self.files[url][byte_range.start : byte_range.end + 1]


class SourceRegistryTests(unittest.TestCase):
    def test_the_central_set(self) -> None:
        self.assertEqual(published_bundle_ids(SPEC), BUNDLES)
        self.assertEqual(SPEC.input_variable_ids, INPUTS)
        self.assertEqual((SPEC.manifest_model, SPEC.product, SPEC.latest_filename), ("CMA-GEPS", "cma-geps-0p25", "latest-cmageps.json"))
        self.assertTrue(SPEC.interval_precipitation)
        self.assertFalse(SPEC.accumulated_precipitation)
        # The analysis carries no precipitation, and its gust and cloud
        # cover are fields of zeros: none of the three is read there.
        self.assertEqual(SPEC.optional_at_analysis, ("apcp", "gust", "tcdc"))
        self.assertEqual(analysis_optional_ids(SPEC, published_bundle_ids(SPEC)), ("prate", "gust", "tcdc"))
        self.assertEqual(SPEC.statistical_processes, (("prate", 0),))

    def test_the_axis_is_six_hourly_to_360_from_two_cycles(self) -> None:
        self.assertEqual(SPEC.steps, ((360, 6),))
        self.assertEqual(SPEC.cycle_hours, 12)
        hours = SPEC.forecast_hours(360)
        self.assertEqual(hours, list(range(0, 361, 6)))
        self.assertEqual(len(hours), 61)

    def test_the_grid_has_no_pole_rows(self) -> None:
        self.assertEqual(SPEC.production_grid, (1440, 720))
        self.assertEqual(SPEC.tile, (48, 48))
        self.assertEqual(SPEC.variant_factors, (2,))

    def test_the_feed_rewrites_and_repacks_every_frame(self) -> None:
        self.assertEqual(SPEC.packing, "jpeg2000")
        self.assertIsInstance(fetcher(SPEC), FrameFeed)
        self.assertTrue(group_needs_eccodes(SPEC, ("tmp2m",)))

    def test_the_prose_names_the_ensemble_centre(self) -> None:
        prose = _PROSE["sources"]["cmageps"]
        self.assertIn("China Meteorological Administration", prose["description"])
        self.assertIn("median", prose["description"])
        self.assertIn("mean", prose["description"])


class CacheTests(unittest.TestCase):
    def test_the_file_names(self) -> None:
        name = cmageps.file_name(RUN, 24)
        self.assertEqual(name, "Z_NAFP_C_BABJ_20261110120000_P_CMA-WIPPSGEPS-GLB-024.grib2")
        self.assertEqual(cmageps.parse_file_name(name), (RUN.time, 24))
        self.assertEqual(cmageps.parse_file_name(cmageps.file_name(RUN, 360)), (RUN.time, 360))
        # A partial upload's numeric suffix stands where the extension would.
        self.assertIsNone(cmageps.parse_file_name(name.replace(".grib2", ".29509")))
        self.assertTrue(cmageps.object_url(RUN, 6).endswith("/medium-range/probabilistic/global/" + cmageps.file_name(RUN, 6)))

    def test_a_run_is_complete_once_the_two_listings_together_name_every_step(self) -> None:
        noaa, jma = cmageps.CACHES[:2]

        def key(cache, hour: int) -> str:
            name = cmageps.file_name(RUN, hour)
            return f"{cache.prefix}{name}" if cache is noaa else f"{cache.prefix}{name[:-6]}/1794279663/{name}"

        held = {noaa.name: [hour for hour in range(0, 361, 6) if hour != 120], jma.name: [120, 126]}

        def listing(url: str) -> str:
            cache = next(cache for cache in cmageps.CACHES if url.startswith(cache.base_url + "/?"))
            contents = "".join(
                f"<Contents><Key>{key(cache, hour)}</Key><Size>80000000</Size></Contents>" for hour in held[cache.name]
            )
            stray = f"<Contents><Key>{key(cache, 30)[:-6]}.29509</Key><Size>5</Size></Contents>"
            return (
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"{contents}{stray}<IsTruncated>false</IsTruncated></ListBucketResult>"
            )

        objects = cmageps.list_run(RUN, fetch=listing)
        self.assertEqual([item.cache for item in objects[126]], ["noaa", "jma"])
        self.assertEqual([item.cache for item in objects[120]], ["jma"])
        self.assertTrue(cmageps.run_is_complete(SPEC, RUN, 360, fetch=listing))
        held[jma.name] = [126]
        self.assertFalse(cmageps.run_is_complete(SPEC, RUN, 360, fetch=listing))
        self.assertTrue(cmageps.run_is_complete(SPEC, RUN, 114, fetch=listing))
        # Only the listable caches are listed; CMA's own is read by name.
        asked: list[str] = []
        cmageps.list_run(RUN, fetch=lambda url: asked.append(url) or listing(url))
        self.assertEqual(len(asked), 2)

    def test_a_file_is_tried_on_the_listed_caches_then_by_name_on_cma_s(self) -> None:
        listed = {24: [CacheObject("noaa", "noaa-url", 10), CacheObject("jma", "jma-url", 10)]}
        found = cmageps.candidates(RUN, 24, listed)
        self.assertEqual([item.cache for item in found], ["noaa", "jma", "cma", "cma"])
        name = cmageps.file_name(RUN, 24)
        # Under the day the file arrived: the cycle's, or the next.
        self.assertEqual(
            [item.url for item in found[2:]],
            [
                f"{cmageps.CMA_CACHE_URL}/{day}/cache/a/wis2/cn-cma/data/core/weather/prediction/forecast/"
                f"medium-range/probabilistic/global/{name}"
                for day in ("20261110", "20261111")
            ],
        )
        self.assertIsNone(found[2].size)
        self.assertEqual([item.cache for item in cmageps.candidates(RUN, 30, listed)], ["cma", "cma"])


class WalkTests(unittest.TestCase):
    def test_the_heads_name_each_product(self) -> None:
        message = _interval((0, 1, 8), SURFACE, 18, 24, 50)
        length, record = cmageps.parse_head(message[: cmageps.HEAD_BYTES], 0, "file")
        self.assertEqual(length, len(message))
        self.assertEqual((record.template, record.percentile, record.statistical, record.interval_hours), (10, 50, 1, 6))
        self.assertEqual(record.forecast_hours, 18)
        self.assertEqual(record.interval_end, RUN.time + timedelta(hours=24))
        self.assertIsNone(record.level_value)
        _, mean = cmageps.parse_head(_mean((0, 3, 5), (100, 0, 50000), 24, derived=4), 0, "file")
        self.assertEqual((mean.template, mean.derived, mean.level_value), (2, 4, 50000.0))
        with self.assertRaises(cmageps.HeadTruncated):
            cmageps.parse_head(message[:40], 0, "file")
        with self.assertRaises(DownloadError):
            cmageps.parse_head(b"GRIB\0\0\0\1" + message[8:], 0, "file")

    def test_the_walk_takes_the_centre_of_each_input_and_stops(self) -> None:
        body, offsets = _file(PRODUCTS_AT_24)
        read = Reader({"file": body})
        found = cmageps.walk("file", len(body), 24, 18, INPUTS, read)
        self.assertEqual({variable_id: record.offset for variable_id, record in found.items()}, offsets)
        # One head per message up to the last input's, none after it.
        self.assertEqual(len(read.asked), len(PRODUCTS_AT_24) - 1)
        # The six-hour window is the step's; the run-long one is not.
        self.assertEqual(found["apcp"].interval_hours, 6)
        with self.assertRaises(DownloadError):
            cmageps.walk("file", len(body), 24, 12, INPUTS, Reader({"file": body}))

    def test_a_file_of_unknown_size_is_walked_the_same(self) -> None:
        body, offsets = _file(PRODUCTS_AT_24)
        found = cmageps.walk("file", None, 24, 18, INPUTS, Reader({"file": body}))
        self.assertEqual({variable_id: record.offset for variable_id, record in found.items()}, offsets)
        # A cache's "not found" page, served whole as a 200, is no GRIB2.
        page = b"<html><body>Page Not Found</body></html>" + b" " * 600
        with self.assertRaises(DownloadError):
            cmageps.walk("page", None, 24, 18, INPUTS, Reader({"page": page}))

    def test_two_centres_of_one_input_are_refused(self) -> None:
        body, _ = _file(PRODUCTS_AT_24 + [("tmp2m", _percentile((0, 0, 0), (103, 0, 2), 24, 50))])
        with self.assertRaises(DownloadError):
            cmageps.walk("file", len(body), 24, 18, INPUTS + ("vis",), Reader({"file": body}))

    def test_the_rewrite_keys(self) -> None:
        body, offsets = _file(PRODUCTS_AT_24)
        found = cmageps.walk("file", len(body), 24, 18, INPUTS, Reader({"file": body}))
        keys = {variable_id: cmageps.rewrite_keys(record) for variable_id, record in found.items()}
        self.assertEqual(keys["hgt500"], {"productDefinitionTemplateNumber": 0})
        self.assertEqual(keys["tmp2m"], {"productDefinitionTemplateNumber": 0})
        ground = {"scaleFactorOfFirstFixedSurface": 0, "scaledValueOfFirstFixedSurface": 0}
        self.assertEqual(keys["apcp"], {"productDefinitionTemplateNumber": 8, **ground})
        self.assertEqual(keys["cape"], {"productDefinitionTemplateNumber": 0, **ground})
        self.assertEqual(keys["gust"], {"productDefinitionTemplateNumber": 0, "typeOfFirstFixedSurface": 1, **ground})

    def test_a_frame_comes_from_the_first_cache_that_serves_it(self) -> None:
        body, _ = _file(PRODUCTS_AT_24)
        read = Reader({"noaa-url": body, "jma-url": body}, broken=("noaa-url",))
        rewritten: list[dict[str, int]] = []

        def rewrite(source: Path, destination: Path, keys: dict[str, int]) -> None:
            rewritten.append(keys)
            destination.write_bytes(source.read_bytes())

        candidates = [CacheObject("noaa", "noaa-url", len(body)), CacheObject("jma", "jma-url", len(body))]
        payload = cmageps.frame_records(SPEC, RUN, 24, INPUTS, candidates, read, rewrite)
        expected = dict(PRODUCTS_AT_24)
        self.assertEqual(payload, b"".join(expected[variable_id] for variable_id in INPUTS))
        self.assertEqual(len(rewritten), len(INPUTS))
        with self.assertRaises(DownloadError):
            cmageps.frame_records(SPEC, RUN, 24, INPUTS, candidates[:1], read, rewrite)


@unittest.skipUnless(shutil.which("grib_set"), "eccodes CLI")
class RewriteTests(unittest.TestCase):
    """The fixture's records turned back into the products CMA writes, then
    walked and rewritten with eccodes: the header index finds every input
    in the rewritten frame, as it does in the fixture."""

    PRODUCT_KEYS = {
        "hgt500": "productDefinitionTemplateNumber=2,derivedForecast=0,numberOfForecastsInEnsemble=31",
        "prmsl": "productDefinitionTemplateNumber=2,derivedForecast=0,numberOfForecastsInEnsemble=31",
        "apcp": "productDefinitionTemplateNumber=10,percentileValue=50,"
        "scaleFactorOfFirstFixedSurface=missing,scaledValueOfFirstFixedSurface=missing",
        "tmp2m": "productDefinitionTemplateNumber=6,percentileValue=50",
        "tmp850": "productDefinitionTemplateNumber=6,percentileValue=50",
        "gust": "productDefinitionTemplateNumber=6,percentileValue=50,typeOfFirstFixedSurface=103,"
        "scaleFactorOfFirstFixedSurface=0,scaledValueOfFirstFixedSurface=10",
        "cape": "productDefinitionTemplateNumber=6,percentileValue=50,"
        "scaleFactorOfFirstFixedSurface=missing,scaledValueOfFirstFixedSurface=missing",
        "tcdc": "productDefinitionTemplateNumber=6,percentileValue=50,"
        "scaleFactorOfFirstFixedSurface=missing,scaledValueOfFirstFixedSurface=missing",
    }

    def test_the_products_become_the_fixture_s_records(self) -> None:
        from xuebuild.eccodescli import set_keys

        scratch = Path(tempfile.mkdtemp(prefix="xue-cmageps-rewrite-"))
        self.addCleanup(shutil.rmtree, scratch, True)
        step = FIXTURE_FRAMES[1]
        frames = grib2.inspect_grib_fast(step, INPUTS)
        spans = grib2.message_spans(step)
        products = []
        for variable_id in INPUTS:
            offset, length = spans[frames[variable_id].band - 1]
            plain = scratch / f"{variable_id}.grib2"
            product = scratch / f"{variable_id}.product.grib2"
            plain.write_bytes(step.read_bytes()[offset : offset + length])
            subprocess.run(["grib_set", "-s", self.PRODUCT_KEYS[variable_id], str(plain), str(product)], check=True)
            products.append(product.read_bytes())
        body = b"".join(products)
        candidates = [CacheObject("noaa", "file", len(body))]
        payload = cmageps.frame_records(SPEC, RUN, 6, INPUTS, candidates, Reader({"file": body}), set_keys)
        frame = scratch / "frame.grib2"
        frame.write_bytes(payload)
        rewritten = grib2.inspect_grib_fast(frame, INPUTS)
        for variable_id in INPUTS:
            self.assertEqual(rewritten[variable_id].band, frames[variable_id].band, variable_id)
            self.assertEqual(rewritten[variable_id].unit, frames[variable_id].unit, variable_id)
            self.assertEqual(rewritten[variable_id].lead_seconds, 6 * 3600, variable_id)


class MatcherTests(unittest.TestCase):
    def test_the_header_index_finds_every_input_in_source_order(self) -> None:
        for path, expected in zip(FIXTURE_FRAMES, (("hgt500", "prmsl", "tmp2m", "tmp850", "cape"), INPUTS)):
            frames = grib2.inspect_grib_fast(path, INPUTS, optional_ids=SPEC.optional_at_analysis)
            self.assertEqual(tuple(variable_id for variable_id in INPUTS if variable_id in frames), expected, path.name)
            self.assertEqual([frames[variable_id].band for variable_id in expected], list(range(1, len(expected) + 1)))

    def test_the_precipitation_is_the_six_hours_ending_at_the_step(self) -> None:
        by_band = {message.band: message for message in grib2.index_messages(FIXTURE_FRAMES[1])}
        apcp = grib2.inspect_grib_fast(FIXTURE_FRAMES[1], ("apcp",))["apcp"]
        message = by_band[apcp.band]
        self.assertEqual((message.parameter_number, message.statistical_process), (8, 1))
        self.assertEqual(message.valid_time, RUN.time + timedelta(hours=6))
        self.assertEqual(apcp.unit, "kg/(m^2)")

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        assert_gdalinfo_agrees(self, ((path, INPUTS, SPEC.optional_at_analysis) for path in FIXTURE_FRAMES))


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.output = REFERENCE.run_directory
        _, cls.report = REFERENCE.get()

    def test_every_published_bundle_is_written(self) -> None:
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["model"], "CMA-GEPS")
        self.assertEqual(manifest["product"], "cma-geps-0p25")
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], list(BUNDLES))

    def test_the_crop_grid(self) -> None:
        grid = stored_bundle(self.output, "tmp2m").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (80, 80))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.25, -0.25))
        # The file's first row is 89.875°N, so its cells sit an eighth of a
        # degree off the GFS grid's.
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (130.0, 49.875))

    def test_the_rate_is_the_six_hour_total_over_six(self) -> None:
        bundle = stored_bundle(self.output, "prate")
        self.assertEqual(bundle.metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 6, "frameCount": 1, "frameStep": 1})
        self.assertEqual(bundle.metadata["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 0)
        rates = quantize.PROFILES["quality"]["prate"].decode(bundle.decode_plane(1, 6))
        # The window's wettest cell has a median of 16.2 mm over the six hours.
        self.assertGreater(float(rates.max()), 2.5)
        self.assertLess(float(rates.max()), 2.9)

    def test_the_gust_and_the_cloud_cover_start_at_the_first_step(self) -> None:
        for bundle_id in ("gust", "tcdc"):
            metadata = stored_bundle(self.output, bundle_id).metadata
            self.assertEqual(metadata["time"]["firstFrameOffset"], 6, bundle_id)
            self.assertNotIn("typeOfStatisticalProcessing", metadata["variables"][0]["parameter"], bundle_id)
        self.assertEqual(stored_bundle(self.output, "gust").metadata["variables"][0]["parameter"]["typeOfFirstFixedSurface"], 1)

    def test_the_temperature_is_celsius(self) -> None:
        bundle = stored_bundle(self.output, "tmp2m")
        values = quantize.PROFILES["quality"]["tmp2m"].decode(bundle.decode_plane(1, 6))
        self.assertGreater(float(values.min()), -25.0)
        self.assertLess(float(values.max()), 30.0)


class NativeParityTests(ParityCase, unittest.TestCase):
    """The same two frames through both encoders, byte for byte."""

    reference = REFERENCE
    expect = (store_path("cmageps.2026111012", "prate"), store_path("cmageps.2026111012", "gust"))


if __name__ == "__main__":
    unittest.main()

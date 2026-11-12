"""The cmageps source: CMA GRAPES GEPS, the China Meteorological
Administration's global ensemble, from the WMO WIS2 global caches.

CMA publishes no members, only ensemble products: one GRIB2 file per lead
time with the mean and spread (template 4.2), the 0/25/50/75/100th
percentiles (4.6, and 4.10 over an interval) and exceedance probabilities
(4.5, 4.9), JPEG2000 packed and with no index. The fetch
(``xuebuild/cmageps.py``) walks each file's message heads over Range and
takes the record of every input the registry's ``template-ensemble`` rules
name — the centre of each quantity (the mean where CMA writes one, the
median elsewhere), its quartiles or spread, the precipitation over the six
hours ending at the step and the probabilities of more than 1 to 100 mm in
the 24 hours ending at it — as CMA wrote it; the frame is then repacked to
grid_simple. The centre is published under the deterministic identities
with an ``ensemble`` block saying which statistic it is, the rest under ids
of their own (docs/format.md §"Ensemble").

``tests/fixtures/cmageps.2026111012.f000.crop.grib2`` through ``f024`` are
an 80 x 80 cell window from 130°E to 149.75°E and 49.875°N to 30.125°N
(Japan and the Pacific east of it) of the analysis and the first four
six-hourly steps of the 2026-11-10 12Z cycle, every record the fetcher
writes in source order: thirteen at the analysis (no precipitation, gust or
cloud cover is read there), twenty-two at the steps before the 24th hour,
and twenty-eight at it, where the 24-hour probabilities begin.
"""

from __future__ import annotations

import copy
import json
import struct
import unittest
from datetime import UTC, datetime, timedelta

import numpy as np

from tests._support import FIXTURES, ParityCase, ReferenceBuild, assert_gdalinfo_agrees, requires_gdalinfo, store_path, stored_bundle
from xuebuild import cmageps, grib2, quantize
from xuebuild.assemble import group_needs_eccodes
from xuebuild.binformat import BundleError, parse_metadata
from xuebuild.discover import analysis_optional_ids, bundle_input_ids, published_bundle_ids, store_dimensions
from xuebuild.errors import DownloadError
from xuebuild.feeds import FrameFeed, fetcher
from xuebuild.fetch import _frame_variable_ids
from xuebuild.idx import ByteRange
from xuebuild.model import GfsRun
from xuebuild.quantize import PROFILES
from xuebuild.registry import document
from xuebuild.rules import EnsembleIdentity
from xuebuild.sources import source_spec
from xuebuild.stac import _PROSE
from xuebuild.variables import variable_spec
from xuebuild.wis2 import CacheObject

HOURS = (0, 6, 12, 18, 24)
FIXTURE_FRAMES = [FIXTURES / f"cmageps.2026111012.f{hour:03d}.crop.grib2" for hour in HOURS]
SPEC = source_spec("cmageps")
RUN = GfsRun(datetime(2026, 11, 10, 12, tzinfo=UTC))
PROBABILITIES = ("apcp24gt1", "apcp24gt5", "apcp24gt10", "apcp24gt25", "apcp24gt50", "apcp24gt100")
INPUTS = (
    "hgt500",
    "hgt500sd",
    "prmsl",
    "prmslsd",
    *PROBABILITIES,
    "apcp",
    "apcpq25",
    "apcpq75",
    "tmp2m",
    "tmp2mq25",
    "tmp2mq75",
    "tmp850",
    "tmp850q25",
    "tmp850q75",
    "gust",
    "gustq25",
    "gustq75",
    "cape",
    "capeq25",
    "capeq75",
    "tcdc",
    "tcdcq25",
    "tcdcq75",
)
ANALYSIS_OPTIONAL = ("apcp", "apcpq25", "apcpq75", "gust", "gustq25", "gustq75", "tcdc", "tcdcq25", "tcdcq75")
AT_ANALYSIS = tuple(variable_id for variable_id in INPUTS if variable_id not in ANALYSIS_OPTIONAL + PROBABILITIES)
BEFORE_A_DAY = tuple(variable_id for variable_id in INPUTS if variable_id not in PROBABILITIES)
BUNDLES = (
    "tmp2m",
    "tmp2mq25",
    "tmp2mq75",
    "prate",
    "prateq25",
    "prateq75",
    *PROBABILITIES,
    "prmsl",
    "prmslsd",
    "gust",
    "gustq25",
    "gustq75",
    "tcdc",
    "tcdcq25",
    "tcdcq75",
    "cape",
    "capeq25",
    "capeq75",
    "tmp850",
    "tmp850q25",
    "tmp850q75",
    "hgt500",
    "hgt500sd",
)
MEDIAN = {"percentileValue": 50}
MEAN = {"derivedForecast": 0}

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
    walk reads. ``extra`` is what the template appends to 4.0's octets."""
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


def _interval_octets(end: int, start: int, statistical: int = 1) -> bytes:
    """The end time and one time range in hours of an interval template."""
    ending = RUN.time + timedelta(hours=end)
    return struct.pack(">HBBBBB", ending.year, ending.month, ending.day, ending.hour, 0, 0) + struct.pack(
        ">BIBBBIBI", 1, 0, statistical, 2, 1, end - start, 255, 0
    )


def _mean(triple, level, hour, derived=0):
    return _message(2, triple, level, hour, bytes([derived, 31]))


def _percentile(triple, level, hour, percentile):
    return _message(6, triple, level, hour, bytes([percentile]))


def _interval(triple, level, start, end, percentile, statistical=1):
    return _message(10, triple, level, start, bytes([percentile]) + _interval_octets(end, start, statistical))


def _limits(threshold: int) -> bytes:
    # CMA writes the threshold into both limits whatever the type uses.
    return bytes([3, 6, 3]) + struct.pack(">BIBI", 2, threshold, 2, threshold)


def _probability(triple, level, hour, threshold):
    return _message(5, triple, level, hour, _limits(threshold))


def _interval_probability(triple, level, start, end, threshold):
    return _message(9, triple, level, start, _limits(threshold) + _interval_octets(end, start))


SURFACE = (1,) + MISSING_LEVEL
TEN_METRES = (103, 0, 10)
TWO_METRES = (103, 0, 2)
APCP = (0, 1, 8)


def _percentiles(variable_id, triple, level, hour):
    """The five percentiles of an instantaneous field, the quartiles and
    the median under their ids when ``variable_id`` is one."""
    named = {25: f"{variable_id}q25", 50: variable_id, 75: f"{variable_id}q75"} if variable_id else {}
    return [(named.get(value), _percentile(triple, level, hour, value)) for value in (0, 25, 50, 75, 100)]


def _window(names, start, end):
    return [(names.get(value), _interval(APCP, SURFACE, start, end, value)) for value in (0, 25, 50, 75, 100)]


#: A +24 h file: every kind of record the real one carries, in its order.
PRODUCTS_AT_24 = [
    ("hgt500", _mean((0, 3, 5), (100, 0, 50000), 24)),
    ("hgt500sd", _mean((0, 3, 5), (100, 0, 50000), 24, derived=4)),
    ("prmsl", _mean((0, 3, 1), (101,) + MISSING_LEVEL, 24)),
    ("prmslsd", _mean((0, 3, 1), (101,) + MISSING_LEVEL, 24, derived=4)),
    *[
        (f"apcp24gt{threshold // 100}", _interval_probability(APCP, SURFACE, 0, 24, threshold))
        for threshold in (100, 500, 1000, 2500, 5000, 10000)
    ],
    *[(None, _interval_probability(APCP, SURFACE, 18, 24, threshold)) for threshold in (100, 500, 1000, 2500, 5000)],
    *[(None, _probability((0, 2, 1), TEN_METRES, 24, threshold)) for threshold in (1000, 1500, 2000, 2500)],
    *[(None, _probability((0, 2, 22), TEN_METRES, 24, threshold)) for threshold in (1500, 2500, 3500)],
    *_window({}, 0, 24),
    *_window({25: "apcpq25", 50: "apcp", 75: "apcpq75"}, 18, 24),
    (None, _interval((0, 1, 29), SURFACE, 18, 24, 50)),
    *_percentiles("tmp2m", (0, 0, 0), TWO_METRES, 24),
    *_percentiles("tmp850", (0, 0, 0), (100, 0, 85000), 24),
    *_percentiles(None, (0, 2, 1), TEN_METRES, 24),
    *_percentiles("gust", (0, 2, 22), TEN_METRES, 24),
    *_percentiles("cape", (0, 7, 6), SURFACE, 24),
    *_percentiles("tcdc", (0, 6, 1), SURFACE, 24),
    *_percentiles(None, (0, 2, 25), (100, 0, 25000), 24),
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
    def test_the_published_set(self) -> None:
        self.assertEqual(published_bundle_ids(SPEC), BUNDLES)
        self.assertEqual(SPEC.input_variable_ids, INPUTS)
        self.assertEqual((SPEC.manifest_model, SPEC.product, SPEC.latest_filename), ("CMA-GEPS", "cma-geps-0p25", "latest-cmageps.json"))
        self.assertTrue(SPEC.interval_precipitation)
        self.assertFalse(SPEC.accumulated_precipitation)
        # The analysis carries no precipitation, and its gust and cloud
        # cover are fields of zeros: none of their percentiles is read there.
        self.assertEqual(SPEC.optional_at_analysis, ANALYSIS_OPTIONAL)
        # The rates, the gust and cloud cover percentiles, and the
        # probabilities, which start at the 24th hour.
        self.assertEqual(
            analysis_optional_ids(SPEC, published_bundle_ids(SPEC)),
            tuple(bundle_id for bundle_id in BUNDLES if bundle_id.startswith(("prate", "gust", "tcdc", "apcp24"))),
        )
        self.assertEqual(SPEC.statistical_processes, (("prate", 0), ("prateq25", 0), ("prateq75", 0)))

    def test_the_centre_names_its_statistic(self) -> None:
        ensembles = {variable_id: ensemble.metadata() for variable_id, ensemble in SPEC.ensembles}
        self.assertEqual(
            ensembles,
            {
                "tmp2m": MEDIAN,
                "prate": MEDIAN,
                "gust": MEDIAN,
                "tcdc": MEDIAN,
                "cape": MEDIAN,
                "tmp850": MEDIAN,
                "prmsl": MEAN,
                "hgt500": MEAN,
            },
        )
        # The shared variables carry no block of their own: GFS's tmp2m is
        # a single forecast.
        for variable_id in ensembles:
            self.assertIsNone(variable_spec(variable_id).grib2_ensemble, variable_id)

    def test_the_rate_quartiles_are_the_quartile_totals_over_the_interval(self) -> None:
        self.assertEqual(SPEC.interval_precipitation_pairs, (("prateq25", "apcpq25"), ("prateq75", "apcpq75")))
        self.assertEqual(bundle_input_ids(SPEC, "prateq25"), ("apcpq25",))
        self.assertEqual(bundle_input_ids(SPEC, "prate"), ("apcp",))
        # Input-only, as apcp is.
        for total_id in ("apcpq25", "apcpq75"):
            self.assertNotIn(total_id, BUNDLES)
            self.assertNotIn(total_id, PROFILES["quality"])

    def test_the_probabilities_start_at_the_24th_hour(self) -> None:
        self.assertEqual(SPEC.variable_first_hours, tuple((variable_id, 24) for variable_id in PROBABILITIES))
        self.assertEqual(_frame_variable_ids(SPEC, 0), AT_ANALYSIS)
        for hour in (6, 12, 18):
            self.assertEqual(_frame_variable_ids(SPEC, hour), BEFORE_A_DAY, hour)
        self.assertEqual(_frame_variable_ids(SPEC, 24), INPUTS)
        self.assertEqual(_frame_variable_ids(SPEC, 360), INPUTS)
        # Each on an axis of its own in the run's store, as the rates are.
        for bundle_id in ("apcp24gt1", "prateq25", "gustq75"):
            self.assertEqual(store_dimensions(SPEC, bundle_id), (f"time_{bundle_id}", "latitude", "longitude"))
        self.assertEqual(store_dimensions(SPEC, "tmp2mq25"), ("time", "latitude", "longitude"))

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

    def test_the_feed_repacks_every_frame(self) -> None:
        self.assertEqual(SPEC.packing, "jpeg2000")
        self.assertIsInstance(fetcher(SPEC), FrameFeed)
        self.assertTrue(group_needs_eccodes(SPEC, ("tmp2m",)))

    def test_the_prose_names_the_ensemble_centre(self) -> None:
        prose = _PROSE["sources"]["cmageps"]
        self.assertIn("China Meteorological Administration", prose["description"])
        self.assertIn("median", prose["description"])
        self.assertIn("mean", prose["description"])


class VariableRegistryTests(unittest.TestCase):
    """The ensemble variables are registry data: identities, codebooks and
    record rules, mirrored to the native encoder through the document."""

    def test_the_quartiles_keep_the_quantity(self) -> None:
        for base_id in ("tmp2m", "tmp850", "gust", "tcdc", "cape", "prate"):
            base = variable_spec(base_id)
            for percentile in (25, 75):
                variable_id = f"{base_id}q{percentile}"
                spec = variable_spec(variable_id)
                with self.subTest(variable=variable_id):
                    self.assertEqual(spec.parameter_metadata(), base.parameter_metadata())
                    self.assertEqual(spec.output_unit, base.output_unit)
                    self.assertEqual(spec.label, f"{base.label}, {percentile}th percentile")
                    self.assertEqual(spec.grib2_ensemble, EnsembleIdentity.percentile_of(percentile))
                    self.assertEqual((spec.predictor, spec.decimation, spec.companions), (base.predictor, base.decimation, base.companions))
                    for profile, codebooks in PROFILES.items():
                        self.assertEqual(codebooks[variable_id], codebooks[base_id], profile)

    def test_the_spreads_and_the_probabilities_have_codebooks_of_their_own(self) -> None:
        expected = {
            "prmslsd": {"type": "linear", "offset": 0.0, "scale": 0.1, "minimumCode": 0, "maximumCode": 250, "nodataCode": 255},
            "hgt500sd": {"type": "linear", "offset": 0.0, "scale": 1.0, "minimumCode": 0, "maximumCode": 250, "nodataCode": 255},
            **{
                variable_id: {"type": "linear", "offset": 0.0, "scale": 0.5, "minimumCode": 0, "maximumCode": 200, "nodataCode": 255}
                for variable_id in PROBABILITIES
            },
        }
        for variable_id, codebook in expected.items():
            for profile, codebooks in PROFILES.items():
                self.assertEqual(codebooks[variable_id].metadata(), codebook, (variable_id, profile))
        self.assertEqual(variable_spec("prmslsd").output_unit, "hPa")
        self.assertEqual(variable_spec("hgt500sd").output_unit, "m")
        self.assertEqual(variable_spec("prmslsd").grib2_ensemble, EnsembleIdentity.derived(4))

    def test_a_probability_is_of_the_24_hour_total(self) -> None:
        for threshold, variable_id in zip((1, 5, 10, 25, 50, 100), PROBABILITIES):
            spec = variable_spec(variable_id)
            with self.subTest(variable=variable_id):
                self.assertEqual(spec.label, f"Probability of 24-hour precipitation above {threshold} mm")
                self.assertEqual(spec.output_unit, "%")
                self.assertEqual(
                    spec.parameter_metadata(),
                    {
                        "discipline": 0,
                        "parameterCategory": 1,
                        "parameterNumber": 8,
                        "typeOfFirstFixedSurface": 1,
                        "scaleFactorOfFirstFixedSurface": 0,
                        "scaledValueOfFirstFixedSurface": 0,
                        "typeOfStatisticalProcessing": 1,
                    },
                )
                self.assertEqual(
                    spec.grib2_ensemble.metadata(),
                    {
                        "probabilityType": 3,
                        "scaleFactorOfLowerLimit": 2,
                        "scaledValueOfLowerLimit": threshold * 100,
                        "scaleFactorOfUpperLimit": None,
                        "scaledValueOfUpperLimit": None,
                    },
                )

    def test_the_document_carries_the_rules_and_the_source_keys(self) -> None:
        whole = document()
        rule = whole["variables"]["apcp24gt10"]["match"]["gdal"]
        self.assertEqual(
            rule,
            [
                {
                    "rule": "template-ensemble",
                    "discipline": 0,
                    "category": 1,
                    "number": 8,
                    "levelType": 1,
                    "levelValue": None,
                    "ensemble": {
                        "probabilityType": 3,
                        "scaleFactorOfLowerLimit": 2,
                        "scaledValueOfLowerLimit": 1000,
                        "scaleFactorOfUpperLimit": None,
                        "scaledValueOfUpperLimit": None,
                    },
                    "hours": 24,
                    "statistical": 1,
                }
            ],
        )
        self.assertEqual(
            whole["variables"]["apcp"]["match"]["gdal"],
            [
                {
                    "rule": "template-ensemble",
                    "discipline": 0,
                    "category": 1,
                    "number": 8,
                    "levelType": 1,
                    "levelValue": None,
                    "ensemble": MEDIAN,
                    "hours": 6,
                    "statistical": 1,
                }
            ],
        )
        self.assertEqual(
            whole["variables"]["tmp2m"]["match"]["gdal"][-1],
            {
                "rule": "template-ensemble",
                "discipline": 0,
                "category": 0,
                "number": 0,
                "levelType": 103,
                "levelValue": 2.0,
                "ensemble": MEDIAN,
                "hours": None,
                "statistical": None,
            },
        )
        self.assertEqual(whole["variables"]["prmslsd"]["ensemble"], {"derivedForecast": 4})
        self.assertNotIn("ensemble", whole["variables"]["tmp2m"])
        source = whole["sources"]["cmageps"]
        self.assertEqual(source["ensembles"][0], ["tmp2m", MEDIAN])
        self.assertEqual(source["intervalPrecipitationPairs"], [["prateq25", "apcpq25"], ["prateq75", "apcpq75"]])
        self.assertEqual(source["variableFirstHours"], [[variable_id, 24] for variable_id in PROBABILITIES])
        # Every other source names none.
        self.assertEqual(whole["sources"]["gfs"]["ensembles"], [])
        self.assertEqual(whole["sources"]["gfs"]["variableFirstHours"], [])


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
        message = _interval(APCP, SURFACE, 18, 24, 50)
        record = cmageps.parse_head(message[: cmageps.HEAD_BYTES], 0, "file")
        self.assertEqual(record.length, len(message))
        identity = record.identity
        self.assertEqual((identity.template, identity.ensemble, identity.statistical_process, identity.interval_hours), (10, EnsembleIdentity.percentile_of(50), 1, 6))
        self.assertEqual(identity.valid_time, RUN.time + timedelta(hours=24))
        self.assertIsNone(identity.level_value)
        spread = cmageps.parse_head(_mean((0, 3, 5), (100, 0, 50000), 24, derived=4), 0, "file").identity
        self.assertEqual((spread.template, spread.ensemble, spread.level_value), (2, EnsembleIdentity.derived(4), 50000.0))
        # A probability keeps the limit its type uses: CMA writes both.
        probability = cmageps.parse_head(_interval_probability(APCP, SURFACE, 0, 24, 1000), 0, "file").identity
        self.assertEqual(probability.ensemble, EnsembleIdentity(probability_type=3, lower_limit=(2, 1000)))
        self.assertEqual(probability.interval_hours, 24)
        # A template the index does not read is a record of nothing.
        self.assertIsNone(cmageps.parse_head(_message(15, (0, 0, 0), SURFACE, 24, bytes(4)), 0, "file").identity)
        with self.assertRaises(cmageps.HeadTruncated):
            cmageps.parse_head(message[:40], 0, "file")
        with self.assertRaises(DownloadError):
            cmageps.parse_head(b"GRIB\0\0\0\1" + message[8:], 0, "file")

    def test_the_walk_takes_each_input_s_record_and_stops(self) -> None:
        body, offsets = _file(PRODUCTS_AT_24)
        read = Reader({"file": body})
        found = cmageps.walk("file", len(body), 24, INPUTS, read)
        self.assertEqual({variable_id: record.offset for variable_id, record in found.items()}, offsets)
        # One head per message up to the last input's, none after it.
        last = max(index for index, (variable_id, _) in enumerate(PRODUCTS_AT_24) if variable_id is not None)
        self.assertEqual(len(read.asked), last + 1)
        # The six-hour window is the step's; the run-long one is not.
        self.assertEqual(found["apcp"].identity.interval_hours, 6)
        # Nothing in a +24 h file is valid at +18 h.
        with self.assertRaises(DownloadError):
            cmageps.walk("file", len(body), 18, INPUTS, Reader({"file": body}))

    def test_a_file_of_unknown_size_is_walked_the_same(self) -> None:
        body, offsets = _file(PRODUCTS_AT_24)
        found = cmageps.walk("file", None, 24, INPUTS, Reader({"file": body}))
        self.assertEqual({variable_id: record.offset for variable_id, record in found.items()}, offsets)
        # A cache's "not found" page, served whole as a 200, is no GRIB2.
        page = b"<html><body>Page Not Found</body></html>" + b" " * 600
        with self.assertRaises(DownloadError):
            cmageps.walk("page", None, 24, INPUTS, Reader({"page": page}))

    def test_two_records_of_one_input_are_refused(self) -> None:
        body, _ = _file(PRODUCTS_AT_24 + [("tmp2mq25", _percentile((0, 0, 0), TWO_METRES, 24, 25))])
        with self.assertRaises(DownloadError):
            cmageps.walk("file", len(body), 24, INPUTS + ("vis",), Reader({"file": body}))

    def test_a_frame_comes_from_the_first_cache_that_serves_it_as_written(self) -> None:
        body, _ = _file(PRODUCTS_AT_24)
        read = Reader({"noaa-url": body, "jma-url": body}, broken=("noaa-url",))
        candidates = [CacheObject("noaa", "noaa-url", len(body)), CacheObject("jma", "jma-url", len(body))]
        payload = cmageps.frame_records(SPEC, RUN, 24, INPUTS, candidates, read)
        expected = {variable_id: message for variable_id, message in PRODUCTS_AT_24 if variable_id is not None}
        self.assertEqual(payload, b"".join(expected[variable_id] for variable_id in INPUTS))
        with self.assertRaises(DownloadError):
            cmageps.frame_records(SPEC, RUN, 24, INPUTS, candidates[:1], read)


class MatcherTests(unittest.TestCase):
    def test_the_header_index_finds_every_input_in_source_order(self) -> None:
        optional = SPEC.optional_at_analysis + PROBABILITIES
        expected = (AT_ANALYSIS, BEFORE_A_DAY, BEFORE_A_DAY, BEFORE_A_DAY, INPUTS)
        for path, hour, wanted in zip(FIXTURE_FRAMES, HOURS, expected):
            frames = grib2.inspect_grib_fast(path, INPUTS, optional_ids=optional)
            self.assertEqual(tuple(variable_id for variable_id in INPUTS if variable_id in frames), wanted, path.name)
            self.assertEqual([frames[variable_id].band for variable_id in wanted], list(range(1, len(wanted) + 1)))
            for variable_id in wanted:
                self.assertEqual(frames[variable_id].lead_seconds, hour * 3600, variable_id)

    def test_the_records_are_the_products_cma_wrote(self) -> None:
        messages = grib2.index_messages(FIXTURE_FRAMES[-1])
        by_band = {message.band: message for message in messages}
        frames = grib2.inspect_grib_fast(FIXTURE_FRAMES[-1], INPUTS)
        apcp = by_band[frames["apcp"].band]
        self.assertEqual((apcp.template, apcp.parameter_number, apcp.statistical_process, apcp.interval_hours), (10, 8, 1, 6))
        self.assertEqual(apcp.ensemble, EnsembleIdentity.percentile_of(50))
        self.assertEqual(apcp.valid_time, RUN.time + timedelta(hours=24))
        self.assertEqual(frames["apcp"].unit, "kg/(m^2)")
        probability = by_band[frames["apcp24gt10"].band]
        self.assertEqual((probability.template, probability.interval_hours), (9, 24))
        self.assertEqual(probability.ensemble, variable_spec("apcp24gt10").grib2_ensemble)
        self.assertEqual(frames["apcp24gt10"].unit, "%")
        # The gust is CMA's 10 m one, matched by its rule on that surface.
        gust = by_band[frames["gust"].band]
        self.assertEqual((gust.level_type, gust.level_value), (103, 10.0))
        hgt, spread = by_band[frames["hgt500"].band], by_band[frames["hgt500sd"].band]
        self.assertEqual((hgt.template, hgt.ensemble, spread.ensemble), (2, EnsembleIdentity.derived(0), EnsembleIdentity.derived(4)))
        # A single-forecast variable matches no ensemble product, and a
        # statistic matches no record but its own.
        for variable_id in ("tmp2m", "prmsl", "hgt500"):
            self.assertFalse(any(grib2._matches(variable_spec(variable_id), message) for message in messages if message.band != frames[variable_id].band))
        self.assertFalse(any(grib2._matches(variable_spec("dpt2m"), message) for message in messages))

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        optional = SPEC.optional_at_analysis + PROBABILITIES
        assert_gdalinfo_agrees(self, ((path, INPUTS, optional) for path in FIXTURE_FRAMES))


class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.output = REFERENCE.run_directory
        _, cls.report = REFERENCE.get()

    def _values(self, bundle_id: str, offset: int) -> np.ndarray:
        bundle = stored_bundle(self.output, bundle_id)
        return quantize.PROFILES[bundle.metadata["profile"]][bundle_id].decode(bundle.decode_plane(1, offset))

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

    def test_every_variable_names_its_statistic(self) -> None:
        for bundle_id in BUNDLES:
            variable = stored_bundle(self.output, bundle_id).metadata["variables"][0]
            spec = variable_spec(bundle_id)
            expected = dict(SPEC.ensembles).get(bundle_id, spec.grib2_ensemble)
            with self.subTest(bundle=bundle_id):
                self.assertEqual(variable["ensemble"], expected.metadata())
                self.assertEqual(list(variable)[-2:], ["ensemble", "quantization"])
        self.assertEqual(stored_bundle(self.output, "tmp2m").metadata["variables"][0]["ensemble"], MEDIAN)
        self.assertEqual(stored_bundle(self.output, "hgt500").metadata["variables"][0]["ensemble"], MEAN)

    def test_the_rates_are_the_six_hour_totals_over_six(self) -> None:
        for bundle_id in ("prate", "prateq25", "prateq75"):
            metadata = stored_bundle(self.output, bundle_id).metadata
            self.assertEqual(metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 6, "frameCount": 4, "frameStep": 6}, bundle_id)
            self.assertEqual(metadata["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 0, bundle_id)
        rates = self._values("prate", 6)
        # The window's wettest cell has a median of 16.2 mm over the six hours.
        self.assertGreater(float(rates.max()), 2.5)
        self.assertLess(float(rates.max()), 2.9)
        for offset in (6, 12, 18, 24):
            q25, q50, q75 = (stored_bundle(self.output, bundle_id).decode_plane(1, offset) for bundle_id in ("prateq25", "prate", "prateq75"))
            self.assertTrue(np.all(q25 <= q50) and np.all(q50 <= q75), offset)

    def test_the_quartiles_bracket_the_median(self) -> None:
        for base_id in ("tmp2m", "tmp850", "cape"):
            for offset in HOURS:
                q25, q50, q75 = (
                    stored_bundle(self.output, bundle_id).decode_plane(1, offset)
                    for bundle_id in (f"{base_id}q25", base_id, f"{base_id}q75")
                )
                self.assertTrue(np.all(q25 <= q50) and np.all(q50 <= q75), (base_id, offset))

    def test_the_probabilities_start_at_the_24th_hour(self) -> None:
        previous = None
        for bundle_id in PROBABILITIES:
            metadata = stored_bundle(self.output, bundle_id).metadata
            self.assertEqual(metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 24, "frameCount": 1, "frameStep": 1}, bundle_id)
            self.assertEqual(metadata["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 1, bundle_id)
            codes = stored_bundle(self.output, bundle_id).decode_plane(1, 24).astype(int)
            self.assertLessEqual(int(codes.max()), 200, bundle_id)
            if previous is not None:
                # A higher threshold is passed by no more members; each
                # record is packed apart, so a code of rounding is allowed.
                self.assertTrue(np.all(codes <= previous + 1), bundle_id)
            previous = codes
        # Somewhere in the window every member has more than 1 mm.
        self.assertEqual(float(self._values("apcp24gt1", 24).max()), 100.0)

    def test_the_spreads_are_small_and_non_negative(self) -> None:
        for bundle_id, ceiling in (("prmslsd", 10.0), ("hgt500sd", 60.0)):
            metadata = stored_bundle(self.output, bundle_id).metadata
            self.assertEqual(metadata["time"]["firstFrameOffset"], 0, bundle_id)
            values = self._values(bundle_id, 24)
            self.assertGreater(float(values.max()), 0.0, bundle_id)
            self.assertLess(float(values.max()), ceiling, bundle_id)

    def test_the_gust_and_the_cloud_cover_start_at_the_first_step(self) -> None:
        for bundle_id in ("gust", "gustq25", "gustq75", "tcdc", "tcdcq25", "tcdcq75"):
            metadata = stored_bundle(self.output, bundle_id).metadata
            self.assertEqual(metadata["time"]["firstFrameOffset"], 6, bundle_id)
            self.assertNotIn("typeOfStatisticalProcessing", metadata["variables"][0]["parameter"], bundle_id)
            # The registered identities: the gust on the ground, the cloud
            # cover over the entire atmosphere, whatever surface CMA wrote.
            surface = 10 if bundle_id.startswith("tcdc") else 1
            self.assertEqual(metadata["variables"][0]["parameter"]["typeOfFirstFixedSurface"], surface, bundle_id)

    def test_the_temperature_is_celsius(self) -> None:
        values = self._values("tmp2m", 6)
        self.assertGreater(float(values.min()), -25.0)
        self.assertLess(float(values.max()), 30.0)


class MetadataValidationTests(unittest.TestCase):
    """The reader's rules for the ensemble block, on the reference build's
    metadata with each malformation in turn."""

    @classmethod
    def setUpClass(cls) -> None:
        output = REFERENCE.run_directory
        cls.percentile = stored_bundle(output, "tmp2mq25").metadata
        cls.derived = stored_bundle(output, "prmslsd").metadata
        cls.probability = stored_bundle(output, "apcp24gt10").metadata

    @staticmethod
    def _edited(base: dict, edit) -> dict:
        """``base`` with ``edit`` applied to its variable."""
        metadata = copy.deepcopy(base)
        edit(metadata["variables"][0])
        return metadata

    @classmethod
    def _edited_block(cls, base: dict, edit) -> dict:
        """``base`` with ``edit`` applied to its variable's ensemble block."""
        return cls._edited(base, lambda variable: edit(variable["ensemble"]))

    def test_the_written_blocks_are_accepted(self) -> None:
        for metadata in (self.percentile, self.derived, self.probability):
            parse_metadata(metadata)
        self.assertEqual(self.probability["variables"][0]["ensemble"]["probabilityType"], 3)

    def test_every_probability_type_takes_its_own_limits(self) -> None:
        limits = {
            0: ((2, 100), None),
            1: (None, (2, 100)),
            2: ((2, 100), (2, 500)),
            3: ((2, 100), None),
            4: (None, (2, 100)),
            5: ((2, 100), None),
        }
        for probability_type, (lower, upper) in limits.items():
            def edit(variable, probability_type=probability_type, lower=lower, upper=upper):
                variable["ensemble"] = EnsembleIdentity(probability_type=probability_type, lower_limit=lower, upper_limit=upper).metadata()

            with self.subTest(type=probability_type):
                parse_metadata(self._edited(self.probability, edit))

    def test_malformed_blocks_are_refused(self) -> None:
        def in_drop_key(block):
            del block["scaledValueOfUpperLimit"]

        def in_extra_key(block):
            block["x"] = 1

        def in_half_null(block):
            block["scaleFactorOfLowerLimit"] = None

        def in_null_where_the_type_uses_it(block):
            block["scaleFactorOfLowerLimit"] = None
            block["scaledValueOfLowerLimit"] = None

        def in_value_where_the_type_does_not(block):
            block["scaleFactorOfUpperLimit"] = 2
            block["scaledValueOfUpperLimit"] = 1000

        def in_type_out_of_range(block):
            block["probabilityType"] = 6

        def in_scale_out_of_range(block):
            block["scaleFactorOfLowerLimit"] = 128

        def in_value_out_of_range(block):
            block["scaledValueOfLowerLimit"] = 0xFFFFFFFF

        def in_a_bool(block):
            block["probabilityType"] = True

        def in_two_shapes(block):
            block["percentileValue"] = 50

        def not_an_object(variable):
            variable["ensemble"] = [1]

        def null_block(variable):
            variable["ensemble"] = None

        def empty_block(variable):
            variable["ensemble"] = {}

        for edit in (
            in_drop_key,
            in_extra_key,
            in_half_null,
            in_null_where_the_type_uses_it,
            in_value_where_the_type_does_not,
            in_type_out_of_range,
            in_scale_out_of_range,
            in_value_out_of_range,
            in_a_bool,
            in_two_shapes,
        ):
            with self.subTest(edit=edit.__name__):
                with self.assertRaisesRegex(BundleError, "ensemble"):
                    parse_metadata(self._edited_block(self.probability, edit))
        for edit in (not_an_object, null_block, empty_block):
            with self.subTest(edit=edit.__name__):
                with self.assertRaisesRegex(BundleError, "ensemble"):
                    parse_metadata(self._edited(self.probability, edit))

        def in_percentile_out_of_range(block):
            block["percentileValue"] = 101

        def in_percentile_float(block):
            block["percentileValue"] = 25.0

        def in_derived_out_of_range(block):
            block["derivedForecast"] = 256

        for base, edit in (
            (self.percentile, in_percentile_out_of_range),
            (self.percentile, in_percentile_float),
            (self.derived, in_derived_out_of_range),
        ):
            with self.subTest(edit=edit.__name__):
                with self.assertRaisesRegex(BundleError, "ensemble"):
                    parse_metadata(self._edited_block(base, edit))

    def test_a_block_below_schema_version_3_is_refused(self) -> None:
        metadata = copy.deepcopy(self.percentile)
        metadata["schemaVersion"] = 2
        del metadata["variables"][0]["parameter"]
        metadata["time"] = {"firstForecastHour": 0, "stepHours": 6, "frameCount": 1}
        with self.assertRaisesRegex(BundleError, "ensemble block requires schemaVersion 3"):
            parse_metadata(metadata)


class NativeParityTests(ParityCase, unittest.TestCase):
    """The same five frames through both encoders, byte for byte: the
    ensemble rules through both matchers, the quartile rates, the
    probabilities' own axis and the ensemble block."""

    reference = REFERENCE
    expect = (
        store_path("cmageps.2026111012", "prate"),
        store_path("cmageps.2026111012", "prateq25"),
        store_path("cmageps.2026111012", "gust"),
        store_path("cmageps.2026111012", "apcp24gt10"),
        store_path("cmageps.2026111012", "prmslsd"),
    )


if __name__ == "__main__":
    unittest.main()

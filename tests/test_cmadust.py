"""The cmadust source: CMA-CW v1.0/DUST, the China Meteorological
Administration's sand and dust storm forecast for the WMO SDS-WAS Asian
Center, from the WMO WIS2 global caches.

A fetched series-file forecast like ``ifshres``, with the fetch in this
repository (``xuebuild/cmadust.py``): every lead time is an uncompressed
NetCDF classic file on two caches, neither of which holds a whole run; the
union of their listings must name every lead time, the header is parsed in
pure Python off one Range read, and only the two surface fields are read
beside it. Each variable's run becomes one classic NetCDF series, latitudes
ascending as the file has them, which the converters turn north-up.

``tests/fixtures/cmadust.2026103000/`` is hours 0–2 of the 2026-10-30 00Z
run over a 5 x 4 cell box on the Hexi Corridor (97.3–97.9°E,
39.75–40.2°N), cut from a fetched run with the script in
``tests/fixtures/README.md``. The box holds a dust core past 10,000 µg/m³
and optical depths far past the codebook's 5, which reach the overflow
code.
"""

from __future__ import annotations

import json
import os
import struct
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import ClassTempRoot, FIXTURES, TempRoot, assert_native_matches, requires_gdal, requires_native_source
from xuebuild import binconvert, cmadust, fetch, observation
from xuebuild.binconvert import published_bundle_ids
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import parse_run, resolve_run
from xuebuild.idx import ByteRange
from xuebuild.manifest import validate_bin_manifest
from xuebuild.model import GfsRun
from xuebuild.observation import series_unit_accepted
from xuebuild.quantize import PROFILES
from xuebuild.sources import source_spec
from xuebuild.stac import _source_prose
from xuebuild.variables import AEROSOL_VARIABLE_IDS, variable_spec

SERIES_DIR = FIXTURES / "cmadust.2026103000"
SPEC = source_spec("cmadust")
RUN = GfsRun(datetime(2026, 10, 30, tzinfo=UTC))
WIDTH, HEIGHT = 5, 4
BUNDLES = ("dustsfc", "aoddust")


# -- A product file, written independently of the module's own writer ------


def _name(text: str) -> bytes:
    raw = text.encode()
    return struct.pack(">I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def _attributes(attributes: dict[str, str]) -> bytes:
    if not attributes:
        return struct.pack(">II", 0, 0)
    out = struct.pack(">II", 0x0C, len(attributes))
    for key, value in attributes.items():
        raw = value.encode()
        out += _name(key) + struct.pack(">II", 2, len(raw)) + raw + b"\0" * (-len(raw) % 4)
    return out


def product_file(
    hour: int,
    *,
    units: dict[str, str] | None = None,
    south: float = cmadust.GRID_SOUTH,
    version: int = 1,
) -> tuple[bytes, dict[str, np.ndarray]]:
    """A file laid out as CMA writes one (dimensions lon, lat and a
    pressure level; the coordinates first, then a 3-D field, then the
    surface fields), with planes that name their lead hour, and the planes
    as written."""
    width, height = cmadust.GRID_WIDTH, cmadust.GRID_HEIGHT
    rows, columns = np.meshgrid(np.arange(height), np.arange(width), indexing="ij")
    planes = {
        "sconc_dust": (rows * 10 + columns + hour * 1000).astype(">f4"),
        "aod550_dust": ((rows + columns) / 1000 + hour).astype(">f4"),
    }
    units = {"sconc_dust": "ug/m3", "aod550_dust": "-", **(units or {})}
    variables = [
        ("lon", (0,), 6, {"units": "degrees_east"}, (cmadust.GRID_WEST + cmadust.GRID_STEP * np.arange(width)).astype(">f8")),
        ("lat", (1,), 6, {"units": "degrees_north"}, (south + cmadust.GRID_STEP * np.arange(height)).astype(">f8")),
        ("level", (2,), 5, {"units": "hPa"}, np.array([1000.0, 500.0], dtype=">f4")),
        ("u", (2, 1, 0), 5, {"units": "m/s"}, np.zeros((2, height, width), dtype=">f4")),
        ("sconc_dust", (1, 0), 5, {"units": units["sconc_dust"]}, planes["sconc_dust"]),
        ("aod550_dust", (1, 0), 5, {"units": units["aod550_dust"]}, planes["aod550_dust"]),
    ]

    def header(begins: list[int]) -> bytes:
        out = b"CDF" + bytes([version]) + struct.pack(">I", 0)
        out += struct.pack(">II", 0x0A, 3) + _name("lon") + struct.pack(">I", width)
        out += _name("lat") + struct.pack(">I", height) + _name("level") + struct.pack(">I", 2)
        out += _attributes({"Title": "Sand and dust storm forecasting", "Model": "CMA-CW v1.0/DUST"})
        out += struct.pack(">II", 0x0B, len(variables))
        for (name, ids, nc_type, attributes, data), begin in zip(variables, begins):
            out += _name(name) + struct.pack(">I", len(ids)) + b"".join(struct.pack(">I", i) for i in ids)
            out += _attributes(attributes) + struct.pack(">II", nc_type, data.nbytes)
            out += struct.pack(">I" if version == 1 else ">Q", begin)
        return out

    length = len(header([0] * len(variables)))
    begins = [length + sum(item[4].nbytes for item in variables[:index]) for index in range(len(variables))]
    body = header(begins) + b"".join(item[4].tobytes() for item in variables)
    return body, {name: np.asarray(plane, dtype=np.float32) for name, plane in planes.items()}


class Bucket:
    """Both caches in memory: listing pages and Range reads, recorded."""

    def __init__(self, files: dict[str, dict[int, bytes]]) -> None:
        self.files = files
        self.keys: dict[str, list[tuple[str, int]]] = {cache.name: [] for cache in cmadust.CACHES}
        self.urls: dict[str, tuple[str, int]] = {}
        self.ranges: list[tuple[str, int, int]] = []
        self.listings: list[str] = []
        for cache in cmadust.CACHES:
            for hour in files.get(cache.name, {}):
                name = cmadust.file_name(RUN, hour)
                key = f"{cache.prefix}{name}" if cache.name == "noaa" else f"{cache.prefix}{name[:-3]}/1793344389/{name}"
                self.keys[cache.name].append((key, hour))
                self.urls[cmadust.object_url(cache, key)] = (cache.name, hour)

    def listing(self, url: str) -> str:
        self.listings.append(url)
        cache = next(cache for cache in cmadust.CACHES if url.startswith(cache.base_url + "/?"))
        contents = "".join(
            f"<Contents><Key>{key}</Key><Size>{len(self.files[cache.name][hour])}</Size></Contents>"
            for key, hour in self.keys[cache.name]
        )
        # A partial upload's numeric suffix and a .bin sit beside the files.
        stray = f"<Contents><Key>{cache.prefix}{cmadust.file_name(RUN, 0)[:-3]}.00618</Key><Size>5</Size></Contents>"
        return (
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"{contents}{stray}<IsTruncated>false</IsTruncated></ListBucketResult>"
        )

    def read(self, url: str, span: ByteRange) -> bytes:
        name, hour = self.urls[url]
        self.ranges.append((name, span.start, span.end))
        return self.files[name][hour][span.start : span.end + 1]


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_a_fetched_series_file_forecast(self) -> None:
        self.assertEqual((SPEC.manifest_model, SPEC.product, SPEC.latest_filename), ("CMA-CW-DUST", "cma-cw-dust-0p15", "latest-cmadust.json"))
        self.assertTrue(SPEC.series_file and SPEC.fetched and SPEC.live)
        self.assertFalse(SPEC.observation)
        self.assertIsNone(SPEC.open_meteo)
        self.assertIsNone(SPEC.regrid)
        self.assertFalse(SPEC.video)
        self.assertEqual(SPEC.variant_factors, ())

    def test_the_axis_is_hourly_to_72_then_three_hourly_to_168(self) -> None:
        self.assertEqual((SPEC.steps, SPEC.first_hour, SPEC.horizon_hours, SPEC.cycle_hours), (((72, 1), (168, 3)), 0, 168, 12))
        hours = SPEC.forecast_hours(168)
        self.assertEqual(len(hours), 105)
        self.assertEqual(hours[70:76], [70, 71, 72, 75, 78, 81])
        with self.assertRaises(DownloadError):
            SPEC.forecast_hours(73)
        with self.assertRaises(DownloadError):
            parse_run("2026103006", "cmadust")

    def test_it_publishes_the_surface_concentration_and_the_dust_optical_depth(self) -> None:
        self.assertEqual(SPEC.input_variable_ids, BUNDLES)
        self.assertEqual(published_bundle_ids(SPEC), BUNDLES)
        self.assertEqual(SPEC.core_bundle_ids, ("dustsfc",))
        self.assertEqual(SPEC.statistical_processes, ())
        self.assertEqual(set(cmadust.FILE_VARIABLES), set(BUNDLES))

    def test_the_grid_is_the_model_s_own(self) -> None:
        self.assertEqual(SPEC.production_grid, (cmadust.GRID_WIDTH, cmadust.GRID_HEIGHT))
        self.assertEqual(SPEC.production_grid, (835, 301))
        latitudes, longitudes = cmadust.grid_coordinates()
        self.assertEqual((latitudes[0], latitudes[-1], longitudes[0], longitudes[-1]), (15.0, 60.0, 25.0, 150.1))
        self.assertEqual(SPEC.tile, (56, 43))

    def test_the_catalog_prose_names_cma_and_the_wmo_data_policy(self) -> None:
        prose = _source_prose(SPEC)
        self.assertEqual(prose["license"], "other")
        self.assertIn("China Meteorological Administration", prose["description"])
        self.assertIn("WMO SDS-WAS Asian Center", prose["description"])
        self.assertIn("free and unrestricted", prose["description"])
        self.assertEqual(prose["providers"][0]["name"], "China Meteorological Administration")
        (licence,) = [link for link in prose["links"] if link["rel"] == "license"]
        self.assertIn("Unified Data Policy", licence["title"])


class VariableTests(unittest.TestCase):
    def test_the_surface_concentration_is_its_own_quantity(self) -> None:
        spec = variable_spec("dustsfc")
        self.assertEqual((spec.label, spec.output_unit, spec.value_range), ("Surface dust concentration", "µg/m³", (0, 50000)))
        # Every particle size: not GEFS-Aerosols' dust under 10 µm, and no
        # GRIB band is matched for it.
        self.assertNotIn("dustsfc", AEROSOL_VARIABLE_IDS)
        self.assertEqual((spec.grib2_discipline, spec.grib2_category, spec.grib2_number, spec.grib2_level_type), (0, 20, 0, 1))
        identity = spec.grib2_aerosol
        assert identity is not None
        self.assertEqual((identity.aerosol_type, identity.size_type, identity.wavelength_type), (62001, 255, 255))
        self.assertNotEqual(identity, variable_spec("pm10dust").grib2_aerosol)
        self.assertEqual(spec.gdal_rules, ())

    def test_the_codebook_is_logarithmic_to_fifty_thousand(self) -> None:
        for profile in ("quality", "balanced"):
            self.assertEqual(
                PROFILES[profile]["dustsfc"].metadata(),
                {
                    "type": "log1p", "trace": 1.0, "scale": 10.0, "maximum": 50000.0,
                    "minimumCode": 1, "maximumCode": 253, "zeroCode": 0, "overflowCode": 254, "nodataCode": 255,
                },
            )
        compact = PROFILES["compact"]["dustsfc"].metadata()
        self.assertEqual((compact["maximum"], compact["maximumCode"], compact["overflowCode"], compact["nodataCode"]), (50000.0, 125, 126, 127))

    def test_the_series_units_are_ones_the_converter_reads(self) -> None:
        for variable_id in BUNDLES:
            self.assertTrue(series_unit_accepted(variable_spec(variable_id), cmadust.SERIES_UNITS[variable_id]), variable_id)


class NamingTests(unittest.TestCase):
    def test_the_file_name_carries_the_lead_in_hundredths_of_an_hour(self) -> None:
        self.assertEqual(cmadust.file_name(RUN, 24), "Z_NAFP_C_BABJ_20261030000000_P_CEMC-CMA-CW-DUST_15KM_ASIA-02400.nc")
        self.assertEqual(cmadust.file_name(RUN, 168), "Z_NAFP_C_BABJ_20261030000000_P_CEMC-CMA-CW-DUST_15KM_ASIA-16800.nc")
        self.assertEqual(cmadust.parse_file_name(cmadust.file_name(RUN, 75)), (RUN.time, 75))

    def test_anything_but_a_whole_file_of_a_whole_hour_is_ignored(self) -> None:
        stem = "Z_NAFP_C_BABJ_20261030000000_P_CEMC-CMA-CW-DUST_15KM_ASIA-05800"
        self.assertIsNone(cmadust.parse_file_name(f"{stem}.00618"))
        self.assertIsNone(cmadust.parse_file_name(f"{stem}.bin"))
        self.assertIsNone(cmadust.parse_file_name(stem.replace("05800", "05830") + ".nc"))
        self.assertIsNone(cmadust.parse_file_name(stem.replace("15KM_ASIA", "15KM_GLB") + ".nc"))

    def test_the_jma_key_s_colon_is_escaped_in_its_url(self) -> None:
        jma = cmadust.CACHES[1]
        self.assertEqual(jma.name, "jma")
        self.assertEqual(
            cmadust.run_prefix(jma, RUN),
            "wis2/data/cn-cma:data.core.weather.prediction.forecast.medium-range.deterministic.limited-area/Z_NAFP_C_BABJ_20261030000000",
        )
        self.assertIn("/wis2/data/cn-cma%3Adata.core.", cmadust.object_url(jma, cmadust.run_prefix(jma, RUN)))


class HeaderTests(unittest.TestCase):
    def test_the_header_names_every_variable_s_offset(self) -> None:
        body, _ = product_file(0)
        header = cmadust.parse_header(body)
        self.assertEqual((header.version, header.numrecs), (1, 0))
        self.assertEqual(header.dimensions, {"lon": 835, "lat": 301, "level": 2})
        self.assertEqual(header.attributes["Model"], "CMA-CW v1.0/DUST")
        sconc = header.variables["sconc_dust"]
        self.assertEqual((sconc.dimensions, sconc.shape, sconc.nc_type, sconc.vsize), (("lat", "lon"), (301, 835), 5, 301 * 835 * 4))
        self.assertEqual(sconc.attributes["units"], "ug/m3")
        self.assertEqual(header.data_end, len(body))
        self.assertEqual(header.variables["lon"].begin, header.length)

    def test_a_64_bit_offset_file_reads_the_same(self) -> None:
        body, planes = product_file(3, version=2)
        header = cmadust.parse_header(body)
        self.assertEqual(header.version, 2)
        plane = cmadust.read_variable("file", header, "sconc_dust", lambda url, span: body[span.start : span.end + 1])
        np.testing.assert_array_equal(plane, planes["sconc_dust"])

    def test_a_short_read_asks_for_more_and_a_foreign_file_is_refused(self) -> None:
        body, _ = product_file(0)
        with self.assertRaises(cmadust.HeaderTruncated) as raised:
            cmadust.parse_header(body[:100])
        self.assertGreater(raised.exception.needed, 100)
        with self.assertRaisesRegex(DownloadError, "not a NetCDF classic"):
            cmadust.parse_header(b"\x89HDF\r\n\x1a\n" + body[8:200])

    def test_a_header_longer_than_the_first_read_is_read_again(self) -> None:
        body, _ = product_file(0)
        reads: list[ByteRange] = []

        def read(url: str, span: ByteRange) -> bytes:
            reads.append(span)
            return body[span.start : span.end + 1]

        with mock.patch.object(cmadust, "HEAD_BYTES", 64):
            header, head = cmadust.read_header("file", read, len(body))
        self.assertEqual(header.dimensions["lon"], 835)
        self.assertGreater(len(reads), 1)
        self.assertGreaterEqual(len(head), header.length)

    def test_the_series_writer_round_trips(self) -> None:
        latitudes, longitudes = np.array([39.75, 39.9]), np.array([97.3, 97.45, 97.6])
        frames = np.arange(12, dtype=np.float32).reshape(2, 2, 3)
        with TempPath() as root:
            path = root / "s.nc"
            cmadust.write_series(path, "dustsfc", RUN.time, [0, 3], latitudes, longitudes, frames, {"title": "t"})
            data = path.read_bytes()
        header = cmadust.parse_header(data)
        self.assertEqual(header.dimensions, {"time": 2, "latitude": 2, "longitude": 3})
        self.assertEqual(header.variables["time"].attributes["units"], "hours since 2026-10-30 00:00:00")
        self.assertEqual(header.variables["dustsfc"].dimensions, ("time", "latitude", "longitude"))
        self.assertTrue(np.isnan(header.variables["dustsfc"].attributes["_FillValue"][0]))
        self.assertEqual(header.data_end, len(data))
        read = lambda url, span: data[span.start : span.end + 1]  # noqa: E731
        np.testing.assert_array_equal(cmadust.read_variable("s", header, "dustsfc", read), frames)
        np.testing.assert_array_equal(cmadust.read_variable("s", header, "time", read), [0, 3])
        np.testing.assert_array_equal(cmadust.read_variable("s", header, "latitude", read), latitudes)


class TempPath:
    def __enter__(self) -> Path:
        import tempfile

        self._scratch = tempfile.TemporaryDirectory(prefix="xue-cmadust-")
        return Path(self._scratch.name)

    def __exit__(self, *exc: object) -> None:
        self._scratch.cleanup()


class ListingTests(unittest.TestCase):
    def test_the_two_caches_together_make_a_run(self) -> None:
        body, _ = product_file(0)
        bucket = Bucket({"noaa": {0: body, 2: body}, "jma": {1: body, 2: body}})
        objects = cmadust.list_run(RUN, fetch=bucket.listing)
        self.assertEqual(sorted(objects), [0, 1, 2])
        # The Met Office/NOAA cache first where both hold a file.
        self.assertEqual([item.cache for item in objects[2]], ["noaa", "jma"])
        self.assertEqual(objects[1][0].size, len(body))
        self.assertTrue(cmadust.run_is_complete(SPEC, RUN, 2, fetch=bucket.listing))
        self.assertFalse(cmadust.run_is_complete(SPEC, RUN, 3, fetch=bucket.listing))
        self.assertEqual(cmadust.missing_hours(SPEC, 4, objects), [3, 4])
        self.assertTrue(all("list-type=2" in url for url in bucket.listings))

    def test_resolve_run_latest_walks_back_over_the_twelve_hour_cycles(self) -> None:
        now = datetime(2026, 10, 30, 13, 0, tzinfo=UTC)
        with mock.patch.object(
            fetch.cmadust, "run_is_complete", side_effect=lambda spec, run, hours, **kwargs: run.id == "2026103000"
        ) as probe:
            run = resolve_run("latest", hours=168, now=now, model="cmadust")
        self.assertEqual(run.id, "2026103000")
        self.assertEqual([call.args[1].id for call in probe.call_args_list], ["2026103012", "2026103000"])
        with mock.patch.object(fetch.cmadust, "run_is_complete", return_value=False):
            with self.assertRaisesRegex(DownloadError, "incomplete"):
                resolve_run("2026103000", hours=168, model="cmadust")


class FetchTests(TempRoot, unittest.TestCase):
    root_prefix = "xue-cmadust-"

    def run_fetch(self, bucket: Bucket, hours: int = 2, **options: object) -> list[Path]:
        return cmadust.fetch_run(
            SPEC,
            RUN,
            hours,
            self.root,
            force=bool(options.pop("force", False)),
            input_ids=options.pop("input_ids", None),  # type: ignore[arg-type]
            fetch_text=bucket.listing,
            read_range=bucket.read,
        )

    def test_the_fetch_writes_one_series_per_variable_and_a_record(self) -> None:
        files = {hour: product_file(hour) for hour in range(3)}
        bucket = Bucket({"noaa": {0: files[0][0], 2: files[2][0]}, "jma": {1: files[1][0]}})
        paths = self.run_fetch(bucket)
        destination = self.root / "cmadust.2026103000"
        self.assertEqual(paths, [destination / "cmadust.2026103000.dustsfc.nc", destination / "cmadust.2026103000.aoddust.nc"])
        for variable_id, name in (("dustsfc", "sconc_dust"), ("aoddust", "aod550_dust")):
            data = (destination / f"cmadust.2026103000.{variable_id}.nc").read_bytes()
            header = cmadust.parse_header(data)
            self.assertEqual(header.attributes["forecast_reference_time"], "2026-10-30T00:00:00Z")
            self.assertEqual(header.variables[variable_id].attributes["units"], cmadust.SERIES_UNITS[variable_id])
            series = cmadust.read_variable("s", header, variable_id, lambda url, span: data[span.start : span.end + 1])
            # South first, as the file has it.
            latitudes = cmadust.read_variable("s", header, "latitude", lambda url, span: data[span.start : span.end + 1])
            self.assertEqual((latitudes[0], latitudes[-1]), (15.0, 60.0))
            for hour in range(3):
                np.testing.assert_array_equal(series[hour], files[hour][1][name], f"{variable_id} +{hour} h")
        record = json.loads((destination / "fetch.json").read_text())
        self.assertEqual((record["model"], record["run"], record["hours"], record["frames"]), ("cmadust", "2026103000", 2, 3))
        self.assertEqual(record["caches"], {"noaa": 2, "jma": 1})
        # One read of the header and the coordinates behind it, then one per
        # surface field: never the pressure-level fields.
        header = cmadust.parse_header(files[0][0])
        sconc, u = header.variables["sconc_dust"], header.variables["u"]
        self.assertEqual(len(bucket.ranges), 3 * 3)
        self.assertIn(("noaa", sconc.begin, sconc.begin + sconc.vsize - 1), bucket.ranges)
        self.assertEqual(sum(1 for _, start, end in bucket.ranges if (start, end) == (0, cmadust.HEAD_BYTES - 1)), 3)
        self.assertFalse(
            any(start < u.begin + u.vsize and end >= u.begin for _, start, end in bucket.ranges if start),
            "a pressure-level field was read",
        )

    def test_a_lead_time_neither_cache_lists_is_an_incomplete_run(self) -> None:
        body, _ = product_file(0)
        bucket = Bucket({"noaa": {0: body}, "jma": {2: body}})
        with self.assertRaisesRegex(DownloadError, r"incomplete: neither cache lists \+1 h"):
            self.run_fetch(bucket)
        self.assertEqual(bucket.ranges, [])

    def test_a_cut_off_upload_falls_back_to_the_other_cache(self) -> None:
        files = {hour: product_file(hour)[0] for hour in range(3)}
        bucket = Bucket({"noaa": {**files, 1: files[1][:-4096]}, "jma": {1: files[1]}})
        self.run_fetch(bucket)
        record = json.loads((self.root / "cmadust.2026103000" / "fetch.json").read_text())
        self.assertEqual(record["caches"], {"noaa": 2, "jma": 1})
        bucket = Bucket({"noaa": {**files, 1: files[1][:-4096]}})
        with self.assertRaisesRegex(DownloadError, r"\+1 h could not be read from any cache"):
            self.run_fetch(bucket, force=True)

    def test_a_file_off_the_product_s_grid_or_unit_is_refused(self) -> None:
        good = {hour: product_file(hour)[0] for hour in (0, 2)}
        for broken, message in (
            (product_file(1, south=15.15)[0], "lat is not the product's"),
            (product_file(1, units={"sconc_dust": "mg/m3"})[0], "sconc_dust is in mg/m3"),
        ):
            with self.subTest(message=message):
                bucket = Bucket({"noaa": {**good, 1: broken}})
                with self.assertRaisesRegex(DownloadError, message):
                    self.run_fetch(bucket, force=True)

    def test_a_job_fetches_only_its_own_bundles_inputs_and_keeps_what_is_on_disk(self) -> None:
        files = {hour: product_file(hour)[0] for hour in range(3)}
        bucket = Bucket({"noaa": files})
        paths = self.run_fetch(bucket, input_ids=("aoddust",))
        self.assertEqual([path.name for path in paths], ["cmadust.2026103000.aoddust.nc"])
        self.assertEqual(len(bucket.ranges), 3 * 2)
        bucket.ranges.clear()
        self.run_fetch(bucket, input_ids=("aoddust",))
        self.assertEqual(bucket.ranges, [])
        self.run_fetch(bucket, input_ids=("aoddust",), force=True)
        self.assertEqual(len(bucket.ranges), 3 * 2)
        with self.assertRaisesRegex(DownloadError, "publishes"):
            self.run_fetch(bucket, input_ids=("pm10dust",))

    def test_the_dispatch_reaches_the_module(self) -> None:
        with mock.patch.object(fetch.cmadust, "fetch_run", return_value=[]) as fetch_run:
            fetch.fetch_run(RUN, 3, self.root, model="cmadust", input_ids=("dustsfc",))
        self.assertEqual(fetch_run.call_args.kwargs["input_ids"], ("dustsfc",))
        self.assertIs(fetch_run.call_args.kwargs["read_range"], fetch.fetch_range)


@requires_gdal
class SeriesTests(unittest.TestCase):
    def test_the_run_is_the_epoch_of_the_time_axis(self) -> None:
        series = observation.inspect_observation(SERIES_DIR, SPEC)
        self.assertEqual(series.frames[0]["dustsfc"].run_time, RUN.time)
        self.assertEqual(series.lead_seconds, [0, 3600, 7200])

    def test_every_input_is_a_series_of_its_own(self) -> None:
        files = observation.series_files(SERIES_DIR, SPEC.input_variable_ids)
        self.assertEqual({v: path.name for v, path in files.items()}, {v: f"cmadust.2026103000.{v}.nc" for v in BUNDLES})


@requires_gdal
class ConversionTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-cmadust-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                SERIES_DIR,
                cls.root / "out",
                model="cmadust",
                skip_video=True,
                work_root=cls.root / "work",
                run_id="2026103000",
                manifest_path=cls.root / "out" / "manifest.json",
                container=True, zarr=False,
            )

    def bundle(self, name: str):
        return read_bundle(self.root / "out" / f"{name}.xue")

    def series(self, name: str) -> np.ndarray:
        data = (SERIES_DIR / f"cmadust.2026103000.{name}.nc").read_bytes()
        header = cmadust.parse_header(data)
        values = cmadust.read_variable("fixture", header, name, lambda url, span: data[span.start : span.end + 1])
        return np.asarray(values, dtype=np.float64)[:, ::-1]  # north first in the bundle

    def test_the_run_publishes_every_bundle_the_source_declares(self) -> None:
        self.assertEqual(tuple(bundle["variable"] for bundle in self.report["bundles"]), BUNDLES)
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("CMA-CW-DUST", "cma-cw-dust-0p15"))
        self.assertEqual(manifest["runTime"], "2026-10-30T00:00:00Z")
        validate_bin_manifest(manifest, expected_hours=2, require_core_variables=True)
        self.assertFalse((self.root / "out" / "dustsfc.half.xue").exists())

    def test_the_grid_is_the_fixture_s_box_north_up(self) -> None:
        grid = self.bundle("dustsfc").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (WIDTH, HEIGHT))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (97.3, 40.2))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.15, -0.15))
        self.assertEqual(grid["rowOrder"], "north-to-south")
        self.assertFalse(grid["wrapLongitude"])

    def test_the_surface_concentration_carries_its_dust_identity(self) -> None:
        variable = self.bundle("dustsfc").metadata["variables"][0]
        self.assertEqual(variable["unit"], "µg/m³")
        self.assertEqual(variable["parameter"]["parameterCategory"], 20)
        self.assertEqual(variable["parameter"]["parameterNumber"], 0)
        self.assertEqual(
            (variable["aerosol"]["aerosolType"], variable["aerosol"]["typeOfSizeInterval"], variable["aerosol"]["typeOfWavelengthInterval"]),
            (62001, 255, 255),
        )

    def test_the_values_are_the_series(self) -> None:
        for name in BUNDLES:
            codebook = PROFILES["quality"][name]
            values = self.series(name)
            for hour in (0, 2):
                with self.subTest(bundle=name, hour=hour):
                    codes = np.asarray(self.bundle(name).decode_plane(1, hour)).reshape(HEIGHT, WIDTH)
                    np.testing.assert_array_equal(codes, codebook.quantize(values[hour]))

    def test_the_dust_core_reaches_the_top_of_the_scale(self) -> None:
        dust = np.asarray(self.bundle("dustsfc").decode_plane(1, 0)).reshape(HEIGHT, WIDTH)
        self.assertGreater(float(self.series("dustsfc")[0].max()), 10000.0)
        self.assertGreater(int(dust.max()), 200)
        self.assertLess(int(dust.max()), PROFILES["quality"]["dustsfc"].overflow_code)
        # The optical depth's codebook stops at 5; the core is past it.
        depth = np.asarray(self.bundle("aoddust").decode_plane(1, 0)).reshape(HEIGHT, WIDTH)
        self.assertIn(PROFILES["quality"]["aoddust"].overflow_code, depth)

    def test_a_complete_build_wants_the_whole_published_grid(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "835x301 grid"):
                binconvert.convert_bin(
                    SERIES_DIR,
                    self.root / "complete",
                    model="cmadust",
                    skip_video=True,
                    require_complete=True,
                    expected_hours=2,
                    container=True, zarr=False,
                )

    @requires_native_source("cmadust")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, SERIES_DIR, self.root / "out", self.report, self.root / "native", model="cmadust")


if __name__ == "__main__":
    unittest.main()

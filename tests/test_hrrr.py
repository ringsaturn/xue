"""The HRRR source: an hourly cycle, a Lambert conformal grid resampled onto
a regular one, and two records that arrive under another centre's spelling.

Three things are new with it. A source that cycles every hour rather than
every six (``SourceSpec.cycle_hours``, read by ``parse_run`` and
``resolve_run``). A source on a map projection, which the encoder resamples
onto the regular grid the format describes before anything else reads it —
``xuebuild/reproject.py``, whose arithmetic the native encoder repeats step
for step, so the resampled planes are checked here against GDAL's own
georeferencing of the fixture and the two encoders against each other. And
two registry widenings: the sea level pressure HRRR writes as ``MSLMA``
(0/3/198) is accepted under ``prmsl``, and the composite reflectivity it
forecasts (``REFC``, 0/16/196) is published under the radar mosaic's
``cref``.

``tests/fixtures/hrrr.2026091100.f000.crop.grib2`` is a 120 x 120 cell
window of the 2026-09-11 00Z analysis over the Gulf coast, every record the
source fetches in source order, still on the model's own projection.
"""

from __future__ import annotations

import filecmp
import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np

from xuebuild import binconvert, grib2, native, zstdcli
from xuebuild.binconvert import (
    GridInfo,
    _extract_planes,
    _grid_info,
    crop_grid,
    published_bundle_ids,
)
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    HRRR_BASE_URLS,
    _download_hrrr_payload,
    _run_is_complete,
    floor_to_cycle,
    hrrr_object_url,
    model_object_url,
    parse_run,
    resolve_run,
)
from xuebuild.gdal import _band_matches, inspect_grib_multi, raster_expression
from xuebuild.idx import field_byte_range
from xuebuild.model import GfsRun
from xuebuild.reproject import (
    LambertConformal,
    ProjectedGrid,
    Regrid,
    build_resampler,
    lambert_conformal_from_wkt,
)
from xuebuild.sources import source_spec
from xuebuild.variables import variable_spec

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "hrrr.2026091100.f000.crop.grib2"
HRRR = source_spec("hrrr")

# The HRRR CONUS grid as GDAL reports it: 1799 x 1059 cells of 3 km, the
# first cell center at 21.138123 N, 122.719528 W.
CONUS = ProjectedGrid(
    projection=LambertConformal(6371229.0, 38.5, 38.5, 38.5, -97.5),
    width=1799,
    height=1059,
    x0=-2699020.14252193 + 1500.0,
    y0=1588193.8474433357 - 1500.0,
    dx=3000.0,
    dy=3000.0,
)

HRRR_F01_IDX = "\n".join(
    [
        "1:0:d=2026091100:REFC:entire atmosphere:1 hour fcst:",
        "2:296476:d=2026091100:RETOP:cloud top:1 hour fcst:",
        "38:16000000:d=2026091100:MSLMA:mean sea level:1 hour fcst:",
        "39:16626119:d=2026091100:HGT:1000 mb:1 hour fcst:",
        "63:30000000:d=2026091100:TMP:2 m above ground:1 hour fcst:",
        "70:31000000:d=2026091100:PRATE:surface:1 hour fcst:",
        "71:31000188:d=2026091100:APCP:surface:0-1 hour acc fcst:",
        "75:32000000:d=2026091100:TCDC:boundary layer cloud layer:1 hour fcst:",
        "79:33000000:d=2026091100:TCDC:entire atmosphere:1 hour fcst:",
        "80:33910487:d=2026091100:HGT:cloud ceiling:1 hour fcst:",
    ]
)

requires_gdalinfo = unittest.skipUnless(shutil.which("gdalinfo") is not None, "gdalinfo is not on PATH")


def wheel_reads_projections() -> bool:
    """Whether the installed wheel is new enough for a projected source: it
    reports the coordinate system `gdalinfo` does, which is what the
    converter recognises a projection from. An older wheel converts the
    regular grids as before and is skipped here, the way CI sits between a
    version bump and its release."""
    if not native.available() or not hasattr(native.require(), "gdal_info"):
        return False
    return "coordinateSystem" in native.require().gdal_info(str(FIXTURE))


requires_projected_native = unittest.skipUnless(
    wheel_reads_projections(), "the installed xuepy wheel predates projected sources"
)


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_hourly_and_regridded(self) -> None:
        self.assertEqual(HRRR.manifest_model, "HRRR")
        self.assertEqual(HRRR.product, "wrfsfc")
        self.assertEqual(HRRR.latest_filename, "latest-hrrr.json")
        self.assertEqual(HRRR.cycle_hours, 1)
        self.assertEqual(HRRR.horizon_hours, 18)
        self.assertEqual(HRRR.forecast_hours(18), list(range(19)))
        self.assertEqual(HRRR.regrid, Regrid(step=0.03))
        self.assertEqual(HRRR.production_grid, (2441, 1051))
        # The global models keep their six-hourly cycles and 240-hour axes.
        for model in ("gfs", "ecmwf", "sflux"):
            self.assertEqual(source_spec(model).cycle_hours, 6, model)
            self.assertEqual(source_spec(model).horizon_hours, 240, model)
            self.assertIsNone(source_spec(model).regrid, model)

    def test_published_bundles(self) -> None:
        published = published_bundle_ids(HRRR)
        self.assertEqual(published[:2], ("tmp2m", "prate"))
        self.assertIn("prmsl", published)
        self.assertIn("cref", published)
        self.assertEqual(published[-4:], ("wind10m", "wind925", "wind850", "wind250"))
        # What the surface file does not carry is not listed.
        for absent in ("hgt250", "rh850", "spfh850", "aptmp2m", "thetae850", "qflux850", "tmpsfc"):
            self.assertNotIn(absent, published, absent)
        # The fixture carries every input, in source order.
        self.assertEqual(len(HRRR.input_variable_ids), 26)


class CycleTests(unittest.TestCase):
    def test_hourly_cycles_are_parsed_and_floored(self) -> None:
        self.assertEqual(parse_run("2026091107", "hrrr").id, "2026091107")
        with self.assertRaisesRegex(DownloadError, "cycles start at 00, 06, 12, 18 UTC"):
            parse_run("2026091107", "gfs")
        self.assertEqual(parse_run("2026091106", "gfs").id, "2026091106")
        now = datetime(2026, 9, 11, 7, 40, tzinfo=UTC)
        self.assertEqual(floor_to_cycle(now, 1).hour, 7)
        self.assertEqual(floor_to_cycle(now).hour, 6)

    def test_object_url(self) -> None:
        run = GfsRun(datetime(2026, 9, 11, 7, tzinfo=UTC))
        self.assertEqual(
            hrrr_object_url(run, 0),
            f"{HRRR_BASE_URLS[0]}/hrrr.20260911/conus/hrrr.t07z.wrfsfcf00.grib2",
        )
        self.assertTrue(model_object_url(run, 18, "hrrr").endswith("hrrr.t07z.wrfsfcf18.grib2"))
        self.assertEqual(len(HRRR_BASE_URLS), 2)
        self.assertTrue(hrrr_object_url(run, 2, base_url=HRRR_BASE_URLS[1]).startswith(HRRR_BASE_URLS[1]))

    def test_latest_steps_back_one_hour_at_a_time(self) -> None:
        now = datetime(2026, 9, 11, 9, 20, tzinfo=UTC)
        checked: list[str] = []

        def exists(url: str) -> bool:
            checked.append(url)
            return "hrrr.t07z" in url

        run = resolve_run("latest", hours=18, now=now, exists=exists, model="hrrr")
        self.assertEqual(run.id, "2026091107")
        self.assertIn("hrrr.t09z.wrfsfcf00.grib2", checked[0])
        self.assertTrue(any("hrrr.t08z" in url for url in checked))
        # A cycle is complete when one mirror has every hour of it — the
        # hours land out of order, so the ends alone prove nothing.
        self.assertTrue(_run_is_complete(run, 18, "hrrr", exists))
        self.assertFalse(_run_is_complete(GfsRun(datetime(2026, 9, 11, 8, tzinfo=UTC)), 18, "hrrr", exists))
        probed: list[str] = []

        def one_hour_short(url: str) -> bool:
            probed.append(url)
            return "wrfsfcf02" not in url or HRRR_BASE_URLS[1] in url

        self.assertTrue(_run_is_complete(run, 18, "hrrr", one_hour_short))
        self.assertTrue(any(HRRR_BASE_URLS[1] in url for url in probed))
        self.assertFalse(_run_is_complete(run, 18, "hrrr", lambda url: "wrfsfcf02" not in url))
        with self.assertRaisesRegex(DownloadError, "not on the HRRR axis"):
            resolve_run("latest", hours=24, now=now, exists=exists, model="hrrr")


class MirrorFallbackTests(unittest.TestCase):
    def test_a_frame_comes_from_the_first_mirror_that_has_it(self) -> None:
        import urllib.error

        run = GfsRun(datetime(2026, 9, 12, 6, tzinfo=UTC))
        served: list[str] = []

        def fetch_text(url: str) -> str:
            served.append(url)
            if url.startswith(HRRR_BASE_URLS[0]):
                raise DownloadError("request failed") from urllib.error.HTTPError(url, 404, "Not Found", None, None)  # type: ignore[arg-type]
            return HRRR_F01_IDX

        with mock.patch("xuebuild.fetch.fetch_text", fetch_text), mock.patch(
            "xuebuild.fetch.fetch_range", lambda url, byte_range: b"x" * byte_range.length
        ):
            payload = _download_hrrr_payload(run, 1, HRRR, ("tmp2m", "prmsl"))
        self.assertEqual(len(payload), 1000000 + 626119)
        self.assertEqual(len(served), 2)
        self.assertTrue(served[1].startswith(HRRR_BASE_URLS[1]))
        with mock.patch(
            "xuebuild.fetch.fetch_text",
            mock.Mock(side_effect=DownloadError("request failed") ),
        ), self.assertRaises(DownloadError):
            _download_hrrr_payload(run, 1, HRRR, ("tmp2m",))


class RecordMatchingTests(unittest.TestCase):
    def test_the_index_falls_back_to_the_alternate_spelling(self) -> None:
        prmsl = variable_spec("prmsl")
        self.assertEqual(prmsl.alternate_index_fields, (":MSLMA:mean sea level:",))
        byte_range = field_byte_range(
            HRRR_F01_IDX, prmsl.index_field, alternate_fields=prmsl.alternate_index_fields
        )
        self.assertEqual((byte_range.start, byte_range.end), (16000000, 16626118))
        with self.assertRaises(DownloadError):
            field_byte_range(HRRR_F01_IDX, prmsl.index_field)
        cref = variable_spec("cref")
        self.assertEqual(field_byte_range(HRRR_F01_IDX, cref.index_field).start, 0)
        # The boundary-layer TCDC never matches the entire-atmosphere phrase.
        tcdc = variable_spec("tcdc")
        self.assertEqual(
            field_byte_range(HRRR_F01_IDX, tcdc.index_field, excluded_phrases=tcdc.excluded_index_phrases).start,
            33000000,
        )

    def test_the_aliases_are_matched_and_never_written(self) -> None:
        prmsl = variable_spec("prmsl")
        self.assertIn((0, 3, 198), prmsl.grib2_aliases)
        self.assertEqual(prmsl.parameter_metadata()["parameterNumber"], 1)
        cref = variable_spec("cref")
        self.assertEqual(cref.grib2_aliases, ((0, 16, 196),))
        self.assertEqual(cref.parameter_metadata()["parameterNumber"], 5)
        self.assertEqual(cref.index_field, ":REFC:entire atmosphere:")
        self.assertEqual(raster_expression("cref", "dB"), raster_expression("cref", "dBZ"))
        with self.assertRaises(ConversionError):
            raster_expression("cref", "mm")
        mslma = {"GRIB_ELEMENT": "MSLMA", "GRIB_SHORT_NAME": "0-MSL", "GRIB_COMMENT": "MSLP (MAPS System Reduction) [Pa]"}
        self.assertTrue(_band_matches("prmsl", mslma, '0[-] MSL="Mean sea level"'))
        refc = {"GRIB_ELEMENT": "REFC", "GRIB_SHORT_NAME": "0-EATM", "GRIB_COMMENT": "Maximum / Composite radar reflectivity [dB]"}
        self.assertTrue(_band_matches("cref", refc, '0[-] EATM="Entire Atmosphere"'))
        self.assertFalse(_band_matches("tcdc", refc, '0[-] EATM="Entire Atmosphere"'))

    def test_every_input_is_found_in_the_fixture_both_ways(self) -> None:
        fast = grib2.inspect_grib_fast(FIXTURE, HRRR.input_variable_ids)
        self.assertEqual(tuple(fast), HRRR.input_variable_ids)
        self.assertEqual([frame.band for frame in fast.values()], list(range(1, 27)))
        self.assertEqual({frame.lead_seconds for frame in fast.values()}, {0})
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            if shutil.which("gdalinfo") is None:
                self.skipTest("gdalinfo is not on PATH")
            slow = inspect_grib_multi(FIXTURE, HRRR.input_variable_ids)
        for variable_id, frame in fast.items():
            self.assertEqual(frame, slow[variable_id], variable_id)
        self.assertEqual(slow["prmsl"].unit, "Pa")
        self.assertEqual(slow["cref"].unit, "dB")


class ProjectionTests(unittest.TestCase):
    def test_the_first_cell_projects_where_gdal_puts_it(self) -> None:
        x, y = CONUS.projection.forward(-122.719528, 21.138123)
        self.assertAlmostEqual(x, CONUS.x0, places=6)
        self.assertAlmostEqual(y, CONUS.y0 - 1058 * CONUS.dy, places=6)
        longitude, latitude = CONUS.projection.inverse(x, y)
        self.assertAlmostEqual(longitude, -122.719528, places=9)
        self.assertAlmostEqual(latitude, 21.138123, places=9)

    def test_the_production_grid_is_the_footprint_at_three_hundredths(self) -> None:
        west, south, east, north = CONUS.footprint()
        # The longitude extremes sit on the northern corners, the latitude
        # extremes at the middle of the top and the corners of the bottom.
        self.assertAlmostEqual(west, -134.0955, places=4)
        self.assertAlmostEqual(east, -60.9172, places=4)
        self.assertAlmostEqual(south, 21.1381, places=4)
        self.assertAlmostEqual(north, 52.6157, places=4)
        resampler = build_resampler(CONUS, HRRR.regrid)
        self.assertEqual((resampler.width, resampler.height), HRRR.production_grid)
        self.assertEqual((resampler.first_longitude, resampler.first_latitude), (-134.1, 52.62))
        self.assertEqual(resampler.column.shape, (1051, 2441))
        self.assertTrue(((resampler.fx >= 0) & (resampler.fx <= 1)).all())
        self.assertTrue(((resampler.fy >= 0) & (resampler.fy <= 1)).all())

    def test_a_constant_plane_stays_constant_and_a_linear_one_is_exact(self) -> None:
        small = ProjectedGrid(CONUS.projection, 40, 30, CONUS.x0, CONUS.y0, CONUS.dx, CONUS.dy)
        resampler = build_resampler(small, Regrid(step=0.05))
        constant = np.full((30, 40), 7.5)
        np.testing.assert_allclose(resampler.take(constant), 7.5, rtol=0, atol=1e-12)
        rows, columns = np.mgrid[0:30, 0:40]
        linear = 2.0 * columns + 0.5 * rows
        expected = 2.0 * (resampler.column + resampler.fx) + 0.5 * (resampler.row + resampler.fy)
        np.testing.assert_allclose(resampler.take(linear), expected, rtol=0, atol=1e-9)
        # Beyond the source the sampling coordinate is clamped, so every
        # target cell — the rectangle's corners the cone never covered
        # included — reads within the source.
        self.assertTrue((resampler.column >= 0).all() and (resampler.column <= 38).all())
        self.assertTrue((resampler.row >= 0).all() and (resampler.row <= 28).all())
        self.assertTrue((resampler.column == 0).any() and (resampler.column == 38).any())
        with self.assertRaises(ConversionError):
            resampler.take(np.zeros((31, 40)))

    def test_wkt_is_read_and_a_geographic_one_is_not_a_projection(self) -> None:
        self.assertIsNone(lambert_conformal_from_wkt(""))
        self.assertIsNone(lambert_conformal_from_wkt('GEOGCRS["Coordinate System imported from GRIB file"]'))
        self.assertIsNone(lambert_conformal_from_wkt('GEOGCS["WGS 84"]'))
        with self.assertRaisesRegex(ConversionError, "unsupported map projection"):
            lambert_conformal_from_wkt('PROJCRS["x",CONVERSION["Polar Stereographic"]]')


@requires_gdalinfo
class FixtureGridTests(unittest.TestCase):
    """The fixture through the reference pipeline, against GDAL's own view
    of where its cells are."""

    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.grid = _grid_info(FIXTURE, HRRR)
        cls.info = json.loads(subprocess.run(["gdalinfo", "-json", str(FIXTURE)], capture_output=True, text=True, check=True).stdout)

    def test_the_projection_is_read_from_the_file(self) -> None:
        resampler = self.grid.resample
        self.assertIsNotNone(resampler)
        self.assertEqual(resampler.source.projection, CONUS.projection)
        self.assertEqual((resampler.source.width, resampler.source.height), (120, 120))
        self.assertEqual(self.grid.source_shape, (120, 120))
        self.assertFalse(self.grid.wraps)
        self.assertEqual((self.grid.longitude_step, self.grid.latitude_step), (0.03, -0.03))

    def test_the_grid_covers_the_footprint_gdal_reports(self) -> None:
        # gdalinfo's wgs84Extent is the cell *edges*; the grid is built from
        # the centers, half a cell inside, and snapped outwards to the step.
        ring = self.info["wgs84Extent"]["coordinates"][0]
        west = min(point[0] for point in ring)
        east = max(point[0] for point in ring)
        south = min(point[1] for point in ring)
        north = max(point[1] for point in ring)
        self.assertLessEqual(self.grid.first_longitude, west + 0.03)
        self.assertGreaterEqual(self.grid.first_longitude, west - 0.03)
        self.assertLessEqual(self.grid.first_latitude, north + 0.03)
        self.assertGreaterEqual(self.grid.first_latitude, north - 0.03)
        last_longitude = self.grid.first_longitude + (self.grid.width - 1) * 0.03
        last_latitude = self.grid.first_latitude - (self.grid.height - 1) * 0.03
        self.assertAlmostEqual(last_longitude, east, delta=0.03)
        self.assertAlmostEqual(last_latitude, south, delta=0.03)
        self.assertEqual((self.grid.width, self.grid.height), (137, 120))

    def test_a_resampled_plane_matches_gdal_at_the_cell_centers(self) -> None:
        # Each target cell whose center GDAL can locate inside the source
        # reads, to interpolation, the source value there: the resampler's
        # own coordinates are checked against GDAL's forward projection of
        # the same centers (through the CLI's `gdaltransform`).
        if shutil.which("gdaltransform") is None:
            self.skipTest("gdaltransform is not on PATH")
        resampler = self.grid.resample
        samples = [(10, 20), (60, 60), (119, 100), (0, 136)]
        points = "\n".join(
            f"{self.grid.first_longitude + column * 0.03} {self.grid.first_latitude - row * 0.03}"
            for row, column in samples
        )
        projected = subprocess.run(
            ["gdaltransform", "-s_srs", "EPSG:4326", "-t_srs", self.info["coordinateSystem"]["wkt"], "-output_xy"],
            input=points,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        transform = self.info["geoTransform"]
        for (row, column), x, y in zip(samples, projected[0::2], projected[1::2]):
            source_column = (float(x) - transform[0]) / transform[1] - 0.5
            source_row = (float(y) - transform[3]) / transform[5] - 0.5
            got_column = resampler.column[row, column] + resampler.fx[row, column]
            got_row = resampler.row[row, column] + resampler.fy[row, column]
            self.assertAlmostEqual(got_column, min(max(source_column, 0.0), 119.0), places=4, msg=(row, column))
            self.assertAlmostEqual(got_row, min(max(source_row, 0.0), 119.0), places=4, msg=(row, column))

    def test_planes_are_resampled_then_cropped(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}), tempfile.TemporaryDirectory() as work:
            frames = inspect_grib_multi(FIXTURE, ("tmp2m", "cref", "prmsl"))
            planes = _extract_planes(frames, self.grid, Path(work))
            self.assertEqual(planes["tmp2m"].shape, (137 * 120,))
            # Air temperature over the Gulf in September, in Celsius.
            self.assertTrue(((planes["tmp2m"] > 10) & (planes["tmp2m"] < 40)).all())
            self.assertTrue((planes["cref"] < 80).all())
            self.assertTrue(((planes["prmsl"] > 990) & (planes["prmsl"] < 1040)).all())
            cropped = crop_grid(self.grid, (-86.0, 28.0, -85.0, 29.0))
            self.assertIs(cropped.resample, self.grid.resample)
            self.assertEqual(cropped.source_shape, (120, 120))
            window = _extract_planes({"tmp2m": frames["tmp2m"]}, cropped, Path(work))["tmp2m"]
            self.assertEqual(window.size, cropped.width * cropped.height)
            full = planes["tmp2m"].reshape(120, 137)
            crop = cropped.crop
            np.testing.assert_array_equal(
                window.reshape(cropped.height, cropped.width),
                full[crop.row_start : crop.row_start + crop.height, crop.column_start : crop.column_start + crop.width],
            )

    def test_the_origin_survives_a_gdal_releases_worth_of_noise(self) -> None:
        # GDAL places a projected grid by projecting the record's first
        # point, and two releases land that double a few nanometres apart
        # (the wheel's 3.13 against Ubuntu's system GDAL, seen on CI). Every
        # resampled coordinate descends from the origin, so two GDALs must
        # read one grid: the origin is taken to the millimetre.
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            original = dict(self.info)
            noisy = dict(original)
            noisy["geoTransform"] = [
                original["geoTransform"][0] + 2.5e-9,
                original["geoTransform"][1],
                0.0,
                original["geoTransform"][3] - 3e-9,
                0.0,
                original["geoTransform"][5],
            ]
            with mock.patch("xuebuild.binconvert.dataset_info", return_value=noisy):
                perturbed = _grid_info(FIXTURE, HRRR)
        self.assertEqual(perturbed.resample.source, self.grid.resample.source)
        np.testing.assert_array_equal(perturbed.resample.fx, self.grid.resample.fx)
        np.testing.assert_array_equal(perturbed.resample.column, self.grid.resample.column)

    def test_a_projected_file_needs_a_regridded_source_and_vice_versa(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "map projection, which this source does not declare"):
                _grid_info(FIXTURE, source_spec("gfs"))
            with self.assertRaisesRegex(ConversionError, "regular grid, but this source declares a projected one"):
                _grid_info(FIXTURE.with_name("gfs.2026081406.f000.crop.grib2"), HRRR)


@requires_gdalinfo
class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-hrrr-"))
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                FIXTURE,
                cls.root / "out",
                model="hrrr",
                skip_video=True,
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_every_published_bundle_is_written_on_the_regular_grid(self) -> None:
        variables = [bundle["variable"] for bundle in self.report["bundles"]]
        self.assertEqual(tuple(variables), published_bundle_ids(HRRR))
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("HRRR", "wrfsfc"))
        bundle = read_bundle(self.root / "out" / "cref.xue")
        grid = bundle.metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (137, 120))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.03, -0.03))
        self.assertFalse(grid["wrapLongitude"])
        # The projection is the encoder's business: the bundle carries an
        # ordinary grid block and the mosaic's identity for the reflectivity.
        self.assertNotIn("projection", grid)
        parameter = bundle.metadata["variables"][0]["parameter"]
        self.assertEqual((parameter["parameterCategory"], parameter["parameterNumber"]), (16, 5))
        self.assertEqual(bundle.metadata["variables"][0]["unit"], "dBZ")
        wind = read_bundle(self.root / "out" / "wind10m.xue")
        self.assertEqual([variable["id"] for variable in wind.metadata["variables"]], ["ugrd10m", "vgrd10m"])

    @requires_projected_native
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI")
        subject = self.root / "native"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            report = native.convert_bin(FIXTURE, subject, model="hrrr", skip_video=True, manifest_path=subject / "manifest.json")
        if report["zstdVersion"] != self.report["zstdVersion"]:
            self.skipTest("libzstd differs between the reference and the wheel")
        names = sorted(path.name for path in (self.root / "out").iterdir())
        self.assertEqual(sorted(path.name for path in subject.iterdir()), names)
        for name in names:
            with self.subTest(artifact=name):
                self.assertTrue(filecmp.cmp(self.root / "out" / name, subject / name, shallow=False), name)


class DecimationTests(unittest.TestCase):
    def test_the_half_grid_forgets_the_resampler(self) -> None:
        resampler = build_resampler(ProjectedGrid(CONUS.projection, 40, 30, CONUS.x0, CONUS.y0, 3000.0, 3000.0), Regrid(0.05))
        grid = GridInfo(
            width=resampler.width,
            height=resampler.height,
            first_longitude=resampler.first_longitude,
            first_latitude=resampler.first_latitude,
            longitude_step=0.05,
            latitude_step=-0.05,
            resample=resampler,
        )
        half = grid.decimated()
        self.assertIsNone(half.resample)
        self.assertEqual(half.width, math.ceil(grid.width / 2))
        self.assertEqual(half.longitude_step, 0.1)

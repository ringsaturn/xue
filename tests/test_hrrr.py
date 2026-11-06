"""The HRRR source: an hourly cycle, a Lambert conformal grid published as the
model computes it, and two records that arrive under another centre's spelling.

Three things are new with it. A source that cycles every hour rather than
every six (``SourceSpec.cycle_hours``, read by ``parse_run`` and
``resolve_run``). A source on a map projection, whose grid the bundle declares
in place of the regular fields and whose grid-relative winds are turned to east
and north before they are quantized: ``xuebuild/reproject.py``, whose arithmetic
the native encoder repeats step for step, so the cells are checked here against
GDAL's own georeferencing of the fixture and the two encoders against each
other. And two registry widenings: the sea level pressure HRRR writes as
``MSLMA`` (0/3/198) is accepted under ``prmsl``, and the composite reflectivity
it forecasts (``REFC``, 0/16/196) is published under the radar mosaic's ``cref``.

``tests/fixtures/hrrr.2026091100.f000.crop.grib2`` is a 120 x 120 cell window of
the 2026-09-11 00Z analysis over the Gulf coast, every record the source fetches
in source order, still on the model's own projection.
"""

from __future__ import annotations

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

from tests._support import ClassTempRoot, FIXTURES, assert_native_matches, requires_gdalinfo, requires_native, stored_bundle
from xuebuild import binconvert, grib2
from xuebuild.bundles import VECTOR_BUNDLES
from xuebuild.binconvert import _extract_planes, _grid_relative_vectors
from xuebuild.discover import published_bundle_ids
from xuebuild.grid import GridInfo, crop_grid, grid_info
from xuebuild.ladder import decimate_codes
from xuebuild.metadata import build_metadata
from xuebuild.quantize import PROFILES
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
    lambert_conformal_from_wkt,
    rotate_winds,
    wind_rotation,
)
from xuebuild.sources import source_spec
from xuebuild.variables import variable_spec

FIXTURE = FIXTURES / "hrrr.2026091100.f000.crop.grib2"
HRRR = source_spec("hrrr")

# The HRRR CONUS grid as GDAL reports it: 1799 x 1059 cells of 3 km, the
# first cell center at 21.138123 N, 122.719528 W.
CONUS = ProjectedGrid(
    projection=LambertConformal(6371229.0, 38.5, 38.5, 38.5, -97.5),
    width=1799,
    height=1059,
    x0=-2699020.143 + 1500.0,
    y0=1588193.847 - 1500.0,
    dx=3000.0,
    dy=-3000.0,
)
CONUS_GRID = GridInfo.projected(CONUS)

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


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_hourly_and_on_its_own_grid(self) -> None:
        self.assertEqual(HRRR.cycle_hours, 1)
        self.assertEqual(HRRR.horizon_hours, 18)
        self.assertEqual(HRRR.forecast_hours(18), list(range(19)))
        self.assertTrue(HRRR.grid_relative_winds)
        self.assertEqual(HRRR.production_grid, (1799, 1059))
        self.assertEqual(HRRR.tile, (64, 64))
        # The global models keep their six-hourly cycles and 240-hour axes.
        for model in ("gfs", "ecmwf", "sflux"):
            self.assertEqual(source_spec(model).cycle_hours, 6, model)
            self.assertEqual(source_spec(model).horizon_hours, 240, model)
            self.assertFalse(source_spec(model).grid_relative_winds, model)

    def test_published_bundles(self) -> None:
        published = published_bundle_ids(HRRR)
        self.assertEqual(published[:2], ("tmp2m", "prate"))
        self.assertIn("prmsl", published)
        self.assertIn("cref", published)
        self.assertIn("orog", published)
        self.assertEqual(published[-5:], ("wind10m", "wind80m", "wind925", "wind850", "wind250"))
        # What the surface file does not carry is not listed.
        for absent in ("hgt250", "rh850", "spfh850", "aptmp2m", "thetae850", "qflux850", "tmpsfc"):
            self.assertNotIn(absent, published, absent)
        # The fixture carries every input, in source order.
        self.assertEqual(len(HRRR.input_variable_ids), 31)
        self.assertEqual(
            HRRR.input_variable_ids[2:6], ("ugrd10m", "vgrd10m", "ugrd80m", "vgrd80m")
        )

    def test_the_80_m_wind_is_a_vector_bundle_with_the_10_m_codebook(self) -> None:
        from xuebuild.quantize import PROFILES

        self.assertEqual(VECTOR_BUNDLES["wind80m"], ("ugrd80m", "vgrd80m"))
        for component, number in (("ugrd80m", 2), ("vgrd80m", 3)):
            spec = variable_spec(component)
            self.assertEqual(spec.index_field, f":{component[0].upper()}GRD:80 m above ground:")
            self.assertEqual((spec.grib2_category, spec.grib2_number), (2, number))
            self.assertEqual((spec.grib2_level_type, spec.grib2_level_value), (103, 80.0))
            self.assertEqual(spec.value_range, (-64, 64))
            for profile in PROFILES.values():
                self.assertIs(profile[component], profile["ugrd10m"], component)
        self.assertEqual(raster_expression(variable_spec("ugrd80m"), "m/s"), raster_expression(variable_spec("ugrd10m"), "m/s"))
        eighty = {"GRIB_ELEMENT": "UGRD", "GRIB_SHORT_NAME": "80-HTGL", "GRIB_COMMENT": "u-component of wind [m/s]"}
        self.assertTrue(_band_matches("ugrd80m", eighty, '80[m] HTGL="Specified height level above ground"'))
        self.assertFalse(_band_matches("vgrd80m", eighty, '80[m] HTGL="Specified height level above ground"'))
        self.assertFalse(_band_matches("ugrd10m", eighty, '80[m] HTGL="Specified height level above ground"'))
        self.assertFalse(_band_matches("ugrd100m", eighty, '80[m] HTGL="Specified height level above ground"'))

    def test_the_series_companions(self) -> None:
        self.assertEqual(HRRR.series_bundle_ids, ("tmp2m", "wind10m", "gust", "tcdc", "wind80m"))
        self.assertTrue(set(HRRR.series_bundle_ids) <= set(published_bundle_ids(HRRR)))


class CycleTests(unittest.TestCase):
    def test_the_synoptic_cycles_reach_f048_on_request(self) -> None:
        # The published axis stays at 18 hours; only a build that names a
        # synoptic cycle may ask for the 48 it runs to.
        self.assertEqual(HRRR.horizon_hours, 18)
        self.assertEqual(HRRR.forecast_hours(48, cycle=12), list(range(49)))
        for cycle in (0, 6, 18):
            self.assertEqual(len(HRRR.forecast_hours(48, cycle=cycle)), 49, cycle)
        with self.assertRaisesRegex(DownloadError, "not on the HRRR axis"):
            HRRR.forecast_hours(48, cycle=3)
        with self.assertRaisesRegex(DownloadError, "not on the HRRR axis"):
            HRRR.forecast_hours(48)
        with self.assertRaises(DownloadError):
            HRRR.forecast_hours(49, cycle=12)
        for cycle in (None, 3, 12):
            self.assertEqual(HRRR.forecast_hours(18, cycle=cycle), list(range(19)), cycle)
            self.assertEqual(HRRR.forecast_hours(5, cycle=cycle), list(range(6)), cycle)
        # A source without long cycles is unchanged whatever the cycle.
        with self.assertRaises(DownloadError):
            source_spec("gfs").forecast_hours(243, cycle=0)

    def test_a_named_synoptic_run_resolves_to_f048(self) -> None:
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return True

        run = resolve_run("2026091112", hours=48, exists=exists, model="hrrr")
        self.assertEqual(run.id, "2026091112")
        self.assertTrue(any("wrfsfcf48" in url for url in probed))
        with self.assertRaisesRegex(DownloadError, "not on the HRRR axis"):
            resolve_run("2026091103", hours=48, exists=exists, model="hrrr")
        with self.assertRaisesRegex(DownloadError, "not on the HRRR axis"):
            resolve_run("latest", hours=48, exists=exists, model="hrrr")

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
        # The GRIB of an hour can land minutes before the index the build
        # reads: the hour is not there until both are.
        self.assertFalse(_run_is_complete(run, 18, "hrrr", lambda url: not url.endswith("wrfsfcf06.grib2.idx")))
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


    def test_a_truncated_copy_falls_through_to_the_next_mirror(self) -> None:
        import urllib.error

        run = GfsRun(datetime(2026, 10, 22, 17, tzinfo=UTC))
        ranged: list[str] = []

        def fetch_range(url: str, byte_range: object) -> bytes:
            ranged.append(url)
            if url.startswith(HRRR_BASE_URLS[0]):
                raise DownloadError("request failed") from urllib.error.HTTPError(
                    url, 416, "Requested range not satisfiable", None, None  # type: ignore[arg-type]
                )
            return b"x" * byte_range.length  # type: ignore[attr-defined]

        with mock.patch("xuebuild.fetch.fetch_text", lambda url: HRRR_F01_IDX), mock.patch(
            "xuebuild.fetch.fetch_range", fetch_range
        ):
            payload = _download_hrrr_payload(run, 1, HRRR, ("tmp2m",))
        self.assertEqual(len(payload), 1000000)
        self.assertTrue(ranged[-1].startswith(HRRR_BASE_URLS[1]))


class RecordMatchingTests(unittest.TestCase):
    def test_the_index_falls_back_to_the_alternate_spelling(self) -> None:
        prmsl = variable_spec("prmsl")
        self.assertEqual(prmsl.alternate_index_fields, (":MSLMA:mean sea level:", ":MSLET:mean sea level:"))
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
        self.assertEqual(raster_expression(variable_spec("cref"), "dB"), raster_expression(variable_spec("cref"), "dBZ"))
        with self.assertRaises(ConversionError):
            raster_expression(variable_spec("cref"), "mm")
        mslma = {"GRIB_ELEMENT": "MSLMA", "GRIB_SHORT_NAME": "0-MSL", "GRIB_COMMENT": "MSLP (MAPS System Reduction) [Pa]"}
        self.assertTrue(_band_matches("prmsl", mslma, '0[-] MSL="Mean sea level"'))
        refc = {"GRIB_ELEMENT": "REFC", "GRIB_SHORT_NAME": "0-EATM", "GRIB_COMMENT": "Maximum / Composite radar reflectivity [dB]"}
        self.assertTrue(_band_matches("cref", refc, '0[-] EATM="Entire Atmosphere"'))
        self.assertFalse(_band_matches("tcdc", refc, '0[-] EATM="Entire Atmosphere"'))

    def test_every_input_is_found_in_the_fixture_both_ways(self) -> None:
        fast = grib2.inspect_grib_fast(FIXTURE, HRRR.input_variable_ids)
        self.assertEqual(tuple(fast), HRRR.input_variable_ids)
        self.assertEqual([frame.band for frame in fast.values()], list(range(1, 32)))
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
    def test_the_south_west_cell_projects_where_gdal_puts_it(self) -> None:
        x, y = CONUS.projection.forward(-122.719528, 21.138123)
        self.assertAlmostEqual(x, CONUS.x0, delta=0.1)
        self.assertAlmostEqual(y, CONUS.y0 + 1058 * CONUS.dy, delta=0.1)
        longitude, latitude = CONUS.projection.inverse(x, y)
        self.assertAlmostEqual(longitude, -122.719528, places=9)
        self.assertAlmostEqual(latitude, 21.138123, places=9)

    def test_the_footprint_is_the_domains(self) -> None:
        west, south, east, north = CONUS.footprint()
        # The longitude extremes sit on the northern corners, the latitude
        # extremes at the middle of the top and the corners of the bottom.
        self.assertAlmostEqual(west, -134.0955, places=4)
        self.assertAlmostEqual(east, -60.9172, places=4)
        self.assertAlmostEqual(south, 21.1381, places=4)
        self.assertAlmostEqual(north, 52.6157, places=4)

    def test_a_southern_cone_inverts_onto_itself(self) -> None:
        south = LambertConformal(6371229.0, -30.0, -60.0, -45.0, 140.0)
        self.assertLess(south.cone_constant, 0.0)
        x, y = south.forward(150.0, -40.0)
        longitude, latitude = south.inverse(x, y)
        self.assertAlmostEqual(longitude, 150.0, places=9)
        self.assertAlmostEqual(latitude, -40.0, places=9)

    def test_a_wrapped_longitude_lands_on_the_same_point(self) -> None:
        self.assertEqual(CONUS.projection.forward_wrapped(262.5, 38.5), CONUS.projection.forward(-97.5, 38.5))
        self.assertEqual(CONUS.projection.forward_wrapped(620.0, 40.0), CONUS.projection.forward(-100.0, 40.0))

    def test_the_wkt_reads_back_as_the_projection(self) -> None:
        self.assertEqual(lambert_conformal_from_wkt(CONUS.projection.wkt()), CONUS.projection)

    def test_the_metadata_block_reads_back_as_the_grid(self) -> None:
        self.assertEqual(ProjectedGrid.from_metadata(CONUS.metadata(), 1799, 1059), CONUS)

    def test_wkt_is_read_and_a_geographic_one_is_not_a_projection(self) -> None:
        self.assertIsNone(lambert_conformal_from_wkt(""))
        self.assertIsNone(lambert_conformal_from_wkt('GEOGCRS["Coordinate System imported from GRIB file"]'))
        self.assertIsNone(lambert_conformal_from_wkt('GEOGCS["WGS 84"]'))
        with self.assertRaisesRegex(ConversionError, "unsupported map projection"):
            lambert_conformal_from_wkt('PROJCRS["x",CONVERSION["Polar Stereographic"]]')


class GridBlockTests(unittest.TestCase):
    def test_the_grid_block_declares_the_projection_alone(self) -> None:
        block = CONUS_GRID.metadata()
        self.assertEqual(
            list(block), ["width", "height", "layout", "rowOrder", "columnOrder", "wrapLongitude", "projection"]
        )
        self.assertEqual((block["width"], block["height"], block["wrapLongitude"]), (1799, 1059, False))
        self.assertEqual(
            block["projection"],
            {
                "kind": "lambert-conformal",
                "radius": 6371229.0,
                "standardParallel1": 38.5,
                "standardParallel2": 38.5,
                "latitudeOfOrigin": 38.5,
                "centralMeridian": -97.5,
                "x0": CONUS.x0,
                "y0": CONUS.y0,
                "dx": 3000.0,
                "dy": -3000.0,
            },
        )

    def test_only_a_projected_grid_is_version_4(self) -> None:
        run_time = datetime(2026, 9, 11, tzinfo=UTC)
        self.assertEqual(build_metadata(run_time, [0], CONUS_GRID, "quality", ("tmp2m",), source=HRRR)["schemaVersion"], 4)
        regular = GridInfo(4, 3, -180.0, 90.0, 90.0, -90.0)
        self.assertEqual(build_metadata(run_time, [0], regular, "quality", ("tmp2m",))["schemaVersion"], 3)

    def test_the_half_grid_keeps_its_first_cell_and_doubles_its_steps(self) -> None:
        half = CONUS_GRID.decimated()
        self.assertEqual((half.width, half.height), (900, 530))
        self.assertEqual((half.projection.x0, half.projection.y0), (CONUS.x0, CONUS.y0))
        self.assertEqual((half.projection.dx, half.projection.dy), (6000.0, -6000.0))
        self.assertEqual(half.projection.height, half.height)

    def test_the_grid_cuts_into_the_tiles_the_comment_says(self) -> None:
        self.assertEqual((-(-1799 // HRRR.tile[0]), -(-1059 // HRRR.tile[1])), (29, 17))


class CropTests(unittest.TestCase):
    def test_a_crop_adds_its_offset_and_covers_the_box(self) -> None:
        box = (-105.0, 35.0, -95.0, 42.0)
        cropped = crop_grid(CONUS_GRID, box)
        crop, window = cropped.crop, cropped.projection
        self.assertEqual((cropped.width, cropped.height), (crop.width, crop.height))
        self.assertEqual(cropped.source_shape, (1059, 1799))
        self.assertEqual(window.x0, CONUS.x0 + crop.column_start * CONUS.dx)
        self.assertEqual(window.y0, CONUS.y0 + crop.row_start * CONUS.dy)
        self.assertEqual((window.dx, window.dy), (CONUS.dx, CONUS.dy))
        # Every point of the box is inside the window, the corners and the
        # middle of the northern edge, which bows past them on the cone.
        for longitude in np.linspace(box[0], box[2], 41):
            for latitude in np.linspace(box[1], box[3], 41):
                x, y = CONUS.projection.forward(float(longitude), float(latitude))
                self.assertLessEqual(window.x0, x)
                self.assertLessEqual(x, window.x0 + (window.width - 1) * window.dx)
                self.assertGreaterEqual(window.y0, y)
                self.assertGreaterEqual(y, window.y0 + (window.height - 1) * window.dy)
        # And it holds nothing it does not need: a cell off each side.
        self.assertLess(window.width, 400)
        self.assertLess(window.height, 400)

    def test_a_box_past_the_domain_is_clamped_to_it(self) -> None:
        cropped = crop_grid(CONUS_GRID, (-170.0, -89.9, -20.0, 89.9))
        self.assertEqual((cropped.width, cropped.height), (1799, 1059))
        self.assertEqual((cropped.projection.x0, cropped.projection.y0), (CONUS.x0, CONUS.y0))

    def test_a_box_with_a_pole_in_it_stays_finite(self) -> None:
        cropped = crop_grid(CONUS_GRID, (-120.0, -90.0, -80.0, 90.0))
        self.assertTrue(math.isfinite(cropped.projection.x0))

    def test_a_box_off_the_grid_is_refused(self) -> None:
        with self.assertRaisesRegex(ConversionError, "does not overlap"):
            crop_grid(CONUS_GRID, (130.0, 30.0, 140.0, 40.0))

    def test_a_box_across_the_antimeridian_is_refused(self) -> None:
        with self.assertRaisesRegex(ConversionError, "antimeridian"):
            crop_grid(CONUS_GRID, (170.0, 30.0, -170.0, 40.0))

    def test_a_window_decimates_like_a_grid(self) -> None:
        cropped = crop_grid(CONUS_GRID, (-105.0, 35.0, -95.0, 42.0))
        half = cropped.decimated()
        self.assertIsNone(half.crop)
        self.assertEqual((half.projection.x0, half.projection.y0), (cropped.projection.x0, cropped.projection.y0))
        self.assertEqual(half.width, (cropped.width + 1) // 2)


class WindRotationTests(unittest.TestCase):
    """``u_e = cos α u + sin α v``, ``v_e = −sin α u + cos α v``, with ``α`` the
    cone constant times the longitude's distance from the central meridian."""

    @staticmethod
    def cell(longitude: float, latitude: float) -> ProjectedGrid:
        x, y = CONUS.projection.forward(longitude, latitude)
        return ProjectedGrid(CONUS.projection, 1, 1, x, y, 3000.0, -3000.0)

    def test_the_central_meridian_is_the_identity(self) -> None:
        x_at_center = ProjectedGrid(CONUS.projection, 1, 7, 0.0, 400000.0, 3000.0, -100000.0)
        sine, cosine = wind_rotation(x_at_center)
        np.testing.assert_array_equal(sine, 0.0)
        np.testing.assert_array_equal(cosine, 1.0)
        u, v = np.arange(7.0), np.arange(7.0) - 3.0
        rotated_u, rotated_v = rotate_winds(x_at_center, u, v)
        np.testing.assert_array_equal(rotated_u, u)
        np.testing.assert_array_equal(rotated_v, v)

    def test_a_worked_example(self) -> None:
        # 122 W at the cone of 38.5 N: n = sin 38.5° = 0.62251, Δλ = −24.5°, so
        # α = −15.2516°, sin α = −0.26306, cos α = 0.96478. On the western side
        # the meridians lean east toward the apex, so a wind blowing along the
        # grid's x axis has a northward part.
        sine, cosine = wind_rotation(self.cell(-122.0, 40.0))
        alpha = math.sin(math.radians(38.5)) * math.radians(-24.5)
        self.assertAlmostEqual(math.degrees(alpha), -15.2516, places=3)
        self.assertAlmostEqual(float(sine[0]), math.sin(alpha), places=12)
        self.assertAlmostEqual(float(cosine[0]), math.cos(alpha), places=12)
        u, v = rotate_winds(self.cell(-122.0, 40.0), np.array([10.0]), np.array([0.0]))
        self.assertAlmostEqual(float(u[0]), 10.0 * math.cos(alpha), places=9)
        self.assertAlmostEqual(float(v[0]), -10.0 * math.sin(alpha), places=9)
        self.assertGreater(float(v[0]), 2.6)
        # The eastern side mirrors it.
        _, east_v = rotate_winds(self.cell(-73.0, 40.0), np.array([10.0]), np.array([0.0]))
        self.assertAlmostEqual(float(east_v[0]), -float(v[0]), places=9)

    def test_the_table_is_what_the_longitudes_say_and_the_speed_is_kept(self) -> None:
        grid = ProjectedGrid(CONUS.projection, 41, 23, CONUS.x0, CONUS.y0, 45000.0, -46000.0)
        sine, cosine = wind_rotation(grid)
        self.assertEqual(sine.shape, (41 * 23,))
        n = CONUS.projection.cone_constant
        for row, column in ((0, 0), (22, 40), (11, 20), (5, 33)):
            longitude, _ = CONUS.projection.inverse(grid.x0 + column * grid.dx, grid.y0 + row * grid.dy)
            alpha = n * math.radians(longitude - CONUS.projection.central_meridian)
            self.assertAlmostEqual(float(sine[row * 41 + column]), math.sin(alpha), places=11)
            self.assertAlmostEqual(float(cosine[row * 41 + column]), math.cos(alpha), places=11)
        generator = np.random.default_rng(7)
        u, v = generator.uniform(-40, 40, sine.shape), generator.uniform(-40, 40, sine.shape)
        rotated_u, rotated_v = rotate_winds(grid, u, v)
        np.testing.assert_allclose(np.hypot(rotated_u, rotated_v), np.hypot(u, v), rtol=1e-12)

    def test_a_southern_cone_takes_its_sign(self) -> None:
        south = LambertConformal(6371229.0, -30.0, -60.0, -45.0, 140.0)
        x, y = south.forward(150.0, -40.0)
        sine, cosine = wind_rotation(ProjectedGrid(south, 1, 1, x, y, 3000.0, -3000.0))
        alpha = south.meridian_angle(150.0)
        self.assertAlmostEqual(float(sine[0]), math.sin(alpha), places=11)
        self.assertAlmostEqual(float(cosine[0]), math.cos(alpha), places=11)

    def test_only_a_grid_relative_sources_vectors_turn(self) -> None:
        self.assertEqual(_grid_relative_vectors(source_spec("gfs"), ("wind10m",)), ())
        self.assertEqual(_grid_relative_vectors(HRRR, ("wind10m", "wind250")), ("wind10m", "wind250"))
        with self.assertRaisesRegex(ConversionError, "grid-relative"):
            _grid_relative_vectors(HRRR, ("wind10m", "qflux850"))


@requires_gdalinfo
class FixtureGridTests(unittest.TestCase):
    """The fixture through the reference pipeline, against GDAL's own view
    of where its cells are."""

    @classmethod
    def setUpClass(cls) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.grid = grid_info(FIXTURE, HRRR)
        cls.info = json.loads(subprocess.run(["gdalinfo", "-json", str(FIXTURE)], capture_output=True, text=True, check=True).stdout)

    def test_the_projection_is_read_from_the_file(self) -> None:
        projected = self.grid.projection
        self.assertIsNotNone(projected)
        self.assertEqual(projected.projection, CONUS.projection)
        self.assertEqual((self.grid.width, self.grid.height), (120, 120))
        self.assertEqual(self.grid.source_shape, (120, 120))
        self.assertFalse(self.grid.wraps)
        self.assertEqual((projected.dx, projected.dy), (3000.0, -3000.0))
        transform = self.info["geoTransform"]
        self.assertEqual(projected.x0, round(transform[0], 3) + 1500.0)
        self.assertEqual(projected.y0, round(transform[3], 3) - 1500.0)

    def test_the_footprint_is_the_one_gdal_reports(self) -> None:
        # gdalinfo's wgs84Extent is the cell *edges*; the footprint is the
        # centers, half a cell inside.
        ring = self.info["wgs84Extent"]["coordinates"][0]
        west, south, east, north = self.grid.projection.footprint()
        self.assertAlmostEqual(west, min(point[0] for point in ring), delta=0.04)
        self.assertAlmostEqual(east, max(point[0] for point in ring), delta=0.04)
        self.assertAlmostEqual(south, min(point[1] for point in ring), delta=0.04)
        self.assertAlmostEqual(north, max(point[1] for point in ring), delta=0.04)

    def test_a_cell_is_where_gdal_says_it_is(self) -> None:
        # The forward formulas against GDAL's own projection of the same
        # places (through the CLI's `gdaltransform`).
        if shutil.which("gdaltransform") is None:
            self.skipTest("gdaltransform is not on PATH")
        projected = self.grid.projection
        samples = [(10, 20), (60, 60), (119, 100), (0, 119)]
        places = [projected.projection.inverse(projected.x0 + column * projected.dx, projected.y0 + row * projected.dy) for row, column in samples]
        points = "\n".join(f"{longitude} {latitude}" for longitude, latitude in places)
        output = subprocess.run(
            ["gdaltransform", "-s_srs", "EPSG:4326", "-t_srs", self.info["coordinateSystem"]["wkt"], "-output_xy"],
            input=points,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        transform = self.info["geoTransform"]
        for (row, column), x, y in zip(samples, output[0::2], output[1::2]):
            self.assertAlmostEqual((float(x) - transform[0]) / transform[1] - 0.5, column, places=3, msg=(row, column))
            self.assertAlmostEqual((float(y) - transform[3]) / transform[5] - 0.5, row, places=3, msg=(row, column))

    def test_planes_are_read_as_they_are_then_cropped(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}), tempfile.TemporaryDirectory() as work:
            frames = inspect_grib_multi(FIXTURE, ("tmp2m", "cref", "prmsl"))
            planes = _extract_planes(frames, self.grid, Path(work))
            self.assertEqual(planes["tmp2m"].shape, (120 * 120,))
            # Air temperature over the Gulf in September, in Celsius.
            self.assertTrue(((planes["tmp2m"] > 10) & (planes["tmp2m"] < 40)).all())
            self.assertTrue((planes["cref"] < 80).all())
            self.assertTrue(((planes["prmsl"] > 990) & (planes["prmsl"] < 1040)).all())
            cropped = crop_grid(self.grid, (-86.0, 28.0, -85.0, 29.0))
            self.assertEqual(cropped.source_shape, (120, 120))
            window = _extract_planes({"tmp2m": frames["tmp2m"]}, cropped, Path(work))["tmp2m"]
            self.assertEqual(window.size, cropped.width * cropped.height)
            full = planes["tmp2m"].reshape(120, 120)
            crop = cropped.crop
            np.testing.assert_array_equal(
                window.reshape(cropped.height, cropped.width),
                full[crop.row_start : crop.row_start + crop.height, crop.column_start : crop.column_start + crop.width],
            )

    def test_the_origin_survives_a_gdal_releases_worth_of_noise(self) -> None:
        # GDAL places a projected grid by projecting the record's first
        # point, and two releases land that double a few nanometres apart
        # (the wheel's 3.13 against Ubuntu's system GDAL, seen on CI). The
        # published origin and every rotated wind descend from it, so two
        # GDALs must read one grid: the origin is taken to the millimetre.
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
            with mock.patch("xuebuild.grid.dataset_info", return_value=noisy):
                perturbed = grid_info(FIXTURE, HRRR)
        self.assertEqual(perturbed.projection, self.grid.projection)

    def test_a_projected_file_needs_a_projected_source_and_vice_versa(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "map projection, which this source does not declare"):
                grid_info(FIXTURE, source_spec("gfs"))
            with self.assertRaisesRegex(ConversionError, "regular grid, but this source declares a projected one"):
                grid_info(FIXTURE.with_name("gfs.2026081406.f000.crop.grib2"), HRRR)


@requires_gdalinfo
class ConversionTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-hrrr-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                FIXTURE,
                cls.root / "out",
                model="hrrr",
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    def test_every_published_bundle_is_written_on_the_models_grid(self) -> None:
        variables = [bundle["variable"] for bundle in self.report["bundles"]]
        self.assertEqual(tuple(variables), published_bundle_ids(HRRR))
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("HRRR", "wrfsfc"))
        bundle = stored_bundle(self.root / "out", "cref")
        grid = bundle.metadata["grid"]
        self.assertEqual(bundle.metadata["schemaVersion"], 4)
        self.assertEqual((grid["width"], grid["height"]), (120, 120))
        self.assertEqual((grid["projection"]["dx"], grid["projection"]["dy"]), (3000.0, -3000.0))
        self.assertFalse(grid["wrapLongitude"])
        self.assertNotIn("firstLongitude", grid)
        for entry in manifest["bundles"]:
            self.assertEqual(entry["grid"], grid, entry["variable"])
        half = json.loads((self.root / "out" / "bundles.half.zarr" / "zarr.json").read_text())
        half_grid = half["attributes"]["xue_bundles"]["cref"]["grid"]
        self.assertEqual((half_grid["width"], half_grid["height"]), (60, 60))
        self.assertEqual(half_grid["projection"]["x0"], grid["projection"]["x0"])
        self.assertEqual(half_grid["projection"]["dx"], 6000.0)
        # The mosaic's identity for the reflectivity.
        parameter = bundle.metadata["variables"][0]["parameter"]
        self.assertEqual((parameter["parameterCategory"], parameter["parameterNumber"]), (16, 5))
        self.assertEqual(bundle.metadata["variables"][0]["unit"], "dBZ")
        wind = stored_bundle(self.root / "out", "wind10m")
        self.assertEqual([variable["id"] for variable in wind.metadata["variables"]], ["ugrd10m", "vgrd10m"])
        wind80 = stored_bundle(self.root / "out", "wind80m")
        self.assertEqual([variable["id"] for variable in wind80.metadata["variables"]], ["ugrd80m", "vgrd80m"])
        surfaces = {
            (variable["parameter"]["typeOfFirstFixedSurface"], variable["parameter"]["scaledValueOfFirstFixedSurface"])
            for variable in wind80.metadata["variables"]
        }
        self.assertEqual(surfaces, {(103, 80)})

    def test_the_store_says_where_its_cells_are(self) -> None:
        store = self.root / "out" / "bundles.zarr"
        group = json.loads((store / "zarr.json").read_text())
        arrays = group["consolidated_metadata"]["metadata"]
        self.assertEqual(arrays["tmp2m"]["dimension_names"], ["time", "y", "x"])
        self.assertEqual(arrays["tmp2m"]["attributes"]["grid_mapping"], "crs")
        self.assertEqual(arrays["y"]["attributes"]["standard_name"], "projection_y_coordinate")
        self.assertEqual(arrays["x"]["attributes"], {"units": "m", "standard_name": "projection_x_coordinate"})
        projection = group["attributes"]["xue_bundles"]["tmp2m"]["grid"]["projection"]
        y = np.frombuffer((store / "y" / "c" / "0").read_bytes(), "<f8")
        x = np.frombuffer((store / "x" / "c" / "0").read_bytes(), "<f8")
        np.testing.assert_array_equal(y, projection["y0"] + np.arange(120.0) * projection["dy"])
        np.testing.assert_array_equal(x, projection["x0"] + np.arange(120.0) * projection["dx"])
        crs = arrays["crs"]
        self.assertEqual((crs["shape"], crs["dimension_names"]), ([], []))
        self.assertEqual(crs["attributes"]["grid_mapping_name"], "lambert_conformal_conic")
        self.assertEqual(crs["attributes"]["standard_parallel"], [38.5, 38.5])
        self.assertEqual(lambert_conformal_from_wkt(crs["attributes"]["crs_wkt"]), CONUS.projection)
        self.assertEqual([path.name for path in (store / "crs").iterdir()], ["zarr.json"])
        for name in ("lat", "latitude", "longitude"):
            self.assertNotIn(name, arrays)

    def test_every_published_wind_is_turned_to_east_and_north(self) -> None:
        # The stored codes against the raw grid-relative records, each pair by
        # its own rotation, wind10m and wind80m and the isobaric ones alike.
        codebooks = PROFILES["quality"]
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}), tempfile.TemporaryDirectory() as work:
            grid = grid_info(FIXTURE, HRRR)
            for bundle_id in HRRR.bundle_vector_ids:
                u_id, v_id = VECTOR_BUNDLES[bundle_id]
                frames = inspect_grib_multi(FIXTURE, (u_id, v_id))
                planes = _extract_planes(frames, grid, Path(work))
                u, v = rotate_winds(grid.projection, planes[u_id], planes[v_id])
                bundle = stored_bundle(self.root / "out", bundle_id)
                with self.subTest(bundle_id):
                    np.testing.assert_array_equal(bundle.decode_plane(1, 0), codebooks[u_id].quantize(u))
                    np.testing.assert_array_equal(bundle.decode_plane(2, 0), codebooks[v_id].quantize(v))
                    # The window lies well east of the central meridian, so
                    # the rotation is no accident of rounding.
                    self.assertFalse((codebooks[u_id].quantize(u) == codebooks[u_id].quantize(planes[u_id])).all())
                    half = stored_bundle(self.root / "out", f"{bundle_id}.half")
                    np.testing.assert_array_equal(
                        half.decode_plane(1, 0), decimate_codes(codebooks[u_id].quantize(u), grid)
                    )

    def test_a_series_store_is_on_the_same_axes(self) -> None:
        group = json.loads((self.root / "out" / "wind10m.series.zarr" / "zarr.json").read_text())
        arrays = group["consolidated_metadata"]["metadata"]
        self.assertEqual(list(arrays), ["ugrd10m", "vgrd10m", "time", "y", "x", "crs"])
        self.assertEqual(arrays["ugrd10m"]["dimension_names"], ["time", "y", "x"])
        self.assertEqual(arrays["ugrd10m"]["attributes"]["grid_mapping"], "crs")
        self.assertEqual(group["attributes"]["xue"]["schemaVersion"], 4)

    @requires_native
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, FIXTURE, self.root / "out", self.report, self.root / "native", model="hrrr")

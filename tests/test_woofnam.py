"""The WOOF-NAM source: NOAA's NAM ``awphys`` layout on NCEP grid 218 from
Recast's host, a projected grid of its own, and a forecast with no
precipitation to publish.

``tests/fixtures/woofnam.2026111600.f000.crop.grib2`` is an 80 x 80 cell window
of the 2026-11-16 00Z analysis over California and the Sierra Nevada, every
record the source fetches in source order.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import (
    ClassTempRoot,
    FIXTURES,
    assert_native_matches,
    requires_gdalinfo,
    requires_native_source,
    stored_bundle,
)
from xuebuild import binconvert, grib2
from xuebuild.binconvert import _extract_planes
from xuebuild.bundles import VECTOR_BUNDLES
from xuebuild.discover import published_bundle_ids
from xuebuild.fetch import WOOFNAM_BASE_URL, _download_noaa_payload, _grib_run_is_complete, model_object_url
from xuebuild.gdal import inspect_grib_multi
from xuebuild.grid import grid_info
from xuebuild.idx import field_byte_range
from xuebuild.model import GfsRun
from xuebuild.quantize import PROFILES
from xuebuild.reproject import rotate_winds
from xuebuild.sources import source_spec
from xuebuild.variables import VARIABLES

FIXTURE = FIXTURES / "woofnam.2026111600.f000.crop.grib2"
WOOFNAM = source_spec("woofnam")
RUN = GfsRun(datetime(2026, 11, 16, 0, tzinfo=UTC))
# Lines of a real forecast-hour ``.idx``, the offsets shortened: each record
# the source reads beside the neighbours it must not be taken for.
INDEX = "\n".join(
    (
        "1:0:d=2026111600:PRMSL:mean sea level:6 hour fcst:",
        "2:10:d=2026111600:MSLET:mean sea level:6 hour fcst:",
        "3:20:d=2026111600:HGT:surface:6 hour fcst:",
        "4:30:d=2026111600:TMP:surface:6 hour fcst:",
        "5:40:d=2026111600:TMP:2 m above ground:6 hour fcst:",
        "6:50:d=2026111600:TMAX:2 m above ground:5-6 hour max fcst:",
        "7:60:d=2026111600:TMIN:2 m above ground:5-6 hour min fcst:",
        "8:70:d=2026111600:CAPE:surface:6 hour fcst:",
        "9:80:d=2026111600:TMP:80 m above ground:6 hour fcst:",
        "10:90:d=2026111600:HGT:0C isotherm:6 hour fcst:",
        "11:100:d=2026111600:RH:0C isotherm:6 hour fcst:",
        "12:110:d=2026111600:CAPE:180-0 mb above ground:6 hour fcst:",
        "13:120:d=2026111600:TCDC:entire atmosphere (considered as a single layer):6 hour fcst:",
        "14:130:d=2026111600:APCP:surface:3-6 hour acc fcst:",
    )
)

# Every input's line, so a whole frame's selection can run.
FULL_INDEX = "\n".join(
    f"{number}:{number * 10}:d=2026111600:{VARIABLES[variable_id].index_field.lstrip(':')}"
    for number, variable_id in enumerate(WOOFNAM.input_variable_ids, start=1)
) + "\n99:9990:d=2026111600:LAST:surface:"


class SourceTests(unittest.TestCase):
    def test_the_grid_and_the_axis(self) -> None:
        self.assertTrue(WOOFNAM.grid_relative_winds)
        self.assertEqual(WOOFNAM.production_grid, (614, 428))
        self.assertEqual(WOOFNAM.cycle_hours, 6)
        self.assertEqual(WOOFNAM.forecast_hours(84), list(range(85)))
        self.assertEqual(WOOFNAM.companion_files, ())

    def test_published_bundles(self) -> None:
        published = published_bundle_ids(WOOFNAM)
        self.assertEqual(published[0], "tmp2m")
        for bundle_id in ("prmsl", "hgt250", "fzlvl", "orog", "wind10m", "wind80m", "wind250"):
            self.assertIn(bundle_id, published)

    def test_no_precipitation_is_read_or_published(self) -> None:
        # The files carry totals in resetting buckets alone, which no rule
        # turns into a rate.
        self.assertEqual(WOOFNAM.core_bundle_ids, ("tmp2m",))
        for variable_id in ("prate", "apcp", "tp"):
            self.assertNotIn(variable_id, WOOFNAM.input_variable_ids)
            self.assertNotIn(variable_id, published_bundle_ids(WOOFNAM))
        self.assertFalse(WOOFNAM.accumulated_precipitation or WOOFNAM.averaged_precipitation or WOOFNAM.interval_precipitation)

    def test_every_published_scalar_is_an_input(self) -> None:
        self.assertEqual(set(WOOFNAM.bundle_scalar_ids) - set(WOOFNAM.input_variable_ids), set())
        for bundle_id in WOOFNAM.bundle_vector_ids:
            for variable_id in VECTOR_BUNDLES[bundle_id]:
                self.assertIn(variable_id, WOOFNAM.input_variable_ids)


class FetchTests(unittest.TestCase):
    def test_object_urls(self) -> None:
        self.assertEqual(WOOFNAM_BASE_URL, "https://woof-nam.recastsystems.com")
        self.assertEqual(
            model_object_url(RUN, 0, "woofnam"),
            f"{WOOFNAM_BASE_URL}/woof-nam/12km/20261116/00z/woof-nam.t00z.awphys00.tm00.grib2",
        )
        self.assertEqual(
            model_object_url(GfsRun(datetime(2026, 11, 15, 18, tzinfo=UTC)), 84, "woofnam"),
            f"{WOOFNAM_BASE_URL}/woof-nam/12km/20261115/18z/woof-nam.t18z.awphys84.tm00.grib2",
        )

    def test_both_ends_prove_a_run(self) -> None:
        # The host writes a run's hours in order.
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return True

        self.assertTrue(_grib_run_is_complete(WOOFNAM, RUN, 84, exists))
        self.assertEqual(probed, [model_object_url(RUN, 0, "woofnam"), model_object_url(RUN, 84, "woofnam")])
        self.assertFalse(_grib_run_is_complete(WOOFNAM, RUN, 84, lambda url: "awphys84" not in url))

    def test_a_frame_is_one_object_and_the_terrain_the_analysis_alone(self) -> None:
        for hour, records in ((0, len(WOOFNAM.input_variable_ids)), (6, len(WOOFNAM.input_variable_ids) - 1)):
            with (
                self.subTest(hour=hour),
                mock.patch("xuebuild.fetch.fetch_text", return_value=FULL_INDEX),
                mock.patch("xuebuild.fetch.fetch_range", side_effect=lambda url, _: url.encode()) as fetched,
            ):
                _download_noaa_payload(RUN, hour, WOOFNAM)
                urls = {call.args[0] for call in fetched.call_args_list}
                self.assertEqual(urls, {model_object_url(RUN, hour, "woofnam")})
                self.assertEqual(fetched.call_count, records)


class IndexTests(unittest.TestCase):
    def range_of(self, variable_id: str):
        variable = VARIABLES[variable_id]
        return field_byte_range(
            INDEX,
            variable.index_field,
            excluded_phrases=variable.excluded_index_phrases,
            alternate_fields=variable.alternate_index_fields,
            qualifier=variable.index_qualifier,
        ).start

    def test_each_record_is_picked_out_of_its_neighbours(self) -> None:
        # PRMSL beside MSLET, the terrain beside the skin temperature, the
        # 2 m temperature beside its hourly extremes and the 80 m one, the
        # surface CAPE beside the layer's, the freezing level beside its
        # humidity.
        self.assertEqual(self.range_of("prmsl"), 0)
        self.assertEqual(self.range_of("orog"), 20)
        self.assertEqual(self.range_of("tmp2m"), 40)
        self.assertEqual(self.range_of("cape"), 70)
        self.assertEqual(self.range_of("fzlvl"), 90)
        self.assertEqual(self.range_of("tcdc"), 120)


class FixtureTests(unittest.TestCase):
    def test_every_input_is_found_both_ways(self) -> None:
        fast = grib2.inspect_grib_fast(FIXTURE, WOOFNAM.input_variable_ids)
        self.assertEqual(tuple(fast), WOOFNAM.input_variable_ids)
        self.assertEqual([frame.band for frame in fast.values()], list(range(1, len(WOOFNAM.input_variable_ids) + 1)))

    def test_the_cloud_cover_is_read_off_nceps_single_layer(self) -> None:
        # Surface type 200 where pgrb2 writes 10: an alternate of the registry.
        tcdc = next(message for message in grib2.index_messages(FIXTURE) if message.parameter_category == 6)
        self.assertEqual((tcdc.parameter_number, tcdc.level_type), (1, 200))

    @requires_gdalinfo
    def test_the_two_paths_agree(self) -> None:
        fast = grib2.inspect_grib_fast(FIXTURE, WOOFNAM.input_variable_ids)
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            slow = inspect_grib_multi(FIXTURE, WOOFNAM.input_variable_ids)
        for variable_id, frame in fast.items():
            self.assertEqual(frame, slow[variable_id], variable_id)


@requires_gdalinfo
class BuildTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-woofnam-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                FIXTURE,
                cls.root / "out",
                model="woofnam",
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    def test_every_published_bundle_is_written_on_the_models_grid(self) -> None:
        self.assertEqual(tuple(bundle["variable"] for bundle in self.report["bundles"]), published_bundle_ids(WOOFNAM))
        bundle = stored_bundle(self.root / "out", "tmp2m")
        grid = bundle.metadata["grid"]
        self.assertEqual(bundle.metadata["schemaVersion"], 4)
        self.assertEqual((grid["width"], grid["height"]), (80, 80))
        projection = grid["projection"]
        self.assertEqual(projection["kind"], "lambert-conformal")
        # Grid 218: tangent at 25N, centred on 95W.
        self.assertEqual(
            (projection["radius"], projection["latitudeOfOrigin"], projection["centralMeridian"]),
            (6371229.0, 25.0, -95.0),
        )
        self.assertEqual((projection["dx"], projection["dy"]), (12191.0, -12191.0))
        self.assertNotIn("firstLongitude", grid)
        self.assertEqual(self.report["grid"], grid)

    def test_every_wind_is_turned_to_east_and_north(self) -> None:
        codebooks = PROFILES["quality"]
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}), tempfile.TemporaryDirectory() as work:
            grid = grid_info(FIXTURE, WOOFNAM)
            for bundle_id in WOOFNAM.bundle_vector_ids:
                u_id, v_id = VECTOR_BUNDLES[bundle_id]
                planes = _extract_planes(inspect_grib_multi(FIXTURE, (u_id, v_id)), grid, Path(work))
                u, v = rotate_winds(grid.projection, planes[u_id], planes[v_id])
                bundle = stored_bundle(self.root / "out", bundle_id)
                with self.subTest(bundle_id):
                    self.assertEqual(bundle.metadata["schemaVersion"], 4)
                    np.testing.assert_array_equal(bundle.decode_plane(1, 0), codebooks[u_id].quantize(u))
                    np.testing.assert_array_equal(bundle.decode_plane(2, 0), codebooks[v_id].quantize(v))
                    # California is far enough west of 95W that the turn shows.
                    self.assertFalse((codebooks[u_id].quantize(u) == codebooks[u_id].quantize(planes[u_id])).all())

    def test_the_store_carries_the_crs(self) -> None:
        group = json.loads((self.root / "out" / "bundles.zarr" / "zarr.json").read_text())
        arrays = group["consolidated_metadata"]["metadata"]
        self.assertEqual(arrays["tmp2m"]["dimension_names"], ["time", "y", "x"])
        self.assertEqual(arrays["crs"]["attributes"]["longitude_of_central_meridian"], -95.0)

    def test_the_manifest_names_the_source(self) -> None:
        manifest = json.loads((self.root / "out" / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("WOOF-NAM", "awphys"))

    @requires_native_source("woofnam")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, FIXTURE, self.root / "out", self.report, self.root / "native", model="woofnam")


if __name__ == "__main__":
    unittest.main()

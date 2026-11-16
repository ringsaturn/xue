"""The WOOF-NAM source: NOAA's NAM ``awphys`` layout on NCEP grid 218 from
Recast's host, a projected grid of its own, and a precipitation that
arrives as the total since the start of a twelve-hour window.

``tests/fixtures/woofnam.2026111600.f00{0,1,2}.crop.grib2`` are an 80 x 80
cell window of the 2026-11-16 00Z analysis and its first two hours over
California and the Sierra Nevada, every record the source fetches in source
order: the analysis has the terrain and no total, the two hours the total
since the run began. ``woofnam.2026111600.f006.apcp.crop.grib2`` is the
same window of the two ``APCP`` records the sixth hour's file holds, the
window's total and the last three hours'.
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
    assert_gdalinfo_agrees,
    assert_native_matches,
    requires_gdalinfo,
    requires_native_source,
    stored_bundle,
)
from xuebuild import binconvert, grib2
from xuebuild.binconvert import _extract_planes, _sharing_plan
from xuebuild.bundles import VECTOR_BUNDLES
from xuebuild.derive import average_window_start, dewindow_precipitation, precipitation_mode, precipitation_window_hours
from xuebuild.discover import analysis_optional_ids, bundle_input_ids, published_bundle_ids
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    WOOFNAM_BASE_URL,
    _download_noaa_payload,
    _grib_run_is_complete,
    model_object_url,
    window_index_qualifier,
)
from xuebuild.gdal import _band_matches, inspect_grib_multi
from xuebuild.grid import grid_info
from xuebuild.idx import field_byte_range
from xuebuild.model import GfsRun, SourceFrame
from xuebuild.quantize import PROFILES
from xuebuild.reproject import rotate_winds
from xuebuild.sources import source_spec
from xuebuild.variables import VARIABLES

FIXTURE = FIXTURES / "woofnam.2026111600.f000.crop.grib2"
FIRST_HOUR = FIXTURES / "woofnam.2026111600.f001.crop.grib2"
SECOND_HOUR = FIXTURES / "woofnam.2026111600.f002.crop.grib2"
FRAMES = [FIXTURE, FIRST_HOUR, SECOND_HOUR]
TWO_TOTALS = FIXTURES / "woofnam.2026111600.f006.apcp.crop.grib2"
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
        "14:130:d=2026111600:APCP:surface:0-6 hour acc fcst:",
        "15:140:d=2026111600:ACPCP:surface:0-6 hour acc fcst:",
        "16:150:d=2026111600:APCP:surface:3-6 hour acc fcst:",
        "17:160:d=2026111600:ACPCP:surface:3-6 hour acc fcst:",
    )
)


def full_index(hour: int) -> str:
    """Every input's line at ``hour``, so a whole frame's selection can
    run: the total as the file names it, and after it the last three
    hours' wherever the file holds one."""
    lines = []
    for number, variable_id in enumerate(WOOFNAM.input_variable_ids, start=1):
        field = VARIABLES[variable_id].index_field.lstrip(":")
        phrase = f"{average_window_start(hour, 12)}-{hour} hour acc fcst:" if variable_id == "apcp" and hour else ""
        lines.append(f"{number}:{number * 10}:d=2026111600:{field}{phrase}")
    if hour and hour % 3 == 0 and hour - 3 != average_window_start(hour, 12):
        lines.append(f"98:9980:d=2026111600:APCP:surface:{hour - 3}-{hour} hour acc fcst:")
    return "\n".join(lines) + "\n99:9990:d=2026111600:LAST:surface:"


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

    def test_the_rate_is_read_off_the_window_s_total(self) -> None:
        # The files carry no rate: APCP is the total since the start of a
        # window that resets every twelve hours.
        self.assertTrue(WOOFNAM.windowed_precipitation)
        self.assertEqual(WOOFNAM.accumulation_window_hours, 12)
        self.assertFalse(WOOFNAM.accumulated_precipitation or WOOFNAM.averaged_precipitation or WOOFNAM.interval_precipitation)
        self.assertEqual((precipitation_mode(WOOFNAM), precipitation_window_hours(WOOFNAM)), ("windowed", 12))
        self.assertEqual(WOOFNAM.input_variable_ids[:2], ("tmp2m", "apcp"))
        for variable_id in ("prate", "tp", "prate_ave"):
            self.assertNotIn(variable_id, WOOFNAM.input_variable_ids)
        self.assertEqual(bundle_input_ids(WOOFNAM, "prate"), ("apcp",))
        self.assertEqual(published_bundle_ids(WOOFNAM)[:2], ("tmp2m", "prate"))
        self.assertNotIn("apcp", published_bundle_ids(WOOFNAM))

    def test_the_rate_has_no_analysis_frame_and_is_a_mean(self) -> None:
        self.assertEqual(WOOFNAM.optional_at_analysis, ("apcp",))
        self.assertEqual(analysis_optional_ids(WOOFNAM, WOOFNAM.bundle_scalar_ids), ("prate",))
        self.assertEqual(WOOFNAM.statistical_processes, (("prate", 0),))

    def test_a_run_without_the_rate_is_not_a_complete_one(self) -> None:
        self.assertEqual(WOOFNAM.core_bundle_ids, ("tmp2m", "prate"))

    def test_every_published_scalar_but_the_rate_is_an_input(self) -> None:
        self.assertEqual(set(WOOFNAM.bundle_scalar_ids) - set(WOOFNAM.input_variable_ids), {"prate"})
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

    def test_a_frame_is_one_object_the_terrain_the_analysis_alone_and_the_total_every_other(self) -> None:
        # The analysis has the terrain and no total, every later hour the
        # total and no terrain.
        for hour in (0, 3, 6, 13):
            with (
                self.subTest(hour=hour),
                mock.patch("xuebuild.fetch.fetch_text", return_value=full_index(hour)),
                mock.patch("xuebuild.fetch.fetch_range", side_effect=lambda url, _: url.encode()) as fetched,
            ):
                _download_noaa_payload(RUN, hour, WOOFNAM)
                urls = {call.args[0] for call in fetched.call_args_list}
                self.assertEqual(urls, {model_object_url(RUN, hour, "woofnam")})
                self.assertEqual(fetched.call_count, len(WOOFNAM.input_variable_ids) - 1)

    def test_the_total_fetched_is_the_window_s(self) -> None:
        # F006 holds the last three hours' total too, after the window's.
        position = WOOFNAM.input_variable_ids.index("apcp") + 1
        with (
            mock.patch("xuebuild.fetch.fetch_text", return_value=full_index(6)),
            mock.patch("xuebuild.fetch.fetch_range", side_effect=lambda _, byte_range: b"") as fetched,
        ):
            _download_noaa_payload(RUN, 6, WOOFNAM, ("apcp",))
        (call,) = fetched.call_args_list
        self.assertEqual(call.args[1].start, position * 10)

    def test_the_phrase_names_the_window_the_hour_falls_in(self) -> None:
        for hour, phrase in (
            (1, ":0-1 hour acc fcst:"),
            (12, ":0-12 hour acc fcst:"),
            (13, ":12-13 hour acc fcst:"),
            (24, ":12-24 hour acc fcst:"),
            (84, ":72-84 hour acc fcst:"),
        ):
            self.assertEqual(window_index_qualifier(WOOFNAM, "apcp", hour), phrase)
        # No total at the analysis, no phrase for any other record, and
        # none on a source whose total is not a window's.
        self.assertIsNone(window_index_qualifier(WOOFNAM, "apcp", 0))
        self.assertIsNone(window_index_qualifier(WOOFNAM, "tmp2m", 6))
        self.assertIsNone(window_index_qualifier(source_spec("cmageps"), "apcp", 6))


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

    def test_the_window_s_total_is_picked_beside_the_last_three_hours(self) -> None:
        # Two APCP lines, and the convective total beside each: the field
        # and surface name both totals, the window's phrase one.
        variable = VARIABLES["apcp"]
        with self.assertRaises(DownloadError):
            field_byte_range(INDEX, variable.index_field)
        picked = field_byte_range(
            INDEX, variable.index_field, qualifier=window_index_qualifier(WOOFNAM, "apcp", 6) or ""
        )
        self.assertEqual(picked.start, 130)


class WindowTests(unittest.TestCase):
    def test_the_window_resets_every_twelve_hours(self) -> None:
        for hour, start in ((1, 0), (12, 0), (13, 12), (24, 12), (25, 24), (84, 72)):
            self.assertEqual(average_window_start(hour, 12), start)

    def test_the_window_s_first_frame_is_its_own_interval(self) -> None:
        total = np.array([2.5, 0.0])
        np.testing.assert_array_equal(dewindow_precipitation(total, 1, None, None, 12), total)
        # F013 opens the next window: the hour's total, whatever F012 held.
        np.testing.assert_array_equal(dewindow_precipitation(np.array([0.75]), 13, None, None, 12), [0.75])

    def test_inside_the_window_totals_are_differenced(self) -> None:
        rate = dewindow_precipitation(np.array([7.0, 3.0]), 5, np.array([2.5, 3.0]), 2, 12)
        np.testing.assert_array_equal(rate, [1.5, 0.0])

    def test_packing_noise_clamps_to_zero(self) -> None:
        rate = dewindow_precipitation(np.array([1.0]), 6, np.array([1.25]), 5, 12)
        np.testing.assert_array_equal(rate, [0.0])

    def test_a_frame_of_the_previous_window_is_no_predecessor(self) -> None:
        with self.assertRaises(ConversionError):
            dewindow_precipitation(np.array([0.75]), 13, np.array([9.0]), 12, 12)
        with self.assertRaises(ConversionError):
            dewindow_precipitation(np.array([0.0]), 0, None, None, 12)

    def test_a_window_entered_late_spans_the_hours_behind_it(self) -> None:
        np.testing.assert_array_equal(dewindow_precipitation(np.array([6.0]), 15, None, None, 12), [2.0])

    def test_a_plane_is_shared_inside_a_window_and_not_across_its_end(self) -> None:
        def frames(hour: int) -> dict[str, SourceFrame]:
            lead = hour * 3600
            ids = ("tmp2m", "apcp") if hour else ("tmp2m",)
            return {
                variable_id: SourceFrame(Path("frame"), band, variable_id, RUN.time, RUN.valid_time(hour), lead, "")
                for band, variable_id in enumerate(ids, start=1)
            }

        hours = (0, 1, 2, 12, 13, 14)
        plan = list(_sharing_plan([frames(hour) for hour in hours], "apcp", WOOFNAM, RUN.time))
        predecessors = [previous[0] if previous is not None else None for _, previous, _, _ in plan]
        # The analysis has no total; F001 and F013 open their windows.
        self.assertEqual(predecessors, [None, None, 1, 2, None, 13])
        self.assertEqual([own is not None for _, _, own, _ in plan], [False, True, True, False, True, False])


class MatcherTests(unittest.TestCase):
    def test_gdal_names_the_total_by_its_length(self) -> None:
        def total(element: str) -> dict[str, str]:
            return {"GRIB_ELEMENT": element, "GRIB_SHORT_NAME": "0-SFC", "GRIB_PDS_PDTN": "8"}

        for hours in (1, 6, 12):
            self.assertTrue(_band_matches("apcp", total(f"APCP{hours:02d}"), ""))
        # Longer than the window, the convective share, another surface.
        self.assertFalse(_band_matches("apcp", total("APCP24"), ""))
        self.assertFalse(_band_matches("apcp", total("ACPCP06"), ""))
        self.assertFalse(_band_matches("apcp", {**total("APCP06"), "GRIB_SHORT_NAME": "2-HTGL"}, ""))


class FixtureTests(unittest.TestCase):
    def test_every_input_is_found_in_source_order(self) -> None:
        # The analysis has no total, the hours after it no terrain.
        for path, absent in ((FIXTURE, "apcp"), (FIRST_HOUR, "orog"), (SECOND_HOUR, "orog")):
            with self.subTest(path.name):
                fast = grib2.inspect_grib_fast(path, WOOFNAM.input_variable_ids, optional_ids=(absent,))
                expected = tuple(variable_id for variable_id in WOOFNAM.input_variable_ids if variable_id != absent)
                self.assertEqual(tuple(fast), expected)
                self.assertEqual([frame.band for frame in fast.values()], list(range(1, len(expected) + 1)))

    def test_the_total_is_an_accumulation_on_the_ground(self) -> None:
        for path, hour in ((FIRST_HOUR, 1), (SECOND_HOUR, 2)):
            frame = grib2.inspect_grib_fast(path, ("apcp",))["apcp"]
            message = grib2.index_messages(path)[frame.band - 1]
            with self.subTest(path.name):
                self.assertEqual((message.parameter_category, message.parameter_number), (1, 8))
                self.assertEqual((message.level_type, message.statistical_process), (1, 1))
                self.assertEqual((frame.lead_seconds, frame.unit), (hour * 3600, "kg/(m^2)"))

    def test_a_file_with_the_last_three_hours_total_too_is_refused(self) -> None:
        # Nothing in the record a matcher reads says where its interval
        # begins, so two totals in one file are two bands of the variable.
        self.assertEqual(len(grib2.index_messages(TWO_TOTALS)), 2)
        with self.assertRaisesRegex(ConversionError, "found 2"):
            grib2.inspect_grib_fast(TWO_TOTALS, ("apcp",))

    @requires_gdalinfo
    def test_gdal_refuses_it_too(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}), self.assertRaisesRegex(ConversionError, "found 2"):
            inspect_grib_multi(TWO_TOTALS, ("apcp",))

    def test_the_cloud_cover_is_read_off_nceps_single_layer(self) -> None:
        # Surface type 200 where pgrb2 writes 10: an alternate of the registry.
        tcdc = next(message for message in grib2.index_messages(FIXTURE) if message.parameter_category == 6)
        self.assertEqual((tcdc.parameter_number, tcdc.level_type), (1, 200))

    @requires_gdalinfo
    def test_the_two_paths_agree(self) -> None:
        assert_gdalinfo_agrees(
            self,
            (
                (FIXTURE, WOOFNAM.input_variable_ids, ("apcp",)),
                (FIRST_HOUR, WOOFNAM.input_variable_ids, ("orog",)),
                (SECOND_HOUR, WOOFNAM.input_variable_ids, ("orog",)),
            ),
        )


@requires_gdalinfo
class BuildTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-woofnam-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                FRAMES,
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

    def test_the_rate_is_the_window_s_total_differenced(self) -> None:
        bundle = stored_bundle(self.root / "out", "prate")
        metadata = bundle.metadata
        self.assertEqual(metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 1, "frameCount": 2, "frameStep": 1})
        self.assertEqual(metadata["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 0)
        codebook = PROFILES["quality"]["prate"]
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}), tempfile.TemporaryDirectory() as work:
            grid = grid_info(FIXTURE, WOOFNAM)
            first, second = (
                _extract_planes(inspect_grib_multi(path, ("apcp",)), grid, Path(work))["apcp"]
                for path in (FIRST_HOUR, SECOND_HOUR)
            )
        # Northern California is wet in both hours: 3.4 mm in the first at
        # the wettest cell, 7.4 mm by the end of the second.
        self.assertGreater(float(first.max()), 3.0)
        self.assertGreater(float(second.max()), 7.0)
        np.testing.assert_array_equal(bundle.decode_plane(1, 1), codebook.quantize(first))
        np.testing.assert_array_equal(bundle.decode_plane(1, 2), codebook.quantize(np.maximum(second - first, 0.0)))
        self.assertFalse((codebook.quantize(second) == bundle.decode_plane(1, 2)).all())

    def test_the_other_fields_keep_the_whole_axis(self) -> None:
        metadata = stored_bundle(self.root / "out", "tmp2m").metadata
        self.assertEqual(metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 3, "frameStep": 1})
        self.assertEqual(stored_bundle(self.root / "out", "orog").metadata["time"]["frameCount"], 1)

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
        assert_native_matches(self, FRAMES, self.root / "out", self.report, self.root / "native", model="woofnam")


if __name__ == "__main__":
    unittest.main()

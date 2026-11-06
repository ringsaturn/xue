"""The RRFS source and the smoke and dust it carries inline (RRFS-SD): HRRR's
projected grid on a frame from two file families, a bucket that fills out of
order, the kg/m³ of the concentrations, and three records spelled unlike HRRR's.

``tests/fixtures/rrfs.2026110500.f000.crop.grib2`` is a 120 x 120 cell window of
the 2026-11-05 00Z analysis over the Sierra Nevada, every record the source
fetches in source order.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
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
from xuebuild import binconvert, grib2, rules
from xuebuild.discover import published_bundle_ids
from xuebuild.errors import ConversionError
from xuebuild.fetch import RRFS_BASE_URL, _download_noaa_payload, _rrfs_run_is_complete, companion_object_url, model_object_url
from xuebuild.gdal import inspect_grib_multi
from xuebuild.idx import field_byte_range
from xuebuild.model import GfsRun
from xuebuild.sources import Regrid, source_spec
from xuebuild.variables import SMOKE_DUST_VARIABLE_IDS, VARIABLES

FIXTURE = FIXTURES / "rrfs.2026110500.f000.crop.grib2"
RRFS = source_spec("rrfs")
HRRR = source_spec("hrrr")
RUN = GfsRun(datetime(2026, 11, 5, 12, tzinfo=UTC))
# Lines of a real forecast-hour ``.idx``, the offsets shortened.
INDEX = "\n".join(
    (
        "1:0:d=2026110512:PRATE:surface:6 hour fcst:",
        "2:10:d=2026110512:PRATE:surface:5-6 hour max fcst:",
        "3:20:d=2026110512:MSLET:mean sea level:6 hour fcst:",
        "4:30:d=2026110512:REFC:entire atmosphere (considered as a single layer):6 hour fcst:",
        "5:40:d=2026110512:MASSDEN:8 m above ground:6 hour fcst:aerosol=Particulate organic matter dry:aerosol_size <2.5e-06:",
        "6:50:d=2026110512:MASSDEN:8 m above ground:6 hour fcst:aerosol=Dust dry:aerosol_size <2.5e-06:",
        "7:60:d=2026110512:MASSDEN:8 m above ground:5-6 hour ave fcst:aerosol=Total aerosol:aerosol_size <2.5e-06",
        "8:70:d=2026110512:MASSDEN:8 m above ground:5-6 hour ave fcst:aerosol=Total aerosol:aerosol_size <1e-05",
        "9:80:d=2026110512:MASSDEN:8 m above ground:6 hour fcst:aerosol=Dust dry:aerosol_size >=2.5e-06,<1e-05:",
        "10:90:d=2026110512:AOTK:entire atmosphere (considered as a single layer):6 hour fcst:",
        "11:100:d=2026110512:COLMD:entire atmosphere (considered as a single layer):6 hour fcst:aerosol=Dust dry:aerosol_size <1e-05:",
    )
)


class SourceTests(unittest.TestCase):
    def test_the_grid_is_hrrrs_and_the_axis_its_own(self) -> None:
        self.assertEqual(RRFS.regrid, Regrid(step=0.03))
        self.assertEqual((RRFS.production_grid, RRFS.tile), (HRRR.production_grid, HRRR.tile))
        self.assertEqual(RRFS.cycle_hours, 6)
        self.assertEqual(RRFS.forecast_hours(84), list(range(85)))

    def test_published_bundles(self) -> None:
        published = published_bundle_ids(RRFS)
        self.assertEqual(published[:2], ("tmp2m", "prate"))
        for variable_id in (*SMOKE_DUST_VARIABLE_IDS, "prmsl", "cref", "hgt250", "wind80m"):
            self.assertIn(variable_id, published)

    def test_every_isobaric_input_comes_from_the_companion(self) -> None:
        companion = RRFS.companion_files[0]
        self.assertEqual(companion.id, "prslev")
        for variable_id in RRFS.input_variable_ids:
            self.assertEqual(RRFS.companion_of(variable_id) is companion, VARIABLES[variable_id].grib2_level_type == 100, variable_id)


class FetchTests(unittest.TestCase):
    def test_object_urls(self) -> None:
        self.assertEqual(
            model_object_url(RUN, 84, "rrfs"), f"{RRFS_BASE_URL}/rrfs.20261105/12/rrfs.t12z.2dfld.3km.f084.conus.grib2"
        )
        self.assertEqual(
            companion_object_url(RUN, 7, "prslev"), f"{RRFS_BASE_URL}/rrfs.20261105/12/rrfs.t12z.prslev.3km.f007.conus.grib2"
        )

    def test_a_run_missing_an_hour_in_the_middle_is_not_complete(self) -> None:
        # The bucket's hours land out of order: both ends can be there first.
        def missing(fragment: str):
            return lambda url: fragment not in url

        self.assertTrue(_rrfs_run_is_complete(RRFS, RUN, 84, lambda url: True))
        for fragment in ("2dfld.3km.f079", "prslev.3km.f079", "prslev.3km.f001.conus.grib2.idx"):
            self.assertFalse(_rrfs_run_is_complete(RRFS, RUN, 84, missing(fragment)), fragment)

    def test_a_frame_is_both_families_in_order(self) -> None:
        with (
            mock.patch("xuebuild.fetch.fetch_text", return_value=FULL_INDEX),
            mock.patch("xuebuild.fetch.fetch_range", side_effect=lambda url, _: url.encode()) as fetched,
        ):
            _download_noaa_payload(RUN, 6, RRFS)
        urls = [call.args[0] for call in fetched.call_args_list]
        families = ["prslev" if ".prslev." in url else "2dfld" for url in urls]
        self.assertEqual(families, sorted(families))
        self.assertEqual(len(urls), len(RRFS.input_variable_ids) - 1)  # The terrain is the analysis's.


# Every input's line, so a whole frame's selection can run.
FULL_INDEX = "\n".join(
    f"{number}:{number * 10}:d=2026110512:{field.lstrip(':')}{qualifier.lstrip(':')}"
    for number, (field, qualifier) in enumerate(
        (
            (
                VARIABLES[variable_id].index_field if variable_id != "prmsl" else ":MSLET:mean sea level:",
                VARIABLES[variable_id].index_qualifier,
            )
            for variable_id in RRFS.input_variable_ids
        ),
        start=1,
    )
) + "\n99:9990:d=2026110512:LAST:surface:"


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
        self.assertEqual(self.range_of("prate"), 0)
        self.assertEqual(self.range_of("prmsl"), 20)
        self.assertEqual(self.range_of("cref"), 30)
        self.assertEqual(self.range_of("smokesfc"), 40)
        self.assertEqual(self.range_of("dustfine"), 50)
        self.assertEqual(self.range_of("dustcoarse"), 80)
        self.assertEqual(self.range_of("aotk"), 90)


class ConversionRuleTests(unittest.TestCase):
    RULE = rules.MASS_DENSITY_TO_MICROGRAMS

    def test_the_arithmetic(self) -> None:
        self.assertEqual(rules.expression(self.RULE, "[kg/(m^3)]"), "A*1000000000")
        plane = np.array([2.5e-8], dtype=np.float64)
        self.assertEqual(rules.convert(self.RULE, "kg/(m^3)", plane)[0], 2.5e-8 * 1e9)

    def test_another_unit_is_refused(self) -> None:
        with self.assertRaises(ConversionError):
            rules.expression(self.RULE, "10^-6g/m^3")


class FixtureTests(unittest.TestCase):
    def test_every_input_is_found_both_ways(self) -> None:
        fast = grib2.inspect_grib_fast(FIXTURE, RRFS.input_variable_ids)
        self.assertEqual(tuple(fast), RRFS.input_variable_ids)
        self.assertEqual(fast["smokesfc"].unit, "kg/(m^3)")

    @requires_gdalinfo
    def test_the_two_paths_agree(self) -> None:
        fast = grib2.inspect_grib_fast(FIXTURE, RRFS.input_variable_ids)
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            slow = inspect_grib_multi(FIXTURE, RRFS.input_variable_ids)
        for variable_id, frame in fast.items():
            self.assertEqual(frame, slow[variable_id], variable_id)


@requires_gdalinfo
class BuildTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-rrfs-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                FIXTURE,
                cls.root / "out",
                model="rrfs",
                work_root=cls.root / "work",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    def test_every_published_bundle_is_written_on_the_regular_grid(self) -> None:
        self.assertEqual(tuple(bundle["variable"] for bundle in self.report["bundles"]), published_bundle_ids(RRFS))
        grid = stored_bundle(self.root / "out", "smokesfc").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (160, 129))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.03, -0.03))

    def test_the_smoke_declares_what_it_is(self) -> None:
        variable = stored_bundle(self.root / "out", "smokesfc").metadata["variables"][0]
        self.assertEqual(variable["unit"], "µg/m³")
        parameter = variable["parameter"]
        self.assertEqual((parameter["parameterCategory"], parameter["parameterNumber"]), (20, 0))
        self.assertEqual((parameter["typeOfFirstFixedSurface"], parameter["scaledValueOfFirstFixedSurface"]), (103, 8))
        self.assertEqual(variable["aerosol"]["aerosolType"], 62010)

    def test_the_pressure_is_written_under_prmsl(self) -> None:
        parameter = stored_bundle(self.root / "out", "prmsl").metadata["variables"][0]["parameter"]
        self.assertEqual((parameter["parameterCategory"], parameter["parameterNumber"]), (3, 1))

    @requires_native_source("rrfs")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, FIXTURE, self.root / "out", self.report, self.root / "native", model="rrfs")


if __name__ == "__main__":
    unittest.main()

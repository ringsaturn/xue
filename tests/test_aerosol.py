"""The GEFS-Aerosols source: the aerosol member of NOAA's GEFS (the GOCART
model coupled to the GFS), read off the GEFS bucket at 0.25° as one GRIB2
per three-hourly frame with an ``.idx`` beside it, the pgrb2 way.

What is new with it is the identity. Every record is GRIB2 product
definition template 4.48, an aerosol product whose parameter triple names
the quantity (0/20/102, the optical thickness; NCEP's local 0/13/193 and
0/13/192, the fine and coarse particulate matter) while the aerosol type
and the size and wavelength intervals in the template tell the species and
the band apart — a dozen ``AOTK`` records on the entire atmosphere per frame.
So a variable's identity is its parameter block *and* an ``AerosolIdentity``
(``xuebuild/variables.py``), written beside the parameter as the metadata's
``aerosol`` block (docs/format.md §"Band, Producer and Aerosol"), read off
the template by the header index (``xuebuild/grib2.py``) and off GDAL's
assembled template values by the band matcher (``xuebuild/gdal.py``), and
spelled out in the ``.idx`` line after the forecast hour, which is what the
registry's ``index_qualifier`` is for. The fields take the precipitation
codebook's logarithmic shape with their own numbers.

``tests/fixtures/aerosol-registry.json`` is the committed golden that holds
the Python and Rust encoders and the frontend to one set of identities and
codebooks, aerosol blocks included. ``tests/fixtures/gefsaero.2026091900.f000.crop.grib2``
and ``f003`` are an 80 x 80 cell window of the analysis and the first step
of the 2026-09-19 00Z cycle over the Sahara and the Gulf of Guinea (10W–10E,
10N–30N), the nine records the source fetches in source order.
"""

from __future__ import annotations

import copy
import filecmp
import json
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np

from xuebuild import binconvert, grib2, native, quantize, zstdcli
from xuebuild.binconvert import analysis_optional_ids, published_bundle_ids, video_variable_ids
from xuebuild.binformat import Bundle, BundleError, read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import GEFS_BASE_URL, _run_is_complete, gefsaero_object_url, model_object_url, resolve_run
from xuebuild.gdal import (
    _band_matches,
    aerosol_optical_depth_expression,
    inspect_grib_multi,
    particulate_matter_expression,
    raster_expression,
)
from xuebuild.idx import field_byte_range
from xuebuild.model import GfsRun
from xuebuild.quantize import PROFILES
from xuebuild.sources import MODEL_CORE_BUNDLES, MODEL_PRODUCTS, source_spec
from xuebuild.stac import prose_document
from xuebuild.variables import AEROSOL_VARIABLE_IDS, VARIABLES, AerosolIdentity, variable_spec

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_FRAMES = [FIXTURES / "gefsaero.2026091900.f000.crop.grib2", FIXTURES / "gefsaero.2026091900.f003.crop.grib2"]
REGISTRY = FIXTURES / "aerosol-registry.json"
GEFSAERO = source_spec("gefsaero")
AOD_IDS = tuple(variable_id for variable_id in AEROSOL_VARIABLE_IDS if variable_id.startswith("aod"))
PM_IDS = ("pm25", "pm10", "pm10dust")
# Code table 4.233, by variable.
AEROSOL_TYPES = {
    "aod": 62000,
    "aoddust": 62001,
    "aodsalt": 62008,
    "aodsulf": 62006,
    "aodorg": 62010,
    "aodbc": 62009,
    "pm25": 62000,
    "pm10": 62000,
    "pm10dust": 62001,
}
# Two frames' worth of the real .idx sidecar, as the bucket serves it
# (2026-09-19 00Z): the analysis spells ``anl`` where every later frame
# spells the hour, and the qualifier sits after either.
IDX_F003 = """\
1:0:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2e-05:aerosol_wavelength >=3.38e-07,<=3.42e-07
2:964538:d=2026091900:ASYSFK:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2e-05:aerosol_wavelength >=3.38e-07,<=3.42e-07
3:2288376:d=2026091900:SSALBK:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2e-05:aerosol_wavelength >=3.38e-07,<=3.42e-07
4:3611986:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2e-05:aerosol_wavelength >=4.3e-07,<=4.5e-07
5:4562806:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2e-05:aerosol_wavelength >=5.45e-07,<=5.65e-07
6:5623507:d=2026091900:SCTAOTK:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2e-05:aerosol_wavelength >=5.45e-07,<=5.65e-07
7:6682180:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Dust dry:aerosol_size <2e-05:aerosol_wavelength >=5.45e-07,<=5.65e-07
8:7123695:d=2026091900:SCTAOTK:entire atmosphere:3 hour fcst:aerosol=Dust dry:aerosol_size <2e-05:aerosol_wavelength >=5.45e-07,<=5.65e-07
9:7559280:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Sea salt dry:aerosol_size <2e-05:aerosol_wavelength >=5.45e-07,<=5.65e-07
10:8596621:d=2026091900:SCTAOTK:entire atmosphere:3 hour fcst:aerosol=Sea salt dry:aerosol_size <2e-05:aerosol_wavelength >=5.45e-07,<=5.65e-07
11:9633962:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Sulphate dry:aerosol_size <2e-05:aerosol_wavelength >=5.45e-07,<=5.65e-07
12:10397801:d=2026091900:SCTAOTK:entire atmosphere:3 hour fcst:aerosol=Sulphate dry:aerosol_size <7e-07:aerosol_wavelength >=5.45e-07,<=5.65e-07
13:11161640:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Particulate organic matter dry:aerosol_size <2e-05:aerosol_wavelength >=5.45e-07,<=5.65e-07
14:11916746:d=2026091900:SCTAOTK:entire atmosphere:3 hour fcst:aerosol=Particulate organic matter dry:aerosol_size <7e-07:aerosol_wavelength >=5.45e-07,<=5.65e-07
15:12670993:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Black carbon dry:aerosol_size <2e-05:aerosol_wavelength >=5.45e-07,<=5.65e-07
16:13445788:d=2026091900:SCTAOTK:entire atmosphere:3 hour fcst:aerosol=Black carbon dry:aerosol_size <7e-07:aerosol_wavelength >=5.45e-07,<=5.65e-07
17:14249315:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2e-05:aerosol_wavelength >=6.2e-07,<=6.7e-07
18:15291401:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2e-05:aerosol_wavelength >=8.41e-07,<=8.76e-07
19:16435263:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2e-05:aerosol_wavelength >=1.628e-06,<=1.652e-06
20:17534194:d=2026091900:AOTK:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2e-05:aerosol_wavelength >=1.1e-05,<=1.12e-05
21:18558072:d=2026091900:PMTF:surface:3 hour fcst:aerosol=Dust dry:aerosol_size <2.5e-06:
22:18781717:d=2026091900:PMTC:surface:3 hour fcst:aerosol=Dust dry:aerosol_size <1e-05:
23:18970080:d=2026091900:PMTF:surface:3 hour fcst:aerosol=Sea salt dry:aerosol_size <2.5e-06:
24:19971452:d=2026091900:PMTC:surface:3 hour fcst:aerosol=Total aerosol:aerosol_size <1e-05:
25:20675299:d=2026091900:PMTF:surface:3 hour fcst:aerosol=Total aerosol:aerosol_size <2.5e-06:
26:21297635:d=2026091900:COLMD:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <1e-05:
27:22075049:d=2026091900:COLMD:entire atmosphere:3 hour fcst:aerosol=Total aerosol:aerosol_size <2.5e-06:
28:22946220:d=2026091900:COLMD:entire atmosphere:3 hour fcst:aerosol=Dust dry:aerosol_size <2.5e-06:
29:23377768:d=2026091900:COLMD:entire atmosphere:3 hour fcst:aerosol=Sea salt dry:aerosol_size <2.5e-06:
30:24385305:d=2026091900:COLMD:entire atmosphere:3 hour fcst:aerosol=Black carbon dry:aerosol_size <2.36e-08:
31:25086501:d=2026091900:COLMD:entire atmosphere:3 hour fcst:aerosol=Particulate organic matter dry:aerosol_size <4.24e-08:
32:25632077:d=2026091900:COLMD:entire atmosphere:3 hour fcst:aerosol=Sulphate dry:aerosol_size <2.5e-06:
"""
IDX_F000 = IDX_F003.replace(":3 hour fcst:", ":anl:")
# Google's copy of the bucket writes the same sidecar with the aerosol type
# in title case (``Dust Dry``, ``Total Aerosol``), the offsets unchanged.
IDX_F003_GOOGLE = (
    IDX_F003.replace("Total aerosol", "Total Aerosol")
    .replace("Dust dry", "Dust Dry")
    .replace("Sea salt dry", "Sea Salt Dry")
    .replace("Sulphate dry", "Sulphate Dry")
    .replace("Particulate organic matter dry", "Particulate Organic Matter Dry")
    .replace("Black carbon dry", "Black Carbon Dry")
)
# The record each variable is, 1-based, in the sidecar above.
IDX_RECORDS = {
    "aod": 5,
    "aoddust": 7,
    "aodsalt": 9,
    "aodsulf": 11,
    "aodorg": 13,
    "aodbc": 15,
    "pm25": 25,
    "pm10": 24,
    "pm10dust": 22,
}

requires_gdalinfo = unittest.skipUnless(shutil.which("gdalinfo") is not None, "gdalinfo is not on PATH")


def registry_entry(variable_id: str) -> dict:
    """The registry as the implementations must agree it is."""
    spec = variable_spec(variable_id)
    assert spec.grib2_aerosol is not None
    return {
        "label": spec.label,
        "unit": spec.output_unit,
        "parameter": spec.parameter_metadata(),
        "aerosol": spec.grib2_aerosol.metadata(),
        "quality": PROFILES["quality"][variable_id].metadata(),
        "compact": PROFILES["compact"][variable_id].metadata(),
    }


class RegistryTests(unittest.TestCase):
    def test_the_committed_registry_still_describes_this_encoder(self) -> None:
        expected = json.loads(REGISTRY.read_text(encoding="utf-8"))
        actual = {variable_id: registry_entry(variable_id) for variable_id in AEROSOL_VARIABLE_IDS}
        self.assertEqual(list(actual), list(expected), "the registry's order is the source's")
        self.assertEqual(
            actual,
            expected,
            "the aerosol registry moved; the Rust encoder and the frontend read the same "
            "fixture, so change it deliberately and change all three",
        )

    def test_every_aerosol_variable_carries_the_whole_identity(self) -> None:
        for variable_id in AEROSOL_VARIABLE_IDS:
            spec = variable_spec(variable_id)
            with self.subTest(variable=variable_id):
                identity = spec.grib2_aerosol
                self.assertIsNotNone(identity)
                assert identity is not None
                self.assertEqual(identity.aerosol_type, AEROSOL_TYPES[variable_id])
                self.assertEqual(spec.grib2_aliases, ())
                self.assertEqual(spec.grib2_alternates, ())
                self.assertEqual(spec.grib2_statistical, None)
                self.assertTrue(spec.index_field and spec.index_qualifier)
                self.assertEqual(spec.grib2_discipline, 0)
        for variable_id in AOD_IDS:
            spec = variable_spec(variable_id)
            self.assertEqual((spec.grib2_category, spec.grib2_number, spec.grib2_level_type, spec.grib2_level_value), (20, 102, 10, None))
            self.assertEqual((spec.grib_element, spec.gdal_unit, spec.output_unit), ("AOTK", "Numeric", "1"))
            identity = spec.grib2_aerosol
            assert identity is not None
            self.assertEqual((identity.size_type, identity.size_first, identity.size_second), (0, (6, 20), (0, 0)))
            self.assertEqual((identity.wavelength_type, identity.wavelength_first, identity.wavelength_second), (7, (9, 545), (9, 565)))
        for variable_id, number, element, limit in (("pm25", 193, "PMTF", (7, 25)), ("pm10", 192, "PMTC", (6, 10)), ("pm10dust", 192, "PMTC", (6, 10))):
            spec = variable_spec(variable_id)
            self.assertEqual((spec.grib2_category, spec.grib2_number, spec.grib2_level_type, spec.grib2_level_value), (13, number, 1, 0.0))
            self.assertEqual((spec.grib_element, spec.gdal_unit, spec.output_unit), (element, "10^-6g/m^3", "µg/m³"))
            identity = spec.grib2_aerosol
            assert identity is not None
            self.assertEqual((identity.size_type, identity.size_first, identity.size_second), (0, limit, (0, 0)))
            self.assertEqual(identity.wavelength_type, AerosolIdentity.MISSING_TYPE)
            self.assertIsNone(identity.wavelength_first)
            self.assertIsNone(identity.wavelength_second)
        # No other variable is an aerosol product.
        self.assertEqual(
            [variable_id for variable_id, spec in VARIABLES.items() if spec.grib2_aerosol is not None],
            list(AEROSOL_VARIABLE_IDS),
        )

    def test_a_missing_interval_carries_no_limits(self) -> None:
        with self.assertRaises(ValueError):
            AerosolIdentity(62000, size_type=255, size_first=(7, 25))
        with self.assertRaises(ValueError):
            AerosolIdentity(62000, wavelength_type=255, wavelength_second=(9, 565))
        block = AerosolIdentity(62000).metadata()
        self.assertEqual(len(block), 11)
        self.assertEqual((block["typeOfSizeInterval"], block["typeOfWavelengthInterval"]), (255, 255))
        self.assertEqual([value for key, value in block.items() if key.startswith("scale")], [None] * 8)

    def test_the_codebooks_are_logarithmic_and_the_converter_clamps_at_their_maximum(self) -> None:
        for variable_id in AEROSOL_VARIABLE_IDS:
            with self.subTest(variable=variable_id):
                quality = PROFILES["quality"][variable_id]
                compact = PROFILES["compact"][variable_id]
                self.assertIsInstance(quality, quantize.PrecipitationCodebook)
                self.assertEqual(PROFILES["balanced"][variable_id], quality)
                self.assertEqual(quality.name, variable_id)
                self.assertEqual((quality.maximum_code, quality.overflow_code, quality.nodata_code), (253, 254, 255))
                self.assertEqual((compact.maximum_code, compact.overflow_code, compact.nodata_code), (125, 126, 127))
                self.assertEqual((compact.trace, compact.scale, compact.maximum), (quality.trace, quality.scale, quality.maximum))
                self.assertEqual(quality.maximum, float(variable_spec(variable_id).value_range[1]))
                # The expression clamps at the maximum, so the overflow code
                # is never written and a value above the maximum decodes to
                # the maximum's own code.
                top = quality.quantize(np.array([quality.maximum]))
                self.assertEqual(int(top[0]), quality.maximum_code)
                self.assertEqual(int(quality.quantize(np.array([0.0]))[0]), 0)
                self.assertEqual(int(quality.quantize(np.array([quality.trace / 2]))[0]), 0)
        self.assertEqual((PROFILES["quality"]["aod"].trace, PROFILES["quality"]["aod"].scale, PROFILES["quality"]["aod"].maximum), (0.005, 0.05, 5.0))
        self.assertEqual((PROFILES["quality"]["pm25"].trace, PROFILES["quality"]["pm25"].scale, PROFILES["quality"]["pm25"].maximum), (0.5, 5.0, 1000.0))
        for variable_id in ("pm10", "pm10dust"):
            self.assertEqual((PROFILES["quality"][variable_id].trace, PROFILES["quality"][variable_id].scale, PROFILES["quality"][variable_id].maximum), (0.5, 5.0, 2000.0))
        # A code resolves a hundredth of an optical depth at 0.1 and a
        # microgram at the PM2.5 air quality thresholds.
        aod = PROFILES["quality"]["aod"]
        self.assertLess(aod.decode(aod.quantize(np.array([0.1])) + 1)[0] - 0.1, 0.01)
        pm25 = PROFILES["quality"]["pm25"]
        self.assertLess(pm25.decode(pm25.quantize(np.array([35.0])) + 1)[0] - 35.0, 1.0)

    def test_the_raster_expressions_accept_gdal_s_units_and_nothing_else(self) -> None:
        for unit in ("[Numeric]", "Numeric", "-", ""):
            self.assertEqual(aerosol_optical_depth_expression(unit), "maximum(0,minimum(5,A))")
        for unit in ("[10^-6g/m^3]", "10^-6g/m^3", "ug/m^3", "µg/m³"):
            self.assertEqual(particulate_matter_expression(unit, maximum=1000), "maximum(0,minimum(1000,A))")
        with self.assertRaises(ConversionError):
            aerosol_optical_depth_expression("kg/m^3")
        with self.assertRaises(ConversionError):
            particulate_matter_expression("kg/m^3", maximum=1000)
        for variable_id in AOD_IDS:
            self.assertEqual(raster_expression(variable_id, "[Numeric]"), "maximum(0,minimum(5,A))")
        self.assertEqual(raster_expression("pm25", "[10^-6g/m^3]"), "maximum(0,minimum(1000,A))")
        self.assertEqual(raster_expression("pm10", "[10^-6g/m^3]"), "maximum(0,minimum(2000,A))")
        self.assertEqual(raster_expression("pm10dust", "[10^-6g/m^3]"), "maximum(0,minimum(2000,A))")


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_publishes_the_nine_and_nothing_else(self) -> None:
        self.assertEqual(GEFSAERO.input_variable_ids, AEROSOL_VARIABLE_IDS)
        self.assertEqual(published_bundle_ids(GEFSAERO), AEROSOL_VARIABLE_IDS)
        self.assertEqual(GEFSAERO.bundle_vector_ids, ())
        self.assertEqual(GEFSAERO.companion_files, ())
        self.assertEqual(GEFSAERO.core_bundle_ids, ("aod",))
        self.assertEqual(analysis_optional_ids(GEFSAERO, published_bundle_ids(GEFSAERO)), ())
        self.assertEqual(GEFSAERO.statistical_processes, ())
        self.assertFalse(GEFSAERO.accumulated_precipitation)
        self.assertFalse(GEFSAERO.observation)
        self.assertTrue(GEFSAERO.live and GEFSAERO.fetched)

    def test_the_axis_is_three_hourly_to_120_from_every_cycle(self) -> None:
        self.assertEqual(GEFSAERO.steps, ((120, 3),))
        self.assertEqual(GEFSAERO.horizon_hours, 120)
        self.assertEqual(GEFSAERO.cycle_hours, 6)
        self.assertEqual(GEFSAERO.forecast_hours(120), list(range(0, 121, 3)))
        self.assertEqual(len(GEFSAERO.forecast_hours(120)), 41)
        with self.assertRaises(DownloadError):
            GEFSAERO.forecast_hours(4)

    def test_the_identity_strings(self) -> None:
        self.assertEqual(
            (GEFSAERO.manifest_model, GEFSAERO.product, GEFSAERO.latest_filename),
            ("GEFS-AEROSOLS", "chem-a2d-0p25", "latest-gefsaero.json"),
        )
        self.assertEqual(MODEL_PRODUCTS["GEFS-AEROSOLS"], "chem-a2d-0p25")
        self.assertEqual(MODEL_CORE_BUNDLES["GEFS-AEROSOLS"], ("aod",))
        self.assertEqual((GEFSAERO.production_grid, GEFSAERO.tile, GEFSAERO.variant_factors), ((1440, 721), (48, 52), (2,)))
        self.assertFalse(GEFSAERO.video)
        self.assertEqual(video_variable_ids(GEFSAERO), frozenset())
        prose = prose_document()["sources"]["gefsaero"]
        self.assertEqual(prose["license"], "other")
        self.assertEqual(prose["providers"][0]["name"], "NOAA / NCEP")


class FetchTests(unittest.TestCase):
    def test_the_object_is_the_chem_member_s_quarter_degree_file(self) -> None:
        run = GfsRun(datetime(2026, 9, 19, 0, tzinfo=UTC))
        self.assertEqual(
            gefsaero_object_url(run, 3),
            f"{GEFS_BASE_URL}/gefs.20260919/00/chem/pgrb2ap25/gefs.chem.t00z.a2d_0p25.f003.grib2",
        )
        self.assertEqual(model_object_url(run, 120, "gefsaero"), gefsaero_object_url(run, 120))
        self.assertTrue(GEFS_BASE_URL.startswith("https://"))
        self.assertFalse(GEFS_BASE_URL.endswith("/"))

    def test_the_idx_qualifier_names_exactly_one_record(self) -> None:
        for text, size in ((IDX_F000, 26_601_934), (IDX_F003, 26_399_859), (IDX_F003_GOOGLE, 26_399_859)):
            lines = text.splitlines()
            for variable_id, record in IDX_RECORDS.items():
                spec = variable_spec(variable_id)
                with self.subTest(variable=variable_id, sidecar=lines[0][-60:]):
                    byte_range = field_byte_range(
                        text,
                        spec.index_field,
                        file_size=size,
                        qualifier=spec.index_qualifier,
                        excluded_phrases=spec.excluded_index_phrases,
                        alternate_fields=spec.alternate_index_fields,
                    )
                    self.assertEqual(byte_range.start, int(lines[record - 1].split(":")[1]))
                    next_offset = int(lines[record].split(":")[1]) if record < len(lines) else size
                    self.assertEqual(byte_range.end, next_offset - 1)
        # Without the qualifier the element and surface name twelve records.
        with self.assertRaisesRegex(DownloadError, "found 12"):
            field_byte_range(IDX_F003, ":AOTK:entire atmosphere:")
        with self.assertRaisesRegex(DownloadError, "found 3"):
            field_byte_range(IDX_F003, ":PMTF:surface:")

    def test_a_run_is_complete_when_both_ends_are_on_the_bucket(self) -> None:
        run = GfsRun(datetime(2026, 9, 19, 0, tzinfo=UTC))
        present = {gefsaero_object_url(run, 0), gefsaero_object_url(run, 120)}
        self.assertTrue(_run_is_complete(run, 120, "gefsaero", lambda url: url in present))
        self.assertFalse(_run_is_complete(run, 120, "gefsaero", lambda url: url == gefsaero_object_url(run, 0)))
        # Every cycle carries the whole axis, so the newest complete cycle
        # is the one --run latest resolves to.
        now = datetime(2026, 9, 19, 13, tzinfo=UTC)
        complete = GfsRun(datetime(2026, 9, 19, 6, tzinfo=UTC))
        ends = {gefsaero_object_url(complete, 0), gefsaero_object_url(complete, 120)}
        self.assertEqual(resolve_run("latest", hours=120, now=now, exists=lambda url: url in ends, model="gefsaero"), complete)


class MatcherTests(unittest.TestCase):
    def test_the_header_index_finds_every_input_in_source_order(self) -> None:
        for path, lead in zip(FIXTURE_FRAMES, (0, 3 * 3600)):
            frames = grib2.inspect_grib_fast(path, GEFSAERO.input_variable_ids)
            self.assertEqual(len(frames), 9, path.name)
            self.assertEqual([frames[variable_id].band for variable_id in GEFSAERO.input_variable_ids], list(range(1, 10)))
            for variable_id, frame in frames.items():
                self.assertEqual(frame.lead_seconds, lead, variable_id)
                self.assertEqual(frame.unit, variable_spec(variable_id).gdal_unit, variable_id)
                self.assertEqual(frame.run_time, datetime(2026, 9, 19, 0, tzinfo=UTC))

    def test_the_records_carry_the_registered_aerosol_identity(self) -> None:
        messages = grib2.index_messages(FIXTURE_FRAMES[1])
        self.assertEqual(len(messages), 9)
        for message, variable_id in zip(messages, AEROSOL_VARIABLE_IDS):
            spec = variable_spec(variable_id)
            with self.subTest(variable=variable_id):
                self.assertEqual(
                    (message.discipline, message.parameter_category, message.parameter_number, message.level_type),
                    (0, spec.grib2_category, spec.grib2_number, spec.grib2_level_type),
                )
                self.assertIsNone(message.statistical_process)
                self.assertEqual(message.aerosol, spec.grib2_aerosol)
        # A record of another species matches nothing but its own variable,
        # and a variable of another quantity matches no aerosol record.
        dust = messages[1]
        self.assertEqual([variable_id for variable_id in AEROSOL_VARIABLE_IDS if grib2._matches(variable_spec(variable_id), dust)], ["aoddust"])
        for variable_id in ("tcdc", "cref", "tmp2m", "prate"):
            self.assertFalse(any(grib2._matches(variable_spec(variable_id), message) for message in messages), variable_id)

    def test_a_record_of_the_wrong_band_or_size_is_not_the_variable(self) -> None:
        message = next(message for message in grib2.index_messages(FIXTURE_FRAMES[0]) if message.band == 1)
        assert message.aerosol is not None
        other_band = AerosolIdentity(
            62000, size_type=0, size_first=(6, 20), size_second=(0, 0), wavelength_type=7, wavelength_first=(9, 430), wavelength_second=(9, 450)
        )
        self.assertNotEqual(message.aerosol, other_band)
        from dataclasses import replace

        self.assertFalse(grib2._matches(variable_spec("aod"), replace(message, aerosol=other_band)))
        self.assertFalse(grib2._matches(variable_spec("aod"), replace(message, aerosol=None)))

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            for path in FIXTURE_FRAMES:
                fast = grib2.inspect_grib_fast(path, GEFSAERO.input_variable_ids)
                slow = inspect_grib_multi(path, GEFSAERO.input_variable_ids)
                self.assertEqual(set(fast), set(slow), path.name)
                for variable_id, frame in fast.items():
                    self.assertEqual(frame, slow[variable_id], f"{path.name} {variable_id}")

    def test_the_band_matcher_reads_the_assembled_template(self) -> None:
        dust = {
            "GRIB_ELEMENT": "AOTK",
            "GRIB_DISCIPLINE": "0(Meteorological)",
            "GRIB_PDS_PDTN": "48",
            "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": "20 102 62001 0 6 20 0 0 7 9 545 9 565 2 0 96 0 0 1 3 10 0 0 255 0 0",
        }
        self.assertTrue(_band_matches("aoddust", dust, ""))
        self.assertFalse(_band_matches("aod", dust, ""))
        self.assertFalse(_band_matches("pm10dust", dust, ""))
        pm10 = {
            "GRIB_ELEMENT": "PMTC",
            "GRIB_DISCIPLINE": "0(Meteorological)",
            "GRIB_PDS_PDTN": "48",
            "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": "13 192 62000 0 6 10 0 0 255 0 0 0 0 2 0 96 0 0 1 3 1 0 0 255 0 0",
        }
        self.assertTrue(_band_matches("pm10", pm10, ""))
        self.assertFalse(_band_matches("pm10dust", pm10, ""))
        self.assertFalse(_band_matches("pm25", pm10, ""))
        # Another template, an unassembled one, or another discipline is
        # not this record whatever the element says.
        self.assertFalse(_band_matches("pm10", {**pm10, "GRIB_PDS_PDTN": "0"}, ""))
        self.assertFalse(_band_matches("pm10", {**pm10, "GRIB_DISCIPLINE": "2(Land)"}, ""))
        self.assertFalse(_band_matches("pm10", {key: value for key, value in pm10.items() if key != "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES"}, ""))
        self.assertFalse(_band_matches("pm10", {**pm10, "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": "13 192"}, ""))
        # The 340 nm total is not the 550 nm total.
        uv = {**dust, "GRIB_PDS_TEMPLATE_ASSEMBLED_VALUES": "20 102 62000 0 6 20 0 0 7 9 338 9 342 2 0 96 0 0 1 3 10 0 0 255 0 0"}
        self.assertFalse(_band_matches("aod", uv, ""))


class ConversionTests(unittest.TestCase):
    """The two-frame fixture through the reference pipeline."""

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-gefsaero-"))
        cls.output = cls.root / "gefsaero.2026091900"
        cls.report = binconvert.convert_bin(
            FIXTURE_FRAMES,
            cls.output,
            work_root=cls.root / "work",
            manifest_path=cls.output / "manifest.json",
            model="gefsaero",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def _plane(self, variable_id: str, offset: int) -> tuple[np.ndarray, dict]:
        bundle = read_bundle(self.output / f"{variable_id}.xue")
        return bundle.decode_plane(1, offset), bundle.metadata

    def test_every_published_bundle_is_written_and_no_video(self) -> None:
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["model"], "GEFS-AEROSOLS")
        self.assertEqual(manifest["product"], "chem-a2d-0p25")
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], list(AEROSOL_VARIABLE_IDS))
        self.assertFalse(any("video" in bundle for bundle in manifest["bundles"]))
        self.assertEqual(self.report["videos"], [])
        self.assertEqual(self.report["precipitationOverflowPoints"], 0)

    def test_the_metadata_carries_the_registered_aerosol_block(self) -> None:
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        for variable_id in AEROSOL_VARIABLE_IDS:
            _, metadata = self._plane(variable_id, 0)
            variable = metadata["variables"][0]
            with self.subTest(variable=variable_id):
                self.assertEqual(metadata["schemaVersion"], 3)
                self.assertEqual(metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 3})
                self.assertEqual(variable["id"], variable_id)
                self.assertEqual(variable["parameter"], registry[variable_id]["parameter"])
                self.assertEqual(variable["aerosol"], registry[variable_id]["aerosol"])
                self.assertEqual(variable["quantization"], registry[variable_id]["quality"])
                self.assertNotIn("band", variable)
                self.assertNotIn("producer", variable)
                self.assertNotIn("typeOfStatisticalProcessing", variable["parameter"])

    def test_the_dust_plume_reaches_the_codebook_and_nothing_overflows(self) -> None:
        codes, _ = self._plane("aoddust", 3)
        codebook = PROFILES["balanced"]["aoddust"]
        self.assertNotIn(codebook.overflow_code, codes)
        self.assertNotIn(codebook.nodata_code, codes)
        depths = codebook.decode(codes)
        self.assertGreater(float(depths.max()), 0.9, "the Saharan plume in the window reads 0.96")
        self.assertLess(float(depths.max()), 1.0)
        codes, _ = self._plane("pm10dust", 3)
        codebook = PROFILES["balanced"]["pm10dust"]
        self.assertNotIn(codebook.overflow_code, codes)
        concentrations = codebook.decode(codes)
        self.assertGreater(float(concentrations.max()), 1500.0, "the plume's surface PM10 reads 1600 µg/m³")
        self.assertLess(float(concentrations.max()), 1700.0)
        # Sea salt is at its trace over the desert and present over the
        # gulf; the total is never below its largest component.
        salt, _ = self._plane("aodsalt", 0)
        self.assertGreater(float(np.mean(salt == 0)), 0.0)
        self.assertGreater(int(salt.max()), 0)
        total, _ = self._plane("aod", 0)
        dust, _ = self._plane("aoddust", 0)
        self.assertTrue(np.all(total.astype(int) >= dust.astype(int) - 1))

    def test_the_half_tier_and_the_poster_follow(self) -> None:
        for variable_id in AEROSOL_VARIABLE_IDS:
            self.assertTrue((self.output / f"{variable_id}.half.xue").is_file(), variable_id)
            self.assertTrue((self.output / f"{variable_id}.poster.bin").is_file(), variable_id)
        half = read_bundle(self.output / "pm25.half.xue")
        self.assertEqual(half.metadata["variables"][0]["aerosol"], variable_spec("pm25").grib2_aerosol.metadata())


class MetadataValidationTests(unittest.TestCase):
    """The reader's rules for the block, on a written bundle's metadata with
    each malformation in turn."""

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-gefsaero-meta-"))
        output = cls.root / "gefsaero.2026091900"
        binconvert.convert_bin(
            FIXTURE_FRAMES[:1],
            output,
            work_root=cls.root / "work",
            model="gefsaero",
            bundle_ids=("pm25", "aod"),
        )
        cls.pm25 = read_bundle(output / "pm25.xue").metadata
        cls.aod = read_bundle(output / "aod.xue").metadata

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @staticmethod
    def _parse(metadata: dict) -> None:
        """The reader's metadata pass alone, on a document rather than a
        file: what a container carries is bytes at an offset, and the pass
        reads nothing else."""
        bundle = Bundle.__new__(Bundle)
        encoded = json.dumps(metadata).encode("utf-8")
        bundle.data = encoded
        bundle.metadata_offset = 0
        bundle.metadata_length = len(encoded)
        bundle._parse_metadata()

    def _edited(self, base: dict, edit) -> dict:
        metadata = copy.deepcopy(base)
        edit(metadata["variables"][0])
        return metadata

    def test_the_written_block_is_accepted(self) -> None:
        self._parse(self.pm25)
        self._parse(self.aod)
        self.assertEqual(self.pm25["variables"][0]["aerosol"]["typeOfWavelengthInterval"], 255)
        self.assertEqual(self.aod["variables"][0]["aerosol"]["typeOfWavelengthInterval"], 7)

    def test_a_variable_without_the_block_is_still_accepted(self) -> None:
        def drop_block(variable):
            del variable["aerosol"]

        self._parse(self._edited(self.pm25, drop_block))

    def test_malformed_blocks_are_refused(self) -> None:
        def drop_key(variable):
            del variable["aerosol"]["scaledValueOfSecondSize"]

        def extra_key(variable):
            variable["aerosol"]["x"] = 1

        def half_null(variable):
            variable["aerosol"]["scaleFactorOfFirstSize"] = None

        def null_under_a_present_type(variable):
            variable["aerosol"]["scaleFactorOfFirstSize"] = None
            variable["aerosol"]["scaledValueOfFirstSize"] = None

        def value_under_a_missing_type(variable):
            variable["aerosol"]["scaleFactorOfFirstWavelength"] = 9
            variable["aerosol"]["scaledValueOfFirstWavelength"] = 545

        def type_out_of_range(variable):
            variable["aerosol"]["aerosolType"] = 65536

        def interval_type_out_of_range(variable):
            variable["aerosol"]["typeOfSizeInterval"] = 256

        def scale_out_of_range(variable):
            variable["aerosol"]["scaleFactorOfFirstSize"] = 128

        def value_out_of_range(variable):
            variable["aerosol"]["scaledValueOfFirstSize"] = 0xFFFFFFFF

        def a_bool(variable):
            variable["aerosol"]["typeOfSizeInterval"] = True

        def a_float(variable):
            variable["aerosol"]["aerosolType"] = 62000.0

        def not_an_object(variable):
            variable["aerosol"] = [1]

        def null_block(variable):
            variable["aerosol"] = None

        edits = (
            drop_key,
            extra_key,
            half_null,
            null_under_a_present_type,
            value_under_a_missing_type,
            type_out_of_range,
            interval_type_out_of_range,
            scale_out_of_range,
            value_out_of_range,
            a_bool,
            a_float,
            not_an_object,
            null_block,
        )
        for edit in edits:
            with self.subTest(edit=edit.__name__):
                with self.assertRaisesRegex(BundleError, "aerosol"):
                    self._parse(self._edited(self.pm25, edit))
        # The AOD block, whose wavelength interval is present, refuses a
        # null limit the same way.
        def null_wavelength(variable):
            variable["aerosol"]["scaleFactorOfSecondWavelength"] = None
            variable["aerosol"]["scaledValueOfSecondWavelength"] = None

        with self.assertRaisesRegex(BundleError, "aerosol"):
            self._parse(self._edited(self.aod, null_wavelength))

    def test_a_block_below_schema_version_3_is_refused(self) -> None:
        # The parameter block is what makes a file version 3; a block
        # beside a parameter-less variable at version 2 is refused as such.
        metadata = copy.deepcopy(self.aod)
        metadata["schemaVersion"] = 2
        del metadata["variables"][0]["parameter"]
        metadata["time"] = {"firstForecastHour": 0, "stepHours": 3, "frameCount": 1}
        with self.assertRaisesRegex(BundleError, "aerosol block requires schemaVersion 3"):
            self._parse(metadata)


@unittest.skipUnless(native.available(), f"{native.DISTRIBUTION} is not installed")
class NativeParityTests(unittest.TestCase):
    """The same two frames through both encoders, byte for byte: the
    template 4.48 identity through both matchers, the logarithmic codebooks
    and the aerosol block, on real GEFS-Aerosols records."""

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        if not native.knows_source("gefsaero"):
            raise unittest.SkipTest(f"the installed {native.DISTRIBUTION} wheel predates the GEFS-Aerosols source")
        cls.root = Path(tempfile.mkdtemp(prefix="xue-gefsaero-parity-"))
        cls.reference, cls.reference_report = cls._build(binconvert, "reference")
        try:
            cls.subject, cls.subject_report = cls._build(native, "subject")
        except ConversionError as exc:
            if "out of step" in str(exc):
                shutil.rmtree(cls.root, ignore_errors=True)
                raise unittest.SkipTest(f"the installed {native.DISTRIBUTION} wheel predates the GEFS-Aerosols source: {exc}")
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _build(cls, implementation, name: str) -> tuple[Path, dict]:
        run_directory = cls.root / name / "gefsaero.2026091900"
        report = implementation.convert_bin(
            FIXTURE_FRAMES,
            run_directory,
            work_root=cls.root / f"{name}-work",
            manifest_path=run_directory / "manifest.json",
            latest_path=cls.root / name / "latest-gefsaero.json",
            run_id="2026091900",
            model="gefsaero",
        )
        return cls.root / name, report

    def test_every_artifact_is_byte_identical(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI below Python 3.14")
        if self.reference_report["zstdVersion"] != self.subject_report["zstdVersion"]:
            self.skipTest("libzstd differs between the reference and the wheel")
        reference = sorted(path.relative_to(self.reference).as_posix() for path in self.reference.rglob("*"))
        subject = sorted(path.relative_to(self.subject).as_posix() for path in self.subject.rglob("*"))
        self.assertEqual(reference, subject)
        self.assertTrue(any(name.endswith("pm10dust.xue") for name in reference))
        for relative in reference:
            path = self.reference / relative
            if path.is_dir():
                continue
            with self.subTest(artifact=relative):
                self.assertTrue(filecmp.cmp(path, self.subject / relative, shallow=False), f"{relative} differs")


if __name__ == "__main__":
    unittest.main()

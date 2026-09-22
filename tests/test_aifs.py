"""The AIFS source: ECMWF's data-driven model from the same open data
service as the IFS, fetched by the IFS path under another model directory
(``aifs-single`` beside ``ifs``, :data:`xuebuild.fetch.ECMWF_OPEN_DATA_MODELS`),
every cycle six-hourly to 360 hours.

What is new with it. The open data encodes four fields differently from
the IFS: the run-total precipitation is the WMO 0/1/52 (a *rate* to GDAL's
tables, TPRATE in kg/(m^2*s)) written as an accumulation in kg/m², a
millimetre, where the IFS's local 0/1/193 is in metres; the total cloud
cover is the WMO 0/6/1 in percent as a layer from the ground surface to
the top of the atmosphere, where the IFS's is the local 0/6/192 fraction;
and the three layer cloud covers, which IFS open data does not carry, sit
on ECMWF's layer boundaries as first fixed surfaces (the ground, 800 hPa,
450 hPa) rather than GRIB2's cloud layer surfaces (214 / 224 / 234). Each
is a ``RecordAlternate`` accepted under the GFS identity, and the
millimetre accumulation is a unit rule (``precipitation_accumulation_is_mm``)
the converter reads off the alternate's unit so the plane is not scaled
up a thousandfold.

``tests/fixtures/aifs.2026091700.f000.crop.grib2`` and ``f006`` are the
same 80 x 80 cell Kara Sea window as the IFS fixture (60–80E, 65–85N: sea
ice, open water and land), the analysis and the first step of the
2026-09-17 00Z cycle, every record the source fetches in source order —
thirty at each, the ``oper`` stream's then the two ``wave`` records.
"""

from __future__ import annotations

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
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    ECMWF_BASE_URLS,
    ECMWF_OPEN_DATA_MODELS,
    _run_is_complete,
    ecmwf_companion_object_url,
    ecmwf_object_url,
    model_object_url,
    resolve_run,
)
from xuebuild.gdal import (
    accumulation_expression,
    inspect_grib_multi,
    precipitation_accumulation_is_mm,
    raster_expression,
)
from xuebuild.model import GfsRun
from xuebuild.sources import MODEL_CORE_BUNDLES, MODEL_PRODUCTS, source_spec
from xuebuild.stac import prose_document
from xuebuild.variables import variable_spec

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_FRAMES = [FIXTURES / "aifs.2026091700.f000.crop.grib2", FIXTURES / "aifs.2026091700.f006.crop.grib2"]
AIFS = source_spec("aifs")
ECMWF = source_spec("ecmwf")
GFS = source_spec("gfs")
# What AIFS open data does not carry of the IFS set (no relative humidity on
# any pressure level, no gust, CAPE, ice thickness or peak period), what the
# IFS source publishes on the isobaric surfaces the AIFS source has not
# taken up yet (the heights, temperatures and winds beyond the synoptic
# levels; the records exist in the AIFS open data), and what AIFS adds.
DROPPED = tuple(f"rh{level}" for level in (1000, 925, 850, 700, 500, 300, 250, 200)) + ("gust", "cape", "icetk", "perpw")
NOT_TAKEN_UP = (
    "hgt1000", "hgt925", "hgt300", "hgt200",
    "tmp1000", "tmp700", "tmp300", "tmp250", "tmp200",
    "wind1000", "wind700", "wind500", "wind300", "wind200",
)
ADDED = ("lcdc", "mcdc", "hcdc")

requires_gdalinfo = unittest.skipUnless(shutil.which("gdalinfo") is not None, "gdalinfo is not on PATH")


class SourceRegistryTests(unittest.TestCase):
    def test_aifs_publishes_the_ifs_set_less_what_it_lacks_plus_the_cloud_layers(self) -> None:
        gfs = published_bundle_ids(GFS)
        ecmwf = published_bundle_ids(ECMWF)
        aifs = published_bundle_ids(AIFS)
        self.assertEqual([bundle_id for bundle_id in gfs if bundle_id in aifs], list(aifs), "GFS order")
        for bundle_id in DROPPED:
            self.assertIn(bundle_id, ecmwf)
            self.assertNotIn(bundle_id, aifs)
        for bundle_id in ADDED:
            self.assertNotIn(bundle_id, ecmwf)
            self.assertIn(bundle_id, aifs)
        self.assertEqual(set(aifs) - set(ecmwf), set(ADDED))
        self.assertEqual(set(ecmwf) - set(aifs), set(DROPPED) | set(NOT_TAKEN_UP))
        self.assertEqual(len(aifs), 28)
        # The 850 hPa specific humidity still feeds the two derivations.
        self.assertIn("qflux850", aifs)
        self.assertIn("thetae850", aifs)
        self.assertIn("wave", aifs)

    def test_the_axis_is_six_hourly_to_360_from_every_cycle(self) -> None:
        self.assertEqual(AIFS.steps, ((360, 6),))
        self.assertEqual(AIFS.horizon_hours, 360)
        self.assertEqual(AIFS.cycle_hours, 6)
        self.assertEqual(AIFS.forecast_hours(360), list(range(0, 361, 6)))
        with self.assertRaises(DownloadError):
            AIFS.forecast_hours(3)

    def test_the_identity_strings(self) -> None:
        self.assertEqual((AIFS.manifest_model, AIFS.product, AIFS.latest_filename), ("AIFS", "aifs-single-0p25", "latest-aifs.json"))
        self.assertEqual(MODEL_PRODUCTS["AIFS"], "aifs-single-0p25")
        self.assertEqual(MODEL_CORE_BUNDLES["AIFS"], ("tmp2m", "prate"))
        self.assertEqual(AIFS.production_grid, ECMWF.production_grid)
        self.assertEqual(AIFS.tile, ECMWF.tile)
        self.assertFalse(AIFS.video)
        self.assertEqual(video_variable_ids(AIFS), frozenset())
        self.assertIn("aifs", prose_document()["sources"])
        self.assertEqual(prose_document()["sources"]["aifs"]["license"], "CC-BY-4.0")

    def test_every_input_names_its_open_data_record(self) -> None:
        for variable_id in AIFS.input_variable_ids:
            self.assertNotEqual(variable_spec(variable_id).ecmwf_param, "", variable_id)
        self.assertEqual(
            {variable_id: variable_spec(variable_id).ecmwf_param for variable_id in ("tcdc", *ADDED, "tmpsfc", "htsgw", "dirpw")},
            {"tcdc": "tcc", "lcdc": "lcc", "mcdc": "mcc", "hcdc": "hcc", "tmpsfc": "skt", "htsgw": "swh", "dirpw": "mwd"},
        )
        self.assertEqual(AIFS.companion_files[0].variable_ids, ("htsgw", "dirpw"))
        self.assertEqual(AIFS.primary_input_ids(), AIFS.input_variable_ids[:-2])

    def test_only_the_rate_is_absent_at_the_analysis(self) -> None:
        self.assertEqual(AIFS.optional_at_analysis, ())
        self.assertEqual(analysis_optional_ids(AIFS, published_bundle_ids(AIFS)), ("prate",))
        self.assertEqual(AIFS.statistical_processes, (("prate", 0),))


class RegistryAlternateTests(unittest.TestCase):
    def test_the_five_aifs_encodings_are_alternates_of_the_gfs_identities(self) -> None:
        tp = variable_spec("tp")
        (aifs_tp,) = tp.grib2_alternates
        self.assertEqual((aifs_tp.triple, aifs_tp.level_type, aifs_tp.statistical, aifs_tp.gdal_unit), ((0, 1, 52), 1, 1, "kg/(m^2*s)"))
        self.assertEqual((tp.grib2_number, tp.gdal_unit), (193, "-"), "the IFS record is still the identity")
        tcdc = variable_spec("tcdc")
        # The third alternate is NCEP's CFSv2 spelling of the identity's own
        # surface, not an AIFS encoding (tests/test_cfs.py).
        self.assertEqual(
            [(alternate.triple, alternate.level_type) for alternate in tcdc.grib2_alternates],
            [((0, 6, 192), 1), ((0, 6, 1), 1), ((0, 6, 1), 200)],
        )
        self.assertEqual(tcdc.grib2_alternates[1].gdal_unit, "", "percent, the identity's own unit")
        for layer, number, surface, value in (("lcdc", 3, 1, None), ("mcdc", 4, 100, 80000.0), ("hcdc", 5, 100, 45000.0)):
            spec = variable_spec(layer)
            (alternate,) = spec.grib2_alternates
            self.assertEqual((alternate.triple, alternate.level_type, alternate.level_value), ((0, 6, number), surface, value))
            self.assertEqual(spec.grib2_level_type, {"lcdc": 214, "mcdc": 224, "hcdc": 234}[layer], "the identity written")

    def test_the_millimetre_accumulation_is_left_alone(self) -> None:
        for unit in ("kg/(m^2*s)", "[kg/(m^2*s)]", "kg m**-2", "kg/m^2", "mm"):
            self.assertTrue(precipitation_accumulation_is_mm(unit), unit)
            self.assertEqual(accumulation_expression(unit), "A")
        for unit in ("-", "m", ""):
            self.assertFalse(precipitation_accumulation_is_mm(unit), unit)
            self.assertEqual(accumulation_expression(unit), "A")
        with self.assertRaises(ConversionError):
            accumulation_expression("in")
        self.assertEqual(raster_expression("tp", "kg/(m^2*s)"), raster_expression("tp", "-"))


class FetchTests(unittest.TestCase):
    def test_the_model_directory_sits_beside_the_ifs(self) -> None:
        run = GfsRun(datetime(2026, 9, 17, 0, tzinfo=UTC))
        self.assertEqual(ECMWF_OPEN_DATA_MODELS, {"ecmwf": "ifs", "aifs": "aifs-single"})
        self.assertEqual(
            ecmwf_object_url(run, 6, model="aifs"),
            "https://storage.googleapis.com/ecmwf-open-data/20260917/00z/aifs-single/0p25/oper/20260917000000-6h-oper-fc.grib2",
        )
        self.assertEqual(
            ecmwf_companion_object_url(run, 360, "wave", model="aifs"),
            "https://storage.googleapis.com/ecmwf-open-data/20260917/00z/aifs-single/0p25/wave/20260917000000-360h-wave-fc.grib2",
        )
        self.assertEqual(model_object_url(run, 6, "aifs"), ecmwf_object_url(run, 6, model="aifs"))
        self.assertEqual(model_object_url(run, 6, "ecmwf"), ecmwf_object_url(run, 6))
        self.assertTrue(ecmwf_object_url(run, 6, model="aifs", base_url=ECMWF_BASE_URLS[1]).startswith(ECMWF_BASE_URLS[1]))
        with self.assertRaises(DownloadError):
            ecmwf_object_url(run, 6, model="gfs")

    def test_a_run_is_complete_only_with_both_streams_on_one_mirror(self) -> None:
        run = GfsRun(datetime(2026, 9, 17, 0, tzinfo=UTC))
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return "/aifs-single/" in url and ("/wave/" not in url or "-0h-" in url)

        self.assertFalse(_run_is_complete(run, 360, "aifs", exists))
        self.assertTrue(all("/aifs-single/0p25/" in url for url in probed))
        self.assertTrue(any(url.endswith("/wave/20260917000000-360h-wave-fc.grib2") for url in probed))
        self.assertTrue(_run_is_complete(run, 360, "aifs", lambda url: "/aifs-single/" in url))

    def test_every_cycle_reaches_360_hours(self) -> None:
        # The IFS skips its 06/18 cycles past 90 hours; AIFS does not.
        now = datetime(2026, 9, 17, 12, 30, tzinfo=UTC)
        seen: list[str] = []

        def exists(url: str) -> bool:
            seen.append(url)
            return "/06z/" in url

        run = resolve_run("latest", hours=360, now=now, exists=exists, model="aifs")
        self.assertEqual(run.id, "2026091706")
        self.assertTrue(any("/12z/aifs-single/" in url for url in seen), "the newest cycle is probed first")
        seen.clear()
        run = resolve_run("latest", hours=240, now=now, exists=lambda url: "/00z/" in url and "/ifs/" in url, model="ecmwf")
        self.assertEqual(run.id, "2026091700")


class MatcherTests(unittest.TestCase):
    def test_the_header_index_finds_every_input_in_source_order(self) -> None:
        for path, expected in zip(FIXTURE_FRAMES, (31, 30)):
            frames = grib2.inspect_grib_fast(path, AIFS.input_variable_ids, optional_ids=("orog",))
            self.assertEqual(len(frames), expected, path.name)
            present = [variable_id for variable_id in AIFS.input_variable_ids if variable_id in frames]
            self.assertEqual([frames[variable_id].band for variable_id in present], list(range(1, expected + 1)))
        step = grib2.inspect_grib_fast(FIXTURE_FRAMES[1], AIFS.input_variable_ids, optional_ids=("orog",))
        self.assertNotIn("orog", step)
        self.assertEqual(step["tp"].lead_seconds, 6 * 3600)
        # The alternates' own units where they have one, the identity's
        # otherwise.
        self.assertEqual(step["tp"].unit, "kg/(m^2*s)")
        self.assertEqual(step["tcdc"].unit, "%")
        for layer in ADDED:
            self.assertEqual(step[layer].unit, "%")
        self.assertEqual(step["tmpsfc"].unit, "C")
        self.assertEqual(step["dirpw"].unit, "Degree true")

    def test_the_records_are_what_the_alternates_say(self) -> None:
        by_band = {message.band: message for message in grib2.index_messages(FIXTURE_FRAMES[1])}
        frames = grib2.inspect_grib_fast(FIXTURE_FRAMES[1], AIFS.input_variable_ids, optional_ids=("orog",))
        tp = by_band[frames["tp"].band]
        self.assertEqual((tp.discipline, tp.parameter_category, tp.parameter_number), (0, 1, 52))
        self.assertEqual((tp.level_type, tp.statistical_process), (1, 1))
        tcc = by_band[frames["tcdc"].band]
        self.assertEqual((tcc.parameter_category, tcc.parameter_number, tcc.level_type, tcc.level_value), (6, 1, 1, None))
        lcc = by_band[frames["lcdc"].band]
        self.assertEqual((lcc.parameter_number, lcc.level_type, lcc.level_value), (3, 1, None))
        mcc = by_band[frames["mcdc"].band]
        self.assertEqual((mcc.parameter_number, mcc.level_type, mcc.level_value), (4, 100, 80000.0))
        hcc = by_band[frames["hcdc"].band]
        self.assertEqual((hcc.parameter_number, hcc.level_type, hcc.level_value), (5, 100, 45000.0))
        skt = by_band[frames["tmpsfc"].band]
        self.assertEqual((skt.parameter_number, skt.level_type), (17, 1))
        self.assertEqual(by_band[frames["htsgw"].band].parameter_number, 3)
        self.assertEqual(by_band[frames["dirpw"].band].parameter_number, 14)

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            for path in FIXTURE_FRAMES:
                fast = grib2.inspect_grib_fast(path, AIFS.input_variable_ids, optional_ids=("orog",))
                slow = inspect_grib_multi(path, AIFS.input_variable_ids, optional_ids=("orog",))
                self.assertEqual(set(fast), set(slow), path.name)
                for variable_id, frame in fast.items():
                    self.assertEqual(frame, slow[variable_id], f"{path.name} {variable_id}")


class ConversionTests(unittest.TestCase):
    """The two-frame fixture through the reference pipeline."""

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-aifs-"))
        cls.output = cls.root / "aifs.2026091700"
        cls.report = binconvert.convert_bin(
            FIXTURE_FRAMES,
            cls.output,
            work_root=cls.root / "work",
            manifest_path=cls.output / "manifest.json",
            model="aifs",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def _plane(self, variable_id: str, offset: int, member: int = 1) -> tuple[np.ndarray, dict]:
        bundle = read_bundle(self.output / f"{variable_id}.xue")
        return bundle.decode_plane(member, offset), bundle.metadata

    def test_every_published_bundle_is_written_and_no_video(self) -> None:
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual(manifest["model"], "AIFS")
        self.assertEqual(manifest["product"], "aifs-single-0p25")
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], list(published_bundle_ids(AIFS)))
        self.assertFalse(any("video" in bundle for bundle in manifest["bundles"]))
        self.assertEqual(self.report["videos"], [])

    def test_the_rate_is_the_millimetre_total_over_the_step(self) -> None:
        # The fixture's step carries up to 3.4 mm over six hours; a plane
        # scaled a thousandfold would be all overflow.
        codes, metadata = self._plane("prate", 6)
        self.assertEqual(metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 6, "frameCount": 1, "frameStep": 1})
        quantization = metadata["variables"][0]["quantization"]
        self.assertNotIn(quantization["overflowCode"], codes)
        codebook = quantize.PROFILES["quality"]["prate"]
        rates = codebook.decode(codes)
        self.assertGreater(float(rates.max()), 0.4)
        self.assertLess(float(rates.max()), 0.7, "3.4 mm over six hours is under 0.6 mm/h")
        self.assertEqual(metadata["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 0)
        _, tmp2m = self._plane("tmp2m", 0)
        self.assertEqual(tmp2m["time"], {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 6})

    def test_the_cloud_covers_reach_the_codebook_in_percent(self) -> None:
        for variable_id, number, surface in (("tcdc", 1, 10), ("lcdc", 3, 214), ("mcdc", 4, 224), ("hcdc", 5, 234)):
            codes, metadata = self._plane(variable_id, 6)
            quantization = metadata["variables"][0]["quantization"]
            self.assertEqual(quantization["scale"], 0.5, variable_id)
            self.assertEqual(int(codes.max()), 200, f"an overcast {variable_id} cell is 100 %")
            self.assertEqual(int(codes.min()), 0, variable_id)
            parameter = metadata["variables"][0]["parameter"]
            self.assertEqual((parameter["parameterNumber"], parameter["typeOfFirstFixedSurface"]), (number, surface), "the GFS identity is written")
            self.assertIsNone(parameter["scaledValueOfFirstFixedSurface"])

    def test_land_is_the_bottom_of_the_wave_codebook(self) -> None:
        codes, _ = self._plane("htsgw", 0)
        self.assertNotIn(255, codes)
        self.assertGreater(float(np.mean(codes == 0)), 0.25, "a third of the window is land")
        u, _ = self._plane("wave", 0, 1)
        v, _ = self._plane("wave", 0, 2)
        self.assertGreater(float(np.mean((u == 127) & (v == 127))), 0.25, "land is (0, 0) in the wave vector")
        self.assertGreater(int(u.max()), 127)
        skin, _ = self._plane("tmpsfc", 0)
        self.assertLess(float(np.mean(skin == 0)), 0.01)


@unittest.skipUnless(native.available(), f"{native.DISTRIBUTION} is not installed")
class NativeParityTests(unittest.TestCase):
    """The same two frames through both encoders, byte for byte — the five
    AIFS encodings under their alternates, the millimetre accumulation, the
    bitmap fill and the wave vector, on real AIFS records."""

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        if not native.knows_source("aifs"):
            raise unittest.SkipTest(f"the installed {native.DISTRIBUTION} wheel predates the AIFS source")
        cls.root = Path(tempfile.mkdtemp(prefix="xue-aifs-parity-"))
        cls.reference, cls.reference_report = cls._build(binconvert, "reference")
        try:
            cls.subject, cls.subject_report = cls._build(native, "subject")
        except ConversionError as exc:
            if "out of step" in str(exc):
                shutil.rmtree(cls.root, ignore_errors=True)
                raise unittest.SkipTest(f"the installed {native.DISTRIBUTION} wheel predates the AIFS source: {exc}")
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _build(cls, implementation, name: str) -> tuple[Path, dict]:
        run_directory = cls.root / name / "aifs.2026091700"
        report = implementation.convert_bin(
            FIXTURE_FRAMES,
            run_directory,
            work_root=cls.root / f"{name}-work",
            manifest_path=run_directory / "manifest.json",
            latest_path=cls.root / name / "latest-aifs.json",
            run_id="2026091700",
            model="aifs",
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
        self.assertTrue(any(name.endswith("lcdc.xue") for name in reference))
        self.assertTrue(any(name.endswith("wave.xue") for name in reference))
        for relative in reference:
            path = self.reference / relative
            if path.is_dir():
                continue
            with self.subTest(artifact=relative):
                self.assertTrue(filecmp.cmp(path, self.subject / relative, shallow=False), f"{relative} differs")


if __name__ == "__main__":
    unittest.main()

"""The ECMWF source aligned with GFS: the surface diagnostics, the vertical
velocity, the equivalent potential temperature and the ocean fields the open
data carries, each published under its GFS identity.

What is new with it. A record can arrive under a *whole* other identity at
another centre, not just another parameter number on the same surface
(``VariableSpec.grib2_alternates``): ECMWF's gust is an interval maximum on
the 10 m surface, its total cloud cover a local parameter at the ground in a
0–1 fraction, its CAPE the most-unstable parcel's, its skin temperature a
parameter of its own with no surface value. An ECMWF ``.index`` spells one
field differently along the axis (``10fg`` / ``10fg3``,
``VariableSpec.ecmwf_alternate_params``). A second file family of the ECMWF
cycle, the ``wave`` stream, read through the same ``CompanionFile``
mechanism as GFS-Wave. A scalar whose record is empty at the analysis
(``optional_at_analysis`` on a source that is not sflux) and whose series
therefore starts at the first step, the way a de-accumulated rate does. And
a source that has switched the H.264 companion off (``SourceSpec.video``).

``tests/fixtures/ecmwf.2026091212.f000.crop.grib2`` and ``f003`` are an 80 x
80 cell window of the 2026-09-12 12Z cycle over the Kara Sea (60–80E,
65–85N: sea ice, open water and land, so every bitmap and fill rule meets
real masked points), every record the source fetches in source order —
thirty-three at the analysis, which has no gust, thirty-four at the first
step — from the ``oper`` and ``wave`` streams as the fetcher assembles them.
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

from xuebuild import binconvert, grib2, native, zstdcli
from xuebuild.binconvert import analysis_optional_ids, published_bundle_ids, video_variable_ids
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    ECMWF_BASE_URLS,
    _run_is_complete,
    ecmwf_companion_object_url,
    ecmwf_object_url,
)
from xuebuild.gdal import inspect_grib_multi
from xuebuild.idx import ecmwf_field_byte_range
from xuebuild.model import GfsRun
from xuebuild.sources import source_spec
from xuebuild.variables import variable_spec

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_FRAMES = [FIXTURES / "ecmwf.2026091212.f000.crop.grib2", FIXTURES / "ecmwf.2026091212.f003.crop.grib2"]
ECMWF = source_spec("ecmwf")
GFS = source_spec("gfs")
# The bundles this widening added to the ECMWF set, in publication order.
ADDED = ("gust", "tcdc", "cape", "dpt2m", "vvel850", "vvel700", "vvel500", "thetae850", "tmpsfc", "icetk", "htsgw", "perpw", "wave")

requires_gdalinfo = unittest.skipUnless(shutil.which("gdalinfo") is not None, "gdalinfo is not on PATH")


class SourceRegistryTests(unittest.TestCase):
    def test_ecmwf_publishes_what_the_open_data_carries_in_gfs_order(self) -> None:
        gfs = published_bundle_ids(GFS)
        ecmwf = published_bundle_ids(ECMWF)
        self.assertEqual([bundle_id for bundle_id in gfs if bundle_id in ecmwf], list(ecmwf))
        for bundle_id in ADDED:
            self.assertIn(bundle_id, ecmwf)
        # Not in the open data: the layer cloud covers, the visibility, the
        # sea ice cover; and no apparent temperature record.
        for bundle_id in ("lcdc", "mcdc", "hcdc", "vis", "icec", "aptmp2m"):
            self.assertIn(bundle_id, gfs)
            self.assertNotIn(bundle_id, ecmwf)
        self.assertEqual(len(ecmwf), 31)

    def test_every_added_input_names_its_ecmwf_record(self) -> None:
        for variable_id in ECMWF.input_variable_ids:
            self.assertNotEqual(variable_spec(variable_id).ecmwf_param, "", variable_id)
        self.assertEqual(
            {variable_id: variable_spec(variable_id).ecmwf_param for variable_id in ("gust", "tcdc", "cape", "dpt2m", "tmpsfc", "icetk", "htsgw", "perpw", "dirpw")},
            {"gust": "10fg", "tcdc": "tcc", "cape": "mucape", "dpt2m": "2d", "tmpsfc": "skt", "icetk": "sithick", "htsgw": "swh", "perpw": "pp1d", "dirpw": "mwd"},
        )
        for level in (850, 700, 500):
            self.assertEqual(variable_spec(f"vvel{level}").ecmwf_param, "w")

    def test_the_gust_is_optional_at_the_analysis_and_starts_at_the_first_step(self) -> None:
        self.assertEqual(ECMWF.optional_at_analysis, ("gust",))
        scalars = published_bundle_ids(ECMWF)
        self.assertEqual(analysis_optional_ids(ECMWF, scalars), ("prate", "gust"))
        self.assertEqual(analysis_optional_ids(GFS, published_bundle_ids(GFS)), ())
        sflux = source_spec("sflux")
        self.assertEqual(analysis_optional_ids(sflux, published_bundle_ids(sflux)), ("prate",))

    def test_ecmwf_and_sflux_ship_no_video(self) -> None:
        for model in ("ecmwf", "sflux"):
            self.assertFalse(source_spec(model).video, model)
            self.assertEqual(video_variable_ids(source_spec(model)), frozenset(), model)
        for model in ("gfs", "hrrr", "radar"):
            self.assertTrue(source_spec(model).video, model)
            self.assertEqual(video_variable_ids(source_spec(model)), binconvert.VIDEO_VARIABLE_IDS, model)


class FetchTests(unittest.TestCase):
    def test_the_wave_stream_sits_beside_oper(self) -> None:
        run = GfsRun(datetime(2026, 9, 12, 12, tzinfo=UTC))
        self.assertEqual(
            ecmwf_object_url(run, 3),
            "https://storage.googleapis.com/ecmwf-open-data/20260912/12z/ifs/0p25/oper/20260912120000-3h-oper-fc.grib2",
        )
        self.assertEqual(
            ecmwf_companion_object_url(run, 3, "wave"),
            "https://storage.googleapis.com/ecmwf-open-data/20260912/12z/ifs/0p25/wave/20260912120000-3h-wave-fc.grib2",
        )
        self.assertTrue(
            ecmwf_companion_object_url(run, 240, "wave", base_url=ECMWF_BASE_URLS[1]).startswith(ECMWF_BASE_URLS[1])
        )
        with self.assertRaises(DownloadError):
            ecmwf_companion_object_url(run, 3, "ice")

    def test_a_run_is_complete_only_with_its_wave_frames(self) -> None:
        run = GfsRun(datetime(2026, 9, 12, 12, tzinfo=UTC))
        probed: list[str] = []

        def exists(url: str) -> bool:
            probed.append(url)
            return "/wave/" not in url or "-0h-" in url

        self.assertFalse(_run_is_complete(run, 240, "ecmwf", exists))
        self.assertTrue(any("/wave/20260912120000-240h-wave-fc.grib2" in url for url in probed))
        self.assertTrue(_run_is_complete(run, 240, "ecmwf", lambda url: True))

    def test_the_gust_is_found_under_either_spelling(self) -> None:
        lines = [
            {"param": "2t", "levtype": "sfc", "_offset": 0, "_length": 10},
            {"param": "10fg3", "levtype": "sfc", "_offset": 10, "_length": 20},
            {"param": "t", "levtype": "pl", "levelist": "850", "_offset": 30, "_length": 5},
        ]
        text = "\n".join(json.dumps(line) for line in lines)
        gust = variable_spec("gust")
        found = ecmwf_field_byte_range(text, gust.ecmwf_param, alternate_params=gust.ecmwf_alternate_params)
        self.assertEqual((found.start, found.end), (10, 29))
        with self.assertRaisesRegex(DownloadError, "10fg"):
            ecmwf_field_byte_range(text, gust.ecmwf_param)
        # The primary spelling wins when present, and an ambiguous alternate
        # is still an error.
        both = text + "\n" + json.dumps({"param": "10fg", "levtype": "sfc", "_offset": 40, "_length": 1})
        self.assertEqual(ecmwf_field_byte_range(both, "10fg", alternate_params=("10fg3",)).start, 40)
        twice = text + "\n" + json.dumps({"param": "10fg3", "levtype": "sfc", "_offset": 50, "_length": 1})
        with self.assertRaises(DownloadError):
            ecmwf_field_byte_range(twice, "10fg", alternate_params=("10fg3",))


class MatcherTests(unittest.TestCase):
    def test_the_header_index_finds_every_input_in_source_order(self) -> None:
        for path, expected in zip(FIXTURE_FRAMES, (33, 34)):
            frames = grib2.inspect_grib_fast(path, ECMWF.input_variable_ids, optional_ids=ECMWF.optional_at_analysis)
            self.assertEqual(len(frames), expected, path.name)
            present = [variable_id for variable_id in ECMWF.input_variable_ids if variable_id in frames]
            self.assertEqual([frames[variable_id].band for variable_id in present], list(range(1, expected + 1)))
        analysis, step = (grib2.inspect_grib_fast(path, ECMWF.input_variable_ids, optional_ids=("gust",)) for path in FIXTURE_FRAMES)
        self.assertNotIn("gust", analysis)
        self.assertEqual(step["gust"].lead_seconds, 3 * 3600)
        # The alternates' own units, the aliases' the registry's.
        self.assertEqual(step["tcdc"].unit, "-")
        self.assertEqual(step["gust"].unit, "m/s")
        self.assertEqual(step["cape"].unit, "J/kg")
        self.assertEqual(step["tmpsfc"].unit, "C")
        self.assertEqual(step["perpw"].unit, "s")
        self.assertEqual(step["dirpw"].unit, "Degree true")
        with self.assertRaisesRegex(ConversionError, "gust"):
            grib2.inspect_grib_fast(FIXTURE_FRAMES[0], ECMWF.input_variable_ids)

    def test_the_records_are_what_the_alternates_say(self) -> None:
        by_band = {message.band: message for message in grib2.index_messages(FIXTURE_FRAMES[1])}
        frames = grib2.inspect_grib_fast(FIXTURE_FRAMES[1], ECMWF.input_variable_ids)
        gust = by_band[frames["gust"].band]
        self.assertEqual((gust.discipline, gust.parameter_category, gust.parameter_number), (0, 2, 22))
        self.assertEqual((gust.level_type, gust.level_value, gust.statistical_process), (103, 10.0, 2))
        tcc = by_band[frames["tcdc"].band]
        self.assertEqual((tcc.parameter_category, tcc.parameter_number, tcc.level_type, tcc.level_value), (6, 192, 1, None))
        cape = by_band[frames["cape"].band]
        self.assertEqual((cape.parameter_category, cape.parameter_number, cape.level_type), (7, 6, 17))
        skt = by_band[frames["tmpsfc"].band]
        self.assertEqual((skt.parameter_category, skt.parameter_number, skt.level_type, skt.level_value), (0, 17, 1, None))
        self.assertEqual(by_band[frames["perpw"].band].parameter_number, 34)
        self.assertEqual(by_band[frames["dirpw"].band].parameter_number, 14)
        self.assertEqual(by_band[frames["htsgw"].band].parameter_number, 3)

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            for path in FIXTURE_FRAMES:
                fast = grib2.inspect_grib_fast(path, ECMWF.input_variable_ids, optional_ids=ECMWF.optional_at_analysis)
                slow = inspect_grib_multi(path, ECMWF.input_variable_ids, optional_ids=ECMWF.optional_at_analysis)
                self.assertEqual(set(fast), set(slow), path.name)
                for variable_id, frame in fast.items():
                    self.assertEqual(frame, slow[variable_id], f"{path.name} {variable_id}")


class ConversionTests(unittest.TestCase):
    """The two-frame fixture through the reference pipeline."""

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-ecmwf-"))
        cls.output = cls.root / "ecmwf.2026091212"
        cls.report = binconvert.convert_bin(
            FIXTURE_FRAMES,
            cls.output,
            work_root=cls.root / "work",
            manifest_path=cls.output / "manifest.json",
            model="ecmwf",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def _plane(self, variable_id: str, offset: int, member: int = 1) -> tuple[np.ndarray, dict]:
        bundle = read_bundle(self.output / f"{variable_id}.xue")
        return bundle.decode_plane(member, offset), bundle.metadata

    def test_every_published_bundle_is_written_and_no_video(self) -> None:
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], list(published_bundle_ids(ECMWF)))
        self.assertFalse(any("video" in bundle for bundle in manifest["bundles"]))
        self.assertEqual(self.report["videos"], [])
        self.assertEqual(list(self.output.glob("*.h264*")), [])

    def test_the_gust_series_starts_at_the_first_step(self) -> None:
        codes, metadata = self._plane("gust", 3)
        self.assertEqual(metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 3, "frameCount": 1, "frameStep": 1})
        self.assertGreater(int(codes.max()), 0)
        _, prate = self._plane("prate", 3)
        self.assertEqual(prate["time"]["firstFrameOffset"], 3)
        _, tmp2m = self._plane("tmp2m", 0)
        self.assertEqual(tmp2m["time"], {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 3})
        # The identity written is GFS's — the ground surface, not the 10 m
        # one — marked as the maximum over the step it is, the way the
        # de-accumulated rate is marked as a mean; an instantaneous field
        # carries no such key.
        parameter = metadata["variables"][0]["parameter"]
        self.assertEqual((parameter["typeOfFirstFixedSurface"], parameter["scaledValueOfFirstFixedSurface"]), (1, 0))
        self.assertEqual(parameter["typeOfStatisticalProcessing"], 2)
        self.assertEqual(prate["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 0)
        self.assertNotIn("typeOfStatisticalProcessing", tmp2m["variables"][0]["parameter"])
        self.assertEqual(ECMWF.statistical_processes, (("prate", 0), ("gust", 2)))
        self.assertEqual(source_spec("sflux").statistical_processes, (("prate", 0),))
        self.assertEqual(GFS.statistical_processes, ())

    def test_the_cloud_fraction_reaches_the_codebook_in_percent(self) -> None:
        codes, metadata = self._plane("tcdc", 0)
        quantization = metadata["variables"][0]["quantization"]
        self.assertEqual(quantization["scale"], 0.5)
        self.assertEqual(int(codes.max()), 200, "an overcast cell is 100 %")
        self.assertEqual(int(codes.min()), 0)
        self.assertEqual(metadata["variables"][0]["parameter"]["parameterNumber"], 1, "0/6/1, never the local 192")

    def test_land_is_the_bottom_of_the_wave_and_ice_codebooks(self) -> None:
        fills = {}
        for variable_id in ("htsgw", "perpw", "icetk"):
            codes, metadata = self._plane(variable_id, 0)
            self.assertNotIn(255, codes, f"{variable_id} carries no nodata code")
            fills[variable_id] = float(np.mean(codes == 0))
        # A third of the window is land, and the ice edge masks more of the
        # wave fields; the skin temperature covers everything.
        self.assertGreater(fills["htsgw"], 0.25)
        self.assertGreater(fills["perpw"], 0.25)
        self.assertGreater(fills["icetk"], 0.25)
        skin, _ = self._plane("tmpsfc", 0)
        self.assertLess(float(np.mean(skin == 0)), 0.01)
        u, _ = self._plane("wave", 0, 1)
        v, _ = self._plane("wave", 0, 2)
        self.assertGreater(float(np.mean((u == 127) & (v == 127))), 0.25, "land is (0, 0) in the wave vector")
        self.assertGreater(int(u.max()), 127)


@unittest.skipUnless(native.available(), f"{native.DISTRIBUTION} is not installed")
class NativeParityTests(unittest.TestCase):
    """The same two frames through both encoders, byte for byte — every
    alternate identity, the fraction unit, the analysis-less gust axis, the
    bitmap fills and the wave vector, on real ECMWF records."""

    root: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-ecmwf-parity-"))
        cls.reference, cls.reference_report = cls._build(binconvert, "reference")
        try:
            cls.subject, cls.subject_report = cls._build(native, "subject")
        except ConversionError as exc:
            if "out of step" in str(exc):
                shutil.rmtree(cls.root, ignore_errors=True)
                raise unittest.SkipTest(f"the installed {native.DISTRIBUTION} wheel predates the ECMWF set: {exc}")
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def _build(cls, implementation, name: str) -> tuple[Path, dict]:
        run_directory = cls.root / name / "ecmwf.2026091212"
        report = implementation.convert_bin(
            FIXTURE_FRAMES,
            run_directory,
            work_root=cls.root / f"{name}-work",
            manifest_path=run_directory / "manifest.json",
            latest_path=cls.root / name / "latest-ecmwf.json",
            run_id="2026091212",
            model="ecmwf",
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
        self.assertTrue(any(name.endswith("gust.xue") for name in reference))
        self.assertTrue(any(name.endswith("wave.xue") for name in reference))
        for relative in reference:
            path = self.reference / relative
            if path.is_dir():
                continue
            with self.subTest(artifact=relative):
                self.assertTrue(filecmp.cmp(path, self.subject / relative, shallow=False), f"{relative} differs")


if __name__ == "__main__":
    unittest.main()

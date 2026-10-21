"""The ECMWF IFS open data at 0.1°: the records of ``ecmwf`` from the same
cycle at the model's full regular grid, under a source of its own.

What is new with it is the fetch alone. At 0.1° the ``oper`` stream is two
objects, ``-sfc`` (every surface and soil record) and ``-pl`` (the pressure
levels), and the ``wave`` stream one object suffixed ``-sfc``
(:class:`xuebuild.fetch.EcmwfOpenData`): the fetcher picks each record's
object by its level type (``ecmwf_object_part``), loads each ``.index``
once per frame and assembles one GRIB per step in source order, so the
converter sees the same frame as for ``ecmwf``. A run is complete only
when all three objects are at both ends, and the 06/18 cycles, which stop
at 144 h here rather than 90, are skipped only past that. The registry
entry is ``ecmwf``'s at ``ifshres``'s grid, tile and two-rung ladder.

``tests/fixtures/ifs.2026102012.f000.crop.grib2`` and ``f003`` are a 100 x
100 cell window of the 2026-10-20 12Z cycle north of Novaya Zemlya (60–70E,
75–85N: sea ice, open water and land), every record the source fetches in
source order — sixty-six at each frame, as the ecmwf crop — from the three
objects as the fetcher assembles them.
"""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from tests._support import FIXTURES, ParityCase, ReferenceBuild, assert_gdalinfo_agrees, requires_gdalinfo
from xuebuild import grib2
from xuebuild.binconvert import analysis_optional_ids, published_bundle_ids, video_variable_ids
from xuebuild.binformat import read_bundle
from xuebuild.errors import DownloadError
from xuebuild.fetch import (
    ECMWF_BASE_URLS,
    ECMWF_OPEN_DATA_MODELS,
    _run_is_complete,
    ecmwf_companion_object_url,
    ecmwf_object_part,
    ecmwf_object_url,
    resolve_run,
)
from xuebuild.model import GfsRun
from xuebuild.sources import SOURCES, source_spec

FIXTURE_FRAMES = [FIXTURES / "ifs.2026102012.f000.crop.grib2", FIXTURES / "ifs.2026102012.f003.crop.grib2"]
IFS = source_spec("ifs")
ECMWF = source_spec("ecmwf")
IFSHRES = source_spec("ifshres")

#: The reference build every class here reads (tests/_support.py).
REFERENCE = ReferenceBuild("ifs", "2026102012", FIXTURE_FRAMES)


def tearDownModule() -> None:
    REFERENCE.cleanup()


class SourceRegistryTests(unittest.TestCase):
    def test_ifs_is_ecmwf_under_its_own_identity(self) -> None:
        self.assertEqual((IFS.manifest_model, IFS.product, IFS.latest_filename), ("IFS", "ifs-0p1", "latest-ifs.json"))
        self.assertEqual(list(SOURCES).index("ifs"), list(SOURCES).index("ecmwf") + 1)
        for field in (
            "steps",
            "cycle_hours",
            "input_variable_ids",
            "bundle_scalar_ids",
            "bundle_vector_ids",
            "bundle_composite_ids",
            "companion_files",
            "optional_at_analysis",
            "statistical_processes",
            "accumulated_precipitation",
            "series_bundle_ids",
            "video",
            "core_bundle_ids",
        ):
            self.assertEqual(getattr(IFS, field), getattr(ECMWF, field), field)
        self.assertEqual(published_bundle_ids(IFS), published_bundle_ids(ECMWF))
        self.assertEqual(analysis_optional_ids(IFS, published_bundle_ids(IFS)), ("prate", "gust"))
        self.assertEqual(video_variable_ids(IFS), frozenset())
        self.assertEqual(IFS.steps, ((144, 3), (240, 6)))
        self.assertTrue(IFS.fetched and IFS.live and not IFS.series_file and not IFS.observation)
        # The 0.25° fit, pending a refit on 0.1° runs.
        self.assertEqual(IFS.cat_calibration, ECMWF.cat_calibration)

    def test_the_grid_tile_and_ladder_are_the_0p1_ones(self) -> None:
        self.assertEqual(IFS.production_grid, (3600, 1801))
        self.assertEqual(IFS.tile, (90, 95))
        self.assertEqual(IFS.variant_factors, (2, 4))
        for field in ("production_grid", "tile", "variant_factors"):
            self.assertEqual(getattr(IFS, field), getattr(IFSHRES, field), field)
            self.assertNotEqual(getattr(IFS, field), getattr(ECMWF, field), field)
        self.assertIsNone(IFS.open_meteo)


class FetchTests(unittest.TestCase):
    def test_the_objects_are_split_by_level_type(self) -> None:
        run = GfsRun(datetime(2026, 10, 20, 12, tzinfo=UTC))
        service = ECMWF_OPEN_DATA_MODELS["ifs"]
        self.assertEqual((service.directory, service.resolution, service.split, service.short_cycle_hours), ("ifs", "0p1", True, 144))
        prefix = "https://storage.googleapis.com/ecmwf-open-data/20261020/12z/ifs/0p1/"
        self.assertEqual(ecmwf_object_url(run, 0, model="ifs", part="sfc"), prefix + "oper/20261020120000-0h-oper-fc-sfc.grib2")
        self.assertEqual(ecmwf_object_url(run, 240, model="ifs", part="pl"), prefix + "oper/20261020120000-240h-oper-fc-pl.grib2")
        self.assertEqual(ecmwf_companion_object_url(run, 3, "wave", model="ifs"), prefix + "wave/20261020120000-3h-wave-fc-sfc.grib2")
        self.assertTrue(ecmwf_object_url(run, 3, model="ifs", part="pl", base_url=ECMWF_BASE_URLS[2]).startswith(ECMWF_BASE_URLS[2]))
        with self.assertRaises(DownloadError):
            ecmwf_object_url(run, 3, model="ifs")
        with self.assertRaises(DownloadError):
            ecmwf_companion_object_url(run, 3, "ice", model="ifs")

    def test_every_input_has_an_object(self) -> None:
        parts = {
            variable_id: "wave" if IFS.companion_of(variable_id) is not None else ecmwf_object_part(variable_id)
            for variable_id in IFS.input_variable_ids
        }
        self.assertEqual(set(parts.values()), {"sfc", "pl", "wave"})
        self.assertEqual(sum(part == "pl" for part in parts.values()), 44)
        self.assertEqual(sum(part == "wave" for part in parts.values()), 3)
        self.assertEqual(sum(part == "sfc" for part in parts.values()), 20)
        self.assertEqual(len(parts), 67)
        self.assertEqual({parts[f"tsoil{n}"] for n in range(1, 5)} | {parts[f"soilw{n}"] for n in range(1, 5)}, {"sfc"})
        self.assertEqual({parts["prmsl"], parts["orog"], parts["gust"], parts["tmp2m"]}, {"sfc"})
        self.assertEqual({parts[f"hgt{level}"] for level in (1000, 925, 850, 700, 500, 300, 250, 200)}, {"pl"})

    def test_a_run_is_complete_only_with_all_three_objects_on_one_mirror(self) -> None:
        run = GfsRun(datetime(2026, 10, 20, 12, tzinfo=UTC))
        for missing in ("oper-fc-sfc", "oper-fc-pl", "wave-fc-sfc"):
            with self.subTest(missing=missing):
                probed: list[str] = []

                def exists(url: str, missing: str = missing) -> bool:
                    probed.append(url)
                    return not url.endswith(f"-240h-{missing}.grib2")

                self.assertFalse(_run_is_complete(run, 240, "ifs", exists))
                self.assertTrue(all("/ifs/0p1/" in url for url in probed))
                self.assertTrue(any(url.endswith(f"-240h-{missing}.grib2") for url in probed))
        self.assertTrue(_run_is_complete(run, 240, "ifs", lambda url: True))

    def test_the_short_cycles_reach_144_hours(self) -> None:
        now = datetime(2026, 10, 20, 23, 0, tzinfo=UTC)
        self.assertEqual(resolve_run("latest", hours=144, now=now, exists=lambda url: True, model="ifs").id, "2026102018")
        self.assertEqual(resolve_run("latest", hours=240, now=now, exists=lambda url: True, model="ifs").id, "2026102012")
        self.assertEqual(resolve_run("latest", hours=144, now=now, exists=lambda url: True, model="ecmwf").id, "2026102012")


class MatcherTests(unittest.TestCase):
    def test_the_header_index_finds_every_input_in_source_order(self) -> None:
        optional = IFS.optional_at_analysis + ("orog",)
        for path, expected in zip(FIXTURE_FRAMES, (66, 66)):
            frames = grib2.inspect_grib_fast(path, IFS.input_variable_ids, optional_ids=optional)
            self.assertEqual(len(frames), expected, path.name)
            present = [variable_id for variable_id in IFS.input_variable_ids if variable_id in frames]
            self.assertEqual([frames[variable_id].band for variable_id in present], list(range(1, expected + 1)))
        analysis, step = (grib2.inspect_grib_fast(path, IFS.input_variable_ids, optional_ids=optional) for path in FIXTURE_FRAMES)
        self.assertNotIn("gust", analysis)
        self.assertIn("orog", analysis)
        self.assertNotIn("orog", step)
        self.assertEqual(step["gust"].lead_seconds, 3 * 3600)

    @requires_gdalinfo
    def test_gdalinfo_agrees_with_the_header_index(self) -> None:
        optional = IFS.optional_at_analysis + ("orog",)
        assert_gdalinfo_agrees(self, ((path, IFS.input_variable_ids, optional) for path in FIXTURE_FRAMES))


class ConversionTests(unittest.TestCase):
    """The two-frame fixture through the reference pipeline: the module's
    reference build, which the parity case compares the wheel's against."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.output = REFERENCE.run_directory
        _, cls.report = REFERENCE.get()

    def test_every_published_bundle_is_written_with_two_rungs_and_no_video(self) -> None:
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual((manifest["model"], manifest["product"]), ("IFS", "ifs-0p1"))
        self.assertEqual([bundle["variable"] for bundle in manifest["bundles"]], list(published_bundle_ids(IFS)))
        self.assertFalse(any("video" in bundle for bundle in manifest["bundles"]))
        self.assertEqual(self.report["videos"], [])
        self.assertEqual(len(self.report["variants"]), 2 * len(published_bundle_ids(IFS)))
        for name in ("tmp2m.half.xue", "tmp2m.quarter.xue", "wave.half.xue", "wave.quarter.xue"):
            self.assertTrue((self.output / name).is_file(), name)

    def test_the_grid_is_the_fixture_s_window_of_the_0p1_one(self) -> None:
        grid = read_bundle(self.output / "tmp2m.xue").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (100, 100))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.1, -0.1))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (60.0, 85.0))
        self.assertEqual(grid["rowOrder"], "north-to-south")
        self.assertFalse(grid["wrapLongitude"])

    def test_the_gust_series_starts_at_the_first_step(self) -> None:
        gust = read_bundle(self.output / "gust.xue")
        self.assertEqual(gust.metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 3, "frameCount": 1, "frameStep": 1})
        self.assertGreater(int(gust.decode_plane(1, 3).max()), 0)
        self.assertEqual(gust.metadata["variables"][0]["parameter"]["typeOfStatisticalProcessing"], 2)
        prate = read_bundle(self.output / "prate.xue")
        self.assertEqual(prate.metadata["time"]["firstFrameOffset"], 3)
        tmp2m = read_bundle(self.output / "tmp2m.xue")
        self.assertEqual(tmp2m.metadata["time"], {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 2, "frameStep": 3})


class NativeParityTests(ParityCase, unittest.TestCase):
    """The same two frames through both encoders, byte for byte, on the
    0.1° grid with its two rungs."""

    reference = REFERENCE
    expect = ("ifs.2026102012/gust.xue", "ifs.2026102012/wave.quarter.xue")


if __name__ == "__main__":
    unittest.main()

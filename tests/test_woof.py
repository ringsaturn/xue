"""The woof source: a WRF-ARW run on Recast Systems' WOOF service, read on
its 500 m innermost domain over Mount Fuji.

Two things are new with it. It is the first forecast with **no feed**
(``SourceSpec.latest_filename`` None on a source that is not an
observation): a forecast is fetched only through a live pointer, so the
source is neither ``fetched`` nor ``live``, ``fetch`` and ``build-bin`` do
not offer it, a complete run cannot be required of it, and it is reached
only through a showcase case naming the ``dataset`` directory the ``xue
wrf-series`` tool wrote. And its axis starts at **f001**
(``first_hour`` 1): hour 0 of a WOOF run is the driving GFS analysis on
the nest, not a WRF forecast, so the tool drops it and every bundle carries
``firstFrameOffset: 1`` — including ``prate``, since the hour's
precipitation total (``apcp``, ``interval_precipitation``) is on every
frame and nothing is analysis-optional.

Everything else is the ``ifshres`` series-file path: one CF NetCDF per
variable, the cycle as the epoch of the ``time`` coordinate, the regional
grid snapped to the thousandth of a degree, a NaN fill. Neither encoder
learns any WRF arithmetic — destaggering, the wind rotation, the dewpoint,
the layered cloud, the cloud water's interpolation onto the volume's
altitudes and the Lambert → 0.005° regrid are the tool's.

The ``cloud3d`` volume is the second volume bundle after MRMS's ``refl3d``:
24 ``clw<m>`` levels of cloud water (0/1/22 on the altitude surface 102,
g/kg), each its own series. A level under the model terrain is NaN in the
series and decodes as 0 g/kg, the bottom of the linear codebook; no nodata
code is written.

``tests/fixtures/woof.2026101006/`` is a 20 x 16 cell window of the
2026-10-10 06Z run's d04 at hours 1 and 2, one file per variable, written
by the tool with the commands in ``tests/fixtures/README.md``.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np

from tests._support import ClassTempRoot, FIXTURES, assert_native_matches, registry_entry, requires_gdal, requires_native_source
from xuebuild import binconvert, observation
from xuebuild.binconvert import interval_rate, published_bundle_ids
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import resolve_run
from xuebuild.manifest import validate_bin_manifest
from xuebuild.quantize import PROFILES
from xuebuild.showcase import parse_case
from xuebuild.sources import SOURCES, source_spec
from xuebuild.stac import _source_prose
from xuebuild.variables import CLOUD_WATER_LEVELS_M, CLOUD_WATER_VARIABLE_IDS, REFLECTIVITY_VARIABLE_IDS, cloud_water_level, variable_spec

SERIES_DIR = FIXTURES / "woof.2026101006"
VOLUME_REGISTRY = FIXTURES / "volume-registry.json"
SPEC = source_spec("woof")
RUN_TIME = datetime(2026, 10, 10, 6, tzinfo=UTC)
WIDTH, HEIGHT = 20, 16

#: The published bundles, in manifest order: the surface set, the cloud
#: layers, the boundary layer, the radiation, the terrain, the 10 m pair and
#: the cloud water volume.
BUNDLES = (
    "tmp2m",
    "prate",
    "tmpsfc",
    "dpt2m",
    "tcdc",
    "lcdc",
    "mcdc",
    "hcdc",
    "hpbl",
    "dswrf",
    "orog",
    "wind10m",
    "cloud3d",
)
#: The tool's surface series; the cloud water levels follow them.
SURFACE_INPUTS = ("tmp2m", "dpt2m", "tmpsfc", "ugrd10m", "vgrd10m", "apcp", "tcdc", "lcdc", "mcdc", "hcdc", "hpbl", "dswrf", "orog")


def localized(text: str) -> dict[str, str]:
    from xuebuild.showcase import LOCALES

    return {locale: text for locale in LOCALES}


@functools.cache
def source_plane(variable_id: str, band: int) -> np.ndarray:
    """One band of one fixture series, in the file's own units — what the
    converter's own extraction reads, read independently. Cached: callers
    must not write into it."""
    name = observation.series_variable_name(variable_id, SPEC)
    dataset = f'NETCDF:"{SERIES_DIR / f"woof.2026101006.{variable_id}.nc"}":{name}'
    with tempfile.TemporaryDirectory() as work:
        raw = Path(work) / "plane.bin"
        subprocess.run(
            [
                "gdal_translate", "-q", "-b", str(band), "-of", "ENVI", "-ot", "Float64",
                "-co", "INTERLEAVE=BSQ", dataset, str(raw),
            ],
            check=True,
        )
        plane = np.fromfile(raw, dtype="<f8").reshape(HEIGHT, WIDTH)
        plane.flags.writeable = False
        return plane


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_a_series_file_forecast_with_no_feed(self) -> None:
        self.assertEqual((SPEC.manifest_model, SPEC.product), ("WOOF-WRF", "nest"))
        self.assertIsNone(SPEC.latest_filename)
        self.assertTrue(SPEC.series_file)
        self.assertFalse(SPEC.observation)
        self.assertFalse(SPEC.live)
        self.assertFalse(SPEC.fetched, "a forecast is fetched only through a live pointer")
        self.assertIsNone(SPEC.window_hours)
        self.assertIsNone(SPEC.cadence_seconds)
        self.assertIsNone(SPEC.open_meteo)
        self.assertIsNone(SPEC.regrid)
        self.assertFalse(SPEC.video)
        # The one source nothing fetches; every other is fetched and live.
        self.assertEqual([source_id for source_id, spec in SOURCES.items() if not spec.fetched], ["woof"])
        self.assertEqual([source_id for source_id, spec in SOURCES.items() if not spec.live], ["woof"])

    def test_the_axis_is_hourly_from_the_first_hour_to_seventy_two(self) -> None:
        self.assertEqual(SPEC.steps, ((72, 1),))
        self.assertEqual(SPEC.first_hour, 1)
        self.assertEqual(SPEC.horizon_hours, 72)
        self.assertEqual(SPEC.forecast_hours(3), [1, 2, 3])
        self.assertEqual(SPEC.forecast_hours(12), list(range(1, 13)))
        self.assertEqual(len(SPEC.forecast_hours(72)), 72)
        for off_axis in (0, 73):
            with self.subTest(hour=off_axis), self.assertRaisesRegex(DownloadError, "from f001"):
                SPEC.forecast_hours(off_axis)

    def test_it_publishes_thirteen_bundles_and_reads_the_total_for_the_rate(self) -> None:
        self.assertEqual(published_bundle_ids(SPEC), BUNDLES)
        self.assertEqual(SPEC.core_bundle_ids, ("tmp2m",))
        self.assertEqual(SPEC.input_variable_ids, SURFACE_INPUTS + CLOUD_WATER_VARIABLE_IDS)
        for variable_id in SPEC.input_variable_ids:
            variable_spec(variable_id)
        self.assertNotIn("apcp", published_bundle_ids(SPEC), "the total is an input only")
        self.assertEqual(binconvert.bundle_input_ids(SPEC, "prate"), ("apcp",))
        self.assertTrue(SPEC.interval_precipitation)
        self.assertFalse(SPEC.accumulated_precipitation or SPEC.averaged_precipitation)
        self.assertEqual(SPEC.statistical_processes, (("prate", 0),))
        self.assertEqual(SPEC.optional_at_analysis, (), "the total is on every frame; there is no analysis frame")

    def test_the_grid_is_the_nest_inscribed_in_the_regular_grid_with_no_ladder(self) -> None:
        self.assertEqual(SPEC.production_grid, (79, 61))
        self.assertEqual(SPEC.tile, (64, 64))
        self.assertEqual(SPEC.variant_factors, ())
        self.assertEqual(SPEC.cycle_hours, 6)
        self.assertEqual(SPEC.companion_files, ())

    def test_nothing_fetches_or_resolves_a_run(self) -> None:
        with self.assertRaisesRegex(DownloadError, "not fetched"):
            resolve_run("latest", hours=12, model="woof")
        with self.assertRaisesRegex(DownloadError, "not fetched"):
            resolve_run("2026101006", hours=12, model="woof")

    def test_the_volume_is_the_cloud_water_on_twenty_four_altitudes(self) -> None:
        self.assertEqual(SPEC.bundle_volume_ids, ("cloud3d",))
        self.assertEqual(binconvert.VOLUME_BUNDLES["cloud3d"], CLOUD_WATER_VARIABLE_IDS)
        self.assertEqual(binconvert.bundle_input_ids(SPEC, "cloud3d"), CLOUD_WATER_VARIABLE_IDS)
        self.assertEqual(
            CLOUD_WATER_LEVELS_M,
            (250, 500, 750, 1000, 1250, 1500, 1750, 2000, 2250, 2500, 2750, 3000,
             3500, 4000, 4500, 5000, 5500, 6000, 7000, 8000, 9000, 10000, 11000, 12000),
        )  # fmt: skip
        self.assertEqual(CLOUD_WATER_VARIABLE_IDS, tuple(f"clw{level}" for level in CLOUD_WATER_LEVELS_M))
        self.assertEqual((cloud_water_level("clw1250"), cloud_water_level("clw1300"), cloud_water_level("cref")), (1250, None, None))
        for variable_id, level in zip(CLOUD_WATER_VARIABLE_IDS, CLOUD_WATER_LEVELS_M):
            spec = variable_spec(variable_id)
            self.assertEqual(spec.label, f"Cloud water at {level / 1000:g} km MSL")
            self.assertEqual((spec.output_unit, spec.gdal_unit, spec.value_range, spec.fill_values), ("g/kg", "g/kg", (0, 3), ()))
            self.assertEqual(
                spec.parameter_metadata(),
                {
                    "discipline": 0,
                    "parameterCategory": 1,
                    "parameterNumber": 22,
                    "typeOfFirstFixedSurface": 102,
                    "scaleFactorOfFirstFixedSurface": 0,
                    "scaledValueOfFirstFixedSurface": level,
                },
            )
            self.assertEqual(observation.accepted_series_units(spec), ("g/kg",), "the tool writes g/kg; there is no unit rule")
            # Linear in every profile, so the volume renderer can
            # interpolate codes; balanced keeps the quality step.
            self.assertEqual(PROFILES["quality"][variable_id].metadata()["maximumCode"], 253)
            self.assertEqual(PROFILES["balanced"][variable_id], PROFILES["quality"][variable_id])
            self.assertEqual(PROFILES["compact"][variable_id].step, 0.02)
            self.assertNotIn(variable_id, binconvert.RAW_VARIABLE_IDS)

    def test_the_committed_volume_registry_still_describes_this_encoder(self) -> None:
        pinned = json.loads(VOLUME_REGISTRY.read_text(encoding="utf-8"))
        actual = {variable_id: registry_entry(variable_id) for variable_id in REFLECTIVITY_VARIABLE_IDS + CLOUD_WATER_VARIABLE_IDS}
        self.assertEqual(
            pinned,
            actual,
            "the volume registry moved; the Rust encoder and the frontend read the same "
            "fixture, so regenerate tests/fixtures/volume-registry.json deliberately "
            "(tests/fixtures/README.md) and change every implementation together",
        )
        self.assertEqual(list(pinned), list(binconvert.VOLUME_BUNDLES["refl3d"] + binconvert.VOLUME_BUNDLES["cloud3d"]))

    def test_the_catalog_prose_names_recast_the_gfs_and_the_dem(self) -> None:
        prose = _source_prose(SPEC)
        self.assertEqual(prose["title"], "Recast WOOF (WRF-ARW) nested run")
        self.assertIn("Recast Systems' WOOF service", prose["description"])
        self.assertIn("0.005°", prose["description"])
        self.assertIn("showcase cases only", prose["description"])
        self.assertIn("cloud water volume", prose["description"])
        self.assertEqual(prose["license"], "other")
        self.assertEqual([provider["name"] for provider in prose["providers"]], ["NOAA / NCEP", "Copernicus", "Xue"])


class CaseDefinitionTests(unittest.TestCase):
    def payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": "fuji-demo",
            "title": localized("Demo"),
            "summary": localized("Demo summary"),
            "model": "woof",
            "dataset": "woof/fuji-2026-10-10/",
            "hours": 12,
            "bbox": [138.54, 35.22, 138.93, 35.52],
            "variables": ["tmpsfc", "wind10m", "lcdc", "prate"],
            "defaultVariable": "tmpsfc",
        }
        payload.update(overrides)
        return payload

    def test_a_case_names_the_dataset_directory_and_no_run(self) -> None:
        spec = parse_case(self.payload())
        self.assertTrue(spec.from_dataset)
        self.assertEqual(spec.run, "")
        self.assertEqual(spec.variables, ("prate", "tmpsfc", "lcdc", "wind10m"))
        with mock.patch.dict(os.environ, {"XUE_OBSERVATION_ROOT": "/data/observations"}):
            self.assertEqual(spec.dataset_path, Path("/data/observations/woof/fuji-2026-10-10"))
        with self.assertRaises(Exception):
            parse_case(self.payload(run="2026101006"))
        with self.assertRaises(Exception):
            parse_case(self.payload(dataset=""))
        # The hours are a point on the axis from f001.
        with self.assertRaises(Exception):
            parse_case(self.payload(hours=73))
        parse_case(self.payload(hours=72))


@requires_gdal
class SeriesTests(unittest.TestCase):
    def test_the_run_is_the_epoch_of_the_time_axis_and_starts_at_the_first_hour(self) -> None:
        series = observation.inspect_observation(SERIES_DIR, SPEC, ("tmp2m", "apcp"))
        self.assertEqual(series.frames[0]["tmp2m"].run_time, RUN_TIME)
        self.assertEqual(series.lead_seconds, [3600, 7200])
        # The total is on every frame, so both series carry the same axis
        # and the first frame of each is hour 1.
        self.assertEqual(series.frames[0]["apcp"].lead_seconds, 3600)
        self.assertEqual(len(series.frames), 2)

    def test_every_input_is_a_series_of_its_own(self) -> None:
        files = observation.series_files(SERIES_DIR, SPEC.input_variable_ids)
        self.assertEqual(len(files), 37)
        self.assertEqual(sorted(files), sorted(SPEC.input_variable_ids))
        for variable_id, path in files.items():
            self.assertEqual(path.name, f"woof.2026101006.{variable_id}.nc")


@requires_gdal
class ConversionTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-woof-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                SERIES_DIR,
                cls.root / "out",
                model="woof",
                skip_video=True,
                work_root=cls.root / "work",
                run_id="2026101006",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    def manifest(self) -> dict:
        return json.loads((self.root / "out" / "manifest.json").read_text())

    def bundle(self, name: str):
        return read_bundle(self.root / "out" / f"{name}.xue")

    def test_the_run_publishes_every_bundle_the_source_declares(self) -> None:
        self.assertEqual(tuple(bundle["variable"] for bundle in self.report["bundles"]), BUNDLES)
        self.assertEqual(self.report["videos"], [])
        manifest = self.manifest()
        self.assertEqual((manifest["model"], manifest["product"]), ("WOOF-WRF", "nest"))
        self.assertEqual(manifest["runTime"], "2026-10-10T06:00:00Z")
        # The manifest's horizon is the last hour; the axis starts at hour 1.
        self.assertEqual(manifest["forecastHours"], 2)
        validate_bin_manifest(manifest, expected_hours=2, require_core_variables=True)
        self.assertTrue((self.root / "out" / "tmp2m.poster.bin").is_file())
        # No ladder: a plane of the nest is under 5000 cells.
        self.assertFalse((self.root / "out" / "tmp2m.half.xue").exists())

    def test_the_grid_is_the_fixture_s_window_of_the_nest(self) -> None:
        grid = self.bundle("tmp2m").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (WIDTH, HEIGHT))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.005, -0.005))
        self.assertTrue(138.54 <= grid["firstLongitude"] <= 138.93, grid["firstLongitude"])
        self.assertTrue(35.22 <= grid["firstLatitude"] <= 35.52, grid["firstLatitude"])
        self.assertEqual(grid["rowOrder"], "north-to-south")
        self.assertFalse(grid["wrapLongitude"])

    def test_every_bundle_starts_at_the_first_hour(self) -> None:
        # f000 is the GFS analysis on the nest and is never written, so the
        # first frame of every bundle is hour 1 — the rate's included, since
        # the total is on every frame rather than missing at an analysis.
        for name in ("tmp2m", "prate", "wind10m", "lcdc", "cloud3d"):
            with self.subTest(bundle=name):
                self.assertEqual(
                    self.bundle(name).metadata["time"],
                    {"unitSeconds": 3600, "firstFrameOffset": 1, "frameCount": 2, "frameStep": 1},
                )
        # The terrain is static: its one frame is the first of the axis.
        self.assertEqual(
            self.bundle("orog").metadata["time"],
            {"unitSeconds": 3600, "firstFrameOffset": 1, "frameCount": 1, "frameStep": 1},
        )

    def test_the_rate_is_declared_a_mean_and_nothing_else_a_statistic(self) -> None:
        processes = {
            name: self.bundle(name).metadata["variables"][0]["parameter"].get("typeOfStatisticalProcessing")
            for name in ("prate", "tmp2m", "dswrf", "tcdc")
        }
        self.assertEqual(processes, {"prate": 0, "tmp2m": None, "dswrf": None, "tcdc": None})

    def test_the_values_are_the_source_s_in_the_codebook_s_units(self) -> None:
        # Planes are addressed by forecast hour, and the first is hour 1.
        for name in ("tmp2m", "tmpsfc", "dpt2m", "hpbl", "orog"):
            with self.subTest(bundle=name):
                bundle = self.bundle(name)
                codebook = PROFILES["quality"][name]
                decoded = codebook.decode(np.asarray(bundle.decode_plane(1, 1)).reshape(HEIGHT, WIDTH))
                source = np.clip(source_plane(name, 1), codebook.minimum, codebook.maximum)
                self.assertLessEqual(float(np.abs(decoded - source).max()), codebook.metadata()["scale"] / 2 + 1e-9)

    def test_the_rate_is_the_hour_s_total_by_interval_rate(self) -> None:
        # Hour 1's total covers the hour from the run time, hour 2's the hour
        # since hour 1: each is a one-hour interval, so the rate is the total.
        bundle = self.bundle("prate")
        codebook = PROFILES["quality"]["prate"]
        wet_cells = 0
        for hour in (1, 2):
            with self.subTest(hour=hour):
                codes = np.asarray(bundle.decode_plane(1, hour)).reshape(HEIGHT, WIDTH)
                total = np.nan_to_num(source_plane("apcp", hour))
                # Code for code what the codebook makes of the total itself:
                # a drizzle under its 0.01 mm/h trace is the zero code, the
                # rest its log1p step.
                np.testing.assert_array_equal(codes, codebook.quantize(interval_rate(total, 1)))
                wet_cells += int((codes > 0).sum())
        self.assertGreater(wet_cells, 0, "the fixture carries some rain to check")

    def test_the_volume_carries_the_cloud_water_levels_in_order(self) -> None:
        metadata = self.bundle("cloud3d").metadata
        variables = metadata["variables"]
        self.assertEqual([variable["id"] for variable in variables], list(CLOUD_WATER_VARIABLE_IDS))
        self.assertEqual([variable["numericId"] for variable in variables], list(range(1, 25)))
        for variable, level in zip(variables, CLOUD_WATER_LEVELS_M):
            with self.subTest(level=level):
                self.assertEqual(variable["unit"], "g/kg")
                self.assertEqual(variable["label"], f"Cloud water at {level / 1000:g} km MSL")
                self.assertEqual(
                    variable["parameter"],
                    {
                        "discipline": 0,
                        "parameterCategory": 1,
                        "parameterNumber": 22,
                        "typeOfFirstFixedSurface": 102,
                        "scaleFactorOfFirstFixedSurface": 0,
                        "scaledValueOfFirstFixedSurface": level,
                    },
                )
                self.assertEqual(
                    variable["quantization"],
                    {"type": "linear", "offset": 0.0, "scale": 0.01, "minimumCode": 0, "maximumCode": 253, "nodataCode": 255},
                )
        self.assertEqual(metadata["grid"], self.bundle("tmp2m").metadata["grid"])

    def test_the_volume_decodes_to_the_series_and_the_ground_to_no_cloud(self) -> None:
        bundle = self.bundle("cloud3d")
        codebook = PROFILES["quality"]["clw250"]
        under_ground = wet = 0
        for numeric_id, variable_id in enumerate(CLOUD_WATER_VARIABLE_IDS, start=1):
            for hour in (1, 2):
                codes = np.asarray(bundle.decode_plane(numeric_id, hour)).reshape(HEIGHT, WIDTH)
                source = source_plane(variable_id, hour)
                below = np.isnan(source)
                # No nodata code is ever written: under the terrain is 0 g/kg.
                self.assertTrue((codes <= codebook.maximum_code).all(), variable_id)
                np.testing.assert_array_equal(codes[below], 0, err_msg=variable_id)
                np.testing.assert_array_equal(codes, codebook.quantize(np.nan_to_num(source, nan=0.0)), err_msg=variable_id)
                under_ground += int(below.sum())
                wet += int((codes > 0).sum())
        self.assertGreater(under_ground, 0, "the fixture carries terrain above some levels")
        self.assertGreater(wet, 0, "the fixture carries some cloud")
        # The lowest five levels are all under this window's ground.
        for numeric_id in range(1, 6):
            self.assertFalse(np.asarray(bundle.decode_plane(numeric_id, 1)).any())
        # Hour 2's cloud sits on the flanks at 2 km, short of a gram per kg.
        decoded = codebook.decode(np.asarray(bundle.decode_plane(CLOUD_WATER_LEVELS_M.index(2000) + 1, 2)))
        self.assertAlmostEqual(float(decoded.max()), 0.86, delta=0.005)

    def test_the_summit_column_is_ground_then_clear_air(self) -> None:
        terrain = source_plane("orog", 1)
        row, column = np.unravel_index(int(terrain.argmax()), terrain.shape)
        self.assertGreater(float(terrain[row, column]), 3600.0)
        bundle = self.bundle("cloud3d")
        for hour in (1, 2):
            for numeric_id, level in enumerate(CLOUD_WATER_LEVELS_M, start=1):
                source = float(source_plane(f"clw{level}", hour)[row, column])
                code = int(np.asarray(bundle.decode_plane(numeric_id, hour)).reshape(HEIGHT, WIDTH)[row, column])
                with self.subTest(hour=hour, level=level):
                    # Under the 3633 m summit the series is NaN; above it the
                    # air is dry on both hours. Either way the code is 0.
                    self.assertEqual(np.isnan(source), level < 4000)
                    self.assertEqual(code, 0)

    def test_a_complete_run_cannot_be_required_of_a_source_with_no_feed(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "no complete run to require"):
                binconvert.convert_bin(
                    SERIES_DIR,
                    self.root / "complete",
                    model="woof",
                    skip_video=True,
                    require_complete=True,
                    expected_hours=2,
                )

    @requires_native_source("woof")
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        assert_native_matches(self, SERIES_DIR, self.root / "out", self.report, self.root / "native", model="woof")


if __name__ == "__main__":
    unittest.main()

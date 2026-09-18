"""The ifshres source: ECMWF's IFS HRES on its native 9 km grid, as
Open-Meteo redistributes it.

Three things are new with it. It is the first **forecast** whose frames
arrive as NetCDF series rather than GRIB records (``SourceSpec.series_file``
on a source that is not an observation): the run's cycle is the epoch of the
``time`` coordinate, the lead times are the cycle's, and the axis is still
validated against the source's published steps. Its fetch is an external
tool under another licence (``xuebuild/om2nccli.py``): ``om2nc fetch`` reads
the byte ranges of one variable out of the ``.om`` files, resamples the
reduced Gaussian grid onto 0.1° by nearest neighbour and writes one CF
NetCDF per variable — the repository imports, links and vendors nothing of
it. And precipitation arrives a third way (``interval_precipitation``): not
a run total to difference (ECMWF ``tp``) nor a window average to de-average
(sflux ``prate_ave``) but the total over the interval since the model's
previous native output time, which becomes a rate by one division.

``tests/fixtures/ifshres.2026091800/`` is the first four steps of the
2026-09-18 00Z cycle over the East China Sea and Kyushu (130–138°E,
30–38°N; 81 x 81 cells of the published 0.1° grid), one file per variable,
cut with the commands in ``tests/fixtures/README.md``. The three
interval variables start at step 1, as they do in the run itself, and
``sea_ice_thickness`` is NaN over all the land in the box — which is what
the NaN fill rule is here for.
"""

from __future__ import annotations

import dataclasses
import filecmp
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import numpy as np

from xuebuild import binconvert, fetch, native, observation, om2nccli, zstdcli
from xuebuild.binconvert import interval_rate, published_bundle_ids
from xuebuild.binformat import read_bundle
from xuebuild.errors import ConversionError, DownloadError
from xuebuild.fetch import (
    OPEN_METEO_BASE_URL,
    _fetch_open_meteo_run,
    _open_meteo_run_is_complete,
    open_meteo_resolution,
    open_meteo_run_url,
    open_meteo_series_name,
    parse_run,
    resolve_run,
)
from xuebuild.manifest import MODEL_CORE_BUNDLES, validate_bin_manifest
from xuebuild.model import GfsRun
from xuebuild.observation import accepted_series_units
from xuebuild.quantize import PROFILES
from xuebuild.sources import source_spec
from xuebuild.stac import _source_prose
from xuebuild.variables import variable_spec

FIXTURES = Path(__file__).parent / "fixtures"
SERIES_DIR = FIXTURES / "ifshres.2026091800"
SPEC = source_spec("ifshres")
RUN = GfsRun(datetime(2026, 9, 18, tzinfo=UTC))

requires_gdal = unittest.skipUnless(
    shutil.which("gdalinfo") is not None and shutil.which("gdal_translate") is not None, "GDAL is not on PATH"
)

#: The published bundles, in manifest order: the GFS surface set Open-Meteo
#: carries, the sflux radiation, and the 10 m wind pair.
BUNDLES = (
    "tmp2m",
    "prate",
    "dswrf",
    "prmsl",
    "gust",
    "tcdc",
    "lcdc",
    "mcdc",
    "hcdc",
    "cape",
    "vis",
    "dpt2m",
    "tmpsfc",
    "icetk",
    "wind10m",
)


def source_plane(variable_id: str, band: int) -> np.ndarray:
    """One band of one fixture series, in the file's own units — what the
    converter's own extraction reads, read independently."""
    name = observation.series_variable_name(variable_id)
    dataset = f'NETCDF:"{SERIES_DIR / f"ifshres.2026091800.{variable_id}.nc"}":{name}'
    with tempfile.TemporaryDirectory() as work:
        raw = Path(work) / "plane.bin"
        subprocess.run(
            [
                "gdal_translate", "-q", "-b", str(band), "-of", "ENVI", "-ot", "Float64",
                "-co", "INTERLEAVE=BSQ", dataset, str(raw),
            ],
            check=True,
        )
        return np.fromfile(raw, dtype="<f8").reshape(81, 81)


class SourceRegistryTests(unittest.TestCase):
    def test_the_source_is_a_fetched_series_file_forecast(self) -> None:
        self.assertTrue(SPEC.fetched and SPEC.live and SPEC.series_file)
        self.assertFalse(SPEC.observation, "a forecast, the first series-file source that is one")
        self.assertEqual(SPEC.open_meteo, "ecmwf_ifs")
        self.assertEqual(
            (SPEC.manifest_model, SPEC.product, SPEC.latest_filename),
            ("ECMWF-HRES", "ifs-hres-0p1", "latest-ifshres.json"),
        )
        self.assertEqual((SPEC.cycle_hours, SPEC.horizon_hours, SPEC.video), (12, 360, False))
        self.assertIsNone(SPEC.window_hours)
        self.assertIsNone(SPEC.cadence_seconds)

    def test_the_axis_is_the_run_s_own_native_cadence(self) -> None:
        self.assertEqual(SPEC.steps, ((90, 1), (144, 3), (360, 6)))
        hours = SPEC.forecast_hours(360)
        self.assertEqual(len(hours), 145)
        self.assertEqual(hours[:3], [0, 1, 2])
        self.assertEqual(hours[89:93], [89, 90, 93, 96])
        self.assertEqual(hours[-3:], [348, 354, 360])
        # A cap between steps has no frame to fetch.
        with self.assertRaises(DownloadError):
            SPEC.forecast_hours(91)

    def test_the_cycles_are_00z_and_12z(self) -> None:
        self.assertEqual(parse_run("2026091812", "ifshres").id, "2026091812")
        with self.assertRaisesRegex(DownloadError, "cycles start at 00, 12"):
            parse_run("2026091806", "ifshres")

    def test_it_publishes_fifteen_bundles_in_the_gfs_order(self) -> None:
        self.assertEqual(published_bundle_ids(SPEC), BUNDLES)
        self.assertEqual(len(BUNDLES), 15)
        self.assertEqual(SPEC.core_bundle_ids, ("tmp2m", "prate"))
        self.assertEqual(MODEL_CORE_BUNDLES["ECMWF-HRES"], ("tmp2m", "prate"))
        # Every published bundle's inputs are fetched, and apcp is fetched
        # without ever being published: it is the rate's input.
        self.assertIn("apcp", SPEC.input_variable_ids)
        self.assertNotIn("apcp", published_bundle_ids(SPEC))
        self.assertEqual(binconvert.bundle_input_ids(SPEC, "prate"), ("apcp",))

    def test_the_interval_quantities_are_declared_as_statistics_and_missing_at_the_analysis(self) -> None:
        self.assertTrue(SPEC.interval_precipitation)
        self.assertFalse(SPEC.accumulated_precipitation or SPEC.averaged_precipitation)
        self.assertEqual(SPEC.optional_at_analysis, ("apcp", "dswrf", "gust"))
        self.assertEqual(SPEC.statistical_processes, (("prate", 0), ("dswrf", 0), ("gust", 2)))
        self.assertEqual(
            binconvert.analysis_optional_ids(SPEC, tuple(b for b in BUNDLES if b != "wind10m")),
            ("prate", "dswrf", "gust"),
        )

    def test_the_grid_is_the_global_tenth_degree_one_the_fetch_asks_for(self) -> None:
        self.assertEqual(SPEC.production_grid, (3600, 1801))
        self.assertEqual(SPEC.tile, (90, 95))
        self.assertEqual(SPEC.variant_factors, (2, 4))
        # The step the tool is told to resample onto is read off that grid,
        # so the fetch and a complete build's grid check cannot disagree.
        self.assertEqual(open_meteo_resolution(SPEC), 0.1)
        # 40 x 19 tiles, the last row 91 cells of the 95 (1801 rows), which
        # is why the height is not one of the tidy divisors of 1800: those
        # leave a one-row tile at the south pole.
        self.assertEqual((3600 // 90, -(-1801 // 95)), (40, 19))
        self.assertEqual(1801 - 18 * 95, 91)

    def test_the_catalog_prose_names_both_ecmwf_and_open_meteo(self) -> None:
        prose = _source_prose(SPEC)
        self.assertEqual(prose["license"], "CC-BY-4.0")
        self.assertEqual([provider["name"] for provider in prose["providers"][:2]], ["ECMWF", "Open-Meteo"])
        self.assertIn("Adapted from ECMWF IFS by ECMWF, licensed under CC BY 4.0", prose["description"])
        self.assertIn("open-meteo.com", prose["description"])


class VariableRegistryTests(unittest.TestCase):
    def test_every_input_carries_the_name_open_meteo_gives_it(self) -> None:
        names = {variable_id: variable_spec(variable_id).open_meteo for variable_id in SPEC.input_variable_ids}
        self.assertEqual(
            names,
            {
                "tmp2m": "temperature_2m",
                "apcp": "precipitation",
                "ugrd10m": "wind_u_component_10m",
                "vgrd10m": "wind_v_component_10m",
                "dswrf": "shortwave_radiation",
                "prmsl": "pressure_msl",
                "gust": "wind_gusts_10m",
                "tcdc": "cloud_cover",
                "lcdc": "cloud_cover_low",
                "mcdc": "cloud_cover_mid",
                "hcdc": "cloud_cover_high",
                "cape": "cape",
                "dpt2m": "dew_point_2m",
                "vis": "visibility",
                "tmpsfc": "surface_temperature",
                "icetk": "sea_ice_thickness",
            },
        )
        # The series is opened by that name; every other source names its
        # NetCDF variable by the Xue id.
        self.assertEqual(observation.series_variable_name("tmp2m"), "temperature_2m")
        self.assertEqual(observation.series_variable_name("cref"), "cref")

    def test_apcp_is_an_interval_accumulation_on_the_ground_surface(self) -> None:
        spec = variable_spec("apcp")
        self.assertEqual(spec.output_unit, "mm")
        self.assertEqual(spec.value_range, (0, 1000))
        self.assertEqual(
            spec.parameter_metadata(),
            {
                "discipline": 0,
                "parameterCategory": 1,
                "parameterNumber": 8,
                "typeOfFirstFixedSurface": 1,
                "scaleFactorOfFirstFixedSurface": 0,
                "scaledValueOfFirstFixedSurface": 0,
            },
        )
        self.assertEqual(spec.grib2_statistical, 1)
        # Input only: no codebook, never a bundle.
        self.assertNotIn("apcp", PROFILES["quality"])

    def test_the_accepted_units_are_the_registry_s_and_what_the_converter_can_read(self) -> None:
        self.assertEqual(accepted_series_units(variable_spec("tmp2m")), ("°C", "degC", "K", "C", "F"))
        self.assertEqual(accepted_series_units(variable_spec("prmsl")), ("hPa", "Pa"))
        self.assertEqual(accepted_series_units(variable_spec("vis")), ("km", "m"))
        self.assertEqual(accepted_series_units(variable_spec("gust")), ("m/s", "m s-1"))
        self.assertEqual(accepted_series_units(variable_spec("dswrf")), ("W/m²", "W m-2"))
        self.assertEqual(accepted_series_units(variable_spec("cape")), ("J/kg", "J kg-1"))
        self.assertEqual(accepted_series_units(variable_spec("apcp")), ("mm", "kg m-2"))
        # A quantity with one spelling and no conversion keeps the one unit.
        self.assertEqual(accepted_series_units(variable_spec("tcdc")), ("%",))
        self.assertEqual(accepted_series_units(variable_spec("icetk")), ("m",))


class IntervalRateTests(unittest.TestCase):
    """``prate`` from an interval total: one division, whatever the step."""

    def test_the_rate_is_the_total_over_the_hours_it_covers(self) -> None:
        total = np.array([0.0, 1.5, 12.0])
        np.testing.assert_allclose(interval_rate(total, 1), [0.0, 1.5, 12.0])
        np.testing.assert_allclose(interval_rate(total, 3), [0.0, 0.5, 4.0])
        np.testing.assert_allclose(interval_rate(total, 6), [0.0, 0.25, 2.0])
        self.assertEqual(interval_rate(total, 3).dtype, np.float64)

    def test_an_interval_of_no_hours_is_a_conversion_error(self) -> None:
        for step in (0, -3):
            with self.assertRaisesRegex(ConversionError, "spans"):
                interval_rate(np.zeros(4), step)


class ToolTests(unittest.TestCase):
    def test_the_command_defaults_to_the_binary_on_path_and_takes_an_override(self) -> None:
        with mock.patch.dict(os.environ, {om2nccli.COMMAND_VARIABLE: ""}):
            self.assertEqual(om2nccli.command(), ["om2nc"])
        with mock.patch.dict(os.environ, {om2nccli.COMMAND_VARIABLE: "cargo run --quiet --bin om2nc --"}):
            self.assertEqual(om2nccli.command(), ["cargo", "run", "--quiet", "--bin", "om2nc", "--"])

    def test_a_missing_tool_says_where_to_get_it(self) -> None:
        with mock.patch.dict(os.environ, {om2nccli.COMMAND_VARIABLE: "/nonexistent/om2nc"}):
            with self.assertRaisesRegex(DownloadError, "releases"):
                om2nccli.version()
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr="command not found: om2nc\n")
        with mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(DownloadError, "command not found"):
                om2nccli.version()

    def test_the_steps_are_named_one_by_one(self) -> None:
        self.assertEqual(om2nccli.step_spec([0, 1, 2, 93, 360]), "0,1,2,93,360")
        with self.assertRaises(DownloadError):
            om2nccli.step_spec([])

    def test_the_fetch_command_names_the_run_the_variable_and_the_grid(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "ifshres.2026091800" / "ifshres.2026091800.tmp2m.nc"

            def run(arguments, **kwargs):
                output.write_bytes(b"series")
                return completed

            with mock.patch.object(subprocess, "run", side_effect=run) as runner, mock.patch.dict(
                os.environ, {om2nccli.COMMAND_VARIABLE: "om2nc"}
            ):
                om2nccli.fetch_variable(
                    model="ecmwf_ifs",
                    init="2026-09-18T00Z",
                    steps=[1, 2, 3],
                    variable="temperature_2m",
                    resolution=0.1,
                    output=output,
                    concurrency=8,
                )
            arguments = runner.call_args.args[0]
            self.assertEqual(arguments[:3], ["om2nc", "-q", "fetch"])
            for flag, value in (
                ("--model", "ecmwf_ifs"),
                ("--init", "2026-09-18T00Z"),
                ("--step", "1,2,3"),
                ("--var", "temperature_2m"),
                ("--resolution", "0.1"),
                ("--concurrency", "8"),
                ("--deflate", "1"),
                ("-o", str(output)),
            ):
                self.assertEqual(arguments[arguments.index(flag) + 1], value, flag)
            self.assertIn("--overwrite", arguments)
            # Every step asked for exists, and the rate derivation is the
            # converters' — never the tool's.
            self.assertNotIn("--missing-as-nan", arguments)
            self.assertNotIn("--accumulate", arguments)
            self.assertTrue(output.parent.is_dir())

    def test_a_failed_or_silent_tool_is_a_download_error(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            arguments = dict(
                model="ecmwf_ifs",
                init="2026-09-18T00Z",
                steps=[0],
                variable="temperature_2m",
                resolution=0.1,
                output=Path(root) / "out.nc",
                concurrency=8,
            )
            failed = subprocess.CompletedProcess([], 1, stdout="", stderr="step 1 is not available\n")
            with mock.patch.object(subprocess, "run", return_value=failed):
                with self.assertRaisesRegex(DownloadError, "step 1 is not available"):
                    om2nccli.fetch_variable(**arguments)
            silent = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with mock.patch.object(subprocess, "run", return_value=silent):
                with self.assertRaisesRegex(DownloadError, "wrote no series"):
                    om2nccli.fetch_variable(**arguments)


def meta_document(reference: str = "2026-09-18T00:00:00Z", completed: bool = True) -> str:
    return json.dumps({"completed": completed, "reference_time": reference, "valid_times": []})


class CompletenessTests(unittest.TestCase):
    def test_the_run_directory_is_the_bucket_s_own_layout(self) -> None:
        self.assertEqual(
            open_meteo_run_url(SPEC, RUN),
            f"{OPEN_METEO_BASE_URL}/data_spatial/ecmwf_ifs/2026/09/18/0000Z/",
        )
        self.assertEqual(open_meteo_series_name(SPEC, RUN, "tmp2m"), "ifshres.2026091800.tmp2m.nc")
        with self.assertRaisesRegex(DownloadError, "not an Open-Meteo source"):
            open_meteo_run_url(source_spec("gfs"), RUN)

    def test_a_run_is_complete_when_its_meta_json_says_so(self) -> None:
        asked: list[str] = []

        def fetch_text(url: str) -> str:
            asked.append(url)
            return meta_document()

        self.assertTrue(_open_meteo_run_is_complete(SPEC, RUN, fetch=fetch_text))
        self.assertEqual(asked, [f"{open_meteo_run_url(SPEC, RUN)}meta.json"])
        # Still being written, or written for another cycle.
        self.assertFalse(_open_meteo_run_is_complete(SPEC, RUN, fetch=lambda url: meta_document(completed=False)))
        self.assertFalse(
            _open_meteo_run_is_complete(SPEC, RUN, fetch=lambda url: meta_document("2026-09-17T12:00:00Z"))
        )

    def test_a_run_with_no_meta_json_is_incomplete_and_a_broken_bucket_is_an_error(self) -> None:
        def missing(url: str) -> str:
            raise DownloadError(f"expected HTTP 200 for {url}, received 404")

        def not_found(url: str) -> str:
            # What the bucket itself answers with: the code is on the cause.
            cause = urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
            raise DownloadError(f"request failed for {url}: {cause}") from cause

        def broken(url: str) -> str:
            raise DownloadError(f"expected HTTP 200 for {url}, received 503")

        self.assertFalse(_open_meteo_run_is_complete(SPEC, RUN, fetch=missing))
        self.assertFalse(_open_meteo_run_is_complete(SPEC, RUN, fetch=not_found))
        with self.assertRaisesRegex(DownloadError, "503"):
            _open_meteo_run_is_complete(SPEC, RUN, fetch=broken)
        with self.assertRaisesRegex(DownloadError, "not JSON"):
            _open_meteo_run_is_complete(SPEC, RUN, fetch=lambda url: "<html>")
        with self.assertRaisesRegex(DownloadError, "reference_time"):
            _open_meteo_run_is_complete(SPEC, RUN, fetch=lambda url: json.dumps({"completed": True}))

    def test_resolve_run_latest_walks_back_over_the_twelve_hour_cycles(self) -> None:
        now = datetime(2026, 9, 18, 13, 30, tzinfo=UTC)
        complete: set[str] = {"2026091800"}
        with mock.patch.object(
            fetch, "_open_meteo_run_is_complete", side_effect=lambda spec, run, **kwargs: run.id in complete
        ) as probe:
            run = resolve_run("latest", hours=360, now=now, model="ifshres")
        self.assertEqual(run.id, "2026091800")
        # 13:30 floors to the 12Z cycle, which has not landed yet; nothing in
        # between is ever asked for.
        self.assertEqual([call.args[1].id for call in probe.call_args_list], ["2026091812", "2026091800"])
        with mock.patch.object(fetch, "_open_meteo_run_is_complete", return_value=False):
            with self.assertRaisesRegex(DownloadError, "incomplete"):
                resolve_run("2026091800", hours=360, model="ifshres")


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-ifshres-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _fetcher(self, written: list[dict]):
        def fetch_variable(**kwargs) -> None:
            written.append(kwargs)
            kwargs["output"].write_bytes(b"series")

        return fetch_variable

    def test_the_fetch_writes_one_series_per_variable_and_a_record(self) -> None:
        written: list[dict] = []
        with (
            mock.patch.object(om2nccli, "version", return_value="om2nc 0.1.0"),
            mock.patch.object(om2nccli, "fetch_variable", side_effect=self._fetcher(written)),
        ):
            paths = fetch.fetch_run(RUN, 3, self.root, model="ifshres")
        destination = self.root / "ifshres.2026091800"
        self.assertEqual(len(paths), 16)
        self.assertEqual(paths[0], destination / "ifshres.2026091800.tmp2m.nc")
        self.assertTrue(all(path.is_file() for path in paths))
        first = written[0]
        self.assertEqual(
            (first["model"], first["init"], first["variable"], first["resolution"], first["concurrency"]),
            ("ecmwf_ifs", "2026-09-18T00Z", "temperature_2m", 0.1, 8),
        )
        # The instantaneous fields take the whole axis; the three that
        # describe an interval have no analysis file to read.
        steps = {call["variable"]: call["steps"] for call in written}
        self.assertEqual(steps["temperature_2m"], [0, 1, 2, 3])
        for name in ("precipitation", "shortwave_radiation", "wind_gusts_10m"):
            self.assertEqual(steps[name], [1, 2, 3], name)
        record = json.loads((destination / "fetch.json").read_text())
        self.assertEqual((record["model"], record["run"], record["hours"]), ("ifshres", "2026091800", 3))
        self.assertEqual((record["openMeteoModel"], record["resolution"], record["om2nc"]), ("ecmwf_ifs", 0.1, "om2nc 0.1.0"))
        self.assertEqual(record["variables"][1], {"variable": "apcp", "series": "ifshres.2026091800.apcp.nc", "steps": 3})

    def test_a_job_fetches_only_its_own_bundles_inputs(self) -> None:
        written: list[dict] = []
        with (
            mock.patch.object(om2nccli, "version", return_value="om2nc 0.1.0"),
            mock.patch.object(om2nccli, "fetch_variable", side_effect=self._fetcher(written)),
        ):
            paths = _fetch_open_meteo_run(
                SPEC, RUN, 3, self.root, force=False, input_ids=("apcp", "ugrd10m", "vgrd10m")
            )
        self.assertEqual([path.name.split(".")[-2] for path in paths], ["apcp", "ugrd10m", "vgrd10m"])
        with self.assertRaisesRegex(DownloadError, "publishes"):
            _fetch_open_meteo_run(SPEC, RUN, 3, self.root, force=False, input_ids=("cref",))

    def test_a_series_already_on_disk_is_kept_unless_forced(self) -> None:
        written: list[dict] = []
        fetcher = self._fetcher(written)
        for force, calls in ((False, 0), (True, 1)):
            written.clear()
            with (
                mock.patch.object(om2nccli, "version", return_value="om2nc 0.1.0"),
                mock.patch.object(om2nccli, "fetch_variable", side_effect=fetcher),
            ):
                _fetch_open_meteo_run(SPEC, RUN, 3, self.root, force=False, input_ids=("tmp2m",))
                written.clear()
                _fetch_open_meteo_run(SPEC, RUN, 3, self.root, force=force, input_ids=("tmp2m",))
            self.assertEqual(len(written), calls, f"force={force}")


def series_info(
    unit: str = "degC",
    times: tuple[float, ...] = (0.0, 1.0),
    epoch: str = "hours since 2026-09-18 00:00:00",
    reference: str | None = "2026-09-18T00:00:00Z",
    fill: object = None,
) -> dict:
    """What ``gdalinfo`` reports for one variable of an om2nc series."""
    metadata = {"time#units": epoch}
    if reference is not None:
        metadata["NC_GLOBAL#forecast_reference_time"] = reference
    return {
        "size": [81, 81],
        "geoTransform": [129.95, 0.1, 0.0, 38.05, 0.0, -0.1],
        "metadata": {"": metadata},
        "bands": [
            {
                "band": index + 1,
                "unit": unit,
                "noDataValue": fill,
                "metadata": {"": {"NETCDF_DIM_time": str(time), "_FillValue": "nan"}},
            }
            for index, time in enumerate(times)
        ],
    }


class SeriesIngestTests(unittest.TestCase):
    """The NetCDF ingest on a forecast series: the cycle comes from the time
    coordinate's epoch, not from the first frame."""

    def _inspect(self, info: dict, variable_ids: tuple[str, ...] = ("tmp2m",)) -> observation.ObservationSeries:
        with (
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(observation, "dataset_info", return_value=info),
        ):
            return observation.inspect_observation(Path("ifshres.nc"), SPEC, variable_ids)

    def test_the_run_is_the_epoch_of_the_time_axis(self) -> None:
        series = self._inspect(series_info(times=(0.0, 1.0, 2.0)))
        first = series.frames[0]["tmp2m"]
        self.assertEqual(first.run_time, datetime(2026, 9, 18, tzinfo=UTC))
        self.assertEqual(series.lead_seconds, [0, 3600, 7200])
        self.assertEqual(str(first.path), 'NETCDF:"ifshres.nc":temperature_2m')

    def test_a_series_that_starts_at_a_step_keeps_the_cycle_s_numbering(self) -> None:
        """The whole point of reading the run off the epoch: the rate's own
        file has no analysis band, and its first frame must still be lead 1
        rather than lead 0."""
        series = self._inspect(series_info(unit="mm", times=(1.0, 2.0, 3.0)), ("apcp",))
        self.assertEqual(series.frames[0]["apcp"].run_time, datetime(2026, 9, 18, tzinfo=UTC))
        self.assertEqual(series.lead_seconds, [3600, 7200, 10800])

    def test_a_reference_time_that_contradicts_the_axis_is_refused(self) -> None:
        with self.assertRaisesRegex(ConversionError, "but its time axis counts from"):
            self._inspect(series_info(reference="2026-09-17T12:00:00Z"))
        # The attribute is optional; the epoch is what the run is read from.
        self.assertEqual(
            self._inspect(series_info(reference=None)).frames[0]["tmp2m"].run_time,
            datetime(2026, 9, 18, tzinfo=UTC),
        )

    def test_a_unit_the_converter_cannot_read_is_refused(self) -> None:
        self.assertEqual(self._inspect(series_info(unit="K")).frames[0]["tmp2m"].unit, "K")
        with self.assertRaisesRegex(ConversionError, "reports unit mm, expected"):
            self._inspect(series_info(unit="mm"))
        with self.assertRaisesRegex(ConversionError, "expected"):
            self._inspect(series_info(unit=""))

    def test_a_nan_fill_value_is_read_off_the_bands_however_gdal_reports_it(self) -> None:
        """A NaN is not a JSON number: the subprocess gdalinfo spells the
        nodata "NaN" and the wheel's linked GDAL reports none at all, so the
        band's own ``_FillValue`` attribute settles it."""
        for reported in (None, "NaN", float("nan")):
            with self.subTest(noDataValue=reported):
                series = self._inspect(series_info(fill=reported))
                self.assertTrue(series.plane_sources["tmp2m"].fill_nan)
                self.assertEqual(series.plane_sources["tmp2m"].fill_values, ())

    @requires_gdal
    def test_the_fixture_run_reads_as_one_cycle_with_three_shorter_series(self) -> None:
        series = observation.inspect_observation(SERIES_DIR, SPEC)
        self.assertEqual(series.lead_seconds, [0, 3600, 7200, 10800])
        self.assertEqual(series.frames[0]["tmp2m"].run_time, datetime(2026, 9, 18, tzinfo=UTC))
        # The analysis frame carries every variable but the three that
        # describe an interval; every later frame carries all sixteen.
        self.assertEqual(set(SPEC.input_variable_ids) - set(series.frames[0]), {"apcp", "dswrf", "gust"})
        self.assertEqual(set(series.frames[1]), set(SPEC.input_variable_ids))
        self.assertEqual(series.frames[1]["apcp"].lead_seconds, 3600)
        self.assertTrue(series.plane_sources["icetk"].fill_nan)
        self.assertEqual(series.plane_sources["icetk"].fill_replacement, 0.0)

    @requires_gdal
    def test_the_slack_is_granted_by_the_declaration_not_by_the_files(self) -> None:
        """One frame of slack, and only for a variable the source declares
        optional at the analysis: a series short of a frame it never
        declared is a fetch that went wrong, not a shorter axis."""
        undeclared = dataclasses.replace(SPEC, optional_at_analysis=())
        with self.assertRaisesRegex(ConversionError, "another time axis"):
            observation.inspect_observation(SERIES_DIR, undeclared, ("tmp2m", "apcp"))


@requires_gdal
class ConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-ifshres-"))
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            cls.report = binconvert.convert_bin(
                SERIES_DIR,
                cls.root / "out",
                model="ifshres",
                skip_video=True,
                work_root=cls.root / "work",
                run_id="2026091800",
                manifest_path=cls.root / "out" / "manifest.json",
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def manifest(self) -> dict:
        return json.loads((self.root / "out" / "manifest.json").read_text())

    def bundle(self, name: str):
        return read_bundle(self.root / "out" / f"{name}.xue")

    def test_the_run_publishes_every_bundle_the_source_declares(self) -> None:
        self.assertEqual(tuple(bundle["variable"] for bundle in self.report["bundles"]), BUNDLES)
        self.assertEqual(self.report["videos"], [])
        manifest = self.manifest()
        self.assertEqual((manifest["model"], manifest["product"]), ("ECMWF-HRES", "ifs-hres-0p1"))
        self.assertEqual(manifest["runTime"], "2026-09-18T00:00:00Z")
        self.assertEqual(manifest["forecastHours"], 3)
        validate_bin_manifest(manifest, expected_hours=3, require_core_variables=True)
        for name in ("tmp2m.half.xue", "tmp2m.quarter.xue", "tmp2m.poster.bin"):
            self.assertTrue((self.root / "out" / name).is_file(), name)

    def test_the_grid_is_the_fixture_s_window_of_the_published_one(self) -> None:
        grid = self.bundle("tmp2m").metadata["grid"]
        self.assertEqual((grid["width"], grid["height"]), (81, 81))
        self.assertEqual((grid["longitudeStep"], grid["latitudeStep"]), (0.1, -0.1))
        self.assertEqual((grid["firstLongitude"], grid["firstLatitude"]), (130.0, 38.0))
        self.assertEqual(grid["rowOrder"], "north-to-south")
        self.assertFalse(grid["wrapLongitude"])

    def test_the_interval_quantities_start_at_the_first_step(self) -> None:
        for name in ("prate", "dswrf", "gust"):
            with self.subTest(bundle=name):
                self.assertEqual(
                    self.bundle(name).metadata["time"],
                    {"unitSeconds": 3600, "firstFrameOffset": 1, "frameCount": 3, "frameStep": 1},
                )
        # Everything else runs from the analysis, on the same uniform step —
        # so no bundle of this fixture lists its offsets.
        self.assertEqual(
            self.bundle("tmp2m").metadata["time"],
            {"unitSeconds": 3600, "firstFrameOffset": 0, "frameCount": 4, "frameStep": 1},
        )

    def test_the_statistics_are_declared_on_the_parameter(self) -> None:
        processes = {
            name: self.bundle(name).metadata["variables"][0]["parameter"].get("typeOfStatisticalProcessing")
            for name in ("prate", "dswrf", "gust", "tmp2m")
        }
        self.assertEqual(processes, {"prate": 0, "dswrf": 0, "gust": 2, "tmp2m": None})

    def test_the_values_are_the_source_s_in_the_codebook_s_units(self) -> None:
        for name, converted in (
            ("tmp2m", lambda plane: plane),
            ("prmsl", lambda plane: plane / 100.0),
            ("vis", lambda plane: plane / 1000.0),
        ):
            with self.subTest(bundle=name):
                bundle = self.bundle(name)
                codebook = PROFILES["quality"][name]
                decoded = codebook.decode(np.asarray(bundle.decode_plane(1, 0)).reshape(81, 81))
                # The file's own values in the codebook's unit, clamped to
                # what it can hold (this box carries visibilities far past
                # the top of the scale).
                source = np.clip(converted(source_plane(name, 1)), codebook.minimum, codebook.maximum)
                # Half a step, cell for cell: the same values, the same
                # units and the same north-up orientation.
                self.assertLessEqual(
                    float(np.abs(decoded - source).max()), codebook.metadata()["scale"] / 2 + 1e-9
                )

    def test_the_rate_is_the_interval_total_over_its_hour(self) -> None:
        bundle = self.bundle("prate")
        codebook = PROFILES["quality"]["prate"]
        rates = codebook.decode(np.asarray(bundle.decode_plane(1, 1)).reshape(81, 81))
        total = source_plane("apcp", 1)
        # The fixture's steps are an hour apart, so the rate is the total
        # itself, within the codebook's own resolution.
        self.assertGreater(float(total.max()), 1.0)
        self.assertLess(float(np.abs(rates - total).max() / total.max()), 0.05)

    def test_the_land_under_the_sea_ice_becomes_the_codebook_bottom(self) -> None:
        plane = np.asarray(self.bundle("icetk").decode_plane(1, 0)).reshape(81, 81)
        land = np.isnan(source_plane("icetk", 1))
        self.assertGreater(int(land.sum()), 1000, "the fixture's box is mostly land")
        self.assertEqual(np.unique(plane[land]).tolist(), [0])

    def test_a_complete_build_wants_the_whole_published_grid(self) -> None:
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "python"}):
            with self.assertRaisesRegex(ConversionError, "3600x1801 grid"):
                binconvert.convert_bin(
                    SERIES_DIR,
                    self.root / "complete",
                    model="ifshres",
                    skip_video=True,
                    require_complete=True,
                    expected_hours=3,
                )

    @unittest.skipUnless(
        native.knows_source("ifshres"), f"the installed {native.DISTRIBUTION} wheel predates the ifshres source"
    )
    def test_the_native_encoder_writes_the_same_bytes(self) -> None:
        if not zstdcli.compresses_in_process():
            self.skipTest("the reference encoder compresses through the zstd CLI")
        subject = self.root / "native"
        with mock.patch.dict(os.environ, {"XUE_ENCODER": "native"}):
            report = native.convert_bin(
                SERIES_DIR, subject, model="ifshres", skip_video=True, manifest_path=subject / "manifest.json"
            )
        if report["zstdVersion"] != self.report["zstdVersion"]:
            self.skipTest("libzstd differs between the reference and the wheel")
        names = sorted(path.name for path in (self.root / "out").iterdir())
        self.assertEqual(sorted(path.name for path in subject.iterdir()), names)
        for name in names:
            with self.subTest(artifact=name):
                self.assertTrue(filecmp.cmp(self.root / "out" / name, subject / name, shallow=False), name)


if __name__ == "__main__":
    unittest.main()

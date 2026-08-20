"""GFS sflux source: URL layout, the optional
analysis-frame PRATE, window de-averaging, Gaussian-grid longitude
normalization, and the dswrf variable's registries."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import numpy as np

from xue.binconvert import (
    GridInfo,
    VARIABLE_NUMERIC_IDS,
    _normalize_longitudes,
    average_window_start,
    deaverage_precipitation,
)
from xue.errors import ConversionError, DownloadError, ManifestError
from xue.fetch import fetch_frame, resolve_run, sflux_object_url
from xue.gdal import averaged_precipitation_expression, flux_expression, raster_expression
from xue.idx import field_byte_range
from xue.manifest import build_bin_manifest, validate_bin_manifest
from xue.model import GfsRun
from xue.quantize import PROFILES, TemperatureCodebook
from xue.sources import source_spec
from xue.variables import variable_spec


SFLUX_F003_IDX = "\n".join(
    [
        "33:63501241:d=2026081700:TMP:2 m above ground:3 hour fcst:",
        "39:79927258:d=2026081700:UGRD:10 m above ground:3 hour fcst:",
        "40:83699053:d=2026081700:VGRD:10 m above ground:3 hour fcst:",
        "42:89389420:d=2026081700:CPRAT:surface:0-3 hour ave fcst:",
        "43:92384838:d=2026081700:PRATE:surface:0-3 hour ave fcst:",
        "44:94803105:d=2026081700:SSRUN:surface:0-3 hour acc fcst:",
        "87:220263116:d=2026081700:DSWRF:surface:0-3 hour ave fcst:",
        "95:246489470:d=2026081700:DSWRF:surface:3 hour fcst:",
        "96:249701643:d=2026081700:DLWRF:surface:3 hour fcst:",
    ]
)


class SfluxFetchTests(unittest.TestCase):
    def test_object_url_and_resolution(self) -> None:
        run = GfsRun(datetime(2026, 8, 15, 6, tzinfo=UTC))
        from xue.fetch import BASE_URL

        self.assertEqual(
            sflux_object_url(run, 0),
            f"{BASE_URL}/gfs.20260815/06/atmos/gfs.t06z.sfluxgrbf000.grib2",
        )
        self.assertTrue(sflux_object_url(run, 120).endswith("gfs.20260815/06/atmos/gfs.t06z.sfluxgrbf120.grib2"))

        now = datetime(2026, 8, 15, 11, 35, tzinfo=UTC)

        def exists(url: str) -> bool:
            return "gfs.20260815/00/" in url and "sfluxgrb" in url

        resolved = resolve_run("latest", hours=120, now=now, exists=exists, model="sflux")
        self.assertEqual(resolved.id, "2026081500")

    def test_index_selection_picks_averaged_prate_and_instantaneous_dswrf(self) -> None:
        prate = variable_spec("prate_ave")
        selected = field_byte_range(
            SFLUX_F003_IDX, prate.index_field, excluded_phrases=prate.excluded_index_phrases
        )
        self.assertEqual((selected.start, selected.end), (92384838, 94803104))

        dswrf = variable_spec("dswrf")
        selected = field_byte_range(
            SFLUX_F003_IDX, dswrf.index_field, excluded_phrases=dswrf.excluded_index_phrases
        )
        self.assertEqual((selected.start, selected.end), (246489470, 249701642))

    def test_analysis_frame_reuse_skips_the_missing_prate_record(self) -> None:
        run = GfsRun(datetime(2026, 8, 15, 6, tzinfo=UTC))
        spec = source_spec("sflux")
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            (destination / "sflux.2026081506.f000.grib2").write_bytes(b"existing GRIB")
            (destination / "sflux.2026081506.f003.grib2").write_bytes(b"existing GRIB")

            with patch("xue.gdal.inspect_grib") as inspect_grib:
                fetch_frame(run, 0, destination, model="sflux")
                analysis_ids = [call.args[1] for call in inspect_grib.call_args_list]
                inspect_grib.reset_mock()
                fetch_frame(run, 3, destination, model="sflux")
                forecast_ids = [call.args[1] for call in inspect_grib.call_args_list]

        self.assertEqual(
            analysis_ids,
            [variable_id for variable_id in spec.input_variable_ids if variable_id != "prate_ave"],
        )
        self.assertEqual(forecast_ids, list(spec.input_variable_ids))


class DeaverageTests(unittest.TestCase):
    def test_window_start_resets_every_six_hours(self) -> None:
        expected = {1: 0, 5: 0, 6: 0, 7: 6, 12: 6, 13: 12, 120: 114}
        for hour, start in expected.items():
            self.assertEqual(average_window_start(hour, 6), start)
        with self.assertRaises(ConversionError):
            average_window_start(0, 6)

    def test_window_first_frame_is_its_own_interval(self) -> None:
        # f007 opens the 6-12 window: its 1-hour average IS the hourly rate.
        average = np.array([0.001, 0.0])
        rate = deaverage_precipitation(average, 7, None, None, 6, 1)
        np.testing.assert_allclose(rate, average * 3600.0)

    def test_inside_window_differences_cumulative_averages(self) -> None:
        # f008 averages 6-8h; the 7-8h hourly rate is 2*ave8 - 1*ave7.
        ave7 = np.array([0.001])
        ave8 = np.array([0.0015])
        rate = deaverage_precipitation(ave8, 8, ave7, 7, 6, 1)
        np.testing.assert_allclose(rate, (2 * ave8 - ave7) * 3600.0)

    def test_packing_noise_clamps_to_zero(self) -> None:
        rate = deaverage_precipitation(np.array([0.001]), 6, np.array([0.00125]), 5, 6, 1)
        np.testing.assert_allclose(rate, [0.0])

    def test_previous_frame_outside_window_rejected(self) -> None:
        with self.assertRaises(ConversionError):
            deaverage_precipitation(np.array([0.001]), 7, np.array([0.001]), 6, 6, 1)


class LongitudeNormalizationTests(unittest.TestCase):
    def test_sflux_gaussian_grid_rolls_to_minus_180(self) -> None:
        # GDAL computes the sflux first cell center as exactly 0.0 (edge
        # -step/2 + step/2); tiny positive offsets must normalize identically.
        for first_longitude in (0.0, 0.0000000407):
            grid = _normalize_longitudes(
                GridInfo(
                    width=3072,
                    height=1536,
                    first_longitude=first_longitude,
                    first_latitude=89.9103,
                    longitude_step=0.1171875001628134,
                    latitude_step=-0.1171470019543974,
                )
            )
            self.assertEqual(grid.column_roll, 1536)
            self.assertAlmostEqual(grid.first_longitude, -180.0, places=5)
            self.assertTrue(grid.wraps)
            # The decimated (poster / half tier) grid never re-rolls.
            self.assertEqual(grid.decimated().column_roll, 0)

    def test_grids_already_starting_at_minus_180_stay_unrolled(self) -> None:
        grid = _normalize_longitudes(
            GridInfo(
                width=1440,
                height=721,
                first_longitude=-179.875,
                first_latitude=89.875,
                longitude_step=0.25,
                latitude_step=-0.25,
            )
        )
        self.assertEqual(grid.column_roll, 0)
        self.assertEqual(grid.first_longitude, -179.875)


class DswrfRegistryTests(unittest.TestCase):
    def test_numeric_id_and_codebooks(self) -> None:
        self.assertEqual(VARIABLE_NUMERIC_IDS["dswrf"], 5)
        for profile, codebooks in PROFILES.items():
            codebook = codebooks["dswrf"]
            self.assertIsInstance(codebook, TemperatureCodebook)
            self.assertEqual(codebook.minimum, 0.0)
            self.assertEqual(codebook.maximum, 1270.0)
            self.assertLessEqual(codebook.maximum_code, 254, profile)

    def test_flux_units_and_expression(self) -> None:
        self.assertEqual(flux_expression("[W/(m^2)]"), "maximum(0,minimum(1270,A))")
        self.assertEqual(raster_expression("dswrf", "W/m^2"), "maximum(0,minimum(1270,A))")
        with self.assertRaises(ConversionError):
            flux_expression("J/m^2")
        self.assertEqual(averaged_precipitation_expression("[kg/(m^2 s)]"), "A")
        with self.assertRaises(ConversionError):
            averaged_precipitation_expression("mm")

    def test_source_registry(self) -> None:
        spec = source_spec("sflux")
        self.assertEqual(spec.manifest_model, "GFS-SFLUX")
        self.assertEqual(spec.product, "sfluxgrb")
        self.assertEqual(spec.latest_filename, "latest-sflux.json")
        self.assertEqual(spec.production_grid, (3072, 1536))
        self.assertEqual(spec.bundle_scalar_ids, ("tmp2m", "prate", "dswrf"))
        self.assertTrue(spec.averaged_precipitation)
        self.assertEqual(spec.optional_at_analysis, ("prate_ave",))
        with self.assertRaises(DownloadError):
            source_spec("gefs")


class SfluxManifestTests(unittest.TestCase):
    def _bundles(self, with_dswrf: bool) -> list[dict[str, object]]:
        bundles = [
            {"variable": "tmp2m", "path": "sflux.2026081506/tmp2m.xue", "byteLength": 1, "crc32": "0" * 8},
            {"variable": "prate", "path": "sflux.2026081506/prate.xue", "byteLength": 1, "crc32": "1" * 8},
        ]
        if with_dswrf:
            bundles.append(
                {"variable": "dswrf", "path": "sflux.2026081506/dswrf.xue", "byteLength": 1, "crc32": "2" * 8}
            )
        bundles.append(
            {"variable": "wind10m", "path": "sflux.2026081506/wind10m.xue", "byteLength": 1, "crc32": "3" * 8}
        )
        return bundles

    def test_sflux_manifest_with_dswrf_validates(self) -> None:
        payload = build_bin_manifest(
            datetime(2026, 8, 15, 6, tzinfo=UTC),
            bundles=self._bundles(with_dswrf=True),
            model="GFS-SFLUX",
            product="sfluxgrb",
        )
        validate_bin_manifest(payload)

    def test_dswrf_stays_optional_and_ordered(self) -> None:
        payload = build_bin_manifest(
            datetime(2026, 8, 15, 6, tzinfo=UTC),
            bundles=self._bundles(with_dswrf=False),
            model="GFS-SFLUX",
            product="sfluxgrb",
        )
        validate_bin_manifest(payload)
        misordered = build_bin_manifest(
            datetime(2026, 8, 15, 6, tzinfo=UTC),
            bundles=self._bundles(with_dswrf=True),
            model="GFS-SFLUX",
            product="sfluxgrb",
        )
        order = misordered["bundles"]
        order[2], order[3] = order[3], order[2]  # dswrf after wind10m
        with self.assertRaises(ManifestError):
            validate_bin_manifest(misordered)

    def test_mismatched_product_rejected(self) -> None:
        payload = build_bin_manifest(
            datetime(2026, 8, 15, 6, tzinfo=UTC),
            bundles=self._bundles(with_dswrf=True),
            model="GFS-SFLUX",
            product="sfluxgrb",
        )
        payload["product"] = "pgrb2.0p25"
        with self.assertRaises(ManifestError):
            validate_bin_manifest(payload)


if __name__ == "__main__":
    unittest.main()

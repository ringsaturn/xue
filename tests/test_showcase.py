from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from xue.binconvert import GridInfo, bundle_input_ids, crop_grid, published_bundle_ids
from xue.errors import ConversionError, ManifestError
from xue.manifest import build_bin_manifest, validate_bin_manifest
from xue.showcase import (
    ShowcaseError,
    _grid_extent,
    build_catalog_entry,
    load_case,
    load_cases,
    parse_case,
    validate_catalog_entry,
)
from xue.sources import source_spec

PRODUCTION_GRID = GridInfo(1440, 721, -180.0, 90.0, 0.25, -0.25)
CASES_DIRECTORY = Path(__file__).resolve().parent.parent / "showcase" / "cases"


def case_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "demo-case",
        "title": {"zh": "示例", "en": "Demo"},
        "summary": {"zh": "示例说明", "en": "Demo summary"},
        "model": "gfs",
        "run": "2021071800",
        "hours": 24,
        "bbox": [105.0, 28.0, 122.0, 42.0],
        "variables": ["prate", "wind10m"],
        "defaultVariable": "prate",
    }
    payload.update(overrides)
    return payload


class CropGridTest(unittest.TestCase):
    def test_window_covers_the_requested_box(self) -> None:
        grid = crop_grid(PRODUCTION_GRID, (105.0, 28.0, 122.0, 42.0))
        self.assertLessEqual(grid.first_longitude, 105.0)
        self.assertGreaterEqual(grid.first_longitude + (grid.width - 1) * 0.25, 122.0)
        self.assertGreaterEqual(grid.first_latitude, 42.0)
        self.assertLessEqual(grid.first_latitude + (grid.height - 1) * -0.25, 28.0)
        self.assertFalse(grid.wraps)
        self.assertEqual(grid.source_shape, (721, 1440))

    def test_window_is_the_smallest_one_that_covers_the_box(self) -> None:
        # Edges landing exactly on cell centers need no extra cell.
        grid = crop_grid(PRODUCTION_GRID, (100.0, 20.0, 110.0, 30.0))
        self.assertEqual((grid.width, grid.height), (41, 41))
        self.assertEqual((grid.first_longitude, grid.first_latitude), (100.0, 30.0))

    def test_crop_matches_a_plain_numpy_window(self) -> None:
        grid = crop_grid(PRODUCTION_GRID, (105.0, 28.0, 122.0, 42.0))
        plane = np.arange(1440 * 721, dtype=np.float64).reshape(721, 1440)
        window = grid.crop.take(plane)
        column = int(round((grid.first_longitude + 180.0) / 0.25))
        row = int(round((90.0 - grid.first_latitude) / 0.25))
        self.assertTrue(np.array_equal(window, plane[row : row + grid.height, column : column + grid.width]))

    def test_antimeridian_window_stays_contiguous(self) -> None:
        grid = crop_grid(PRODUCTION_GRID, (170.0, -20.0, -170.0, 10.0))
        self.assertEqual(grid.width, 81)
        self.assertEqual(grid.first_longitude, 170.0)
        plane = np.arange(1440 * 721, dtype=np.float64).reshape(721, 1440)
        window = grid.crop.take(plane)
        self.assertEqual(window.shape, (grid.height, grid.width))
        # The seam is where the source's last column meets its first.
        self.assertEqual(window[0, 39], plane[grid.crop.row_start, 1439])
        self.assertEqual(window[0, 40], plane[grid.crop.row_start, 0])

    def test_full_width_band_keeps_wrapping(self) -> None:
        grid = crop_grid(PRODUCTION_GRID, (-180.0, -30.0, 180.0, 30.0))
        self.assertEqual(grid.width, 1440)
        self.assertEqual(grid.first_longitude, -180.0)
        self.assertTrue(grid.wraps)

    def test_window_clamps_to_a_regional_source(self) -> None:
        # A box hanging off the source keeps the overlap, not the request.
        regional = GridInfo(100, 50, 100.0, 40.0, 0.25, -0.25)
        grid = crop_grid(regional, (90.0, 20.0, 110.0, 50.0))
        self.assertEqual((grid.first_longitude, grid.first_latitude), (100.0, 40.0))
        self.assertEqual((grid.width, grid.height), (41, 50))

    def test_column_roll_survives_the_crop(self) -> None:
        gaussian = GridInfo(3072, 1536, -180.0, 89.91, 360 / 3072, -0.1171875, column_roll=1536)
        grid = crop_grid(gaussian, (-100.0, 20.0, -60.0, 50.0))
        self.assertEqual(grid.column_roll, 1536)
        self.assertEqual(grid.crop.source_width, 3072)

    def test_rejects_a_box_a_regional_source_cannot_serve(self) -> None:
        regional = GridInfo(100, 50, 100.0, 40.0, 0.25, -0.25)
        with self.assertRaises(ConversionError):
            crop_grid(regional, (170.0, 20.0, -170.0, 30.0))
        with self.assertRaises(ConversionError):
            crop_grid(regional, (0.0, 20.0, 20.0, 30.0))

    def test_rejects_invalid_boxes(self) -> None:
        for bbox in [(105.0, 42.0, 122.0, 28.0), (105.0, -100.0, 122.0, 42.0), (105.0, 28.0, 122.0, 95.0)]:
            with self.assertRaises(ConversionError):
                crop_grid(PRODUCTION_GRID, bbox)

    def test_equal_longitudes_span_the_whole_globe(self) -> None:
        # -180 to 180 is a full turn, which is zero modulo 360, so equal
        # longitudes have to mean the whole width rather than nothing.
        for bbox in [(-180.0, -30.0, 180.0, 30.0), (105.0, -30.0, 105.0, 30.0)]:
            self.assertEqual(crop_grid(PRODUCTION_GRID, bbox).width, 1440)

    def test_a_grid_can_only_be_cropped_once(self) -> None:
        grid = crop_grid(PRODUCTION_GRID, (105.0, 28.0, 122.0, 42.0))
        with self.assertRaises(ConversionError):
            crop_grid(grid, (106.0, 29.0, 120.0, 40.0))


class BundleInputsTest(unittest.TestCase):
    def test_precipitation_resolves_per_source(self) -> None:
        self.assertEqual(bundle_input_ids(source_spec("gfs"), "prate"), ("prate",))
        self.assertEqual(bundle_input_ids(source_spec("ecmwf"), "prate"), ("tp",))
        self.assertEqual(bundle_input_ids(source_spec("sflux"), "prate"), ("prate_ave",))

    def test_wind_expands_into_both_components(self) -> None:
        self.assertEqual(bundle_input_ids(source_spec("gfs"), "wind10m"), ("ugrd10m", "vgrd10m"))

    def test_only_sflux_publishes_solar_radiation(self) -> None:
        self.assertIn("dswrf", published_bundle_ids(source_spec("sflux")))
        self.assertNotIn("dswrf", published_bundle_ids(source_spec("gfs")))


class CaseDefinitionTest(unittest.TestCase):
    def test_parses_a_complete_definition(self) -> None:
        spec = parse_case(case_payload(tags=["typhoon"], eventTime="2021-07-20T08:00:00Z"))
        self.assertEqual(spec.id, "demo-case")
        self.assertEqual(spec.variables, ("prate", "wind10m"))
        self.assertEqual(spec.output_subdirectory, "showcase/demo-case")
        self.assertEqual(spec.profile, "quality")

    def test_variables_are_reordered_into_manifest_order(self) -> None:
        spec = parse_case(case_payload(variables=["wind10m", "prate", "tmp2m"]))
        self.assertEqual(spec.variables, ("tmp2m", "prate", "wind10m"))

    def test_rejects_a_variable_the_source_does_not_publish(self) -> None:
        with self.assertRaises(ShowcaseError):
            parse_case(case_payload(variables=["dswrf"]))

    def test_rejects_an_hour_off_the_published_axis(self) -> None:
        with self.assertRaises(ShowcaseError):
            parse_case(case_payload(hours=125))

    def test_rejects_a_default_variable_the_case_does_not_ship(self) -> None:
        with self.assertRaises(ShowcaseError):
            parse_case(case_payload(defaultVariable="tmp2m"))

    def test_rejects_a_missing_locale(self) -> None:
        with self.assertRaises(ShowcaseError):
            parse_case(case_payload(title={"en": "Demo"}))

    def test_rejects_an_sflux_case_with_nothing_at_the_analysis_hour(self) -> None:
        # sflux publishes no PRATE record at f000, so a prate-only case has no
        # variable to key its frames by.
        with self.assertRaises(ShowcaseError):
            parse_case(case_payload(model="sflux", variables=["prate"], defaultVariable="prate"))
        parse_case(case_payload(model="sflux", variables=["prate", "wind10m"], defaultVariable="prate"))

    def test_definition_file_must_be_named_after_its_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "other-name.json"
            path.write_text(json.dumps(case_payload()), encoding="utf-8")
            with self.assertRaises(ShowcaseError):
                load_case(path)

    def test_shipped_case_definitions_are_valid(self) -> None:
        specs = load_cases(CASES_DIRECTORY)
        self.assertTrue(specs)
        self.assertEqual(len({spec.id for spec in specs}), len(specs))


class CatalogEntryTest(unittest.TestCase):
    def build_entry(self, **overrides: object) -> dict[str, object]:
        spec = parse_case(case_payload(**overrides))
        grid = crop_grid(PRODUCTION_GRID, spec.bbox)
        manifest = build_bin_manifest(
            __import__("datetime").datetime(2021, 7, 18, tzinfo=__import__("datetime").UTC),
            bundles=[
                {"variable": "prate", "path": "prate.xue", "byteLength": 32, "crc32": "00000000"},
                {"variable": "wind10m", "path": "wind10m.xue", "byteLength": 64, "crc32": "00000001"},
            ],
            expected_hours=spec.hours,
            require_core_variables=False,
        )
        return build_catalog_entry(
            spec,
            manifest,
            json.dumps(manifest).encode(),
            {"byteLength": 96, "grid": grid.metadata()},
        )

    def test_entry_describes_the_cropped_case(self) -> None:
        entry = self.build_entry()
        self.assertEqual(entry["manifestPath"], "showcase/demo-case/manifest.json")
        self.assertEqual(entry["variables"], ["prate", "wind10m"])
        self.assertEqual(entry["modelId"], "gfs")
        self.assertEqual(entry["model"], "GFS")
        # The data extent covers the requested box, rounded out to whole cells.
        west, south, east, north = entry["dataBbox"]
        self.assertLessEqual(west, 105.0)
        self.assertGreaterEqual(east, 122.0)
        self.assertLessEqual(south, 28.0)
        self.assertGreaterEqual(north, 42.0)

    def test_rejects_a_manifest_outside_the_case_directory(self) -> None:
        entry = self.build_entry()
        entry["manifestPath"] = "showcase/other/manifest.json"
        with self.assertRaises(ShowcaseError):
            validate_catalog_entry(entry)

    def test_grid_extent_of_an_antimeridian_window(self) -> None:
        grid = crop_grid(PRODUCTION_GRID, (170.0, -20.0, -170.0, 10.0))
        west, south, east, north = _grid_extent(grid.metadata())
        self.assertEqual(west, 170.0)
        self.assertEqual(east, -170.0)
        self.assertLessEqual(south, -20.0)
        self.assertGreaterEqual(north, 10.0)


class RestrictedManifestTest(unittest.TestCase):
    """A case manifest ships only its event's bundles; a run manifest must
    still carry the core pair."""

    def payload(self) -> dict[str, object]:
        return build_bin_manifest(
            __import__("datetime").datetime(2021, 7, 18, tzinfo=__import__("datetime").UTC),
            bundles=[{"variable": "prate", "path": "prate.xue", "byteLength": 32, "crc32": "00000000"}],
            expected_hours=24,
            require_core_variables=False,
        )

    def test_a_case_manifest_may_omit_the_core_pair(self) -> None:
        validate_bin_manifest(self.payload(), expected_hours=24, require_core_variables=False)

    def test_a_run_manifest_may_not(self) -> None:
        with self.assertRaises(ManifestError):
            validate_bin_manifest(self.payload(), expected_hours=24)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from xuebuild.binconvert import GridInfo, bundle_input_ids, crop_grid, published_bundle_ids
from xuebuild.errors import ConversionError, ManifestError, XueError
from xuebuild.manifest import build_bin_manifest, validate_bin_manifest
from xuebuild.showcase import (
    LOCALES,
    OBSERVATION_ROOT_ENV,
    ShowcaseError,
    _grid_extent,
    build_catalog_entry,
    collect_catalog,
    load_case,
    load_cases,
    parse_case,
    refresh_sidecar,
    validate_catalog_entry,
    write_catalog,
)
from xuebuild.sources import source_spec

PRODUCTION_GRID = GridInfo(1440, 721, -180.0, 90.0, 0.25, -0.25)
CASES_DIRECTORY = Path(__file__).resolve().parent.parent / "showcase" / "cases"


def localized(text: str) -> dict[str, str]:
    """The same text under every locale a case must carry."""
    return {locale: text for locale in LOCALES}


def observation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "demo-observation",
        "title": localized("Demo"),
        "summary": localized("Demo summary"),
        "model": "cma",
        "dataset": "event/series.nc",
        "hours": 24,
        "bbox": [105.0, 14.0, 130.0, 34.0],
        "variables": ["cref"],
    }
    payload.update(overrides)
    return payload


def fetched_observation_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "demo-window",
        "title": localized("Demo"),
        "summary": localized("Demo summary"),
        "model": "mrms",
        "run": "2021082912",
        "hours": 12,
        "bbox": [-95.0, 27.0, -85.0, 33.0],
        "variables": ["cref"],
    }
    payload.update(overrides)
    return payload


def case_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "demo-case",
        "title": localized("Demo"),
        "summary": localized("Demo summary"),
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

    def test_regional_grid_past_the_antimeridian_is_cropped_in_its_own_copy_of_the_world(self) -> None:
        # A Himawari disk on plate carrée: 80.7 to 200.7 at 0.04°, not wrapping.
        disk = GridInfo(3000, 3000, 80.7, 60.0, 0.04, -0.04)
        self.assertFalse(disk.wraps)
        # A box spelled west of -180 lands on the disk's eastern columns...
        east = crop_grid(disk, (-170.0, 0.0, -160.0, 10.0))
        self.assertEqual(east.crop.column_start, 2732)
        self.assertEqual(east.width, 252)
        self.assertEqual(east.first_longitude, -170.02)
        # ...the same box spelled past 180 is the same window...
        self.assertEqual(crop_grid(disk, (190.0, 0.0, 200.0, 10.0)).crop, east.crop)
        # ...a box just west of the origin clamps to it rather than reading
        # as almost a whole world east, and one east of the disk misses it.
        west = crop_grid(disk, (70.0, 0.0, 90.0, 10.0))
        self.assertEqual((west.crop.column_start, west.first_longitude), (0, 80.7))
        with self.assertRaises(ConversionError):
            crop_grid(disk, (-150.0, 0.0, -100.0, 10.0))

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

    def test_a_source_without_wind_publishes_no_wind_bundle(self) -> None:
        self.assertEqual(published_bundle_ids(source_spec("cma")), ("cref",))


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

    def test_requires_every_ui_locale(self) -> None:
        # The UI's eleven, held to the frontend's list by the shared fixture.
        fixture = json.loads((CASES_DIRECTORY.parent.parent / "tests" / "fixtures" / "locales.json").read_text())
        self.assertEqual(list(LOCALES), fixture)
        with self.assertRaisesRegex(ShowcaseError, "title is missing ja"):
            parse_case(case_payload(title={**localized("Demo"), "ja": ""}))
        with self.assertRaises(ShowcaseError):
            parse_case(case_payload(title={"en": "Demo"}))

    def test_keeps_a_locale_beyond_the_required(self) -> None:
        # A case translated further than the UI must reach the catalog intact.
        spec = parse_case(case_payload(title={**localized("Demo"), "pt-BR": "Exemplo"}))
        self.assertEqual(spec.title, {**localized("Demo"), "pt-BR": "Exemplo"})

    def test_rejects_a_key_that_is_not_a_locale_tag(self) -> None:
        with self.assertRaises(ShowcaseError):
            parse_case(case_payload(title={**localized("Demo"), "Japanese": "見本"}))

    def test_rejects_an_empty_extra_locale(self) -> None:
        with self.assertRaises(ShowcaseError):
            parse_case(case_payload(title={**localized("Demo"), "pt-BR": "  "}))

    def test_a_published_row_is_held_only_to_its_first_two_locales(self) -> None:
        # Rows on the bucket predate the wider set; the catalog keeps them.
        entry, _ = build_entry()
        entry["title"] = {"zh": "示例", "en": "Demo"}
        validate_catalog_entry(entry)
        entry["title"] = {"en": "Demo"}
        with self.assertRaises(ShowcaseError):
            validate_catalog_entry(entry)

    def test_rejects_an_sflux_case_with_nothing_at_the_analysis_hour(self) -> None:
        # sflux publishes no PRATE record at f000, so a prate-only case has no
        # variable to key its frames by.
        with self.assertRaises(ShowcaseError):
            parse_case(case_payload(model="sflux", variables=["prate"], defaultVariable="prate"))
        parse_case(case_payload(model="sflux", variables=["prate", "wind10m"], defaultVariable="prate"))

    def test_parses_an_observation_definition(self) -> None:
        spec = parse_case(observation_payload())
        self.assertEqual(spec.model, "cma")
        self.assertEqual(spec.variables, ("cref",))
        # No cycle to fetch: the dataset file says when the series starts.
        self.assertEqual(spec.run, "")
        self.assertEqual(spec.dataset_path.name, "series.nc")

    def test_observation_dataset_resolves_against_the_configured_root(self) -> None:
        spec = parse_case(observation_payload(dataset="event/series.nc"))
        with mock.patch.dict(os.environ, {OBSERVATION_ROOT_ENV: "/data/radar"}):
            self.assertEqual(spec.dataset_path, Path("/data/radar/event/series.nc"))
        absolute = parse_case(observation_payload(dataset="/elsewhere/series.nc"))
        with mock.patch.dict(os.environ, {OBSERVATION_ROOT_ENV: "/data/radar"}):
            self.assertEqual(absolute.dataset_path, Path("/elsewhere/series.nc"))

    def test_run_and_dataset_belong_to_different_kinds_of_case(self) -> None:
        with self.assertRaises(ShowcaseError):
            parse_case(observation_payload(run="2026082516"))
        with self.assertRaises(ShowcaseError):
            parse_case(observation_payload(dataset=""))
        with self.assertRaises(ShowcaseError):
            parse_case(case_payload(dataset="series.nc"))

    def test_an_observation_case_has_no_published_axis_to_land_on(self) -> None:
        # 125 is off every forecast axis; an observation series has none, so
        # any positive hour is a legal declaration until the file is read.
        parse_case(observation_payload(hours=125))
        with self.assertRaises(ShowcaseError):
            parse_case(observation_payload(hours=0))

    def test_parses_a_fetched_observation_definition(self) -> None:
        # MRMS is an observation like the CMA mosaic but fetched from its
        # bucket: a case names the window's first hour as its run, the way a
        # forecast case names a cycle, and no dataset file.
        spec = parse_case(fetched_observation_payload())
        self.assertEqual(spec.model, "mrms")
        self.assertEqual(spec.run, "2021082912")
        self.assertEqual(spec.dataset, "")
        self.assertFalse(spec.from_dataset)
        self.assertTrue(parse_case(observation_payload()).from_dataset)
        self.assertFalse(parse_case(case_payload()).from_dataset)
        with self.assertRaises(ShowcaseError):
            spec.dataset_path

    def test_a_fetched_observation_case_names_a_window_not_a_file(self) -> None:
        with self.assertRaises(ShowcaseError):
            parse_case(fetched_observation_payload(dataset="series.nc"))
        with self.assertRaisesRegex(ShowcaseError, "window's first hour"):
            parse_case(fetched_observation_payload(run=""))
        # Any hour is a cycle on the two-minute mosaic; the run must still be
        # a well-formed hour.
        parse_case(fetched_observation_payload(run="2021082913"))
        with self.assertRaises(XueError):
            parse_case(fetched_observation_payload(run="2021-08-29T12"))

    def test_a_fetched_observation_window_is_any_whole_number_of_hours(self) -> None:
        # 125 is off every forecast axis; a window is as long as it says.
        parse_case(fetched_observation_payload(hours=125))
        with self.assertRaises(ShowcaseError):
            parse_case(fetched_observation_payload(hours=0))

    def test_a_fetched_observation_case_ships_what_the_source_publishes(self) -> None:
        spec = parse_case(fetched_observation_payload(variables=["prate", "cref"]))
        self.assertEqual(spec.variables, ("cref", "prate"))
        with self.assertRaises(ShowcaseError):
            parse_case(fetched_observation_payload(variables=["tmp2m"]))

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


def build_entry(**overrides: object) -> tuple[dict[str, object], dict[str, object]]:
    """A catalog row for the demo case, as if built, plus its manifest."""
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
    entry = build_catalog_entry(
        spec,
        manifest,
        json.dumps(manifest).encode(),
        {"byteLength": 96, "grid": grid.metadata()},
    )
    return entry, manifest


class CatalogEntryTest(unittest.TestCase):
    def test_entry_describes_the_cropped_case(self) -> None:
        entry, _ = build_entry()
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
        entry, _ = build_entry()
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


class RefreshSidecarTest(unittest.TestCase):
    """A built case's row rewritten from its definition, bundles untouched."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-showcase-refresh-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        entry, manifest = build_entry()
        case_dir = self.root / "showcase" / "demo-case"
        case_dir.mkdir(parents=True)
        (case_dir / "manifest.json").write_bytes(json.dumps(manifest).encode())
        (case_dir / "case.json").write_text(json.dumps(entry), encoding="utf-8")
        self.entry = entry

    def test_prose_and_tags_follow_the_definition(self) -> None:
        spec = parse_case(
            case_payload(
                title={**localized("Demo"), "ja": "見本"},
                summary=localized("Corrected"),
                tags=["typhoon"],
                credit="NOAA GFS",
                eventTime="2021-07-20T08:00:00Z",
                defaultVariable="wind10m",
            )
        )
        refreshed = refresh_sidecar(spec, self.root)
        on_disk = json.loads((self.root / "showcase" / "demo-case" / "case.json").read_text(encoding="utf-8"))
        self.assertEqual(refreshed, on_disk)
        self.assertEqual(on_disk["title"]["ja"], "見本")
        self.assertEqual(on_disk["summary"]["en"], "Corrected")
        self.assertEqual((on_disk["tags"], on_disk["credit"], on_disk["eventTime"]), (["typhoon"], "NOAA GFS", "2021-07-20T08:00:00Z"))
        self.assertEqual(on_disk["defaultVariable"], "wind10m")
        # Everything that names the bytes is as built.
        for key in ("manifestCrc32", "byteLength", "bbox", "dataBbox", "grid", "variables", "run", "runTime"):
            self.assertEqual(on_disk[key], self.entry[key])
        # A field the definition dropped leaves the row.
        refresh_sidecar(parse_case(case_payload()), self.root)
        on_disk = json.loads((self.root / "showcase" / "demo-case" / "case.json").read_text(encoding="utf-8"))
        self.assertNotIn("tags", on_disk)
        self.assertNotIn("credit", on_disk)
        # And the catalog collects the refreshed row.
        catalog = collect_catalog(self.root)
        self.assertEqual(catalog["cases"][0]["summary"]["en"], "Demo summary")

    def test_the_catalog_writes_its_stac_face(self) -> None:
        # showcase.json and, beside it, the STAC documents derived from the
        # same rows: one Item per case next to its manifest, the showcase
        # Collection, the root catalog (docs/stac.md).
        write_catalog(self.root)
        item = json.loads((self.root / "showcase" / "demo-case" / "item.json").read_text(encoding="utf-8"))
        collection = json.loads((self.root / "showcase" / "collection.json").read_text(encoding="utf-8"))
        catalog = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(item["id"], "demo-case")
        self.assertEqual(item["bbox"], self.entry["dataBbox"])
        self.assertEqual(item["assets"]["manifest"]["href"], f"manifest.json?v={self.entry['manifestCrc32']}")
        self.assertEqual(
            [link["href"] for link in collection["links"] if link["rel"] == "item"], ["demo-case/item.json"]
        )
        self.assertIn("showcase/collection.json", [link["href"] for link in catalog["links"]])

    def test_a_renamed_source_id_is_carried_onto_the_row(self) -> None:
        # The bytes are identified by the manifest's model string; the
        # shorthand the shell derives from it follows the definition, so a
        # case built under a source's old id refreshes onto the new one.
        stale = {**self.entry, "modelId": "gfs-old"}
        (self.root / "showcase" / "demo-case" / "case.json").write_text(json.dumps(stale), encoding="utf-8")
        refreshed = refresh_sidecar(parse_case(case_payload()), self.root)
        self.assertEqual((refreshed["modelId"], refreshed["model"]), ("gfs", "GFS"))

    def test_a_definition_that_moved_on_needs_a_rebuild(self) -> None:
        for overrides in ({"hours": 48}, {"bbox": [100.0, 20.0, 120.0, 40.0]}, {"variables": ["prate"]}, {"run": "2021071900"}, {"model": "ecmwf"}):
            with self.assertRaisesRegex(ShowcaseError, "rebuild"):
                refresh_sidecar(parse_case(case_payload(**overrides)), self.root)
        with self.assertRaisesRegex(ShowcaseError, "not built"):
            refresh_sidecar(parse_case(case_payload(id="other-case")), self.root)


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

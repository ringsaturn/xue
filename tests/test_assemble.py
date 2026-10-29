"""A run built one bundle group at a time is the run built in one go.

The scheduled publish fans a run out over one job per bundle group and
merges the parts afterwards (`xuebuild.assemble`). That is only sound if a
bundle's bytes do not depend on which other bundles were built beside it and
the merged manifest is the one a monolithic build writes — so this builds
the GRIB fixture both ways and compares every artifact, the manifest and the
live pointer byte for byte. The grouping and the merge are checked on their
own, without a build.
"""

from __future__ import annotations

import filecmp
import json
import unittest
import zlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from tests._support import ClassTempRoot
from xuebuild import assemble, binconvert, stac, zarrstore
from xuebuild.binconvert import published_bundle_ids
from xuebuild.errors import ManifestError
from xuebuild.manifest import build_bin_manifest
from xuebuild.sources import SOURCES, source_spec

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_GRIB = REPOSITORY_ROOT / "tests" / "fixtures" / "gfs.2026081406.f000.crop.grib2"


class BundleGroupTests(unittest.TestCase):
    def test_every_published_bundle_lands_in_exactly_one_group(self) -> None:
        for model, source in SOURCES.items():
            if not source.live:
                continue
            for max_groups in (1, 4, 16, 100):
                with self.subTest(model=model, max_groups=max_groups):
                    groups = assemble.bundle_groups(source, max_groups)
                    self.assertLessEqual(len(groups), max_groups)
                    self.assertEqual(
                        sorted(bundle_id for group in groups for bundle_id in group),
                        sorted(published_bundle_ids(source)),
                    )
                    self.assertTrue(all(group for group in groups))

    def test_groups_keep_publication_order(self) -> None:
        source = source_spec("gfs")
        order = {bundle_id: index for index, bundle_id in enumerate(published_bundle_ids(source))}
        groups = assemble.bundle_groups(source, 8)
        for group in groups:
            self.assertEqual(list(group), sorted(group, key=order.get))
        self.assertEqual(groups, sorted(groups, key=lambda group: order[group[0]]))

    def test_enough_groups_means_one_bundle_each(self) -> None:
        source = source_spec("gfs")
        groups = assemble.bundle_groups(source, len(published_bundle_ids(source)) + 5)
        # Every bundle is its own group, save orog: the fetch reads it at the
        # analysis alone, so it cannot key a run axis and shares a group with
        # the lightest bundle present in every frame.
        mine = list(published_bundle_ids(source))
        anchor = mine.index("tmp2m")
        self.assertEqual(groups, [(b,) for b in mine[:anchor]] + [("tmp2m", "orog")] + [(b,) for b in mine[anchor + 1 :] if b != "orog"])

    def test_the_matrix_flags_the_video_jobs(self) -> None:
        # No source ships the companion; the flag is held on one that would.
        matrix = assemble.bundle_group_matrix(replace(source_spec("gfs"), video=True), 100)
        flagged = {entry["group"]: entry["video"] for entry in matrix}
        self.assertEqual({group for group, video in flagged.items() if video}, {"tmp2m", "prate"})
        self.assertFalse(any(entry["eccodes"] for entry in matrix), "GFS repacks nothing")

    def test_a_source_without_video_flags_no_job(self) -> None:
        # With the H.264 companion off no job installs ffmpeg, and the
        # companion does not weigh on the grouping: tmp2m, prate and dswrf
        # each cost one plane.
        for model in ("sflux", "ecmwf"):
            with self.subTest(model=model):
                source = source_spec(model)
                self.assertFalse(source.video)
                self.assertEqual(binconvert.video_variable_ids(source), frozenset())
                matrix = assemble.bundle_group_matrix(source, 100)
                self.assertFalse(any(entry["video"] for entry in matrix))
        matrix = assemble.bundle_group_matrix(source_spec("sflux"), 100)
        self.assertEqual([entry["slug"] for entry in matrix], ["tmp2m-prate", "dswrf-orog", "wind10m"])
        self.assertFalse(any(entry["eccodes"] for entry in matrix), "sflux repacks nothing")

    def test_a_bundle_absent_from_the_analysis_never_builds_alone(self) -> None:
        # sflux has no PRATE record at f000: a prate-only job fetches nothing
        # for the analysis and the converter has no variable present in
        # every file (the 2026-09-12 00Z publish failed exactly so). prate
        # therefore always shares a job with a bundle present at f000 — the
        # lightest such group, and the lightest such bundle when a top-up
        # asks for prate alone, rebuilt byte-identical beside it.
        source = source_spec("sflux")
        for max_groups in (1, 2, 3, 4, 100):
            with self.subTest(max_groups=max_groups):
                for group in assemble.bundle_groups(source, max_groups):
                    if "prate" in group:
                        self.assertTrue(any(bundle_id != "prate" for bundle_id in group), group)
        self.assertEqual(assemble.bundle_groups(source, 100, ("prate",)), [("tmp2m", "prate")])
        self.assertEqual(assemble.bundle_groups(source, 100, ("prate", "dswrf")), [("prate", "dswrf")])
        # GFS has no analysis-optional input, so nothing changes there.
        gfs = source_spec("gfs")
        self.assertEqual(assemble.bundle_groups(gfs, 100, ("prate",)), [("prate",)])

    def test_the_static_terrain_never_builds_alone(self) -> None:
        # orog is fetched at the analysis alone (`VariableSpec.static`), so a
        # job that builds it alone would fetch one frame and leave the
        # converter with no variable present in every file to key the axis by
        # (the 2026-09-22 06Z aifs publish failed exactly so: it fetched the
        # analysis and then repacked empty frames). It shares a job with the
        # lightest bundle present in every frame, rebuilt byte-identical
        # beside it on a top-up.
        for model in ("gfs", "sflux", "ecmwf", "aifs", "hrrr"):
            with self.subTest(model=model):
                source = source_spec(model)
                for max_groups in (1, 2, 3, 4, 100):
                    for group in assemble.bundle_groups(source, max_groups):
                        if "orog" in group:
                            self.assertTrue(any(bundle_id != "orog" for bundle_id in group), group)
                groups = assemble.bundle_groups(source, 100, ("orog",))
                self.assertEqual(len(groups), 1)
                self.assertEqual(len(groups[0]), 2)
                self.assertIn("orog", groups[0])
        # The companion is the lightest anchored bundle of that source, the
        # first in publication order on a tie: the 2 m temperature, which
        # weighs what sea level pressure does on GFS and HRRR.
        self.assertEqual(assemble.bundle_groups(source_spec("gfs"), 100, ("orog",)), [("tmp2m", "orog")])
        self.assertEqual(assemble.bundle_groups(source_spec("hrrr"), 100, ("orog",)), [("tmp2m", "orog")])
        self.assertEqual(assemble.bundle_groups(source_spec("sflux"), 100, ("orog",)), [("tmp2m", "orog")])

    def test_no_groups_is_refused(self) -> None:
        with self.assertRaises(ManifestError):
            assemble.bundle_groups(source_spec("gfs"), 0)

    def test_the_partial_manifest_name_is_the_group(self) -> None:
        self.assertEqual(
            assemble.partial_manifest_path(Path("run"), ("hgt850", "rh700")),
            Path("run/manifest.part.hgt850-rh700.json"),
        )

    def test_a_subset_is_grouped_on_its_own(self) -> None:
        # A top-up builds only what the live run lacks; the groups keep the
        # publication order and never reach past the subset.
        source = source_spec("gfs")
        groups = assemble.bundle_groups(source, 2, ("wave", "tmpsfc", "wind10m"))
        self.assertEqual(sorted(bundle_id for group in groups for bundle_id in group), ["tmpsfc", "wave", "wind10m"])
        self.assertEqual(len(groups), 2)
        self.assertEqual(assemble.bundle_groups(source, 16, ()), [])
        self.assertEqual(assemble.bundle_group_matrix(source, 16, ()), [])
        with self.assertRaisesRegex(ManifestError, "not \\['sst'\\]"):
            assemble.bundle_groups(source, 16, ("tmpsfc", "sst"))

    def test_the_missing_bundles_are_what_the_live_run_lacks(self) -> None:
        source = source_spec("gfs")
        published = published_bundle_ids(source)
        live = {"bundles": [_entry(bundle_id) for bundle_id in published if bundle_id not in ("htsgw", "prmsl")]}
        self.assertEqual(assemble.missing_bundle_ids(source, live), ("prmsl", "htsgw"))
        self.assertEqual(assemble.missing_bundle_ids(source, {"bundles": [_entry(b) for b in published]}), ())
        # A live entry for a bundle no longer published is not "missing".
        live["bundles"].append(_entry("retired"))
        self.assertEqual(assemble.missing_bundle_ids(source, live), ("prmsl", "htsgw"))
        with self.assertRaises(ManifestError):
            assemble.missing_bundle_ids(source, {"bundles": "tmp2m"})


def _entry(variable: str) -> dict:
    return {"variable": variable, "path": f"{variable}.xue", "byteLength": 10, "crc32": "0badf00d"}


def _part(source, variables: list[str], run_time: datetime | None = None) -> dict:
    return build_bin_manifest(
        run_time or datetime(2026, 8, 14, 6, tzinfo=UTC),
        bundles=[_entry(variable) for variable in variables],
        expected_hours=240,
        model=source.manifest_model,
        product=source.product,
        require_core_variables=False,
    )


class MergePartialManifestTests(unittest.TestCase):
    source = source_spec("sflux")

    def test_parts_merge_in_publication_order(self) -> None:
        merged = assemble.merge_partial_manifests(
            [_part(self.source, ["wind10m", "prate"]), _part(self.source, ["dswrf", "orog"]), _part(self.source, ["tmp2m"])],
            source=self.source,
            expected_hours=240,
        )
        self.assertEqual(
            [bundle["variable"] for bundle in merged["bundles"]], list(published_bundle_ids(self.source))
        )
        self.assertEqual(merged["schemaVersion"], 5)
        self.assertEqual(merged["forecastHours"], 240)

    def test_a_zarr_descriptor_is_carried_through_unchanged(self) -> None:
        """A part built with --zarr names its stores; the merge must keep
        them, on the bundle and on its variants, exactly as written."""
        store = {"path": "tmp2m.zarr", "byteLength": 12_347_075, "crc32": "760cef95"}
        variant_store = {"path": "tmp2m.half.zarr", "byteLength": 4_000_000, "crc32": "0d3a2e6b"}
        with_store = _part(self.source, ["tmp2m"])
        with_store["bundles"][0]["zarr"] = dict(store)
        with_store["bundles"][0]["variants"] = [
            {
                "path": "tmp2m.half.xue",
                "width": 720,
                "height": 361,
                "byteLength": 5,
                "crc32": "0badf00d",
                "bandwidth": 1,
                "zarr": dict(variant_store),
            }
        ]
        merged = assemble.merge_partial_manifests(
            [_part(self.source, ["wind10m", "prate", "dswrf", "orog"]), with_store],
            source=self.source,
            expected_hours=240,
        )
        entry = next(bundle for bundle in merged["bundles"] if bundle["variable"] == "tmp2m")
        self.assertEqual(entry["zarr"], store)
        self.assertEqual(entry["variants"][0]["zarr"], variant_store)
        self.assertTrue(all("zarr" not in bundle for bundle in merged["bundles"] if bundle["variable"] != "tmp2m"))

    def test_a_store_only_part_merges_and_is_weighed_by_its_store(self) -> None:
        """A part whose bundle ships only its Zarr store — no ``.xue`` — is
        merged like any other, and the size report reads the store's bytes
        where the container's are absent."""
        store_only = {
            "variable": "tmp2m",
            "zarr": {"path": "tmp2m.zarr", "byteLength": 700, "crc32": "760cef95"},
            "variants": [
                {
                    "width": 720,
                    "height": 361,
                    "bandwidth": 1,
                    "zarr": {"path": "tmp2m.half.zarr", "byteLength": 300, "crc32": "0d3a2e6b"},
                }
            ],
        }
        part = build_bin_manifest(
            datetime(2026, 8, 14, 6, tzinfo=UTC),
            bundles=[store_only],
            expected_hours=240,
            model=self.source.manifest_model,
            product=self.source.product,
            require_core_variables=False,
        )
        merged = assemble.merge_partial_manifests(
            [_part(self.source, ["wind10m", "prate", "dswrf", "orog"]), part],
            source=self.source,
            expected_hours=240,
        )
        entry = next(bundle for bundle in merged["bundles"] if bundle["variable"] == "tmp2m")
        self.assertNotIn("path", entry)
        self.assertEqual(entry["zarr"]["path"], "tmp2m.zarr")
        self.assertEqual(assemble._delivery_bytes(entry), 700)
        self.assertEqual(assemble._delivery_bytes(entry["variants"][0]), 300)
        self.assertEqual(assemble._delivery_bytes(_entry("prate")), 10)

    def test_a_missing_bundle_is_an_error_not_a_shorter_run(self) -> None:
        with self.assertRaisesRegex(ManifestError, "missing \\['dswrf', 'orog'\\]"):
            assemble.merge_partial_manifests(
                [_part(self.source, ["tmp2m", "prate"]), _part(self.source, ["wind10m"])],
                source=self.source,
                expected_hours=240,
            )

    def test_a_bundle_in_two_parts_is_an_error(self) -> None:
        with self.assertRaisesRegex(ManifestError, "more than one"):
            assemble.merge_partial_manifests(
                [_part(self.source, ["tmp2m", "prate", "dswrf"]), _part(self.source, ["wind10m", "tmp2m"])],
                source=self.source,
                expected_hours=240,
            )

    def test_parts_of_different_runs_are_refused(self) -> None:
        with self.assertRaisesRegex(ManifestError, "disagree on runTime"):
            assemble.merge_partial_manifests(
                [
                    _part(self.source, ["tmp2m", "prate", "dswrf"]),
                    _part(self.source, ["wind10m"], run_time=datetime(2026, 8, 14, 12, tzinfo=UTC)),
                ],
                source=self.source,
                expected_hours=240,
            )

    def test_parts_of_another_model_are_refused(self) -> None:
        with self.assertRaisesRegex(ManifestError, "not GFS"):
            assemble.merge_partial_manifests(
                [_part(self.source, ["tmp2m", "prate", "dswrf", "wind10m"])],
                source=source_spec("gfs"),
                expected_hours=240,
            )

    def test_no_parts_is_an_error(self) -> None:
        with self.assertRaises(ManifestError):
            assemble.merge_partial_manifests([], source=self.source, expected_hours=240)

    def test_parts_top_up_a_live_manifest(self) -> None:
        # The live run lacks dswrf and carries a bundle no longer published;
        # one part brings dswrf and rebuilds prate. The result is the
        # manifest a whole build writes today: the live entries kept, the
        # part's entry winning for prate, the retired one dropped.
        live = _part(self.source, ["tmp2m", "prate", "wind10m", "orog"])
        live["bundles"].append(_entry("retired"))
        live["bundles"][1]["crc32"] = "11111111"
        part = _part(self.source, ["dswrf", "prate"])
        merged = assemble.merge_partial_manifests([part], source=self.source, expected_hours=240, base=live)
        self.assertEqual([bundle["variable"] for bundle in merged["bundles"]], list(published_bundle_ids(self.source)))
        self.assertEqual(merged["bundles"][1]["crc32"], "0badf00d", "the part replaces the live entry")
        whole = assemble.merge_partial_manifests(
            [_part(self.source, ["tmp2m", "prate", "dswrf", "wind10m", "orog"])], source=self.source, expected_hours=240
        )
        self.assertEqual(merged, whole)

    def test_a_top_up_still_needs_every_bundle(self) -> None:
        live = _part(self.source, ["tmp2m", "prate"])
        with self.assertRaisesRegex(ManifestError, "missing \\['orog', 'wind10m'\\]"):
            assemble.merge_partial_manifests([_part(self.source, ["dswrf"])], source=self.source, expected_hours=240, base=live)

    def test_a_top_up_onto_another_run_is_refused(self) -> None:
        live = _part(self.source, ["tmp2m", "prate", "wind10m"], run_time=datetime(2026, 8, 14, 12, tzinfo=UTC))
        with self.assertRaisesRegex(ManifestError, "live manifest and the partial manifests disagree on runTime"):
            assemble.merge_partial_manifests([_part(self.source, ["dswrf"])], source=self.source, expected_hours=240, base=live)


class SplitBuildIdentityTests(ClassTempRoot, unittest.TestCase):
    """The GRIB fixture built whole and built in groups, then assembled."""

    root: Path
    whole: Path
    split: Path

    root_prefix = "xue-assemble-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.source = source_spec("gfs")
        cls.whole = cls.root / "whole"
        cls.split = cls.root / "split"
        run_id = "2026081406"
        run_directory = f"gfs.{run_id}"
        binconvert.convert_bin(
            FIXTURE_GRIB,
            cls.whole / run_directory,
            work_root=cls.root / "whole-work",
            manifest_path=cls.whole / run_directory / "manifest.json",
            latest_path=cls.whole / "latest.json",
            run_id=run_id,
            model="gfs",
            container=True, zarr=False,
        )
        # Four groups, so that vector, derived and video bundles each share a
        # job with scalars — the grouping the workflow would use, not one
        # bundle per job.
        cls.groups = assemble.bundle_groups(cls.source, 4)
        for group in cls.groups:
            binconvert.convert_bin(
                FIXTURE_GRIB,
                cls.split / run_directory,
                work_root=cls.root / f"split-work-{assemble.bundle_group_slug(group)}",
                manifest_path=assemble.partial_manifest_path(cls.split / run_directory, group),
                model="gfs",
                bundle_ids=group,
                container=True, zarr=False,
            )
        # The fixture is one analysis frame; convert_bin's default axis is 120 h.
        cls.report = assemble.assemble_run(cls.split, model="gfs", run_id=run_id, expected_hours=120)
        # The STAC documents the CLI derives after either path has written
        # its manifest (build-bin for the whole run, assemble-run for the
        # pieces): the Item, the source's Collection and the root catalog.
        for root in (cls.whole, cls.split):
            stac.write_run_documents(root, source=cls.source, manifest_path=root / run_directory / "manifest.json")

    def _artifacts(self, root: Path) -> list[Path]:
        return sorted(
            path.relative_to(root)
            for path in root.rglob("*")
            if path.is_file() and not path.name.startswith(assemble.PARTIAL_MANIFEST_PREFIX)
        )

    def test_the_groups_cover_the_run(self) -> None:
        self.assertGreater(len(self.groups), 1)
        self.assertEqual(self.report["bundles"], list(published_bundle_ids(self.source)))

    def test_the_same_files_are_written(self) -> None:
        self.assertEqual(self._artifacts(self.whole), self._artifacts(self.split))

    def test_every_artifact_is_byte_identical(self) -> None:
        """Bundles, variants, posters, companions, the manifest, the pointer
        and the STAC documents derived from the manifest: nothing may
        depend on what else was in the build."""
        for relative in self._artifacts(self.whole):
            with self.subTest(artifact=relative.as_posix()):
                self.assertTrue(
                    filecmp.cmp(self.whole / relative, self.split / relative, shallow=False),
                    f"{relative} differs between the whole build and the assembled one",
                )

    def test_the_pointer_covers_the_assembled_manifest(self) -> None:
        pointer = json.loads((self.split / "latest.json").read_text(encoding="utf-8"))
        whole = json.loads((self.whole / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer, whole)

    def test_the_stac_documents_describe_the_run(self) -> None:
        self.assertIn(Path("gfs.2026081406/item.json"), self._artifacts(self.split))
        self.assertIn(Path("gfs/item.json"), self._artifacts(self.split))
        self.assertIn(Path("gfs/collection.json"), self._artifacts(self.split))
        self.assertIn(Path("catalog.json"), self._artifacts(self.split))
        item = json.loads((self.split / "gfs.2026081406" / "item.json").read_text(encoding="utf-8"))
        manifest = json.loads((self.split / "gfs.2026081406" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(item["assets"]["manifest"]["xue:crc32"], json.loads((self.split / "latest.json").read_text())["manifestCrc32"])
        self.assertTrue({bundle["variable"] for bundle in manifest["bundles"]} <= set(item["assets"]))

    def test_assembling_again_is_a_no_op(self) -> None:
        # Same parts, same manifest: an unforced repeat writes nothing and
        # raises nothing, exactly as a repeated monolithic build behaves.
        before = (self.split / "gfs.2026081406" / "manifest.json").stat().st_mtime_ns
        repeat = assemble.assemble_run(self.split, model="gfs", run_id="2026081406", expected_hours=120)
        self.assertEqual(repeat, self.report)
        self.assertEqual((self.split / "gfs.2026081406" / "manifest.json").stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()


class TopUpIdentityTests(ClassTempRoot, unittest.TestCase):
    """A run published short of some bundles, topped up with only those,
    is the run built whole today — manifest, pointer and artifacts."""

    root: Path

    root_prefix = "xue-topup-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.source = source_spec("gfs")
        run_id = "2026081406"
        cls.run_directory = f"gfs.{run_id}"
        cls.whole = cls.root / "whole"
        cls.topped = cls.root / "topped"
        binconvert.convert_bin(
            FIXTURE_GRIB,
            cls.whole / cls.run_directory,
            work_root=cls.root / "whole-work",
            manifest_path=cls.whole / cls.run_directory / "manifest.json",
            latest_path=cls.whole / "latest.json",
            run_id=run_id,
            model="gfs",
            container=True, zarr=False,
        )
        # The live run: the whole manifest without the ocean set and the wave
        # vector, the way a run published before those bundles existed
        # reads — and carrying a scalar wave direction, which was published
        # for a day before the vector replaced it, so the top-up has an
        # entry to drop as well as bundles to add.
        whole_manifest = json.loads((cls.whole / cls.run_directory / "manifest.json").read_text(encoding="utf-8"))
        cls.ocean = ("tmpsfc", "icec", "icetk", "htsgw", "perpw", "wave")
        live = dict(whole_manifest)
        live["bundles"] = [bundle for bundle in whole_manifest["bundles"] if bundle["variable"] not in cls.ocean]
        stale = json.loads(json.dumps(next(b for b in whole_manifest["bundles"] if b["variable"] == "htsgw")))
        stale["variable"] = "dirpw"
        stale["path"] = "dirpw.xue"
        live["bundles"].append(stale)
        cls.base = cls.root / "live-manifest.json"
        cls.base.write_text(json.dumps(live), encoding="utf-8")
        cls.missing = assemble.missing_bundle_ids(cls.source, live)
        cls.groups = assemble.bundle_groups(cls.source, 2, cls.missing)
        for group in cls.groups:
            binconvert.convert_bin(
                FIXTURE_GRIB,
                cls.topped / cls.run_directory,
                work_root=cls.root / f"topped-work-{assemble.bundle_group_slug(group)}",
                manifest_path=assemble.partial_manifest_path(cls.topped / cls.run_directory, group),
                model="gfs",
                bundle_ids=group,
                container=True, zarr=False,
            )
        cls.report = assemble.assemble_run(
            cls.topped, model="gfs", run_id=run_id, expected_hours=120, base_manifest=cls.base
        )
        for root in (cls.whole, cls.topped):
            stac.write_run_documents(root, source=cls.source, manifest_path=root / cls.run_directory / "manifest.json")

    def test_only_the_missing_bundles_were_built(self) -> None:
        self.assertEqual(self.missing, self.ocean)
        self.assertEqual(sorted(self.report["built"]), sorted(self.ocean))
        self.assertEqual(self.report["dropped"], ["dirpw"])
        self.assertEqual(self.report["bundles"], list(published_bundle_ids(self.source)))
        built = {
            path.name.split(".")[0]
            for path in (self.topped / self.run_directory).iterdir()
            if not path.name.startswith("manifest") and path.name != stac.ITEM_FILENAME
        }
        self.assertEqual(built, set(self.ocean))

    def test_the_manifest_and_pointer_are_the_whole_builds(self) -> None:
        for name in (
            f"{self.run_directory}/manifest.json",
            "latest.json",
            f"{self.run_directory}/item.json",
            "gfs/item.json",
            "gfs/collection.json",
        ):
            with self.subTest(file=name):
                self.assertTrue(filecmp.cmp(self.whole / name, self.topped / name, shallow=False), name)

    def test_the_built_artifacts_are_byte_identical(self) -> None:
        for path in sorted((self.topped / self.run_directory).iterdir()):
            if path.name.startswith(assemble.PARTIAL_MANIFEST_PREFIX):
                continue
            with self.subTest(artifact=path.name):
                self.assertTrue(filecmp.cmp(path, self.whole / self.run_directory / path.name, shallow=False), path.name)


def _store_artifacts(root: Path) -> list[Path]:
    """Every file a build left, but the local parts: a partial manifest and
    a store's partial group document are never published."""
    return sorted(
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file()
        and not path.name.startswith(assemble.PARTIAL_MANIFEST_PREFIX)
        and not path.name.startswith(zarrstore.PART_PREFIX)
    )


class SharedStoreSplitIdentityTests(ClassTempRoot, unittest.TestCase):
    """The GRIB fixture built whole and in groups into the run's shared
    stores: every array, coordinate and group document, the manifest (each
    entry's shared ``crc32`` included) and the pointer come out the same."""

    root: Path
    root_prefix = "xue-assemble-stores-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.source = source_spec("gfs")
        cls.whole = cls.root / "whole"
        cls.split = cls.root / "split"
        run_id = "2026081406"
        cls.run_directory = f"gfs.{run_id}"
        binconvert.convert_bin(
            FIXTURE_GRIB,
            cls.whole / cls.run_directory,
            work_root=cls.root / "whole-work",
            manifest_path=cls.whole / cls.run_directory / "manifest.json",
            latest_path=cls.whole / "latest.json",
            run_id=run_id,
            model="gfs",
        )
        cls.groups = assemble.bundle_groups(cls.source, 4)
        for group in cls.groups:
            binconvert.convert_bin(
                FIXTURE_GRIB,
                cls.split / cls.run_directory,
                work_root=cls.root / f"split-work-{assemble.bundle_group_slug(group)}",
                manifest_path=assemble.partial_manifest_path(cls.split / cls.run_directory, group),
                model="gfs",
                bundle_ids=group,
                partial=True,
            )
        cls.parts_before = sorted(
            path.relative_to(cls.split) for path in (cls.split / cls.run_directory).rglob(f"{zarrstore.PART_PREFIX}*")
        )
        cls.report = assemble.assemble_run(cls.split, model="gfs", run_id=run_id, expected_hours=120)

    def test_a_job_leaves_parts_and_no_group_document(self) -> None:
        self.assertEqual(
            {path.parent.name for path in self.parts_before}, {"bundles.zarr", "bundles.half.zarr"}
        )
        self.assertEqual(
            {path.name for path in self.parts_before if path.parent.name == "bundles.zarr"},
            {f"{zarrstore.PART_PREFIX}{group[0]}.json" for group in self.groups},
        )

    def test_the_whole_build_names_one_store_per_tier(self) -> None:
        manifest = json.loads((self.whole / self.run_directory / "manifest.json").read_text(encoding="utf-8"))
        full = {bundle["zarr"]["path"] for bundle in manifest["bundles"]}
        half = {variant["zarr"]["path"] for bundle in manifest["bundles"] for variant in bundle.get("variants", [])}
        self.assertEqual(full, {"bundles.zarr"})
        self.assertEqual(half, {"bundles.half.zarr"})
        for path in ("bundles.zarr", "bundles.half.zarr"):
            raw = (self.whole / self.run_directory / path / "zarr.json").read_bytes()
            crc32 = f"{zlib.crc32(raw) & 0xFFFFFFFF:08x}"
            named = {
                node["zarr"]["crc32"]
                for bundle in manifest["bundles"]
                for node in (bundle, *bundle.get("variants", []))
                if node["zarr"]["path"] == path
            }
            self.assertEqual(named, {crc32}, path)
            group = json.loads(raw)
            self.assertEqual(list(group["attributes"]["xue_bundles"]), [bundle["variable"] for bundle in manifest["bundles"]])
        self.assertEqual(list((self.whole / self.run_directory / "bundles.zarr").glob(f"{zarrstore.PART_PREFIX}*")), [])

    def test_every_artifact_is_byte_identical(self) -> None:
        whole, split = _store_artifacts(self.whole), _store_artifacts(self.split)
        self.assertEqual(whole, split)
        for relative in whole:
            with self.subTest(artifact=relative.as_posix()):
                self.assertTrue(filecmp.cmp(self.whole / relative, self.split / relative, shallow=False), relative)

    def test_assembling_again_is_a_no_op(self) -> None:
        group = self.split / self.run_directory / "bundles.zarr" / "zarr.json"
        before = (group.stat().st_mtime_ns, (self.split / self.run_directory / "manifest.json").stat().st_mtime_ns)
        repeat = assemble.assemble_run(self.split, model="gfs", run_id="2026081406", expected_hours=120)
        self.assertEqual(repeat, self.report)
        after = (group.stat().st_mtime_ns, (self.split / self.run_directory / "manifest.json").stat().st_mtime_ns)
        self.assertEqual(after, before)


class SharedStoreTopUpIdentityTests(ClassTempRoot, unittest.TestCase):
    """A run published into shared stores short of the ocean bundles, topped
    up with only those onto the live manifest and group documents, is the
    run built whole today: the group documents are rewritten under a new
    CRC, which every entry — the live ones too — carries."""

    root: Path
    root_prefix = "xue-topup-stores-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.source = source_spec("gfs")
        run_id = "2026081406"
        cls.run_directory = f"gfs.{run_id}"
        cls.whole = cls.root / "whole"
        cls.live = cls.root / "live"
        cls.topped = cls.root / "topped"
        binconvert.convert_bin(
            FIXTURE_GRIB,
            cls.whole / cls.run_directory,
            work_root=cls.root / "whole-work",
            manifest_path=cls.whole / cls.run_directory / "manifest.json",
            latest_path=cls.whole / "latest.json",
            run_id=run_id,
            model="gfs",
        )
        cls.ocean = ("tmpsfc", "icec", "icetk", "htsgw", "perpw", "wave")
        live_ids = tuple(bundle_id for bundle_id in published_bundle_ids(cls.source) if bundle_id not in cls.ocean)
        binconvert.convert_bin(
            FIXTURE_GRIB,
            cls.live / cls.run_directory,
            work_root=cls.root / "live-work",
            manifest_path=cls.live / cls.run_directory / "manifest.json",
            model="gfs",
            bundle_ids=live_ids,
        )
        cls.live_manifest = json.loads((cls.live / cls.run_directory / "manifest.json").read_text(encoding="utf-8"))
        cls.missing = assemble.missing_bundle_ids(cls.source, cls.live_manifest)
        for group in assemble.bundle_groups(cls.source, 2, cls.missing):
            binconvert.convert_bin(
                FIXTURE_GRIB,
                cls.topped / cls.run_directory,
                work_root=cls.root / f"topped-work-{assemble.bundle_group_slug(group)}",
                manifest_path=assemble.partial_manifest_path(cls.topped / cls.run_directory, group),
                model="gfs",
                bundle_ids=group,
                partial=True,
            )
        cls.report = assemble.assemble_run(
            cls.topped,
            model="gfs",
            run_id=run_id,
            expected_hours=120,
            base_manifest=cls.live / cls.run_directory / "manifest.json",
            # What `make live-store-groups` fetches: the live group documents
            # at their manifest paths.
            base_stores=cls.live / cls.run_directory,
        )

    def test_only_the_missing_bundles_were_built(self) -> None:
        self.assertEqual(self.missing, self.ocean)
        self.assertEqual(sorted(self.report["built"]), sorted(self.ocean))

    def test_the_manifest_and_group_documents_are_the_whole_builds(self) -> None:
        for name in ("manifest.json", "bundles.zarr/zarr.json", "bundles.half.zarr/zarr.json"):
            with self.subTest(file=name):
                self.assertTrue(
                    filecmp.cmp(self.whole / self.run_directory / name, self.topped / self.run_directory / name, shallow=False),
                    name,
                )
        live_crc = self.live_manifest["bundles"][0]["zarr"]["crc32"]
        topped = json.loads((self.topped / self.run_directory / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotEqual(topped["bundles"][0]["zarr"]["crc32"], live_crc, "a top-up is a new ?v=")

    def test_the_built_arrays_are_byte_identical(self) -> None:
        for relative in _store_artifacts(self.topped / self.run_directory):
            with self.subTest(artifact=relative.as_posix()):
                self.assertTrue(
                    filecmp.cmp(self.topped / self.run_directory / relative, self.whole / self.run_directory / relative, shallow=False),
                    relative,
                )

    def test_a_top_up_without_the_live_group_documents_is_refused(self) -> None:
        with self.assertRaisesRegex(ManifestError, "--base-stores"):
            assemble.assemble_run(
                self.topped,
                model="gfs",
                run_id="2026081406",
                expected_hours=120,
                force=True,
                base_manifest=self.live / self.run_directory / "manifest.json",
            )

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
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from xuebuild import assemble, binconvert
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
        self.assertEqual(groups, [(bundle_id,) for bundle_id in published_bundle_ids(source)])

    def test_the_matrix_flags_the_video_jobs(self) -> None:
        matrix = assemble.bundle_group_matrix(source_spec("sflux"), 100)
        self.assertEqual(
            {entry["group"]: entry["video"] for entry in matrix},
            {"tmp2m": True, "prate": True, "dswrf": True, "wind10m": False},
        )
        self.assertEqual([entry["slug"] for entry in matrix], ["tmp2m", "prate", "dswrf", "wind10m"])

    def test_no_groups_is_refused(self) -> None:
        with self.assertRaises(ManifestError):
            assemble.bundle_groups(source_spec("gfs"), 0)

    def test_the_partial_manifest_name_is_the_group(self) -> None:
        self.assertEqual(
            assemble.partial_manifest_path(Path("run"), ("hgt850", "rh700")),
            Path("run/manifest.part.hgt850-rh700.json"),
        )


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
            [_part(self.source, ["wind10m", "prate"]), _part(self.source, ["dswrf"]), _part(self.source, ["tmp2m"])],
            source=self.source,
            expected_hours=240,
        )
        self.assertEqual(
            [bundle["variable"] for bundle in merged["bundles"]], list(published_bundle_ids(self.source))
        )
        self.assertEqual(merged["schemaVersion"], 5)
        self.assertEqual(merged["forecastHours"], 240)

    def test_a_missing_bundle_is_an_error_not_a_shorter_run(self) -> None:
        with self.assertRaisesRegex(ManifestError, "missing \\['dswrf'\\]"):
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


class SplitBuildIdentityTests(unittest.TestCase):
    """The GRIB fixture built whole and built in groups, then assembled."""

    root: Path
    whole: Path
    split: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-assemble-"))
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
            )
        # The fixture is one analysis frame; convert_bin's default axis is 120 h.
        cls.report = assemble.assemble_run(cls.split, model="gfs", run_id=run_id, expected_hours=120)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

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
        """Bundles, variants, posters, companions, the manifest and the
        pointer: nothing may depend on what else was in the build."""
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

    def test_assembling_again_is_a_no_op(self) -> None:
        # Same parts, same manifest: an unforced repeat writes nothing and
        # raises nothing, exactly as a repeated monolithic build behaves.
        before = (self.split / "gfs.2026081406" / "manifest.json").stat().st_mtime_ns
        repeat = assemble.assemble_run(self.split, model="gfs", run_id="2026081406", expected_hours=120)
        self.assertEqual(repeat, self.report)
        self.assertEqual((self.split / "gfs.2026081406" / "manifest.json").stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()

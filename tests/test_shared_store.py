"""The shared store (docs/zarr-profile.md, profile 2): every bundle of one
resolution tier of a run in one ``bundles[.<tier>].zarr``.

The writer is held to its byte rules (key orders, coordinates written once,
the group document's CRC on every bundle), a split write to a whole one, the
merge to its refusals, the readers to both profiles, the encoder's dimension
rule to the source registry, and the manifest validators' one exception to
the duplicate-path rule to exactly the shared store names.
"""

from __future__ import annotations

import filecmp
import json
import unittest
import zlib
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from tests._support import TempRoot
from xuebuild import zarrstore
from xuebuild.binconvert import GridInfo, build_metadata, store_dimensions
from xuebuild.errors import BundleError, ManifestError
from xuebuild.manifest import is_shared_store_path, validate_bin_manifest
from xuebuild.sources import source_spec

GRID = GridInfo(
    width=17,
    height=9,
    first_longitude=-180.0,
    first_latitude=90.0,
    longitude_step=21.0,
    latitude_step=-20.0,
)
TILE = (5, 4)
RUN_TIME = datetime(2026, 10, 20, 0, tzinfo=UTC)
HOURS = list(range(8))
PRIMARY = ("time", "latitude", "longitude")


def plane(hour: int, salt: int) -> np.ndarray:
    return ((np.arange(GRID.width * GRID.height) * (salt + 3) + hour * 7) % 200).astype(np.uint8)


def member(bundle_id: str, variable_ids: tuple[str, ...], hours: list[int], dimensions=PRIMARY, *, salt: int = 0):
    metadata = build_metadata(RUN_TIME, hours, GRID, "balanced", variable_ids)
    planes = {
        variable_id: [plane(hour, salt + index) for hour in hours] for index, variable_id in enumerate(variable_ids)
    }
    predictors = {variable_id: "raw" if variable_id == "prate" else "previous" for variable_id in variable_ids}
    return zarrstore.SharedMember(
        bundle_id, zarrstore.CodesSource(metadata, planes, predictors, TILE), tuple(dimensions)
    )


def members(salt: int = 0) -> list[zarrstore.SharedMember]:
    """A scalar on the run's axis, the rate on an axis of its own (no
    analysis frame), and a two-variable vector."""
    return [
        member("tmp2m", ("tmp2m",), HOURS, salt=salt),
        member("prate", ("prate",), HOURS[1:], ("time_prate", "latitude", "longitude"), salt=salt),
        member("wind10m", ("ugrd10m", "vgrd10m"), HOURS, salt=salt),
    ]


def tree(root: Path) -> list[Path]:
    return sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file())


class StoreDimensionsTest(unittest.TestCase):
    """`binconvert.store_dimensions`, the rule both encoders apply."""

    def test_a_bundle_on_the_run_axis_and_grid_takes_the_primary_names(self) -> None:
        for model, bundle_id in (("gfs", "tmp2m"), ("gfs", "prate"), ("gfs", "wind10m"), ("ecmwf", "tmp2m")):
            with self.subTest(model=model, bundle=bundle_id):
                self.assertEqual(store_dimensions(source_spec(model), bundle_id), PRIMARY)

    def test_an_axis_cut_by_a_rule_takes_its_own_time(self) -> None:
        # The de-accumulated / de-averaged rate and the interval maximum
        # have no analysis frame; the static terrain has only that one.
        for model, bundle_id in (("ecmwf", "prate"), ("ecmwf", "gust"), ("sflux", "prate"), ("gfs", "orog")):
            with self.subTest(model=model, bundle=bundle_id):
                self.assertEqual(
                    store_dimensions(source_spec(model), bundle_id), (f"time_{bundle_id}", "latitude", "longitude")
                )

    def test_a_grid_family_of_its_own_takes_the_familys_grid(self) -> None:
        # Named by the family (`bundle_grid_family`), so every bundle of the
        # CFSv2 pressure levels shares one pair.
        cfs = source_spec("cfs")
        self.assertEqual(store_dimensions(cfs, "hgt500"), ("time", "latitude_pgb", "longitude_pgb"))
        self.assertEqual(store_dimensions(cfs, "wind850"), ("time", "latitude_pgb", "longitude_pgb"))
        self.assertEqual(store_dimensions(cfs, "tmp2m"), PRIMARY)

    def test_every_source_names_valid_coordinates(self) -> None:
        from xuebuild.binconvert import published_bundle_ids
        from xuebuild.sources import SOURCES

        for model, source in SOURCES.items():
            for bundle_id in published_bundle_ids(source):
                with self.subTest(model=model, bundle=bundle_id):
                    zarrstore._check_dimensions(bundle_id, store_dimensions(source, bundle_id))

    def test_the_rule_is_a_pure_function_of_the_source(self) -> None:
        source = source_spec("ecmwf")
        self.assertEqual(store_dimensions(source, "prate"), store_dimensions(source_spec("ecmwf"), "prate"))


class SharedStoreWriterTest(TempRoot, unittest.TestCase):
    def test_the_group_document_spells_the_profile(self) -> None:
        store = self.root / "bundles.zarr"
        written = zarrstore.write_shared_store(store, members())
        raw = (store / "zarr.json").read_bytes()
        group = json.loads(raw)
        self.assertEqual(list(group), ["zarr_format", "node_type", "attributes", "consolidated_metadata"])
        self.assertEqual(list(group["attributes"]), ["xue_profile", "xue_bundles"])
        self.assertEqual(group["attributes"]["xue_profile"], 2)
        self.assertEqual(list(group["attributes"]["xue_bundles"]), ["tmp2m", "prate", "wind10m"])
        self.assertNotIn("xue", group["attributes"])
        self.assertEqual(
            list(group["consolidated_metadata"]["metadata"]),
            ["tmp2m", "prate", "ugrd10m", "vgrd10m", "time", "latitude", "longitude", "time_prate"],
        )
        # The same serializer as profile 1: two-space indent, raw UTF-8, a newline.
        self.assertEqual(raw, (json.dumps(group, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        crc32 = f"{zlib.crc32(raw) & 0xFFFFFFFF:08x}"
        self.assertEqual({bundle.crc32 for bundle in written.values()}, {crc32})
        self.assertEqual(zarrstore.find_parts(store), [], "a whole write leaves no part")

    def test_arrays_name_their_bundle_and_dimensions(self) -> None:
        store = self.root / "bundles.zarr"
        zarrstore.write_shared_store(store, members())
        for name, bundle_id, dimensions in (
            ("tmp2m", "tmp2m", PRIMARY),
            ("prate", "prate", ["time_prate", "latitude", "longitude"]),
            ("vgrd10m", "wind10m", PRIMARY),
        ):
            with self.subTest(array=name):
                document = zarrstore.read_json(store / name / "zarr.json")
                self.assertEqual(list(document["attributes"]["xue"]), ["bundle", "variable", "predictor"])
                self.assertEqual(document["attributes"]["xue"]["bundle"], bundle_id)
                self.assertEqual(document["dimension_names"], list(dimensions))
        self.assertEqual(zarrstore.read_json(store / "time_prate" / "zarr.json")["shape"], [len(HOURS) - 1])
        self.assertEqual(zarrstore.read_json(store / "time" / "zarr.json")["shape"], [len(HOURS)])
        self.assertEqual(
            np.frombuffer((store / "time_prate" / "c" / "0").read_bytes(), dtype="<i4").tolist(),
            [hour * 3600 for hour in HOURS[1:]],
        )

    def test_a_bundles_byte_length_is_its_arrays_and_coordinates(self) -> None:
        store = self.root / "bundles.zarr"
        written = zarrstore.write_shared_store(store, members())

        def size(*names: str) -> int:
            return sum(path.stat().st_size for name in names for path in (store / name).rglob("*") if path.is_file())

        self.assertEqual(written["tmp2m"].byte_length, size("tmp2m", "time", "latitude", "longitude"))
        self.assertEqual(written["prate"].byte_length, size("prate", "time_prate", "latitude", "longitude"))
        self.assertEqual(
            written["wind10m"].byte_length, size("ugrd10m", "vgrd10m", "time", "latitude", "longitude")
        )

    def test_writing_twice_is_byte_identical(self) -> None:
        first, second = self.root / "a" / "bundles.zarr", self.root / "b" / "bundles.zarr"
        zarrstore.write_shared_store(first, members())
        zarrstore.write_shared_store(second, members())
        self.assertEqual(tree(first), tree(second))
        for relative in tree(first):
            self.assertTrue(filecmp.cmp(first / relative, second / relative, shallow=False), relative)

    def test_a_split_write_is_the_whole_write(self) -> None:
        whole = self.root / "whole" / "bundles.zarr"
        zarrstore.write_shared_store(whole, members())
        split = self.root / "split" / "bundles.zarr"
        jobs = [members()[:1], members()[1:]]
        for job in jobs:
            reports = [
                zarrstore.write_bundle_arrays(item.source, split, item.bundle_id, item.dimensions) for item in job
            ]
            path, crc32 = zarrstore.write_part(split, reports)
            self.assertEqual(path.name, f"zarr.part.{job[0].bundle_id}.json")
            self.assertEqual(crc32, f"{zlib.crc32(path.read_bytes()) & 0xFFFFFFFF:08x}")
        crc32 = zarrstore.finalize_store(split, ["tmp2m", "prate", "wind10m"])
        self.assertEqual(crc32, f"{zlib.crc32((whole / 'zarr.json').read_bytes()) & 0xFFFFFFFF:08x}")
        self.assertEqual(len(zarrstore.find_parts(split)), 2, "assembly keeps the parts so it can run again")
        self.assertEqual(zarrstore.finalize_store(split, ["tmp2m", "prate", "wind10m"]), crc32)
        for part in zarrstore.find_parts(split):
            part.unlink()
        self.assertEqual(tree(whole), tree(split))
        for relative in tree(whole):
            self.assertTrue(filecmp.cmp(whole / relative, split / relative, shallow=False), relative)

    def test_a_top_up_merges_onto_the_live_group(self) -> None:
        whole = self.root / "whole" / "bundles.zarr"
        zarrstore.write_shared_store(whole, members())
        live = self.root / "live" / "bundles.zarr"
        zarrstore.write_shared_store(live, [members()[0], members()[2]])
        topped = self.root / "topped" / "bundles.zarr"
        prate = members()[1]
        zarrstore.write_part(topped, [zarrstore.write_bundle_arrays(prate.source, topped, "prate", prate.dimensions)])
        zarrstore.finalize_store(topped, ["tmp2m", "prate", "wind10m"], base=zarrstore.read_json(live / "zarr.json"))
        self.assertEqual((topped / "zarr.json").read_bytes(), (whole / "zarr.json").read_bytes())

    def test_the_merge_refuses_what_does_not_add_up(self) -> None:
        def part(*items: zarrstore.SharedMember) -> dict:
            store = self.root / f"part-{'-'.join(item.bundle_id for item in items)}"
            reports = [zarrstore.write_bundle_arrays(item.source, store, item.bundle_id, item.dimensions) for item in items]
            return zarrstore.group_document([report.entry() for report in reports])

        tmp2m, prate, wind = members()
        order = ["tmp2m", "prate", "wind10m"]
        with self.assertRaisesRegex(BundleError, "more than one"):
            zarrstore.merge_group_documents(order, [part(tmp2m, prate), part(prate, wind)])
        with self.assertRaisesRegex(BundleError, "carries bundle wind10m"):
            zarrstore.merge_group_documents(order, [part(tmp2m, prate)])
        with self.assertRaisesRegex(BundleError, "does not name"):
            zarrstore.merge_group_documents(["tmp2m"], [part(tmp2m, prate)])
        # Two jobs that disagree about the run's axis cannot share `time`.
        short = member("wind10m", ("ugrd10m", "vgrd10m"), HOURS[:-1])
        with self.assertRaisesRegex(BundleError, "coordinate time differs"):
            zarrstore.merge_group_documents(["tmp2m", "wind10m"], [part(tmp2m), part(short)])

    def test_bundles_of_one_grid_family_share_its_coordinates(self) -> None:
        store = self.root / "bundles.zarr"
        family = ("time", "latitude_pgb", "longitude_pgb")
        zarrstore.write_shared_store(
            store,
            [
                members()[0],
                member("hgt500", ("hgt500",), HOURS, family),
                member("tmp500", ("tmp500",), HOURS, family),
            ],
        )
        group = zarrstore.read_json(store / "zarr.json")
        self.assertEqual(
            list(group["consolidated_metadata"]["metadata"]),
            ["tmp2m", "hgt500", "tmp500", "time", "latitude", "longitude", "latitude_pgb", "longitude_pgb"],
        )
        self.assertEqual(zarrstore.array_dimensions(store, "tmp500"), family)

    def test_bad_names_are_refused(self) -> None:
        tmp2m = members()[0]
        store = self.root / "bundles.zarr"
        with self.assertRaises(BundleError):
            zarrstore.write_bundle_arrays(tmp2m.source, store, "bundles")
        for dimensions in (
            ("time_prate", "latitude", "longitude"),
            ("t", "latitude", "longitude"),
            ("time", "latitude"),
            ("time", "latitude_pgb", "longitude"),
            ("time", "latitude_pgb", "longitude_x"),
            ("time", "latitude_Pgb", "longitude_Pgb"),
        ):
            with self.subTest(dimensions=dimensions), self.assertRaises(BundleError):
                zarrstore.write_bundle_arrays(tmp2m.source, store, "tmp2m", dimensions)


class SharedStoreReaderTest(TempRoot, unittest.TestCase):
    def test_a_bundle_is_opened_by_store_and_id(self) -> None:
        store = self.root / "bundles.zarr"
        zarrstore.write_shared_store(store, members())
        metadata = zarrstore.bundle_metadata(store, "prate")
        self.assertEqual([variable["id"] for variable in metadata["variables"]], ["prate"])
        self.assertEqual(metadata["time"]["frameCount"], len(HOURS) - 1)
        with self.assertRaisesRegex(BundleError, "name the bundle"):
            zarrstore.bundle_metadata(store)
        with self.assertRaisesRegex(BundleError, "no bundle gust"):
            zarrstore.bundle_metadata(store, "gust")
        self.assertEqual(zarrstore.array_dimensions(store, "prate"), ("time_prate", "latitude", "longitude"))

    def test_the_codes_read_back_through_either_profile(self) -> None:
        shared = self.root / "bundles.zarr"
        zarrstore.write_shared_store(shared, members())
        single = self.root / "tmp2m.zarr"
        zarrstore.write_store(members()[0].source, single)
        self.assertEqual(zarrstore.bundle_metadata(single)["variables"][0]["id"], "tmp2m")
        self.assertEqual(zarrstore.bundle_metadata(single, "tmp2m"), zarrstore.bundle_metadata(shared, "tmp2m"))
        for got, want, hour in zip(zarrstore.read_frames(shared, "tmp2m"), zarrstore.read_frames(single, "tmp2m"), HOURS):
            np.testing.assert_array_equal(got, want)
            np.testing.assert_array_equal(got.ravel(), plane(hour, 0))
        np.testing.assert_array_equal(zarrstore.read_plane(shared, "prate", 0).ravel(), plane(HOURS[1], 0))

    def test_a_job_reads_its_bundle_through_its_part(self) -> None:
        store = self.root / "bundles.zarr"
        prate = members()[1]
        zarrstore.write_part(store, [zarrstore.write_bundle_arrays(prate.source, store, "prate", prate.dimensions)])
        self.assertEqual(zarrstore.bundle_metadata(store, "prate")["time"]["frameCount"], len(HOURS) - 1)

    def test_a_malformed_group_is_refused(self) -> None:
        store = self.root / "bundles.zarr"
        zarrstore.write_shared_store(store, members())
        group = zarrstore.read_json(store / "zarr.json")
        cases = {
            "profile 3": {**group, "attributes": {**group["attributes"], "xue_profile": 3}},
            "empty bundles": {**group, "attributes": {"xue_profile": 2, "xue_bundles": {}}},
            "bundles not an object": {**group, "attributes": {"xue_profile": 2, "xue_bundles": []}},
            "no bundles": {**group, "attributes": {"xue_profile": 2}},
        }
        for label, document in cases.items():
            with self.subTest(label):
                (store / "zarr.json").write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaises(BundleError):
                    zarrstore.bundle_metadata(store, "tmp2m")


class SharedStorePathTest(unittest.TestCase):
    def test_exactly_the_shared_names_are_shared(self) -> None:
        for path in ("bundles.zarr", "bundles.half.zarr", "bundles.eighth.zarr", "bundles.other.zarr",
                     "../../goeseast.segments/202610112200/0a1b2c3d/bundles.half.zarr"):
            with self.subTest(path=path):
                self.assertTrue(is_shared_store_path(path))
                self.assertTrue(zarrstore.is_shared_store_path(path))
        for path in ("bundles.series.zarr", "tmp2m.zarr", "Bundles.zarr", "bundles.half2.zarr", "bundles..zarr",
                     "xbundles.zarr", "bundles.zarr/x", "bundles.half.xue"):
            with self.subTest(path=path):
                self.assertFalse(is_shared_store_path(path))
                self.assertFalse(zarrstore.is_shared_store_path(path))


def _manifest(*bundles: dict) -> dict:
    return {
        "schemaVersion": 5,
        "model": "GFS",
        "product": "pgrb2.0p25",
        "runTime": "2026-10-20T00:00:00Z",
        "forecastHours": 4,
        "bundles": list(bundles),
    }


def _store(path: str, byte_length: int = 7) -> dict:
    return {"path": path, "byteLength": byte_length, "crc32": "deadbeef"}


class SharedManifestTest(unittest.TestCase):
    def test_entries_of_one_tier_share_its_store(self) -> None:
        half = {"width": 9, "height": 5, "bandwidth": 1}
        validate_bin_manifest(
            _manifest(
                {"variable": "tmp2m", "zarr": _store("bundles.zarr"), "variants": [{**half, "zarr": _store("bundles.half.zarr")}]},
                {"variable": "prate", "zarr": _store("bundles.zarr", 5), "variants": [{**half, "zarr": _store("bundles.half.zarr")}]},
            ),
            expected_hours=4,
        )

    def test_shared_segments_are_shared(self) -> None:
        path = "../../gfs.segments/202610112200/0a1b2c3d/bundles.zarr"
        validate_bin_manifest(
            _manifest(
                {"variable": "tmp2m", "zarr": {"byteLength": 7, "segments": [_store(path)]}},
                {"variable": "prate", "zarr": {"byteLength": 3, "segments": [_store(path, 3)]}},
            ),
            expected_hours=4,
        )

    def test_every_other_path_is_still_unique(self) -> None:
        cases = {
            "one-bundle store": ({"zarr": _store("tmp2m.zarr")}, {"zarr": _store("tmp2m.zarr")}),
            "series named like a shared store": (
                {"zarr": _store("bundles.zarr"), "series": _store("bundles.series.zarr")},
                {"zarr": _store("bundles.zarr"), "series": _store("bundles.series.zarr")},
            ),
            "not a tier": ({"zarr": _store("bundles.half2.zarr")}, {"zarr": _store("bundles.half2.zarr")}),
            "container": (
                {"path": "bundles.xue", "byteLength": 1, "crc32": "deadbeef"},
                {"path": "bundles.xue", "byteLength": 1, "crc32": "deadbeef"},
            ),
        }
        for label, (first, second) in cases.items():
            with self.subTest(label), self.assertRaisesRegex(ManifestError, "duplicate bundle paths"):
                validate_bin_manifest(
                    _manifest({"variable": "tmp2m", **first}, {"variable": "prate", **second}), expected_hours=4
                )


class SharedStoreAssetsTest(unittest.TestCase):
    def test_every_bundle_keeps_its_asset_and_names_itself(self) -> None:
        from xuebuild.stac import manifest_assets

        def entry(variable: str, size: int) -> dict:
            return {
                "variable": variable,
                "zarr": {"path": "bundles.zarr", "byteLength": size, "crc32": "760cef95"},
                "variants": [
                    {"width": 9, "height": 5, "bandwidth": 1,
                     "zarr": {"path": "bundles.half.zarr", "byteLength": size // 4, "crc32": "0d3a2e6b"}}
                ],
                "series": {"path": f"{variable}.series.zarr", "byteLength": 3, "crc32": "feedface"},
            }

        assets = manifest_assets({"bundles": [entry("tmp2m", 400), entry("prate", 200)]}, "01234567")
        for bundle_id, size in (("tmp2m", 400), ("prate", 200)):
            with self.subTest(bundle=bundle_id):
                self.assertEqual(assets[bundle_id]["href"], "bundles.zarr")
                self.assertEqual(assets[bundle_id]["xue:bundle"], bundle_id)
                self.assertEqual(assets[bundle_id]["file:size"], size)
                self.assertEqual(assets[f"{bundle_id}-half"]["href"], "bundles.half.zarr")
                self.assertEqual(assets[f"{bundle_id}-half"]["xue:tier"], "half")
                self.assertEqual(assets[f"{bundle_id}-half"]["xue:bundle"], bundle_id)
                self.assertNotIn("xue:bundle", assets[f"{bundle_id}-series"], "a series store is its bundle's own")


if __name__ == "__main__":
    unittest.main()

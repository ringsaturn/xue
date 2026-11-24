"""The column store (docs/zarr-profile.md, "Column store"): the plan, the
reference writer held to the codes of the map store it cuts and to the
committed golden, the reader's refusals, the build that attaches a store to a
run, and the manifest's `columns`.

The native writer is held to this one by ``tests/test_columns_native.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import numpy as np

from tests import prepare_column_golden as golden
from tests._support import ClassTempRoot, TempRoot, assert_trees_identical
from tests.jsonschemas import errors as schema_errors
from xuebuild import columnbuild, columns, encoder, native, registry, zarrcodec, zarrstore, zstdcli
from xuebuild.cli import main
from xuebuild.columnarrays import LEVELLED_QUANTITIES, ColumnArraySpec, column_arrays, column_bundle_ids
from xuebuild.columns import ColumnGeometry, ColumnStore, ColumnStoreError
from xuebuild.discover import published_bundle_ids
from xuebuild.errors import ConversionError, ManifestError, XueError
from xuebuild.manifest import validate_bin_manifest, write_pointer_to_manifest
from xuebuild.river import network
from xuebuild.sources import SOURCES, source_spec
from xuebuild.stac import manifest_assets
from xuebuild.variables import variable_spec

FIXTURE = golden.FIXTURES
STORE = FIXTURE / columns.STORE_NAME
RUN_ID = f"{golden.RUN_TIME:%Y%m%d%H}"
HOURS = {variable_id: axis for _, variable_ids, axis in golden.BUNDLES for variable_id in variable_ids}
COLUMN_ARRAYS = tuple(ColumnArraySpec(array_id, members) for array_id, _, members in golden.ARRAYS)
NEVER_WRITTEN = (2**64 - 1, 2**64 - 1)


def same_zstd() -> bool:
    recorded = json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))["zstdVersion"]
    return ".".join(map(str, zstdcli.zstd_version()[:3])) == recorded


def cube(variable_id: str) -> np.ndarray:
    """What the map store holds of a variable, ``[frames, height, width]``."""
    return np.stack([golden.codes(variable_id, hour) for hour in HOURS[variable_id]])


def read_array(store: ColumnStore, array_id: str) -> np.ndarray:
    """A whole array, ``[frames, levels, height, width]``, block by block."""
    geometry = store.geometry(array_id)
    out = np.empty((geometry.frame_count, geometry.level_count, geometry.height, geometry.width), dtype=np.uint8)
    for row in range(0, geometry.height, geometry.block_height):
        for column in range(0, geometry.width, geometry.block_width):
            block = store.block_codes(array_id, row, column)
            height, width = min(geometry.block_height, geometry.height - row), min(geometry.block_width, geometry.width - column)
            out[:, :, row : row + height, column : column + width] = block[:, :, :height, :width]
    return out


def edit_json(path: Path, change: Any) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(json.dumps(document), encoding="utf-8")


class GeometryTests(unittest.TestCase):
    def test_the_global_grid(self) -> None:
        geometry = ColumnGeometry(161, 15, 721, 1440)
        self.assertEqual(geometry.inner_shape, (161, 15, 8, 8))
        self.assertEqual(geometry.shard_shape, (161, 15, 96, 1440))
        self.assertEqual((geometry.shard_count, geometry.chunks_per_shard, geometry.index_bytes), (8, 2160, 34564))
        self.assertEqual(geometry.shard_path(7), "c/0/0/7/0")
        self.assertEqual(ColumnGeometry(161, None, 721, 1440).shard_path(7), "c/0/7/0")
        self.assertEqual(geometry.shard_rows(7), range(672, 721))
        self.assertEqual(geometry.locate(720, 1439), (7, 6 * 180 + 179))

    def test_the_document_is_the_authority(self) -> None:
        document = zarrstore.read_json(STORE / "tmp" / "zarr.json")
        geometry = ColumnGeometry.from_document(document, "tmp")
        self.assertEqual((geometry.shape, geometry.levels), ((7, 3, 100, 41), 3))
        self.assertEqual((geometry.shard_height, geometry.shard_width), (96, 48))
        other = json.loads(json.dumps(document))
        other["chunk_grid"]["configuration"]["chunk_shape"] = [7, 3, 16, 64]
        other["codecs"][0]["configuration"]["chunk_shape"] = [7, 3, 4, 16]
        other = ColumnGeometry.from_document(other, "tmp")
        self.assertEqual((other.block_rows, other.block_columns, other.shard_count), (4, 4, 7))

    def test_bad_documents_are_refused(self) -> None:
        document = zarrstore.read_json(STORE / "tmp" / "zarr.json")
        for outer, inner in (
            ([7, 3, 92, 48], [7, 3, 8, 8]),
            ([7, 3, 96, 40], [7, 3, 8, 8]),
            ([7, 3, 96, 44], [7, 3, 8, 8]),
            ([14, 3, 96, 48], [7, 3, 8, 8]),
            ([7, 3, 96, 48], [7, 1, 8, 8]),
            ([7, 3, 96, 48], [7, 3, 0, 8]),
            ([7, 3, 96, 48], [3, 8, 8]),
            ([7, 3, 96, True], [7, 3, 8, 8]),
        ):
            with self.subTest(outer=outer, inner=inner):
                broken = json.loads(json.dumps(document))
                broken["chunk_grid"]["configuration"]["chunk_shape"] = outer
                broken["codecs"][0]["configuration"]["chunk_shape"] = inner
                with self.assertRaises(ColumnStoreError):
                    ColumnGeometry.from_document(broken, "tmp")
        with self.assertRaises(ColumnStoreError):
            ColumnGeometry(1, 1, 8, 8).locate(8, 0)


class GoldenTests(TempRoot, unittest.TestCase):
    """The committed fixture, which the Rust and web readers read too."""

    def test_every_cell_holds_the_map_stores_codes(self) -> None:
        store = ColumnStore(STORE)
        self.assertEqual(store.array_ids, [array_id for array_id, _, _ in golden.ARRAYS])
        for array_id, _, members in golden.ARRAYS:
            codes = read_array(store, array_id)
            for level, (_, variable_id) in enumerate(members):
                with self.subTest(array=array_id, variable=variable_id):
                    np.testing.assert_array_equal(codes[:, level], cube(variable_id))
                    frames = zarrstore.read_frames(FIXTURE / "map" / "bundles.zarr", variable_id)
                    np.testing.assert_array_equal(codes[:, level], np.stack(frames))

    def test_the_expected_cells(self) -> None:
        store = ColumnStore(STORE)
        for cell in json.loads((FIXTURE / "expected.json").read_text(encoding="utf-8"))["cells"]:
            for array_id, codes in cell["arrays"].items():
                with self.subTest(cell=(cell["row"], cell["column"]), array=array_id):
                    self.assertEqual(store.column(array_id, cell["row"], cell["column"]).tolist(), codes)

    def test_the_generator_still_writes_it(self) -> None:
        if not same_zstd():
            raise unittest.SkipTest("the fixture was compressed with another libzstd")
        golden.prepare(self.root / "column")
        assert_trees_identical(self, FIXTURE, self.root / "column", ["columns.zarr/tmp/c/0/0/1/0", "columns.zarr/orog/c/0/1/0"])

    def test_the_plan_is_the_committed_one(self) -> None:
        self.assertEqual(json.loads((FIXTURE / "plan.json").read_text(encoding="utf-8")), golden.plan())

    def test_the_documents_match_the_schemas(self) -> None:
        group = json.loads((STORE / "zarr.json").read_text(encoding="utf-8"))
        self.assertEqual(schema_errors("column-group", group), [])
        for path in STORE.glob("*/zarr.json"):
            document = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(schema_errors("store-array", document), [], path)
            self.assertEqual(group["consolidated_metadata"]["metadata"][path.parent.name], document)

    def test_the_group_document(self) -> None:
        raw = (STORE / "zarr.json").read_bytes()
        group = json.loads(raw)
        self.assertEqual(raw, zarrstore._dump_json(group))
        self.assertEqual(list(group), ["zarr_format", "node_type", "attributes", "consolidated_metadata"])
        self.assertEqual(list(group["attributes"]), ["xue_columns"])
        block = group["attributes"]["xue_columns"]
        self.assertEqual(list(block), ["version", "model", "product", "runTime", "profile", "time", "grid", "arrays"])
        metadata = zarrstore.bundle_metadata(FIXTURE / "map" / "bundles.zarr", "tmp850")
        for key in ("model", "product", "runTime", "profile", "time", "grid"):
            self.assertEqual(block[key], metadata[key])
        self.assertEqual(
            list(group["consolidated_metadata"]["metadata"]),
            ["tmp", "hgt", "ugrd", "vgrd", "tmp2m", "orog", "prate", "time", "level", "latitude", "longitude",
             "level_hgt", "level_ugrd", "level_vgrd", "time_orog", "time_prate"],
        )
        arrays = {entry["id"]: entry for entry in block["arrays"]}
        self.assertEqual(list(arrays["tmp"]), ["id", "levels", "members", "shards"])
        self.assertEqual(list(arrays["prate"]), ["id", "time", "members", "shards"])
        self.assertEqual(arrays["tmp"]["levels"], [1000, 850, 500])
        self.assertEqual(arrays["ugrd"]["members"][0]["bundle"], "wind850")
        self.assertEqual(arrays["orog"]["time"]["frameCount"], 1)
        self.assertEqual(arrays["tmp"]["members"][1], {"bundle": "tmp850", "variable": metadata["variables"][0]})
        offsets = [member["variable"]["quantization"]["offset"] for member in arrays["tmp"]["members"]]
        self.assertEqual(len(set(offsets)), 3, "each level has its own codebook")

    def test_the_group_crc_covers_every_shard(self) -> None:
        for entry in json.loads((STORE / "zarr.json").read_text(encoding="utf-8"))["attributes"]["xue_columns"]["arrays"]:
            geometry = ColumnStore(STORE).geometry(entry["id"])
            self.assertEqual(len(entry["shards"]), geometry.shard_count)
            for shard, crc32 in enumerate(entry["shards"]):
                data = (STORE / entry["id"] / geometry.shard_path(shard)).read_bytes()
                self.assertEqual(f"{zlib.crc32(data):08x}", crc32)

    def test_the_chains(self) -> None:
        for array_id, chain in (
            ("tmp", ["xue.delta", "bytes", "zstd"]),
            ("tmp2m", ["xue.delta", "bytes", "zstd"]),
            ("prate", ["bytes", "zstd"]),
            ("orog", ["bytes", "zstd"]),
        ):
            configuration = zarrstore.read_json(STORE / array_id / "zarr.json")["codecs"][0]["configuration"]
            self.assertEqual([codec["name"] for codec in configuration["codecs"]], chain, array_id)
            self.assertEqual(configuration["codecs"][-1]["configuration"], {"level": 6, "checksum": True})
            self.assertEqual(configuration["index_location"], "start")

    def test_the_schema_refuses_what_is_not_this_branch(self) -> None:
        def broken(change: Any) -> dict[str, Any]:
            group = json.loads((STORE / "zarr.json").read_text(encoding="utf-8"))
            change(group["attributes"])
            return group

        for change in (
            lambda a: a.update(xue_profile=2),
            lambda a: a.update(xue_network={"version": 1}),
            lambda a: a["xue_columns"]["grid"].update(projection={}),
            lambda a: a["xue_columns"]["arrays"][0].update(id="level"),
            lambda a: a["xue_columns"]["arrays"][0].update(levels=[2**31, 850, 500]),
            lambda a: a["xue_columns"]["arrays"][0]["members"][0].update(bundle="Tmp"),
        ):
            self.assertNotEqual(schema_errors("column-group", broken(change)), [])

    def test_the_attributes(self) -> None:
        tmp = zarrstore.read_json(STORE / "tmp" / "zarr.json")["attributes"]
        self.assertEqual(tmp, {"xue_columns": {"array": "tmp"}, "units": "°C", "_FillValue": 255})
        surface = zarrstore.read_json(STORE / "tmp2m" / "zarr.json")["attributes"]
        codebook = zarrstore.bundle_metadata(FIXTURE / "map" / "bundles.zarr", "tmp2m")["variables"][0]["quantization"]
        self.assertEqual(list(surface), ["xue_columns", "long_name", "units", "scale_factor", "add_offset", "_FillValue"])
        self.assertEqual((surface["scale_factor"], surface["add_offset"]), (codebook["scale"], codebook["offset"]))
        self.assertNotIn("scale_factor", zarrstore.read_json(STORE / "prate" / "zarr.json")["attributes"])

    def test_the_coordinates(self) -> None:
        def values(name: str, dtype: str) -> list[float]:
            return np.frombuffer((STORE / name / "c" / "0").read_bytes(), dtype=dtype).tolist()

        self.assertEqual(values("level", "<i4"), [1000, 850, 500])
        self.assertEqual(values("level_hgt", "<i4"), [1000, 500])
        self.assertEqual(values("level_ugrd", "<i4"), [850])
        self.assertEqual(values("time", "<i4"), [hour * 3600 for hour in golden.HOURS])
        self.assertEqual(values("time_orog", "<i4"), [0])
        self.assertEqual(values("latitude", "<f8")[-1], 10.5)
        self.assertEqual(
            zarrstore.read_json(STORE / "level" / "zarr.json")["attributes"],
            {"standard_name": "air_pressure", "units": "hPa", "positive": "down"},
        )
        self.assertEqual((STORE / "latitude" / "c" / "0").read_bytes(), (FIXTURE / "map" / "bundles.zarr" / "latitude" / "c" / "0").read_bytes())

    def test_a_shard_is_its_index_then_its_payloads(self) -> None:
        store = ColumnStore(STORE)
        for array_id in store.array_ids:
            geometry = store.geometry(array_id)
            for shard in range(geometry.shard_count):
                pairs = store.shard_index(array_id, shard)
                written = [pair for pair in pairs if pair != NEVER_WRITTEN]
                rows = len(geometry.shard_rows(shard))
                self.assertEqual(len(written), -(-rows // 8) * geometry.block_columns)
                self.assertEqual(pairs[: len(written)], written, "a band's unwritten blocks follow its written ones")
                cursor = geometry.index_bytes
                for offset, length in written:
                    self.assertEqual(offset, cursor)
                    cursor += length
                self.assertEqual((STORE / array_id / geometry.shard_path(shard)).stat().st_size, cursor)
        self.assertEqual(sum(pair == NEVER_WRITTEN for pair in store.shard_index("tmp", 1)), 66)

    def test_a_block_past_the_grid_is_fill(self) -> None:
        store = ColumnStore(STORE)
        block = store.block_codes("hgt", 99, 40)
        self.assertEqual(block.shape, (7, 2, 8, 8))
        self.assertTrue((block[:, :, 4:, :] == 255).all() and (block[:, :, :, 1:] == 255).all())
        np.testing.assert_array_equal(block[:, 0, 3, 0], cube("hgt1000")[:, 99, 40])


class WriterTests(TempRoot, unittest.TestCase):
    """:func:`columns.write_column_store` over scratch copies of the map
    store."""

    def setUp(self) -> None:
        super().setUp()
        shutil.copytree(FIXTURE / "map", self.root / "map")
        self.group = self.root / "map" / "bundles.zarr" / "zarr.json"
        self.output = self.root / columns.STORE_NAME

    def write(self, plan: dict[str, Any] | None = None, jobs: int = 1, output: Path | None = None) -> dict[str, Any]:
        return columns.write_column_store(self.root, plan or golden.plan(), output or self.output, jobs)

    def refuse(self, message: str, plan: dict[str, Any] | None = None) -> None:
        with self.assertRaisesRegex(XueError, message):
            self.write(plan)

    def test_the_bytes_do_not_depend_on_the_jobs(self) -> None:
        reports = [self.write(jobs=jobs, output=self.root / f"jobs{jobs}.zarr") for jobs in (1, 3)]
        assert_trees_identical(self, self.root / "jobs1.zarr", self.root / "jobs3.zarr")
        self.assertEqual({**reports[0], "path": ""}, {**reports[1], "path": ""})

    def test_the_report(self) -> None:
        report = self.write()
        self.assertEqual(set(report), {"path", "byteLength", "crc32", "arrays"})
        self.assertEqual(report["path"], str(self.output))
        self.assertEqual(report["byteLength"], zarrstore.store_byte_length(self.output))
        self.assertEqual(report["crc32"], f"{zlib.crc32((self.output / 'zarr.json').read_bytes()):08x}")
        for array_id, size in report["arrays"].items():
            self.assertEqual(size, sum(path.stat().st_size for path in (self.output / array_id / "c").rglob("*") if path.is_file()))

    def test_other_codes_are_another_crc(self) -> None:
        first = self.write()
        with mock.patch.object(golden, "codes", lambda variable_id, hour: np.full((100, 41), 7, dtype=np.uint8)):
            shutil.rmtree(self.root / "map")
            golden.write_map(self.root / "map")
        self.assertEqual(
            self.group.read_bytes(), (FIXTURE / "map" / "bundles.zarr" / "zarr.json").read_bytes(), "the map group holds no code"
        )
        self.assertNotEqual(self.write(output=self.root / "other.zarr")["crc32"], first["crc32"])

    def test_it_reads_only_the_group_documents_and_the_shards(self) -> None:
        inputs = set(columnbuild.plan_inputs(golden.plan()))
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.relative_to(self.root).as_posix() not in inputs:
                path.unlink()
        self.write()
        if same_zstd():
            assert_trees_identical(self, STORE, self.output)

    def test_a_map_store_of_the_delta_chain_is_cut_the_same(self) -> None:
        def rewrite() -> list[zarrstore.SharedMember]:
            members = []
            for bundle_id, variable_ids, hours in golden.BUNDLES:
                source = zarrstore.CodesSource(
                    zarrstore.bundle_metadata(FIXTURE / "map" / "bundles.zarr", bundle_id),
                    {v: [golden.codes(v, hour) for hour in hours] for v in variable_ids},
                    {v: "raw" if variable_spec(v).predictor == "raw" else "previous" for v in variable_ids},
                    (9, 13),
                )
                members.append(zarrstore.SharedMember(bundle_id, source, golden.dimensions(bundle_id, hours), delta=True))
            return members

        self.write(output=self.root / "plain.zarr")
        zarrstore.write_shared_store(self.root / "map" / "bundles.zarr", rewrite(), index_location="start")
        self.write(output=self.root / "delta.zarr")
        assert_trees_identical(self, self.root / "plain.zarr", self.root / "delta.zarr")

    def test_one_bundle_stores_are_cut_too(self) -> None:
        metadata = zarrstore.bundle_metadata(FIXTURE / "map" / "bundles.zarr", "tmp850")
        source = zarrstore.CodesSource(
            metadata, {"tmp850": [golden.codes("tmp850", hour) for hour in golden.HOURS]}, {"tmp850": "previous"}, golden.TILE
        )
        zarrstore.write_store(source, self.root / "tmp850.zarr")
        plan = {
            "columnsPlan": 1,
            "arrays": [
                {"id": "tmp", "predictor": "previous", "levels": [850],
                 "members": [{"bundle": "tmp850", "variable": "tmp850", "store": "tmp850.zarr"}]}
            ],
        }
        self.write(plan)
        np.testing.assert_array_equal(read_array(ColumnStore(self.output), "tmp")[:, 0], cube("tmp850"))

    def test_a_gaussian_grid_keeps_its_rows(self) -> None:
        def change(group: dict[str, Any]) -> None:
            for metadata in group["attributes"]["xue_bundles"].values():
                metadata["grid"]["gaussian"] = {"n": 95, "firstRow": 10, "rowStride": 1}

        edit_json(self.group, change)
        self.write()
        latitude = np.frombuffer((self.output / "latitude" / "c" / "0").read_bytes(), dtype="<f8")
        self.assertNotEqual(latitude[0], golden.GRID.first_latitude)
        self.assertEqual(ColumnStore(self.output).block["grid"]["gaussian"]["n"], 95)

    def test_a_chunk_the_map_store_never_wrote_is_fill(self) -> None:
        shard = self.root / "map" / "bundles.zarr" / "tmp2m" / "c" / "0" / "0" / "0"
        data = bytearray(shard.read_bytes())
        geometry, _, _, _ = zarrstore._array_layout(self.root / "map" / "bundles.zarr", "tmp2m")
        start = len(data) - geometry.shard_index_bytes
        data[start : start + 16] = b"\xff" * 16
        data[-4:] = zarrstore._INDEX_CHECKSUM.pack(zarrstore.crc32c(bytes(data[start:-4])))
        shard.write_bytes(bytes(data))
        self.write()
        found = read_array(ColumnStore(self.output), "tmp2m")[:, 0]
        self.assertTrue((found[:6, :24, :16] == 255).all())
        np.testing.assert_array_equal(found[6:], cube("tmp2m")[6:])
        np.testing.assert_array_equal(found[:, 24:], cube("tmp2m")[:, 24:])

    def test_bad_plans_are_refused(self) -> None:
        def broken(change: Any) -> dict[str, Any]:
            plan = golden.plan()
            change(plan)
            return plan

        def array(plan: dict[str, Any], array_id: str) -> dict[str, Any]:
            return next(entry for entry in plan["arrays"] if entry["id"] == array_id)

        for message, change in (
            ("version 2", lambda p: p.update(columnsPlan=2)),
            ("version 1.0", lambda p: p.update(columnsPlan=1.0)),
            ("version True", lambda p: p.update(columnsPlan=True)),
            ("the column plan", lambda p: p.update(extra=1)),
            ("at least one array", lambda p: p.update(arrays=[])),
            ("array 0", lambda p: p["arrays"][0].update(delta=True)),
            ("array 0", lambda p: p["arrays"][0].pop("predictor")),
            ("unknown predictor", lambda p: p["arrays"][0].update(predictor="anchor")),
            ("cannot name", lambda p: p["arrays"][0].update(id="time")),
            ("cannot name", lambda p: p["arrays"][0].update(id="Tmp")),
            ("occurs twice", lambda p: p["arrays"][1].update(id="tmp")),
            ("occurs twice", lambda p: array(p, "hgt")["members"].__setitem__(0, dict(array(p, "tmp")["members"][0]))),
            ("has no members", lambda p: p["arrays"][0].update(members=[], levels=[])),
            ("strictly descending", lambda p: p["arrays"][0].update(levels=[500, 850, 1000])),
            ("strictly descending", lambda p: p["arrays"][0].update(levels=[1000, 850])),
            ("strictly descending", lambda p: p["arrays"][0].update(levels=[1000, 850, 0])),
            ("strictly descending", lambda p: p["arrays"][0].update(levels=[1000, 850, 500.0])),
            ("at most 2147483647", lambda p: p["arrays"][0].update(levels=[2**31, 850, 500])),
            ("named by it", lambda p: p["arrays"][0].pop("levels")),
            ("named by it", lambda p: array(p, "tmp2m").update(id="t2")),
            ("a member", lambda p: p["arrays"][0]["members"][0].update(level=1000)),
            ("a member", lambda p: p["arrays"][0]["members"][0].update(store=1)),
            ("carries no bundle", lambda p: p["arrays"][0]["members"][0].update(bundle="tmp700")),
            ("has no array", lambda p: p["arrays"][0]["members"][0].update(variable="tmp700")),
            ("cannot read", lambda p: p["arrays"][0]["members"][0].update(store="other.zarr")),
            ("share one time axis", lambda p: (p["arrays"].pop(), p["arrays"][0]["members"][0].update(bundle="prate", variable="prate"))),
        ):
            with self.subTest(message=message):
                self.refuse(message, broken(change))
                self.assertFalse(self.output.exists(), "a refused plan writes nothing")

    def test_an_output_that_exists_is_refused(self) -> None:
        self.output.mkdir()
        self.refuse("exists")

    def test_stores_a_column_store_cannot_hold_are_refused(self) -> None:
        projection = {
            "kind": "lambert-conformal", "radius": 6371229.0, "standardParallel1": 38.5, "standardParallel2": 38.5,
            "latitudeOfOrigin": 38.5, "centralMeridian": -97.5, "x0": -60000.0, "y0": 150000.0, "dx": 3000.0, "dy": -3000.0,
        }

        def bundle(group: dict[str, Any]) -> dict[str, Any]:
            return group["attributes"]["xue_bundles"]["tmp850"]

        def document(group: dict[str, Any]) -> dict[str, Any]:
            return group["consolidated_metadata"]["metadata"]["tmp850"]

        def project(group: dict[str, Any]) -> None:
            grid = bundle(group)["grid"]
            bundle(group)["schemaVersion"] = 4
            bundle(group)["grid"] = {
                **{key: grid[key] for key in ("width", "height", "layout", "rowOrder", "columnOrder")},
                "wrapLongitude": False,
                "projection": projection,
            }

        for message, change in (
            ("nodataCode", lambda g: bundle(g)["variables"][0]["quantization"].update(nodataCode=254)),
            ("not a byte", lambda g: bundle(g)["variables"][0]["quantization"].update(nodataCode=255.0)),
            ("grid is not", lambda g: bundle(g)["grid"].update(firstLongitude=90.0)),
            ("grid is not", lambda g: bundle(g)["grid"].update(firstLongitude=100)),
            ("another run or profile", lambda g: bundle(g).update(profile="quality")),
            ("another run or profile", lambda g: bundle(g).update(runTime="2026-11-24T06:00:00Z")),
            ("regular and Gaussian", project),
            ("one shard", lambda g: document(g)["chunk_grid"]["configuration"]["chunk_shape"].__setitem__(0, 6)),
            ("shape is not", lambda g: document(g)["shape"].__setitem__(2, 40)),
            ("inner codec chain", lambda g: document(g)["codecs"][0]["configuration"]["codecs"].pop()),
            ("has no array", lambda g: g["consolidated_metadata"]["metadata"].pop("tmp850")),
        ):
            with self.subTest(message=message):
                shutil.copyfile(FIXTURE / "map" / "bundles.zarr" / "zarr.json", self.group)
                edit_json(self.group, change)
                self.refuse(message)

    def test_damaged_shards_are_refused(self) -> None:
        shard = self.root / "map" / "bundles.zarr" / "tmp850" / "c" / "0" / "0" / "0"
        data = shard.read_bytes()
        for message, broken in (
            ("CRC-32C", data[:-5] + bytes([data[-5] ^ 1]) + data[-4:]),
            ("smaller than its index", data[:100]),
        ):
            shard.write_bytes(broken)
            with self.subTest(message=message):
                self.refuse(message)
                self.assertFalse(self.output.exists(), "a failed cut removes its output")
        shard.unlink()
        self.refuse("no map shard")
        self.assertFalse(self.output.exists())

    def test_a_variable_without_a_label_has_no_long_name(self) -> None:
        edit_json(self.group, lambda g: g["attributes"]["xue_bundles"]["tmp2m"]["variables"][0].pop("label"))
        edit_json(self.group, lambda g: g["attributes"]["xue_bundles"]["tmp850"]["variables"][0].pop("unit"))
        self.write()
        self.assertNotIn("long_name", zarrstore.read_json(self.output / "tmp2m" / "zarr.json")["attributes"])
        self.assertNotIn("units", zarrstore.read_json(self.output / "tmp" / "zarr.json")["attributes"])


class ReaderTests(TempRoot, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = self.root / columns.STORE_NAME
        shutil.copytree(STORE, self.store)

    def test_bad_groups_are_refused(self) -> None:
        def block(group: dict[str, Any]) -> dict[str, Any]:
            return group["attributes"]["xue_columns"]

        def document(group: dict[str, Any], array_id: str = "tmp") -> dict[str, Any]:
            return group["consolidated_metadata"]["metadata"][array_id]

        for message, change in (
            ("is not a column store", lambda g: g["attributes"].update(xue_network={"version": 1})),
            ("is not a column store", lambda g: g["attributes"].update(xue_profile=2)),
            ("is not a column store", lambda g: g["attributes"].pop("xue_columns")),
            ("version 2", lambda g: block(g).update(version=2)),
            ("runTime", lambda g: block(g).pop("runTime")),
            ("grid", lambda g: block(g)["grid"].update(projection={})),
            ("consolidated", lambda g: g.pop("consolidated_metadata")),
            ("non-empty list", lambda g: block(g).update(arrays=[])),
            ("cannot name", lambda g: block(g)["arrays"][0].update(id="level")),
            ("occurs twice", lambda g: block(g)["arrays"].append(block(g)["arrays"][0])),
            ("strictly descending", lambda g: block(g)["arrays"][0].update(levels=[850, 1000, 500])),
            ("one member", lambda g: block(g)["arrays"][0].pop("levels")),
            ("without a bundle", lambda g: block(g)["arrays"][0]["members"][0].pop("bundle")),
            ("CRC-32", lambda g: block(g)["arrays"][0].update(shards=["nope"])),
            ("no array document", lambda g: g["consolidated_metadata"]["metadata"].pop("tmp")),
            ("uint8", lambda g: document(g).update(data_type="int32")),
            ("is not the block's", lambda g: document(g)["shape"].__setitem__(2, 96)),
            ("inner chain", lambda g: document(g)["codecs"][0]["configuration"].update(codecs=[{"name": "bytes"}])),
            ("axis 0", lambda g: document(g)["codecs"][0]["configuration"]["codecs"][0].update(configuration={"axis": 1})),
            ("index location", lambda g: document(g)["codecs"][0]["configuration"].update(index_location="middle")),
            ("index codecs", lambda g: document(g)["codecs"][0]["configuration"]["index_codecs"].pop()),
            ("fill_value", lambda g: document(g).update(fill_value=0)),
            ("dimension names", lambda g: document(g, "prate").update(dimension_names=["time", "latitude", "longitude"])),
            ("dimension names", lambda g: document(g).update(dimension_names=["time", "level_hgt", "latitude", "longitude"])),
            ("dimension names", lambda g: document(g).update(dimension_names=["time", "level_tmp", "latitude", "longitude"])),
            ("dimension names", lambda g: document(g, "hgt").update(dimension_names=["time", "level", "latitude", "longitude"])),
            ("does not name the array", lambda g: document(g)["attributes"]["xue_columns"].update(array="hgt")),
            ("does not name the array", lambda g: document(g)["attributes"].pop("xue_columns")),
            ("repeats the store's time", lambda g: block(g)["arrays"][0].update(time=block(g)["time"])),
            ("named for its variable", lambda g: block(g)["arrays"][4]["members"][0]["variable"].update(id="tmp3m")),
            ("without a bundle", lambda g: block(g)["arrays"][0]["members"][0].update(bundle="Tmp")),
            ("at most", lambda g: block(g)["arrays"][0].update(levels=[2**31, 850, 500])),
            ("time", lambda g: block(g)["time"].update(frameCount=6)),
            ("consolidated", lambda g: g["consolidated_metadata"].update(kind="other")),
        ):
            with self.subTest(message=message):
                shutil.copyfile(STORE / "zarr.json", self.store / "zarr.json")
                edit_json(self.store / "zarr.json", change)
                with self.assertRaisesRegex(ColumnStoreError, message):
                    ColumnStore(self.store)

    def test_an_unknown_field_is_ignored(self) -> None:
        edit_json(self.store / "zarr.json", lambda g: g["attributes"]["xue_columns"].update(later=1))
        edit_json(self.store / "zarr.json", lambda g: g["attributes"]["xue_columns"]["arrays"][0].pop("shards"))
        self.assertEqual(ColumnStore(self.store).column("tmp", 3, 4).shape, (7, 3))

    def test_a_damaged_index_is_refused(self) -> None:
        shard = self.store / "tmp" / "c" / "0" / "0" / "0" / "0"
        data = bytearray(shard.read_bytes())
        data[3] ^= 1
        shard.write_bytes(bytes(data))
        with self.assertRaisesRegex(ColumnStoreError, "CRC-32C"):
            ColumnStore(self.store).column("tmp", 0, 0)

    def test_the_stem_is_no_bundle_id(self) -> None:
        self.assertFalse(set(zarrstore.RESERVED_BUNDLE_IDS) & {*registry.document()["variables"], *registry.document()["bundles"]})
        metadata = zarrstore.bundle_metadata(FIXTURE / "map" / "bundles.zarr", "orog")
        source = zarrstore.CodesSource(metadata, {"orog": [golden.codes("orog", 0)]}, {"orog": "previous"}, golden.TILE)
        for bundle_id in zarrstore.RESERVED_BUNDLE_IDS:
            with self.assertRaisesRegex(XueError, "cannot name a bundle"):
                zarrstore.write_bundle_arrays(source, self.root / "map.zarr", bundle_id)

    def test_the_other_branches_readers_refuse_the_block(self) -> None:
        with self.assertRaises(XueError):
            zarrstore.read_group(self.store)
        with self.assertRaises(network.NetworkStoreError):
            network.read_group(self.store)
        shared = self.root / "bundles.zarr"
        shutil.copytree(FIXTURE / "map" / "bundles.zarr", shared)
        edit_json(shared / "zarr.json", lambda g: g["attributes"].update(xue_columns={"version": 1}))
        with self.assertRaisesRegex(XueError, "another branch"):
            zarrstore.read_group(shared)

    @unittest.skipUnless(zarrcodec.available(), "zarr-python is not installed (uv sync --group zarr)")
    def test_zarr_python_reads_the_store(self) -> None:
        import zarr

        zarrcodec.register()
        group = zarr.open_group(self.store, mode="r")
        for array_id, levels, members in golden.ARRAYS:
            codes = group[array_id][:]
            for level, (_, variable_id) in enumerate(members):
                with self.subTest(array=array_id, variable=variable_id):
                    np.testing.assert_array_equal(codes if levels is None else codes[:, level], cube(variable_id))

    @unittest.skipUnless(zarrcodec.available(), "zarr-python is not installed (uv sync --group zarr)")
    def test_xarray_opens_the_cube(self) -> None:
        import warnings

        import xarray as xr

        zarrcodec.register()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            dataset = xr.open_zarr(self.store)
            self.assertEqual(dataset["tmp"].dims, ("time", "level", "latitude", "longitude"))
            self.assertEqual(dataset["level"].values.tolist(), [1000, 850, 500])
            quantization = zarrstore.bundle_metadata(FIXTURE / "map" / "bundles.zarr", "tmp2m")["variables"][0]["quantization"]
            codes = cube("tmp2m").astype(np.float64)
            physical = np.where(codes == 255, np.nan, codes * quantization["scale"] + quantization["offset"])
            np.testing.assert_allclose(dataset["tmp2m"].values, physical)


class ColumnArraysTests(unittest.TestCase):
    """What a source's column store is made of, off the registry."""

    def test_no_source_ships_one(self) -> None:
        for source_id, source in SOURCES.items():
            self.assertEqual(source.column_arrays, (), source_id)

    def test_the_field_stays_out_of_the_registry_document(self) -> None:
        with mock.patch.dict(SOURCES, gfs=replace(source_spec("gfs"), column_arrays=column_arrays(source_spec("gfs")))):
            enabled = registry.document_json()
        self.assertEqual(enabled, registry.document_json())

    def test_the_global_runs(self) -> None:
        families = [array_id for array_id, _ in LEVELLED_QUANTITIES]
        surface = ["tmp2m", "dpt2m", "prmsl", "orog", "ugrd10m", "vgrd10m", "cape"]
        for source_id, levels, extra in (("gfs", 15, ["cin"]), ("ecmwf", 12, []), ("ifs", 12, [])):
            with self.subTest(source=source_id):
                source = source_spec(source_id)
                arrays = column_arrays(source)
                self.assertEqual([array.id for array in arrays], families + surface + extra)
                for array in arrays:
                    found = [columns.isobaric_level(variable_id) for _, variable_id in array.members]
                    if array.id in families:
                        self.assertEqual(len(found), levels)
                        self.assertEqual((found[0], found[-1]), (1000, 100))
                        self.assertEqual(found, sorted(found, reverse=True))
                    else:
                        self.assertEqual(found, [None])
                enabled = replace(source, column_arrays=arrays)
                self.assertLessEqual(column_bundle_ids(enabled), set(published_bundle_ids(source)))

    def test_every_source_gives_a_valid_plan(self) -> None:
        for source_id, source in SOURCES.items():
            arrays = column_arrays(source)
            if not arrays:
                continue
            with self.subTest(source=source_id):
                manifest = {
                    "bundles": [
                        {"variable": bundle_id, "zarr": {"path": "bundles.zarr", "byteLength": 1, "crc32": "00000000"}}
                        for bundle_id in published_bundle_ids(source)
                    ]
                }
                plan = columns.build_plan(replace(source, column_arrays=arrays), manifest)
                self.assertEqual(columns._check_plan(plan), plan["arrays"])
                self.assertEqual(json.loads(json.dumps(plan)), plan)

    def test_a_statistic_of_an_ensemble_is_not_the_quantity(self) -> None:
        self.assertEqual(column_arrays(source_spec("cmageps")), ())

    def test_a_derived_bundle_is_not_a_member(self) -> None:
        members = {bundle_id for array in column_arrays(source_spec("gfs")) for bundle_id, _ in array.members}
        self.assertFalse({"qflux850", "thetae850", "cat250", "prate"} & members)


class PlanTests(unittest.TestCase):
    source = replace(source_spec("gfs"), column_arrays=COLUMN_ARRAYS)
    manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))

    def test_the_fixtures_plan(self) -> None:
        self.assertEqual(columns.build_plan(self.source, self.manifest), golden.plan())

    def test_the_inputs(self) -> None:
        with mock.patch.dict(SOURCES, gfs=self.source):
            inputs = columnbuild.column_inputs(self.manifest)
        self.assertEqual(inputs[:2], ["map/bundles.zarr/zarr.json", "map/bundles.zarr/tmp1000/c/0/0/0"])
        self.assertEqual(len(inputs), len(set(inputs)))
        self.assertEqual(len(inputs), 1 + sum(len(members) for _, _, members in golden.ARRAYS))
        self.assertTrue(all((FIXTURE / path).is_file() for path in inputs))
        self.assertEqual(columnbuild.column_inputs(self.manifest), [], "a source without one reads nothing")

    def test_what_has_no_plan(self) -> None:
        mixed = replace(self.source, column_arrays=(ColumnArraySpec("x", (("tmp850", "tmp850"), ("cat300", "cat300"))),))
        with self.assertRaisesRegex(ColumnStoreError, "mixes predicted and RAW"):
            columns.build_plan(mixed, self.manifest)
        pair = replace(self.source, column_arrays=(ColumnArraySpec("x", (("tmp850", "tmp850"), ("tmp2m", "tmp2m"))),))
        with self.assertRaisesRegex(ColumnStoreError, "not all on isobaric"):
            columns.build_plan(pair, self.manifest)
        with self.assertRaisesRegex(ColumnStoreError, "ships no column store"):
            columns.build_plan(source_spec("gfs"), self.manifest)
        lacking = {"bundles": [bundle for bundle in self.manifest["bundles"] if bundle["variable"] != "tmp850"]}
        with self.assertRaisesRegex(ColumnStoreError, "lacks bundle tmp850"):
            columns.build_plan(self.source, lacking)
        segmented = json.loads(json.dumps(self.manifest))
        segmented["bundles"][4]["zarr"] = {"byteLength": 1, "segments": []}
        with self.assertRaisesRegex(ColumnStoreError, "no whole map store"):
            columns.build_plan(self.source, segmented)


class BuildTests(ClassTempRoot, unittest.TestCase):
    """``build-columns`` on a run directory as a publish job fills it."""

    root_prefix = "xue-columns-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.published = cls.root / "published" / f"gfs.{RUN_ID}"
        cls.published.mkdir(parents=True)
        written = golden.write_map(cls.published)
        manifest = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))
        del manifest["columns"]
        for entry in manifest["bundles"]:
            entry["zarr"] = written[entry["variable"]].descriptor(cls.published)
        (cls.published / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        write_pointer_to_manifest(
            cls.published.parent / "latest.json",
            cls.published / "manifest.json",
            run_id=RUN_ID,
            run_time=golden.RUN_TIME,
            model="GFS",
            product="pgrb2.0p25",
        )

    def setUp(self) -> None:
        self.data = self.root / self.id().rsplit(".", 1)[-1]
        self.run_directory = self.data / f"gfs.{RUN_ID}"
        for patch in (
            mock.patch.dict(SOURCES, gfs=replace(source_spec("gfs"), column_arrays=COLUMN_ARRAYS)),
            mock.patch.dict(os.environ, {encoder.SELECTION_VARIABLE: "python"}),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        # Only what the download step fetches.
        manifest = json.loads((self.published / "manifest.json").read_text(encoding="utf-8"))
        for path in ("manifest.json", *columnbuild.column_inputs(manifest)):
            (self.run_directory / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.published / path, self.run_directory / path)
        shutil.copyfile(self.published.parent / "latest.json", self.data / "latest.json")

    def build(self, **options: Any) -> dict[str, Any]:
        return columnbuild.build_columns(self.data, model="gfs", run_id=RUN_ID, jobs=options.pop("jobs", 1), **options)

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.run_directory / "manifest.json").read_text(encoding="utf-8"))

    def test_the_store_is_attached(self) -> None:
        before = self.manifest()
        report = self.build()
        manifest = self.manifest()
        self.assertEqual({key: value for key, value in manifest.items() if key != "columns"}, before)
        store = self.run_directory / columns.STORE_NAME
        self.assertEqual(
            manifest["columns"],
            {
                "path": "columns.zarr",
                "byteLength": zarrstore.store_byte_length(store),
                "crc32": f"{zlib.crc32((store / 'zarr.json').read_bytes()):08x}",
            },
        )
        self.assertEqual(report["columns"], manifest["columns"])
        self.assertEqual(report["checkedColumns"], len(COLUMN_ARRAYS) * columnbuild.CHECK_CELLS)
        self.assertEqual(report["encoder"], "python")
        self.assertEqual(schema_errors("manifest", manifest), [])
        pointer = json.loads((self.data / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(pointer["run"], RUN_ID)
        self.assertEqual(pointer["manifestCrc32"], f"{zlib.crc32((self.run_directory / 'manifest.json').read_bytes()):08x}")
        np.testing.assert_array_equal(read_array(ColumnStore(store), "tmp")[:, 1], cube("tmp850"))

    def test_building_again_changes_nothing(self) -> None:
        self.build()
        first = self.data.with_name("first")
        shutil.copytree(self.data, first)
        self.build(jobs=2)
        assert_trees_identical(self, first, self.data)

    def test_dry_writes_nothing(self) -> None:
        listing = sorted(self.data.rglob("*"))
        report = self.build(dry=True)
        self.assertEqual(sorted(self.data.rglob("*")), listing)
        self.assertEqual(report["arrays"]["tmp"], {"shape": [7, 3, 100, 41], "shards": 2})

    def test_a_source_without_column_arrays_has_nothing_to_do(self) -> None:
        with mock.patch.dict(SOURCES, gfs=replace(source_spec("gfs"), column_arrays=())):
            report = self.build()
        self.assertIsNone(report["columns"])
        self.assertNotIn("columns", self.manifest())

    def test_another_run_or_model_is_refused(self) -> None:
        shutil.copytree(self.run_directory, self.data / "gfs.2026112406")
        with self.assertRaisesRegex(ColumnStoreError, "not 2026112406"):
            columnbuild.build_columns(self.data, model="gfs", run_id="2026112406", jobs=1)
        edit_json(self.run_directory / "manifest.json", lambda m: m.update(model="ECMWF", product="ifs-0p25"))
        with self.assertRaisesRegex(ManifestError, "not GFS"):
            self.build()

    def test_a_pointer_of_another_run_is_not_moved(self) -> None:
        (self.data / "latest.json").write_text(json.dumps({"run": "2026112406"}), encoding="utf-8")
        with self.assertRaisesRegex(ColumnStoreError, "names run 2026112406"):
            self.build()

    def test_a_run_that_is_not_live_is_not_attached_to(self) -> None:
        (self.data / "latest.json").unlink()
        with self.assertRaisesRegex(ColumnStoreError, "no live pointer"):
            self.build()
        self.assertFalse((self.data / "latest.json").exists())

    def test_an_object_a_cdn_would_not_cache_is_refused(self) -> None:
        with mock.patch.object(columnbuild, "MAX_OBJECT_BYTES", 3000), self.assertRaisesRegex(ColumnStoreError, "a CDN caches"):
            self.build()
        self.assertNotIn("columns", self.manifest())
        self.assertEqual(list(self.run_directory.glob("*columns*")), [])

    def test_the_levels_are_the_published_surfaces(self) -> None:
        plan = golden.plan()
        columnbuild.check_levels(columns.resolve_plan(FIXTURE, plan))
        plan["arrays"][0]["levels"] = [1000, 900, 500]
        with self.assertRaisesRegex(ColumnStoreError, "tmp850 was published on 850 hPa, not 900"):
            columnbuild.check_levels(columns.resolve_plan(FIXTURE, plan))

    def test_missing_and_stale_inputs_are_refused(self) -> None:
        shard = self.run_directory / "bundles.zarr" / "tmp850" / "c" / "0" / "0" / "0"
        shard.unlink()
        with self.assertRaisesRegex(ColumnStoreError, "lacks 1 inputs"):
            self.build()
        shutil.copyfile(self.published / "bundles.zarr" / "tmp850" / "c" / "0" / "0" / "0", shard)
        with (self.run_directory / "bundles.zarr" / "zarr.json").open("a") as handle:
            handle.write("\n")
        with self.assertRaisesRegex(ColumnStoreError, "not the group document"):
            self.build()

    def test_a_store_that_does_not_hold_the_codes_is_not_attached(self) -> None:
        real = columns.write_column_store

        def swapped(run_directory: Path, plan: dict[str, Any], output: Path, jobs: int | None = None) -> dict[str, Any]:
            other = json.loads(json.dumps(plan))
            members = other["arrays"][0]["members"]
            members[0], members[1] = members[1], members[0]
            return real(run_directory, other, output, jobs)

        with mock.patch.object(columns, "write_column_store", swapped), self.assertRaisesRegex(ColumnStoreError, "does not hold"):
            self.build()
        self.assertNotIn("columns", self.manifest())
        self.assertEqual((self.data / "latest.json").read_bytes(), (self.published.parent / "latest.json").read_bytes())
        self.assertEqual([path.name for path in self.run_directory.glob("*columns*")], [])

    def test_a_failed_build_leaves_the_store_before_it(self) -> None:
        self.build()
        before = self.data.with_name("before")
        shutil.copytree(self.data, before)
        with mock.patch.object(columnbuild, "check_columns", side_effect=ColumnStoreError("no")), self.assertRaises(ColumnStoreError):
            self.build()
        assert_trees_identical(self, before, self.data)

    def run_cli(self, *arguments: str) -> tuple[int | None, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = main(list(arguments))
        return status, out.getvalue()

    def test_the_commands(self) -> None:
        manifest_path = str(self.run_directory / "manifest.json")
        _, listed = self.run_cli("column-inputs", "--manifest", manifest_path)
        self.assertEqual(listed.splitlines(), columnbuild.column_inputs(self.manifest()))
        missing = ("columns-missing", "--model", "gfs", "--base-manifest", manifest_path)
        self.assertEqual(self.run_cli(*missing)[1], "true\n")
        _, output = self.run_cli("build-columns", "--model", "gfs", "--run", RUN_ID, "--output-dir", str(self.data), "--jobs", "1")
        report = json.loads(output)
        self.assertEqual(report["columns"], self.manifest()["columns"])
        item = json.loads((self.run_directory / "item.json").read_text(encoding="utf-8"))
        self.assertEqual(item["assets"]["columns"]["xue:crc32"], report["columns"]["crc32"])
        self.assertEqual(item["assets"]["manifest"]["href"], f"manifest.json?v={json.loads((self.data / 'latest.json').read_text())['manifestCrc32']}")
        self.assertEqual(self.run_cli(*missing)[1], "false\n")

    def test_columns_missing(self) -> None:
        enabled, plain = source_spec("gfs"), replace(source_spec("gfs"), column_arrays=())
        self.assertTrue(columnbuild.columns_missing(enabled, self.manifest()))
        self.assertFalse(columnbuild.columns_missing(plain, self.manifest()))
        self.assertFalse(columnbuild.columns_missing(enabled, {**self.manifest(), "columns": {}}))
        with self.assertRaises(ManifestError):
            columnbuild.columns_missing(source_spec("ecmwf"), self.manifest())

    def test_the_sampled_cells_are_inside_and_fixed(self) -> None:
        for height, width in ((721, 1440), (100, 41), (1, 1)):
            cells = columnbuild.sample_cells(height, width)
            self.assertEqual(len(cells), columnbuild.CHECK_CELLS)
            self.assertTrue(all(0 <= row < height and 0 <= column < width for row, column in cells))
            self.assertEqual(cells, columnbuild.sample_cells(height, width))


class EncoderSelectionTests(unittest.TestCase):
    """Which writer ``build-columns`` runs (``XUE_ENCODER``)."""

    def select(self, value: str, wheel: bool) -> str:
        with (
            mock.patch.dict(os.environ, {encoder.SELECTION_VARIABLE: value}),
            mock.patch.object(native, "columns_available", return_value=wheel),
            mock.patch.object(native, "available", return_value=True),
            mock.patch.object(native, "require", return_value=object()),
        ):
            return encoder.resolve_columns()

    def test_the_selection(self) -> None:
        self.assertEqual(self.select("python", True), "python")
        self.assertEqual(self.select("auto", True), "native")
        self.assertEqual(self.select("auto", False), "python")
        self.assertEqual(self.select("native", False), "native", "a missing writer is the call's loud error")

    def test_a_wheel_without_the_writer_is_an_error(self) -> None:
        class Old:
            convert_bin = None

        with mock.patch.object(native, "require", return_value=Old()):
            self.assertFalse(native.columns_available())
            with self.assertRaisesRegex(ConversionError, "write_columns"):
                native.write_columns(Path("run"), golden.plan(), Path("out"))

    def test_the_wheel_is_handed_the_plan(self) -> None:
        wheel = mock.Mock()
        with mock.patch.object(native, "require", return_value=wheel):
            native.write_columns(Path("run"), golden.plan(), Path("out"), 3)
        (run_directory, plan, output), options = wheel.write_columns.call_args
        self.assertEqual((run_directory, json.loads(plan), output, options), ("run", golden.plan(), "out", {"jobs": 3}))


class ManifestTests(unittest.TestCase):
    """The optional top-level ``columns``, by the validator and the schema."""

    base = json.loads((FIXTURE / "manifest.json").read_text(encoding="utf-8"))

    def check(self, columns_entry: Any, *, hand: bool, schema: bool) -> None:
        manifest = {**self.base, "columns": columns_entry}
        if hand:
            validate_bin_manifest(manifest, expected_hours=12)
        else:
            with self.assertRaises(ManifestError):
                validate_bin_manifest(manifest, expected_hours=12)
        self.assertEqual(schema_errors("manifest", manifest) == [], schema, columns_entry)

    def test_good(self) -> None:
        for path in ("columns.zarr", "a/b/columns.zarr"):
            self.check({"path": path, "byteLength": 1, "crc32": "0badf00d"}, hand=True, schema=True)
        self.check({"path": "columns.zarr", "byteLength": 1, "crc32": "0badf00d", "later": 1}, hand=True, schema=True)
        without = {key: value for key, value in self.base.items() if key != "columns"}
        validate_bin_manifest(without, expected_hours=12)

    def test_bad(self) -> None:
        good = {"path": "columns.zarr", "byteLength": 1, "crc32": "0badf00d"}
        for change in (
            {"path": "/columns.zarr"},
            {"path": "https://x/columns.zarr"},
            {"path": "../columns.zarr"},
            {"path": "a/../columns.zarr"},
            {"path": "mycolumns.zarr"},
            {"path": "columns.half.zarr"},
            {"path": "columns.zarr/"},
            {"path": None},
            {"byteLength": 0},
            {"byteLength": 1.5},
            {"byteLength": "1"},
            {"crc32": "0BADF00D"},
            {"crc32": "0badf00"},
        ):
            with self.subTest(change=change):
                self.check({**good, **change}, hand=False, schema=False)
        for key in good:
            with self.subTest(missing=key):
                self.check({k: v for k, v in good.items() if k != key}, hand=False, schema=False)
        self.check(None, hand=False, schema=False)
        self.check({**good, "byteLength": True}, hand=False, schema=False)

    def test_the_path_is_unique(self) -> None:
        manifest = json.loads(json.dumps(self.base))
        manifest["bundles"][0]["zarr"]["path"] = "columns.zarr"
        with self.assertRaisesRegex(ManifestError, "duplicate"):
            validate_bin_manifest(manifest, expected_hours=12)

    def test_the_stac_asset(self) -> None:
        asset = manifest_assets(self.base, "01234567")["columns"]
        self.assertEqual(asset["href"], "columns.zarr")
        self.assertEqual((asset["xue:kind"], asset["xue:tier"], asset["xue:columns"]), ("store", "full", True))
        self.assertEqual((asset["file:size"], asset["xue:crc32"]), (self.base["columns"]["byteLength"], self.base["columns"]["crc32"]))
        self.assertNotIn("file:checksum", asset, "a store is not one file")
        without = {key: value for key, value in self.base.items() if key != "columns"}
        self.assertNotIn("columns", manifest_assets(without, "01234567"))


if __name__ == "__main__":
    unittest.main()

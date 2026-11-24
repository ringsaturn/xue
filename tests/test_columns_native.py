"""The native column store writer (`xue.write_columns`) held to the reference
(:func:`xuebuild.columns.write_column_store`): one plan, one run directory,
every object byte for byte, at one worker and at four. Over the committed
fixture's map store and over synthetic ones whose grids, axes, levels,
predictors and codebooks are the awkward cases.
"""

from __future__ import annotations

import json
import shutil
import unittest
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from tests import prepare_column_golden as golden
from tests._support import TempRoot, assert_trees_identical, require_comparable_compression
from xuebuild import columns, native, zarrstore, zstdcli
from xuebuild.errors import XueError
from xuebuild.grid import GaussianRows, GridInfo
from xuebuild.metadata import build_metadata
from xuebuild.variables import variable_spec

RUN_TIME = datetime(2026, 11, 24, 0, tzinfo=UTC)
REFERENCE_ZSTD = ".".join(map(str, zstdcli.zstd_version()))
NODATA = 255

GRID = GridInfo(
    width=41, height=100, first_longitude=100.0, first_latitude=60.0, longitude_step=0.5, latitude_step=-0.5
)
"""Two bands, the second one block row, and a padded last block column."""
IRREGULAR = [0, 1, 2, 3, 6, 9, 12]
TILE = (16, 24)

requires_write_columns = unittest.skipUnless(
    native.columns_available(), f"the installed {native.DISTRIBUTION} wheel has no write_columns"
)


def predictor(variable_id: str) -> str:
    return "raw" if variable_spec(variable_id).predictor == "raw" else "previous"


def planes(variable_id: str, offsets: Sequence[int], grid: GridInfo) -> list[np.ndarray]:
    """Codes of a variable over its axis: smooth enough to compress, noisy
    enough that a cell out of place shows, and some of them nodata."""
    generator = np.random.default_rng([sum(variable_id.encode()), grid.height, grid.width])
    rows, cols = np.mgrid[0 : grid.height, 0 : grid.width]
    frames = []
    for offset in offsets:
        plane = (40 + rows // 3 + (cols + offset) // 5 + generator.integers(0, 3, rows.shape)).astype(np.uint8)
        plane[generator.random(rows.shape) < 0.02] = NODATA
        frames.append(plane)
    return frames


def member(
    bundle_id: str,
    variable_ids: tuple[str, ...],
    offsets: list[int],
    *,
    grid: GridInfo = GRID,
    tile: tuple[int, int] = TILE,
    delta: bool = False,
    axis: str = "time",
) -> zarrstore.SharedMember:
    source = zarrstore.CodesSource(
        build_metadata(RUN_TIME, offsets, grid, "balanced", variable_ids),
        {variable_id: planes(variable_id, offsets, grid) for variable_id in variable_ids},
        {variable_id: predictor(variable_id) for variable_id in variable_ids},
        tile,
    )
    return zarrstore.SharedMember(bundle_id, source, (axis, "latitude", "longitude"), delta=delta)


def array(
    array_id: str,
    members: Sequence[tuple[str, str]],
    levels: Sequence[int] | None = None,
    *,
    store: str = "bundles.zarr",
    stored: str | None = None,
) -> dict[str, Any]:
    """A plan's array; its predictor the registry's unless ``stored`` says."""
    return {
        "id": array_id,
        "predictor": stored or predictor(members[0][1]),
        **({"levels": list(levels)} if levels is not None else {}),
        "members": [{"bundle": bundle, "variable": variable, "store": store} for bundle, variable in members],
    }


def plan(*arrays: dict[str, Any]) -> dict[str, Any]:
    return {"columnsPlan": columns.PLAN_VERSION, "arrays": list(arrays)}


@requires_write_columns
class NativeColumnWriterTests(TempRoot, unittest.TestCase):
    """Directory parity."""

    def write(self, *members: zarrstore.SharedMember, index_location: str = "end") -> None:
        zarrstore.write_shared_store(self.root / "bundles.zarr", list(members), index_location=index_location)

    def assert_parity(self, document: dict[str, Any], run_directory: Path | None = None) -> None:
        import xue

        run_directory = run_directory or self.root
        reference = self.root / "reference" / columns.STORE_NAME
        report = columns.write_column_store(run_directory, document, reference, jobs=1)
        for jobs in (1, 4):
            subject = self.root / f"native-{jobs}" / columns.STORE_NAME
            written = xue.write_columns(str(run_directory), json.dumps(document), str(subject), jobs=jobs)
            require_comparable_compression(self, {"zstdVersion": REFERENCE_ZSTD}, written)
            with self.subTest(jobs=jobs):
                assert_trees_identical(self, reference, subject, expect=["zarr.json", "time/zarr.json"])
                self.assertEqual(
                    {**report, "path": str(subject)},
                    {key: value for key, value in written.items() if key != "zstdVersion"},
                )
                self.assertEqual(list(report["arrays"]), list(written["arrays"]))
        parallel = self.root / "reference-4" / columns.STORE_NAME
        columns.write_column_store(run_directory, document, parallel, jobs=4)
        assert_trees_identical(self, reference, parallel)

    def test_the_fixture(self) -> None:
        self.assert_parity(golden.plan(), golden.FIXTURES)

    def test_levels_and_codebooks_that_differ(self) -> None:
        levels = {"tmp": (1000, 925, 850, 500, 250), "hgt": (1000, 500), "rh": (850, 500)}
        self.write(
            *(
                member(f"{quantity}{level}", (f"{quantity}{level}",), IRREGULAR)
                for quantity, surfaces in levels.items()
                for level in surfaces
            ),
            member("wind850", ("ugrd850", "vgrd850"), IRREGULAR),
            member("wind500", ("ugrd500", "vgrd500"), IRREGULAR),
        )
        codebooks = {
            json.dumps(variable["quantization"])
            for bundle in ("hgt1000", "hgt500")
            for variable in zarrstore.bundle_metadata(self.root / "bundles.zarr", bundle)["variables"]
        }
        self.assertEqual(len(codebooks), 2, "the heights' codebooks must differ for this to test anything")
        self.assert_parity(
            plan(
                *(
                    array(quantity, [(f"{quantity}{level}", f"{quantity}{level}") for level in surfaces], surfaces)
                    for quantity, surfaces in levels.items()
                ),
                array("ugrd", [("wind850", "ugrd850"), ("wind500", "ugrd500")], (850, 500)),
                array("vgrd", [("wind850", "vgrd850"), ("wind500", "vgrd500")], (850, 500)),
            )
        )

    def test_the_grids(self) -> None:
        shapes = {
            "one cell": (1, 1),
            "one block": (8, 8),
            "one band, whole blocks": (96, 16),
            "one row past a band": (97, 7),
            "three bands": (193, 9),
        }
        for name, (height, width) in shapes.items():
            with self.subTest(grid=name):
                shutil.rmtree(self.root)
                self.root.mkdir()
                grid = replace(GRID, height=height, width=width, latitude_step=-0.25)
                self.write(
                    member("tmp850", ("tmp850",), IRREGULAR, grid=grid),
                    member("tmp500", ("tmp500",), IRREGULAR, grid=grid),
                    member("tmp2m", ("tmp2m",), IRREGULAR, grid=grid),
                )
                self.assert_parity(
                    plan(
                        array("tmp", [("tmp850", "tmp850"), ("tmp500", "tmp500")], (850, 500)),
                        array("tmp2m", [("tmp2m", "tmp2m")]),
                    )
                )

    def test_a_gaussian_grid(self) -> None:
        grid = replace(GRID, height=94, first_latitude=88.542, latitude_step=-0.9473684210526315, gaussian=GaussianRows(95))
        self.write(member("tmp2m", ("tmp2m",), IRREGULAR, grid=grid))
        self.assert_parity(plan(array("tmp2m", [("tmp2m", "tmp2m")])))

    def test_axes_that_differ(self) -> None:
        self.write(
            # The store's axis is the first array's, here not the levelled ones'.
            member("orog", ("orog",), [0], axis="time_orog"),
            member("prate", ("prate",), IRREGULAR[1:], axis="time_prate"),
            member("tmp850", ("tmp850",), IRREGULAR),
            member("tmp500", ("tmp500",), IRREGULAR),
            member("tmp2m", ("tmp2m",), list(range(0, 39, 3)), axis="time_tmp2m"),
        )
        self.assert_parity(
            plan(
                array("orog", [("orog", "orog")], stored="previous"),
                array("tmp", [("tmp850", "tmp850"), ("tmp500", "tmp500")], (850, 500)),
                array("prate", [("prate", "prate")]),
                array("tmp2m", [("tmp2m", "tmp2m")]),
            )
        )

    def test_the_plans_predictor_is_what_is_stored(self) -> None:
        self.write(member("tmp850", ("tmp850",), IRREGULAR), member("prate", ("prate",), IRREGULAR))
        for stored in ("raw", "previous"):
            for name in ("reference", "reference-4", "native-1", "native-4"):
                shutil.rmtree(self.root / name, ignore_errors=True)
            with self.subTest(stored=stored):
                self.assert_parity(
                    plan(
                        array("tmp", [("tmp850", "tmp850")], (850,), stored=stored),
                        array("prate", [("prate", "prate")], stored=stored),
                    )
                )

    def test_map_stores_of_the_other_forms(self) -> None:
        """The delta chain and an index at the start, in a shared store, and a
        one-bundle store beside it."""
        self.write(
            member("tmp850", ("tmp850",), IRREGULAR, tile=(9, 13), delta=True),
            member("prate", ("prate",), IRREGULAR, tile=(41, 100)),
            index_location="start",
        )
        single = member("tmp500", ("tmp500",), IRREGULAR, tile=(5, 96))
        zarrstore.write_store(single.source, self.root / "tmp500.zarr")
        levelled = array("tmp", [("tmp850", "tmp850"), ("tmp500", "tmp500")], (850, 500))
        levelled["members"][1]["store"] = "tmp500.zarr"
        self.assert_parity(plan(levelled, array("prate", [("prate", "prate")])))

    def test_a_tile_never_written(self) -> None:
        self.write(member("tmp2m", ("tmp2m",), IRREGULAR))
        shard = self.root / "bundles.zarr" / "tmp2m" / "c" / "0" / "0" / "0"
        data = bytearray(shard.read_bytes())
        geometry, _, _, _ = zarrstore._array_layout(self.root / "bundles.zarr", "tmp2m")
        start = len(data) - geometry.shard_index_bytes
        data[start : start + 16] = b"\xff" * 16
        data[-4:] = zarrstore._INDEX_CHECKSUM.pack(zarrstore.crc32c(bytes(data[start:-4])))
        shard.write_bytes(bytes(data))
        self.assert_parity(plan(array("tmp2m", [("tmp2m", "tmp2m")])))


@requires_write_columns
class NativeColumnRefusalTests(TempRoot, unittest.TestCase):
    """What the reference refuses the wheel refuses, and neither leaves
    its output behind."""

    def setUp(self) -> None:
        super().setUp()
        shutil.copytree(golden.FIXTURES / "map", self.root / "map")
        self.group = self.root / "map" / "bundles.zarr" / "zarr.json"
        self.output = self.root / columns.STORE_NAME

    def refuse(self, document: Any) -> None:
        import xue

        with self.assertRaises(XueError):
            columns.write_column_store(self.root, document, self.output, 1)
        self.assertFalse(self.output.exists())
        with self.assertRaises(RuntimeError) as raised:
            xue.write_columns(str(self.root), json.dumps(document), str(self.output), jobs=2)
        self.assertTrue(str(raised.exception))
        self.assertFalse(self.output.exists())

    def test_plans(self) -> None:
        changes: dict[str, Callable[[dict[str, Any]], Any]] = {
            "another version": lambda p: p.update(columnsPlan=2),
            "a boolean version": lambda p: p.update(columnsPlan=True),
            "a fractional version": lambda p: p.update(columnsPlan=1.0),
            "no version": lambda p: p.pop("columnsPlan"),
            "an unknown key": lambda p: p.update(block=8),
            "no arrays": lambda p: p.update(arrays=[]),
            "an unknown array key": lambda p: p["arrays"][0].update(shardRows=96),
            "no predictor": lambda p: p["arrays"][0].pop("predictor"),
            "an unknown predictor": lambda p: p["arrays"][0].update(predictor="anchor"),
            "a bad id": lambda p: p["arrays"][0].update(id="Tmp"),
            "a coordinate's id": lambda p: p["arrays"][0].update(id="level"),
            "a repeated id": lambda p: p["arrays"][1].update(id="tmp"),
            "a repeated variable": lambda p: p["arrays"].append(
                {"id": "tmp500", "predictor": "raw", "members": [p["arrays"][0]["members"][2]]}
            ),
            "no members": lambda p: p["arrays"][0].update(members=[], levels=[]),
            "ascending levels": lambda p: p["arrays"][0].update(levels=[500, 850, 1000]),
            "a level short": lambda p: p["arrays"][0].update(levels=[1000, 850]),
            "a fractional level": lambda p: p["arrays"][0].update(levels=[1000, 850, 500.5]),
            "a level of zero": lambda p: p["arrays"][0].update(levels=[1000, 850, 0]),
            "a level past the coordinate": lambda p: p["arrays"][0].update(levels=[2**31, 850, 500]),
            "null levels": lambda p: p["arrays"][0].update(levels=None),
            "members without levels": lambda p: p["arrays"][0].pop("levels"),
            "an unknown member key": lambda p: p["arrays"][0]["members"][0].update(level=1000),
            "an empty store": lambda p: p["arrays"][0]["members"][0].update(store=""),
            "a missing store": lambda p: p["arrays"][0]["members"][0].update(store="other.zarr"),
            "a missing bundle": lambda p: p["arrays"][0]["members"][0].update(bundle="tmp700"),
            "a missing variable": lambda p: p["arrays"][0]["members"][0].update(variable="tmp700"),
            "two axes in one array": lambda p: (
                p["arrays"].pop(),
                p["arrays"][0]["members"][0].update(bundle="prate", variable="prate"),
            ),
        }
        for name, change in changes.items():
            with self.subTest(plan=name):
                document = golden.plan()
                change(document)
                self.refuse(document)

    def test_stores(self) -> None:
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
                "projection": {
                    "kind": "lambert-conformal", "radius": 6371229.0, "standardParallel1": 38.5,
                    "standardParallel2": 38.5, "latitudeOfOrigin": 38.5, "centralMeridian": -97.5,
                    "x0": -60000.0, "y0": 150000.0, "dx": 3000.0, "dy": -3000.0,
                },
            }  # fmt: skip

        changes: dict[str, Callable[[dict[str, Any]], Any]] = {
            "another nodata code": lambda g: bundle(g)["variables"][0]["quantization"].update(nodataCode=254),
            "another grid": lambda g: bundle(g)["grid"].update(firstLongitude=90.0),
            "a grid spelled otherwise": lambda g: bundle(g)["grid"].update(firstLongitude=100),
            "another run": lambda g: bundle(g).update(runTime="2026-11-24T06:00:00Z"),
            "another profile": lambda g: bundle(g).update(profile="quality"),
            "a projected grid": project,
            "two shards": lambda g: document(g)["chunk_grid"]["configuration"]["chunk_shape"].__setitem__(0, 6),
            "another shape": lambda g: document(g)["shape"].__setitem__(2, 40),
            "another codec chain": lambda g: document(g)["codecs"][0]["configuration"]["codecs"].pop(),
            "no array document": lambda g: g["consolidated_metadata"]["metadata"].pop("tmp850"),
            "no consolidated block": lambda g: g.pop("consolidated_metadata"),
            "another branch's block": lambda g: g["attributes"].update(xue_columns={}),
            "another profile version": lambda g: g["attributes"].update(xue_profile=9),
        }
        original = json.loads(self.group.read_text(encoding="utf-8"))
        for name, change in changes.items():
            with self.subTest(store=name):
                group = json.loads(json.dumps(original))
                change(group)
                self.group.write_text(json.dumps(group), encoding="utf-8")
                self.refuse(golden.plan())

    def test_shards(self) -> None:
        shard = self.root / "map" / "bundles.zarr" / "tmp850" / "c" / "0" / "0" / "0"
        data = shard.read_bytes()
        for name, broken in (
            ("an index that does not verify", data[:-5] + bytes([data[-5] ^ 1]) + data[-4:]),
            ("less than its index", data[:100]),
            ("a payload that does not decode", bytes(16) + data[16:]),
        ):
            with self.subTest(shard=name):
                shard.write_bytes(broken)
                self.refuse(golden.plan())
        shard.unlink()
        self.refuse(golden.plan())

    def test_an_output_that_exists(self) -> None:
        import xue

        self.output.mkdir()
        (self.output / "kept").write_text("kept", encoding="utf-8")
        with self.assertRaises(XueError):
            columns.write_column_store(self.root, golden.plan(), self.output, 1)
        with self.assertRaises(RuntimeError):
            xue.write_columns(str(self.root), json.dumps(golden.plan()), str(self.output))
        self.assertEqual([path.name for path in self.output.iterdir()], ["kept"])


if __name__ == "__main__":
    unittest.main()

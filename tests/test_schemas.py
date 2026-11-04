"""Every committed or built golden matches its normative JSON Schema
(``docs/schemas/``), and every schema is a valid draft 2020-12 document that
some golden exercises. The goldens are what the writers produce: the point
products' (``tests/prepare_<product>_golden.py``), the frozen container
corpus the Rust decoder reads (``tests/fixtures/v1/``), the committed
stores (``indicators/store``, ``river/network``), and what this test builds
in a scratch directory without GDAL: the synthetic run the shell's tests
play (``tests/prepare_web_fixture.py``), a rolling window cut into segments
(``tests/test_segments.py``'s round), and a polar store from the nexrad
fixture's sweeps. They are what the shell's, the Rust decoder's and the API
worker's tests read, so this is where the three meet the one contract."""

from __future__ import annotations

import json
import shutil
import struct
import tempfile
import unittest
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from tests import prepare_web_fixture
from tests.jsonschemas import SCHEMAS, check, check_lines, validator

FIXTURES = Path(__file__).resolve().parent / "fixtures"

JSON = "json"
"""A whole JSON document."""
LINES = "lines"
"""A JSONL file whose every line is one document."""
CONTAINER = "container"
"""A ``.xue`` container's embedded metadata JSON."""
POSTERS = "posters"
"""Every poster's ``metadataJson`` in a manifest."""
GROUP_BUNDLES = "group-bundles"
"""Every bundle's metadata a grid store's group document carries."""

BUILT = "@built/"
"""A pattern under the scratch directory ``SchemaTest`` builds, not under
``tests/fixtures``."""

GRID_STORES = ("indicators/store/*/*.zarr", BUILT + "web/*.zarr", BUILT + "segments/*.segments/*/*/*.zarr")
"""Every grid store the goldens hold: profile 1 and profile 2, shared and
one-bundle, map, series and segment stores."""

GOLDENS: dict[str, tuple[tuple[str, str], ...]] = {
    "pointer": ((JSON, "*/expected/latest-*.json"),),
    "fire-index": ((JSON, "fire/expected/fire.*/index.json"),),
    "fire-line": ((LINES, "fire/expected/fire.*/*.jsonl"),),
    "aq-index": ((JSON, "aq/expected/aq/*/*/*/aq.*/index.json"),),
    "aq-stations": ((JSON, "aq/expected/aq/*/*/*/aq.*/*.stations.json"),),
    "aq-line": ((LINES, "aq/expected/aq/*/*/*/aq.*/*.jsonl"),),
    "alerts-index": ((JSON, "alerts/expected/alerts/*/*/*/alerts.*/index.json"),),
    "alerts-line": ((LINES, "alerts/expected/alerts/*/*/*/alerts.*/*.jsonl"),),
    "tc-index": ((JSON, "tc/expected/index.json"),),
    "tc-storm": ((JSON, "tc/expected/[A-Zx]*.json"),),
    "sounding-index": ((JSON, "sounding/expected/index.json"),),
    "sounding-line": ((LINES, "sounding/expected/soundings.jsonl"),),
    "airport-index": ((JSON, "airport/expected/airport/*/*/*/airport.*/index.json"),),
    "airport-line": ((LINES, "airport/expected/airport/*/*/*/airport.*/history.jsonl"),),
    "synop-index": ((JSON, "synop/expected/synop/*/*/*/synop.*/index.json"),),
    "synop-index-v1": ((JSON, "synop/v1/synop.*/index.json"),),
    "synop-stations": ((JSON, "synop/expected/synop/*/*/*/synop.*/*.stations.json"),),
    "synop-line": (
        (LINES, "synop/expected/synop/*/*/*/synop.*/*.jsonl"),
        (LINES, "synop/v1/synop.*/*.jsonl"),
    ),
    "manifest": (
        (JSON, "indicators/store/*/manifest.json"),
        (JSON, BUILT + "web/manifest.json"),
        (JSON, BUILT + "web/*/manifest.json"),
        (JSON, BUILT + "web/showcase/*/manifest.json"),
        (JSON, BUILT + "segments/*/*/manifest.json"),
    ),
    "bundle-metadata": (
        (CONTAINER, "v1/*.xue"),
        (CONTAINER, BUILT + "web/*.xue"),
        (POSTERS, BUILT + "web/manifest.json"),
        (POSTERS, BUILT + "web/showcase/*/manifest.json"),
        *((GROUP_BUNDLES, f"{store}/zarr.json") for store in GRID_STORES),
    ),
    "store-group": tuple((JSON, f"{store}/zarr.json") for store in GRID_STORES),
    "store-array": (
        *((JSON, f"{store}/*/zarr.json") for store in GRID_STORES),
        (JSON, "river/network/*.zarr/*/zarr.json"),
        (JSON, BUILT + "polar/*.zarr/*/zarr.json"),
    ),
    "network-group": ((JSON, "river/network/*.zarr/zarr.json"),),
    "polar-group": ((JSON, BUILT + "polar/*.zarr/zarr.json"),),
}
"""Schema → the goldens it holds."""


def container_metadata(path: Path) -> dict[str, Any]:
    """The metadata JSON a ``.xue`` container embeds (docs/format.md,
    FixedHeader: its offset at byte 24, its length at byte 32), the same in
    container v1 and v2."""
    data = path.read_bytes()
    offset, length = struct.unpack_from("<QQ", data, 24)
    return json.loads(data[offset : offset + length].decode("utf-8"))


def group_bundles(group: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    attributes = group["attributes"]
    if "xue" in attributes:
        yield "xue", attributes["xue"]
    for bundle_id, metadata in attributes.get("xue_bundles", {}).items():
        yield f"xue_bundles.{bundle_id}", metadata


def documents(kind: str, path: Path) -> Iterator[tuple[str, Any]]:
    """The documents a golden file holds for its schema, each with a label."""
    if kind == CONTAINER:
        yield path.name, container_metadata(path)
    elif kind == POSTERS:
        for bundle in json.loads(path.read_text(encoding="utf-8"))["bundles"]:
            if "poster" in bundle:
                yield f"{path.name} {bundle['variable']} poster", json.loads(bundle["poster"]["metadataJson"])
    elif kind == GROUP_BUNDLES:
        for label, metadata in group_bundles(json.loads(path.read_text(encoding="utf-8"))):
            yield f"{path.parent.name} {label}", metadata
    else:
        yield path.name, json.loads(path.read_text(encoding="utf-8"))


def build_web(root: Path) -> None:
    """The shell's synthetic runs: a GFS run whose bundles ship containers,
    shared map stores at two tiers, series stores and posters; an ECMWF,
    an sflux and a showcase manifest; and a one-bundle delta store."""
    with mock.patch.object(prepare_web_fixture, "WEB_FIXTURE_ROOT", root):
        prepare_web_fixture.prepare_web_fixture()


def build_segments(root: Path) -> None:
    """A rolling-window round cut into shared segments, one bundle on an
    axis of its own (``time_prate``), and its manifest naming them."""
    from tests.test_segments import OFFSETS, write_round
    from xuebuild import segments

    round_directory = write_round(root, "gfs.2026101122/0130", OFFSETS, prate_offsets=OFFSETS[6:])
    segments.segment_round(round_directory, data_root=root, source_id="gfs")


def build_polar(root: Path) -> None:
    """A round store and a window store of the nexrad fixture's sweeps."""
    from xuebuild.nexrad.level3 import read_sweep
    from xuebuild.nexrad.store import SiteSweeps, write_store, write_window_store

    def sweep(name: str):
        return read_sweep((FIXTURES / "nexrad" / "nexrad" / name[:3] / name).read_bytes())

    gwx = [sweep("GWX_N0G_2023_03_25_01_36_23"), sweep("GWX_N0G_2023_03_25_01_38_00")]
    dgx = [sweep("DGX_N0G_2023_03_25_01_37_39")]
    sites = [
        SiteSweeps("DGX", dgx[0].site_latitude, dgx[0].site_longitude, 100.0, dgx),
        SiteSweeps("GWX", gwx[0].site_latitude, gwx[0].site_longitude, 180.0, gwx),
    ]
    write_store(root / "n0g.zarr", product="n0g", round_time=datetime(2023, 3, 25, 1, 40, tzinfo=UTC), sites=sites)
    rounds = [datetime(2023, 3, 25, 1, 35, tzinfo=UTC), datetime(2023, 3, 25, 1, 40, tzinfo=UTC)]
    write_window_store(root / "window.zarr", product="n0g", rounds=rounds, sites=sites)


class SchemaTest(unittest.TestCase):
    built: Path

    @classmethod
    def setUpClass(cls) -> None:
        cls._scratch = tempfile.mkdtemp(prefix="xue-schemas-")
        cls.built = Path(cls._scratch)
        build_web(cls.built / "web")
        build_segments(cls.built / "segments")
        build_polar(cls.built / "polar")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._scratch, ignore_errors=True)

    def paths(self, pattern: str) -> list[Path]:
        if pattern.startswith(BUILT):
            return sorted(self.built.glob(pattern.removeprefix(BUILT)))
        return sorted(FIXTURES.glob(pattern))

    def test_every_schema_is_valid_and_has_goldens(self) -> None:
        names = sorted(path.name.removesuffix(".schema.json") for path in SCHEMAS.glob("*.schema.json"))
        self.assertEqual(names, sorted(GOLDENS))
        for name in names:
            with self.subTest(name):
                validator(name)
                schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertTrue(schema["$id"].endswith(f"/docs/schemas/{name}.schema.json"))

    def test_the_goldens_match(self) -> None:
        for name, globs in GOLDENS.items():
            count = 0
            for kind, pattern in globs:
                for path in self.paths(pattern):
                    with self.subTest(name=name, path=str(path)):
                        if kind == LINES:
                            count += check_lines(name, path)
                            continue
                        for label, document in documents(kind, path):
                            check(name, document, label)
                            count += 1
            self.assertGreater(count, 0, f"no golden exercises {name}")

    def test_the_grid_goldens_cover_every_shape(self) -> None:
        """The grid goldens reach the shapes the schemas name: both store
        profiles, a segmented store, a series store, a listed and a uniform
        time axis, and a store whose bundle runs on an axis of its own."""
        groups = [json.loads(path.read_text(encoding="utf-8")) for store in GRID_STORES for path in self.paths(f"{store}/zarr.json")]
        self.assertEqual({group["attributes"]["xue_profile"] for group in groups}, {1, 2})
        manifests = [json.loads(path.read_text(encoding="utf-8")) for path in self.paths(BUILT + "segments/*/*/manifest.json")]
        self.assertTrue(any("segments" in bundle.get("zarr", {}) for manifest in manifests for bundle in manifest["bundles"]))
        web = json.loads((self.built / "web" / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(any("series" in bundle for bundle in web["bundles"]))
        axes = [metadata["time"] for group in groups for _, metadata in group_bundles(group)]
        self.assertTrue(any("frameStep" in axis for axis in axes) and any("frameOffsets" in axis for axis in axes))
        arrays = [
            name
            for group in groups
            for name in group.get("consolidated_metadata", {}).get("metadata", {})
        ]
        self.assertIn("time_prate", arrays)


if __name__ == "__main__":
    unittest.main()

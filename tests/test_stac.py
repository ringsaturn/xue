"""The STAC catalog derived beside the manifests (`xuebuild.stac`).

Every document is a pure function of a manifest, a catalog row and the
source registry, so these build the documents from synthetic manifests and
hold them to the shapes `docs/stac.md` promises: the asset per artifact, the
crc32 as a multihash, the grid read off the poster, the links climbing back
to the root, the pointer mirrored as a Collection. When pystac is installed
the documents are also round-tripped through it (no network: the published
schemas are not fetched here).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from xuebuild import stac
from xuebuild.manifest import build_bin_manifest
from xuebuild.sources import SOURCES, source_spec

try:
    import pystac
except ImportError:  # pragma: no cover - optional
    pystac = None


def _metadata(
    *, grid: dict, unit_seconds: int = 3600, first: int = 0, count: int = 7, step: int | None = 1, offsets=None
) -> str:
    time: dict = {"unitSeconds": unit_seconds, "firstFrameOffset": first, "frameCount": count}
    if offsets is not None:
        time["frameOffsets"] = list(offsets)
    else:
        time["frameStep"] = step
    return json.dumps(
        {
            "schemaVersion": 3,
            "model": "GFS",
            "product": "pgrb2.0p25",
            "runTime": "2026-08-14T06:00:00Z",
            "profile": "balanced",
            "time": time,
            "grid": {
                "layout": "row-major",
                "rowOrder": "north-to-south",
                "columnOrder": "west-to-east",
                **grid,
            },
            "variables": [],
        }
    )


GLOBAL_POSTER_GRID = {
    "width": 720,
    "height": 361,
    "firstLongitude": -180.0,
    "firstLatitude": 90.0,
    "longitudeStep": 0.5,
    "latitudeStep": -0.5,
    "wrapLongitude": True,
}
REGIONAL_POSTER_GRID = {
    "width": 1221,
    "height": 526,
    "firstLongitude": -134.1,
    "firstLatitude": 52.62,
    "longitudeStep": 0.06,
    "latitudeStep": -0.06,
    "wrapLongitude": False,
}


def _poster(variable: str, metadata: str) -> dict:
    return {
        "path": f"{variable}.poster.bin",
        "width": 720,
        "height": 361,
        "byteLength": 85_848,
        "crc32": "afe3b694",
        "metadataJson": metadata,
    }


def _store_entry(variable: str, *, poster: dict | None = None) -> dict:
    entry: dict = {
        "variable": variable,
        "zarr": {"path": f"{variable}.zarr", "byteLength": 12_347_075, "crc32": "760cef95"},
        "variants": [
            {
                "width": 720,
                "height": 361,
                "bandwidth": 7_510_029,
                "zarr": {"path": f"{variable}.half.zarr", "byteLength": 4_000_000, "crc32": "0d3a2e6b"},
            }
        ],
    }
    if poster is not None:
        entry["poster"] = poster
    return entry


def _container_entry(variable: str) -> dict:
    return {"variable": variable, "path": f"{variable}.xue", "byteLength": 10, "crc32": "0badf00d"}


def _manifest(bundles: list[dict], *, model: str = "gfs", hours: int = 240) -> dict:
    source = source_spec(model)
    return build_bin_manifest(
        datetime(2026, 8, 14, 6, tzinfo=UTC),
        bundles=bundles,
        expected_hours=hours,
        model=source.manifest_model,
        product=source.product,
        require_core_variables=False,
    )


def _gfs_manifest() -> dict:
    poster = _poster("tmp2m", _metadata(grid=GLOBAL_POSTER_GRID, count=161, offsets=[*range(121), *range(123, 241, 3)]))
    both = {**_store_entry("prate"), "path": "prate.xue", "byteLength": 20, "crc32": "0badf00d"}
    both["video"] = {
        "streamPath": "prate.h264",
        "indexPath": "prate.h264.index.json",
        "byteLength": 26_638_477,
        "crc32": "bf0e3d46",
        "codec": "avc1.f40028",
        "width": 1440,
        "height": 721,
        "gop": 6,
        "frameCount": 160,
        "metadataJson": _metadata(
            grid={**GLOBAL_POSTER_GRID, "width": 1440, "height": 721, "longitudeStep": 0.25, "latitudeStep": -0.25},
            count=160,
            offsets=[*range(1, 121), *range(123, 241, 3)],
        ),
    }
    return _manifest([_store_entry("tmp2m", poster=poster), both, _container_entry("wind10m"), _container_entry("mystery")])


class RunItemTests(unittest.TestCase):
    source = source_spec("gfs")

    def setUp(self) -> None:
        self.manifest = _gfs_manifest()
        self.item = stac.run_item(
            self.manifest, "dbf3a790", source=self.source, manifest_relative_path="gfs.2026081406/manifest.json"
        )

    def test_the_item_is_the_run(self) -> None:
        self.assertEqual(self.item["id"], "gfs.2026081406")
        self.assertEqual(self.item["collection"], "gfs")
        self.assertEqual(self.item["stac_version"], stac.STAC_VERSION)
        properties = self.item["properties"]
        self.assertEqual(properties["forecast:reference_datetime"], "2026-08-14T06:00:00Z")
        self.assertEqual(properties["forecast:horizon"], "PT240H")
        self.assertIs(properties["forecast:perturbed"], False)
        # The union of every axis the metadata carries: the analysis from the
        # poster, the rate's first step from the video, f240 from both.
        self.assertEqual(properties["start_datetime"], "2026-08-14T06:00:00Z")
        self.assertEqual(properties["end_datetime"], "2026-08-24T06:00:00Z")
        self.assertEqual(properties["datetime"], properties["start_datetime"])
        self.assertEqual(properties["xue:frameCount"], 161)
        self.assertIsNone(properties["cube:dimensions"]["time"]["step"])

    def test_the_grid_is_read_off_the_video_first(self) -> None:
        # The video's metadata carries the full grid; a poster's is decimated.
        dimensions = self.item["properties"]["cube:dimensions"]
        self.assertEqual(self.item["bbox"], [-180.0, -90.0, 180.0, 90.0])
        self.assertEqual(dimensions["x"]["step"], 0.25)
        self.assertEqual(dimensions["y"]["extent"], [-90.0, 90.0])
        self.assertEqual(self.item["geometry"]["type"], "Polygon")

    def test_a_poster_grid_is_halved_back(self) -> None:
        poster = _poster("tmp2m", _metadata(grid=REGIONAL_POSTER_GRID, count=19))
        item = stac.run_item(
            _manifest([_store_entry("tmp2m", poster=poster)], model="hrrr", hours=18),
            "0badf00d",
            source=source_spec("hrrr"),
            manifest_relative_path="hrrr.2026081406/manifest.json",
        )
        dimensions = item["properties"]["cube:dimensions"]
        self.assertEqual(item["bbox"], [-134.1, 21.12, -60.9, 52.62])
        self.assertAlmostEqual(dimensions["x"]["step"], 0.03)
        self.assertEqual(dimensions["time"]["step"], "PT1H")
        self.assertEqual(item["properties"]["forecast:horizon"], "PT18H")

    def test_assets_are_one_per_artifact(self) -> None:
        assets = self.item["assets"]
        self.assertEqual(assets["manifest"]["href"], "manifest.json?v=dbf3a790")
        self.assertEqual(assets["manifest"]["roles"], ["metadata"])
        store = assets["tmp2m"]
        self.assertEqual(store["href"], "tmp2m.zarr")
        self.assertEqual(store["type"], stac.ZARR_MEDIA_TYPE)
        self.assertEqual(store["xue:crc32"], "760cef95")
        self.assertEqual(store["file:size"], 12_347_075)
        self.assertNotIn("file:checksum", store, "a store is many objects; its crc32 is its root document's")
        self.assertEqual(assets["tmp2m-half"]["href"], "tmp2m.half.zarr")
        self.assertEqual(assets["tmp2m-half"]["xue:tier"], "half")
        self.assertEqual(assets["tmp2m-half"]["xue:grid"], {"width": 720, "height": 361})
        self.assertEqual(assets["tmp2m-poster"]["roles"], ["overview"])
        # A container beside a store is its own asset, not an alternate of it.
        self.assertEqual(assets["prate"]["type"], stac.ZARR_MEDIA_TYPE)
        self.assertEqual(assets["prate-xue"]["href"], "prate.xue")
        self.assertEqual(assets["prate-xue"]["file:checksum"], "b202040badf00d")
        self.assertEqual(assets["prate-video"]["type"], stac.VIDEO_MEDIA_TYPE)
        self.assertEqual(assets["prate-video"]["xue:codec"], "avc1.f40028")
        self.assertEqual(assets["prate-video-index"]["href"], "prate.h264.index.json")
        # A container-only entry is the data asset under the bundle's key.
        self.assertEqual(assets["wind10m"]["xue:kind"], "container")
        self.assertEqual(assets["wind10m"]["file:checksum"], stac.CRC32_MULTIHASH_PREFIX + "0badf00d")
        self.assertNotIn("wind10m-xue", assets)

    def test_variables_are_the_arrays(self) -> None:
        variables = self.item["properties"]["cube:variables"]
        self.assertEqual(variables["tmp2m"]["unit"], "°C")
        self.assertEqual(variables["tmp2m"]["dimensions"], ["time", "y", "x"])
        self.assertNotIn("wind10m", variables, "a vector bundle is its two component arrays")
        self.assertEqual(variables["ugrd10m"]["xue:bundle"], "wind10m")
        self.assertEqual(variables["vgrd10m"]["unit"], "m/s")
        # A bundle the registry does not know is listed by name alone.
        self.assertEqual(variables["mystery"], {"dimensions": ["time", "y", "x"], "type": "data"})

    def test_links_climb_to_the_root(self) -> None:
        rels = {link["rel"]: link["href"] for link in self.item["links"]}
        self.assertEqual(rels["root"], "../catalog.json")
        self.assertEqual(rels["parent"], "../gfs/collection.json")
        self.assertEqual(rels["collection"], "../gfs/collection.json")
        round_item = stac.run_item(
            self.manifest, "dbf3a790", source=self.source, manifest_relative_path="gfs.2026081406/1455/manifest.json"
        )
        self.assertEqual(round_item["id"], "gfs.2026081406.1455")
        self.assertEqual(round_item["links"][0]["href"], "../../catalog.json")

    def test_a_manifest_without_metadata_has_no_geometry(self) -> None:
        item = stac.run_item(
            _manifest([_container_entry("tmp2m"), _container_entry("prate")]),
            "0badf00d",
            source=self.source,
            manifest_relative_path="gfs.2026081406/manifest.json",
        )
        self.assertIsNone(item["geometry"])
        self.assertNotIn("bbox", item)
        self.assertEqual(list(item["properties"]["cube:dimensions"]), ["time"])
        self.assertIsNone(item["properties"]["xue:frameCount"])
        # The declared span stands in for the axis.
        self.assertEqual(item["properties"]["end_datetime"], "2026-08-24T06:00:00Z")

    def test_an_observation_carries_no_forecast_fields(self) -> None:
        poster = _poster("cref", _metadata(grid=REGIONAL_POSTER_GRID, unit_seconds=120, count=117))
        item = stac.run_item(
            _manifest([_store_entry("cref", poster=poster)], model="mrms", hours=4),
            "0badf00d",
            source=source_spec("mrms"),
            manifest_relative_path="mrms.2026081406/1315/manifest.json",
        )
        self.assertNotIn(stac.FORECAST_EXTENSION, item["stac_extensions"])
        self.assertNotIn("forecast:reference_datetime", item["properties"])
        self.assertIs(item["properties"]["xue:observation"], True)
        self.assertEqual(item["properties"]["cube:dimensions"]["time"]["step"], "PT2M")
        self.assertEqual(item["properties"]["end_datetime"], "2026-08-14T09:52:00Z")

    def test_the_manifest_must_be_a_run_manifest(self) -> None:
        with self.assertRaises(stac.StacError):
            stac.run_item(self.manifest, "dbf3a790", source=self.source, manifest_relative_path="manifest.json")


class CollectionAndCatalogTests(unittest.TestCase):
    def test_the_collection_mirrors_the_pointer(self) -> None:
        source = source_spec("gfs")
        item = stac.run_item(_gfs_manifest(), "dbf3a790", source=source, manifest_relative_path="gfs.2026081406/manifest.json")
        collection = stac.source_collection(source, item, "gfs.2026081406/item.json")
        self.assertEqual(collection["id"], "gfs")
        self.assertEqual(collection["license"], "other")
        self.assertEqual(collection["extent"]["spatial"]["bbox"], [[-180.0, -90.0, 180.0, 90.0]])
        self.assertEqual(collection["extent"]["temporal"]["interval"], [["2026-08-14T06:00:00Z", "2026-08-24T06:00:00Z"]])
        links = {link["rel"]: link["href"] for link in collection["links"]}
        # The live Item at the stable path beside the Collection, not the
        # run's own, which dies with the run.
        self.assertEqual(links["item"], "item.json")
        self.assertEqual(links["latest-version"], "item.json")
        self.assertEqual(links["alternate"], "../gfs.2026081406/item.json")
        self.assertEqual(links["xue:pointer"], "../latest.json")
        self.assertEqual(links["root"], "../catalog.json")
        self.assertIn("license", links)
        self.assertEqual(collection["summaries"]["forecast:reference_datetime"], ["2026-08-14T06:00:00Z"])

    def test_the_live_item_is_the_run_item_relocated(self) -> None:
        item = stac.run_item(
            _gfs_manifest(), "dbf3a790", source=source_spec("gfs"), manifest_relative_path="gfs.2026081406/manifest.json"
        )
        live = stac.relocate_item(item, from_dir="gfs.2026081406", to_dir="gfs")
        self.assertEqual(live["id"], item["id"])
        self.assertEqual(live["properties"], item["properties"])
        self.assertEqual(live["assets"]["tmp2m"]["href"], "../gfs.2026081406/tmp2m.zarr")
        self.assertEqual(live["assets"]["manifest"]["href"], "../gfs.2026081406/manifest.json?v=dbf3a790")
        links = {link["rel"]: link["href"] for link in live["links"]}
        self.assertEqual(links, {"root": "../catalog.json", "parent": "collection.json", "collection": "collection.json"})
        # A round's Item is one directory deeper; the relocation climbs it.
        deep = stac.run_item(
            _gfs_manifest(), "dbf3a790", source=source_spec("gfs"), manifest_relative_path="gfs.2026081406/1455/manifest.json"
        )
        live = stac.relocate_item(deep, from_dir="gfs.2026081406/1455", to_dir="gfs")
        self.assertEqual(live["assets"]["tmp2m"]["href"], "../gfs.2026081406/1455/tmp2m.zarr")
        self.assertEqual(live["links"][0]["href"], "../catalog.json")
        # Everything else is untouched: the two documents differ only in hrefs.
        self.assertEqual(stac.relocate_item(live, from_dir="gfs", to_dir="gfs.2026081406/1455"), deep)

    def test_a_source_without_a_feed_has_no_collection(self) -> None:
        # Every registered source has a feed now; the rule is checked on a
        # copy of the radar source with its pointer taken off.
        import dataclasses

        source = dataclasses.replace(source_spec("cma"), latest_filename=None)
        item = stac.run_item(_gfs_manifest(), "dbf3a790", source=source_spec("gfs"), manifest_relative_path="gfs.2026081406/manifest.json")
        with self.assertRaises(stac.StacError):
            stac.source_collection(source, item, "gfs.2026081406/item.json")

    def test_the_catalog_lists_every_live_source_and_the_showcase(self) -> None:
        catalog = stac.root_catalog()
        children = [link["href"] for link in catalog["links"] if link["rel"] == "child"]
        self.assertEqual(
            children,
            [f"{source.id}/collection.json" for source in SOURCES.values() if source.live] + ["showcase/collection.json"],
        )
        self.assertEqual(stac.root_catalog(), catalog, "a pure function of the registry")

    def test_every_source_has_catalog_prose(self) -> None:
        for source in SOURCES.values():
            with self.subTest(source=source.id):
                prose = stac._source_prose(source)
                self.assertTrue(prose["title"] and prose["description"] and prose["license"])
                if prose["license"] == "other":
                    self.assertTrue(
                        any(link["rel"] == "license" for link in prose["links"]) or source.id == "cma",
                        "an `other` license needs a link",
                    )


class CaseItemTests(unittest.TestCase):
    def _entry(self, **overrides: object) -> dict:
        entry = {
            "id": "ida-2021",
            "title": {"en": "Hurricane Ida on radar", "zh": "艾达"},
            "summary": {"en": "Landfall.", "zh": "登陆。"},
            "modelId": "mrms",
            "model": source_spec("mrms").manifest_model,
            "product": source_spec("mrms").product,
            "run": "2021082912",
            "runTime": "2021-08-29T12:00:00Z",
            "forecastHours": 12,
            "bbox": [-95.0, 27.0, -85.0, 33.0],
            "dataBbox": [-95.01, 26.99, -84.99, 33.01],
            "grid": {"width": 501, "height": 301},
            "variables": ["cref"],
            "defaultVariable": "cref",
            "manifestPath": "showcase/ida-2021/manifest.json",
            "manifestCrc32": "0badf00d",
            "byteLength": 1,
            "eventTime": "2021-08-29T16:55:00Z",
            "tags": ["hurricane", "radar"],
            "credit": "NOAA MRMS",
        }
        entry.update(overrides)
        return entry

    def test_the_case_is_an_item_of_the_showcase(self) -> None:
        source = source_spec("mrms")
        poster = _poster(
            "cref",
            _metadata(
                grid={**REGIONAL_POSTER_GRID, "width": 251, "height": 151, "firstLongitude": -95.01, "firstLatitude": 33.01, "longitudeStep": 0.04, "latitudeStep": -0.04},
                unit_seconds=120,
                count=361,
            ),
        )
        manifest = build_bin_manifest(
            datetime(2021, 8, 29, 12, tzinfo=UTC),
            bundles=[_store_entry("cref", poster=poster)],
            expected_hours=12,
            model=source.manifest_model,
            product=source.product,
            require_core_variables=False,
        )
        item = stac.case_item(self._entry(), manifest, "0badf00d")
        self.assertEqual(item["id"], "ida-2021")
        self.assertEqual(item["collection"], "showcase")
        self.assertEqual(item["bbox"], [-95.01, 26.99, -84.99, 33.01])
        properties = item["properties"]
        self.assertEqual(properties["datetime"], "2021-08-29T16:55:00Z", "a case is found by its event")
        self.assertEqual(properties["start_datetime"], "2021-08-29T12:00:00Z")
        self.assertEqual(properties["end_datetime"], "2021-08-30T00:00:00Z")
        self.assertEqual(properties["title"], "Hurricane Ida on radar")
        self.assertEqual(properties["xue:title"]["zh"], "艾达")
        self.assertEqual(properties["xue:tags"], ["hurricane", "radar"])
        self.assertEqual(properties["license"], "other")
        self.assertEqual(properties["cube:dimensions"]["x"]["step"], 0.02)
        self.assertNotIn("forecast:reference_datetime", properties)
        self.assertEqual(item["links"][0]["href"], "../../catalog.json")
        self.assertEqual(item["assets"]["cref"]["href"], "cref.zarr")

    def test_a_window_across_the_antimeridian_is_two_polygons(self) -> None:
        source = source_spec("mrms")
        manifest = build_bin_manifest(
            datetime(2021, 8, 29, 12, tzinfo=UTC),
            bundles=[_container_entry("cref")],
            expected_hours=12,
            model=source.manifest_model,
            product=source.product,
            require_core_variables=False,
        )
        item = stac.case_item(self._entry(dataBbox=[170.0, -20.0, -170.0, 20.0], grid={"width": 21, "height": 21}), manifest, "0badf00d")
        self.assertEqual(item["geometry"]["type"], "MultiPolygon")
        self.assertEqual(item["bbox"], [170.0, -20.0, -170.0, 20.0])
        self.assertEqual(item["properties"]["cube:dimensions"]["x"]["step"], 1.0)


class WritingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="xue-stac-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_run_documents_land_beside_the_manifest_and_at_the_root(self) -> None:
        manifest_path = self.root / "gfs.2026081406" / "manifest.json"
        manifest_path.parent.mkdir()
        manifest_path.write_text(json.dumps(_gfs_manifest()), encoding="utf-8")
        written = stac.write_run_documents(self.root, source=source_spec("gfs"), manifest_path=manifest_path)
        self.assertEqual(Path(written["item"]), self.root / "gfs.2026081406" / "item.json")
        self.assertEqual(Path(written["liveItem"]), self.root / "gfs" / "item.json")
        self.assertEqual(Path(written["collection"]), self.root / "gfs" / "collection.json")
        self.assertEqual(Path(written["catalog"]), self.root / "catalog.json")
        item = json.loads(Path(written["item"]).read_text(encoding="utf-8"))
        collection = json.loads(Path(written["collection"]).read_text(encoding="utf-8"))
        self.assertEqual(item["assets"]["manifest"]["href"].split("=")[1], item["assets"]["manifest"]["xue:crc32"])
        self.assertEqual(collection["xue:live"], "gfs.2026081406")
        # Writing again from the same manifest changes nothing.
        before = Path(written["item"]).stat().st_mtime_ns
        stac.write_run_documents(self.root, source=source_spec("gfs"), manifest_path=manifest_path)
        self.assertEqual(Path(written["item"]).stat().st_mtime_ns, before)

    def test_a_partial_manifest_is_not_a_run(self) -> None:
        part = self.root / "gfs.2026081406" / "manifest.part.tmp2m.json"
        part.parent.mkdir()
        part.write_text(json.dumps(_gfs_manifest()), encoding="utf-8")
        with self.assertRaises(stac.StacError):
            stac.write_run_documents(self.root, source=source_spec("gfs"), manifest_path=part)


@unittest.skipIf(pystac is None, "pystac is not installed")
class PystacRoundTripTests(unittest.TestCase):
    def test_pystac_reads_what_is_written(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="xue-stac-pystac-"))
        self.addCleanup(shutil.rmtree, root, True)
        manifest_path = root / "gfs.2026081406" / "manifest.json"
        manifest_path.parent.mkdir()
        manifest_path.write_text(json.dumps(_gfs_manifest()), encoding="utf-8")
        stac.write_run_documents(root, source=source_spec("gfs"), manifest_path=manifest_path)
        catalog = pystac.Catalog.from_file(str(root / "catalog.json"))
        collection = catalog.get_child("gfs")
        assert collection is not None
        items = list(collection.get_items())
        self.assertEqual([item.id for item in items], ["gfs.2026081406"])
        self.assertEqual(items[0].assets["tmp2m"].media_type, stac.ZARR_MEDIA_TYPE)
        self.assertEqual(items[0].bbox, [-180.0, -90.0, 180.0, 90.0])
        # Walked from the Collection, the Item is the live one, and its
        # assets resolve into the run directory.
        self.assertEqual(items[0].get_self_href(), str(root / "gfs" / "item.json"))
        self.assertEqual(items[0].assets["tmp2m"].get_absolute_href(), str(root / "gfs.2026081406" / "tmp2m.zarr"))


if __name__ == "__main__":
    unittest.main()

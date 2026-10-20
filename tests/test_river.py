"""River flow (``xuebuild/river``, ``docs/river.md`` and the network store
of ``docs/zarr-profile.md``): a synthetic 20-reach, 8-frame network
written and read back chunk by chunk, the Hilbert order and the WKB
helpers, the GeoParquet's row groups against the store's feature chunks, a
whole network and a whole grid case built from a work directory, the
index validator on malformed indexes, and the showcase's ``river`` block.

Fixtures: ``tests/fixtures/river/`` (see its README). GeoParquet needs
pyarrow; the tests that write it skip without it.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import shutil
import struct
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from tests._support import ClassTempRoot
from xuebuild import zarrcodec, zarrstore, zstdcli
from xuebuild.common import crc32_hex
from xuebuild.pointproduct import encode_json
from xuebuild.river import geoparquet, network
from xuebuild.river.build import build, load_definition, series_line
from xuebuild.river.schema import RiverIndexError, read_index, validate_index, validate_line
from xuebuild.showcase import ShowcaseError, _catalog_river, parse_case, validate_catalog_entry

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "river"
HAS_PYARROW = importlib.util.find_spec("pyarrow") is not None
RUN = datetime(2025, 7, 4, 0, tzinfo=UTC)
FRAMES, FEATURES, ROWS = 8, 20, 8


def wkb_line(coordinates: list[list[float]]) -> bytes:
    return struct.pack("<BII", 1, 2, len(coordinates)) + b"".join(struct.pack("<dd", x, y) for x, y in coordinates)


def synthetic_values() -> np.ndarray:
    """``[8, 20]`` m³/s: a flood wave down the reaches, one lake reach
    (NaN throughout), one dry cell and one beyond the codebook."""
    t = np.arange(FRAMES, dtype=np.float64)[:, None]
    f = np.arange(FEATURES, dtype=np.float64)[None, :]
    values = 0.5 + 900.0 * np.exp(-((t - f / 4) ** 2) / 3.0)
    values[:, 13] = np.nan
    values[2, 4] = 0.0
    values[5, 9] = 250000.0
    return values.astype(np.float32)


def geometry_for(path: Path, rows: int = ROWS, count: int = FEATURES) -> dict[str, object]:
    return network.geometry_descriptor(path, rows, count)


class CodebookTests(unittest.TestCase):
    def test_streamflow_round_trips_within_a_code(self) -> None:
        values = np.array([0.0, 0.005, 0.02, 1.0, 37.5, 8919.8, 99999.0, np.nan, -0.1])
        codes = network.quantize(values)
        self.assertEqual(codes[0], 0)
        self.assertEqual(codes[1], 0)
        self.assertEqual(codes[-2], 255)
        self.assertEqual(codes[-1], 0)
        decoded = network.dequantize(codes)
        # Below 1 m³/s log1p is close to linear, so the grid is about 0.046
        # m³/s a code there; the 4.7 % relative spacing holds from 1 m³/s up.
        self.assertLess(abs(decoded[2] - values[2]), 0.05)
        for value, back in zip(values[3:7], decoded[3:7]):
            self.assertLess(abs(back - value) / value, 0.025)
        self.assertTrue(math.isnan(decoded[-2]))

    def test_overflow_has_its_own_code(self) -> None:
        self.assertEqual(network.quantize(np.array([250000.0]))[0], 254)

    def test_the_codebook_is_the_profiles(self) -> None:
        self.assertEqual(
            network.VARIABLE["quantization"],
            {
                "type": "log1p", "trace": 0.01, "scale": 1.0, "maximum": 100000.0, "minimumCode": 1,
                "maximumCode": 253, "zeroCode": 0, "overflowCode": 254, "nodataCode": 255,
            },
        )


class NetworkStoreTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-river-store-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.parquet = cls.root / "reaches.parquet"
        cls.parquet.write_bytes(b"PAR1 stand-in for the geometry PAR1")
        cls.values = synthetic_values()
        cls.ids = np.arange(FEATURES, dtype=np.int64) * 3 + 1000
        cls.baseflow = np.linspace(0.1, 2.0, FEATURES, dtype=np.float32)
        cls.baseflow[13] = np.nan
        cls.times = [RUN + timedelta(hours=1 + i) for i in range(FRAMES)]
        cls.reports = {}
        for delta in (True, False):
            cls.reports[delta] = network.write_network_store(
                cls.root / f"{'delta' if delta else 'plain'}.zarr",
                model="NWM v3.0",
                product="short_range",
                run_time=RUN,
                valid_times=cls.times,
                values=cls.values,
                feature_ids=cls.ids,
                baseflow=cls.baseflow,
                geometry=geometry_for(cls.parquet),
                delta=delta,
            )

    def test_every_chunk_reads_back_through_the_index(self) -> None:
        expected = network.quantize(self.values)
        for delta, report in self.reports.items():
            with self.subTest(delta=delta):
                shard = (report.path / "streamflow" / "c" / "0" / "0").read_bytes()
                time_chunks, feature_chunks = 2, 3
                pairs = zarrstore._parse_index(
                    zarrstore.shard_index_bytes(shard, time_chunks * feature_chunks, "end"), time_chunks * feature_chunks
                )
                self.assertEqual(len(shard), sum(nbytes for _offset, nbytes in pairs) + 16 * 6 + 4)
                for position, (offset, nbytes) in enumerate(pairs):
                    chunk, group = divmod(position, feature_chunks)
                    raw = zstdcli.decompress(shard[offset : offset + nbytes], expected_length=6 * ROWS)
                    self.assertTrue(zstdcli.frame_has_checksum(shard[offset : offset + nbytes]))
                    block = np.frombuffer(raw, dtype=np.uint8).reshape(6, ROWS)
                    if delta:
                        block = np.cumsum(block, axis=0, dtype=np.uint8)
                    want = np.full((6, ROWS), 255, dtype=np.uint8)
                    part = expected[chunk * 6 : chunk * 6 + 6, group * ROWS : group * ROWS + ROWS]
                    want[: part.shape[0], : part.shape[1]] = part
                    np.testing.assert_array_equal(block, want)
                np.testing.assert_array_equal(network.read_codes(report.path), expected)

    def test_the_array_is_cut_as_the_profile_says(self) -> None:
        array = zarrstore.read_json(self.reports[True].path / "streamflow" / "zarr.json")
        self.assertEqual(array["shape"], [FRAMES, FEATURES])
        self.assertEqual(array["dimension_names"], ["time", "feature"])
        self.assertEqual(array["chunk_grid"]["configuration"]["chunk_shape"], [12, 24])
        sharding = array["codecs"][0]
        self.assertEqual(sharding["name"], "sharding_indexed")
        self.assertEqual(sharding["configuration"]["chunk_shape"], [6, ROWS])
        self.assertEqual([codec["name"] for codec in sharding["configuration"]["codecs"]], ["xue.delta", "bytes", "zstd"])
        self.assertEqual(sharding["configuration"]["index_codecs"][1], {"name": "crc32c"})
        self.assertEqual(sharding["configuration"]["index_location"], "end")
        self.assertEqual(array["fill_value"], 255)
        plain = zarrstore.read_json(self.reports[False].path / "streamflow" / "zarr.json")
        self.assertEqual([codec["name"] for codec in plain["codecs"][0]["configuration"]["codecs"]], ["bytes", "zstd"])

    def test_the_group_carries_the_network_block(self) -> None:
        report = self.reports[True]
        root = (report.path / "zarr.json").read_bytes()
        self.assertEqual(report.crc32, crc32_hex(root))
        self.assertEqual(report.byte_length, zarrstore.store_byte_length(report.path))
        block = network.read_group(report.path)
        self.assertEqual(block["runTime"], "2025-07-04T00:00:00Z")
        self.assertEqual(block["time"], {"unitSeconds": 3600, "firstFrameOffset": 1, "frameCount": 8, "frameStep": 1})
        self.assertEqual(block["geometry"]["featureCount"], FEATURES)
        self.assertEqual(network.frame_times(block), self.times)
        group = json.loads(root)
        self.assertEqual(set(group["attributes"]), {"xue_network"})
        self.assertEqual(set(group["consolidated_metadata"]["metadata"]), {"streamflow", "feature_id", "time", "baseflow"})

    def test_the_side_arrays(self) -> None:
        path = self.reports[True].path
        np.testing.assert_array_equal(network.read_side(path, "feature_id"), self.ids)
        np.testing.assert_array_equal(network.read_side(path, "time"), [int(moment.timestamp()) for moment in self.times])
        np.testing.assert_array_equal(network.read_side(path, "baseflow"), self.baseflow)
        self.assertEqual(zarrstore.read_json(path / "baseflow" / "zarr.json")["fill_value"], "NaN")

    def test_another_branchs_block_is_refused(self) -> None:
        broken = self.root / "broken.zarr"
        shutil.copytree(self.reports[True].path, broken)
        group = json.loads((broken / "zarr.json").read_text())
        group["attributes"]["xue_polar"] = {"version": 1}
        (broken / "zarr.json").write_text(json.dumps(group))
        with self.assertRaises(network.NetworkStoreError):
            network.read_group(broken)
        group["attributes"] = {"xue_network": {**group["attributes"]["xue_network"], "version": 2}}
        (broken / "zarr.json").write_text(json.dumps(group))
        with self.assertRaises(network.NetworkStoreError):
            network.read_group(broken)

    def test_bad_inputs_are_refused(self) -> None:
        common = dict(
            model="m", product="p", run_time=RUN, feature_ids=self.ids, baseflow=self.baseflow,
            geometry=geometry_for(self.parquet),
        )
        with self.assertRaises(network.NetworkStoreError):
            network.write_network_store(self.root / "x.zarr", valid_times=self.times[::-1], values=self.values, **common)
        with self.assertRaises(network.NetworkStoreError):
            network.write_network_store(self.root / "x.zarr", valid_times=self.times[:3], values=self.values, **common)
        with self.assertRaises(network.NetworkStoreError):
            network.write_network_store(
                self.root / "x.zarr", valid_times=self.times, values=self.values,
                **{**common, "geometry": geometry_for(self.parquet, count=FEATURES + 1)},
            )

    @unittest.skipUnless(zarrcodec.available(), "zarr-python is not installed (uv sync --group zarr)")
    def test_zarr_python_reads_the_store(self) -> None:
        import zarr

        zarrcodec.register()
        for delta, report in self.reports.items():
            with self.subTest(delta=delta):
                group = zarr.open_group(report.path, mode="r")
                np.testing.assert_array_equal(group["streamflow"][:], network.quantize(self.values))
                np.testing.assert_array_equal(group["feature_id"][:], self.ids)
                self.assertEqual(dict(group.attrs)["xue_network"]["version"], 1)


class HilbertAndWkbTests(unittest.TestCase):
    def test_the_first_order_curve(self) -> None:
        cells = [(0, 0), (0, 1), (1, 1), (1, 0)]
        distance = geoparquet.hilbert_index(np.array([c[0] for c in cells]), np.array([c[1] for c in cells]), order=1)
        self.assertEqual(distance.tolist(), [0, 1, 2, 3])

    def test_the_curve_is_a_walk_of_neighbours(self) -> None:
        side = 8
        x, y = np.meshgrid(np.arange(side), np.arange(side), indexing="ij")
        distance = geoparquet.hilbert_index(x.ravel(), y.ravel(), order=3)
        self.assertEqual(sorted(distance.tolist()), list(range(side * side)))
        order = np.argsort(distance)
        steps = np.abs(np.diff(x.ravel()[order])) + np.abs(np.diff(y.ravel()[order]))
        self.assertTrue((steps == 1).all())

    def test_linestrings_and_their_boxes(self) -> None:
        coordinates = [[-99.123456789, 30.1], [-99.0, 30.2], [-98.9, 30.15]]
        parsed = geoparquet.linestring_coordinates(wkb_line(coordinates))
        np.testing.assert_array_equal(parsed, coordinates)
        xmin, ymin, xmax, ymax = geoparquet.bounds32(parsed)
        self.assertLessEqual(xmin, -99.123456789)
        self.assertLessEqual(ymin, 30.1)
        self.assertGreaterEqual(xmax, -98.9)
        self.assertGreaterEqual(ymax, 30.2)
        self.assertAlmostEqual(geoparquet.centroid(np.array([[0.0, 0.0], [2.0, 0.0]]))[0], 1.0)

    def test_other_geometries_are_refused(self) -> None:
        point = struct.pack("<BIdd", 1, 1, 0.0, 0.0)
        with self.assertRaises(geoparquet.GeoParquetError):
            geoparquet.linestring_coordinates(point)
        with self.assertRaises(geoparquet.GeoParquetError):
            geoparquet.linestring_coordinates(wkb_line([[0.0, 0.0], [1.0, 1.0]])[:-1])


def write_work(root: Path, *, kind: str) -> Path:
    """A work directory in the case adapters' shape, from the fixtures."""
    work = root / f"work-{kind}"
    if work.exists():
        shutil.rmtree(work)
    (work / "gauges").mkdir(parents=True)
    for name in ("gauge.csv", "gauge.stage.csv", "outflow.csv"):
        shutil.copyfile(FIXTURES / name, work / "gauges" / name)
    (work / "extent").mkdir()
    shutil.copyfile(FIXTURES / "extent.geojson", work / "extent" / "aoi.geojson")
    reaches = json.loads((FIXTURES / "reaches.json").read_text())["reaches"]
    gauge = {"id": "08165500", "name": "Test gauge", "lon": -98.85, "lat": 30.03, "source": "USGS NWIS", "series": "gauges/gauge.csv"}
    case: dict[str, object] = {"id": "test", "kind": kind, "credits": ["Synthetic test data"]}
    if kind == "network":
        records = geoparquet.Reaches(
            feature_id=np.array([reach["feature_id"] for reach in reaches], dtype=np.int64),
            order=np.array([reach["order"] for reach in reaches], dtype=np.int8),
            gage=[reach["gage"] for reach in reaches],
            wkb=[wkb_line(reach["coordinates"]) for reach in reaches],
        )
        # The work file as an adapter writes it: one row group, feature_id order.
        geoparquet.write_reaches(work / "features.parquet", records, row_group_rows=len(reaches))
        (work / "frames").mkdir()
        (work / "static").mkdir()
        values = synthetic_values()
        frames = []
        for index in range(FRAMES):
            name = f"frames/ana_{index:02d}.npy"
            np.save(work / name, values[index])
            frames.append({"track": "analysis", "cycle": None, "valid": f"2025-07-04T{index:02d}:00:00Z", "lead_h": 0, "file": name})
            name = f"frames/fc_{index:02d}.npy"
            np.save(work / name, values[index] * 0.5)
            frames.append({"track": "forecast", "cycle": "2025-07-04T02:00:00Z", "valid": f"2025-07-04T{index + 3:02d}:00:00Z", "lead_h": index + 1, "file": name})
        np.save(work / "static" / "baseflow.npy", np.full(FEATURES, 1.5, dtype=np.float32))
        case.update(
            features={"file": "features.parquet"},
            baseflow={"file": "static/baseflow.npy"},
            frames=frames,
            gauges=[{**gauge, "stage": "gauges/gauge.stage.csv", "stage_unit": "ft", "value_unit": "m3/s", "feature_id": 1021,
                     "obs_notes": {"discharge_peak": {"time": "2025-07-04T01:00:00Z", "value": 120.25, "qualifiers": "A,e"}}}],
        )
    else:
        case.update(
            gauges=[{**gauge, "id": "dam", "cell_lon": -98.851, "cell_lat": 30.031, "series_extra": {"outflow": "gauges/outflow.csv"},
                     "reported": [{"what": "peak inflow", "time": "2025-07-04T01:00:00Z", "value": 120.25, "unit": "m3/s", "source": "Agency, report"},
                                  {"what": "last stage", "time": None, "value": 5.5, "unit": "m", "source": "Agency"}]}],
            extent=[{"path": "extent/aoi.geojson", "at": "2025-07-05T10:00:00Z", "credit": "Test mapping service"}],
            disclaimer="Synthetic.",
        )
    (work / "case.json").write_text(json.dumps(case))
    return work


def write_definition(root: Path, kind: str, work: Path) -> Path:
    definition: dict[str, object] = {"id": f"test-{kind}", "kind": kind, "work": str(work), "output": str(root / kind / "river")}
    if kind == "network":
        definition.update(
            model="NWM v3.0",
            tracks=[
                {"id": "analysis", "kind": "analysis", "product": "analysis_assim"},
                {"id": "short_range-2025070402", "kind": "forecast", "product": "short_range", "cycle": "2025-07-04T02:00:00Z"},
            ],
            gauges=["08165500"],
        )
    else:
        definition.update(gauges=["dam"], extent=["extent/aoi.geojson"])
    path = root / f"{kind}.json"
    path.write_text(json.dumps(definition))
    return path


@unittest.skipUnless(HAS_PYARROW, "GeoParquet needs pyarrow")
class NetworkBuildTests(ClassTempRoot, unittest.TestCase):
    root_prefix = "xue-river-network-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        work = write_work(cls.root, kind="network")
        cls.definition = load_definition(write_definition(cls.root, "network", work))
        cls.index = build(cls.definition)
        cls.river = cls.root / "network" / "river"

    def test_the_index_validates_against_its_files(self) -> None:
        self.assertEqual(read_index(self.river / "index.json"), self.index)
        self.assertEqual([track["id"] for track in self.index["tracks"]], ["analysis", "short_range-2025070402"])
        self.assertEqual(self.index["tracks"][1]["runTime"], "2025-07-04T02:00:00Z")

    def test_row_groups_align_with_the_feature_chunks(self) -> None:
        import pyarrow.parquet as pq

        handle = pq.ParquetFile(self.river / "reaches.parquet")
        rows = [handle.metadata.row_group(group).num_rows for group in range(handle.metadata.num_row_groups)]
        self.assertEqual(rows, [FEATURES])  # 20 reaches in one 4096-row group
        report = geoparquet.write_reaches(self.root / "small.parquet", geoparquet.read_reaches(self.river / "reaches.parquet"), row_group_rows=ROWS)
        self.assertEqual(report.rows, FEATURES)
        small = pq.ParquetFile(report.path)
        self.assertEqual([small.metadata.row_group(g).num_rows for g in range(small.metadata.num_row_groups)], [8, 8, 4])
        self.assertEqual(report.footer_bytes, geoparquet.footer_length(report.path))
        geo = geoparquet.geo_metadata(report.path)
        self.assertEqual(geo["version"], "1.1.0")
        self.assertEqual(geo["columns"]["geometry"]["covering"]["bbox"]["xmin"], ["bbox", "xmin"])
        self.assertEqual(small.schema_arrow.names, list(geoparquet.COLUMNS))

    def test_the_store_feature_axis_is_the_parquet_row_order(self) -> None:
        ids = geoparquet.row_feature_ids(self.river / "reaches.parquet")
        self.assertNotEqual(ids.tolist(), sorted(ids.tolist()))  # Hilbert order, not feature_id order
        source = {1000 + 3 * i: i for i in range(FEATURES)}
        values = synthetic_values()
        for track in self.index["tracks"]:
            store = self.river / track["store"]["path"]
            np.testing.assert_array_equal(network.read_side(store, "feature_id"), ids)
            scale = 1.0 if track["kind"] == "analysis" else 0.5
            expected = network.quantize(values[:, [source[int(feature)] for feature in ids]] * scale)
            np.testing.assert_array_equal(network.read_codes(store), expected)

    def test_the_gauge_names_its_row_and_its_line(self) -> None:
        gauge = self.index["gauges"][0]
        ids = geoparquet.row_feature_ids(self.river / "reaches.parquet")
        self.assertEqual(int(ids[gauge["feature"]]), 1021)
        data = (self.river / "gauges.jsonl").read_bytes()
        span = gauge["series"]
        line = json.loads(data[span["offset"] : span["offset"] + span["length"]])
        self.assertEqual(line["id"], "08165500")
        self.assertEqual(line["time"][:3], ["2025-07-04T00:00:00Z", "2025-07-04T00:15:00Z", "2025-07-04T00:30:00Z"])
        self.assertEqual(line["value"][:4], [1.5, None, None, 120.25])
        self.assertEqual(line["stage"][:4], [3.1, None, 5.4, 12.75])
        self.assertEqual(line["stageUnit"], "ft")
        self.assertEqual(gauge["peaks"], [{"time": "2025-07-04T01:00:00Z", "value": 120.2, "note": "USGS, estimated"}])


class GridBuildTests(ClassTempRoot, unittest.TestCase):
    """A grid case writes no GeoParquet, so it needs no pyarrow; its index
    is also the base the malformed indexes are cut from."""

    root_prefix = "xue-river-grid-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        work = write_work(cls.root, kind="grid")
        cls.index = build(load_definition(write_definition(cls.root, "grid", work)))
        cls.river = cls.root / "grid" / "river"

    def test_the_grid_index(self) -> None:
        index = read_index(self.river / "index.json")
        self.assertNotIn("tracks", index)
        gauge = index["gauges"][0]
        self.assertNotIn("feature", gauge)
        self.assertEqual(gauge["sample"], [-98.851, 30.031])
        self.assertEqual(gauge["peaks"][1], {"value": 5.5, "unit": "m", "note": "last stage (Agency)"})
        data = (self.river / "gauges.jsonl").read_bytes()
        outflow = json.loads(data[gauge["outflow"]["offset"] : gauge["outflow"]["offset"] + gauge["outflow"]["length"]])
        self.assertEqual(outflow, {"id": "dam.outflow", "time": ["2025-07-04T00:30:00Z", "2025-07-04T01:30:00Z"], "value": [0.0, 40.5]})
        self.assertEqual(index["extent"][0]["crc32"], crc32_hex((FIXTURES / "extent.geojson").read_bytes()))
        self.assertEqual(index["disclaimer"], "Synthetic.")

    def write(self, payload: dict[str, object]) -> Path:
        path = self.river / "index.json"
        self.addCleanup(path.write_bytes, encode_json(self.index))
        path.write_bytes(encode_json(payload))
        return path

    def test_malformed_indexes_are_refused(self) -> None:
        gauge = self.index["gauges"][0]
        cases = {
            "version": {**self.index, "version": 2},
            "kind": {**self.index, "kind": "mesh"},
            "unknown key": {**self.index, "extra": 1},
            "tracks on grid": {**self.index, "tracks": []},
            "feature on grid": {**self.index, "gauges": [{**gauge, "feature": 0}]},
            "climbing path": {**self.index, "extent": [{**self.index["extent"][0], "path": "../extent/aoi.geojson"}]},
            "absolute path": {**self.index, "extent": [{**self.index["extent"][0], "path": "/extent/aoi.geojson"}]},
            "crc": {**self.index, "extent": [{**self.index["extent"][0], "crc32": "00000000"}]},
            "missing file": {**self.index, "extent": [{**self.index["extent"][0], "path": "extent/none.geojson"}]},
            "span off a line": {**self.index, "gauges": [{**gauge, "series": {**gauge["series"], "offset": 1}}]},
            "span on the other line": {**self.index, "gauges": [{**gauge, "series": gauge["outflow"]}]},
            "span path": {**self.index, "gauges": [{**gauge, "series": {**gauge["series"], "path": "other.jsonl"}}]},
            "peak time": {**self.index, "gauges": [{**gauge, "peaks": [{"time": "2025-07-04 01:00", "value": 1}]}]},
            "empty grid": {key: value for key, value in self.index.items() if key not in ("gauges", "extent")},
            "duplicate gauge": {**self.index, "gauges": [gauge, gauge]},
        }
        for label, payload in cases.items():
            with self.subTest(label), self.assertRaises(RiverIndexError):
                read_index(self.write(payload))

    def test_a_network_index_needs_its_geometry(self) -> None:
        geometry = {"path": "reaches.parquet", "byteLength": 10, "crc32": "0badf00d", "featureCount": 5, "rowGroupRows": 4096}
        track = {"id": "analysis", "kind": "analysis", "runTime": "2025-07-04T00:00:00Z",
                 "store": {"path": "analysis.zarr", "byteLength": 10, "crc32": "0badf00d"}}
        good = {**self.index, "kind": "network", "geometry": geometry, "tracks": [track],
                "gauges": [{**self.index["gauges"][0], "feature": 4}]}
        del good["gauges"][0]["sample"]
        validate_index(copy.deepcopy(good))
        for label, payload in {
            "no geometry": {key: value for key, value in good.items() if key != "geometry"},
            "no feature": {**good, "gauges": [{key: value for key, value in good["gauges"][0].items() if key != "feature"}]},
            "feature past the end": {**good, "gauges": [{**good["gauges"][0], "feature": 5}]},
            "track kind": {**good, "tracks": [{**track, "kind": "hindcast"}]},
            "store path": {**good, "tracks": [{**track, "store": {**track["store"], "path": "analysis"}}]},
        }.items():
            with self.subTest(label), self.assertRaises(RiverIndexError):
                validate_index(payload)

    def test_a_line_is_held_to_its_shape(self) -> None:
        validate_line(series_line("a", [("2025-07-04T00:00:00Z", 1.0)], [("2025-07-04T00:00:00Z", 2.0)], "ft"), "line")
        for label, line in {
            "lengths": {"id": "a", "time": ["2025-07-04T00:00:00Z"], "value": [1, 2]},
            "order": {"id": "a", "time": ["2025-07-04T01:00:00Z", "2025-07-04T00:00:00Z"], "value": [1, 2]},
            "stage without unit": {"id": "a", "time": ["2025-07-04T00:00:00Z"], "value": [1], "stage": [1]},
        }.items():
            with self.subTest(label), self.assertRaises(RiverIndexError):
                validate_line(line, "line")

    def test_the_showcase_catalogs_the_index(self) -> None:
        output = self.root / "data"
        case_dir = output / "showcase" / "test-grid"
        shutil.copytree(self.river, case_dir / "river", dirs_exist_ok=True)
        block = _catalog_river(output, {"id": "test-grid", "river": {"index": "river/index.json"}})
        data = (case_dir / "river" / "index.json").read_bytes()
        self.assertEqual(block, {"index": "river/index.json", "byteLength": len(data), "crc32": crc32_hex(data)})
        with self.assertRaises(ShowcaseError):
            _catalog_river(output, {"id": "test-grid", "river": {"index": "nowhere/index.json"}})


def localized(text: str) -> dict[str, str]:
    return {locale: text for locale in ("zh", "zh-Hant", "en", "ja", "ko", "de", "fr", "es", "pt", "tr", "ru")}


class ShowcaseRiverBlockTests(unittest.TestCase):
    def payload(self, **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "id": "river-case",
            "title": localized("River"),
            "summary": localized("River summary"),
            "model": "hrrr",
            "run": "2025070400",
            "hours": 18,
            "bbox": [-100.55, 29.0, -97.45, 31.0],
            "variables": ["prate", "cref"],
            "defaultVariable": "prate",
            "river": {"index": "river/index.json"},
        }
        payload.update(overrides)
        return payload

    def test_a_definition_with_a_river_block_validates(self) -> None:
        self.assertEqual(parse_case(self.payload()).river, {"index": "river/index.json"})

    def test_the_guadalupe_definition_validates(self) -> None:
        path = Path(__file__).resolve().parents[1] / "showcase" / "cases" / "guadalupe-2025.json"
        spec = parse_case(json.loads(path.read_text(encoding="utf-8")))
        self.assertEqual(spec.river, {"index": "river/index.json"})

    def test_a_bad_river_block_is_refused(self) -> None:
        for river in ({"index": "../river/index.json"}, {"index": "/river/index.json"}, {"index": "river/rivers.json"}, {"window": "river/index.json"}, "river/index.json"):
            with self.subTest(river=river), self.assertRaises(ShowcaseError):
                parse_case(self.payload(river=river))

    def test_a_catalog_row_carries_the_block(self) -> None:
        row = {
            "id": "river-case", "modelId": "hrrr", "model": "HRRR", "product": "p", "run": "2025070400",
            "runTime": "2025-07-04T00:00:00Z", "manifestPath": "showcase/river-case/manifest.json",
            "manifestCrc32": "0badf00d", "title": localized("t"), "summary": localized("s"),
            "bbox": [0, 0, 1, 1], "dataBbox": [0, 0, 1, 1], "variables": ["prate"], "defaultVariable": "prate",
            "forecastHours": 18, "river": {"index": "river/index.json", "byteLength": 10, "crc32": "0badf00d"},
        }
        validate_catalog_entry(row)
        with self.assertRaises(ShowcaseError):
            validate_catalog_entry({**row, "river": {"index": "river/index.json", "extra": 1}})


if __name__ == "__main__":
    unittest.main()

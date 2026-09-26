"""The Zarr v3 store derived from a bundle.

Held to the bundle the way the native encoder is held to the reference: the
store is another packaging of the same codes, so what is pinned is that a
NumPy-only read-back of every frame equals ``decode_plane``, in both codec
chains and with the shard index at either end; that the counts of chunks
whose compressed bytes equal the bundle's are exactly the ones the layout
predicts (every chunk covering one of the bundle's groups on an unclipped
tile, before the axis changes step); and — when zarr-python and xarray are
installed — that a generic Zarr client reads the same codes and dequantizes
the linear codebook to physical values on its own.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import warnings
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from xuebuild import binconvert, binformat, temporal, zarrcodec, zarrstore, zstdcli
from xuebuild.binconvert import GridInfo, build_metadata
from xuebuild.errors import BundleError, ManifestError
from xuebuild.manifest import validate_bin_manifest
from xuebuild.sources import source_spec

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_GRIB = REPOSITORY_ROOT / "tests" / "fixtures" / "gfs.2026081406.f000.crop.grib2"

# The same miniature GFS-shaped axis the golden fixture uses: thirteen hourly
# frames, then eight three-hourly ones. The bundle cuts its groups at the
# change of step ([0-5], [6-11], [12], [15-30], [33, 36]); the store's
# regular six-frame grid does not ([0-5], [6-11], [12-17], [18-20]), so the
# third store chunk straddles two of the bundle's groups and the last is
# padded. Only the first two store chunks coincide with a bundle group.
MIXED_HOURS = list(range(13)) + list(range(15, 37, 3))
# A grid no tile divides: 17 x 9 cells in 5 x 4 tiles leaves a two-column
# last tile column and a one-row last tile row, so six of the twelve tiles
# are unclipped.
GRID = GridInfo(
    width=17,
    height=9,
    first_longitude=-180.0,
    first_latitude=90.0,
    longitude_step=21.0,
    latitude_step=-20.0,
)
TILE = (5, 4)
UNCLIPPED_TILES = 6
COINCIDING_CHUNKS = 2
PREDICTORS = {1: binformat.PREDICTOR_PREVIOUS, 2: binformat.PREDICTOR_RAW}


def plane(hour: int, variable_id: int) -> np.ndarray:
    points = GRID.width * GRID.height
    codes = ((np.arange(points, dtype=np.uint32) * (3 + variable_id) + hour * 7) % 251).astype(np.uint8)
    if variable_id == 1:
        # A nodata cell, so the CF fill value has something to mask.
        codes[0] = 255
    return codes


def write_v2(path: Path, variable_ids: tuple[str, ...]) -> binformat.Bundle:
    numeric_ids = tuple(range(1, len(variable_ids) + 1))
    planes = {hour: {numeric_id: plane(hour, numeric_id) for numeric_id in numeric_ids} for hour in MIXED_HOURS}
    metadata = build_metadata(datetime(2026, 8, 14, 6, tzinfo=UTC), MIXED_HOURS, GRID, "quality", variable_ids)
    tiles = binformat.TileGeometry(GRID.width, GRID.height, *TILE)
    predictors = {numeric_id: PREDICTORS[numeric_id] for numeric_id in numeric_ids}
    variables, groups, raw_chunks = temporal.build_chunks(MIXED_HOURS, planes, tiles, predictors)
    chunks = []
    for entry, stored in raw_chunks:
        payload = zstdcli.compress(stored.tobytes())
        chunks.append(binformat.ChunkPayload(replace(entry, compressed_length=len(payload)), payload))
    binformat.write_bundle_v2(
        path,
        metadata,
        tile_width=TILE[0],
        tile_height=TILE[1],
        variables=variables,
        groups=groups,
        chunks=chunks,
    )
    return binformat.read_bundle(path)


def write_v1(path: Path) -> binformat.Bundle:
    """A plane-major bundle of RAW planes: the legacy container the exporter
    must still read, which carries no tiling of its own."""
    metadata = build_metadata(datetime(2026, 8, 14, 6, tzinfo=UTC), MIXED_HOURS, GRID, "quality", ("prate",))
    payloads = []
    for hour in MIXED_HOURS:
        codes = plane(hour, 2)
        payload = zstdcli.compress(codes.tobytes())
        entry = binformat.PlaneEntry(
            variable_id=1,
            predictor=binformat.PREDICTOR_RAW,
            compression=binformat.COMPRESSION_ZSTD,
            flags=binformat.FLAG_ZSTD_CHECKSUM,
            frame_offset=hour,
            dependency_offset=binformat.NO_DEPENDENCY,
            group_id=0,
            compressed_length=len(payload),
            data_offset=0,
            decoded_length=len(codes),
            crc32=binformat.crc32_plane(codes),
            minimum_code=int(codes.min()),
            maximum_code=int(codes.max()),
        )
        payloads.append(binformat.PlanePayload(entry, payload))
    binformat.write_bundle(path, metadata, payloads)
    return binformat.read_bundle(path)


def expected_codes(bundle: binformat.Bundle, numeric_id: int) -> np.ndarray:
    return np.stack(
        [bundle.decode_plane(numeric_id, offset).reshape(bundle.height, bundle.width) for offset in bundle.frame_offsets]
    )


def read_back(store: Path, name: str, frame_count: int) -> np.ndarray:
    return np.stack([zarrstore.read_plane(store, name, frame) for frame in range(frame_count)])


class Crc32cTests(unittest.TestCase):
    def test_reference_vector(self) -> None:
        self.assertEqual(zarrstore.crc32c(b"123456789"), 0xE3069283)
        self.assertEqual(zarrstore.crc32c(b""), 0)


class ExportTests(unittest.TestCase):
    """One two-variable bundle (a PREVIOUS and a RAW variable, the wind
    layout) exported four ways, plus a single-variable one."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-zarr-"))
        cls.bundle = write_v2(cls.root / "pair.xue", ("tmp2m", "prate"))
        cls.single = write_v2(cls.root / "single.xue", ("tmp2m",))
        cls.stores: dict[tuple[bool, str], zarrstore.ExportReport] = {}
        for delta in (False, True):
            for location in zarrstore.INDEX_LOCATIONS:
                cls.stores[delta, location] = zarrstore.export_bundle(
                    cls.root / "pair.xue",
                    cls.root / f"pair-{int(delta)}-{location}.zarr",
                    delta=delta,
                    index_location=location,
                )
        cls.single_store = zarrstore.export_bundle(cls.root / "single.xue", cls.root / "single.zarr")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_default_index_location_is_the_suffix_range_one(self) -> None:
        self.assertEqual(zarrstore.DEFAULT_INDEX_LOCATION, "end")
        self.assertEqual(self.single_store.index_location, "end")

    def test_read_back_equals_decode_plane(self) -> None:
        for (delta, location), report in self.stores.items():
            for numeric_id, name in self.bundle.variable_ids.items():
                with self.subTest(delta=delta, index_location=location, variable=name):
                    np.testing.assert_array_equal(
                        read_back(report.path, name, self.bundle.frame_count),
                        expected_codes(self.bundle, numeric_id),
                    )
        np.testing.assert_array_equal(
            read_back(self.single_store.path, "tmp2m", self.single.frame_count), expected_codes(self.single, 1)
        )

    def test_chunks_come_back_trimmed(self) -> None:
        store = self.stores[True, "end"].path
        geometry = zarrstore.ArrayGeometry.from_metadata(zarrstore.read_json(store / "tmp2m" / "zarr.json"))
        # One shard per array: the axis of 21 frames rounded up to four
        # time chunks of six, over the whole grid of whole tiles.
        self.assertEqual(geometry.shard_shape, (24, 12, 20))
        self.assertEqual(geometry.inner_shape, (6, 4, 5))
        self.assertEqual((geometry.tile_rows, geometry.tile_columns, geometry.time_chunks), (3, 4, 4))
        self.assertEqual((geometry.shard_count, geometry.chunks_per_shard, geometry.shard_of(3)), (1, 48, (0, 36)))
        # The south-east tile is clipped on both sides; the last time chunk
        # holds three of its six frames.
        last_tile = geometry.tile_count - 1
        self.assertEqual(zarrstore.read_array_chunk(store, "tmp2m", 0, last_tile).shape, (6, 1, 2))
        self.assertEqual(zarrstore.read_array_chunk(store, "tmp2m", 3, 0).shape, (3, 4, 5))
        expected = expected_codes(self.bundle, 1)
        np.testing.assert_array_equal(zarrstore.read_array_chunk(store, "tmp2m", 3, 0), expected[18:21, 0:4, 0:5])
        with self.assertRaises(BundleError):
            zarrstore.read_array_chunk(store, "tmp2m", 4, 0)
        with self.assertRaises(BundleError):
            zarrstore.read_plane(store, "tmp2m", self.bundle.frame_count)

    def test_byte_identity_is_measured_where_the_layouts_coincide(self) -> None:
        """A store chunk is comparable to a bundle chunk when it covers the
        same six frames on an unclipped tile: the two chunks before the
        change of step, on the six whole tiles, for each of two variables.
        Under the delta chain every one of them is byte-identical; under the
        standard chain only the RAW variable's are, since the PREVIOUS
        variable's bundle chunks hold residuals."""
        comparable = COINCIDING_CHUNKS * UNCLIPPED_TILES * len(self.bundle.variable_ids)
        for (delta, location), report in self.stores.items():
            with self.subTest(delta=delta, index_location=location):
                self.assertEqual(report.chunk_count, 2 * 4 * 12)
                self.assertEqual(report.comparable_chunks, comparable)
                self.assertEqual(report.identical_chunks, comparable if delta else comparable // 2)
        self.assertEqual(self.single_store.comparable_chunks, COINCIDING_CHUNKS * UNCLIPPED_TILES)
        self.assertEqual(self.single_store.identical_chunks, 0)

    def test_store_layout(self) -> None:
        for (delta, location), report in self.stores.items():
            with self.subTest(delta=delta, index_location=location):
                store = report.path
                root = zarrstore.read_json(store / "zarr.json")
                self.assertEqual(root["node_type"], "group")
                self.assertEqual(root["attributes"]["xue"], self.bundle.metadata)
                self.assertEqual(root["attributes"]["xue_profile"], 1)
                self.assertEqual(report.crc32, f"{binformat.crc32_plane((store / 'zarr.json').read_bytes()):08x}")
                self.assertEqual(report.byte_length, zarrstore.store_byte_length(store))
                self.assertEqual(sorted(report.arrays), ["latitude", "longitude", "prate", "time", "tmp2m"])
                # Every array document is repeated inline in the group's, so
                # a client that cannot list the store (plain HTTP) still
                # finds the arrays; and the group's CRC covers them all.
                consolidated = root["consolidated_metadata"]
                self.assertEqual((consolidated["kind"], consolidated["must_understand"]), ("inline", False))
                self.assertEqual(sorted(consolidated["metadata"]), sorted(report.arrays))
                for name in report.arrays:
                    self.assertEqual(consolidated["metadata"][name], zarrstore.read_json(store / name / "zarr.json"))
                for name, predictor in (("tmp2m", "previous"), ("prate", "raw")):
                    array = zarrstore.read_json(store / name / "zarr.json")
                    variable = next(v for v in self.bundle.metadata["variables"] if v["id"] == name)
                    self.assertEqual(array["shape"], [21, 9, 17])
                    self.assertEqual(array["data_type"], "uint8")
                    self.assertEqual(array["chunk_grid"]["configuration"]["chunk_shape"], [24, 12, 20])
                    self.assertEqual(array["chunk_key_encoding"], {"name": "default"})
                    self.assertEqual(array["fill_value"], variable["quantization"]["nodataCode"])
                    self.assertEqual(array["dimension_names"], ["time", "latitude", "longitude"])
                    sharding = array["codecs"][0]
                    self.assertEqual(sharding["name"], "sharding_indexed")
                    self.assertEqual(sharding["configuration"]["chunk_shape"], [6, 4, 5])
                    self.assertEqual(sharding["configuration"]["index_location"], location)
                    self.assertEqual(
                        [codec["name"] for codec in sharding["configuration"]["index_codecs"]], ["bytes", "crc32c"]
                    )
                    chain = [codec["name"] for codec in sharding["configuration"]["codecs"]]
                    self.assertEqual(chain, (["xue.delta"] if delta and predictor == "previous" else []) + ["bytes", "zstd"])
                    self.assertEqual(sharding["configuration"]["codecs"][-1]["configuration"], {"level": 15, "checksum": True})
                    attributes = array["attributes"]
                    self.assertEqual(attributes["xue"], {"variable": variable, "predictor": predictor})
                    self.assertEqual(attributes["_FillValue"], variable["quantization"]["nodataCode"])
                    if name == "tmp2m":
                        self.assertEqual(attributes["scale_factor"], 0.5)
                        self.assertEqual(attributes["add_offset"], -60.0)
                    else:
                        self.assertNotIn("scale_factor", attributes)
                        self.assertNotIn("add_offset", attributes)
                    # One object per array, whatever the axis: `c/0/0/0`.
                    self.assertEqual([p.as_posix() for p in (store / name / "c").rglob("*") if p.is_file()], [f"{store / name}/c/0/0/0"])
                    self.assertNotIn("xue_index", root["attributes"])
                    self.assertFalse((store / "index.bin").exists())
                time_axis = zarrstore.read_json(store / "time" / "zarr.json")
                self.assertEqual(time_axis["attributes"]["units"], "seconds since 2026-08-14T06:00:00Z")
                np.testing.assert_array_equal(
                    np.frombuffer((store / "time" / "c" / "0").read_bytes(), dtype="<i4"),
                    np.array(MIXED_HOURS) * 3600,
                )
                np.testing.assert_array_equal(
                    np.frombuffer((store / "latitude" / "c" / "0").read_bytes(), dtype="<f8"),
                    90.0 - 20.0 * np.arange(9),
                )

    def test_the_shard_index_names_every_chunk_of_the_padded_axis(self) -> None:
        """The index covers the shard's whole shape — four time chunks of
        twelve tiles — and the time chunk past the axis is stored at the
        fill value; a time chunk is one contiguous run of the object."""
        report = self.stores[False, "end"]
        store = report.path
        geometry = zarrstore.ArrayGeometry.from_metadata(zarrstore.read_json(store / "prate" / "zarr.json"))
        shard = (store / "prate" / "c" / "0" / "0" / "0").read_bytes()
        entries = zarrstore._shard_index(shard, geometry.chunks_per_shard, "end")
        self.assertEqual(len(entries), 48)
        offsets = [offset for offset, _ in entries]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(offsets[0], 0)
        for (offset, length), (next_offset, _) in zip(entries, entries[1:]):
            self.assertEqual(offset + length, next_offset)
        # Tile 1 of the last time chunk (frames 18–20) reads trimmed to
        # three frames; nothing past the axis is readable.
        self.assertEqual(zarrstore.read_array_chunk(store, "prate", 3, 1).shape, (3, 4, 5))
        with self.assertRaises(BundleError):
            zarrstore.read_array_chunk(store, "prate", 4, 0)

    def test_a_store_cut_one_shard_per_time_chunk_still_reads(self) -> None:
        """The reader takes the shard's frame count from the array's chunk
        shape: a store written one shard per time chunk — the profile's
        earlier form, still on the bucket — reads the same codes."""
        report = self.stores[True, "start"]
        store = self.root / "per-chunk.zarr"
        shutil.copytree(report.path, store)
        for name in ("tmp2m", "prate"):
            geometry = zarrstore.ArrayGeometry.from_metadata(zarrstore.read_json(store / name / "zarr.json"))
            shard = (store / name / "c" / "0" / "0" / "0").read_bytes()
            entries = zarrstore._shard_index(shard, geometry.chunks_per_shard, "start")
            (store / name / "c" / "0" / "0" / "0").unlink()
            for time_chunk in range(geometry.time_chunks_per_shard):
                payloads = [shard[o : o + n] for o, n in entries[time_chunk * 12 : (time_chunk + 1) * 12]]
                (store / name / "c" / str(time_chunk) / "0").mkdir(parents=True, exist_ok=True)
                (store / name / "c" / str(time_chunk) / "0" / "0").write_bytes(zarrstore._pack_shard(payloads, "start"))
            metadata = zarrstore.read_json(store / name / "zarr.json")
            metadata["chunk_grid"]["configuration"]["chunk_shape"] = [6, 12, 20]
            (store / name / "zarr.json").write_bytes(zarrstore._dump_json(metadata))
            geometry = zarrstore.ArrayGeometry.from_metadata(metadata)
            self.assertEqual((geometry.shard_frames, geometry.shard_count, geometry.chunks_per_shard), (6, 4, 12))
        for numeric_id, name in self.bundle.variable_ids.items():
            np.testing.assert_array_equal(
                read_back(store, name, self.bundle.frame_count), expected_codes(self.bundle, numeric_id)
            )
        broken = zarrstore.read_json(store / "prate" / "zarr.json")
        broken["chunk_grid"]["configuration"]["chunk_shape"] = [9, 12, 20]
        with self.assertRaises(BundleError):
            zarrstore.ArrayGeometry.from_metadata(broken)

    def test_the_shard_index_is_verified(self) -> None:
        report = self.stores[False, "end"]
        store = self.root / "corrupt.zarr"
        shutil.copytree(report.path, store)
        shard = store / "prate" / "c" / "0" / "0" / "0"
        data = bytearray(shard.read_bytes())
        data[-1] ^= 0xFF
        shard.write_bytes(bytes(data))
        with self.assertRaises(BundleError):
            zarrstore.read_array_chunk(store, "prate", 0, 0)

    def test_a_v1_bundle_exports_with_an_explicit_tile(self) -> None:
        bundle = write_v1(self.root / "legacy.xue")
        self.assertEqual(bundle.container_version, binformat.VERSION)
        report = zarrstore.export_bundle(self.root / "legacy.xue", self.root / "legacy.zarr", tile=TILE)
        np.testing.assert_array_equal(read_back(report.path, "prate", bundle.frame_count), expected_codes(bundle, 1))
        self.assertEqual((report.comparable_chunks, report.identical_chunks), (0, 0))
        self.assertEqual(zarrstore.read_json(report.path / "prate" / "zarr.json")["attributes"]["xue"]["predictor"], "raw")

    def test_the_manifest_descriptor(self) -> None:
        report = self.single_store
        descriptor = report.descriptor(self.root)
        self.assertEqual(descriptor, {"path": "single.zarr", "byteLength": report.byte_length, "crc32": report.crc32})
        self.assertEqual(zarrstore.store_path_for(Path("x/tmp2m.half.xue")), Path("x/tmp2m.half.zarr"))

    def test_an_export_replaces_a_stale_store(self) -> None:
        store = self.root / "stale.zarr"
        store.mkdir()
        (store / "leftover").write_text("x")
        zarrstore.export_bundle(self.root / "single.xue", store)
        self.assertFalse((store / "leftover").exists())
        self.assertTrue((store / "tmp2m" / "zarr.json").is_file())


class SeriesExportTests(unittest.TestCase):
    """The series store: the same codes cut one inner chunk per 8 x 8 block
    over the whole axis, so a cell's series is one chunk where the map store
    costs one per time chunk."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-zarr-series-"))
        cls.bundle = write_v2(cls.root / "pair.xue", ("tmp2m", "prate"))
        cls.delta = zarrstore.export_series(cls.root / "pair.xue", cls.root / "pair.series.zarr")
        cls.plain = zarrstore.export_series(
            cls.root / "pair.xue", cls.root / "plain.series.zarr", delta=False
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_read_back_equals_decode_plane(self) -> None:
        for report in (self.delta, self.plain):
            for numeric_id, name in self.bundle.variable_ids.items():
                with self.subTest(store=report.path.name, variable=name):
                    np.testing.assert_array_equal(
                        read_back(report.path, name, self.bundle.frame_count),
                        expected_codes(self.bundle, numeric_id),
                    )

    def test_one_chunk_per_block_over_the_whole_axis(self) -> None:
        geometry = zarrstore.ArrayGeometry.from_metadata(
            zarrstore.read_json(self.delta.path / "tmp2m" / "zarr.json")
        )
        self.assertEqual(geometry.inner_shape, (self.bundle.frame_count, 8, 8))
        self.assertEqual((geometry.time_chunks, geometry.shard_frames), (1, self.bundle.frame_count))
        # 17 x 9 cells in 8 x 8 blocks: three columns, two rows, each last clipped.
        self.assertEqual((geometry.tile_columns, geometry.tile_rows, geometry.tile_count), (3, 2, 6))
        self.assertEqual(geometry.shard_index_bytes, 16 * 6 + 4)
        # A cell's whole series is one inner chunk, at the block's full shape.
        tile = (4 // 8) * geometry.tile_columns + 4 // 8
        row, column = geometry.tile_origin(tile)
        block = zarrstore.read_array_chunk(self.delta.path, "tmp2m", 0, tile)
        self.assertEqual(block.shape, (self.bundle.frame_count, 8, 8))
        np.testing.assert_array_equal(block[:, 4 - row, 4 - column], self.bundle.decode_series(1, 4, 4))

    def test_the_chains_are_the_bundles_predictors(self) -> None:
        for report, delta in ((self.delta, True), (self.plain, False)):
            for name, predictor in (("tmp2m", "previous"), ("prate", "raw")):
                chain = [
                    codec["name"]
                    for codec in zarrstore.read_json(report.path / name / "zarr.json")["codecs"][0][
                        "configuration"
                    ]["codecs"]
                ]
                self.assertEqual(
                    chain,
                    (["xue.delta"] if delta and predictor == "previous" else []) + ["bytes", "zstd"],
                )
        # The group carries the bundle's metadata verbatim, as the map store's does.
        self.assertEqual(
            zarrstore.read_json(self.delta.path / "zarr.json")["attributes"]["xue"], self.bundle.metadata
        )

    def test_the_series_store_path(self) -> None:
        self.assertEqual(zarrstore.series_store_path_for(Path("x/tmp2m.xue")), Path("x/tmp2m.series.zarr"))

    def test_the_rollout_switch_is_per_source(self) -> None:
        """The companion ships only where a source opts in, so it can be
        observed on one dataset first. GFS is that dataset; every other source
        is off until its own registry entry says otherwise."""
        gfs = source_spec("gfs")
        self.assertTrue(gfs.series_bundle_ids)
        for bundle_id in gfs.series_bundle_ids:
            self.assertIn(bundle_id, binconvert.published_bundle_ids(gfs))
            self.assertNotIn(".half", bundle_id)
        for model in ("ecmwf", "sflux", "hrrr", "cfs", "mrms", "jma"):
            with self.subTest(model=model):
                self.assertEqual(source_spec(model).series_bundle_ids, ())


@unittest.skipUnless(zarrcodec.available(), "zarr-python is not installed (uv sync --group zarr)")
class ZarrClientTests(unittest.TestCase):
    """What a generic client sees. The standard chain needs nothing
    registered; the delta chain needs `xuebuild.zarrcodec.register`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-zarr-client-"))
        cls.bundle = write_v2(cls.root / "pair.xue", ("tmp2m", "prate"))
        cls.standard = zarrstore.export_bundle(cls.root / "pair.xue", cls.root / "standard.zarr")
        cls.delta = zarrstore.export_bundle(cls.root / "pair.xue", cls.root / "delta.zarr", delta=True)
        cls.start = zarrstore.export_bundle(
            cls.root / "pair.xue", cls.root / "start.zarr", delta=True, index_location="start"
        )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_zarr_python_reads_the_codes(self) -> None:
        import zarr

        zarrcodec.register()
        for report in (self.standard, self.delta, self.start):
            group = zarr.open_group(report.path, mode="r")
            for numeric_id, name in self.bundle.variable_ids.items():
                with self.subTest(store=report.path.name, variable=name):
                    np.testing.assert_array_equal(group[name][:], expected_codes(self.bundle, numeric_id))
            self.assertEqual(dict(group.attrs)["xue"], self.bundle.metadata)

    def test_xarray_dequantizes_the_linear_codebook(self) -> None:
        import xarray as xr

        zarrcodec.register()
        codes = expected_codes(self.bundle, 1)
        physical = np.where(codes == 255, np.nan, -60.0 + codes.astype(np.float64) * 0.5)
        for report in (self.standard, self.delta):
            with self.subTest(store=report.path.name), warnings.catch_warnings():
                warnings.simplefilter("ignore")
                # The default: xarray takes the consolidated metadata, the
                # way it would over an origin it cannot list.
                dataset = xr.open_zarr(report.path)
                self.assertEqual(dataset["tmp2m"].dims, ("time", "latitude", "longitude"))
                np.testing.assert_allclose(dataset["tmp2m"].values, physical)
                # The log codebook has no CF spelling: the codes come through
                # as they are, nodata masked.
                np.testing.assert_array_equal(dataset["prate"].values, expected_codes(self.bundle, 2))
                self.assertEqual(
                    dataset["time"].values[:2].astype("datetime64[s]").tolist(),
                    [datetime(2026, 8, 14, 6), datetime(2026, 8, 14, 7)],
                )
                self.assertEqual(dataset["time"].values[-1].astype("datetime64[s]").item(), datetime(2026, 8, 15, 18))
                np.testing.assert_allclose(dataset["latitude"].values, 90.0 - 20.0 * np.arange(9))
                self.assertEqual(dataset.attrs["xue_profile"], 1)

    def test_the_delta_codec_round_trips_through_zarr_python(self) -> None:
        import zarr

        zarrcodec.register()
        block = (np.arange(6 * 4 * 5, dtype=np.uint32) * 37 % 256).astype(np.uint8).reshape(6, 4, 5)
        array = zarr.create_array(
            zarr.storage.MemoryStore(),
            shape=block.shape,
            chunks=block.shape,
            dtype="uint8",
            filters=[zarrcodec.XueDeltaCodec(axis=0)],
        )
        array[:] = block
        np.testing.assert_array_equal(array[:], block)
        self.assertEqual(zarrcodec.XueDeltaCodec.from_dict({"name": "xue.delta"}).axis, 0)
        np.testing.assert_array_equal(zarrcodec.delta_decode(zarrcodec.delta_encode(block)), block)


@unittest.skipUnless(shutil.which("gdal_translate"), "the reference encoder needs a system GDAL")
class BuildIntegrationTests(unittest.TestCase):
    """`convert_bin(..., zarr=True)`: one store beside every bundle and
    variant, named in the manifest, and the bundles unchanged by it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="xue-zarr-build-"))
        cls.without = cls.root / "without"
        cls.with_zarr = cls.root / "with"
        cls.reports = {}
        for directory, zarr in ((cls.without, False), (cls.with_zarr, True)):
            cls.reports[zarr] = binconvert.convert_bin(
                FIXTURE_GRIB,
                directory,
                work_root=cls.root / f"work-{int(zarr)}",
                manifest_path=directory / "manifest.json",
                skip_video=True,
                zarr=zarr,
            )

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_every_bundle_and_variant_has_a_store_named_in_the_manifest(self) -> None:
        manifest = json.loads((self.with_zarr / "manifest.json").read_text(encoding="utf-8"))
        validate_bin_manifest(manifest, expected_hours=manifest["forecastHours"])
        for bundle in manifest["bundles"]:
            for artifact in (bundle, *bundle.get("variants", [])):
                with self.subTest(path=artifact["path"]):
                    store = artifact["zarr"]
                    self.assertEqual(store["path"], artifact["path"].removesuffix(".xue") + ".zarr")
                    self.assertEqual(store["byteLength"], zarrstore.store_byte_length(self.with_zarr / store["path"]))
                    root = (self.with_zarr / store["path"] / "zarr.json").read_bytes()
                    self.assertEqual(store["crc32"], f"{binformat.crc32_plane(root):08x}")
                    bundle_file = binformat.read_bundle(self.with_zarr / artifact["path"])
                    self.assertEqual(json.loads(root)["attributes"]["xue"], bundle_file.metadata)
                    for numeric_id, name in bundle_file.variable_ids.items():
                        np.testing.assert_array_equal(
                            zarrstore.read_plane(self.with_zarr / store["path"], name, 0),
                            bundle_file.decode_plane(numeric_id, bundle_file.frame_offsets[0]).reshape(
                                bundle_file.height, bundle_file.width
                            ),
                        )

    def test_the_store_does_not_change_the_bundle(self) -> None:
        for path in sorted(self.without.rglob("*.xue")):
            relative = path.relative_to(self.without)
            with self.subTest(bundle=relative.as_posix()):
                self.assertEqual(path.read_bytes(), (self.with_zarr / relative).read_bytes())
        without = json.loads((self.without / "manifest.json").read_text(encoding="utf-8"))
        with_zarr = json.loads((self.with_zarr / "manifest.json").read_text(encoding="utf-8"))

        def strip(manifest: dict) -> dict:
            for bundle in manifest["bundles"]:
                bundle.pop("zarr", None)
                bundle.pop("series", None)
                for variant in bundle.get("variants", []):
                    variant.pop("zarr", None)
            return manifest

        self.assertEqual(without, strip(with_zarr))
        self.assertEqual(list(self.without.rglob("*.zarr")), [])

    def test_the_series_companion_is_named_and_reads_the_same_codes(self) -> None:
        manifest = json.loads((self.with_zarr / "manifest.json").read_text(encoding="utf-8"))
        for bundle in manifest["bundles"]:
            with self.subTest(variable=bundle["variable"]):
                if bundle["variable"] not in source_spec("gfs").series_bundle_ids:
                    self.assertNotIn("series", bundle)
                    continue
                series = bundle["series"]
                self.assertEqual(series["path"], bundle["path"].removesuffix(".xue") + ".series.zarr")
                self.assertEqual(series["byteLength"], zarrstore.store_byte_length(self.with_zarr / series["path"]))
                root = (self.with_zarr / series["path"] / "zarr.json").read_bytes()
                self.assertEqual(series["crc32"], f"{binformat.crc32_plane(root):08x}")
                bundle_file = binformat.read_bundle(self.with_zarr / bundle["path"])
                for numeric_id, name in bundle_file.variable_ids.items():
                    store = self.with_zarr / series["path"]
                    geometry = zarrstore.ArrayGeometry.from_metadata(zarrstore.read_json(store / name / "zarr.json"))
                    self.assertEqual(geometry.time_chunks, 1)
                    tile = 0
                    row, column = geometry.tile_origin(tile)
                    height, width = geometry.tile_shape(tile)
                    block = zarrstore.read_array_chunk(store, name, 0, tile)
                    np.testing.assert_array_equal(
                        block, expected_codes(bundle_file, numeric_id)[:, row : row + height, column : column + width]
                    )

    def test_a_stale_descriptor_is_rejected_when_the_store_moves(self) -> None:
        manifest = json.loads((self.with_zarr / "manifest.json").read_text(encoding="utf-8"))
        manifest["bundles"][0]["zarr"]["path"] = manifest["bundles"][0]["path"]
        with self.assertRaises(ManifestError):
            validate_bin_manifest(manifest, expected_hours=manifest["forecastHours"])


if __name__ == "__main__":
    unittest.main()

"""The Zarr v3 store written from a bundle's codes.

What is pinned is that a NumPy-only read-back of every frame equals the
codes the store was written from, in both codec chains and with the shard
index at either end, on an axis that changes step and a grid no tile
divides; and — when zarr-python and xarray are installed — that a generic
Zarr client reads the same codes and dequantizes the linear codebook to
physical values on its own.
"""

from __future__ import annotations

import json
import shutil
import unittest
import warnings
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from tests._support import ClassTempRoot, requires_native
from xuebuild import binconvert, binformat, zarrcodec, zarrstore, zstdcli
from xuebuild.binconvert import GridInfo, build_metadata
from xuebuild.errors import BundleError, ManifestError
from xuebuild.manifest import validate_bin_manifest
from xuebuild.sources import source_spec

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_GRIB = REPOSITORY_ROOT / "tests" / "fixtures" / "gfs.2026081406.f000.crop.grib2"

# The same miniature GFS-shaped axis the golden fixture uses: thirteen hourly
# frames, then eight three-hourly ones. The store's regular six-frame grid
# ([0-5], [6-11], [12-17], [18-20]) pads its last time chunk.
MIXED_HOURS = list(range(13)) + list(range(15, 37, 3))
# A grid no tile divides: 17 x 9 cells in 5 x 4 tiles leaves a two-column
# last tile column and a one-row last tile row.
GRID = GridInfo(
    width=17,
    height=9,
    first_longitude=-180.0,
    first_latitude=90.0,
    longitude_step=21.0,
    latitude_step=-20.0,
)
TILE = (5, 4)
PAIR = ("tmp2m", "prate")
PREDICTORS = {1: "previous", 2: "raw"}


def plane(hour: int, variable_id: int) -> np.ndarray:
    points = GRID.width * GRID.height
    codes = ((np.arange(points, dtype=np.uint32) * (3 + variable_id) + hour * 7) % 251).astype(np.uint8)
    if variable_id == 1:
        # A nodata cell, so the CF fill value has something to mask.
        codes[0] = 255
    return codes


def codes_source(names: tuple[str, ...]) -> zarrstore.CodesSource:
    """A bundle's codes as a build holds them: ``plane(hour, n)`` for the
    n-th variable, a PREVIOUS and then a RAW one (the wind layout)."""
    metadata = build_metadata(datetime(2026, 8, 14, 6, tzinfo=UTC), MIXED_HOURS, GRID, "quality", names)
    planes = {name: [plane(hour, index) for hour in MIXED_HOURS] for index, name in enumerate(names, start=1)}
    predictors = {name: PREDICTORS[index] for index, name in enumerate(names, start=1)}
    return zarrstore.CodesSource(metadata, planes, predictors, TILE)


def expected_codes(numeric_id: int) -> np.ndarray:
    """The n-th variable's codes over the whole axis, ``(frames, rows, columns)``."""
    return np.stack([plane(hour, numeric_id).reshape(GRID.height, GRID.width) for hour in MIXED_HOURS])


def read_back(store: Path, name: str, frame_count: int) -> np.ndarray:
    return np.stack([zarrstore.read_plane(store, name, frame) for frame in range(frame_count)])


class Crc32cTests(unittest.TestCase):
    def test_reference_vector(self) -> None:
        self.assertEqual(zarrstore.crc32c(b"123456789"), 0xE3069283)
        self.assertEqual(zarrstore.crc32c(b""), 0)


class StoreTests(ClassTempRoot, unittest.TestCase):
    """One two-variable bundle (a PREVIOUS and a RAW variable, the wind
    layout) written four ways, plus a single-variable one."""

    root_prefix = "xue-zarr-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.source = codes_source(PAIR)
        cls.stores: dict[tuple[bool, str], zarrstore.ExportReport] = {}
        for delta in (False, True):
            for location in zarrstore.INDEX_LOCATIONS:
                cls.stores[delta, location] = zarrstore.write_store(
                    cls.source,
                    cls.root / f"pair-{int(delta)}-{location}.zarr",
                    delta=delta,
                    index_location=location,
                )
        cls.single_store = zarrstore.write_store(codes_source(("tmp2m",)), cls.root / "single.zarr")

    def test_default_index_location_is_the_suffix_range_one(self) -> None:
        self.assertEqual(zarrstore.DEFAULT_INDEX_LOCATION, "end")
        self.assertEqual(self.single_store.index_location, "end")

    def test_read_back_equals_the_codes(self) -> None:
        for (delta, location), report in self.stores.items():
            for numeric_id, name in enumerate(PAIR, start=1):
                with self.subTest(delta=delta, index_location=location, variable=name):
                    np.testing.assert_array_equal(
                        read_back(report.path, name, len(MIXED_HOURS)), expected_codes(numeric_id)
                    )
        np.testing.assert_array_equal(read_back(self.single_store.path, "tmp2m", len(MIXED_HOURS)), expected_codes(1))

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
        expected = expected_codes(1)
        np.testing.assert_array_equal(zarrstore.read_array_chunk(store, "tmp2m", 3, 0), expected[18:21, 0:4, 0:5])
        with self.assertRaises(BundleError):
            zarrstore.read_array_chunk(store, "tmp2m", 4, 0)
        with self.assertRaises(BundleError):
            zarrstore.read_plane(store, "tmp2m", len(MIXED_HOURS))

    def test_every_inner_chunk_of_the_axis_is_written(self) -> None:
        for (delta, location), report in self.stores.items():
            with self.subTest(delta=delta, index_location=location):
                self.assertEqual(report.chunk_count, 2 * 4 * 12)

    def test_store_layout(self) -> None:
        for (delta, location), report in self.stores.items():
            with self.subTest(delta=delta, index_location=location):
                store = report.path
                root = zarrstore.read_json(store / "zarr.json")
                self.assertEqual(root["node_type"], "group")
                self.assertEqual(root["attributes"]["xue"], self.source.metadata)
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
                    variable = next(v for v in self.source.metadata["variables"] if v["id"] == name)
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
        for numeric_id, name in enumerate(PAIR, start=1):
            np.testing.assert_array_equal(read_back(store, name, len(MIXED_HOURS)), expected_codes(numeric_id))
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

    def test_the_manifest_descriptor(self) -> None:
        report = self.single_store
        descriptor = report.descriptor(self.root)
        self.assertEqual(descriptor, {"path": "single.zarr", "byteLength": report.byte_length, "crc32": report.crc32})

    def test_a_write_replaces_a_stale_store(self) -> None:
        store = self.root / "stale.zarr"
        store.mkdir()
        (store / "leftover").write_text("x")
        zarrstore.write_store(codes_source(("tmp2m",)), store)
        self.assertFalse((store / "leftover").exists())
        self.assertTrue((store / "tmp2m" / "zarr.json").is_file())


class SeriesStoreTests(ClassTempRoot, unittest.TestCase):
    """The series store: the same codes cut one inner chunk per 8 x 8 block
    over the whole axis, so a cell's series is one chunk where the map store
    costs one per time chunk."""

    root_prefix = "xue-zarr-series-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.source = codes_source(PAIR)
        cls.delta = zarrstore.write_store(cls.source, cls.root / "pair.series.zarr", delta=True, series=True)
        cls.plain = zarrstore.write_store(cls.source, cls.root / "plain.series.zarr", series=True)

    def test_read_back_equals_the_codes(self) -> None:
        for report in (self.delta, self.plain):
            for numeric_id, name in enumerate(PAIR, start=1):
                with self.subTest(store=report.path.name, variable=name):
                    np.testing.assert_array_equal(
                        read_back(report.path, name, len(MIXED_HOURS)), expected_codes(numeric_id)
                    )

    def test_one_chunk_per_block_over_the_whole_axis(self) -> None:
        geometry = zarrstore.ArrayGeometry.from_metadata(
            zarrstore.read_json(self.delta.path / "tmp2m" / "zarr.json")
        )
        self.assertEqual(geometry.inner_shape, (len(MIXED_HOURS), 8, 8))
        self.assertEqual((geometry.time_chunks, geometry.shard_frames), (1, len(MIXED_HOURS)))
        # 17 x 9 cells in 8 x 8 blocks: three columns, two rows, each last clipped.
        self.assertEqual((geometry.tile_columns, geometry.tile_rows, geometry.tile_count), (3, 2, 6))
        self.assertEqual(geometry.shard_index_bytes, 16 * 6 + 4)
        # A cell's whole series is one inner chunk, at the block's full shape.
        tile = (4 // 8) * geometry.tile_columns + 4 // 8
        row, column = geometry.tile_origin(tile)
        block = zarrstore.read_array_chunk(self.delta.path, "tmp2m", 0, tile)
        self.assertEqual(block.shape, (len(MIXED_HOURS), 8, 8))
        np.testing.assert_array_equal(block[:, 4 - row, 4 - column], expected_codes(1)[:, 4, 4])

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
            zarrstore.read_json(self.delta.path / "zarr.json")["attributes"]["xue"], self.source.metadata
        )

    def test_the_rollout_switch_is_per_source(self) -> None:
        """The companion ships only where a source opts in, so it can be
        observed on one dataset first. GFS is that dataset; every other source
        is off until its own registry entry says otherwise."""
        gfs = source_spec("gfs")
        self.assertTrue(gfs.series_bundle_ids)
        for bundle_id in gfs.series_bundle_ids:
            self.assertIn(bundle_id, binconvert.published_bundle_ids(gfs))
            self.assertNotIn(".half", bundle_id)
        for model in ("hrrr", "sflux"):
            with self.subTest(model=model):
                spec = source_spec(model)
                self.assertTrue(spec.series_bundle_ids)
                for bundle_id in spec.series_bundle_ids:
                    self.assertIn(bundle_id, binconvert.published_bundle_ids(spec))
        for model in ("ecmwf", "cfs", "mrms", "jma"):
            with self.subTest(model=model):
                self.assertEqual(source_spec(model).series_bundle_ids, ())


class CodesSourceTests(unittest.TestCase):
    def test_codes_that_do_not_match_the_metadata_are_refused(self) -> None:
        metadata = codes_source(("tmp2m",)).metadata
        with self.assertRaises(BundleError):
            zarrstore.CodesSource(metadata, {"prate": [plane(0, 1)] * len(MIXED_HOURS)}, {"prate": "raw"}, TILE)
        with self.assertRaises(BundleError):
            zarrstore.CodesSource(metadata, {"tmp2m": [plane(0, 1)]}, {"tmp2m": "previous"}, TILE)
        with self.assertRaises(BundleError):
            zarrstore.CodesSource(
                metadata, {"tmp2m": [plane(0, 1)] * len(MIXED_HOURS)}, {"tmp2m": "anchor"}, TILE
            )


@requires_native
class NativeStoreWriterTests(ClassTempRoot, unittest.TestCase):
    """The native store writer (`xue.write_store`) against the reference
    (`zarrstore.write_store` over a `CodesSource`), object for object, on
    the same awkward axis and grid: a PREVIOUS and a RAW variable, a unit
    outside ASCII, map and series layouts, both codec chains."""

    root_prefix = "xue-zarr-native-"

    def test_both_writers_write_the_same_store(self) -> None:
        import xue

        from tests._support import assert_trees_identical

        for names in (("tmp2m", "prate"), ("tmp2m",)):
            metadata = build_metadata(datetime(2026, 8, 14, 6, tzinfo=UTC), MIXED_HOURS, GRID, "quality", names)
            planes = {
                name: [plane(hour, index) for hour in MIXED_HOURS] for index, name in enumerate(names, start=1)
            }
            predictors = {name: PREDICTORS[index] for index, name in enumerate(names, start=1)}
            source = zarrstore.CodesSource(metadata, planes, predictors, TILE)
            for series in (False, True):
                for delta in (False, True):
                    stem = f"{'-'.join(names)}-{int(series)}-{int(delta)}"
                    with self.subTest(store=stem):
                        reference = zarrstore.write_store(
                            source, self.root / "reference" / f"{stem}.zarr", delta=delta, series=series
                        )
                        subject = xue.write_store(
                            binformat.metadata_json_bytes(metadata).decode("utf-8"),
                            [
                                (name, predictors[name] == "previous", [np.ascontiguousarray(p).ravel() for p in planes[name]])
                                for name in names
                            ],
                            TILE,
                            self.root / "subject" / f"{stem}.zarr",
                            delta=delta,
                            series=series,
                        )
                        if subject["zstdVersion"] != ".".join(map(str, zstdcli.zstd_version())):
                            self.skipTest(f"libzstd differs: the wheel carries {subject['zstdVersion']}")
                        assert_trees_identical(self, reference.path, Path(subject["path"]))
                        # The wheel may still report the export's chunk
                        # counts, always zero for a store written from codes.
                        self.assertEqual((subject.get("comparableChunks", 0), subject.get("identicalChunks", 0)), (0, 0))
                        ignored = ("path", "zstdVersion", "comparableChunks", "identicalChunks")
                        self.assertEqual(
                            {key: value for key, value in reference.to_dict().items() if key != "path"},
                            {key: value for key, value in subject.items() if key not in ignored},
                        )


@unittest.skipUnless(zarrcodec.available(), "zarr-python is not installed (uv sync --group zarr)")
class ZarrClientTests(ClassTempRoot, unittest.TestCase):
    """What a generic client sees. The standard chain needs nothing
    registered; the delta chain needs `xuebuild.zarrcodec.register`."""

    root_prefix = "xue-zarr-client-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.source = codes_source(PAIR)
        cls.standard = zarrstore.write_store(cls.source, cls.root / "standard.zarr")
        cls.delta = zarrstore.write_store(cls.source, cls.root / "delta.zarr", delta=True)
        cls.start = zarrstore.write_store(cls.source, cls.root / "start.zarr", delta=True, index_location="start")

    def test_zarr_python_reads_the_codes(self) -> None:
        import zarr

        zarrcodec.register()
        for report in (self.standard, self.delta, self.start):
            group = zarr.open_group(report.path, mode="r")
            for numeric_id, name in enumerate(PAIR, start=1):
                with self.subTest(store=report.path.name, variable=name):
                    np.testing.assert_array_equal(group[name][:], expected_codes(numeric_id))
            self.assertEqual(dict(group.attrs)["xue"], self.source.metadata)

    def test_xarray_dequantizes_the_linear_codebook(self) -> None:
        import xarray as xr

        zarrcodec.register()
        codes = expected_codes(1)
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
                np.testing.assert_array_equal(dataset["prate"].values, expected_codes(2))
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


def open_container(path: Path):
    """A `.xue` the build wrote, opened by the wheel's decoder."""
    import xue  # noqa: PLC0415 - the wheel; the class skips without it

    return xue.Bundle.open(path)


def container_codes(bundle, numeric_id: int) -> np.ndarray:
    grid = bundle.metadata["grid"]
    return np.stack(
        [np.asarray(bundle.decode(numeric_id, offset)).reshape(grid["height"], grid["width"]) for offset in bundle.frame_offsets]
    )


@unittest.skipUnless(shutil.which("gdal_translate"), "the reference encoder needs a system GDAL")
@requires_native
class BuildIntegrationTests(ClassTempRoot, unittest.TestCase):
    """`convert_bin(..., zarr=True, container=True)`: every bundle and variant
    in its tier's shared store, named in the manifest, holding the codes the
    container beside it holds (read through the wheel's decoder), and the
    containers unchanged by it."""

    root_prefix = "xue-zarr-build-"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
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
                container=True,
            )

    def test_every_bundle_and_variant_has_a_store_named_in_the_manifest(self) -> None:
        manifest = json.loads((self.with_zarr / "manifest.json").read_text(encoding="utf-8"))
        validate_bin_manifest(manifest, expected_hours=manifest["forecastHours"])
        for bundle in manifest["bundles"]:
            for artifact in (bundle, *bundle.get("variants", [])):
                with self.subTest(path=artifact["path"]):
                    store = artifact["zarr"]
                    tier = artifact["path"].removesuffix(".xue").split(".")[1:]
                    self.assertEqual(store["path"], ".".join(["bundles", *tier, "zarr"]))
                    root_dir = self.with_zarr / store["path"]
                    bundle_file = open_container(self.with_zarr / artifact["path"])
                    names = [variable["id"] for variable in bundle_file.metadata["variables"]]
                    used = [*names, *zarrstore.array_dimensions(root_dir, names[0])]
                    self.assertEqual(
                        store["byteLength"],
                        sum(path.stat().st_size for name in used for path in (root_dir / name).rglob("*") if path.is_file()),
                        "a bundle's share: its arrays and the coordinates it uses",
                    )
                    root = (root_dir / "zarr.json").read_bytes()
                    self.assertEqual(store["crc32"], f"{binformat.crc32_plane(root):08x}")
                    self.assertEqual(zarrstore.bundle_metadata(root_dir, bundle["variable"]), bundle_file.metadata)
                    for numeric_id, name in enumerate(names, start=1):
                        np.testing.assert_array_equal(
                            zarrstore.read_plane(self.with_zarr / store["path"], name, 0),
                            container_codes(bundle_file, numeric_id)[0],
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
                bundle_file = open_container(self.with_zarr / bundle["path"])
                names = [variable["id"] for variable in bundle_file.metadata["variables"]]
                for numeric_id, name in enumerate(names, start=1):
                    store = self.with_zarr / series["path"]
                    geometry = zarrstore.ArrayGeometry.from_metadata(zarrstore.read_json(store / name / "zarr.json"))
                    self.assertEqual(geometry.time_chunks, 1)
                    tile = 0
                    row, column = geometry.tile_origin(tile)
                    height, width = geometry.tile_shape(tile)
                    block = zarrstore.read_array_chunk(store, name, 0, tile)
                    np.testing.assert_array_equal(
                        block, container_codes(bundle_file, numeric_id)[:, row : row + height, column : column + width]
                    )

    def test_a_stale_descriptor_is_rejected_when_the_store_moves(self) -> None:
        manifest = json.loads((self.with_zarr / "manifest.json").read_text(encoding="utf-8"))
        manifest["bundles"][0]["zarr"]["path"] = manifest["bundles"][0]["path"]
        with self.assertRaises(ManifestError):
            validate_bin_manifest(manifest, expected_hours=manifest["forecastHours"])


if __name__ == "__main__":
    unittest.main()

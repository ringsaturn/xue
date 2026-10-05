"""The nexrad product (``xuebuild/nexrad``, ``docs/nexrad.md``): the Level 3
reader on real sweeps, the polar store round trip, two rounds built from the
fixture into a window, the late-sweep rule, the same interval written as a
case's window stores, and the validators.

The fixture is seven real Level 3 files from the Rolling Fork tornado night
(2023-03-25 01:36–01:44Z): DGX's two N0B and two N0G sweeps, and three of
GWX's N0G sweeps — GWX was running MESO-SAILS, so two of them fall in one
five-minute round.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from xuebuild import zstdcli
from xuebuild.errors import NexradProductError
from xuebuild.nexrad.build import build_case, build_round, catch_up, load_stations, replay, write_pointer
from xuebuild.nexrad.fetch import key_time, parse_stations
from xuebuild.nexrad.level3 import BEAMS, read_sweep
from xuebuild.nexrad.schema import WINDOW_FILENAME, parse_round, read_window, validate_pointer, validate_window
from xuebuild.nexrad.store import SiteSweeps, read_site, write_store, write_window_store

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nexrad"
SWEEPS = sorted((FIXTURES / "nexrad").glob("*/*_*"))
START = datetime(2023, 3, 25, 1, 35, tzinfo=UTC)
END = datetime(2023, 3, 25, 1, 45, tzinfo=UTC)


def sweep(name: str):
    return read_sweep((FIXTURES / "nexrad" / name[:3] / name).read_bytes())


class Level3Test(unittest.TestCase):
    def test_every_fixture_sweep_reads(self) -> None:
        self.assertEqual(len(SWEEPS), 7)
        for path in SWEEPS:
            parsed = read_sweep(path.read_bytes())
            # The header's time is the sweep's own start, and the key's.
            self.assertEqual(parsed.scan_time, key_time(path.name), path.name)
            self.assertEqual(parsed.codes.shape, (BEAMS, 1840 if "_N0B_" in path.name else 1200))
            self.assertAlmostEqual(parsed.elevation, 0.5, places=1)

    def test_sails_sweeps_have_their_own_times(self) -> None:
        first = sweep("GWX_N0G_2023_03_25_01_36_23")
        second = sweep("GWX_N0G_2023_03_25_01_38_00")
        self.assertLess(first.scan_time, second.scan_time)
        self.assertGreater(first.codes.astype(bool).sum(), 0)

    def test_velocity_carries_range_folding(self) -> None:
        codes = sweep("GWX_N0G_2023_03_25_01_41_22").codes
        self.assertGreater(int(np.count_nonzero(codes == 1)), 0)

    def test_a_foreign_codebook_is_refused(self) -> None:
        data = bytearray((FIXTURES / "nexrad" / "DGX" / "DGX_N0B_2023_03_25_01_37_39").read_bytes())
        start = data.find(b"\r\r\n", data.find(b"\r\r\n") + 3) + 3
        # Halfword 32 (the increment) of the product description block.
        offset = start + 18 + (32 - 10) * 2
        data[offset : offset + 2] = (10).to_bytes(2, "big")
        with self.assertRaises(NexradProductError):
            read_sweep(bytes(data))

    def test_a_product_this_one_does_not_carry_is_refused(self) -> None:
        data = bytearray((FIXTURES / "nexrad" / "DGX" / "DGX_N0B_2023_03_25_01_37_39").read_bytes())
        start = data.find(b"\r\r\n", data.find(b"\r\r\n") + 3) + 3
        offset = start + 18 + (16 - 10) * 2
        data[offset : offset + 2] = (94).to_bytes(2, "big")
        with self.assertRaises(NexradProductError):
            read_sweep(bytes(data))


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name) / "n0g.zarr"
        names = ["GWX_N0G_2023_03_25_01_36_23", "GWX_N0G_2023_03_25_01_38_00"]
        gwx = [sweep(name) for name in names]
        dgx = [sweep("DGX_N0G_2023_03_25_01_37_39")]
        self.sites = [
            SiteSweeps("DGX", dgx[0].site_latitude, dgx[0].site_longitude, 100.0, dgx),
            SiteSweeps("GWX", gwx[0].site_latitude, gwx[0].site_longitude, 180.0, gwx),
        ]
        self.report = write_store(self.root, product="n0g", round_time=datetime(2023, 3, 25, 1, 40, tzinfo=UTC), sites=self.sites)

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_the_shard_round_trips_by_site(self) -> None:
        for index, entry in enumerate(self.sites):
            codes, times = read_site(self.root, "n0g", index)
            self.assertEqual(len(codes), len(entry.sweeps))
            for got, want in zip(codes, entry.sweeps):
                np.testing.assert_array_equal(got, want.codes)
            self.assertEqual(list(times), [int(s.scan_time.timestamp()) for s in entry.sweeps])

    def test_the_spans_read_without_the_index(self) -> None:
        shard = (self.root / "n0g" / "c" / "0" / "0" / "0" / "0").read_bytes()
        site, offset, length, sweeps = self.report.chunks[1]
        self.assertEqual((site, sweeps), ("GWX", 2))
        codes = np.frombuffer(zstdcli.decompress(shard[offset : offset + length], expected_length=2 * BEAMS * 1200), np.uint8)
        np.testing.assert_array_equal(codes.reshape(2, BEAMS, 1200)[1], self.sites[1].sweeps[1].codes)

    def test_the_group_names_its_branch(self) -> None:
        group = json.loads((self.root / "zarr.json").read_text())
        self.assertEqual(group["attributes"]["xue_polar"]["version"], 1)
        self.assertEqual(group["attributes"]["xue_polar"]["sites"], ["DGX", "GWX"])
        self.assertNotIn("xue", group["attributes"])
        array = json.loads((self.root / "n0g" / "zarr.json").read_text())
        self.assertEqual(array["dimension_names"], ["site", "scan", "azimuth", "range"])
        self.assertEqual(array["attributes"]["xue_polar"]["variable"]["signConvention"], "positive_away")
        self.assertEqual(array["codecs"][0]["configuration"]["chunk_shape"], [1, 2, BEAMS, 1200])

    def test_a_padding_sweep_is_marked_in_scan_time(self) -> None:
        times = np.frombuffer((self.root / "scan_time" / "c" / "0" / "0").read_bytes(), dtype="<i8").reshape(2, 2)
        self.assertEqual(int(times[0, 1]), -1)

    def test_a_stock_zarr_client_reads_it(self) -> None:
        try:
            import zarr
        except ImportError:
            self.skipTest("zarr-python is not installed (the `zarr` dependency group)")
        group = zarr.open_group(str(self.root), mode="r")
        np.testing.assert_array_equal(group["n0g"][1, 1], self.sites[1].sweeps[1].codes)
        np.testing.assert_array_equal(group["site_latitude"][:], [s.latitude for s in self.sites])


class WindowStoreTest(unittest.TestCase):
    """The fixture interval as a case: one store per product holding both
    rounds, each site's round one span of its one shard."""

    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        scratch = Path(self.scratch.name)
        raw = scratch / "raw"
        shutil.copytree(FIXTURES, raw)
        self.case = scratch / "case"
        self.report = build_case(start=START, end=END, sites=["GWX", "DGX"], raw_root=raw, output_root=self.case, fetch=False, now=END)
        self.rounds = replay(start=START, end=END, sites=["DGX", "GWX"], raw_root=raw, output_root=scratch / "live", fetch=False, now=END)
        self.live = scratch / "live"

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_a_case_is_two_stores_and_a_manifest(self) -> None:
        tops = sorted(path.name for path in self.case.iterdir())
        self.assertEqual(tops, ["index.json", "n0b.zarr", "n0g.zarr"])
        objects = [path for path in self.case.rglob("*") if path.is_file()]
        self.assertEqual(len(objects), 2 * (1 + 2 + 7 * 2) + 1)

    def test_its_rounds_are_the_replays_rounds(self) -> None:
        window = self.report["window"]
        live = self.rounds["window"]
        self.assertEqual(window["sites"], live["sites"])
        self.assertEqual([entry["round"] for entry in window["rounds"]], [entry["round"] for entry in live["rounds"]])
        for mine, theirs in zip(window["rounds"], live["rounds"]):
            self.assertEqual(mine["path"], "./")
            for product in ("n0b", "n0g"):
                self.assertEqual(product in mine, product in theirs)
                if product in mine:
                    self.assertEqual(mine[product]["scans"], theirs[product]["scans"])
                    self.assertEqual([row[0::3] for row in mine[product]["chunks"]], [row[0::3] for row in theirs[product]["chunks"]])

    def test_every_span_decodes_to_the_replays_sweeps(self) -> None:
        window = self.report["window"]
        sites = [row[0] for row in window["sites"]]
        for position, entry in enumerate(window["rounds"]):
            for product in ("n0b", "n0g"):
                block = entry.get(product)
                if block is None:
                    continue
                gates = {"n0b": 1840, "n0g": 1200}[product]
                shard = (self.case / f"{product}.zarr" / product / "c" / "0" / "0" / "0" / "0").read_bytes()
                self.assertEqual(len(shard), block["shard"]["byteLength"])
                live_root = self.live / f"nexrad.{entry['round'][:16].replace('-', '').replace('T', '').replace(':', '')}" / f"{product}.zarr"
                for site, offset, length, sweeps in block["chunks"]:
                    raw = zstdcli.decompress(shard[offset : offset + length], expected_length=block["depth"] * BEAMS * gates)
                    codes = np.frombuffer(raw, np.uint8).reshape(block["depth"], BEAMS, gates)[:sweeps]
                    live_group = json.loads((live_root / "zarr.json").read_text())
                    want, _times = read_site(live_root, product, live_group["attributes"]["xue_polar"]["sites"].index(sites[site]))
                    np.testing.assert_array_equal(codes, want)
                    store_group = json.loads((self.case / f"{product}.zarr" / "zarr.json").read_text())
                    through_index, _ = read_site(self.case / f"{product}.zarr", product, store_group["attributes"]["xue_polar"]["sites"].index(sites[site]), position)
                    np.testing.assert_array_equal(through_index, want)

    def test_the_group_names_its_rounds(self) -> None:
        group = json.loads((self.case / "n0g.zarr" / "zarr.json").read_text())
        block = group["attributes"]["xue_polar"]
        self.assertEqual(block["rounds"], ["2023-03-25T01:40:00Z", "2023-03-25T01:45:00Z"])
        self.assertNotIn("round", block)
        array = json.loads((self.case / "n0g.zarr" / "n0g" / "zarr.json").read_text())
        depth = array["codecs"][0]["configuration"]["chunk_shape"][1]
        self.assertEqual(array["shape"][1], 2 * depth)
        self.assertEqual(depth, self.report["stores"]["n0g"]["depth"])

    def test_a_site_without_a_sweep_in_a_round_reads_empty(self) -> None:
        group = json.loads((self.case / "n0b.zarr" / "zarr.json").read_text())
        sites = group["attributes"]["xue_polar"]["sites"]
        empty = [
            (row, column)
            for row in range(len(sites))
            for column in range(2)
            if len(read_site(self.case / "n0b.zarr", "n0b", row, column)[0]) == 0
        ]
        listed = {(row[0], position) for position, entry in enumerate(self.report["window"]["rounds"]) for row in entry.get("n0b", {}).get("chunks", [])}
        self.assertEqual(len(empty) + len(listed), len(sites) * 2)

    def test_a_sweep_past_the_last_round_is_refused(self) -> None:
        late = sweep("GWX_N0G_2023_03_25_01_38_00")
        with self.assertRaises(NexradProductError):
            write_window_store(
                Path(self.scratch.name) / "late.zarr",
                product="n0g",
                rounds=[datetime(2023, 3, 25, 1, 35, tzinfo=UTC)],
                sites=[SiteSweeps("GWX", 0.0, 0.0, 0.0, [late])],
            )

    def test_a_stock_zarr_client_reads_it(self) -> None:
        try:
            import zarr
        except ImportError:
            self.skipTest("zarr-python is not installed (the `zarr` dependency group)")
        group = zarr.open_group(str(self.case / "n0g.zarr"), mode="r")
        times = group["scan_time"][:]
        row, column = np.argwhere(times != -1)[-1]
        codes, _ = read_site(self.case / "n0g.zarr", "n0g", int(row), int(column) // group["n0g"].chunks[1])
        np.testing.assert_array_equal(group["n0g"][int(row), int(column)], codes[int(column) % group["n0g"].chunks[1]])


class WindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.raw = Path(self.scratch.name) / "raw"
        shutil.copytree(FIXTURES, self.raw)
        self.output = Path(self.scratch.name) / "out"

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def build(self) -> dict:
        return replay(start=START, end=END, sites=["DGX", "GWX"], raw_root=self.raw, output_root=self.output, fetch=False, now=END)

    def test_two_rounds_make_a_valid_window(self) -> None:
        report = self.build()
        window = read_window(self.output / "nexrad.202303250145" / WINDOW_FILENAME)
        self.assertEqual([row[0] for row in window["sites"]], ["DGX", "GWX"])
        self.assertEqual([entry["round"] for entry in window["rounds"]], ["2023-03-25T01:40:00Z", "2023-03-25T01:45:00Z"])
        first, second = window["rounds"]
        # GWX's two MESO-SAILS sweeps before 01:40 share the first round.
        self.assertEqual(first["n0g"]["scans"], [[0, [1679708259]], [1, [1679708183, 1679708280]]])
        self.assertEqual([row[0] for row in first["n0b"]["chunks"]], [0])
        self.assertEqual(second["n0g"]["scans"], [[0, [1679708597]], [1, [1679708482]]])
        self.assertEqual(report["round"], "202303250145")

    def test_every_round_path_resolves_from_the_manifest(self) -> None:
        self.build()
        manifest = self.output / "nexrad.202303250145" / WINDOW_FILENAME
        window = json.loads(manifest.read_text())
        for entry in window["rounds"]:
            for product in ("n0b", "n0g"):
                if product in entry:
                    shard = (manifest.parent / entry["path"] / f"{product}.zarr" / product / "c" / "0" / "0" / "0" / "0").resolve()
                    self.assertEqual(shard.stat().st_size, entry[product]["shard"]["byteLength"])

    def test_a_late_sweep_lands_in_the_next_round(self) -> None:
        late = self.raw / "nexrad" / "GWX" / "GWX_N0G_2023_03_25_01_38_00"
        held = late.read_bytes()
        late.unlink()
        stations = load_stations(self.raw, fetch=False)
        first = build_round(parse_round("202303250140"), raw_root=self.raw, output_root=self.output, stations=stations, sites=["DGX", "GWX"], previous=None, window_seconds=600, first_start=START, fetch=False, now=END)
        late.write_bytes(held)
        second = build_round(parse_round("202303250145"), raw_root=self.raw, output_root=self.output, stations=stations, sites=["DGX", "GWX"], previous=first["window"], window_seconds=600, first_start=START, fetch=False, now=END)
        rounds = second["window"]["rounds"]
        self.assertEqual(rounds[0]["n0g"]["scans"][1], [1, [1679708183]])
        # The 01:38:00 sweep arrived after round 01:40 was built, so the next round carries it.
        self.assertEqual(rounds[1]["n0g"]["scans"][1], [1, [1679708280, 1679708482]])

    def test_a_gap_is_filled_with_ordinary_rounds(self) -> None:
        stations = load_stations(self.raw, fetch=False)
        first = build_round(parse_round("202303250135"), raw_root=self.raw, output_root=self.output, stations=stations, sites=["DGX", "GWX"], previous=None, window_seconds=3600, first_start=START, fetch=False, now=END)
        built = catch_up(END, raw_root=self.raw, output_root=self.output, stations=stations, sites=["DGX", "GWX"], previous=first["window"], window_seconds=3600, fetch=False, now=END)
        # A run that missed 01:40 builds it before 01:45, rather than one
        # round holding both.
        self.assertEqual([report["round"] for report in built], ["202303250140", "202303250145"])
        self.assertEqual(len(built[-1]["window"]["rounds"]), 2)

    def test_the_pointer_names_the_newest_window(self) -> None:
        report = self.build()
        path = write_pointer(self.output, parse_round("202303250145"), report["windowBytes"])
        pointer = json.loads(path.read_text())
        self.assertEqual(pointer["path"], "nexrad.202303250145/index.json")
        validate_pointer(pointer)
        with self.assertRaises(NexradProductError):
            validate_pointer(pointer | {"path": "nexrad.202303250140/index.json"})


def _without_compressed_bytes(window: dict) -> dict:
    """A window manifest with what depends on the compressor's bytes taken
    out: each shard's length and CRC and each chunk's span. What is left —
    sites, rounds, sweep counts and times, the store roots — is the same
    whichever zstd engine wrote the stores."""
    window = copy.deepcopy(window)
    for entry in window["rounds"]:
        for product in ("n0b", "n0g"):
            block = entry.get(product)
            if block:
                block.pop("shard")
                block["chunks"] = [[site, sweeps] for site, _offset, _length, sweeps in block["chunks"]]
    window.pop("generated", None)
    return window


class GoldenTest(unittest.TestCase):
    def test_the_rounds_match_the_golden_whatever_the_compressor(self) -> None:
        from tests.prepare_nexrad_golden import ROUND, build

        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            build(output)
            built = json.loads((output / ROUND / WINDOW_FILENAME).read_bytes())
            golden = json.loads((FIXTURES / "expected" / ROUND / WINDOW_FILENAME).read_bytes())
            self.assertEqual(_without_compressed_bytes(built), _without_compressed_bytes(golden))

    def test_the_case_matches_the_golden_whatever_the_compressor(self) -> None:
        from tests.prepare_nexrad_golden import CASE, build_case_window

        with tempfile.TemporaryDirectory() as scratch:
            built = build_case_window(Path(scratch))["window"]
            golden = json.loads((FIXTURES / "expected" / CASE / WINDOW_FILENAME).read_bytes())
            for window in (built, golden):
                for entry in window["rounds"]:
                    for product in ("n0b", "n0g"):
                        if product in entry:
                            entry[product].pop("group")
            self.assertEqual(_without_compressed_bytes(built), _without_compressed_bytes(golden))

    def test_the_build_matches_the_golden(self) -> None:
        from tests.prepare_nexrad_golden import ROUND, build

        # The golden pins the stores' compressed bytes (through every CRC
        # that covers them), which only the in-process engine reproduces:
        # the zstd CLI (Python < 3.14) streams and writes other bytes.
        if not zstdcli.compresses_in_process():
            self.skipTest("the stores are compressed through the zstd CLI, which cannot match the golden's bytes")
        expected = FIXTURES / "expected"
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            build(output)
            for relative in (f"{ROUND}/{WINDOW_FILENAME}", f"{ROUND}/item.json", "nexrad/collection.json", "nexrad/item.json", "latest-nexrad.json"):
                with self.subTest(relative):
                    self.assertEqual(json.loads((output / relative).read_bytes()), json.loads((expected / relative).read_bytes()))
            from tests.prepare_nexrad_golden import CASE, build_case_window

            build_case_window(output / CASE)
            self.assertEqual(json.loads((output / CASE / WINDOW_FILENAME).read_bytes()), json.loads((expected / CASE / WINDOW_FILENAME).read_bytes()))


class ValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        scratch = tempfile.TemporaryDirectory()
        raw = Path(scratch.name) / "raw"
        shutil.copytree(FIXTURES, raw)
        cls.window = replay(start=START, end=END, sites=["DGX", "GWX"], raw_root=raw, output_root=Path(scratch.name) / "out", fetch=False, now=END)["window"]
        scratch.cleanup()

    def broken(self, change) -> dict:
        window = copy.deepcopy(self.window)
        change(window)
        return window

    def test_the_built_window_passes(self) -> None:
        validate_window(self.window)

    def test_malformed_windows_are_refused(self) -> None:
        cases = {
            "overlapping spans": lambda w: w["rounds"][0]["n0g"]["chunks"][1].__setitem__(1, 0),
            "span past the shard": lambda w: w["rounds"][0]["n0g"]["chunks"][1].__setitem__(2, 10**9),
            "sweep count mismatch": lambda w: w["rounds"][0]["n0g"]["chunks"][1].__setitem__(3, 3),
            "sweep after its round": lambda w: w["rounds"][0]["n0g"]["scans"][0][1].__setitem__(0, 1679709000),
            "sweep repeated across rounds": lambda w: w["rounds"][1]["n0g"]["scans"][1].__setitem__(1, [1679708280]),
            "round outside the window": lambda w: w.__setitem__("windowSeconds", 60),
            "path not ../": lambda w: w["rounds"][0].__setitem__("path", "nexrad.202303250140/"),
            "site past the table": lambda w: w["rounds"][0]["n0b"]["chunks"][0].__setitem__(0, 7),
            "duplicate site": lambda w: w["sites"].append(list(w["sites"][0])),
            "schema version": lambda w: w.__setitem__("schemaVersion", 2),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(NexradProductError):
                validate_window(self.broken(change))

    def test_malformed_case_windows_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            raw = Path(scratch) / "raw"
            shutil.copytree(FIXTURES, raw)
            case = build_case(start=START, end=END, sites=["DGX", "GWX"], raw_root=raw, output_root=Path(scratch) / "case", fetch=False, now=END)["window"]
        validate_window(case)
        cases = {
            "depth missing": lambda w: w["rounds"][0]["n0g"].pop("depth"),
            "depth differs": lambda w: w["rounds"][1]["n0g"].__setitem__("depth", 9),
            "sweeps past the depth": lambda w: w["rounds"][0]["n0g"].__setitem__("depth", 1) or w["rounds"][1]["n0g"].__setitem__("depth", 1),
            "another shard": lambda w: w["rounds"][1]["n0g"]["shard"].__setitem__("crc32", "00000000"),
            "mixed paths": lambda w: w["rounds"][1].__setitem__("path", "../nexrad.202303250145/"),
        }
        for name, change in cases.items():
            window = copy.deepcopy(case)
            change(window)
            with self.subTest(name), self.assertRaises(NexradProductError):
                validate_window(window)
        live = copy.deepcopy(self.window)
        live["rounds"][0]["n0g"]["depth"] = 2
        with self.assertRaises(NexradProductError):
            validate_window(live)

    def test_stations(self) -> None:
        stations = parse_stations((FIXTURES / "nexrad" / "nexrad-stations.txt").read_text())
        self.assertEqual(sorted(stations), ["DGX", "GWX"])
        self.assertEqual(stations["GWX"].icao, "KGWX")


if __name__ == "__main__":
    unittest.main()

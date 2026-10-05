"""The synop product (``xuebuild/synop``): the AMeDAS adapter's readers on
the fetched fixture and on constructed cells, the merge, the index's byte
spans and current values, the validators on malformed input, and two
consecutive rounds held to a committed golden.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.prepare_synop_golden import FIXTURES, GENERATED, ROUNDS, build_rounds, pretty
from xuebuild.errors import SynopProductError
from xuebuild.synop import amedas
from xuebuild.synop.build import build_product, load_previous_index
from xuebuild.synop.schema import (
    ELEMENTS,
    ROW_HEAD,
    build_pointer,
    encode_json,
    floor_round,
    parse_round,
    read_index,
    round_directory,
    validate_index,
    validate_pointer,
    validate_station_line,
)

EXPECTED = FIXTURES / "expected"
TABLE = json.loads((FIXTURES / "amedas-stations.json").read_text(encoding="utf-8"))


def column(key: str) -> int:
    return len(ROW_HEAD) + ELEMENTS.index(key)


def row_of(index: dict, station: str) -> list:
    return next(row for row in index["stations"] if row[0] == station)


class AdapterTest(unittest.TestCase):
    def test_wind_direction_is_sixteen_points_with_calm_as_none(self) -> None:
        self.assertIsNone(amedas.wind_direction(0))
        self.assertEqual(amedas.wind_direction(1), 23)
        self.assertEqual(amedas.wind_direction(4), 90)
        self.assertEqual(amedas.wind_direction(16), 360)
        self.assertIsNone(amedas.wind_direction(17))
        self.assertIsNone(amedas.wind_direction(None))

    def test_only_normal_and_quasi_normal_values_are_kept(self) -> None:
        converted = amedas.convert(
            {
                "temp": [12.3, 0],
                "humidity": [100, 1],
                "pressure": [1000.0, 2],
                "wind": [3.0, 5],
                "precipitation10m": [None, 0],
                "visibility": [20000.0, 6],
                "sun10m": [10, None],
            }
        )
        self.assertEqual(converted["t"], 12.3)
        self.assertEqual(converted["rh"], 100)
        for key in ("p", "ws", "pr", "vis", "sun"):
            self.assertIsNone(converted[key], key)

    def test_a_value_out_of_range_becomes_null(self) -> None:
        self.assertIsNone(amedas.convert({"temp": [999.0, 0]})["t"])
        self.assertIsNone(amedas.convert({"humidity": [-5, 0]})["rh"])

    def test_table_ranks_and_positions(self) -> None:
        stations = amedas.parse_table(json.dumps(TABLE))
        self.assertEqual(stations["50066"].rank, 0)  # Mt. Fuji, type F
        self.assertEqual(stations["11001"].rank, 1)  # an automatic station
        self.assertEqual(stations["12217"].rank, 2)  # precipitation only
        self.assertEqual(stations["50066"].id, "amedas:50066")
        self.assertEqual(stations["50066"].elev, 3775.0)
        self.assertEqual(stations["50066"].lat, round(35 + 21.6 / 60, 4))
        self.assertEqual(stations["50066"].names, {"ja": "富士山", "ja-Kana": "フジサン"})

    def test_snapshot_times(self) -> None:
        latest = datetime(2026, 10, 5, 0, 30, tzinfo=UTC)
        self.assertEqual(len(amedas.snapshot_times(latest, None)), 144)
        self.assertEqual(
            amedas.snapshot_times(latest, latest - timedelta(minutes=20)),
            [latest - timedelta(minutes=10), latest],
        )
        self.assertEqual(amedas.snapshot_times(latest, latest), [])
        self.assertEqual(len(amedas.snapshot_times(latest, latest - timedelta(days=3))), 144)

    def test_stamps_are_jst(self) -> None:
        moment = datetime(2026, 10, 5, 0, 10, tzinfo=UTC)
        self.assertEqual(amedas.snapshot_stamp(moment), "20261005091000")
        self.assertEqual(amedas.parse_stamp("20261005091000"), moment)
        self.assertEqual(amedas.parse_latest_time("2026-10-05T09:10:00+09:00"), moment)
        with self.assertRaises(SynopProductError):
            amedas.parse_latest_time("2026-10-05T09:10:00")


class RoundsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory()
        cls.output = Path(cls.scratch.name)
        cls.reports = build_rounds(cls.output)
        cls.directories = [cls.output / round_directory(parse_round(name)) for name in ROUNDS]
        cls.indexes = [read_index(directory / "index.json") for directory in cls.directories]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scratch.cleanup()

    def test_golden(self) -> None:
        for directory in self.directories:
            expected = EXPECTED / directory.name
            self.assertEqual(pretty(json.loads((directory / "index.json").read_bytes())) + "\n", (expected / "index.json").read_text())
            self.assertEqual((directory / "amedas.jsonl").read_bytes(), (expected / "amedas.jsonl").read_bytes())
            self.assertEqual(pretty(json.loads((directory / "item.json").read_bytes())) + "\n", (expected / "item.json").read_text())
        for relative in ("synop/collection.json", "synop/item.json", "latest-synop.json"):
            self.assertEqual(pretty(json.loads((self.output / relative).read_bytes())) + "\n", (EXPECTED / relative).read_text(), relative)

    def test_every_span_slices_out_its_own_station(self) -> None:
        for directory, index in zip(self.directories, self.indexes):
            body = (directory / "amedas.jsonl").read_bytes()
            for row in index["stations"]:
                station = json.loads(body[row[-2] : row[-2] + row[-1]])
                self.assertEqual(station["id"], row[0])
                validate_station_line(station, row[0])

    def test_the_second_round_merges_onto_the_first(self) -> None:
        first, second = self.indexes
        tokyo = json.loads(self._line(1, "amedas:44132"))
        self.assertEqual(len(tokyo["time"]), 3)
        self.assertEqual(tokyo["time"], sorted(tokyo["time"]))
        self.assertEqual(second["networks"][0]["latest"], "2026-10-05T00:10:00Z")
        self.assertEqual(first["networks"][0]["latest"], "2026-10-05T00:00:00Z")

    def test_a_station_the_table_does_not_place_is_dropped(self) -> None:
        ids = {row[0] for row in self.indexes[1]["stations"]}
        self.assertNotIn("amedas:12066", ids)
        self.assertIn("amedas:12217", ids)

    def test_slow_elements_carry_into_the_index_within_the_hour(self) -> None:
        fuji = row_of(self.indexes[1], "amedas:50066")
        self.assertEqual(fuji[7], "2026-10-05T00:10:00Z")
        self.assertIsNotNone(fuji[column("rh")])  # only on the hour
        self.assertIsNotNone(fuji[column("p")])

    def test_accumulations_are_never_borrowed_from_an_older_period(self) -> None:
        otani = row_of(self.indexes[1], "amedas:31386")
        self.assertIsNone(otani[column("pr")])  # quality 4 at the newest time
        self.assertEqual(otani[column("pr1h")], 0.0)  # quality 1 is kept

    def test_the_pointer_names_the_newest_round(self) -> None:
        pointer = json.loads((self.output / "latest-synop.json").read_bytes())
        self.assertEqual(pointer["path"], f"{self.directories[1].name}/index.json")
        validate_pointer(pointer)

    def _line(self, position: int, station: str) -> bytes:
        index = self.indexes[position]
        row = row_of(index, station)
        body = (self.directories[position] / "amedas.jsonl").read_bytes()
        return body[row[-2] : row[-2] + row[-1]]


class MergeTest(unittest.TestCase):
    """Rounds built to trip the merge, on a copy of the fixture."""

    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        root = Path(self.scratch.name)
        self.raw = root / "raw"
        self.output = root / "out"
        shutil.copytree(FIXTURES, self.raw, ignore=shutil.ignore_patterns("expected"))
        self.first = parse_round(ROUNDS[0])
        build_product(self.first, self.raw, self.output, now=GENERATED[0])

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def _record(self, moment: datetime, *, map_ok: bool) -> None:
        directory = self.raw / round_directory(moment)
        directory.mkdir(parents=True, exist_ok=True)
        status = {"id": "jma-amedas-map", "ok": map_ok, "fetched": "2026-10-05T01:00:00Z"}
        if map_ok:
            status |= {"file": f"{directory.name}/amedas", "snapshots": 0, "missing": 0}
        else:
            status |= {"error": "HTTP Error 503"}
        record = {
            "round": directory.name.split(".")[1],
            "sources": [status, {"id": "jma-amedas-table", "ok": True, "file": "amedas-stations.json"}],
        }
        (directory / "fetch.json").write_text(json.dumps(record))

    def test_a_failed_network_keeps_its_history_and_withholds_the_pointer(self) -> None:
        later = self.first + timedelta(minutes=30)
        self._record(later, map_ok=False)
        (self.output / "latest-synop.json").unlink()
        previous = read_index(self.output / round_directory(self.first) / "index.json")
        report = build_product(later, self.raw, self.output, previous_index=previous, now=later)
        self.assertIsNone(report["pointer"])
        index = read_index(self.output / round_directory(later) / "index.json")
        self.assertEqual(len(index["stations"]), len(previous["stations"]))
        self.assertFalse(index["sources"][0]["ok"])

    def test_the_window_drops_what_is_older_than_a_day(self) -> None:
        later = self.first + timedelta(hours=24)
        self._record(later, map_ok=True)
        previous = load_previous_index(None, self.output)
        report = build_product(later, self.raw, self.output, previous_index=previous, now=later)
        self.assertIsNotNone(report["pointer"])  # the network answered, with nothing new
        index = read_index(self.output / round_directory(later) / "index.json")
        # 23:50 fell out; the 00:00 snapshot is exactly 24 hours old and stays.
        self.assertTrue(index["stations"])
        self.assertTrue(all(row[7] == "2026-10-05T00:00:00Z" for row in index["stations"]))
        later = later + timedelta(minutes=10)
        self._record(later, map_ok=True)
        build_product(later, self.raw, self.output, previous_index=load_previous_index(None, self.output), now=later)
        index = read_index(self.output / round_directory(later) / "index.json")
        self.assertEqual(index["stations"], [])
        self.assertEqual(index["networks"], [])

    def test_a_revised_value_replaces_the_one_it_revises(self) -> None:
        second = parse_round(ROUNDS[1])
        snapshot = self.raw / round_directory(second) / "amedas" / "map" / "20261005090000.json"
        cells = json.loads((self.raw / round_directory(self.first) / "amedas" / "map" / "20261005090000.json").read_text())
        cells["44132"]["temp"] = [21.5, 0]
        snapshot.write_text(json.dumps(cells))
        build_product(second, self.raw, self.output, previous_index=load_previous_index(None, self.output), now=GENERATED[1])
        index = read_index(self.output / round_directory(second) / "index.json")
        row = row_of(index, "amedas:44132")
        line = json.loads((self.output / round_directory(second) / "amedas.jsonl").read_bytes()[row[-2] : row[-2] + row[-1]])
        position = line["time"].index(int(datetime(2026, 10, 5, 0, 0, tzinfo=UTC).timestamp()))
        self.assertEqual(line["obs"]["t"][position], 21.5)

    def test_an_existing_round_is_not_rebuilt_without_force(self) -> None:
        with self.assertRaises(SynopProductError):
            build_product(self.first, self.raw, self.output, now=GENERATED[0])


class ValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = json.loads((EXPECTED / f"synop.{ROUNDS[1]}" / "index.json").read_text())
        line = (EXPECTED / f"synop.{ROUNDS[1]}" / "amedas.jsonl").read_bytes().splitlines()[0]
        cls.line = json.loads(line)

    def broken_index(self, change) -> dict:
        index = copy.deepcopy(self.index)
        change(index)
        return index

    def test_the_golden_passes(self) -> None:
        validate_index(self.index)
        validate_station_line(self.line, "line")

    def test_malformed_indexes_are_refused(self) -> None:
        cases = {
            "unsorted": lambda index: index["stations"].reverse(),
            "span past the file": lambda index: index["stations"][-1].__setitem__(-1, 10**7),
            "overlapping spans": lambda index: index["stations"][1].__setitem__(-2, 0),
            "wrong network": lambda index: index["stations"][0].__setitem__(0, "gts:47401"),
            "short row": lambda index: index["stations"][0].pop(),
            "temperature out of range": lambda index: index["stations"][0].__setitem__(column("t"), 99.0),
            "fractional humidity": lambda index: index["stations"][0].__setitem__(column("rh"), 63.5),
            "off-round issue": lambda index: index.__setitem__("issued", "2026-10-05T00:15:00Z"),
            "bad file name": lambda index: index["networks"][0]["file"].__setitem__("path", "history.jsonl"),
            "network twice": lambda index: index["networks"].append(index["networks"][0]),
            "schema version": lambda index: index.__setitem__("schemaVersion", 2),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(SynopProductError):
                validate_index(self.broken_index(change))

    def test_unknown_elements_and_networks_are_admitted(self) -> None:
        def widen(index: dict) -> None:
            index["elements"].append("dewpoint2")
            for row in index["stations"]:
                row.insert(len(ROW_HEAD) + len(ELEMENTS), 1.5)

        validate_index(self.broken_index(widen))

    def test_malformed_lines_are_refused(self) -> None:
        cases = {
            "time not increasing": lambda line: line["time"].reverse(),
            "column too short": lambda line: line["obs"]["t"].pop(),
            "bad id": lambda line: line.__setitem__("id", "11001"),
            "rank": lambda line: line.__setitem__("rank", 3),
        }
        for name, change in cases.items():
            line = copy.deepcopy(self.line)
            change(line)
            with self.subTest(name), self.assertRaises(SynopProductError):
                validate_station_line(line, "line")

    def test_pointer(self) -> None:
        moment = parse_round(ROUNDS[1])
        pointer = build_pointer(moment, f"{round_directory(moment)}/index.json", encode_json(self.index))
        validate_pointer(pointer)
        with self.assertRaises(SynopProductError):
            validate_pointer(pointer | {"path": f"synop.{ROUNDS[0]}/index.json"})

    def test_rounds(self) -> None:
        self.assertEqual(floor_round(datetime(2026, 10, 5, 0, 19, 59, tzinfo=UTC)), parse_round("202610050010"))
        for value in ("202610050015", "2026100500", "202613050010"):
            with self.subTest(value), self.assertRaises(SynopProductError):
                parse_round(value)


if __name__ == "__main__":
    unittest.main()

"""The aq product (``xuebuild/aq``): the AirNow and EPD adapters' readers on
the fetched fixture and on constructed input, the unit conversion, the
merge, the stations files' byte spans and current values, the validators on
malformed input, and two consecutive rounds held to a committed golden.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.prepare_aq_golden import FIXTURES, GENERATED, ROUNDS, build_rounds, pretty
from xuebuild.aq import airnow, hkepd
from xuebuild.aq.build import build_product, load_previous_index
from xuebuild.aq.schema import (
    ELEMENTS,
    ROW_HEAD,
    AqProductError,
    bounded,
    build_pointer,
    encode_json,
    floor_round,
    parse_round,
    read_index,
    round_directory,
    to_mixing_ratio,
    validate_index,
    validate_pointer,
    validate_station_line,
    validate_stations_file,
)

EXPECTED = FIXTURES / "expected"
FIRST, SECOND = (parse_round(name) for name in ROUNDS)
AIRNOW_HEADER = (
    '"AQSID","SiteName","Status","EPARegion","Latitude","Longitude","Elevation","GMTOffset","CountryCode",'
    '"StateName","ValidDate","ValidTime","DataSource","ReportingArea_PipeDelimited","OZONE_AQI","PM10_AQI",'
    '"PM25_AQI","NO2_AQI","OZONE_Measured","PM10_Measured","PM25_Measured","NO2_Measured","PM25","PM25_Unit",'
    '"OZONE","OZONE_Unit","NO2","NO2_Unit","CO","CO_Unit","SO2","SO2_Unit","PM10","PM10_Unit"'
)


def column(key: str) -> int:
    return len(ROW_HEAD) + ELEMENTS.index(key)


def epoch(*parts: int) -> int:
    return int(datetime(*parts, tzinfo=UTC).timestamp())


def airnow_row(code: str, *, lat: str = "34.0", lon: str = "-118.0", elev: str = "30.5", pm25: str = "5.3", unit: str = "UG/M3", name: str = "Site") -> str:
    return (
        f'"{code}","{name}","Active","R9","{lat}","{lon}","{elev}","-8","US","CA","10/31/2026","13:00","Agency",'
        f'"","","","","","0","0","1","0","{pm25}","{unit}","","","","","","","","","",""'
    )


class AdapterTest(unittest.TestCase):
    def test_file_hours_reread_the_recent_three_and_fill_a_gap(self) -> None:
        moment = datetime(2026, 10, 31, 15, 45, tzinfo=UTC)
        hours = airnow.file_hours(moment, None)
        self.assertEqual(len(hours), 24)
        self.assertEqual(hours[-1], datetime(2026, 10, 31, 14, tzinfo=UTC))
        self.assertEqual(hours, sorted(hours))
        # Up to date: the three newest hours again, nothing older.
        latest = datetime(2026, 10, 31, 15, tzinfo=UTC)
        self.assertEqual(airnow.file_hours(moment, latest), [datetime(2026, 10, 31, hour, tzinfo=UTC) for hour in (12, 13, 14)])
        # Five hours behind: everything after the previous latest.
        behind = airnow.file_hours(moment, latest - timedelta(hours=5))
        self.assertEqual(behind[0], datetime(2026, 10, 31, 10, tzinfo=UTC))
        self.assertEqual(len(behind), 5)
        self.assertEqual(len(airnow.file_hours(moment, latest - timedelta(days=3))), 24)

    def test_file_urls_are_in_the_dated_archive(self) -> None:
        self.assertEqual(
            airnow.file_url(datetime(2026, 10, 31, 13, tzinfo=UTC)),
            "https://files.airnowtech.org/airnow/2026/20261031/HourlyAQObs_2026103113.dat",
        )

    def test_airnow_rows(self) -> None:
        body = "\n".join(
            [
                AIRNOW_HEADER,
                airnow_row("060371103", pm25="-0.2"),
                airnow_row("060371104", elev="0.0"),
                airnow_row("060371105", unit="PPB"),
                airnow_row("060371106", lat="0.0", lon="0.0"),
                airnow_row("060371107", pm25="-7.0", name="Montréal"),
            ]
        ).encode("utf-8")
        rows, unplaced = airnow.parse_file(body)
        self.assertEqual(unplaced, 1)
        by_id = {meta.id: (meta, time, elements) for meta, time, elements in rows}
        meta, time, elements = by_id["airnow:060371103"]
        self.assertEqual(time, epoch(2026, 10, 31, 14))  # ValidTime is the start of the hour
        self.assertEqual(elements["pm25"], -0.2)  # small negatives stay as AirNow states them
        self.assertEqual(meta.agency, "Agency")
        self.assertEqual(meta.elev, 30.5)
        self.assertIsNone(by_id["airnow:060371104"][0].elev)  # 0.0 is AirNow's unknown
        self.assertIsNone(by_id["airnow:060371105"][2]["pm25"])  # wrong unit
        self.assertIsNone(by_id["airnow:060371107"][2]["pm25"])  # below the floor
        self.assertEqual(by_id["airnow:060371107"][0].name, "Montréal")

    def test_airnow_without_its_columns_is_refused(self) -> None:
        with self.assertRaises(AqProductError):
            airnow.parse_file(b'"AQSID","SiteName"\n"1","x"\n')

    def test_mixing_ratio_at_20_degrees(self) -> None:
        self.assertAlmostEqual(to_mixing_ratio("o3", 48.00), 24.04)
        self.assertAlmostEqual(to_mixing_ratio("no2", 46.01), 24.04)
        self.assertAlmostEqual(to_mixing_ratio("so2", 64.07), 24.04)
        self.assertAlmostEqual(to_mixing_ratio("co", 28010.0), 24.04)
        self.assertEqual(bounded("co", to_mixing_ratio("co", 646.0)), 0.55)
        self.assertEqual(bounded("o3", to_mixing_ratio("o3", 105.6)), 52.9)

    def test_bounds_and_precision(self) -> None:
        self.assertEqual(bounded("pm25", 12.34), 12.3)
        self.assertEqual(bounded("co", 0.333), 0.33)
        self.assertEqual(bounded("pm25", -0.04), 0.0)
        self.assertIsNone(bounded("pm25", -5.1))
        self.assertIsNone(bounded("co", 101.0))
        self.assertIsNone(bounded("o3", None))

    def test_epd_times_are_the_end_of_the_hour_in_utc(self) -> None:
        data = (FIXTURES / f"aq.{ROUNDS[0]}" / "hkepd" / "24pc_Eng.xml").read_bytes()
        rows = hkepd.parse_file(data)
        times = sorted({time for _, time, _ in rows})
        self.assertEqual(len(times), 24)
        self.assertEqual(times[-1], epoch(2026, 10, 31, 14))  # 22:00 +0800
        names = {name for name, _, _ in rows}
        self.assertIn("Unlisted Station", names)

    def test_epd_values(self) -> None:
        xml = (
            b"<r><PollutantConcentration><StationName>Central</StationName>"
            b"<DateTime>Sat, 31 Oct 2026 22:00:00 +0800</DateTime><NO2>46.01</NO2><O3>-</O3><SO2>64.07</SO2>"
            b"<CO>646.0</CO><PM10>40.9</PM10><PM2.5>27.5</PM2.5></PollutantConcentration></r>"
        )
        ((name, time, elements),) = hkepd.parse_file(xml)
        self.assertEqual(name, "Central")
        self.assertEqual(time, epoch(2026, 10, 31, 14))
        self.assertEqual(elements, {"pm25": 27.5, "pm10": 40.9, "o3": None, "no2": 24.0, "so2": 24.0, "co": 0.55})

    def test_epd_table(self) -> None:
        table = hkepd.load_table()
        self.assertEqual(len(table), 18)
        self.assertEqual({meta.id for meta in table.values() if meta.rank == 2}, {"hkepd:causeway-bay", "hkepd:central", "hkepd:mong-kok"})
        self.assertEqual(table["Central/Western"].id, "hkepd:central-western")
        self.assertEqual(table["Central/Western"].names, {"zh-Hant": "中西區"})
        self.assertTrue(all(meta.agency == "EPD" for meta in table.values()))


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

    def table(self, position: int, network: str) -> dict:
        return json.loads((self.directories[position] / f"{network}.stations.json").read_bytes())

    def row(self, position: int, station: str) -> list:
        return next(row for row in self.table(position, station.split(":")[0])["stations"] if row[0] == station)

    def line(self, position: int, station: str) -> dict:
        row = self.row(position, station)
        body = (self.directories[position] / f"{station.split(':')[0]}.jsonl").read_bytes()
        return json.loads(body[row[-2] : row[-2] + row[-1]])

    def test_golden(self) -> None:
        for directory, index in zip(self.directories, self.indexes):
            expected = EXPECTED / directory.name
            self.assertEqual(pretty(json.loads((directory / "index.json").read_bytes())) + "\n", (expected / "index.json").read_text())
            for network in index["networks"]:
                path, table = network["file"]["path"], network["stations"]["path"]
                self.assertEqual((directory / path).read_bytes(), (expected / path).read_bytes(), path)
                self.assertEqual(pretty(json.loads((directory / table).read_bytes())) + "\n", (expected / table).read_text(), table)
            if (expected / "item.json").exists():
                self.assertEqual(pretty(json.loads((directory / "item.json").read_bytes())) + "\n", (expected / "item.json").read_text())
        for relative in ("aq/collection.json", "aq/item.json", "latest-aq.json"):
            if (EXPECTED / relative).exists():
                self.assertEqual(pretty(json.loads((self.output / relative).read_bytes())) + "\n", (EXPECTED / relative).read_text(), relative)

    def test_every_span_slices_out_its_own_station(self) -> None:
        for directory, index in zip(self.directories, self.indexes):
            for network in index["networks"]:
                table = json.loads((directory / network["stations"]["path"]).read_bytes())
                validate_stations_file(table, network, index["elements"], index["issued"])
                body = (directory / network["file"]["path"]).read_bytes()
                self.assertEqual(len(body), network["file"]["byteLength"])
                for row in table["stations"]:
                    station = json.loads(body[row[-2] : row[-2] + row[-1]])
                    self.assertEqual(station["id"], row[0])
                    validate_station_line(station, row[0])
                self.assertEqual(len(body.splitlines()), network["stations"]["count"])

    def test_the_second_round_adds_the_new_hour(self) -> None:
        first, second = self.indexes
        self.assertEqual(first["networks"][0]["latest"], "2026-10-31T14:00:00Z")
        self.assertEqual(second["networks"][0]["latest"], "2026-10-31T15:00:00Z")
        charlottetown = self.line(1, "airnow:000020104")
        self.assertEqual(charlottetown["time"], [epoch(2026, 10, 31, hour) for hour in (13, 14, 15)])

    def test_a_rewritten_hour_replaces_the_old_values(self) -> None:
        position = self.line(0, "airnow:060371103")["time"].index(epoch(2026, 10, 31, 13))
        self.assertNotEqual(self.line(0, "airnow:060371103")["obs"]["pm25"][position], 99.9)
        self.assertEqual(self.line(1, "airnow:060371103")["obs"]["pm25"][position], 99.9)

    def test_a_failed_network_is_carried_over(self) -> None:
        first, second = self.indexes
        sources = {source["id"]: source for source in second["sources"]}
        self.assertFalse(sources["epd-24pc"]["ok"])
        self.assertIn("error", sources["epd-24pc"])
        hk = [network for network in second["networks"] if network["id"] == "hkepd"]
        self.assertEqual(len(hk), 1)
        self.assertEqual(
            (self.directories[1] / "hkepd.jsonl").read_bytes(), (self.directories[0] / "hkepd.jsonl").read_bytes()
        )
        self.assertEqual(hk[0]["latest"], "2026-10-31T14:00:00Z")

    def test_unplaced_and_empty_stations_are_left_out(self) -> None:
        airnow_ids = {row[0] for row in self.table(1, "airnow")["stations"]}
        self.assertNotIn("airnow:040217003", airnow_ids)  # at 0°, 0°
        self.assertNotIn("airnow:001101010", airnow_ids)  # listed without data
        self.assertIn("airnow:031AZE010001", airnow_ids)
        hk_ids = {row[0] for row in self.table(0, "hkepd")["stations"]}
        self.assertEqual(hk_ids, {"hkepd:causeway-bay", "hkepd:central-western", "hkepd:tap-mun", "hkepd:tung-chung"})
        sources = {source["id"]: source for source in self.indexes[0]["sources"]}
        self.assertEqual(sources["epd-24pc"]["unplaced"], 1)

    def test_rows_carry_agency_rank_and_elevation(self) -> None:
        baku = self.row(1, "airnow:031AZE010001")
        self.assertIsNone(baku[3])  # AirNow's 0.0
        self.assertEqual(baku[6], "U.S. Department of State Azerbaijan - Baku")
        causeway = self.row(0, "hkepd:causeway-bay")
        self.assertEqual(causeway[4], 2)
        self.assertEqual(causeway[6], "EPD")
        self.assertEqual(causeway[7], "2026-10-31T14:00:00Z")
        self.assertEqual(self.line(0, "hkepd:causeway-bay")["names"], {"zh-Hant": "銅鑼灣"})

    def test_current_values_look_back_an_hour(self) -> None:
        for station in ("airnow:000020104", "airnow:060371103"):
            row = self.row(1, station)
            line = self.line(1, station)
            newest = line["time"][-1]
            self.assertEqual(row[7], datetime.fromtimestamp(newest, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
            for key in ELEMENTS:
                values = line["obs"].get(key, [None] * len(line["time"]))
                recent = [value for time, value in zip(line["time"], values) if time >= newest - 3600 and value is not None]
                self.assertEqual(row[column(key)], recent[-1] if recent else None, (station, key))

    def test_the_pointer_names_the_newest_round(self) -> None:
        pointer = json.loads((self.output / "latest-aq.json").read_bytes())
        self.assertEqual(pointer["path"], f"{self.directories[1].name}/index.json")
        validate_pointer(pointer)


class MergeTest(unittest.TestCase):
    """Rounds built to trip the merge, on a copy of the fixture."""

    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        root = Path(self.scratch.name)
        self.raw = root / "raw"
        self.output = root / "out"
        shutil.copytree(FIXTURES, self.raw, ignore=shutil.ignore_patterns("expected"))
        build_product(FIRST, self.raw, self.output, now=GENERATED[0])

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def _record(self, moment: datetime, *, airnow_ok: bool) -> None:
        """A round in which AirNow answered with no new hour (or failed)
        and EPD failed."""
        directory = self.raw / round_directory(moment)
        directory.mkdir(parents=True, exist_ok=True)
        failed = {"ok": False, "fetched": "2026-10-31T16:00:00Z", "error": "HTTP Error 503"}
        airnow_status = {"id": "airnow-hourly", "ok": True, "file": f"{directory.name}/airnow", "files": 0, "missing": 3}
        sources = [airnow_status if airnow_ok else {"id": "airnow-hourly", **failed}, {"id": "epd-24pc", **failed}]
        (directory / "fetch.json").write_text(json.dumps({"round": directory.name.split(".")[1], "sources": sources}))

    def test_no_network_arriving_withholds_the_pointer_and_keeps_history(self) -> None:
        later = FIRST + timedelta(minutes=30)
        self._record(later, airnow_ok=False)
        (self.output / "latest-aq.json").unlink()
        previous = read_index(self.output / round_directory(FIRST) / "index.json")
        report = build_product(later, self.raw, self.output, previous_index=previous, now=later)
        self.assertIsNone(report["pointer"])
        index = read_index(self.output / round_directory(later) / "index.json")
        self.assertEqual(
            [network["stations"]["count"] for network in index["networks"]],
            [network["stations"]["count"] for network in previous["networks"]],
        )
        self.assertTrue(all(not source["ok"] for source in index["sources"]))

    def test_the_window_drops_what_ended_more_than_a_day_ago(self) -> None:
        later = FIRST + timedelta(hours=22, minutes=45)  # 14:00 tomorrow: the hour ending 14:00 today is still inside
        self._record(later, airnow_ok=True)
        build_product(later, self.raw, self.output, previous_index=load_previous_index(None, self.output), now=later)
        index = read_index(self.output / round_directory(later) / "index.json")
        airnow_entry = next(network for network in index["networks"] if network["id"] == "airnow")
        self.assertEqual(airnow_entry["latest"], "2026-10-31T14:00:00Z")
        line = json.loads((self.output / round_directory(later) / "airnow.jsonl").read_bytes().splitlines()[0])
        self.assertEqual(line["time"], [epoch(2026, 10, 31, 14)])
        later = later + timedelta(hours=1)
        self._record(later, airnow_ok=True)
        report = build_product(later, self.raw, self.output, previous_index=load_previous_index(None, self.output), now=later)
        index = read_index(self.output / round_directory(later) / "index.json")
        self.assertEqual(index["networks"], [])
        self.assertIsNotNone(report["pointer"])  # AirNow answered, with nothing new

    def test_an_existing_round_is_not_rebuilt_without_force(self) -> None:
        with self.assertRaises(AqProductError):
            build_product(FIRST, self.raw, self.output, now=GENERATED[0])


class ValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        directory = EXPECTED / f"aq.{ROUNDS[1]}"
        cls.index = json.loads((directory / "index.json").read_text())
        cls.network = cls.index["networks"][0]
        cls.table = json.loads((directory / cls.network["stations"]["path"]).read_text())
        cls.line = json.loads((directory / cls.network["file"]["path"]).read_bytes().splitlines()[0])

    def test_the_golden_passes(self) -> None:
        validate_index(self.index)
        validate_stations_file(self.table, self.network, self.index["elements"], self.index["issued"])
        validate_station_line(self.line, "line")

    def test_malformed_indexes_are_refused(self) -> None:
        cases = {
            "off-round issue": lambda index: index.__setitem__("issued", "2026-10-31T15:47:00Z"),
            "bad file name": lambda index: index["networks"][0]["file"].__setitem__("path", "history.jsonl"),
            "bad stations name": lambda index: index["networks"][0]["stations"].__setitem__("path", "airnow.json"),
            "no count": lambda index: index["networks"][0]["stations"].pop("count"),
            "preliminary missing": lambda index: index["networks"][0].pop("preliminary"),
            "network twice": lambda index: index["networks"].append(index["networks"][0]),
            "duplicate element": lambda index: index["elements"].append("pm25"),
            "schema version": lambda index: index.__setitem__("schemaVersion", 2),
        }
        for name, change in cases.items():
            index = copy.deepcopy(self.index)
            change(index)
            with self.subTest(name), self.assertRaises(AqProductError):
                validate_index(index)

    def test_malformed_stations_files_are_refused(self) -> None:
        cases = {
            "unsorted": lambda table, network: table["stations"].reverse(),
            "span past the file": lambda table, network: table["stations"][-1].__setitem__(-1, 10**7),
            "overlapping spans": lambda table, network: table["stations"][1].__setitem__(-2, 0),
            "wrong network": lambda table, network: table["stations"][0].__setitem__(0, "hkepd:central"),
            "short row": lambda table, network: table["stations"][0].pop(),
            "pm25 out of range": lambda table, network: table["stations"][0].__setitem__(column("pm25"), 20000.0),
            "elements differ": lambda table, network: table["elements"].reverse(),
            "count differs": lambda table, network: network["stations"].__setitem__("count", 1),
            "agency not a string": lambda table, network: table["stations"][0].__setitem__(6, 3),
        }
        for name, change in cases.items():
            table, network = copy.deepcopy(self.table), copy.deepcopy(self.network)
            change(table, network)
            with self.subTest(name), self.assertRaises(AqProductError):
                validate_stations_file(table, network, self.index["elements"], self.index["issued"])

    def test_unknown_elements_are_admitted(self) -> None:
        table = copy.deepcopy(self.table)
        elements = [*self.index["elements"], "nh3"]
        table["elements"] = elements
        for row in table["stations"]:
            row.insert(len(ROW_HEAD) + len(ELEMENTS), 1.5)
        validate_stations_file(table, self.network, elements, self.index["issued"])

    def test_malformed_lines_are_refused(self) -> None:
        cases = {
            "time not increasing": lambda line: line["time"].reverse(),
            "column too short": lambda line: line["obs"][next(iter(line["obs"]))].pop(),
            "bad id": lambda line: line.__setitem__("id", "060371103"),
            "rank": lambda line: line.__setitem__("rank", 3),
        }
        for name, change in cases.items():
            line = copy.deepcopy(self.line)
            change(line)
            with self.subTest(name), self.assertRaises(AqProductError):
                validate_station_line(line, "line")

    def test_pointer(self) -> None:
        pointer = build_pointer(SECOND, f"{round_directory(SECOND)}/index.json", encode_json(self.index))
        validate_pointer(pointer)
        with self.assertRaises(AqProductError):
            validate_pointer(pointer | {"path": f"aq.{ROUNDS[0]}/index.json"})

    def test_rounds(self) -> None:
        self.assertEqual(floor_round(datetime(2026, 10, 31, 15, 49, 59, tzinfo=UTC)), parse_round("202610311545"))
        for value in ("202610311547", "2026103115", "202613310015"):
            with self.subTest(value), self.assertRaises(AqProductError):
                parse_round(value)


if __name__ == "__main__":
    unittest.main()

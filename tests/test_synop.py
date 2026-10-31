"""The synop product (``xuebuild/synop``): the AMeDAS, DWD and HKO
adapters' readers on the fetched fixture and on constructed input, the
merge, the stations files' byte spans and current values, the validators on
malformed input, the round after the switch from schema v1, and two
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
from xuebuild.synop import amedas, dwd, hko
from xuebuild.synop.build import build_product, load_previous_index
from xuebuild.synop.schema import (
    ELEMENTS,
    ROW_HEAD,
    ROW_HEAD_V1,
    build_pointer,
    encode_json,
    floor_round,
    parse_round,
    read_index,
    read_stations_file,
    round_directory,
    validate_index,
    validate_pointer,
    validate_station_line,
    validate_stations_file,
)

EXPECTED = FIXTURES / "expected"
V1 = FIXTURES / "v1"
TABLE = json.loads((FIXTURES / "amedas-stations.json").read_text(encoding="utf-8"))
HAS_BUFR_DUMP = shutil.which("bufr_dump") is not None
NETWORK_IDS = ("amedas", "dwd", "hko")


def column(key: str) -> int:
    return len(ROW_HEAD) + ELEMENTS.index(key)


def stations_of(directory: Path, index: dict) -> dict[str, list]:
    """Every station row of a v2 round, by id."""
    rows: dict[str, list] = {}
    for network in index["networks"]:
        for row in read_stations_file(directory, index, network["id"])["stations"]:
            rows[row[0]] = row
    return rows


def line_of(directory: Path, row: list) -> dict:
    network = row[0].split(":")[0]
    body = (directory / f"{network}.jsonl").read_bytes()
    return json.loads(body[row[-2] : row[-2] + row[-1]])


def walked(*entries: tuple) -> list[tuple]:
    """A DWD subset as ``(key, value, units)``, behind a station header."""
    header = [
        ("shortStationName", None, "CCITT IA5"),
        ("blockNumber", 10, "Numeric"),
        ("stationNumber", 393, "Numeric"),
        ("stationOrSiteName", "Lindenberg", "CCITT IA5"),
        ("year", 2026, "a"),
        ("month", 10, "mon"),
        ("day", 5, "d"),
        ("hour", 0, "h"),
        ("minute", 0, "min"),
        ("latitude", 52.2085, "deg"),
        ("longitude", 14.118, "deg"),
        ("heightOfStationGroundAboveMeanSeaLevel", 97.7, "m"),
    ]
    return header + [entry if len(entry) == 3 else (*entry, None) for entry in entries]


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


class DwdTest(unittest.TestCase):
    LISTING = (
        '<a href="../">../</a>\n'
        '<a href="json/">json/</a>\n'
        '<a href="Z__C_EDZW_20261031140500_bda01%2Csynop_bufr_GER_999999_999999__MW_847.bin">x</a>\n'
        '<a href="Z__C_EDZW_20261031140000_bda01%2Csynop_bufr_GER_999999_999999__MW_846.bin">x</a>\n'
        '<a href="Z__C_EDZW_20261031142400_bda01%2Csynop_bufr_GER_999999_999999__MW_849.bin">x</a>\n'
        '<a href="Z__C_EDZW_latest_bda01%2Csynop_bufr_GER_999999_999999__MW_XXX.bin">x</a>\n'
    )

    def test_the_listing_is_the_stamped_batches_oldest_first(self) -> None:
        batches = dwd.parse_listing(self.LISTING)
        self.assertEqual([stamp.strftime("%H%M") for stamp, _ in batches], ["1400", "1405", "1424"])
        self.assertEqual(batches[0][1], "Z__C_EDZW_20261031140000_bda01,synop_bufr_GER_999999_999999__MW_846.bin")

    def test_a_round_fetches_the_batches_after_the_networks_latest(self) -> None:
        batches = dwd.parse_listing(self.LISTING)
        moment = datetime(2026, 10, 31, 14, 30, tzinfo=UTC)
        since = datetime(2026, 10, 31, 14, 0, tzinfo=UTC)
        names = [name for _, name in dwd.batches_to_fetch(batches, moment, since)]
        self.assertEqual(len(names), 2)  # 14:05 and 14:24; 14:00 was read by the round that published 14:00
        self.assertEqual(len(dwd.batches_to_fetch(batches, datetime(2026, 10, 31, 14, 10, tzinfo=UTC), since)), 1)
        self.assertEqual(len(dwd.batches_to_fetch(batches, moment, None)), 3)
        self.assertEqual(len(dwd.batches_to_fetch(batches, moment + timedelta(days=2), None)), 0)

    def test_values_are_taken_by_key_and_the_period_or_height_before_them(self) -> None:
        subset = dwd.parse_subset(
            walked(
                ("heightOfSensorAboveLocalGroundOrDeckOfMarinePlatform", 2, "m"),
                ("airTemperature", 282.85, "K"),
                ("heightOfSensorAboveLocalGroundOrDeckOfMarinePlatform", 0.05, "m"),
                ("airTemperature", 282.25, "K"),  # the grass minimum
                ("heightOfSensorAboveLocalGroundOrDeckOfMarinePlatform", 2, "m"),
                ("relativeHumidity", 84.5, "%"),
                ("dewpointTemperature", 280.48, "K"),
                ("timePeriod", -10, "min"),
                ("totalPrecipitationOrTotalWaterEquivalent", 0.3, "kg m-2"),
                ("timePeriod", -1, "min"),
                ("totalPrecipitationOrTotalWaterEquivalent", 0.05, "kg m-2"),
                ("timePeriod", 0, "min"),
                ("nonCoordinatePressure", 101700, "Pa"),
                ("pressureReducedToMeanSeaLevel", 102980, "Pa"),
                ("horizontalVisibility", 30000, "m"),
                ("meteorologicalOpticalRange", 38761, "m"),
                ("totalSnowDepth", -0.01, "m"),
                ("timePeriod", -10, "min"),
                ("windSpeed", 2.2, "m/s"),
                ("windDirection", 350, "deg"),
                ("timePeriod", -60, "min"),
                ("maximumWindGustSpeed", 5.4, "m/s"),
                ("timePeriod", -10, "min"),
                ("maximumWindGustSpeed", 4.1, "m/s"),
                ("sunshineOverPeriodSpecified", 7, "min"),
                ("timePeriod", -1, "h"),
                ("totalPrecipitationOrTotalWaterEquivalent", 1.2, "kg m-2"),
                ("totalSunshine", 40, "min"),
            )
        )
        assert subset is not None
        self.assertEqual(subset.station, "10393")
        self.assertEqual(subset.wmo, "10393")
        self.assertEqual(subset.epoch, int(datetime(2026, 10, 5, tzinfo=UTC).timestamp()))
        elements = subset.elements
        self.assertEqual(elements["t"], 9.7)
        self.assertEqual(elements["td"], 7.3)
        self.assertEqual(elements["rh"], 85)  # half up
        self.assertEqual(elements["pr"], 0.3)
        self.assertEqual(elements["pr1h"], 1.2)
        self.assertEqual(elements["p"], 1017.0)
        self.assertEqual(elements["slp"], 1029.8)
        self.assertEqual(elements["vis"], 38761)  # the ten-minute MOR over the observer's
        self.assertNotIn("snow", elements)  # −0.01 m is "a little", not a depth
        self.assertEqual((elements["ws"], elements["wd"]), (2.2, 350))
        self.assertEqual(elements["gust"], 4.1)  # the shorter period
        self.assertEqual(elements["sun"], 7.0)

    def test_calm_has_no_direction_and_snow_is_centimetres(self) -> None:
        subset = dwd.parse_subset(
            walked(
                ("timePeriod", -10, "min"),
                ("windSpeed", 0, "m/s"),
                ("windDirection", 0, "deg"),
                ("timePeriod", 0, "min"),
                ("totalSnowDepth", 0.23, "m"),
            )
        )
        assert subset is not None
        self.assertEqual(subset.elements["ws"], 0.0)
        self.assertNotIn("wd", subset.elements)
        self.assertEqual(subset.elements["snow"], 23)

    def test_identity_is_dwds_station_identifier(self) -> None:
        national = [("shortStationName", "M031", None), *walked()[1:]]
        national[1] = ("blockNumber", None, None)
        subset = dwd.parse_subset(national)
        assert subset is not None
        self.assertEqual((subset.station, subset.wmo), ("M031", None))
        nil = [(key, None, units) for key, _value, units in walked()]
        self.assertIsNone(dwd.parse_subset(nil))

    @unittest.skipUnless(HAS_BUFR_DUMP, "bufr_dump (eccodes) is not installed")
    def test_the_recorded_batches(self) -> None:
        paths = sorted((FIXTURES / f"synop.{ROUNDS[0]}" / "dwd").glob("*.bin"))
        stations, unplaced = dwd.read_batches(paths)
        self.assertEqual(unplaced, 1)  # 10004's NIL report
        self.assertEqual(sorted(stations), ["10271", "10393", "10961", "A116", "M031", "O598"])
        hour = int(datetime(2026, 10, 4, 23, 40, tzinfo=UTC).timestamp())
        lindenberg = stations["10393"].observations[hour].elements
        # The hourly national template's one-hour total, the SYNOP
        # template's temperature, both at the same time.
        self.assertEqual(lindenberg["pr1h"], 0.2)
        self.assertEqual(lindenberg["t"], 9.7)
        self.assertEqual(lindenberg["gust"], 3.9)  # −10 min, not the −60 min 5.4
        self.assertEqual(dwd._rank(stations["M031"]), 2)
        self.assertEqual(dwd._rank(stations["A116"]), 1)
        self.assertEqual(dwd._rank(stations["10271"]), 0)


class HkoTest(unittest.TestCase):
    def test_names_resolve_per_file(self) -> None:
        self.assertEqual(hko.resolve("temperature", "Chek Lap Kok"), "HKA")
        self.assertEqual(hko.resolve("temperature", "Tsing Yi"), "TY1")
        self.assertEqual(hko.resolve("wind", "Tsing Yi"), "SHL")
        self.assertEqual(hko.resolve("wind", "Tap Mun"), "TME")
        self.assertEqual(hko.resolve("temperature", "Kai Tak Runway Park"), "SE1")
        self.assertEqual(hko.resolve("wind", "Waglan Island"), "WGL")
        self.assertIsNone(hko.resolve("wind", "Hong Kong Sea School"))
        self.assertIsNone(hko.resolve("visibility", "Sai Wan Ho"))

    def test_the_table(self) -> None:
        table = hko.station_table()
        self.assertEqual(len(table["byName"]), len(table["stations"]))
        for code, entry in table["stations"].items():
            self.assertTrue(entry["names"]["zh-Hant"], code)
            self.assertTrue(22.0 < entry["lat"] < 22.7 and 113.8 < entry["lon"] < 114.5, code)
        for kind in table["fileAliases"]:
            self.assertIn(kind, hko.FILES)
        self.assertEqual(table["stations"]["HKO"]["rank"], 0)
        self.assertEqual(table["stations"]["WGL"]["elev"], 56.0)

    def test_wind(self) -> None:
        self.assertEqual(hko.wind("Southeast", "22", "34"), {"wd": 135, "ws": 6.1, "gust": 9.4})
        self.assertEqual(hko.wind("North", "36", "40")["wd"], 360)
        self.assertEqual(hko.wind("Calm", "Calm", "5"), {"wd": None, "ws": 0.0, "gust": 1.4})
        self.assertEqual(hko.wind("N/A", "N/A", "N/A"), {"wd": None, "ws": None, "gust": None})
        self.assertIsNone(hko.wind("Variable", "4", "9")["wd"])

    def test_readings(self) -> None:
        self.assertEqual(hko.visibility("18 km"), 18000)
        self.assertEqual(hko.visibility("800 m"), 800)
        self.assertIsNone(hko.visibility("N/A"))
        self.assertEqual(hko.parse_time("202610050810"), int(datetime(2026, 10, 5, 0, 10, tzinfo=UTC).timestamp()))
        rows = hko.parse_csv("wind", "Date time,Station,d,s,g\n202610050800,Wetland Park,Calm,Calm,5,\n")
        self.assertEqual(rows[0][0], "Wetland Park")
        self.assertEqual(rows[0][2]["ws"], 0.0)


@unittest.skipUnless(HAS_BUFR_DUMP, "bufr_dump (eccodes) is not installed: the DWD batches do not decode")
class RoundsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory()
        cls.output = Path(cls.scratch.name)
        cls.reports = build_rounds(cls.output)
        cls.directories = [cls.output / round_directory(parse_round(name)) for name in ROUNDS]
        cls.indexes = [read_index(directory / "index.json") for directory in cls.directories]
        cls.rows = [stations_of(directory, index) for directory, index in zip(cls.directories, cls.indexes)]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scratch.cleanup()

    def test_golden(self) -> None:
        for directory, index in zip(self.directories, self.indexes):
            expected = EXPECTED / directory.name
            self.assertEqual(pretty(json.loads((directory / "index.json").read_bytes())) + "\n", (expected / "index.json").read_text())
            self.assertEqual([network["id"] for network in index["networks"]], list(NETWORK_IDS))
            for network in index["networks"]:
                for name in (network["file"]["path"], network["stations"]["path"]):
                    if name.endswith(".jsonl"):
                        self.assertEqual((directory / name).read_bytes(), (expected / name).read_bytes(), name)
                    else:
                        self.assertEqual(pretty(json.loads((directory / name).read_bytes())) + "\n", (expected / name).read_text(), name)
            self.assertEqual(pretty(json.loads((directory / "item.json").read_bytes())) + "\n", (expected / "item.json").read_text())
        for relative in ("synop/collection.json", "synop/item.json", "latest-synop.json"):
            self.assertEqual(pretty(json.loads((self.output / relative).read_bytes())) + "\n", (EXPECTED / relative).read_text(), relative)

    def test_every_span_slices_out_its_own_station(self) -> None:
        for directory, rows in zip(self.directories, self.rows):
            for station, row in rows.items():
                line = line_of(directory, row)
                self.assertEqual(line["id"], station)
                validate_station_line(line, station)

    def test_the_index_carries_no_stations(self) -> None:
        for index in self.indexes:
            self.assertNotIn("stations", index)
            for network in index["networks"]:
                self.assertEqual(network["stations"]["path"], f"{network['id']}.stations.json")

    def test_the_second_round_merges_onto_the_first(self) -> None:
        first, second = self.indexes
        tokyo = line_of(self.directories[1], self.rows[1]["amedas:44132"])
        self.assertEqual(len(tokyo["time"]), 3)
        self.assertEqual(tokyo["time"], sorted(tokyo["time"]))
        self.assertEqual(second["networks"][0]["latest"], "2026-10-05T00:10:00Z")
        self.assertEqual(first["networks"][0]["latest"], "2026-10-05T00:00:00Z")

    def test_a_station_the_table_does_not_place_is_dropped(self) -> None:
        ids = set(self.rows[1])
        self.assertNotIn("amedas:12066", ids)
        self.assertIn("amedas:12217", ids)
        self.assertNotIn("dwd:10004", ids)
        hko_source = next(source for source in self.indexes[1]["sources"] if source["id"] == hko.SOURCE)
        self.assertEqual(hko_source["unplaced"], 3)  # Hong Kong Sea School, Central, Sai Wan Ho

    def test_slow_elements_carry_into_the_stations_file_within_the_hour(self) -> None:
        fuji = self.rows[1]["amedas:50066"]
        self.assertEqual(fuji[6], "2026-10-05T00:10:00Z")
        self.assertIsNotNone(fuji[column("rh")])  # only on the hour
        self.assertIsNotNone(fuji[column("p")])

    def test_accumulations_are_never_borrowed_from_an_older_period(self) -> None:
        otani = self.rows[1]["amedas:31386"]
        self.assertIsNone(otani[column("pr")])  # quality 4 at the newest time
        self.assertEqual(otani[column("pr1h")], 0.0)  # quality 1 is kept

    def test_dwd_pieces_of_one_time_are_laid_over_each_other(self) -> None:
        line = line_of(self.directories[1], self.rows[1]["dwd:O598"])
        hour = line["time"].index(int(datetime(2026, 10, 4, 23, 40, tzinfo=UTC).timestamp()))
        # The hourly batch (first round) gave the one-hour total, the
        # ten-minute batch (second round) the ten-minute values of the
        # same time; neither erased the other.
        self.assertEqual(line["obs"]["pr1h"][hour], 0.4)
        self.assertEqual(line["obs"]["pr"][hour], 0.0)
        self.assertEqual(self.rows[1]["dwd:M031"][4], 2)  # precipitation only
        self.assertEqual(self.rows[1]["dwd:10393"][4], 0)  # a WMO station

    def test_dew_point_is_published_where_reported(self) -> None:
        self.assertEqual(self.rows[1]["dwd:10393"][column("td")], 7.8)
        self.assertIsNone(self.rows[1]["amedas:44132"][column("td")])
        self.assertIsNone(self.rows[1]["hko:HKO"][column("td")])

    def test_an_hko_station_missing_from_a_round_keeps_its_history(self) -> None:
        waglan = self.rows[1]["hko:WGL"]
        self.assertEqual(waglan[6], "2026-10-05T00:00:00Z")
        self.assertEqual(waglan[column("vis")], 26000)
        wetland = line_of(self.directories[0], self.rows[0]["hko:WLP"])
        self.assertNotIn("wd", wetland["obs"])  # Calm: no direction at any time
        self.assertEqual(wetland["obs"]["ws"], [0.0])

    def test_the_pointer_names_the_newest_round(self) -> None:
        pointer = json.loads((self.output / "latest-synop.json").read_bytes())
        self.assertEqual(pointer["schemaVersion"], 1)
        self.assertEqual(pointer["path"], f"{self.directories[1].name}/index.json")
        validate_pointer(pointer)


class SwitchTest(unittest.TestCase):
    """The first v2 round, built onto a live round of schema v1."""

    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        self.output = Path(self.scratch.name)
        shutil.copytree(V1 / f"synop.{ROUNDS[0]}", self.output / f"synop.{ROUNDS[0]}")

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def test_a_v1_index_is_read(self) -> None:
        previous = read_index(V1 / f"synop.{ROUNDS[0]}" / "index.json")
        self.assertEqual(previous["schemaVersion"], 1)
        self.assertEqual(len(previous["stations"][0]), len(ROW_HEAD_V1) + len(previous["elements"]) + 2)

    def test_the_next_round_merges_onto_the_v1_round(self) -> None:
        previous = read_index(self.output / f"synop.{ROUNDS[0]}" / "index.json")
        moment = parse_round(ROUNDS[1])
        build_product(moment, FIXTURES, self.output, previous_index=previous, now=GENERATED[1])
        directory = self.output / round_directory(moment)
        index = read_index(directory / "index.json")
        self.assertEqual(index["schemaVersion"], 2)
        rows = stations_of(directory, index)
        # The AMeDAS history came out of the v1 round's file: the network
        # file is what the golden's second round, built on a v2 first
        # round, holds byte for byte.
        self.assertEqual((directory / "amedas.jsonl").read_bytes(), (EXPECTED / directory.name / "amedas.jsonl").read_bytes())
        self.assertEqual(len(line_of(directory, rows["amedas:44132"])["time"]), 3)
        # The networks the v1 round did not carry start their window here.
        self.assertTrue(all(len(line_of(directory, row)["time"]) == 1 for station, row in rows.items() if station.startswith("hko:")))


@unittest.skipUnless(HAS_BUFR_DUMP, "bufr_dump (eccodes) is not installed: the DWD batches do not decode")
class MergeTest(unittest.TestCase):
    """Rounds built to trip the merge, on a copy of the fixture."""

    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory()
        root = Path(self.scratch.name)
        self.raw = root / "raw"
        self.output = root / "out"
        shutil.copytree(FIXTURES, self.raw, ignore=shutil.ignore_patterns("expected", "v1"))
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

    def _count(self, moment: datetime) -> int:
        index = read_index(self.output / round_directory(moment) / "index.json")
        return sum(network["stations"]["count"] for network in index["networks"])

    def test_a_failed_network_keeps_its_history_and_withholds_the_pointer(self) -> None:
        later = self.first + timedelta(minutes=30)
        self._record(later, map_ok=False)
        (self.output / "latest-synop.json").unlink()
        previous = read_index(self.output / round_directory(self.first) / "index.json")
        report = build_product(later, self.raw, self.output, previous_index=previous, now=later)
        self.assertIsNone(report["pointer"])
        self.assertEqual(self._count(later), self._count(self.first))
        index = read_index(self.output / round_directory(later) / "index.json")
        self.assertFalse(index["sources"][0]["ok"])
        self.assertTrue(all(source["error"] == "not fetched" for source in index["sources"][2:]))

    def test_the_window_drops_what_is_older_than_a_day(self) -> None:
        later = self.first + timedelta(hours=24)
        self._record(later, map_ok=True)
        previous = load_previous_index(None, self.output)
        report = build_product(later, self.raw, self.output, previous_index=previous, now=later)
        self.assertIsNotNone(report["pointer"])  # AMeDAS answered, with nothing new
        directory = self.output / round_directory(later)
        index = read_index(directory / "index.json")
        rows = stations_of(directory, index)
        # 23:50 fell out, and DWD's 23:40 with it; the 00:00 snapshots are
        # exactly 24 hours old and stay.
        self.assertTrue(rows)
        self.assertEqual([network["id"] for network in index["networks"]], ["amedas", "hko"])
        self.assertTrue(all(row[6] == "2026-10-05T00:00:00Z" for row in rows.values()))
        later = later + timedelta(minutes=10)
        self._record(later, map_ok=True)
        build_product(later, self.raw, self.output, previous_index=load_previous_index(None, self.output), now=later)
        index = read_index(self.output / round_directory(later) / "index.json")
        self.assertEqual(index["networks"], [])

    def test_a_revised_value_replaces_the_one_it_revises(self) -> None:
        second = parse_round(ROUNDS[1])
        snapshot = self.raw / round_directory(second) / "amedas" / "map" / "20261005090000.json"
        cells = json.loads((self.raw / round_directory(self.first) / "amedas" / "map" / "20261005090000.json").read_text())
        cells["44132"]["temp"] = [21.5, 0]
        snapshot.write_text(json.dumps(cells))
        build_product(second, self.raw, self.output, previous_index=load_previous_index(None, self.output), now=GENERATED[1])
        directory = self.output / round_directory(second)
        rows = stations_of(directory, read_index(directory / "index.json"))
        line = line_of(directory, rows["amedas:44132"])
        position = line["time"].index(int(datetime(2026, 10, 5, 0, 0, tzinfo=UTC).timestamp()))
        self.assertEqual(line["obs"]["t"][position], 21.5)

    def test_an_existing_round_is_not_rebuilt_without_force(self) -> None:
        with self.assertRaises(SynopProductError):
            build_product(self.first, self.raw, self.output, now=GENERATED[0])


class ValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        directory = EXPECTED / f"synop.{ROUNDS[1]}"
        cls.index = json.loads((directory / "index.json").read_text())
        cls.stations = json.loads((directory / "amedas.stations.json").read_text())
        cls.line = json.loads((directory / "amedas.jsonl").read_bytes().splitlines()[0])
        cls.v1 = json.loads((V1 / f"synop.{ROUNDS[0]}" / "index.json").read_text())

    def broken(self, payload: dict, change) -> dict:
        payload = copy.deepcopy(payload)
        change(payload)
        return payload

    def test_the_golden_passes(self) -> None:
        validate_index(self.index)
        validate_stations_file(self.stations, self.index, "amedas")
        validate_station_line(self.line, "line")
        validate_index(self.v1)

    def test_the_golden_stations_files_are_the_bytes_the_index_names(self) -> None:
        directory = EXPECTED / f"synop.{ROUNDS[1]}"
        for network in self.index["networks"]:
            payload = json.loads((directory / network["stations"]["path"]).read_text())
            self.assertEqual(len(encode_json(payload)), network["stations"]["byteLength"])
            validate_stations_file(payload, self.index, network["id"])

    def test_malformed_indexes_are_refused(self) -> None:
        cases = {
            "off-round issue": lambda index: index.__setitem__("issued", "2026-10-05T00:15:00Z"),
            "bad file name": lambda index: index["networks"][0]["file"].__setitem__("path", "history.jsonl"),
            "bad stations file name": lambda index: index["networks"][0]["stations"].__setitem__("path", "amedas.json"),
            "no stations file": lambda index: index["networks"][0].pop("stations"),
            "negative count": lambda index: index["networks"][0]["stations"].__setitem__("count", -1),
            "network twice": lambda index: index["networks"].append(index["networks"][0]),
            "stations in the index": lambda index: index.__setitem__("stations", []),
            "schema version": lambda index: index.__setitem__("schemaVersion", 3),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(SynopProductError):
                validate_index(self.broken(self.index, change))

    def test_malformed_stations_files_are_refused(self) -> None:
        cases = {
            "unsorted": lambda payload: payload["stations"].reverse(),
            "span past the file": lambda payload: payload["stations"][-1].__setitem__(-1, 10**7),
            "overlapping spans": lambda payload: payload["stations"][1].__setitem__(-2, 0),
            "wrong network": lambda payload: payload["stations"][0].__setitem__(0, "dwd:10393"),
            "short row": lambda payload: payload["stations"][0].pop(),
            "temperature out of range": lambda payload: payload["stations"][0].__setitem__(column("t"), 99.0),
            "dew point out of range": lambda payload: payload["stations"][0].__setitem__(column("td"), 61.0),
            "fractional humidity": lambda payload: payload["stations"][0].__setitem__(column("rh"), 63.5),
            "a row fewer than counted": lambda payload: payload["stations"].pop(),
            "another round": lambda payload: payload.__setitem__("issued", "2026-10-05T00:00:00Z"),
            "other elements": lambda payload: payload["elements"].reverse(),
            "another network": lambda payload: payload.__setitem__("network", "dwd"),
            "schema version": lambda payload: payload.__setitem__("schemaVersion", 1),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(SynopProductError):
                validate_stations_file(self.broken(self.stations, change), self.index, "amedas")

    def test_malformed_v1_indexes_are_still_refused(self) -> None:
        cases = {
            "unsorted": lambda index: index["stations"].reverse(),
            "wrong network": lambda index: index["stations"][0].__setitem__(0, "gts:47401"),
            "short row": lambda index: index["stations"][0].pop(),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(SynopProductError):
                validate_index(self.broken(self.v1, change))

    def test_unknown_elements_and_networks_are_admitted(self) -> None:
        def widen(payload: dict) -> None:
            payload["elements"].append("dewpoint2")
            for row in payload.get("stations", []):
                row.insert(len(ROW_HEAD) + len(ELEMENTS), 1.5)

        index = self.broken(self.index, widen)
        validate_index(index)
        validate_stations_file(self.broken(self.stations, widen), index, "amedas")

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

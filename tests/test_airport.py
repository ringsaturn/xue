"""The airport product (``xuebuild/airport``): the two readers on the
fetched fixture and on constructed edge cases, the sharding and merge
rules on rounds built to trip them, the validators on malformed input, and
two consecutive rounds held to a committed golden.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from xuebuild.airport.build import build_product, load_previous_index
from xuebuild.airport.fetch import FETCH_FILENAME, SOURCE_IDS
from xuebuild.airport.metar import parse_metars
from xuebuild.airport.schema import (
    HISTORY_HOURS,
    SHARD_DIRECTORY,
    build_pointer,
    crc32_hex,
    encode_json,
    floor_round,
    parse_round,
    read_index,
    round_directory,
    shard_key,
    shard_path,
    validate_index,
    validate_pointer,
    validate_shard,
)
from xuebuild.airport.stations import parse_stations
from xuebuild.airport.taf import parse_tafs
from xuebuild.airport.units import cloud_base, visibility, wind_speed
from xuebuild.errors import AirportProductError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "airport"
EXPECTED = FIXTURES / "expected"
ROUNDS = ("202609161430", "202609161440")
GENERATED = (datetime(2026, 9, 16, 14, 36, 20, tzinfo=UTC), datetime(2026, 9, 16, 14, 46, 20, tzinfo=UTC))

COLUMNS = (
    "raw_text",
    "station_id",
    "observation_time",
    "latitude",
    "longitude",
    "temp_c",
    "dewpoint_c",
    "wind_dir_degrees",
    "wind_speed_kt",
    "wind_gust_kt",
    "visibility_statute_mi",
    "altim_in_hg",
    "sea_level_pressure_mb",
    "auto",
    "wx_string",
    "sky_cover",
    "cloud_base_ft_agl",
    "sky_cover",
    "cloud_base_ft_agl",
    "sky_cover",
    "cloud_base_ft_agl",
    "sky_cover",
    "cloud_base_ft_agl",
    "flight_category",
    "metar_type",
    "elevation_m",
)
HEADER = ",".join(COLUMNS)
PREAMBLE = ("No errors", "No warnings", "12 ms", "data source=metars", "2 results")

EMPTY_TAFS = (
    '<?xml version="1.0" encoding="UTF-8"?><response version="2.0"><errors/><warnings/>'
    '<data num_results="0"></data></response>'
)


def csv_document(*rows: str, preamble: tuple[str, ...] = ()) -> str:
    return "\n".join([*preamble, HEADER, *rows]) + "\n"


def metar_row(icao: str, time: str, *, clouds: tuple[tuple[str, str], ...] = (), **values: object) -> str:
    """One CSV row, by column name. The defaults are a plain report; a
    column set to ``""`` is one the AWC left empty."""
    row = {
        "raw_text": f'"METAR {icao} {time[8:10]}{time[11:13]}{time[14:16]}Z"',
        "station_id": icao,
        "observation_time": time,
        "latitude": "35.553",
        "longitude": "139.781",
        "temp_c": "21",
        "dewpoint_c": "20",
        "wind_dir_degrees": "20",
        "wind_speed_kt": "11",
        "visibility_statute_mi": "6+",
        "altim_in_hg": "30.03",
        "flight_category": "VFR",
        "metar_type": "METAR",
        "elevation_m": "5",
    }
    row.update({key: "" if value is None else str(value) for key, value in values.items()})
    cells = []
    layer = 0
    for column in COLUMNS:
        if column == "sky_cover":
            cells.append(clouds[layer][0] if layer < len(clouds) else "")
        elif column == "cloud_base_ft_agl":
            cells.append(clouds[layer][1] if layer < len(clouds) else "")
            layer += 1
        else:
            cells.append(str(row.get(column, "")))
    return ",".join(cells)


def write_round(raw_root: Path, name: str, metars: str, tafs: str = EMPTY_TAFS, *, ok: tuple[str, ...] = SOURCE_IDS) -> None:
    """A fetched round directory as :mod:`xuebuild.airport.fetch` leaves
    it, without the network."""
    directory = raw_root / f"airport.{name}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metars.cache.csv").write_text(metars, encoding="utf-8")
    (directory / "tafs.cache.xml").write_text(tafs, encoding="utf-8")
    sources = []
    for source_id, filename in (
        ("awc-metars", f"airport.{name}/metars.cache.csv"),
        ("awc-tafs", f"airport.{name}/tafs.cache.xml"),
        ("awc-stations", "airport-stations.json"),
    ):
        if source_id in ok:
            sources.append({"id": source_id, "ok": True, "fetched": "2026-09-16T14:35:58Z", "file": filename})
        else:
            sources.append({"id": source_id, "ok": False, "fetched": "2026-09-16T14:35:58Z", "error": "HTTP 503"})
    if not (raw_root / "airport-stations.json").exists():
        (raw_root / "airport-stations.json").write_text("[]", encoding="utf-8")
    (directory / FETCH_FILENAME).write_text(json.dumps({"round": name, "sources": sources}, indent=2), encoding="utf-8")


class MetarTests(unittest.TestCase):
    def test_reads_the_fixture(self) -> None:
        reports = parse_metars((FIXTURES / f"airport.{ROUNDS[0]}" / "metars.cache.csv").read_text())
        self.assertGreater(len(reports), 200)
        haneda = max((report for report in reports if report.icao == "RJTT"), key=lambda report: report.time)
        self.assertEqual(haneda.time, datetime(2026, 9, 16, 14, 0, tzinfo=UTC))
        self.assertEqual(haneda.type, "METAR")
        self.assertFalse(haneda.auto)
        self.assertEqual(haneda.vis, 7000)
        self.assertIn("RJTT", haneda.raw)

    def test_skips_the_api_preamble(self) -> None:
        document = csv_document(metar_row("RJTT", "2026-09-16T14:30:00.000Z"), preamble=PREAMBLE)
        reports = parse_metars(document)
        self.assertEqual([report.icao for report in reports], ["RJTT"])
        with self.assertRaises(ValueError):
            parse_metars("No errors\nNo warnings\n")

    def test_units_are_si_at_the_parser(self) -> None:
        row = metar_row(
            "RJTT",
            "2026-09-16T14:30:00.000Z",
            temp_c="21.1",
            dewpoint_c="20.4",
            wind_dir_degrees="110",
            wind_speed_kt="11",
            wind_gust_kt="17",
            visibility_statute_mi="7",
            altim_in_hg="30.27",
            sea_level_pressure_mb="1013.25",
        )
        report = parse_metars(csv_document(row))[0]
        self.assertEqual(report.t, 21.1)
        self.assertEqual(report.td, 20.4)
        self.assertEqual(report.wd, 110)
        self.assertEqual(report.ws, 5.7)  # 11 kt × 0.514444 = 5.658884
        self.assertEqual(report.gust, 8.7)  # 17 kt × 0.514444 = 8.745548
        self.assertEqual(report.vis, 11300)  # 7 statute miles = 11265.4 m
        self.assertEqual(report.qnh, 1025.1)  # 30.27 inHg × 33.8639 = 1025.06
        self.assertEqual(report.slp, 1013.2)  # half to even, as round() does

    def test_visibility_conventions(self) -> None:
        self.assertEqual(visibility("6+"), 10000)
        self.assertEqual(visibility("10+"), 10000)
        self.assertEqual(visibility(">6"), 10000)
        self.assertEqual(visibility("P6SM"), 10000)
        self.assertEqual(visibility("0.25"), 400)
        self.assertEqual(visibility("1.86"), 3000)
        self.assertIsNone(visibility(""))
        self.assertIsNone(visibility(None))
        self.assertIsNone(visibility("clear"))

    def test_cloud_layers_merge_the_four_column_pairs(self) -> None:
        row = metar_row(
            "RJTT",
            "2026-09-16T14:30:00.000Z",
            clouds=(("FEW", "400"), ("SCT", "4300"), ("BKN", "9000"), ("OVC", "")),
        )
        report = parse_metars(csv_document(row))[0]
        self.assertEqual(report.cloud, [["FEW", 120], ["SCT", 1310], ["BKN", 2740], ["OVC", None]])
        self.assertEqual(cloud_base(1000), 300)
        self.assertEqual(wind_speed(None), None)
        bare = parse_metars(csv_document(metar_row("RJTT", "2026-09-16T14:30:00.000Z")))[0]
        self.assertEqual(bare.cloud, [])

    def test_absent_values_are_null(self) -> None:
        row = metar_row(
            "KXSA",
            "2026-09-16T14:35:00.000Z",
            temp_c="",
            dewpoint_c="",
            wind_dir_degrees="",
            wind_speed_kt="",
            visibility_statute_mi="",
            altim_in_hg="",
            flight_category="null",
            metar_type="SPECI",
            auto="TRUE",
        )
        report = parse_metars(csv_document(row))[0]
        self.assertEqual(
            (report.t, report.td, report.wd, report.ws, report.gust, report.vis, report.qnh, report.category),
            (None, None, None, None, None, None, None, None),
        )
        self.assertEqual(report.type, "SPECI")
        self.assertTrue(report.auto)

    def test_a_row_without_a_position_or_a_time_is_skipped(self) -> None:
        rows = (
            metar_row("RJTT", "2026-09-16T14:30:00.000Z", latitude=""),
            metar_row("RJAA", "not a time"),
            metar_row("RJBB", "2026-09-16T14:30:00.000Z"),
        )
        self.assertEqual([report.icao for report in parse_metars(csv_document(*rows))], ["RJBB"])


class TafTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tafs = {taf.icao: taf for taf in parse_tafs((FIXTURES / f"airport.{ROUNDS[0]}" / "tafs.cache.xml").read_text())}

    def test_periods_are_sorted_and_converted(self) -> None:
        haneda = self.tafs["RJTT"]
        self.assertEqual(haneda.issued, datetime(2026, 9, 16, 11, 5, tzinfo=UTC))
        self.assertFalse(haneda.amended)
        starts = [period.start for period in haneda.periods]
        self.assertEqual(starts, sorted(starts))
        prevailing = next(period for period in haneda.periods if period.change is None)
        self.assertEqual(prevailing.ws, 7.2)  # 14 kt
        self.assertEqual(prevailing.vis, 9000)
        self.assertEqual(prevailing.cloud, [["FEW", 240], ["BKN", 460]])
        self.assertIn("TEMPO", {period.change for period in haneda.periods})

    def test_probability_groups_and_amendments(self) -> None:
        payloads = [taf.to_json() for taf in self.tafs.values()]
        for payload in payloads:
            for period in payload["periods"]:
                self.assertIn(period["change"], (None, "FM", "BECMG", "TEMPO", "PROB"))
                if period["prob"] is not None:
                    self.assertTrue(0 <= period["prob"] <= 100)
        document = (
            '<response><data num_results="1"><TAF><raw_text><![CDATA[TAF RJTT 161105Z 1612/1718]]></raw_text>'
            "<station_id>RJTT</station_id><issue_time>2026-09-16T11:05:00.000Z</issue_time>"
            "<valid_time_from>2026-09-16T12:00:00.000Z</valid_time_from>"
            "<valid_time_to>2026-09-17T18:00:00.000Z</valid_time_to><remarks>AMD</remarks>"
            "<forecast><fcst_time_from>2026-09-16T12:00:00.000Z</fcst_time_from>"
            "<fcst_time_to>2026-09-16T16:00:00.000Z</fcst_time_to><change_indicator>TEMPO</change_indicator>"
            "<probability>40</probability><wind_speed_kt>20</wind_speed_kt><wind_gust_kt>35</wind_gust_kt>"
            '<visibility_statute_mi>6+</visibility_statute_mi><sky_condition sky_cover="NSC"/>'
            "</forecast></TAF></data></response>"
        )
        taf = parse_tafs(document)[0]
        self.assertTrue(taf.amended)
        period = taf.periods[0]
        self.assertEqual((period.change, period.prob), ("TEMPO", 40))
        self.assertEqual((period.ws, period.gust, period.vis), (10.3, 18.0, 10000))
        self.assertEqual(period.cloud, [["NSC", None]])

    def test_a_malformed_document_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            parse_tafs("<response>")


class StationTableTests(unittest.TestCase):
    def test_reads_the_fixture(self) -> None:
        table = parse_stations((FIXTURES / "airport-stations.json").read_text())
        haneda = table["RJTT"]
        self.assertEqual((haneda.name, haneda.iata, haneda.wmo), ("Tokyo/Haneda Intl", "HND", "47671"))
        self.assertEqual(haneda.elev, 5.0)
        self.assertIsNone(table["RKSI"].wmo)
        with self.assertRaises(ValueError):
            parse_stations("{}")


class ShardingTests(unittest.TestCase):
    def test_the_key_is_two_letters_and_three_under_k(self) -> None:
        self.assertEqual(shard_key("RJTT"), "RJ")
        self.assertEqual(shard_key("ZBAA"), "ZB")
        self.assertEqual(shard_key("KABQ"), "KAB")
        self.assertEqual(shard_key("KJFK"), "KJF")
        self.assertEqual(shard_path("RJ", "1f3a9c2e"), "../airport-shards/RJ-1f3a9c2e.json")

    def test_rounds_are_ten_minute_minutes(self) -> None:
        self.assertEqual(parse_round("202609161430"), datetime(2026, 9, 16, 14, 30, tzinfo=UTC))
        self.assertEqual(round_directory(parse_round("202609161430")), "airport.202609161430")
        self.assertEqual(floor_round(datetime(2026, 9, 16, 14, 39, 51, tzinfo=UTC)), parse_round("202609161430"))
        for bad in ("202609161435", "2026091614", "20260916143z"):
            with self.subTest(round=bad), self.assertRaises(AirportProductError):
                parse_round(bad)


class BuildTests(unittest.TestCase):
    """The merge, the history window and the content-addressed shards, on
    rounds small enough to reason about."""

    def scratch(self) -> tuple[Path, Path]:
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        return directory / "raw", directory / "out"

    def build(self, raw: Path, output: Path, name: str, **kwargs: object) -> dict[str, object]:
        moment = parse_round(name)
        return build_product(
            moment,
            raw,
            output,
            previous_index=load_previous_index(None, output),
            force=True,
            now=moment + timedelta(minutes=5),
            **kwargs,  # type: ignore[arg-type]
        )

    def test_merge_keeps_history_and_the_new_report_wins(self) -> None:
        raw, output = self.scratch()
        write_round(raw, "202609161430", csv_document(metar_row("RJTT", "2026-09-16T14:30:00.000Z", temp_c="21")))
        self.build(raw, output, "202609161430")
        write_round(
            raw,
            "202609161440",
            csv_document(
                metar_row("RJTT", "2026-09-16T14:30:00.000Z", temp_c="22"),  # a correction of the same hour
                metar_row("RJTT", "2026-09-16T14:40:00.000Z", temp_c="23"),
                metar_row("ZBAA", "2026-09-16T14:40:00.000Z", temp_c="19"),
            ),
        )
        report = self.build(raw, output, "202609161440")
        index = read_index(output / "airport.202609161440" / "index.json")
        shard = json.loads((output / SHARD_DIRECTORY / Path(index["shards"]["RJ"]["path"]).name).read_bytes())
        times = [metar["time"] for metar in shard["stations"]["RJTT"]["metars"]]
        self.assertEqual(times, ["2026-09-16T14:40:00Z", "2026-09-16T14:30:00Z"])
        self.assertEqual([metar["t"] for metar in shard["stations"]["RJTT"]["metars"]], [23.0, 22.0])
        self.assertEqual(report["stations"], 2)
        self.assertEqual([row[0] for row in index["stations"]], ["RJTT", "ZBAA"])
        self.assertEqual(index["stations"][0][4], "2026-09-16T14:40:00Z")

    def test_a_station_that_did_not_report_keeps_its_history_and_its_shard(self) -> None:
        raw, output = self.scratch()
        write_round(
            raw,
            "202609161430",
            csv_document(
                metar_row("RJTT", "2026-09-16T14:30:00.000Z"),
                metar_row("ZBAA", "2026-09-16T14:30:00.000Z"),
            ),
        )
        first = self.build(raw, output, "202609161430")
        self.assertEqual(first["shards"], {"total": 2, "written": 2, "unchanged": 0, "missing": []})
        write_round(raw, "202609161440", csv_document(metar_row("ZBAA", "2026-09-16T14:40:00.000Z")))
        second = self.build(raw, output, "202609161440")
        self.assertEqual(second["shards"]["written"], 1)
        self.assertEqual(second["shards"]["unchanged"], 1)
        before = read_index(output / "airport.202609161430" / "index.json")
        after = read_index(output / "airport.202609161440" / "index.json")
        self.assertEqual(before["shards"]["RJ"], after["shards"]["RJ"])  # same name, not rewritten
        self.assertNotEqual(before["shards"]["ZB"], after["shards"]["ZB"])
        self.assertEqual(after["stations"][0][0], "RJTT")

    def test_a_shard_is_named_by_the_crc32_of_its_own_bytes(self) -> None:
        raw, output = self.scratch()
        write_round(raw, "202609161430", csv_document(metar_row("RJTT", "2026-09-16T14:30:00.000Z")))
        self.build(raw, output, "202609161430")
        index = read_index(output / "airport.202609161430" / "index.json")
        for shard, entry in index["shards"].items():
            path = output / "airport.202609161430" / entry["path"]
            payload = path.read_bytes()
            self.assertEqual(path.name, f"{shard}-{crc32_hex(payload)}.json")
            self.assertEqual(len(payload), entry["byteLength"])
            self.assertEqual(crc32_hex(payload), entry["crc32"])
            validate_shard(json.loads(payload))

    def test_reports_older_than_the_window_fall_off(self) -> None:
        raw, output = self.scratch()
        write_round(raw, "202609161430", csv_document(metar_row("RJTT", "2026-09-16T14:30:00.000Z")))
        self.build(raw, output, "202609161430")
        later = parse_round("202609161430") + timedelta(hours=HISTORY_HOURS, minutes=10)
        name = later.strftime("%Y%m%d%H%M")
        write_round(raw, name, csv_document(metar_row("RJTT", f"{later:%Y-%m-%dT%H:%M}:00.000Z")))
        self.build(raw, output, name)
        index = read_index(output / f"airport.{name}" / "index.json")
        shard = json.loads((output / SHARD_DIRECTORY / Path(index["shards"]["RJ"]["path"]).name).read_bytes())
        self.assertEqual([metar["time"] for metar in shard["stations"]["RJTT"]["metars"]], [f"{later:%Y-%m-%dT%H:%M}:00Z"])

    def test_a_station_leaves_when_nothing_is_left_in_the_window(self) -> None:
        raw, output = self.scratch()
        write_round(raw, "202609161430", csv_document(metar_row("RJTT", "2026-09-16T14:30:00.000Z")))
        self.build(raw, output, "202609161430")
        later = parse_round("202609161430") + timedelta(hours=HISTORY_HOURS + 1)
        name = later.strftime("%Y%m%d%H%M")
        write_round(raw, name, csv_document(metar_row("ZBAA", f"{later:%Y-%m-%dT%H:%M}:00.000Z")))
        report = self.build(raw, output, name)
        index = read_index(output / f"airport.{name}" / "index.json")
        self.assertEqual([row[0] for row in index["stations"]], ["ZBAA"])
        self.assertNotIn("RJ", index["shards"])
        self.assertEqual(report["stations"], 1)

    def test_the_station_table_names_the_station_and_a_stranger_keeps_its_own_position(self) -> None:
        raw, output = self.scratch()
        write_round(
            raw,
            "202609161430",
            csv_document(
                metar_row("RJTT", "2026-09-16T14:30:00.000Z"),
                metar_row("RJXX", "2026-09-16T14:30:00.000Z", latitude="12.5", longitude="-70.25", elevation_m="17"),
            ),
        )
        (raw / "airport-stations.json").write_text(
            json.dumps([{"icaoId": "RJTT", "iataId": "HND", "wmoId": "47671", "site": "Tokyo/Haneda Intl", "lat": 35.553, "lon": 139.781, "elev": 5}]),
            encoding="utf-8",
        )
        self.build(raw, output, "202609161430")
        index = read_index(output / "airport.202609161430" / "index.json")
        shard = json.loads((output / SHARD_DIRECTORY / Path(index["shards"]["RJ"]["path"]).name).read_bytes())
        self.assertEqual(shard["stations"]["RJTT"]["name"], "Tokyo/Haneda Intl")
        self.assertEqual(shard["stations"]["RJTT"]["iata"], "HND")
        stranger = shard["stations"]["RJXX"]
        self.assertIsNone(stranger["name"])
        self.assertEqual((stranger["lat"], stranger["lon"], stranger["elev"]), (12.5, -70.25, 17.0))

    def test_the_pointer_is_withheld_only_when_both_observations_fail(self) -> None:
        raw, output = self.scratch()
        write_round(
            raw,
            "202609161430",
            csv_document(metar_row("RJTT", "2026-09-16T14:30:00.000Z")),
            ok=("awc-tafs", "awc-stations"),
        )
        report = self.build(raw, output, "202609161430")
        self.assertIsNotNone(report["pointer"])
        self.assertEqual(report["stations"], 0)  # the TAFs alone publish nothing new
        write_round(raw, "202609161440", csv_document(metar_row("RJTT", "2026-09-16T14:40:00.000Z")), ok=("awc-stations",))
        failed = self.build(raw, output, "202609161440")
        self.assertIsNone(failed["pointer"])
        self.assertTrue((output / "airport.202609161440" / "index.json").exists())
        pointer = json.loads((output / "latest-airport.json").read_bytes())
        self.assertEqual(pointer["path"], "airport.202609161430/index.json")  # the previous round stays live
        statuses = {status["id"]: status for status in failed["sources"]}
        self.assertEqual(statuses["awc-metars"]["error"], "HTTP 503")
        self.assertFalse(statuses["awc-tafs"]["ok"])

    def test_a_failed_taf_fetch_keeps_the_previous_forecasts(self) -> None:
        raw, output = self.scratch()
        taf = (
            '<response><data num_results="1"><TAF><raw_text><![CDATA[TAF RJTT 161105Z 1612/1718]]></raw_text>'
            "<station_id>RJTT</station_id><issue_time>2026-09-16T11:05:00.000Z</issue_time>"
            "<valid_time_from>2026-09-16T12:00:00.000Z</valid_time_from>"
            "<valid_time_to>2026-09-17T18:00:00.000Z</valid_time_to>"
            "<forecast><fcst_time_from>2026-09-16T12:00:00.000Z</fcst_time_from>"
            "<fcst_time_to>2026-09-17T18:00:00.000Z</fcst_time_to></forecast></TAF></data></response>"
        )
        write_round(raw, "202609161430", csv_document(metar_row("RJTT", "2026-09-16T14:30:00.000Z")), taf)
        first = self.build(raw, output, "202609161430")
        self.assertEqual(first["tafs"], 1)
        write_round(
            raw,
            "202609161440",
            csv_document(metar_row("RJTT", "2026-09-16T14:40:00.000Z")),
            ok=("awc-metars", "awc-stations"),
        )
        second = self.build(raw, output, "202609161440")
        self.assertEqual(second["tafs"], 1)
        index = read_index(output / "airport.202609161440" / "index.json")
        self.assertEqual(index["stations"][0][13], 1)
        write_round(raw, "202609161450", csv_document(metar_row("RJTT", "2026-09-16T14:50:00.000Z")))
        third = self.build(raw, output, "202609161450")
        self.assertEqual(third["tafs"], 0)  # the cache came back and no longer carries it

    def test_a_round_is_not_rebuilt_without_force(self) -> None:
        raw, output = self.scratch()
        write_round(raw, "202609161430", csv_document(metar_row("RJTT", "2026-09-16T14:30:00.000Z")))
        self.build(raw, output, "202609161430")
        with self.assertRaises(AirportProductError):
            build_product(parse_round("202609161430"), raw, output)


class GoldenTests(unittest.TestCase):
    def build_both(self, output: Path) -> list[dict[str, object]]:
        reports = []
        for name, generated in zip(ROUNDS, GENERATED):
            reports.append(
                build_product(
                    parse_round(name),
                    FIXTURES,
                    output,
                    previous_index=load_previous_index(None, output),
                    force=True,
                    now=generated,
                )
            )
        return reports

    def test_two_rounds_match_the_golden(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch)
            reports = self.build_both(output)
            built = {
                str(path.relative_to(output)): json.loads(path.read_bytes())
                for path in sorted(output.rglob("*.json"))
            }
            expected = {
                str(path.relative_to(EXPECTED)): json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(EXPECTED.rglob("*.json"))
            }
            self.assertEqual(sorted(built), sorted(expected))
            for name in sorted(expected):
                with self.subTest(file=name):
                    self.assertEqual(built[name], expected[name])
            self.assertEqual(reports[0]["shards"]["unchanged"], 0)
            self.assertGreater(reports[1]["shards"]["unchanged"], 0)
            self.assertGreater(reports[1]["reports"], reports[0]["reports"])
            index_bytes = (output / f"airport.{ROUNDS[1]}" / "index.json").read_bytes()
            pointer = json.loads((output / "latest-airport.json").read_bytes())
            self.assertEqual((pointer["byteLength"], pointer["crc32"]), (len(index_bytes), crc32_hex(index_bytes)))

    def test_a_rebuild_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self.build_both(Path(first))
            self.build_both(Path(second))
            left = {str(path.relative_to(first)): path.read_bytes() for path in sorted(Path(first).rglob("*.json"))}
            right = {str(path.relative_to(second)): path.read_bytes() for path in sorted(Path(second).rglob("*.json"))}
            self.assertEqual(left, right)


class SchemaTests(unittest.TestCase):
    def index(self) -> dict[str, object]:
        return json.loads((EXPECTED / f"airport.{ROUNDS[1]}" / "index.json").read_text(encoding="utf-8"))

    def shard(self) -> dict[str, object]:
        path = next(path for path in sorted((EXPECTED / SHARD_DIRECTORY).iterdir()) if path.name.startswith("RJ-"))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_the_golden_validates(self) -> None:
        validate_index(self.index())
        validate_shard(self.shard())
        validate_pointer(json.loads((EXPECTED / "latest-airport.json").read_text(encoding="utf-8")))

    def test_rejects_a_malformed_index(self) -> None:
        cases: list[tuple[str, object]] = [
            ("schemaVersion", 2),
            ("issued", "2026-09-16T14:35:00Z"),  # not a round
            ("generated", "2026-09-16 14:46:20"),
            ("shards", []),
            ("stations", {}),
            ("sources", [{"id": "awc-metars", "ok": False}]),
        ]
        for key, value in cases:
            payload = self.index()
            payload[key] = value
            with self.subTest(key=key), self.assertRaises(AirportProductError):
                validate_index(payload)

    def test_an_index_row_is_fourteen_values_in_order(self) -> None:
        payload = self.index()
        rows = payload["stations"]
        assert isinstance(rows, list)
        with self.assertRaises(AirportProductError):
            validate_index({**payload, "stations": [rows[0][:-1]]})
        out_of_order = [rows[1], rows[0]]
        with self.assertRaises(AirportProductError):
            validate_index({**payload, "stations": out_of_order})
        unnamed = list(rows[0])
        unnamed[0] = "QQQQ"  # a shard the index does not carry
        with self.assertRaises(AirportProductError):
            validate_index({**payload, "stations": [unnamed]})
        wind = list(rows[0])
        wind[7] = 400
        with self.assertRaises(AirportProductError):
            validate_index({**payload, "stations": [wind]})

    def test_a_shard_entry_must_name_its_own_bytes(self) -> None:
        payload = self.index()
        shards = payload["shards"]
        assert isinstance(shards, dict)
        moved = {key: dict(entry) for key, entry in shards.items()}
        moved["RJ"]["path"] = "../airport-shards/RJ-00000000.json"
        with self.assertRaises(AirportProductError):
            validate_index({**payload, "shards": moved})
        absolute = {key: dict(entry) for key, entry in shards.items()}
        absolute["RJ"]["path"] = f"/airport-shards/RJ-{absolute['RJ']['crc32']}.json"
        with self.assertRaises(AirportProductError):
            validate_index({**payload, "shards": absolute})

    def test_rejects_a_malformed_shard(self) -> None:
        payload = self.shard()
        stations = payload["stations"]
        assert isinstance(stations, dict)
        icao = next(iter(stations))
        with self.assertRaises(AirportProductError):
            validate_shard({**payload, "shard": "ZB"})  # the stations do not belong to it
        with self.assertRaises(AirportProductError):
            validate_shard({**payload, "stations": {icao: {**stations[icao], "metars": []}}})
        reversed_history = dict(stations[icao])
        reversed_history["metars"] = list(reversed(reversed_history["metars"]))
        if len(reversed_history["metars"]) > 1:
            with self.assertRaises(AirportProductError):
                validate_shard({**payload, "stations": {icao: reversed_history}})
        broken = dict(stations[icao])
        broken["metars"] = [{**broken["metars"][0], "cloud": [["FEW"]]}]
        with self.assertRaises(AirportProductError):
            validate_shard({**payload, "stations": {icao: broken}})
        typed = dict(stations[icao])
        typed["metars"] = [{**typed["metars"][0], "type": "TREND"}]
        with self.assertRaises(AirportProductError):
            validate_shard({**payload, "stations": {icao: typed}})

    def test_pointer(self) -> None:
        index = encode_json({"schemaVersion": 1})
        pointer = build_pointer(parse_round(ROUNDS[0]), f"airport.{ROUNDS[0]}/index.json", index)
        self.assertEqual(pointer["crc32"], crc32_hex(index))
        self.assertEqual(pointer["product"], "airport")
        for bad in (f"airport.{ROUNDS[1]}/index.json", f"/airport.{ROUNDS[0]}/index.json", "index.json"):
            with self.subTest(path=bad), self.assertRaises(AirportProductError):
                validate_pointer({**pointer, "path": bad})
        with self.assertRaises(AirportProductError):
            validate_pointer({**pointer, "schemaVersion": 2})


if __name__ == "__main__":
    unittest.main()

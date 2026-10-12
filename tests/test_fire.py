"""The fire product (``xuebuild/fire``): the FIRMS reader on the fetched
fixture and on constructed rows, the merge, the index's byte spans, the
validators on malformed input, and two consecutive rounds held to a
committed golden.
"""

from __future__ import annotations

import copy
import csv
import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path

from tests.prepare_fire_golden import FIXTURES, GENERATED, ROUNDS, build_rounds, pretty
from xuebuild.errors import FireProductError
from xuebuild.fire import firms
from xuebuild.fire.build import build_product, load_previous_index
from xuebuild.fire.schema import (
    build_pointer,
    encode_json,
    floor_round,
    parse_round,
    read_index,
    round_directory,
    validate_hour_line,
    validate_index,
    validate_pointer,
)
from xuebuild.fire.sensors import SENSORS, SOURCES

EXPECTED = FIXTURES / "expected"
VIIRS = next(source for source in SOURCES if source.id == "firms-noaa-20-viirs-c2")
MODIS = next(source for source in SOURCES if source.id == "firms-modis-c6-1")
VIIRS_HEADER = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight"
MODIS_HEADER = "latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,confidence,version,bright_t31,frp,daynight"


def detections_of(body: bytes) -> list[firms.Detection]:
    found = []
    for line in body.decode("ascii").splitlines():
        hour = json.loads(line)
        for position in range(hour["n"]):
            found.append(
                firms.Detection(
                    hour["hour"] + hour["t"][position],
                    hour["lat"][position],
                    hour["lon"][position],
                    hour["frp"][position],
                    hour["bt"][position],
                    hour["conf"][position],
                    hour["dn"][position],
                )
            )
    return found


class ReaderTest(unittest.TestCase):
    def test_a_viirs_row(self) -> None:
        text = f"{VIIRS_HEADER}\n47.12345,119.98765,367.0,0.4,0.6,2026-10-11,0508,N20,high,2.0NRT,290.1,301.6,D\n"
        result = firms.read_csv(text, VIIRS)
        self.assertEqual((result.reports, result.rejected), (1, 0))
        (detection,) = result.detections["viirs-noaa20"]
        self.assertEqual(detection.time, int(datetime(2026, 10, 11, 5, 8, tzinfo=UTC).timestamp()))
        # Half to even on the decimal, not on a float's binary approximation.
        self.assertEqual((detection.lat, detection.lon), (471234, 1199876))
        self.assertEqual((detection.frp, detection.bt, detection.conf, detection.dn), (3016, 3670, "h", "D"))

    def test_modis_splits_by_platform_and_classes_its_percentage(self) -> None:
        text = (
            f"{MODIS_HEADER}\n"
            "-15.7334,48.01693,305.51,1.62,1.25,2026-10-11,0005,A,29,6.1NRT,290.85,14.05,N\n"
            "-16.16046,48.63194,311.78,1.44,1.19,2026-10-11,5,T,80,6.1NRT,288.27,18.1,D\n"
        )
        result = firms.read_csv(text, MODIS)
        self.assertEqual([detection.conf for detection in result.detections["modis-aqua"]], ["l"])
        (terra,) = result.detections["modis-terra"]
        self.assertEqual(terra.conf, "h")
        self.assertEqual(terra.time, int(datetime(2026, 10, 11, 0, 5, tzinfo=UTC).timestamp()))
        self.assertEqual([firms.modis_confidence(value) for value in (0, 30, 79, 80, 100)], ["l", "n", "n", "h", "h"])

    def test_bad_rows_are_counted_not_raised(self) -> None:
        good = "47.1,119.9,340.0,0.4,0.6,2026-10-11,0508,N20,nominal,2.0NRT,290.1,3.0,N"
        rows = [
            good.replace(",N20,", ",N,"),  # another satellite's row
            good[:-1] + "X",  # day/night
            "abc" + good[4:],  # latitude
            good.replace("2026-10-11", "2026-13-11"),  # date
            good.replace(",nominal,", ",maybe,"),  # confidence
            good.replace("340.0", "1340.0"),  # brightness out of range
            good.replace(",3.0,", ",-1.0,"),  # negative power
        ]
        result = firms.read_csv("\n".join([VIIRS_HEADER, good, *rows]) + "\n", VIIRS)
        self.assertEqual((result.reports, result.rejected), (8, 7))
        self.assertEqual(len(result.detections["viirs-noaa20"]), 1)


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
        for directory, index in zip(self.directories, self.indexes):
            expected = EXPECTED / directory.name
            self.assertEqual(pretty(json.loads((directory / "index.json").read_bytes())) + "\n", (expected / "index.json").read_text())
            for sensor in index["sensors"]:
                path = sensor["file"]["path"]
                self.assertEqual((directory / path).read_bytes(), (expected / path).read_bytes(), path)
            self.assertEqual(pretty(json.loads((directory / "item.json").read_bytes())) + "\n", (expected / "item.json").read_text())
        for relative in ("fire/collection.json", "fire/item.json", "latest-fire.json"):
            self.assertEqual(pretty(json.loads((self.output / relative).read_bytes())) + "\n", (EXPECTED / relative).read_text(), relative)

    def test_every_sensor_is_published_in_order(self) -> None:
        for index in self.indexes:
            self.assertEqual([sensor["id"] for sensor in index["sensors"]], [sensor.id for sensor in SENSORS])

    def test_every_span_slices_out_its_own_hour(self) -> None:
        for directory, index in zip(self.directories, self.indexes):
            for row in index["hours"]:
                sensor = index["sensors"][row[0]]
                body = (directory / sensor["file"]["path"]).read_bytes()
                hour = json.loads(body[row[4] : row[4] + row[5]])
                validate_hour_line(hour, sensor["id"])
                self.assertEqual(hour["sensor"], sensor["id"])
                self.assertEqual(datetime.fromtimestamp(hour["hour"], UTC), datetime.fromisoformat(row[1]))
                self.assertEqual(hour["n"], row[2])
                self.assertEqual(max(hour["frp"]) / 10, row[3])

    def test_a_span_of_hours_is_one_range(self) -> None:
        index, directory = self.indexes[1], self.directories[1]
        rows = [row for row in index["hours"] if row[0] == 1]
        body = (directory / index["sensors"][1]["file"]["path"]).read_bytes()
        span = body[rows[0][4] : rows[-1][4] + rows[-1][5]]
        self.assertEqual(span, body.rstrip(b"\n"))
        self.assertEqual(len(span.split(b"\n")), len(rows))

    def test_the_second_round_merges_onto_the_first(self) -> None:
        first, second = self.indexes
        terra = [sensor for sensor in second["sensors"] if sensor["id"] == "modis-terra"][0]
        self.assertEqual(terra["latest"], "2026-10-12T01:32:00Z")  # new in the second round's file
        snpp = [index["sensors"][0] for index in (first, second)]
        self.assertEqual(snpp[0]["count"], snpp[1]["count"])  # the second file repeats what the first had

    def test_the_window_drops_a_detection_older_than_seven_days(self) -> None:
        old = int(datetime(2026, 10, 5, 4, 12, tzinfo=UTC).timestamp())
        found = [detections_of((directory / "viirs-noaa20.jsonl").read_bytes()) for directory in self.directories]
        self.assertIn(old, [detection.time for detection in found[0]])
        self.assertNotIn(old, [detection.time for detection in found[1]])

    def test_a_revised_detection_replaces_the_one_it_revises(self) -> None:
        with (FIXTURES / round_directory(parse_round(ROUNDS[1])) / "noaa-20-viirs-c2.csv").open() as handle:
            revised = next(row for row in csv.DictReader(handle) if row["frp"] == "99.9")
        lat = int(Decimal(revised["latitude"]).scaleb(4).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
        lon = int(Decimal(revised["longitude"]).scaleb(4).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
        found = [
            [detection for detection in detections_of((directory / "viirs-noaa20.jsonl").read_bytes()) if (detection.lat, detection.lon) == (lat, lon)]
            for directory in self.directories
        ]
        self.assertEqual(len(found[0]), 1)
        self.assertEqual(len(found[1]), 1)
        self.assertNotEqual(found[0][0].frp, 999)
        self.assertEqual(found[1][0].frp, 999)

    def test_rejected_rows_are_reported(self) -> None:
        sources = {source["id"]: source for source in self.indexes[1]["sources"]}
        self.assertEqual(sources["firms-noaa-20-viirs-c2"]["rejected"], 3)
        self.assertNotIn("file", sources["firms-noaa-20-viirs-c2"])

    def test_the_pointer_names_the_newest_round(self) -> None:
        pointer = json.loads((self.output / "latest-fire.json").read_bytes())
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
        self.first = parse_round(ROUNDS[0])
        build_product(self.first, self.raw, self.output, now=GENERATED[0])

    def tearDown(self) -> None:
        self.scratch.cleanup()

    def _record(self, moment: datetime, *, ok: bool) -> None:
        directory = self.raw / round_directory(moment)
        directory.mkdir(parents=True, exist_ok=True)
        sources = []
        for source in SOURCES:
            if ok:
                (directory / source.raw_name).write_text((MODIS_HEADER if source.modis else VIIRS_HEADER) + "\n")
                sources.append({"id": source.id, "ok": True, "url": source.url, "file": f"{directory.name}/{source.raw_name}"})
            else:
                sources.append({"id": source.id, "ok": False, "url": source.url, "error": "HTTP Error 503"})
        (directory / "fetch.json").write_text(json.dumps({"round": directory.name.split(".")[1], "sources": sources}))

    def test_failed_sources_keep_their_history_and_withhold_the_pointer(self) -> None:
        later = self.first + timedelta(hours=3)
        self._record(later, ok=False)
        (self.output / "latest-fire.json").unlink()
        previous = read_index(self.output / round_directory(self.first) / "index.json")
        report = build_product(later, self.raw, self.output, previous_index=previous, now=later)
        self.assertIsNone(report["pointer"])
        index = read_index(self.output / round_directory(later) / "index.json")
        self.assertEqual([sensor["id"] for sensor in index["sensors"]], [sensor["id"] for sensor in previous["sensors"]])
        # Only the NOAA-20 detection of 10-05 04:12 has left the window.
        counts = [sensor["count"] for sensor in previous["sensors"]]
        counts[1] -= 1
        self.assertEqual([sensor["count"] for sensor in index["sensors"]], counts)
        self.assertFalse(any(source["ok"] for source in index["sources"]))

    def test_the_window_empties_after_seven_days(self) -> None:
        later = self.first + timedelta(days=8)
        self._record(later, ok=True)
        report = build_product(later, self.raw, self.output, previous_index=load_previous_index(None, self.output), now=later)
        self.assertIsNotNone(report["pointer"])  # FIRMS answered, with nothing new
        index = read_index(self.output / round_directory(later) / "index.json")
        self.assertEqual((index["sensors"], index["hours"]), ([], []))

    def test_a_previous_file_that_is_not_its_index_starts_the_window_again(self) -> None:
        later = self.first + timedelta(hours=1)
        self._record(later, ok=True)
        path = self.output / round_directory(self.first) / "viirs-snpp.jsonl"
        path.write_bytes(path.read_bytes() + b"\n")
        build_product(later, self.raw, self.output, previous_index=load_previous_index(None, self.output), now=later)
        index = read_index(self.output / round_directory(later) / "index.json")
        self.assertNotIn("viirs-snpp", [sensor["id"] for sensor in index["sensors"]])
        self.assertIn("viirs-noaa20", [sensor["id"] for sensor in index["sensors"]])

    def test_an_existing_round_is_not_rebuilt_without_force(self) -> None:
        with self.assertRaises(FireProductError):
            build_product(self.first, self.raw, self.output, now=GENERATED[0])


class ValidatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.index = json.loads((EXPECTED / f"fire.{ROUNDS[1]}" / "index.json").read_text())
        lines = (EXPECTED / f"fire.{ROUNDS[1]}" / "viirs-noaa20.jsonl").read_bytes().splitlines()
        cls.line = json.loads(max(lines, key=len))

    def broken_index(self, change) -> dict:
        index = copy.deepcopy(self.index)
        change(index)
        return index

    def test_the_golden_passes(self) -> None:
        validate_index(self.index)
        validate_hour_line(self.line, "line")

    def test_malformed_indexes_are_refused(self) -> None:
        cases = {
            "unsorted": lambda index: index["hours"].reverse(),
            "gap between spans": lambda index: index["hours"][1].__setitem__(4, index["hours"][1][4] + 1),
            "short of the file": lambda index: index["sensors"][0]["file"].__setitem__("byteLength", 10**7),
            "count is not the sum": lambda index: index["sensors"][0].__setitem__("count", 1),
            "short row": lambda index: index["hours"][0].pop(),
            "off-hour": lambda index: index["hours"][0].__setitem__(1, "2026-10-11T05:30:00Z"),
            "sensor out of range": lambda index: index["hours"][-1].__setitem__(0, 9),
            "off-round issue": lambda index: index.__setitem__("issued", "2026-10-12T05:10:00Z"),
            "window backwards": lambda index: index["window"].__setitem__("start", "2026-10-13T00:00:00Z"),
            "bad file name": lambda index: index["sensors"][0]["file"].__setitem__("path", "fires.jsonl"),
            "sensor twice": lambda index: index["sensors"].append(index["sensors"][0]),
            "schema version": lambda index: index.__setitem__("schemaVersion", 2),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(FireProductError):
                validate_index(self.broken_index(change))

    def test_an_unknown_sensor_is_admitted(self) -> None:
        validate_index(self.broken_index(lambda index: index["sensors"][0].__setitem__("id", "abi-goes19") or index["sensors"][0]["file"].__setitem__("path", "abi-goes19.jsonl")))

    def test_malformed_lines_are_refused(self) -> None:
        cases = {
            "unsorted": lambda line: (line["lat"].reverse(), line["lon"].reverse()),
            "column too short": lambda line: line["frp"].pop(),
            "off-minute": lambda line: line["t"].__setitem__(0, line["t"][0] + 1),
            "past the hour": lambda line: line["t"].__setitem__(-1, 3600),
            "confidence": lambda line: line.__setitem__("conf", "x" * line["n"]),
            "day or night": lambda line: line.__setitem__("dn", "D" * (line["n"] - 1)),
            "brightness": lambda line: line["bt"].__setitem__(0, 100),
            "off-hour": lambda line: line.__setitem__("hour", line["hour"] + 60),
            "latitude": lambda line: line["lat"].__setitem__(-1, 900001),
        }
        for name, change in cases.items():
            line = copy.deepcopy(self.line)
            change(line)
            with self.subTest(name), self.assertRaises(FireProductError):
                validate_hour_line(line, "line")

    def test_pointer(self) -> None:
        moment = parse_round(ROUNDS[1])
        pointer = build_pointer(moment, f"{round_directory(moment)}/index.json", encode_json(self.index))
        validate_pointer(pointer)
        with self.assertRaises(FireProductError):
            validate_pointer(pointer | {"path": f"fire.{ROUNDS[0]}/index.json"})

    def test_rounds(self) -> None:
        self.assertEqual(floor_round(datetime(2026, 10, 12, 5, 59, 59, tzinfo=UTC)), parse_round("202610120500"))
        for value in ("202610120510", "2026101205", "202613120500"):
            with self.subTest(value), self.assertRaises(FireProductError):
                parse_round(value)


if __name__ == "__main__":
    unittest.main()

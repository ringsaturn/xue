"""The alerts product (``xuebuild/alerts``): the six adapters on the
fetched fixture and on constructed input, the category and level tables,
polygon rounding, the snapshot with its ``ended[]`` and carry-over,
the validators on malformed input, and two consecutive rounds held to a
committed golden.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from tests import jsonschemas
from tests.prepare_alerts_golden import FIXTURES, ROUNDS, build_rounds, pretty
from xuebuild.airport.fetch import SourceStatus
from xuebuild.alerts import archive, areas, cma, cnmaps, eccc, hko, jma, meteoalarm, nws
from xuebuild.alerts.build import build_product, load_previous_index
from xuebuild.alerts.geometry import bbox, normalize
from xuebuild.alerts.boxes import area_box, load_boxes
from xuebuild.alerts.build import _union
from xuebuild.alerts.networks import CMA_AREAS, JMA_CLASS20, METEOALARM_AREAS, NETWORKS, NWS_ZONES
from xuebuild.alerts.schema import (
    CATEGORIES,
    ROW_FIELDS,
    AlertsProductError,
    areas_bbox_path,
    areas_path,
    build_pointer,
    crc32_hex,
    encode_json,
    floor_round,
    parse_round,
    round_directory,
    validate_alert_line,
    validate_index,
    validate_pointer,
)
from xuebuild.alerts.tables import (
    cma_category,
    cma_level,
    eccc_category,
    eccc_level,
    hko_category,
    hko_level,
    jma_kind,
    meteoalarm_category,
    meteoalarm_level,
    nws_category,
    nws_level,
    tables,
    tables_crc32,
)

EXPECTED = FIXTURES / "expected"
FIRST, SECOND = (parse_round(name) for name in ROUNDS)


def built() -> tuple[tempfile.TemporaryDirectory, Path]:
    scratch = tempfile.TemporaryDirectory()
    output = Path(scratch.name)
    build_rounds(output)
    return scratch, output


def index_of(output: Path, moment: datetime) -> dict:
    return json.loads((output / round_directory(moment) / "index.json").read_bytes())


class TablesTest(unittest.TestCase):
    def test_tables_use_only_the_closed_categories(self) -> None:
        payload = tables()
        used = set(payload["nws"]["categories"].values()) | {kind["category"] for kind in payload["jma"]["kinds"].values()}
        self.assertLessEqual(used, set(CATEGORIES))

    def test_nws_levels_follow_the_event_suffix(self) -> None:
        self.assertEqual(nws_level("Special Weather Statement", "Moderate"), 1)
        self.assertEqual(nws_level("Hydrologic Outlook", "Unknown"), 1)
        self.assertEqual(nws_level("Small Craft Advisory", "Minor"), 2)
        self.assertEqual(nws_level("Winter Storm Watch", "Severe"), 2)
        self.assertEqual(nws_level("Gale Warning", "Moderate"), 3)
        self.assertEqual(nws_level("Tornado Warning", "Extreme"), 4)
        self.assertEqual(nws_level("Tornado Watch", "Extreme"), 2)
        self.assertEqual(nws_level("Evacuation Immediate", "Extreme"), 4)
        self.assertEqual(nws_level("Air Quality Alert", "Unknown"), 2)
        self.assertEqual(nws_level("Something New", None), 1)

    def test_nws_categories(self) -> None:
        self.assertEqual(nws_category("Red Flag Warning"), "fire")
        self.assertEqual(nws_category("Rip Current Statement"), "coastal")
        self.assertEqual(nws_category("Gale Warning"), "marine")
        self.assertEqual(nws_category("Tornado Warning"), "thunderstorm")
        self.assertEqual(nws_category("Something New"), "other")

    def test_jma_kinds(self) -> None:
        self.assertEqual((jma_kind("10")["category"], jma_kind("10")["level"]), ("rain", 2))
        self.assertEqual((jma_kind("03")["category"], jma_kind("03")["level"]), ("rain", 3))
        self.assertEqual((jma_kind("49")["category"], jma_kind("49")["level"]), ("landslide", 4))
        self.assertEqual((jma_kind("33")["category"], jma_kind("33")["level"]), ("rain", 5))
        self.assertEqual(jma_kind("48")["category"], "coastal")
        self.assertIsNone(jma_kind("99"))

    def test_eccc_levels_by_colour_then_type(self) -> None:
        self.assertEqual(eccc_level("yellow", "warning"), 2)
        self.assertEqual(eccc_level("orange", "warning"), 3)
        self.assertEqual(eccc_level("red", "warning"), 4)
        self.assertEqual(eccc_level(None, "statement"), 1)
        self.assertEqual(eccc_level(None, "advisory"), 2)
        self.assertEqual(eccc_level(None, "warning"), 3)
        self.assertEqual(eccc_category("Fog (advisory)"), "fog")
        self.assertEqual(eccc_category("Air quality"), "air")
        self.assertEqual(eccc_category("Unheard of"), "other")


class GeometryTest(unittest.TestCase):
    def test_polygons_keep_every_corner_at_four_decimals(self) -> None:
        ring = [[-100.0, 30.0], [-99.5, 30.00001], [-99.0, 30.0], [-99.0, 31.123456], [-100.0, 31.0], [-100.0, 30.0]]
        result = normalize({"type": "Polygon", "coordinates": [ring]})
        self.assertEqual(result["type"], "MultiPolygon")
        self.assertEqual(
            result["coordinates"],
            [[[[-100.0, 30.0], [-99.5, 30.0], [-99.0, 30.0], [-99.0, 31.1235], [-100.0, 31.0], [-100.0, 30.0]]]],
        )
        self.assertEqual(bbox(result), (-100.0, 30.0, -99.0, 31.1235))

    def test_a_sliver_stays_and_a_hole_rounding_collapses_is_dropped(self) -> None:
        sliver = [[0.0, 0.0], [0.0005, 0.0002], [0.001, 0.0], [0.0, 0.0]]
        result = normalize({"type": "Polygon", "coordinates": [sliver]})
        self.assertEqual(result["coordinates"], [[[[0.0, 0.0], [0.0005, 0.0002], [0.001, 0.0], [0.0, 0.0]]]])
        outer = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
        hole = [[0.5, 0.5], [0.50001, 0.5], [0.5, 0.50001], [0.5, 0.5]]
        result = normalize({"type": "MultiPolygon", "coordinates": [[outer, hole]]})
        self.assertEqual(len(result["coordinates"][0]), 1)

    def test_no_polygon_is_none(self) -> None:
        self.assertIsNone(normalize(None))
        self.assertIsNone(normalize({"type": "Point", "coordinates": [0, 0]}))
        self.assertIsNone(normalize({"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]}))

    def test_normalized_geometry_validates(self) -> None:
        feature = json.loads((FIXTURES / "alerts.202611010000/eccc/page-001.json").read_text(encoding="utf-8"))["features"][1]
        result = normalize(feature["geometry"])
        line = eccc.convert(feature)
        self.assertEqual(line["geometry"], result)
        self.assertEqual(validate_alert_line(line, "eccc"), bbox(result))


class NwsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads((FIXTURES / "alerts.202611010000/nws/active.json").read_text(encoding="utf-8"))

    def test_lines_keep_texts_codes_and_areas(self) -> None:
        line = nws.convert(self.payload["features"][1])
        self.assertEqual(line["native"]["event"], "Small Craft Advisory")
        self.assertEqual(line["native"]["code"], "SCY")
        self.assertEqual(line["areas"], ["ANZ639"])
        self.assertIsNone(line["geometry"])
        self.assertEqual(set(line["texts"]["en"]), {"headline", "description", "instruction", "area"})
        validate_alert_line(line, "nws")

    def test_tests_and_cancels_are_not_in_force(self) -> None:
        test = next(feature for feature in self.payload["features"] if feature["properties"]["status"] == "Test")
        self.assertIsNone(nws.convert(test))
        cancel = copy.deepcopy(self.payload["features"][2])
        cancel["properties"]["messageType"] = "Cancel"
        cancel["properties"]["references"] = [{"identifier": "urn:oid:x"}]
        self.assertIsNone(nws.convert(cancel))
        self.assertEqual(nws.cancelled_by(cancel), ["nws:urn:oid:x"])

    def test_an_update_names_what_it_replaces(self) -> None:
        line = nws.convert(self.payload["features"][0])  # an Update of an earlier flood warning
        self.assertEqual(line["replaces"], ["nws:urn:oid:2.49.0.1.840.0.7a03a51affa066bb29da3c0017294f2962c3807a.001.1"])
        validate_alert_line(line, "nws")
        self.assertNotIn("replaces", nws.convert(self.payload["features"][2]))  # an Alert references nothing
        broken = copy.deepcopy(line)
        broken["replaces"] = [line["id"]]
        with self.assertRaises(AlertsProductError):
            validate_alert_line(broken, "nws")
        broken["replaces"] = ["jma:130000-10"]
        with self.assertRaises(AlertsProductError):
            validate_alert_line(broken, "nws")

    def test_times_are_utc(self) -> None:
        line = nws.convert(self.payload["features"][0])
        self.assertEqual(line["sent"], "2026-10-31T23:30:00Z")
        self.assertEqual(line["expires"], "2026-11-01T00:03:00Z")


class JmaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reports = json.loads((FIXTURES / "alerts.202611010000/jma/map.json").read_text(encoding="utf-8"))

    def test_one_alert_per_office_and_kind_with_its_areas(self) -> None:
        alerts, lifted, skipped = jma.convert(self.reports)
        by_id = {line["id"]: line for line in alerts}
        self.assertIn("jma:011000-14", by_id)
        self.assertEqual(by_id["jma:011000-14"]["areas"], sorted(by_id["jma:011000-14"]["areas"]))
        self.assertIsNone(by_id["jma:011000-14"]["expires"])
        self.assertEqual(by_id["jma:011000-14"]["texts"]["ja"]["headline"], self.reports[0]["headlineText"])
        self.assertEqual(skipped, 1)  # the office the table does not know
        for line in alerts:
            validate_alert_line(line, line["id"])

    def test_a_lowered_urgent_warning_is_replaced_and_a_lifted_kind_cancelled(self) -> None:
        alerts, lifted, _ = jma.convert(self.reports)
        by_id = {line["id"]: line for line in alerts}
        # 危険警報から注意報 on 29: the 注意報 replaces the office's 危険警報.
        self.assertEqual(by_id["jma:150000-29"]["replaces"], ["jma:150000-49"])
        self.assertNotIn("jma:150000-49", lifted)
        self.assertTrue(any(identifier.endswith("-48") for identifier in lifted))  # 高潮危険警報 解除

    def test_a_stale_map_time_makes_the_source_fail(self) -> None:
        directory = FIXTURES / "alerts.202611010000/jma"
        map_time = jma.parse_map_time((directory / "map_time.json").read_text(encoding="utf-8"))
        relative = "alerts.202611010000/jma"
        fresh = SourceStatus(jma.SOURCE, True, fetched=map_time + timedelta(hours=6), file=relative)
        self.assertTrue(jma.read(FIXTURES, FIRST, {jma.SOURCE: fresh}).ok)
        stale = SourceStatus(jma.SOURCE, True, fetched=map_time + timedelta(hours=6, minutes=1), file=relative)
        result = jma.read(FIXTURES, FIRST, {jma.SOURCE: stale})
        self.assertFalse(result.ok)
        self.assertFalse(stale.ok)
        self.assertIn("has not advanced", stale.error)

    def test_an_unchanged_map_time_carries_over(self) -> None:
        directory = FIXTURES / "alerts.202611010000/jma"
        map_time = jma.parse_map_time((directory / "map_time.json").read_text(encoding="utf-8"))
        status = SourceStatus(jma.SOURCE, True, fetched=map_time + timedelta(minutes=5), file="alerts.202611010000/jma", detail={"unchanged": True})
        result = jma.read(FIXTURES, FIRST, {jma.SOURCE: status})
        self.assertTrue(result.ok and result.unchanged)


class EcccTest(unittest.TestCase):
    def test_ended_features_are_cancelled_not_in_force(self) -> None:
        status = SourceStatus(eccc.SOURCE, True, fetched=FIRST, file="alerts.202611010000/eccc")
        result = eccc.read(FIXTURES, FIRST, {eccc.SOURCE: status})
        self.assertEqual(len(result.alerts), 2)
        self.assertEqual(result.cancelled, {"eccc:1957348081643283032202610300501_fea1-2521"})
        for line in result.alerts:
            self.assertEqual(set(line["texts"]), {"en", "fr"})
            self.assertEqual(line["areas"], [])
            self.assertIsNotNone(line["geometry"])


class BuildTest(unittest.TestCase):
    def test_golden(self) -> None:
        scratch, output = built()
        with scratch:
            for moment in (FIRST, SECOND):
                directory = round_directory(moment)
                index = index_of(output, moment)
                expected = (EXPECTED / directory / "index.json").read_text(encoding="utf-8")
                self.assertEqual(pretty(index) + "\n", expected, directory)
                for network in index["networks"]:
                    path = network["file"]["path"]
                    self.assertEqual((output / directory / path).read_bytes(), (EXPECTED / directory / path).read_bytes(), path)
            pointer = json.loads((output / "latest-alerts.json").read_bytes())
            self.assertEqual(pretty(pointer) + "\n", (EXPECTED / "latest-alerts.json").read_text(encoding="utf-8"))

    def test_spans_address_their_own_line(self) -> None:
        scratch, output = built()
        with scratch:
            for moment in (FIRST, SECOND):
                index = index_of(output, moment)
                for row in index["alerts"]:
                    network = index["networks"][row[1]]
                    body = (output / round_directory(moment) / network["file"]["path"]).read_bytes()
                    offset, length = row[-2], row[-1]
                    line = json.loads(body[offset : offset + length])
                    self.assertEqual(line["id"], row[0])
                    self.assertEqual(line["native"]["event"], row[ROW_FIELDS.index("event")])

    def test_ended_reasons(self) -> None:
        scratch, output = built()
        with scratch:
            rows = [row for row in index_of(output, SECOND)["ended"] if not row[0].startswith("meteoalarm:")]
        ended = {row[0].split(":", 1)[0] + ":" + row[2] for row in rows}
        reasons = {row[0]: row[2] for row in rows}
        self.assertEqual(sorted(reasons.values()), ["cancelled", "expired", "expired", "gone", "replaced"])
        self.assertIn("eccc:expired", ended)
        self.assertEqual(reasons["nws:urn:oid:2.49.0.1.840.0.0e283990ffa56627410f1c41a79337ea2e7e74d6.012.1"], "cancelled")
        # An Update of the special weather statement names the one it replaced.
        statement = "nws:urn:oid:2.49.0.1.840.0.cad42b2581233afb49e1922a1d62d31e8b56842b"
        replaced = next(row for row in rows if row[2] == "replaced")
        self.assertEqual(replaced, [f"{statement}.001.1", 0, "replaced", f"{statement}.002.1"])

    def test_a_failed_network_carries_its_lines_over(self) -> None:
        scratch, output = built()
        with scratch:
            first, second = index_of(output, FIRST), index_of(output, SECOND)
            for network_id in ("jma", "eccc"):
                before = (output / round_directory(FIRST) / f"{network_id}.jsonl").read_bytes().splitlines()
                after = (output / round_directory(SECOND) / f"{network_id}.jsonl").read_bytes().splitlines()
                self.assertTrue(set(after) <= set(before), network_id)
            self.assertEqual(
                (output / round_directory(FIRST) / "jma.jsonl").read_bytes(), (output / round_directory(SECOND) / "jma.jsonl").read_bytes()
            )
            eccc_first = next(network for network in first["networks"] if network["id"] == "eccc")
            eccc_second = next(network for network in second["networks"] if network["id"] == "eccc")
            self.assertEqual(eccc_first["latest"], eccc_second["latest"])
            sources = {status["id"]: status for status in second["sources"]}
            self.assertFalse(sources["eccc-alerts"]["ok"])
            # JMA's map did not move: unchanged, so carried over, and not
            # yet stale (docs/alerts.md, JMA health).
            self.assertTrue(sources["jma-warning"]["ok"])

    def test_networks_and_areas(self) -> None:
        scratch, output = built()
        with scratch:
            index = index_of(output, FIRST)
        self.assertEqual([network["id"] for network in index["networks"]], [network.id for network in NETWORKS])
        self.assertEqual(index["networks"][0]["areas"]["path"], areas_path("nws-zones", NWS_ZONES.version))
        self.assertEqual(index["networks"][1]["areas"]["path"], areas_path("jma-class20", JMA_CLASS20.version))
        self.assertIsNone(index["networks"][2]["areas"])
        self.assertEqual(index["networks"][3]["areas"]["path"], areas_path("meteoalarm-areas", METEOALARM_AREAS.version))
        self.assertEqual(index["networks"][3]["attribution"], "EUMETNET – MeteoAlarm")
        self.assertEqual(index["categories"], list(CATEGORIES))

    def test_a_round_is_byte_identical_when_rebuilt(self) -> None:
        scratch, output = built()
        with scratch:
            directory = output / round_directory(FIRST)
            before = {path.name: path.read_bytes() for path in directory.iterdir()}
            with tempfile.TemporaryDirectory() as again:
                build_product(FIRST, FIXTURES, Path(again), previous_index=None, force=True, now=datetime(2026, 11, 1, 0, 1, 40, tzinfo=UTC))
                after = {path.name: path.read_bytes() for path in (Path(again) / round_directory(FIRST)).iterdir()}
            self.assertEqual({name: before[name] for name in after}, after)

    def test_a_round_is_not_rebuilt_without_force(self) -> None:
        scratch, output = built()
        with scratch, self.assertRaises(AlertsProductError):
            build_product(SECOND, FIXTURES, output, previous_index=load_previous_index(None, output))

    def test_no_network_arriving_withholds_the_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as raw, tempfile.TemporaryDirectory() as output:
            report = build_product(FIRST, Path(raw), Path(output), now=FIRST)
            self.assertIsNone(report["pointer"])
            self.assertFalse((Path(output) / "latest-alerts.json").exists())


class SchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.index = json.loads((EXPECTED / round_directory(SECOND) / "index.json").read_text(encoding="utf-8"))

    def test_the_golden_validates(self) -> None:
        validate_index(self.index)

    def test_areas_paths_climb_from_either_layout(self) -> None:
        """A round flat under the root climbs one directory to the boundary
        sets, one filed under its day five; the bbox file climbs as far."""
        for depth in (1, 5):
            index = copy.deepcopy(self.index)
            for network in index["networks"]:
                if network.get("areas"):
                    areas = network["areas"]
                    areas["path"] = areas_path(areas["set"], areas["version"], depth)
                    areas["bbox"]["path"] = areas_bbox_path(areas["set"], areas["version"], depth)
            with self.subTest(depth=depth):
                validate_index(index)
        index = copy.deepcopy(self.index)
        areas = next(network["areas"] for network in index["networks"] if network.get("areas"))
        for depth, bbox_depth in ((5, 1), (1, 5), (2, 2)):
            areas["path"] = areas_path(areas["set"], areas["version"], depth)
            areas["bbox"]["path"] = areas_bbox_path(areas["set"], areas["version"], bbox_depth)
            with self.subTest(depth=depth, bbox=bbox_depth), self.assertRaises(AlertsProductError):
                validate_index(index)

    def test_rounds_are_five_minutes(self) -> None:
        self.assertEqual(parse_round("202611010005"), datetime(2026, 11, 1, 0, 5, tzinfo=UTC))
        with self.assertRaises(AlertsProductError):
            parse_round("202611010003")
        self.assertEqual(floor_round(datetime(2026, 11, 1, 0, 9, 59, tzinfo=UTC)), datetime(2026, 11, 1, 0, 5, tzinfo=UTC))

    def assertRejected(self, index: dict) -> None:
        with self.assertRaises(AlertsProductError):
            validate_index(index)

    def assertOffSchema(self, schema: str, payload: dict) -> None:
        """A break only the schema can see: the writer's validator keeps
        to what a schema cannot say."""
        self.assertTrue(jsonschemas.errors(schema, payload))

    def test_malformed_indexes_are_rejected(self) -> None:
        unsorted = copy.deepcopy(self.index)
        unsorted["alerts"].reverse()
        self.assertRejected(unsorted)
        category = copy.deepcopy(self.index)
        category["alerts"][0][2] = "weather"
        self.assertRejected(category)
        level = copy.deepcopy(self.index)
        level["alerts"][0][3] = 6
        self.assertOffSchema("alerts-index", level)
        span = copy.deepcopy(self.index)
        span["alerts"][0][-2] = 10**6
        self.assertRejected(span)
        ended = copy.deepcopy(self.index)
        ended["ended"] = [[self.index["alerts"][0][0], self.index["alerts"][0][1], "gone"]]
        self.assertRejected(ended)
        reason = copy.deepcopy(self.index)
        reason["ended"][0][2] = "downgraded"
        self.assertOffSchema("alerts-index", reason)
        successor = copy.deepcopy(self.index)
        replaced = next(row for row in successor["ended"] if row[2] == "replaced")
        replaced[3] = replaced[0]  # not in force
        self.assertRejected(successor)
        unnamed = copy.deepcopy(self.index)
        next(row for row in unnamed["ended"] if row[2] == "replaced").pop()
        self.assertRejected(unnamed)
        named = copy.deepcopy(self.index)
        next(row for row in named["ended"] if row[2] == "gone").append(self.index["alerts"][0][0])
        self.assertRejected(named)
        area = copy.deepcopy(self.index)
        area["networks"][0]["areas"]["path"] = "alerts-areas/nws-zones.pmtiles"
        self.assertRejected(area)
        foreign = copy.deepcopy(self.index)
        foreign["alerts"][0][1] = 0  # an eccc id under nws
        self.assertRejected(foreign)
        reordered = copy.deepcopy(self.index)
        reordered["columns"].reverse()
        self.assertRejected(reordered)
        self.assertOffSchema("alerts-index", reordered)
        positional = copy.deepcopy(self.index)
        del positional["columns"]  # a round from before the field
        validate_index(positional)
        jsonschemas.check("alerts-index", positional)

    def test_malformed_lines_are_rejected(self) -> None:
        line = json.loads((EXPECTED / round_directory(SECOND) / "eccc.jsonl").read_bytes().splitlines()[0])
        validate_alert_line(line, "line")
        for change in ({"status": "test"}, {"texts": {"en": {"headline": 3}}}, {"level": 0}):
            self.assertOffSchema("alerts-line", line | change)
        for change in (
            {"network": "nws"},
            {"areas": ["A", "A"]},
            {"geometry": {"type": "MultiPolygon", "coordinates": [[[[0.12345, 0.0], [1.0, 0.0], [1.0, 1.0], [0.12345, 0.0]]]]}},
            {"geometry": {"type": "MultiPolygon", "coordinates": [[[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]]]}},
        ):
            broken = line | change
            with self.assertRaises(AlertsProductError, msg=str(change)):
                validate_alert_line(broken, "line")

    def test_provenance_fields(self) -> None:
        self.assertEqual(self.index["builder"], {"name": "xuebuild", "revision": None})
        self.assertEqual(self.index["mapping"], {"tables": tables_crc32()})
        sources = {status["id"]: status for status in self.index["sources"]}
        self.assertRegex(sources["nws-alerts"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("sha256", sources["eccc-alerts"])  # the fetch failed
        for network in self.index["networks"]:
            self.assertTrue(network["licenseUrl"].startswith("https://"))
        for key, value in (("builder", {"name": "other"}), ("mapping", {"tables": "x"})):
            broken = copy.deepcopy(self.index)
            broken[key] = value
            self.assertOffSchema("alerts-index", broken)
        digest = copy.deepcopy(self.index)
        digest["sources"][0]["sha256"] = "ABC"
        self.assertOffSchema("alerts-index", digest)
        terms = copy.deepcopy(self.index)
        terms["networks"][0]["licenseUrl"] = "weather.gov"
        self.assertOffSchema("alerts-index", terms)
        # Every one is optional: an index written before them still reads.
        older = copy.deepcopy(self.index)
        for key in ("builder", "mapping"):
            del older[key]
        for network in older["networks"]:
            del network["licenseUrl"]
        for status in older["sources"]:
            status.pop("sha256", None)
        validate_index(older)
        jsonschemas.check("alerts-index", older)

    def test_pointer(self) -> None:
        index_bytes = encode_json(self.index)
        pointer = build_pointer(SECOND, f"{round_directory(SECOND)}/index.json", index_bytes)
        validate_pointer(pointer)
        with self.assertRaises(AlertsProductError):
            validate_pointer(pointer | {"path": f"{round_directory(FIRST)}/index.json"})
        for path in (f"alerts/2026/11/01/alerts.{ROUNDS[1]}/index.json", f"alerts.{ROUNDS[1]}/index.json"):
            validate_pointer(pointer | {"path": path})
        for path in (f"alerts/2026/10/31/alerts.{ROUNDS[1]}/index.json", f"alerts/2026/11/01/alerts.{ROUNDS[0]}/index.json"):
            with self.subTest(path=path), self.assertRaises(AlertsProductError):
                validate_pointer(pointer | {"path": path})


class MeteoAlarmTest(unittest.TestCase):
    def feed(self, moment: datetime, country: str) -> list[dict]:
        path = FIXTURES / f"alerts.{moment:%Y%m%d%H%M}" / "meteoalarm" / f"{country}.json"
        return json.loads(path.read_text(encoding="utf-8"))["warnings"]

    def test_levels_by_awareness_colour_and_categories_by_type(self) -> None:
        self.assertIsNone(meteoalarm_level("1; green; Minor"))
        self.assertEqual(meteoalarm_level("2; yellow; Moderate"), 2)
        self.assertEqual(meteoalarm_level("3; Orange; Severe"), 3)
        self.assertEqual(meteoalarm_level("4; red; Extreme"), 4)
        self.assertIsNone(meteoalarm_level(None))
        self.assertEqual(meteoalarm_category("1; Wind"), "wind")
        self.assertEqual(meteoalarm_category("1; wind"), "wind")
        self.assertEqual(meteoalarm_category("5; high-temperature"), "heat")
        self.assertEqual(meteoalarm_category("7; coastalevent"), "coastal")
        self.assertEqual(meteoalarm_category("13; rain-flood"), "flood")
        self.assertEqual(meteoalarm_category("99; new"), "other")
        used = set(tables()["meteoalarm"]["categoryByAwarenessType"].values())
        self.assertLessEqual(used, set(CATEGORIES))

    def test_each_country_names_its_areas_its_own_way(self) -> None:
        germany, france, italy = (self.feed(FIRST, country) for country in ("germany", "france", "italy"))
        warn_cells = meteoalarm.convert(germany[1], "germany", FIRST)
        assert warn_cells is not None
        self.assertTrue(warn_cells["areas"] and all(code.startswith("WARNCELLID:") for code in warn_cells["areas"]))
        self.assertIsNone(warn_cells["geometry"])
        self.assertEqual(set(warn_cells["texts"]), {"de-DE", "en", "fr"})
        self.assertEqual(warn_cells["native"]["country"], "DE")
        self.assertEqual(warn_cells["native"]["level"], "2; yellow; Moderate")
        departements = meteoalarm.convert(france[1], "france", FIRST)
        assert departements is not None
        self.assertTrue(departements["areas"] and all(code.startswith("NUTS3:FR") for code in departements["areas"]))
        polygon = meteoalarm.convert(france[2], "france", FIRST)
        assert polygon is not None
        self.assertEqual(polygon["areas"], [])
        self.assertIsNotNone(polygon["geometry"])
        validate_alert_line(polygon, "meteoalarm")
        emma = meteoalarm.convert(italy[1], "italy", FIRST)
        assert emma is not None
        self.assertTrue(emma["areas"][0].startswith("EMMA_ID:IT"))
        # An expired message and a green one are not published.
        self.assertIsNone(meteoalarm.convert(germany[0], "germany", FIRST))
        self.assertIsNone(meteoalarm.convert(france[0], "france", FIRST))
        self.assertIsNone(meteoalarm.convert(italy[0], "italy", FIRST))

    def test_cap_references_are_replaces(self) -> None:
        update = meteoalarm.convert(self.feed(SECOND, "france")[1], "france", SECOND)
        assert update is not None
        self.assertEqual(update["replaces"], ["meteoalarm:2.49.0.0.250.0.FR.20261101060012.602021"])

    def test_a_cap_polygon_is_latitude_first(self) -> None:
        self.assertEqual(meteoalarm._ring("45.0,7.0 45.0,8.0 46.0,8.0 45.0,7.0"), [[7.0, 45.0], [8.0, 45.0], [8.0, 46.0], [7.0, 45.0]])
        self.assertIsNone(meteoalarm._ring("45.0;7.0 45,8"))

    def test_an_area_is_named_by_its_preferred_geocode(self) -> None:
        both = {"geocode": [{"valueName": "EMMA_ID", "value": "DE801"}, {"valueName": "WARNCELLID", "value": "501000005"}]}
        self.assertEqual(meteoalarm._area_code(both), "WARNCELLID:501000005")
        self.assertEqual(meteoalarm._area_code({"geocode": [{"valueName": "NUTS3", "value": "FR711"}]}), "NUTS3:FR711")
        self.assertIsNone(meteoalarm._area_code({"geocode": []}))

    def test_a_failed_country_is_carried_and_the_others_read(self) -> None:
        statuses = {
            f"meteoalarm-{country}": SourceStatus(f"meteoalarm-{country}", True, file=f"alerts.{ROUNDS[1]}/meteoalarm/{country}.json")
            for country in ("france", "italy")
        }
        result = meteoalarm.read(FIXTURES, SECOND, statuses)
        self.assertTrue(result.ok)
        assert result.carry is not None
        self.assertTrue(result.carry({"native": {"country": "DE"}}))
        self.assertFalse(result.carry({"native": {"country": "FR"}}))
        self.assertEqual(statuses["meteoalarm-france"].detail["reports"], 2)
        self.assertFalse(meteoalarm.read(FIXTURES, SECOND, {}).ok)

    def test_the_rounds(self) -> None:
        scratch, output = built()
        with scratch:
            first, second = index_of(output, FIRST), index_of(output, SECOND)
            before = (output / round_directory(FIRST) / "meteoalarm.jsonl").read_bytes().splitlines()
            after = (output / round_directory(SECOND) / "meteoalarm.jsonl").read_bytes().splitlines()
        rows = {row[0]: row for row in second["alerts"] if row[0].startswith("meteoalarm:")}
        # Germany's feed failed: its lines carry over byte for byte, less
        # the one that expired between the rounds.
        german = [line for line in before if b'"country":"DE"' in line]
        self.assertEqual(len(german), 2)
        self.assertIn(b"33aa44fa", german[0])  # the one made to expire at 00:03
        self.assertEqual([line for line in after if b'"country":"DE"' in line], german[1:])
        ended = {row[0]: row[2:] for row in second["ended"] if row[0].startswith("meteoalarm:")}
        self.assertEqual(sorted(reason[0] for reason in ended.values()), ["expired", "gone", "replaced"])
        self.assertEqual(ended["meteoalarm:2.49.0.0.250.0.FR.20261101060012.602021"][1], "meteoalarm:2.49.0.0.250.0.FR.20261101060012.999021")
        # Warn cells and départements are boxed by the set; EMMA_ID is not.
        italian = rows["meteoalarm:2.49.0.0.380.3.IT.261031105004.036"]
        self.assertEqual(italian[8:12], [None, None, None, None])
        self.assertTrue(all(row[8] is not None for alert, row in rows.items() if ".IT." not in alert))
        sources = {status["id"]: status for status in second["sources"]}
        self.assertFalse(sources["meteoalarm-germany"]["ok"])
        self.assertEqual(sources["meteoalarm-andorra"]["error"], "not fetched")
        self.assertEqual(len([status for status in first["sources"] if status["id"].startswith("meteoalarm-")]), len(meteoalarm.COUNTRIES))

    def test_a_box_beside_a_polygon_is_the_union(self) -> None:
        self.assertEqual(_union((0.0, 0.0, 1.0, 1.0), (2.0, -1.0, 3.0, 0.5)), (0.0, -1.0, 3.0, 1.0))
        self.assertIsNone(_union((0.0, 0.0, 1.0, 1.0), None))


class CmaTest(unittest.TestCase):
    def caps(self, moment: datetime) -> dict[str, bytes]:
        directory = FIXTURES / f"alerts.{moment:%Y%m%d%H%M}" / "cma"
        listing = json.loads((directory / cma.LIST_FILENAME).read_text(encoding="utf-8"))
        return {
            path: (directory / cma.CAP_DIRECTORY / cma._cap_filename(path)).read_bytes()
            for path in cma._listed(listing)
            if (directory / cma.CAP_DIRECTORY / cma._cap_filename(path)).exists()
        }

    def test_levels_by_severity_and_categories_by_the_chinese_event(self) -> None:
        self.assertEqual([cma_level(severity) for severity in ("Minor", "Moderate", "Severe", "Extreme", None)], [2, 2, 3, 4, 2])
        self.assertEqual(cma_category("大雾"), "fog")
        self.assertEqual(cma_category("地质灾害气象风险"), "landslide")
        self.assertEqual(cma_category("新的预警"), "other")
        self.assertLessEqual(set(tables()["cma"]["categoryByEvent"].values()), set(CATEGORIES))

    def test_the_listing_keeps_china_alone(self) -> None:
        listing = json.loads((FIXTURES / f"alerts.{ROUNDS[0]}" / "cma" / cma.LIST_FILENAME).read_text(encoding="utf-8"))
        paths = cma._listed(listing)
        self.assertEqual(len(paths), 3)
        self.assertTrue(all(path.startswith("cn-cma-xx/") for path in paths))
        self.assertEqual(cma._cap_filename("cn-cma-xx/2026/11/12/01/45/14-b8111d9e4d5a50fd37eba18cf1dc93c3.xml"), "20261112014514-b8111d9e4d5a50fd37eba18cf1dc93c3.xml")
        with self.assertRaises(ValueError):
            cma._listed({"items": None})

    def test_a_message_is_two_languages_and_its_county_codes(self) -> None:
        lines = [cma.convert(body, path, FIRST) for path, body in self.caps(FIRST).items()]
        by_event = {line["native"]["event"]: line for line in lines if line is not None}
        fog = by_event["大雾"]
        self.assertEqual(set(fog["texts"]), {"zh-CN", "en-US"})
        self.assertEqual((fog["level"], fog["native"]["colour"]), (2, "yellow"))
        self.assertEqual(fog["areas"], ["120115"])
        self.assertIsNone(fog["geometry"])
        self.assertTrue(fog["url"].startswith(cma.CAP_URL.format(path="cn-cma-xx/")))
        validate_alert_line(fog, "cma")
        # A township's code is drawn by its county; the twelve digits stay in native.
        frost = by_event["霜冻"]
        self.assertEqual(frost["areas"], ["330110"])
        self.assertEqual(frost["native"]["geocodes"], ["330110014000"])
        self.assertEqual(by_event["大风"]["sender"], "Nongan County Meteorological Observatory")

    def test_references_take_the_identifier_second_from_the_end(self) -> None:
        update = next(line for path, body in self.caps(SECOND).items() for line in [cma.convert(body, path, SECOND)] if line and line["native"]["msgType"] == "Update")
        self.assertEqual(update["replaces"], ["cma:33011041600000_20261110151408"])

    def test_a_message_that_did_not_arrive_carries_over(self) -> None:
        status = SourceStatus(cma.SOURCE, True, file=f"alerts.{ROUNDS[1]}/cma")
        result = cma.read(FIXTURES, SECOND, {cma.SOURCE: status})
        self.assertTrue(result.ok)
        assert result.carry is not None
        missing = next(path for path in cma._listed(json.loads((FIXTURES / f"alerts.{ROUNDS[1]}" / "cma" / cma.LIST_FILENAME).read_bytes())) if path not in self.caps(SECOND))
        self.assertTrue(result.carry({"url": cma.CAP_URL.format(path=missing)}))
        self.assertFalse(result.carry({"url": None}))
        self.assertEqual(result.latest, datetime(2026, 11, 1, 0, 9, tzinfo=UTC))
        self.assertFalse(cma.read(FIXTURES, SECOND, {}).ok)

    def test_the_rounds(self) -> None:
        index = json.loads((EXPECTED / round_directory(SECOND) / "index.json").read_text(encoding="utf-8"))
        ended = {row[0]: row[2:] for row in index["ended"] if row[0].startswith("cma:")}
        self.assertEqual(ended["cma:22012241600000_20261112094751"], ["expired"])
        self.assertEqual(ended["cma:33011041600000_20261110151408"], ["replaced", "cma:33011041600000_20261101080400"])
        rows = {row[0]: row for row in index["alerts"] if row[0].startswith("cma:")}
        self.assertIn("cma:12011541600000_20261111192247", rows)  # its message failed: carried over
        self.assertEqual(rows["cma:23270341600000_20261112070109"][8:12], [None, None, None, None])  # 新林区, not in the set
        self.assertTrue(all(row[8] is not None for alert, row in rows.items() if "232703" not in alert))


class HkoTest(unittest.TestCase):
    def test_levels_and_categories_by_code(self) -> None:
        self.assertEqual([hko_level(code) for code in ("WRAINA", "WRAINR", "WRAINB", "TC1", "TC8NE", "TC10", "WFNTSA")], [2, 3, 4, 2, 3, 4, 1])
        self.assertEqual(hko_category("TC9"), "wind")
        self.assertEqual(hko_category("WL"), "landslide")
        self.assertEqual((hko_category("WNEW"), hko_level("WNEW")), ("other", 2))

    def test_a_warning_is_three_languages_over_the_territory(self) -> None:
        status = SourceStatus(hko.SOURCE, True, file=f"alerts.{ROUNDS[0]}/hko")
        result = hko.read(FIXTURES, FIRST, {hko.SOURCE: status})
        by_code = {line["native"]["code"]: line for line in result.alerts}
        rain = by_code["WRAINA"]
        self.assertEqual(rain["id"], "hko:WRAINA-20261031T232000Z")
        self.assertEqual(set(rain["texts"]), {"en", "zh-Hant", "zh-Hans"})
        self.assertEqual(rain["texts"]["en"]["headline"], "Rainstorm Warning Signal (Amber)")
        self.assertEqual(rain["texts"]["zh-Hant"]["headline"], "暴雨警告信號 (黃色)")
        self.assertIn("Amber Rainstorm", rain["texts"]["en"]["description"])
        self.assertEqual(rain["areas"], [hko.AREA])
        self.assertEqual(by_code["WTS"]["expires"], "2026-11-01T00:03:00Z")
        for line in result.alerts:
            validate_alert_line(line, "hko")
        self.assertEqual(status.detail["reports"], 3)

    def test_a_cancel_is_not_in_force_and_ends_as_cancelled(self) -> None:
        status = SourceStatus(hko.SOURCE, True, file=f"alerts.{ROUNDS[1]}/hko")
        result = hko.read(FIXTURES, SECOND, {hko.SOURCE: status})
        self.assertEqual([line["id"] for line in result.alerts], ["hko:WRAINR-20261101T000200Z"])
        self.assertEqual(result.cancelled, {"hko:WMSGNL-20261031T220000Z"})
        index = json.loads((EXPECTED / round_directory(SECOND) / "index.json").read_text(encoding="utf-8"))
        ended = {row[0]: row[2:] for row in index["ended"] if row[0].startswith("hko:")}
        self.assertEqual(ended, {"hko:WMSGNL-20261031T220000Z": ["cancelled"], "hko:WRAINA-20261031T232000Z": ["gone"], "hko:WTS-20261031T231000Z": ["expired"]})


class ArchiveTest(unittest.TestCase):
    def archived(self, archive_root: Path) -> tuple[dict, dict, list[dict]]:
        scratch, output = built()
        with scratch:
            first = archive.archive_round(output, archive_root, FIRST, base_url=None)
            second = archive.archive_round(output, archive_root, SECOND, base_url=None, compact=True)
        directory = archive_root / archive.month_directory("2026-11")
        records = [json.loads(line) for line in (directory / archive.ALERTS_FILENAME).read_bytes().splitlines()]
        return first, second, records

    def test_two_rounds_fold_into_one_month(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            first, second, records = self.archived(Path(root))
            self.assertFalse(first["folded"])  # the same hour: journaled only
            self.assertTrue(second["folded"])
            self.assertEqual(second["written"][-1], "alerts/archive/catalog.json")
            index = json.loads((Path(root) / archive.month_directory("2026-11") / "index.json").read_bytes())
            archive.validate_month_index(index)
            archive.validate_catalog(json.loads((Path(root) / "alerts/archive/catalog.json").read_bytes()))
            self.assertFalse((Path(root) / archive.JOURNAL_FILENAME).exists())
        by_id = {record["id"]: record for record in records}
        self.assertEqual([record["id"] for record in records], sorted(by_id))
        statement = "nws:urn:oid:2.49.0.1.840.0.cad42b2581233afb49e1922a1d62d31e8b56842b"
        self.assertEqual(
            by_id[f"{statement}.001.1"]["ended"],
            {"time": "2026-11-01T00:05:00Z", "reason": "replaced", "by": f"{statement}.002.1"},
        )
        self.assertEqual(by_id[f"{statement}.002.1"]["first"], "2026-11-01T00:05:00Z")
        self.assertIsNone(by_id[f"{statement}.002.1"]["ended"])
        self.assertEqual(by_id["jma:011000-14"]["first"], "2026-11-01T00:00:00Z")

    def test_a_fold_is_a_function_of_the_records(self) -> None:
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            self.archived(Path(one))
            self.archived(Path(two))
            relative = archive.month_directory("2026-11")
            for name in (archive.ALERTS_FILENAME, "index.json"):
                self.assertEqual((Path(one) / relative / name).read_bytes(), (Path(two) / relative / name).read_bytes())
            scratch, output = built()
            with scratch:
                again = archive.archive_round(output, Path(one), SECOND, base_url=None, compact=True)
            self.assertEqual(again["written"], [])  # journaling a round twice changes nothing

    def test_the_index_names_its_columns_and_one_without_them_still_reads(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            self.archived(Path(root))
            path = Path(root) / archive.month_directory("2026-11") / "index.json"
            written = path.read_bytes()
            index = json.loads(written)
            self.assertEqual(index["columns"], list(archive.ROW_FIELDS))
            index.pop("columns")
            archive.validate_month_index(index)  # every month published before the field
            with self.assertRaisesRegex(AlertsProductError, "index.columns must be"):
                archive.validate_month_index(index | {"columns": list(reversed(archive.ROW_FIELDS))})
            path.write_bytes(encode_json(index))
            scratch, output = built()
            with scratch:
                archive.archive_round(output, Path(root), SECOND, base_url=None, compact=True)
            self.assertEqual(path.read_bytes(), written)  # an old index folds into one that names them

    def test_folding_keeps_the_first_round_and_the_last_line(self) -> None:
        line = {"id": "nws:x", "category": "other", "level": 1, "native": {"event": "E"}}
        later = line | {"level": 2}
        ended = {"time": "2026-11-01T00:10:00Z", "reason": "gone"}
        record = archive.fold(None, [{"round": "2026-11-01T00:05:00Z", "id": "nws:x", "line": line}])
        record = archive.fold(record, [{"round": "2026-11-01T00:10:00Z", "id": "nws:x", "ended": ended}])
        self.assertEqual(record["ended"], ended)
        record = archive.fold(record, [{"round": "2026-11-01T00:15:00Z", "id": "nws:x", "line": later}])
        self.assertEqual((record["first"], record["ended"], record["alert"]["level"]), ("2026-11-01T00:05:00Z", None, 2))
        self.assertIsNone(archive.fold(None, [{"round": "2026-11-01T00:10:00Z", "id": "nws:x", "ended": ended}]))

    def test_a_fold_is_due_once_an_hour(self) -> None:
        entries = [{"round": "2026-11-01T00:55:00Z"}]
        self.assertFalse(archive.due(entries, datetime(2026, 11, 1, 0, 55, tzinfo=UTC)))
        self.assertTrue(archive.due(entries, datetime(2026, 11, 1, 1, 0, tzinfo=UTC)))


class AreasTest(unittest.TestCase):
    def test_the_nws_set_has_the_layers_the_index_names(self) -> None:
        self.assertEqual(tuple(layer.name for layer in areas.SETS["nws-zones"].layers), NWS_ZONES.layers)

    def test_ugc_codes_from_the_shapefile_attributes(self) -> None:
        self.assertEqual(areas._zone_code({"STATE": "OK", "ZONE": "025"}), "OKZ025")
        self.assertEqual(areas._county_code({"STATE": "OK", "FIPS": "40109"}), "OKC109")
        self.assertEqual(areas._marine_code({"ID": "ANZ639"}), "ANZ639")
        self.assertIsNone(areas._county_code({"STATE": "OK", "FIPS": None}))
        self.assertEqual(areas._jma_code({"regioncode": "1310300"}), "1310300")
        self.assertIsNone(areas._jma_code({"regioncode": "13103"}))

    def test_meteoalarm_codes_from_the_upstream_attributes(self) -> None:
        self.assertEqual(tuple(layer.name for layer in areas.SETS["meteoalarm-areas"].layers), METEOALARM_AREAS.layers)
        self.assertEqual(areas._warncell_code({"WARNCELLID": 501000005}), "WARNCELLID:501000005")
        self.assertIsNone(areas._warncell_code({"WARNCELLID": None}))
        self.assertEqual(areas._departement_code({"code_insee": "01"}), "NUTS3:FR711")  # Ain, NUTS 2013
        self.assertEqual(areas._departement_code({"code_insee": "75"}), "NUTS3:FR101")
        self.assertIsNone(areas._departement_code({"code_insee": "975"}))

    def test_cma_codes_by_level_and_gcj_turned_into_wgs84(self) -> None:
        self.assertEqual(tuple(layer.name for layer in areas.SETS["cma-areas"].layers), CMA_AREAS.layers)
        self.assertEqual([cnmaps.layer_of(code) for code in ("110000", "441900", "419001", "810000")], ["cn_province", "cn_prefecture", "cn_county", "cn_province"])
        self.assertEqual(areas._adcode({"adcode": "120115"}), "120115")
        self.assertIsNone(areas._adcode({"adcode": "1201"}))
        # Tiananmen in GCJ-02 (Amap) and WGS84: the offset is about 600 m.
        lon, lat = cnmaps.gcj_to_wgs(np.array([116.403963]), np.array([39.915119]))
        self.assertAlmostEqual(float(lon[0]), 116.39772, places=4)
        self.assertAlmostEqual(float(lat[0]), 39.91372, places=4)

    def test_the_cnmaps_release_becomes_three_layers_with_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            land = Path(scratch) / "release" / cnmaps.LAND
            land.mkdir(parents=True)
            square = {"type": "MultiPolygon", "coordinates": [[[[87.0, 44.0], [87.1, 44.0], [87.1, 44.1], [87.0, 44.0]]]]}
            for code in ("100000", "650000", "654200", "654223"):
                (land / f"{code}.geojson").write_text(json.dumps(square), encoding="utf-8")
            directory = cnmaps.prepare(Path(scratch) / "release", Path(scratch))
            layers = {path.stem: json.loads(path.read_text()) for path in directory.glob("*.geojson")}
            codes = {name: [feature["properties"]["adcode"] for feature in layer["features"]] for name, layer in layers.items()}
            self.assertEqual(codes, {"cn_province": ["650000"], "cn_prefecture": ["654200"], "cn_county": ["654223", "654203"]})
            ring = layers["cn_county"]["features"][0]["geometry"]["coordinates"][0][0]
            self.assertNotEqual(ring[0], [87.0, 44.0])  # moved off GCJ-02
            with self.assertRaises(AlertsProductError):
                cnmaps.prepare(Path(scratch) / "nothing", Path(scratch))

    def test_the_pinned_box_files_are_the_fixture(self) -> None:
        for area_set in (NWS_ZONES, JMA_CLASS20, METEOALARM_AREAS, CMA_AREAS):
            body = (FIXTURES / "alerts-areas" / area_set.bbox_filename).read_bytes()
            self.assertEqual((len(body), crc32_hex(body)), (area_set.bbox_byte_length, area_set.bbox_crc32))
            self.assertEqual(list(json.loads(body)["layers"]), list(area_set.layers))

    def test_a_box_is_the_union_of_its_codes_or_none(self) -> None:
        boxes = {"zone": {"A": (0.0, 0.0, 1.0, 1.0)}, "fire": {"A": (5.0, 5.0, 6.0, 6.0)}, "marine": {"B": (-2.0, 3.0, -1.0, 4.0)}}
        self.assertEqual(area_box(["A", "B"], ("zone", "marine"), boxes), (-2.0, 0.0, 1.0, 4.0))
        self.assertEqual(area_box(["A"], ("fire",), boxes), (5.0, 5.0, 6.0, 6.0))
        self.assertIsNone(area_box(["A", "C"], ("zone", "marine"), boxes))  # a code the set lacks
        self.assertIsNone(area_box([], ("zone",), boxes))
        line = {"native": {"event": "Red Flag Warning"}}
        self.assertEqual(nws.area_layers(line), ("fire",))

    def test_area_coded_rows_have_boxes_and_a_build_without_the_file_has_none(self) -> None:
        index = json.loads((EXPECTED / round_directory(FIRST) / "index.json").read_text(encoding="utf-8"))
        jma_rows = [row for row in index["alerts"] if row[0].startswith("jma:")]
        self.assertTrue(jma_rows and all(row[8] is not None for row in jma_rows))
        with tempfile.TemporaryDirectory() as empty:
            self.assertIsNone(load_boxes(NWS_ZONES, Path(empty), None))
            stale = Path(empty) / "alerts-areas" / NWS_ZONES.bbox_filename
            stale.parent.mkdir()
            stale.write_bytes(b"{}")
            self.assertIsNone(load_boxes(NWS_ZONES, Path(empty), None))  # not the pinned CRC32

    def test_the_boundary_tool_refuses_bad_input(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(AlertsProductError):
                areas.build_area_set("nws-zones", Path(empty), "2026-04-16", Path(empty))
            with self.assertRaises(AlertsProductError):
                areas.build_area_set("meteoalarm", Path(empty), "20260416", Path(empty))


if __name__ == "__main__":
    unittest.main()

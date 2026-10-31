"""The alerts product (``xuebuild/alerts``): the three adapters on the
fetched fixture and on constructed input, the category and level tables,
polygon simplification, the snapshot with its ``ended[]`` and carry-over,
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

from tests.prepare_alerts_golden import FIXTURES, ROUNDS, build_rounds, pretty
from xuebuild.airport.fetch import SourceStatus
from xuebuild.alerts import areas, eccc, jma, nws
from xuebuild.alerts.build import build_product, load_previous_index
from xuebuild.alerts.geometry import bbox, simplify
from xuebuild.alerts.networks import NETWORKS, NWS_ZONES
from xuebuild.alerts.schema import (
    CATEGORIES,
    ROW_FIELDS,
    AlertsProductError,
    areas_path,
    build_pointer,
    encode_json,
    floor_round,
    parse_round,
    round_directory,
    validate_alert_line,
    validate_index,
    validate_pointer,
)
from xuebuild.alerts.tables import eccc_category, eccc_level, jma_kind, nws_category, nws_level, tables

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
    def test_collinear_points_go_and_coordinates_keep_four_decimals(self) -> None:
        ring = [[-100.0, 30.0], [-99.5, 30.00001], [-99.0, 30.0], [-99.0, 31.123456], [-100.0, 31.0], [-100.0, 30.0]]
        result = simplify({"type": "Polygon", "coordinates": [ring]})
        self.assertEqual(result["type"], "MultiPolygon")
        self.assertEqual(result["coordinates"], [[[[-100.0, 30.0], [-99.0, 30.0], [-99.0, 31.1235], [-100.0, 31.0], [-100.0, 30.0]]]])
        self.assertEqual(bbox(result), (-100.0, 30.0, -99.0, 31.1235))

    def test_a_sliver_falls_back_to_the_rounded_ring_and_a_tiny_hole_is_dropped(self) -> None:
        sliver = [[0.0, 0.0], [0.0005, 0.0002], [0.001, 0.0], [0.0, 0.0]]
        result = simplify({"type": "Polygon", "coordinates": [sliver]})
        self.assertEqual(result["coordinates"], [[[[0.0, 0.0], [0.0005, 0.0002], [0.001, 0.0], [0.0, 0.0]]]])
        outer = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]
        hole = [[0.5, 0.5], [0.50001, 0.5], [0.5, 0.50001], [0.5, 0.5]]
        result = simplify({"type": "MultiPolygon", "coordinates": [[outer, hole]]})
        self.assertEqual(len(result["coordinates"][0]), 1)

    def test_no_polygon_is_none(self) -> None:
        self.assertIsNone(simplify(None))
        self.assertIsNone(simplify({"type": "Point", "coordinates": [0, 0]}))
        self.assertIsNone(simplify({"type": "Polygon", "coordinates": [[[0, 0], [0, 0], [0, 0], [0, 0]]]}))

    def test_simplified_geometry_validates(self) -> None:
        feature = json.loads((FIXTURES / "alerts.202611010000/eccc/page-001.json").read_text(encoding="utf-8"))["features"][1]
        result = simplify(feature["geometry"])
        line = eccc.convert(feature)
        self.assertEqual(line["geometry"], result)
        self.assertEqual(validate_alert_line(line, "eccc"), bbox(result))
        original = sum(len(ring) for ring in feature["geometry"]["coordinates"])
        self.assertLess(sum(len(ring) for polygon in result["coordinates"] for ring in polygon), original)


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
        self.assertIsNone(nws.convert(self.payload["features"][5]))  # status Test
        cancel = copy.deepcopy(self.payload["features"][2])
        cancel["properties"]["messageType"] = "Cancel"
        cancel["properties"]["references"] = [{"identifier": "urn:oid:x"}]
        self.assertIsNone(nws.convert(cancel))
        self.assertEqual(nws.cancelled_by(cancel), ["nws:urn:oid:x"])

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

    def test_a_lowered_urgent_warning_and_a_lifted_kind_are_cancelled(self) -> None:
        _, lifted, _ = jma.convert(self.reports)
        self.assertIn("jma:150000-49", lifted)  # 危険警報から注意報 on 29
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
            ended = {row[0].split(":", 1)[0] + ":" + row[2] for row in index_of(output, SECOND)["ended"]}
            reasons = {row[0]: row[2] for row in index_of(output, SECOND)["ended"]}
        self.assertEqual(sorted(reasons.values()), ["cancelled", "expired", "expired", "gone"])
        self.assertIn("eccc:expired", ended)
        self.assertEqual(reasons["nws:urn:oid:2.49.0.1.840.0.0e283990ffa56627410f1c41a79337ea2e7e74d6.012.1"], "cancelled")

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
            self.assertFalse(sources["jma-warning"]["ok"])

    def test_networks_and_areas(self) -> None:
        scratch, output = built()
        with scratch:
            index = index_of(output, FIRST)
        self.assertEqual([network["id"] for network in index["networks"]], [network.id for network in NETWORKS])
        self.assertEqual(index["networks"][0]["areas"]["path"], areas_path("nws-zones", NWS_ZONES.version))
        self.assertIsNone(index["networks"][1]["areas"])
        self.assertIsNone(index["networks"][2]["areas"])
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

    def test_rounds_are_five_minutes(self) -> None:
        self.assertEqual(parse_round("202611010005"), datetime(2026, 11, 1, 0, 5, tzinfo=UTC))
        with self.assertRaises(AlertsProductError):
            parse_round("202611010003")
        self.assertEqual(floor_round(datetime(2026, 11, 1, 0, 9, 59, tzinfo=UTC)), datetime(2026, 11, 1, 0, 5, tzinfo=UTC))

    def assertRejected(self, index: dict) -> None:
        with self.assertRaises(AlertsProductError):
            validate_index(index)

    def test_malformed_indexes_are_rejected(self) -> None:
        unsorted = copy.deepcopy(self.index)
        unsorted["alerts"].reverse()
        self.assertRejected(unsorted)
        category = copy.deepcopy(self.index)
        category["alerts"][0][2] = "weather"
        self.assertRejected(category)
        level = copy.deepcopy(self.index)
        level["alerts"][0][3] = 6
        self.assertRejected(level)
        span = copy.deepcopy(self.index)
        span["alerts"][0][-2] = 10**6
        self.assertRejected(span)
        ended = copy.deepcopy(self.index)
        ended["ended"] = [[self.index["alerts"][0][0], self.index["alerts"][0][1], "gone"]]
        self.assertRejected(ended)
        reason = copy.deepcopy(self.index)
        reason["ended"][0][2] = "downgraded"
        self.assertRejected(reason)
        area = copy.deepcopy(self.index)
        area["networks"][0]["areas"]["path"] = "alerts-areas/nws-zones.pmtiles"
        self.assertRejected(area)
        foreign = copy.deepcopy(self.index)
        foreign["alerts"][0][1] = 0  # an eccc id under nws
        self.assertRejected(foreign)

    def test_malformed_lines_are_rejected(self) -> None:
        line = json.loads((EXPECTED / round_directory(SECOND) / "eccc.jsonl").read_bytes().splitlines()[0])
        validate_alert_line(line, "line")
        for change in (
            {"status": "test"},
            {"network": "nws"},
            {"texts": {"en": {"headline": 3}}},
            {"areas": ["A", "A"]},
            {"geometry": {"type": "MultiPolygon", "coordinates": [[[[0.12345, 0.0], [1.0, 0.0], [1.0, 1.0], [0.12345, 0.0]]]]}},
            {"geometry": {"type": "MultiPolygon", "coordinates": [[[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]]]}},
        ):
            broken = line | change
            with self.assertRaises(AlertsProductError, msg=str(change)):
                validate_alert_line(broken, "line")

    def test_pointer(self) -> None:
        index_bytes = encode_json(self.index)
        pointer = build_pointer(SECOND, f"{round_directory(SECOND)}/index.json", index_bytes)
        validate_pointer(pointer)
        with self.assertRaises(AlertsProductError):
            validate_pointer(pointer | {"path": f"{round_directory(FIRST)}/index.json"})


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

    def test_the_boundary_tool_refuses_bad_input(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(AlertsProductError):
                areas.build_area_set("nws-zones", Path(empty), "2026-04-16", Path(empty))
            with self.assertRaises(AlertsProductError):
                areas.build_area_set("meteoalarm", Path(empty), "20260416", Path(empty))


if __name__ == "__main__":
    unittest.main()

"""The indicators product as written: the golden build, idempotence and
``--force`` reproduction, source isolation, the validators, the words that
never appear, the serialisation, and the context file."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from tests.prepare_indicators_golden import EXPECTED, RUN, STORE_ROOT, build_fixture, serve
from xuebuild.indicators import build as build_module
from xuebuild.indicators.context import ONI_PATH, parse_oni, update_context
from xuebuild.indicators.regions import REGION_IDS
from xuebuild.indicators.schema import (
    IndicatorsError,
    check_vocabulary,
    encode_json,
    forbidden_in,
    validate_context,
    validate_index,
    validate_line,
    validate_season,
)

REPOSITORY = Path(__file__).resolve().parents[1]
MONTH = "features/gfs/2026-08.jsonl"
SEASON = "season/us-ia.2026.json"
ONI_SAMPLE = """ SEAS  YR   TOTAL   ANOM
   DJF 1950  24.72  -1.53
   JFM 1950  25.17  -1.34
   FMA 1950  25.75  -1.16
"""


def expected_files() -> dict[str, bytes]:
    return {str(path.relative_to(EXPECTED)): path.read_bytes() for path in sorted(EXPECTED.rglob("*")) if path.is_file()}


class BuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix="xue-indicators-build-"))
        self.output = self.scratch / "indicators"

    def tearDown(self) -> None:
        shutil.rmtree(self.scratch)

    def written(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.output)): path.read_bytes() for path in sorted(self.output.rglob("*")) if path.is_file()}

    def test_the_build_matches_the_golden(self) -> None:
        report = build_fixture(self.output)
        self.assertEqual(self.written(), expected_files())
        self.assertEqual([source["status"] for source in report["sources"]], ["added"])
        self.assertEqual(report["written"], [MONTH, SEASON, "index.json"])

    def test_a_second_build_changes_nothing_and_force_reproduces_the_line(self) -> None:
        build_fixture(self.output)
        before = self.written()
        again = build_fixture(self.output)
        self.assertEqual((again["sources"][0]["status"], again["written"], again["index"]["changed"]), ("skipped", [], False))
        forced = build_fixture(self.output, force=True)
        self.assertEqual(forced["sources"][0]["status"], "reproduced")
        self.assertEqual(forced["written"], [])
        self.assertEqual(self.written(), before)

    def test_force_refuses_a_line_that_would_change(self) -> None:
        build_fixture(self.output)
        index = json.loads((self.output / "index.json").read_text())
        month = self.output / MONTH
        tampered = month.read_bytes().replace(b'"t2m_mean":25.46', b'"t2m_mean":25.47')
        month.write_bytes(tampered)
        # Keep the index consistent with the tampered file, so the
        # reproduction check is what refuses it.
        entry = next(item for item in index["files"] if item["path"] == MONTH)
        entry["crc32"] = build_module.crc32_hex(tampered)
        (self.output / "index.json").write_bytes(encode_json(index))
        report = build_fixture(self.output, force=True, dry_run=True)
        self.assertEqual(report["sources"][0]["status"], "failed")
        self.assertIn("different bytes", report["sources"][0]["error"])

    def test_a_month_file_unlike_its_index_entry_is_not_appended_to(self) -> None:
        build_fixture(self.output)
        # Rename the indexed run, so the fixture run is new again and the
        # build has to append to a file the index already describes.
        index = json.loads((self.output / "index.json").read_text())
        index["files"][0]["runs"][0]["run"] = "2026081500"
        (self.output / "index.json").write_bytes(encode_json(index))
        month = self.output / MONTH
        original = month.read_bytes()
        month.write_bytes(original + b"\n")
        report = build_fixture(self.output, dry_run=True)
        self.assertEqual(report["sources"][0]["status"], "failed")
        self.assertIn("pull it again", report["sources"][0]["error"])
        month.unlink()
        report = build_fixture(self.output, dry_run=True)
        self.assertIn("pull it before appending", report["sources"][0]["error"])
        month.write_bytes(original)
        report = build_fixture(self.output)
        self.assertEqual((report["sources"][0]["status"], report["sources"][0]["offset"]), ("added", len(original)))

    def test_dry_run_writes_nothing(self) -> None:
        report = build_fixture(self.output, dry_run=True)
        self.assertEqual(report["sources"][0]["status"], "added")
        self.assertFalse(self.output.exists())

    def test_a_failing_source_does_not_stop_the_others(self) -> None:
        from tests.prepare_indicators_golden import GRID_ID, WEIGHTS  # noqa: PLC0415

        weights_dir = self.scratch / "weights"
        shutil.copytree(WEIGHTS, weights_dir / f"{GRID_ID}.zarr")
        with serve(STORE_ROOT) as (base_url, _requests):
            report = build_module.build(
                self.output,
                sources=("gfs", "ecmwf"),
                base_url=base_url,
                weights_dir=weights_dir,
                source_grids={"gfs": GRID_ID},
            )
        statuses = {source["id"]: source["status"] for source in report["sources"]}
        self.assertEqual(statuses, {"gfs": "added", "ecmwf": "failed"})
        index = json.loads((self.output / "index.json").read_text())
        self.assertEqual(
            [(source["id"], source["ok"], source["latestRun"]) for source in index["sources"]],
            [("gfs", True, RUN), ("ecmwf", False, None)],
        )
        self.assertIn("HTTP 404", index["sources"][1]["error"])

    def test_a_named_round_reads_that_run(self) -> None:
        report = build_fixture(self.output, round_run=RUN)
        self.assertEqual(report["sources"][0]["status"], "added")
        line = json.loads((self.output / MONTH).read_text())
        self.assertEqual(line["manifest"], {"path": f"gfs.{RUN}/manifest.json", "crc32": None})
        with self.assertRaises(IndicatorsError):
            build_module.parse_round("2026-08-15")

    def test_one_run_is_one_range_of_its_month_file(self) -> None:
        build_fixture(self.output)
        index = json.loads((self.output / "index.json").read_text())
        entry = next(item for item in index["files"] if item["path"] == MONTH)
        run = entry["runs"][0]
        with serve(self.output) as (base_url, _requests):
            from xuebuild.indicators.store import http_fetch  # noqa: PLC0415

            span = http_fetch(f"{base_url}{MONTH}?v={entry['crc32']}", (run["offset"], run["offset"] + run["length"] - 1))
        self.assertEqual(json.loads(span)["run"], RUN)
        self.assertTrue(span.endswith(b"\n"))


class GoldenSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = expected_files()
        cls.line_bytes = cls.files[MONTH]
        cls.line = json.loads(cls.line_bytes)
        cls.index = json.loads(cls.files["index.json"])
        cls.season = json.loads(cls.files[SEASON])

    def test_the_golden_passes_its_validators(self) -> None:
        validate_line(self.line, REGION_IDS)
        validate_index(self.index)
        validate_season(self.season)

    def test_encode_json_is_stable(self) -> None:
        self.assertEqual(encode_json(json.loads(self.line_bytes)) + b"\n", self.line_bytes)
        self.assertEqual(encode_json(json.loads(self.files["index.json"])), self.files["index.json"])
        self.assertEqual(self.line_bytes.count(b"\n"), 1)
        self.assertTrue(self.line_bytes.isascii())

    def test_the_index_describes_its_files(self) -> None:
        for entry in self.index["files"]:
            data = self.files[entry["path"]]
            self.assertEqual((entry["byteLength"], entry["crc32"]), (len(data), build_module.crc32_hex(data)))

    def mutated(self, change) -> dict:
        line = json.loads(self.line_bytes)
        change(line)
        return line

    def test_the_line_validator_refuses(self) -> None:
        day = lambda line: line["regions"]["us-ia"]["days"][0]  # noqa: E731
        cases = {
            "out of range": lambda line: day(line).update(precip=600.0),
            "order": lambda line: day(line).update(t2m_min=40.0),
            "fraction": lambda line: day(line).update(hot35_frac=1.2),
            "extra key": lambda line: line.update(note="x"),
            "unknown region": lambda line: line["regions"].update({"xx-yy": line["regions"]["us-ia"]}),
            "dates": lambda line: line["regions"]["us-ia"]["days"].reverse(),
            "flag": lambda line: day(line).update(complete=1),
            "nan": lambda line: day(line).update(gdd=float("nan")),
        }
        for name, change in cases.items():
            with self.subTest(name), self.assertRaises(IndicatorsError):
                validate_line(self.mutated(change), REGION_IDS)

    def test_the_index_validator_refuses_a_gap_in_the_runs(self) -> None:
        index = json.loads(self.files["index.json"])
        index["files"][0]["runs"][0]["length"] -= 1
        with self.assertRaises(IndicatorsError):
            validate_index(index)


class VocabularyTests(unittest.TestCase):
    def test_words_are_matched_whole(self) -> None:
        self.assertEqual(forbidden_in("alertLevel"), ["alert"])
        self.assertEqual(forbidden_in("Short-range"), ["short"])
        self.assertEqual(forbidden_in("price_index"), ["price"])
        self.assertEqual(forbidden_in("contracts"), ["contracts"])
        self.assertEqual(forbidden_in("watches"), ["watches"])
        for clean in ("longitude", "hot35_days", "byteLength", "precip", "warm", "shorten"):
            self.assertEqual(forbidden_in(clean), [], clean)

    def test_keys_and_string_values_are_checked(self) -> None:
        with self.assertRaisesRegex(IndicatorsError, "signal"):
            check_vocabulary({"a": [{"b": "a buy signal"}]})
        with self.assertRaisesRegex(IndicatorsError, "bear"):
            check_vocabulary({"bearMarket": 1})
        check_vocabulary({"note": "area-weighted daily precipitation", "value": 3})

    def test_nothing_published_uses_them(self) -> None:
        for path, data in expected_files().items():
            with self.subTest(path):
                check_vocabulary(json.loads(data.splitlines()[0] if path.endswith(".jsonl") else data))
        check_vocabulary(
            {
                "note": build_module.NOTE,
                "attribution": list(build_module.ATTRIBUTION),
                "features": list(build_module.FEATURES),
            }
        )

    def test_the_spec_lists_every_field(self) -> None:
        spec = (REPOSITORY / "docs" / "indicators.md").read_text(encoding="utf-8")
        for feature in build_module.FEATURES:
            self.assertIn(f"| `{feature['id']}` |", spec, feature["id"])
        for word in ("signal", "alert", "warning", "watch", "price", "contract", "ticker"):
            self.assertIn(f"`{word}`", spec)


class ContextTests(unittest.TestCase):
    def test_the_table_parses(self) -> None:
        rows = parse_oni(ONI_SAMPLE)
        self.assertEqual(rows[0], {"season": "DJF", "year": 1950, "total": 24.72, "anomaly": -1.53})
        with self.assertRaises(IndicatorsError):
            parse_oni("SEAS YR\nDJF 1950 1 2\n")

    def test_an_unchanged_table_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            first = update_context(root, datetime(2026, 10, 8, 1, tzinfo=UTC), text=ONI_SAMPLE)
            data = (root / ONI_PATH).read_bytes()
            again = update_context(root, datetime(2026, 10, 8, 2, tzinfo=UTC), text=ONI_SAMPLE)
            self.assertEqual((first["changed"], again["changed"]), (True, False))
            self.assertEqual((root / ONI_PATH).read_bytes(), data)
            revised = update_context(
                root, datetime(2026, 11, 8, tzinfo=UTC), text=ONI_SAMPLE + "   MAM 1950  26.12  -1.18\n"
            )
            payload = json.loads((root / ONI_PATH).read_text())
            validate_context(payload)
            self.assertEqual((revised["rows"], len(payload["fetches"])), (4, 2))
            self.assertEqual(payload["fetches"][0]["fetched"], "2026-10-08T01:00:00Z")


if __name__ == "__main__":
    unittest.main()

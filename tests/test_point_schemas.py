"""Every point product's committed golden matches its normative JSON Schema
(``docs/schemas/``), and every schema is a valid draft 2020-12 document that
some golden exercises. The goldens are what the writers produce
(``tests/prepare_<product>_golden.py``) and what the shell's and the API
worker's tests read, so this is where the three meet the one contract."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.pointschemas import SCHEMAS, check, check_lines, validator

FIXTURES = Path(__file__).resolve().parent / "fixtures"

JSON = "json"
LINES = "lines"

GOLDENS: dict[str, tuple[tuple[str, str], ...]] = {
    "pointer": ((JSON, "*/expected/latest-*.json"),),
    "fire-index": ((JSON, "fire/expected/fire.*/index.json"),),
    "fire-line": ((LINES, "fire/expected/fire.*/*.jsonl"),),
    "aq-index": ((JSON, "aq/expected/aq/*/*/*/aq.*/index.json"),),
    "aq-stations": ((JSON, "aq/expected/aq/*/*/*/aq.*/*.stations.json"),),
    "aq-line": ((LINES, "aq/expected/aq/*/*/*/aq.*/*.jsonl"),),
    "alerts-index": ((JSON, "alerts/expected/alerts/*/*/*/alerts.*/index.json"),),
    "alerts-line": ((LINES, "alerts/expected/alerts/*/*/*/alerts.*/*.jsonl"),),
    "tc-index": ((JSON, "tc/expected/index.json"),),
    "tc-storm": ((JSON, "tc/expected/[A-Zx]*.json"),),
    "sounding-index": ((JSON, "sounding/expected/index.json"),),
    "sounding-line": ((LINES, "sounding/expected/soundings.jsonl"),),
}
"""Schema → the golden files it holds: whole JSON documents, or JSONL files
whose every line is one document."""


class SchemaTest(unittest.TestCase):
    def test_every_schema_is_valid_and_has_goldens(self) -> None:
        names = sorted(path.name.removesuffix(".schema.json") for path in SCHEMAS.glob("*.schema.json"))
        self.assertEqual(names, sorted(GOLDENS))
        for name in names:
            with self.subTest(name):
                validator(name)
                schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertTrue(schema["$id"].endswith(f"/docs/schemas/{name}.schema.json"))

    def test_the_goldens_match(self) -> None:
        for name, globs in GOLDENS.items():
            documents = 0
            for kind, pattern in globs:
                for path in sorted(FIXTURES.glob(pattern)):
                    with self.subTest(name=name, path=str(path.relative_to(FIXTURES))):
                        if kind == JSON:
                            check(name, json.loads(path.read_text(encoding="utf-8")), str(path.relative_to(FIXTURES)))
                            documents += 1
                        else:
                            documents += check_lines(name, path)
            self.assertGreater(documents, 0, f"no golden exercises {name}")


if __name__ == "__main__":
    unittest.main()

"""docs/README.md's version table agrees with the constants the code writes."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from xuebuild import binconvert, binformat, metrics
from xuebuild.indicators import schema as indicators_schema

README = Path(__file__).resolve().parents[1] / "docs" / "README.md"


def version_rows() -> dict[str, tuple[str, str]]:
    """Artifact name -> (written, read) cells of the `## Versions` table."""
    text = README.read_text(encoding="utf-8")
    section = text.split("## Versions", 1)[1].split("\n## ", 1)[0]
    rows = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and not set(cells[0]) <= {"-", " "} and cells[0] != "Artifact":
            rows[cells[0]] = (cells[1], cells[2])
    return rows


def first_integer(cell: str) -> int:
    found = re.search(r"\d+", cell)
    assert found, cell
    return int(found.group())


class VersionTableCase(unittest.TestCase):
    def row(self, prefix: str) -> tuple[str, str]:
        rows = version_rows()
        matches = [cells for name, cells in rows.items() if name.startswith(prefix)]
        self.assertEqual(len(matches), 1, f"{prefix!r} must be one row of the table: {sorted(rows)}")
        return matches[0]

    def test_bundle_metadata(self) -> None:
        written, read = self.row("Bundle metadata")
        self.assertEqual(first_integer(written), binconvert.METADATA_SCHEMA_VERSION)
        self.assertEqual(first_integer(read.split("to")[-1]), binconvert.METADATA_SCHEMA_VERSION)

    def test_container(self) -> None:
        written, read = self.row("Container")
        self.assertEqual(first_integer(written), binformat.VERSION_V2)
        self.assertEqual(sorted(int(n) for n in re.findall(r"\d+", read)), sorted(binformat.CONTAINER_VERSIONS))

    def test_indicators(self) -> None:
        written, _ = self.row("Indicators")
        self.assertEqual(first_integer(written), indicators_schema.SCHEMA_VERSION)

    def test_run_metrics(self) -> None:
        written, _ = self.row("Run metrics")
        self.assertEqual(first_integer(written), metrics.SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()

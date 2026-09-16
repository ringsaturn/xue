"""Regenerate the radiosonde sounding product golden from the fetched
fixture directory.

``tests/fixtures/sounding/sounding.2026091402/`` is one issue hour's raw
directory as ``xue sounding-build`` fetches it: the JMA gateway with three
bulletins verbatim off the WIS2 Global Cache — JMA's five Japanese
stations, the US centre's Guam ascent (a ``CCA`` correction, 219 levels)
and China's thirteen stations with their native WIGOS identifiers — and
the DWD gateway with a ``fetch.json`` saying its listing failed, so the
golden carries the one-source-down path. This script builds the product
from it, offline, with a fixed generation time, and writes the result
under ``tests/fixtures/sounding/expected/`` for
``tests/test_sounding.py`` to hold the build to — the index
pretty-printed, ``soundings.jsonl`` exactly as it is published.

Run it after a deliberate change to the parser, the derived quantities or
the schema, and commit the diff with the change::

    .venv/bin/python tests/prepare_sounding_golden.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from xuebuild.sounding.build import build_product
from xuebuild.sounding.schema import parse_issue

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sounding"
ISSUE = "2026091402"
GENERATED = datetime(2026, 9, 14, 2, 5, tzinfo=UTC)


def pretty_index(value: object, indent: int = 0) -> str:
    """JSON with objects and lists of objects one entry per line and lists
    of scalars — the level arrays — kept on one line, so the golden diffs
    by field rather than by level."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f'{pad} "{key}": {pretty_index(item, indent + 1)}' for key, item in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(not isinstance(item, (dict, list)) for item in value):
            return "[" + ", ".join(json.dumps(item) for item in value) + "]"
        items = [f"{pad} {pretty_index(item, indent + 1)}" for item in value]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return json.dumps(value, ensure_ascii=False)


def build_expected(destination: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch)
        report = build_product(parse_issue(ISSUE), FIXTURES, output, force=True, now=GENERATED)
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        directory = output / f"sounding.{ISSUE}"
        # The index is pretty-printed so the golden diffs by field; the
        # soundings file is committed exactly as published, one line per
        # station, which is diff-friendly already and is the thing a
        # reader's byte offsets point into.
        index = json.loads((directory / "index.json").read_bytes())
        (destination / "index.json").write_text(pretty_index(index) + "\n")
        shutil.copyfile(directory / "soundings.jsonl", destination / "soundings.jsonl")
        pointer = json.loads((output / "latest-sounding.json").read_bytes())
        (destination / "latest-sounding.json").write_text(pretty_index(pointer) + "\n")
    return report


if __name__ == "__main__":
    report = build_expected(FIXTURES / "expected")
    print(json.dumps({key: report[key] for key in ("stations", "fresh", "copied", "soundings", "byteLength")}, indent=2))

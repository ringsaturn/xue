"""Regenerate the tropical cyclone product golden from the fetched
fixture directory.

``tests/fixtures/tc/tc.2026091206/`` is one issue hour's raw directory as
``xue tc-build`` fetches it — JTWC's warning and alert, NHC's decks and
storm list, the NCEP tracker files, ECMWF's ``tf`` BUFR cropped to one
system each with ``bufr_filter``, IBTrACS' active list cut to two storms —
with the ``fetch.json`` each source leaves. This script builds the product
from it, offline, with a fixed generation time, and writes the result
pretty-printed under ``tests/fixtures/tc/expected/`` for
``tests/test_tc.py`` to hold the build to.

Run it after a deliberate change to a parser, the identity rules or the
schema, and commit the diff with the change::

    .venv/bin/python tests/prepare_tc_golden.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from xuebuild.tc.build import build_product
from xuebuild.tc.schema import parse_issue

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "tc"
ISSUE = "2026091206"
GENERATED = datetime(2026, 9, 12, 7, 5, tzinfo=UTC)


def pretty(value: object, indent: int = 0) -> str:
    """JSON with objects and lists of objects one entry per line and lists
    of scalars — the ensembles' fixed-point arrays — kept on one line, so
    the golden diffs by field rather than by array slot."""
    pad = " " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f'{pad} "{key}": {pretty(item, indent + 1)}' for key, item in value.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(value, list):
        if not value:
            return "[]"
        if all(not isinstance(item, (dict, list)) for item in value):
            return "[" + ", ".join(json.dumps(item) for item in value) + "]"
        items = [f"{pad} {pretty(item, indent + 1)}" for item in value]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return json.dumps(value, ensure_ascii=False)


def build_expected(destination: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch)
        report = build_product(parse_issue(ISSUE), FIXTURES, output, force=True, now=GENERATED)
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        for path in sorted((output / f"tc.{ISSUE}").iterdir()):
            payload = json.loads(path.read_bytes())
            (destination / path.name).write_text(pretty(payload) + "\n")
        pointer = json.loads((output / "latest-tc.json").read_bytes())
        (destination / "latest-tc.json").write_text(pretty(pointer) + "\n")
    return report


if __name__ == "__main__":
    report = build_expected(FIXTURES / "expected")
    print(json.dumps({"storms": report["storms"], "pointer": report["pointer"] is not None}, indent=2))

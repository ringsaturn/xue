"""Regenerate the synop product golden from the fetched fixture
directories.

``tests/fixtures/synop/synop.202610050000/`` and ``…0010/`` are two
consecutive rounds' raw directories as ``xue synop-build`` fetches them —
JMA AMeDAS map snapshots cut to fifteen stations (a summit, a remote
island, a precipitation-only gauge, a station the trimmed table does not
place, cells with quasi-normal and missing quality codes), each round's
``latest_time.txt`` and ``fetch.json`` — beside the station table they
share. This script builds the first round, then the second on top of it,
offline and with fixed generation times, and writes both under
``tests/fixtures/synop/expected/`` for ``tests/test_synop.py`` to hold the
build to: the index and the STAC documents pretty-printed, and each
network file exactly as published, one station per line.

Run it after a deliberate change to an adapter, the merge or the schema,
and commit the diff with the change::

    .venv/bin/python tests/prepare_synop_golden.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from xuebuild.synop.build import build_product, load_previous_index
from xuebuild.synop.schema import parse_round, round_directory

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "synop"
ROUNDS = ("202610050000", "202610050010")
GENERATED = (datetime(2026, 10, 5, 0, 5, 30, tzinfo=UTC), datetime(2026, 10, 5, 0, 15, 30, tzinfo=UTC))


def pretty(value: object, indent: int = 0) -> str:
    """JSON with objects and lists of objects one entry per line and lists
    of scalars — an index row — kept on one line, so the golden diffs by
    field rather than by array slot."""
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
            return "[" + ", ".join(json.dumps(item, ensure_ascii=False) for item in value) + "]"
        items = [f"{pad} {pretty(item, indent + 1)}" for item in value]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return json.dumps(value, ensure_ascii=False)


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(pretty(json.loads(source.read_bytes())) + "\n")


def build_rounds(output: Path) -> list[dict[str, object]]:
    """Both rounds into ``output``, the second merged onto the first."""
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


def build_expected(destination: Path) -> list[dict[str, object]]:
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch)
        reports = build_rounds(output)
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        for name in ROUNDS:
            directory = round_directory(parse_round(name))
            index = json.loads((output / directory / "index.json").read_bytes())
            _copy(output / directory / "index.json", destination / directory / "index.json")
            for network in index["networks"]:
                path = network["file"]["path"]
                (destination / directory / path).write_bytes((output / directory / path).read_bytes())
            _copy(output / directory / "item.json", destination / directory / "item.json")
        _copy(output / "synop" / "collection.json", destination / "synop" / "collection.json")
        _copy(output / "synop" / "item.json", destination / "synop" / "item.json")
        _copy(output / "latest-synop.json", destination / "latest-synop.json")
    return reports


if __name__ == "__main__":
    reports = build_expected(FIXTURES / "expected")
    print(json.dumps([{key: report[key] for key in ("round", "stations", "observations", "networks")} for report in reports], indent=2))

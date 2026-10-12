"""Regenerate the fire product golden from the fetched fixture
directories.

``tests/fixtures/fire/fire.202610120400/`` and ``…0500/`` are two
consecutive rounds' raw directories as ``xue fire-build`` fetches them —
the four FIRMS last-24-hour files of 2026-10-12 cut to a few dozen rows
each, mostly from the 46–50°N, 110–124°E box of the Arxan border fire,
with both MODIS platforms, a NOAA-20 detection older than the second
round's window, one present in both rounds with a revised FRP, and three
rows the reader must reject — and each round's ``fetch.json``. This script
builds the first round, then the second on top of it, offline and with
fixed generation times, and writes both under
``tests/fixtures/fire/expected/`` for ``tests/test_fire.py`` to hold the
build to: the index and the STAC documents pretty-printed, and each sensor
file exactly as published, one hour per line.

Run it after a deliberate change to the reader, the merge or the schema,
and commit the diff with the change::

    .venv/bin/python tests/prepare_fire_golden.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from xuebuild.fire.build import build_product, load_previous_index
from xuebuild.fire.schema import parse_round, round_directory

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "fire"
ROUNDS = ("202610120400", "202610120500")
GENERATED = (datetime(2026, 10, 12, 4, 23, 40, tzinfo=UTC), datetime(2026, 10, 12, 5, 23, 40, tzinfo=UTC))


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
            for sensor in index["sensors"]:
                path = sensor["file"]["path"]
                (destination / directory / path).write_bytes((output / directory / path).read_bytes())
            _copy(output / directory / "item.json", destination / directory / "item.json")
        _copy(output / "fire" / "collection.json", destination / "fire" / "collection.json")
        _copy(output / "fire" / "item.json", destination / "fire" / "item.json")
        _copy(output / "latest-fire.json", destination / "latest-fire.json")
    return reports


if __name__ == "__main__":
    reports = build_expected(FIXTURES / "expected")
    print(json.dumps([{key: report[key] for key in ("round", "detections", "sensors", "sources")} for report in reports], indent=2))

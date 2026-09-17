"""Regenerate the airport product golden from the fetched fixture
directories.

``tests/fixtures/airport/airport.202609161430/`` and
``…1440/`` are two consecutive rounds' raw directories as ``xue
airport-build`` fetches them — the Aviation Weather Center's decoded METAR
CSV and TAF XML, cut to a couple of hundred stations across the world,
with the ``fetch.json`` a fetch leaves — beside the station table the two
rounds share. This script builds the first round, then the second on top
of it, offline and with fixed generation times, and writes both under
``tests/fixtures/airport/expected/`` for ``tests/test_airport.py`` to hold
the build to: the index and the STAC documents pretty-printed, and
``history.jsonl`` exactly as published, which is already one line per
station and diffs that way. The root ``catalog.json`` the build also
writes is a function of the source registry rather than of this product
and is pinned by ``tests/test_stac.py`` instead.

The second round is what makes the golden worth having: it carries the
merge (a station's new observation joins its history, newest first), the
stations that appear between rounds, and the byte spans the index records
over a history file that has grown.

Run it after a deliberate change to a parser, the merge or the schema, and
commit the diff with the change::

    .venv/bin/python tests/prepare_airport_golden.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from xuebuild.airport.build import build_product, load_previous_index
from xuebuild.airport.schema import HISTORY_FILENAME, parse_round, round_directory

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "airport"
ROUNDS = ("202609161430", "202609161440")
GENERATED = (datetime(2026, 9, 16, 14, 36, 20, tzinfo=UTC), datetime(2026, 9, 16, 14, 46, 20, tzinfo=UTC))


def pretty(value: object, indent: int = 0) -> str:
    """JSON with objects and lists of objects one entry per line and lists
    of scalars — a station's compact index row, a cloud layer — kept on one
    line, so the golden diffs by field rather than by array slot."""
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


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(pretty(json.loads(source.read_bytes())) + "\n")


def build_expected(destination: Path) -> list[dict[str, object]]:
    reports = []
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch)
        for name, generated in zip(ROUNDS, GENERATED):
            moment = parse_round(name)
            reports.append(
                build_product(
                    moment,
                    FIXTURES,
                    output,
                    previous_index=load_previous_index(None, output),
                    force=True,
                    now=generated,
                )
            )
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        for name in ROUNDS:
            directory = round_directory(parse_round(name))
            _copy(output / directory / "index.json", destination / directory / "index.json")
            # The history file is published as it is: one station per
            # line, so a diff of the golden is a diff of the stations.
            (destination / directory / HISTORY_FILENAME).write_bytes(
                (output / directory / HISTORY_FILENAME).read_bytes()
            )
            # The round's STAC Item, written beside the index by the build
            # and a pure function of it (docs/stac.md "Point products").
            _copy(output / directory / "item.json", destination / directory / "item.json")
        # The product's Collection and its live Item, which the newest
        # round leaves at the stable path the pointer's readers follow.
        _copy(output / "airport" / "collection.json", destination / "airport" / "collection.json")
        _copy(output / "airport" / "item.json", destination / "airport" / "item.json")
        _copy(output / "latest-airport.json", destination / "latest-airport.json")
    return reports


if __name__ == "__main__":
    reports = build_expected(FIXTURES / "expected")
    print(json.dumps([{key: report[key] for key in ("round", "stations", "reports", "history")} for report in reports], indent=2))

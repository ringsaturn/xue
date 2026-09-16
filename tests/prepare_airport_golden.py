"""Regenerate the airport product golden from the fetched fixture
directories.

``tests/fixtures/airport/airport.202609161430/`` and
``…1440/`` are two consecutive rounds' raw directories as ``xue
airport-build`` fetches them — the Aviation Weather Center's decoded METAR
CSV and TAF XML, cut to a set of stations that covers the sharding rule
(``RJ``, ``ZB``, ``LF``, the ``K``-by-three block ``KAB`` and ``KSR``), with
the ``fetch.json`` a fetch leaves — beside the station table the two rounds
share. This script builds the first round, then the second on top of it,
offline and with fixed generation times, and writes both pretty-printed
under ``tests/fixtures/airport/expected/`` for ``tests/test_airport.py`` to
hold the build to.

The second round is what makes the golden worth having: it carries the
merge (a station's new observation joins its history, newest first), the
stations that appear between rounds, and the content-addressed shards —
the shards whose stations did not report again keep their names and are
not written, and the index names the files that were already there.

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
from xuebuild.airport.schema import SHARD_DIRECTORY, parse_round, round_directory

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
        for shard in sorted((output / SHARD_DIRECTORY).iterdir()):
            _copy(shard, destination / SHARD_DIRECTORY / shard.name)
        _copy(output / "latest-airport.json", destination / "latest-airport.json")
    return reports


if __name__ == "__main__":
    reports = build_expected(FIXTURES / "expected")
    print(json.dumps([{key: report[key] for key in ("round", "stations", "reports", "shards")} for report in reports], indent=2))

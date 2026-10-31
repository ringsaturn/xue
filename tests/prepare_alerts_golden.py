"""Regenerate the alerts product golden from the fetched fixture
directories.

``tests/fixtures/alerts/alerts.202611010000/`` and ``…0005/`` are two
consecutive rounds' raw directories as ``xue alerts-build`` fetches them,
cut from recorded upstream answers with their times moved onto the two
rounds:

- ``nws/active.json`` — round one: a flood warning with a polygon that
  expires between the rounds, a small craft advisory (zones only), a gale
  warning, a special weather statement, a tornado warning marked CAP
  severity Extreme (a recorded flood warning's polygon under a tornado
  warning's event, codes and VTEC) and a test message. Round two: the
  expired flood warning still listed, the advisory and the tornado
  warning, a ``Cancel`` of the gale warning, a new winter storm watch, and
  no special weather statement.
- ``jma/`` — round one: ``map_time.json`` and four ``map.json`` reports
  cut to a few areas (Wakkanai's thunder and snow accretion advisories,
  Niigata's landslide 危険警報 lowered to 注意報, a lifted storm surge
  危険警報, and a copy of Wakkanai's report under an office name the table
  does not know). Round two: the same ``map_time.json`` fetched 32 minutes
  after it — unchanged, so the network carries over as it stands.
- ``eccc/page-001.json`` — round one: a fog advisory that expires between
  the rounds, a wind warning and an ended snowfall warning. Round two's
  fetch failed (``fetch.json``), so the network carries over and loses the
  fog advisory to its expiry.

This script builds the first round, then the second on top of it, offline
and with fixed generation times, and writes both under
``tests/fixtures/alerts/expected/`` for ``tests/test_alerts.py`` to hold
the build to: the index pretty-printed, and each network file exactly as
published, one alert per line.

Run it after a deliberate change to an adapter, a table or the schema, and
commit the diff with the change::

    .venv/bin/python tests/prepare_alerts_golden.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from xuebuild.alerts.build import build_product, load_previous_index
from xuebuild.alerts.schema import parse_round, round_directory

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "alerts"
ROUNDS = ("202611010000", "202611010005")
GENERATED = (datetime(2026, 11, 1, 0, 1, 40, tzinfo=UTC), datetime(2026, 11, 1, 0, 12, 10, tzinfo=UTC))


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
    destination.write_text(pretty(json.loads(source.read_bytes())) + "\n", encoding="utf-8")


def build_rounds(output: Path) -> list[dict[str, object]]:
    """Both rounds into ``output``, the second against the first."""
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
        _copy(output / "latest-alerts.json", destination / "latest-alerts.json")
    return reports


if __name__ == "__main__":
    reports = build_expected(FIXTURES / "expected")
    print(json.dumps([{key: report[key] for key in ("round", "alerts", "ended", "carried", "networks")} for report in reports], indent=2))

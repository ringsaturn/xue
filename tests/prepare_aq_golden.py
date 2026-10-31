"""Regenerate the aq product golden from the fetched fixture directories.

``tests/fixtures/aq/aq.202610311515/`` and ``…1545/`` are two consecutive
rounds' raw directories as ``xue aq-build`` fetches them, recorded on
2026-10-31 and cut down:

- AirNow ``HourlyAQObs`` files trimmed to ten sites (a Canadian ozone-only
  site, a multi-pollutant one, a Québec site with an accented name, a US
  site with CO, one with a small negative PM2.5, the Baku embassy post with
  a twelve-character AQSID and no elevation, a site at 0°, 0°, and a
  Japanese site AirNow lists without data). The first round read hours
  12 and 13 UTC (14 was not posted yet); the second reads 12 and 13 again,
  as AirNow rewrote them, and 14. In the second round's copy of hour 12
  Los Angeles – N. Main Street's PM2.5 was edited by hand to 99.9 so the
  rewrite visibly replaces the first round's value.
- The EPD ``24pc_Eng.xml`` trimmed to four stations (Causeway Bay roadside,
  Central/Western, Tap Mun, Tung Chung), plus one element copied under the
  made-up name ``Unlisted Station``, which the station table does not
  place. In the second round the EPD fetch failed (``fetch.json`` says so
  and there is no file), so the network is carried over from the first.

This script builds the first round, then the second on top of it, offline
and with fixed generation times, and writes both under
``tests/fixtures/aq/expected/`` for ``tests/test_aq.py`` to hold the build
to: the index and the stations files pretty-printed, and each network file
exactly as published, one station per line. The STAC documents are copied
too once the build writes them.

Run it after a deliberate change to an adapter, the merge or the schema,
and commit the diff with the change::

    .venv/bin/python tests/prepare_aq_golden.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from xuebuild.aq.build import build_product, load_previous_index
from xuebuild.aq.schema import parse_round, round_directory

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "aq"
ROUNDS = ("202610311515", "202610311545")
GENERATED = (datetime(2026, 10, 31, 15, 16, 50, tzinfo=UTC), datetime(2026, 10, 31, 15, 46, 50, tzinfo=UTC))


def pretty(value: object, indent: int = 0) -> str:
    """JSON with objects and lists of objects one entry per line and lists
    of scalars — a stations row — kept on one line, so the golden diffs by
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
                table = network["stations"]["path"]
                _copy(output / directory / table, destination / directory / table)
            if (output / directory / "item.json").exists():
                _copy(output / directory / "item.json", destination / directory / "item.json")
        for relative in ("aq/collection.json", "aq/item.json"):
            if (output / relative).exists():
                _copy(output / relative, destination / relative)
        _copy(output / "latest-aq.json", destination / "latest-aq.json")
    return reports


if __name__ == "__main__":
    reports = build_expected(FIXTURES / "expected")
    print(json.dumps([{key: report[key] for key in ("round", "stations", "observations", "networks")} for report in reports], indent=2))

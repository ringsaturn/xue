"""Regenerate the nexrad golden from the Level 3 fixture.

``tests/fixtures/nexrad/nexrad/`` holds seven real Level 3 files from the
Rolling Fork tornado night (DGX's N0B and N0G, three of GWX's MESO-SAILS
N0G sweeps) and the two stations' rows of NCEI's station list. This script
replays the rounds ending 01:40 and 01:45Z offline, with a fixed generation
time, and writes the newest round's window manifest and STAC documents
under ``tests/fixtures/nexrad/expected/`` for ``tests/test_nexrad.py`` and
``tests/test_stac.py`` to hold the build to. It builds the same interval as
a showcase case too, its window stores beside one manifest, and pins that
manifest as ``expected/case/index.json``. The stores themselves are not
pinned: their codes are checked against the source files directly.

Run it after a deliberate change to the reader, the round rule or the
manifest, and commit the diff with the change::

    .venv/bin/python tests/prepare_nexrad_golden.py
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from xuebuild.nexrad.build import build_case, replay, write_pointer
from xuebuild.nexrad.schema import WINDOW_FILENAME, parse_round
from xuebuild.stac import write_point_product_documents

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nexrad"
START = datetime(2023, 3, 25, 1, 35, tzinfo=UTC)
END = datetime(2023, 3, 25, 1, 45, tzinfo=UTC)
ROUND = "nexrad.202303250145"
CASE = "case"


def build(output: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as scratch:
        raw = Path(scratch) / "raw"
        shutil.copytree(FIXTURES / "nexrad", raw / "nexrad")
        report = replay(start=START, end=END, sites=["DGX", "GWX"], raw_root=raw, output_root=output, fetch=False, now=END)
    write_pointer(output, parse_round("202303250145"), report["windowBytes"])
    write_point_product_documents(output, product="nexrad", index_path=output / ROUND / WINDOW_FILENAME)
    return report


def build_case_window(output: Path) -> dict[str, object]:
    """The fixture interval as a showcase case: window stores and their
    manifest written straight into ``output``."""
    with tempfile.TemporaryDirectory() as scratch:
        raw = Path(scratch) / "raw"
        shutil.copytree(FIXTURES / "nexrad", raw / "nexrad")
        return build_case(start=START, end=END, sites=["DGX", "GWX"], raw_root=raw, output_root=output, fetch=False, now=END)


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(json.loads(source.read_bytes()), indent=1, ensure_ascii=False) + "\n")


def build_expected(destination: Path) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        output = Path(scratch)
        build(output)
        if destination.exists():
            shutil.rmtree(destination)
        for relative in (f"{ROUND}/{WINDOW_FILENAME}", f"{ROUND}/item.json", "nexrad/collection.json", "nexrad/item.json", "latest-nexrad.json"):
            _copy(output / relative, destination / relative)
        build_case_window(output / CASE)
        _copy(output / CASE / WINDOW_FILENAME, destination / CASE / WINDOW_FILENAME)


if __name__ == "__main__":
    build_expected(FIXTURES / "expected")
    print(sorted(str(path.relative_to(FIXTURES)) for path in (FIXTURES / "expected").rglob("*.json")))

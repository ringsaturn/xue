"""Regenerate the column store fixture, ``tests/fixtures/column/``:

- ``map/bundles.zarr``: a shared map store of a synthetic GFS run on a grid of
  100 x 41 cells, so a column store's second shard holds one block row, cut by
  the grid, and eleven never-written ones, and its last block column is padded;
  an axis that is hourly to +3 h and three-hourly after; temperatures on three
  levels and heights on two, each level's codebook its own, the wind pair on
  one, the 2 m temperature, a RAW precipitation rate with no analysis frame,
  and the one-frame terrain. Some cells are nodata, a whole block among them.
- ``plan.json``: the plan both writers take, and ``columns.zarr/``: what the
  reference writer cuts from it.
- ``manifest.json``, naming both stores, and ``expected.json``: the codes of
  some cells (``arrays``: ``[[code per level] per frame]``), and the libzstd
  the shards were compressed with.

Run it after a deliberate change to the format or the fixture, and commit the
diff with the change::

    .venv/bin/python tests/prepare_column_golden.py

No GDAL, no network.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xuebuild import columns, zarrstore, zstdcli  # noqa: E402
from xuebuild.common import crc32_hex  # noqa: E402
from xuebuild.grid import GridInfo  # noqa: E402
from xuebuild.manifest import build_bin_manifest  # noqa: E402
from xuebuild.metadata import build_metadata  # noqa: E402
from xuebuild.variables import variable_spec  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "column"

RUN_TIME = datetime(2026, 11, 24, 0, tzinfo=UTC)
PROFILE = "balanced"
GRID = GridInfo(width=41, height=100, first_longitude=100.0, first_latitude=60.0, longitude_step=0.5, latitude_step=-0.5)
TILE = (16, 24)
"""A map tile 24 rows tall: no multiple of it is the band's first or last row
but 0 and 96."""
HOURS = [0, 1, 2, 3, 6, 9, 12]
NODATA = 255

BUNDLES: tuple[tuple[str, tuple[str, ...], list[int]], ...] = (
    ("tmp2m", ("tmp2m",), HOURS),
    ("prate", ("prate",), HOURS[1:]),
    ("hgt1000", ("hgt1000",), HOURS),
    ("hgt500", ("hgt500",), HOURS),
    ("tmp1000", ("tmp1000",), HOURS),
    ("tmp850", ("tmp850",), HOURS),
    ("tmp500", ("tmp500",), HOURS),
    ("orog", ("orog",), [0]),
    ("wind850", ("ugrd850", "vgrd850"), HOURS),
)
"""Each bundle's variables and its axis."""

ARRAYS: tuple[tuple[str, tuple[int, ...] | None, tuple[tuple[str, str], ...]], ...] = (
    ("tmp", (1000, 850, 500), (("tmp1000", "tmp1000"), ("tmp850", "tmp850"), ("tmp500", "tmp500"))),
    ("hgt", (1000, 500), (("hgt1000", "hgt1000"), ("hgt500", "hgt500"))),
    ("ugrd", (850,), (("wind850", "ugrd850"),)),
    ("vgrd", (850,), (("wind850", "vgrd850"),)),
    ("tmp2m", None, (("tmp2m", "tmp2m"),)),
    ("orog", None, (("orog", "orog"),)),
    ("prate", None, (("prate", "prate"),)),
)

CELLS = ((0, 0), (7, 7), (8, 8), (24, 17), (41, 20), (60, 12), (95, 40), (96, 0), (99, 40))
"""Block corners, a nodata cell, a nodata block, the last row of the first
band, the first of the second, and the grid's last cell."""


def codes(variable_id: str, hour: int) -> np.ndarray:
    """One plane of codes: patches of cells drifting east with the hour, each
    variable its own. Integers alone, and patches rather than a wave, so the
    planes are the same everywhere and the fixture stays small."""
    salt = 11 * sum(variable_id.encode())
    rows, cols = np.mgrid[0 : GRID.height, 0 : GRID.width]
    plane = (40 + (rows // 9 * 5 + (cols + hour) // 11 * 13 + salt) % 120 + hour).astype(np.uint8)
    if variable_id == "prate":
        plane[plane < 90] = 0
    plane[41, 20] = NODATA
    plane[56:64, 8:16] = NODATA
    return plane


def dimensions(bundle_id: str, hours: list[int]) -> tuple[str, str, str]:
    return ("time" if hours == HOURS else f"time_{bundle_id}", "latitude", "longitude")


def write_map(root: Path) -> dict[str, zarrstore.BundleWrite]:
    """The map store ``<root>/bundles.zarr``."""
    members = []
    for bundle_id, variable_ids, hours in BUNDLES:
        source = zarrstore.CodesSource(
            build_metadata(RUN_TIME, hours, GRID, PROFILE, variable_ids),
            {variable_id: [codes(variable_id, hour) for hour in hours] for variable_id in variable_ids},
            {
                variable_id: "raw" if variable_spec(variable_id).predictor == "raw" else "previous"
                for variable_id in variable_ids
            },
            TILE,
        )
        members.append(zarrstore.SharedMember(bundle_id, source, dimensions(bundle_id, hours)))
    return zarrstore.write_shared_store(root / "bundles.zarr", members)


def plan(store: str = "map/bundles.zarr") -> dict[str, Any]:
    """The plan of the fixture's column store, its map store at ``store``."""
    arrays = []
    for array_id, levels, members in ARRAYS:
        predictors = {"raw" if variable_spec(variable_id).predictor == "raw" else "previous" for _, variable_id in members}
        arrays.append(
            {
                "id": array_id,
                "predictor": predictors.pop(),
                **({"levels": list(levels)} if levels is not None else {}),
                "members": [
                    {"bundle": bundle_id, "variable": variable_id, "store": store} for bundle_id, variable_id in members
                ],
            }
        )
    return {"columnsPlan": columns.PLAN_VERSION, "arrays": arrays}


def expected() -> dict[str, Any]:
    hours = {variable_id: axis for _, variable_ids, axis in BUNDLES for variable_id in variable_ids}
    cells = []
    for row, column in CELLS:
        arrays = {
            array_id: [
                [int(codes(variable_id, hour)[row, column]) for _, variable_id in members]
                for hour in hours[members[0][1]]
            ]
            for array_id, _, members in ARRAYS
        }
        cells.append({"row": row, "column": column, "arrays": arrays})
    return {"zstdVersion": ".".join(map(str, zstdcli.zstd_version()[:3])), "cells": cells}


def _dump(path: Path, payload: dict[str, Any], indent: int | None = 2) -> None:
    path.write_text(json.dumps(payload, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")


def prepare(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    (root / "map").mkdir(parents=True)
    written = write_map(root / "map")
    _dump(root / "plan.json", plan())
    report = columns.write_column_store(root, plan(), root / columns.STORE_NAME, jobs=1)
    manifest = build_bin_manifest(
        RUN_TIME,
        bundles=[{"variable": bundle_id, "zarr": written[bundle_id].descriptor(root)} for bundle_id, _, _ in BUNDLES],
        expected_hours=HOURS[-1],
        columns={"path": columns.STORE_NAME, "byteLength": report["byteLength"], "crc32": report["crc32"]},
    )
    _dump(root / "manifest.json", manifest)
    _dump(root / "expected.json", expected(), indent=None)
    assert report["crc32"] == crc32_hex((root / columns.STORE_NAME / "zarr.json").read_bytes())


if __name__ == "__main__":
    prepare(FIXTURES)
    print(f"wrote {FIXTURES}")

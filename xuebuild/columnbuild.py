"""Attaching a column store to a published run (``build-columns``): cut it
through the selected encoder from the map stores on disk, read a sample back,
and only then name it in the manifest and rewrite the pointer. The writer is
``columns.py``; the publish job is in ``docs/contribution/publishing.md``.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import numpy as np

from . import encoder
from .assemble import read_manifest
from .columnarrays import ISOBARIC_SURFACE, PASCALS_PER_HPA
from .columns import (
    STORE_NAME,
    ColumnLayout,
    ColumnStore,
    ColumnStoreError,
    build_plan,
    read_map_rows,
    resolve_plan,
)
from .common import crc32_hex
from .errors import ManifestError
from .manifest import _parse_time, validate_bin_manifest, write_bin_manifest, write_pointer_to_manifest
from .sources import SOURCES, SourceSpec, source_spec

MAX_OBJECT_BYTES = 512_000_000
"""Cloudflare caches no object over 512 MB (https://developers.cloudflare.com/cache/concepts/default-cache-behavior/),
so every range of a larger shard would be read from the bucket."""

CHECK_CELLS = 6
"""Cells of each array the build reads back."""


def plan_inputs(plan: dict[str, Any]) -> list[str]:
    """The run-relative objects a cut of ``plan`` reads: each map store's group
    document, whose consolidated metadata holds every array's, and each
    member's shard."""
    inputs: dict[str, None] = {}
    for array in plan["arrays"]:
        for member in array["members"]:
            inputs[f"{member['store']}/zarr.json"] = None
            inputs[f"{member['store']}/{member['variable']}/c/0/0/0"] = None
    return list(inputs)


def manifest_source(manifest: dict[str, Any]) -> SourceSpec:
    """The registered source a manifest is of."""
    for source in SOURCES.values():
        if manifest.get("model") == source.manifest_model and manifest.get("product") == source.product:
            return source
    raise ManifestError(f"manifest of {manifest.get('model')} {manifest.get('product')} is of no registered source")


def _require_source(source: SourceSpec, manifest: dict[str, Any], what: str) -> None:
    if manifest.get("model") != source.manifest_model or manifest.get("product") != source.product:
        raise ManifestError(
            f"{what} is of {manifest.get('model')} {manifest.get('product')}, not {source.manifest_model} {source.product}"
        )


def column_inputs(manifest: dict[str, Any]) -> list[str]:
    """What ``build-columns`` reads of a run beside its manifest; nothing on a
    source that ships no column store."""
    source = manifest_source(manifest)
    return plan_inputs(build_plan(source, manifest)) if source.column_arrays else []


def columns_missing(source: SourceSpec, manifest: dict[str, Any]) -> bool:
    """Whether a published run lacks the column store its source ships."""
    _require_source(source, manifest, "the manifest")
    return bool(source.column_arrays) and "columns" not in manifest


def sample_cells(height: int, width: int, count: int = CHECK_CELLS) -> list[tuple[int, int]]:
    """The far corners, then cells spread over the grid: the same cells on
    every build of one grid."""
    cells = [(0, 0), (height - 1, width - 1)]
    for number in range(1, count - 1):
        # Multiples of the golden ratio spread without repeating a column.
        fraction = (number * 0.6180339887498949) % 1.0
        cells.append((height * number // (count - 1), int(width * fraction)))
    return cells


def check_columns(run_directory: Path, plan: dict[str, Any], store_path: Path) -> int:
    """Hold a sample of a written store's cells to the map stores it was cut
    from; the number of columns compared."""
    layout = resolve_plan(run_directory, plan)
    store = ColumnStore(store_path)
    compared = 0
    for array in layout.arrays:
        for row, column in sample_cells(array.geometry.height, array.geometry.width):
            found = store.column(array.id, row, column)
            for level, member in enumerate(array.members):
                codes = read_map_rows(member.array, range(row, row + 1))[:, 0, column]
                if not np.array_equal(found[:, level], codes):
                    raise ColumnStoreError(
                        f"{store_path}/{array.id} does not hold {member.id}'s codes at row {row}, column {column}"
                    )
            compared += 1
    return compared


def published_level(variable: dict[str, Any]) -> int | None:
    """The isobaric surface a published variable's parameter block names, in
    hPa; None on any other surface."""
    parameter = variable.get("parameter")
    if not isinstance(parameter, dict) or parameter.get("typeOfFirstFixedSurface") != ISOBARIC_SURFACE:
        return None
    factor = parameter.get("scaleFactorOfFirstFixedSurface")
    scaled = parameter.get("scaledValueOfFirstFixedSurface")
    if not isinstance(factor, int) or not isinstance(scaled, int):
        raise ColumnStoreError(f"{variable['id']}: an isobaric surface without a value")
    # Whole-number arithmetic: a level is an index here, not a measurement.
    numerator = scaled * 10 ** max(-factor, 0)
    denominator = 10 ** max(factor, 0) * PASCALS_PER_HPA
    if numerator <= 0 or numerator % denominator:
        raise ColumnStoreError(f"{variable['id']}: its isobaric surface is not a whole number of hPa")
    return numerator // denominator


def check_levels(layout: ColumnLayout) -> None:
    """Hold each array's levels, the registry's, to the surfaces the stores
    were published with."""
    for array in layout.arrays:
        for member, level in zip(array.members, array.levels or (None,), strict=True):
            if published_level(member.variable) != level:
                raise ColumnStoreError(
                    f"column array {array.id}: {member.id} was published on "
                    f"{published_level(member.variable)} hPa, not {level}"
                )


def _check_objects(store_path: Path) -> None:
    for path in sorted(store_path.rglob("*")):
        if path.is_file() and path.stat().st_size > MAX_OBJECT_BYTES:
            raise ColumnStoreError(
                f"{path.relative_to(store_path)} is {path.stat().st_size} bytes, over the {MAX_OBJECT_BYTES} a CDN caches"
            )


def _check_inputs(run_directory: Path, manifest: dict[str, Any], plan: dict[str, Any]) -> None:
    """Every input on disk, and each group document the one the manifest
    names: a directory filled by a download may hold a document a top-up has
    replaced since."""
    missing = [path for path in plan_inputs(plan) if not (run_directory / path).is_file()]
    if missing:
        raise ColumnStoreError(f"{run_directory} lacks {len(missing)} inputs, {missing[0]} the first")
    named = {entry["zarr"]["path"]: entry["zarr"]["crc32"] for entry in manifest["bundles"] if "path" in entry.get("zarr", {})}
    for store in {member["store"] for array in plan["arrays"] for member in array["members"]}:
        document = run_directory / store / "zarr.json"
        if crc32_hex(document.read_bytes()) != named[store]:
            raise ColumnStoreError(f"{document} is not the group document the manifest names")


def build_columns(
    output_dir: Path, *, model: str, run_id: str, jobs: int | None = None, dry: bool = False
) -> dict[str, Any]:
    """Cut a run's ``columns.zarr`` and attach it: the store, the manifest
    with its ``columns``, then the live pointer. The run directory may hold no
    more than its manifest and what :func:`column_inputs` lists. The STAC
    documents are the caller's. ``dry`` checks the inputs and writes nothing."""
    started = time.monotonic()
    source = source_spec(model)
    report: dict[str, Any] = {"model": source.manifest_model, "run": run_id}
    if not source.column_arrays:
        return {**report, "columns": None, "reason": f"{source.id} ships no column store"}
    if not source.live or source.latest_filename is None:
        raise ColumnStoreError(f"{source.manifest_model} has no live pointer to attach a column store under")
    run_directory = output_dir / f"{source.id}.{run_id}"
    manifest_path = run_directory / "manifest.json"
    manifest = read_manifest(manifest_path)
    _require_source(source, manifest, str(manifest_path))
    hours = manifest.get("forecastHours")
    if not isinstance(hours, int):
        raise ManifestError("manifest forecastHours must be an integer")
    validate_bin_manifest(manifest, expected_hours=hours)
    run_time = _parse_time(manifest["runTime"], "runTime")
    if run_time.strftime("%Y%m%d%H") != run_id:
        raise ColumnStoreError(f"{manifest_path} is of run {run_time:%Y%m%d%H}, not {run_id}")
    latest_path = output_dir / source.latest_filename
    # Attaching a store rewrites the pointer of a run that is live; it is no way
    # to take a run live or to move the pointer to another.
    if not latest_path.is_file():
        raise ColumnStoreError(f"no live pointer at {latest_path}")
    named = read_manifest(latest_path, what="live pointer").get("run")
    if named != run_id:
        raise ColumnStoreError(f"{latest_path} names run {named}, not {run_id}")
    plan = build_plan(source, manifest)
    _check_inputs(run_directory, manifest, plan)
    layout = resolve_plan(run_directory, plan)
    check_levels(layout)
    if dry:
        return {
            **report,
            "dry": True,
            "arrays": {
                array.id: {"shape": list(array.geometry.shape), "shards": array.geometry.shard_count}
                for array in layout.arrays
            },
        }
    # Cut beside the store's path and moved onto it once checked, so a failed
    # build leaves the store a manifest may already name as it was.
    staged = run_directory / f".{STORE_NAME}"
    shutil.rmtree(staged, ignore_errors=True)
    writer = encoder.resolve_columns()
    try:
        written = encoder.write_columns(run_directory, plan, staged, jobs or os.cpu_count() or 1)
        cut = time.monotonic()
        _check_objects(staged)
        compared = check_columns(run_directory, plan, staged)
        store_path = run_directory / STORE_NAME
        if store_path.exists():
            shutil.rmtree(store_path)
        staged.rename(store_path)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    manifest["columns"] = {"path": STORE_NAME, "byteLength": written["byteLength"], "crc32": written["crc32"]}
    write_bin_manifest(manifest_path, manifest, force=True, expected_hours=hours)
    write_pointer_to_manifest(
        latest_path,
        manifest_path,
        run_id=run_id,
        run_time=run_time,
        model=source.manifest_model,
        product=source.product,
    )
    return {
        **report,
        "encoder": writer,
        "manifest": str(manifest_path),
        "latest": str(latest_path),
        "columns": manifest["columns"],
        "arrays": written["arrays"],
        "checkedColumns": compared,
        "seconds": {"cut": round(cut - started, 1), "check": round(time.monotonic() - cut, 1)},
    }

"""A run built in pieces: grouping its bundles into jobs, and putting the
pieces back together into one manifest and the live pointer.

The scheduled publish fans a run out over one GitHub Actions job per bundle
group (`.github/workflows/publish.yml`). Each job fetches only its own
inputs, converts with ``bundle_ids`` restricted, uploads its artifacts to the
run directory on R2 and leaves behind a *partial manifest* — the manifest a
restricted build writes, holding just that group's bundles — under
``manifest.part.<group>.json``. One finishing job collects the parts, merges
them here into the run's real ``manifest.json`` in publication order, and
only then writes the pointer whose CRC32 covers it. Nothing in the container
or manifest format knows any of this happened: a bundle's bytes do not depend
on which other bundles were built beside it (``tests/test_assemble.py`` holds
that), and the merged manifest is exactly the one a monolithic build writes.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path
from typing import Any

from .binconvert import VIDEO_VARIABLE_IDS, bundle_input_ids, published_bundle_ids
from .errors import ManifestError
from .manifest import (
    _parse_time,
    build_bin_manifest,
    build_latest_pointer,
    write_bin_manifest,
    write_latest_pointer,
)
from .sources import SourceSpec, source_spec

PARTIAL_MANIFEST_PREFIX = "manifest.part."
"""File name prefix of a partial manifest inside the run directory. The
upload targets exclude the pattern, so a part never reaches the bucket."""

DEFAULT_MAX_GROUPS = 16
"""Groups a run is split into by default. A free GitHub plan runs 20 jobs
at once across the whole account; this leaves room for a test run or the
staggered sflux publish beside a GFS one."""


def bundle_group_slug(bundle_ids: tuple[str, ...]) -> str:
    """The name a group goes by in file and artifact names. Bundle ids are
    ``^[a-z][a-z0-9]*$``, so a dash cannot occur inside one."""
    return "-".join(bundle_ids)


def partial_manifest_path(run_directory: Path, bundle_ids: tuple[str, ...]) -> Path:
    return run_directory / f"{PARTIAL_MANIFEST_PREFIX}{bundle_group_slug(bundle_ids)}.json"


def group_needs_video(bundle_ids: tuple[str, ...]) -> bool:
    """Whether a group ships an H.264 companion, i.e. whether its job needs
    ffmpeg installed. The encoder skips the companion with a warning when
    ffmpeg is missing, so the job has to know up front."""
    return any(bundle_id in VIDEO_VARIABLE_IDS for bundle_id in bundle_ids)


def _bundle_weight(source: SourceSpec, bundle_id: str) -> int:
    # A job's time is a fixed setup cost plus work that scales with the planes
    # it fetches and compresses: one per input variable (a vector pair is
    # two, the vapour flux three). The video companion is an ffmpeg install
    # and an encode on top.
    weight = 1 + len(bundle_input_ids(source, bundle_id))
    if bundle_id in VIDEO_VARIABLE_IDS:
        weight += 1
    return weight


def bundle_groups(source: SourceSpec, max_groups: int = DEFAULT_MAX_GROUPS) -> list[tuple[str, ...]]:
    """Partition a source's published bundles into at most ``max_groups``
    groups of roughly equal build cost, one job each.

    Longest-processing-time-first: the heaviest bundles go first, each into
    the lightest group so far. Within a group and across groups the
    publication order is kept, so a job's name reads the way the manifest
    does. With ``max_groups`` at or above the bundle count every bundle is
    its own group.
    """
    if max_groups < 1:
        raise ManifestError("a run needs at least one bundle group")
    published = published_bundle_ids(source)
    order = {bundle_id: index for index, bundle_id in enumerate(published)}
    bins: list[tuple[int, list[str]]] = [(0, []) for _ in range(min(max_groups, len(published)))]
    for bundle_id in sorted(published, key=lambda bundle_id: (-_bundle_weight(source, bundle_id), order[bundle_id])):
        lightest = min(range(len(bins)), key=lambda index: (bins[index][0], index))
        weight, members = bins[lightest]
        bins[lightest] = (weight + _bundle_weight(source, bundle_id), members + [bundle_id])
    groups = [tuple(sorted(members, key=order.get)) for _, members in bins if members]
    return sorted(groups, key=lambda group: order[group[0]])


def bundle_group_matrix(source: SourceSpec, max_groups: int = DEFAULT_MAX_GROUPS) -> list[dict[str, Any]]:
    """The groups as a GitHub Actions matrix: ``group`` is the space-separated
    ids a shell passes straight to ``--bundles``, ``slug`` names the job's
    files and artifacts, ``video`` says whether the job installs ffmpeg."""
    return [
        {"group": " ".join(group), "slug": bundle_group_slug(group), "video": group_needs_video(group)}
        for group in bundle_groups(source, max_groups)
    ]


def find_partial_manifests(run_directory: Path) -> list[Path]:
    return sorted(run_directory.glob(f"{PARTIAL_MANIFEST_PREFIX}*.json"))


def merge_partial_manifests(
    parts: list[dict[str, Any]],
    *,
    source: SourceSpec,
    expected_hours: int,
) -> dict[str, Any]:
    """Merge the partial manifests of one run into its manifest.

    Every part must describe the same run — model, product, run time, axis —
    and together they must cover every bundle the source publishes exactly
    once: a group that failed to build is a missing bundle, not a shorter
    run. The result is rebuilt through ``build_bin_manifest`` in publication
    order, with the core pair required, so it is validated exactly as a
    monolithic build's manifest is.
    """
    if not parts:
        raise ManifestError("no partial manifests to assemble")
    first = parts[0]
    for key in ("schemaVersion", "model", "product", "runTime", "forecastHours"):
        values = {json.dumps(part.get(key)) for part in parts}
        if len(values) != 1:
            raise ManifestError(f"partial manifests disagree on {key}: {sorted(values)}")
    if first.get("model") != source.manifest_model or first.get("product") != source.product:
        raise ManifestError(
            f"partial manifests describe {first.get('model')} {first.get('product')}, "
            f"not {source.manifest_model} {source.product}"
        )
    entries: dict[str, dict[str, Any]] = {}
    for part in parts:
        bundles = part.get("bundles")
        if not isinstance(bundles, list):
            raise ManifestError("partial manifest bundles must be a list")
        for bundle in bundles:
            variable = bundle.get("variable") if isinstance(bundle, dict) else None
            if not isinstance(variable, str):
                raise ManifestError("partial manifest bundle has no variable")
            if variable in entries:
                raise ManifestError(f"bundle {variable} appears in more than one partial manifest")
            entries[variable] = bundle
    published = published_bundle_ids(source)
    missing = [bundle_id for bundle_id in published if bundle_id not in entries]
    unexpected = [variable for variable in entries if variable not in published]
    if missing or unexpected:
        raise ManifestError(
            f"partial manifests do not add up to a {source.manifest_model} run: "
            f"missing {missing}, unexpected {unexpected}"
        )
    return build_bin_manifest(
        _parse_time(first.get("runTime"), "runTime"),
        bundles=[entries[bundle_id] for bundle_id in published],
        expected_hours=expected_hours,
        model=source.manifest_model,
        product=source.product,
    )


def assemble_run(
    output_dir: Path,
    *,
    model: str,
    run_id: str,
    expected_hours: int,
    force: bool = False,
) -> dict[str, Any]:
    """Write ``<output_dir>/<model>.<run>/manifest.json`` from the partial
    manifests in that directory, then the model's live pointer beside the
    run directory, and return a small report."""
    source = source_spec(model)
    run_directory = output_dir / f"{source.id}.{run_id}"
    part_paths = find_partial_manifests(run_directory)
    if not part_paths:
        raise ManifestError(f"no {PARTIAL_MANIFEST_PREFIX}*.json in {run_directory}")
    parts = []
    for path in part_paths:
        try:
            parts.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"partial manifest is unreadable: {path}: {exc}") from exc
    payload = merge_partial_manifests(parts, source=source, expected_hours=expected_hours)
    manifest_path = run_directory / "manifest.json"
    write_bin_manifest(manifest_path, payload, force=force, expected_hours=expected_hours)
    manifest_bytes = manifest_path.read_bytes()
    latest_path = output_dir / source.latest_filename
    pointer = build_latest_pointer(
        run_id,
        _parse_time(payload["runTime"], "runTime"),
        manifest_path=manifest_path.relative_to(output_dir).as_posix(),
        manifest_crc32=f"{zlib.crc32(manifest_bytes) & 0xFFFFFFFF:08x}",
        model=source.manifest_model,
        product=source.product,
    )
    write_latest_pointer(latest_path, pointer)
    return {
        "model": source.manifest_model,
        "run": run_id,
        "manifest": str(manifest_path),
        "latest": str(latest_path),
        "parts": [path.name for path in part_paths],
        "bundles": [bundle["variable"] for bundle in payload["bundles"]],
        "byteLength": sum(
            bundle["byteLength"] + sum(variant["byteLength"] for variant in bundle.get("variants", []))
            for bundle in payload["bundles"]
        ),
    }

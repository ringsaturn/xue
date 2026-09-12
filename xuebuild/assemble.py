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

The same independence is what makes a **top-up** sound: when the run R2
already serves lacks bundles the source has since started publishing (a new
variable landed between two cycles), the publish builds only the missing
groups and merges their parts *onto* the live manifest
(``merge_partial_manifests(..., base=...)``) instead of rebuilding the run.
The result is again the manifest a whole build would have written, and the
pointer flips to its new CRC32 under the same run id.
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


def group_needs_eccodes(source: SourceSpec, bundle_ids: tuple[str, ...]) -> bool:
    """Whether a group fetches records the fetcher repacks with ``grib_set``
    (a companion family with ``repack``), i.e. whether its job needs eccodes
    installed. ECMWF needs it for every group and the workflow says so on its
    own; this is for the sources that need it for some bundles only."""
    return any(
        (companion := source.companion_of(input_id)) is not None and companion.repack
        for bundle_id in bundle_ids
        for input_id in bundle_input_ids(source, bundle_id)
    )


def _bundle_weight(source: SourceSpec, bundle_id: str) -> int:
    # A job's time is a fixed setup cost plus work that scales with the planes
    # it fetches and compresses: one per input variable (a vector pair is
    # two, the vapour flux three). The video companion is an ffmpeg install
    # and an encode on top.
    weight = 1 + len(bundle_input_ids(source, bundle_id))
    if bundle_id in VIDEO_VARIABLE_IDS:
        weight += 1
    return weight


def manifest_bundle_ids(manifest: dict[str, Any]) -> tuple[str, ...]:
    """The bundle ids a manifest carries, in its order."""
    bundles = manifest.get("bundles") if isinstance(manifest, dict) else None
    if not isinstance(bundles, list):
        raise ManifestError("manifest bundles must be a list")
    variables = []
    for bundle in bundles:
        variable = bundle.get("variable") if isinstance(bundle, dict) else None
        if not isinstance(variable, str):
            raise ManifestError("manifest bundle has no variable")
        variables.append(variable)
    return tuple(variables)


def missing_bundle_ids(source: SourceSpec, manifest: dict[str, Any]) -> tuple[str, ...]:
    """The bundles the source publishes that ``manifest`` (a published run's)
    does not carry, in publication order — what a top-up has to build. Empty
    when the run is complete as published today."""
    present = set(manifest_bundle_ids(manifest))
    return tuple(bundle_id for bundle_id in published_bundle_ids(source) if bundle_id not in present)


def bundle_groups(
    source: SourceSpec,
    max_groups: int = DEFAULT_MAX_GROUPS,
    bundle_ids: tuple[str, ...] | None = None,
) -> list[tuple[str, ...]]:
    """Partition a source's published bundles — or the subset ``bundle_ids``
    of them, for a top-up — into at most ``max_groups`` groups of roughly
    equal build cost, one job each. No bundles means no groups.

    Longest-processing-time-first: the heaviest bundles go first, each into
    the lightest group so far. Within a group and across groups the
    publication order is kept, so a job's name reads the way the manifest
    does. With ``max_groups`` at or above the bundle count every bundle is
    its own group.
    """
    if max_groups < 1:
        raise ManifestError("a run needs at least one bundle group")
    published = published_bundle_ids(source)
    if bundle_ids is None:
        bundle_ids = published
    else:
        unknown = [bundle_id for bundle_id in bundle_ids if bundle_id not in published]
        if unknown:
            raise ManifestError(f"{source.manifest_model} publishes {list(published)}, not {unknown}")
    order = {bundle_id: index for index, bundle_id in enumerate(published)}
    bins: list[tuple[int, list[str]]] = [(0, []) for _ in range(min(max_groups, len(bundle_ids)))]
    for bundle_id in sorted(bundle_ids, key=lambda bundle_id: (-_bundle_weight(source, bundle_id), order[bundle_id])):
        lightest = min(range(len(bins)), key=lambda index: (bins[index][0], index))
        weight, members = bins[lightest]
        bins[lightest] = (weight + _bundle_weight(source, bundle_id), members + [bundle_id])
    groups = [tuple(sorted(members, key=order.get)) for _, members in bins if members]
    return sorted(groups, key=lambda group: order[group[0]])


def bundle_group_matrix(
    source: SourceSpec,
    max_groups: int = DEFAULT_MAX_GROUPS,
    bundle_ids: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """The groups as a GitHub Actions matrix: ``group`` is the space-separated
    ids a shell passes straight to ``--bundles``, ``slug`` names the job's
    files and artifacts, ``video`` says whether the job installs ffmpeg and
    ``eccodes`` whether it installs grib_set."""
    return [
        {
            "group": " ".join(group),
            "slug": bundle_group_slug(group),
            "video": group_needs_video(group),
            "eccodes": group_needs_eccodes(source, group),
        }
        for group in bundle_groups(source, max_groups, bundle_ids)
    ]


def find_partial_manifests(run_directory: Path) -> list[Path]:
    return sorted(run_directory.glob(f"{PARTIAL_MANIFEST_PREFIX}*.json"))


def merge_partial_manifests(
    parts: list[dict[str, Any]],
    *,
    source: SourceSpec,
    expected_hours: int,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge the partial manifests of one run into its manifest.

    Every part must describe the same run — model, product, run time, axis —
    and together they must cover every bundle the source publishes exactly
    once: a group that failed to build is a missing bundle, not a shorter
    run. The result is rebuilt through ``build_bin_manifest`` in publication
    order, with the core pair required, so it is validated exactly as a
    monolithic build's manifest is.

    With ``base`` — the manifest the run is already published under — the
    parts top it up instead: the base's entries come first, a part's entry
    replaces the base's for the same bundle, and a base entry for a bundle
    the source no longer publishes is dropped (its objects stay in the
    bucket until the run is pruned). The base has to describe the same run
    the parts do, by the same keys.
    """
    if not parts:
        raise ManifestError("no partial manifests to assemble")
    first = parts[0]
    described = parts if base is None else [base, *parts]
    for key in ("schemaVersion", "model", "product", "runTime", "forecastHours"):
        values = {json.dumps(part.get(key)) for part in described}
        if len(values) != 1:
            what = "partial manifests" if base is None else "the live manifest and the partial manifests"
            raise ManifestError(f"{what} disagree on {key}: {sorted(values)}")
    if first.get("model") != source.manifest_model or first.get("product") != source.product:
        raise ManifestError(
            f"partial manifests describe {first.get('model')} {first.get('product')}, "
            f"not {source.manifest_model} {source.product}"
        )
    published = published_bundle_ids(source)
    entries: dict[str, dict[str, Any]] = {}
    if base is not None:
        manifest_bundle_ids(base)
        entries = {bundle["variable"]: bundle for bundle in base["bundles"] if bundle["variable"] in published}
    fresh: set[str] = set()
    for part in parts:
        for variable, bundle in zip(manifest_bundle_ids(part), part["bundles"], strict=True):
            if variable in fresh:
                raise ManifestError(f"bundle {variable} appears in more than one partial manifest")
            fresh.add(variable)
            entries[variable] = bundle
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


def read_manifest(path: Path, *, what: str = "manifest") -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{what} is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError(f"{what} is not a JSON object: {path}")
    return payload


def assemble_run(
    output_dir: Path,
    *,
    model: str,
    run_id: str,
    expected_hours: int,
    force: bool = False,
    base_manifest: Path | None = None,
) -> dict[str, Any]:
    """Write ``<output_dir>/<model>.<run>/manifest.json`` from the partial
    manifests in that directory — onto ``base_manifest``, the run's live
    manifest, for a top-up — then the model's live pointer beside the run
    directory, and return a small report."""
    source = source_spec(model)
    run_directory = output_dir / f"{source.id}.{run_id}"
    part_paths = find_partial_manifests(run_directory)
    if not part_paths:
        raise ManifestError(f"no {PARTIAL_MANIFEST_PREFIX}*.json in {run_directory}")
    parts = [read_manifest(path, what="partial manifest") for path in part_paths]
    base = None if base_manifest is None else read_manifest(base_manifest, what="live manifest")
    payload = merge_partial_manifests(parts, source=source, expected_hours=expected_hours, base=base)
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
    fresh = [variable for part in parts for variable in manifest_bundle_ids(part)]
    return {
        "model": source.manifest_model,
        "run": run_id,
        "manifest": str(manifest_path),
        "latest": str(latest_path),
        "parts": [path.name for path in part_paths],
        "bundles": [bundle["variable"] for bundle in payload["bundles"]],
        # What the parts contributed: everything for a whole run; for a
        # top-up, the bundles added to the live manifest (or rebuilt over
        # it), and the live entries that are no longer published.
        "built": fresh,
        "dropped": [] if base is None else [v for v in manifest_bundle_ids(base) if v not in published_bundle_ids(source)],
        "byteLength": sum(
            bundle["byteLength"] + sum(variant["byteLength"] for variant in bundle.get("variants", []))
            for bundle in payload["bundles"]
        ),
    }

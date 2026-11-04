"""Run a build through the native encoder instead of the CLI pipeline.

`xuepy` is this repository's Rust encoder, published to PyPI and imported as
`xue`. It does the same work `binconvert.convert_bin` does — GDAL extraction,
quantization, Zarr stores, the optional container, manifest — in one process
with GDAL, grib-rs and zstd linked in, and is held to the Python encoder by
byte-for-byte identical output (`docs/encoder.md`). Installing it is what
lets a build skip the per-plane `gdal_translate` and `zstd` subprocesses.

Two things the native encoder does not do, and this module supplies:

* the shared stores' group documents. The wheel writes every bundle's
  arrays and coordinates into ``bundles[.<tier>].zarr`` and one partial
  group document per store (``zarr.part.<first bundle>.json``), exactly the
  bytes the reference writes there, and its manifest entries carry that
  part's CRC-32. Unless the build is one job of a fanned-out publish,
  :func:`zarrstore.finalize_store` then writes each store's ``zarr.json``
  from its part — the reference's own code, so the group document has one
  implementation — and the entries are stamped with its CRC-32
  (``docs/encoder.md``, "Shared stores").

* the live pointer. The pointer carries the manifest's CRC32, so it can only
  be written once the stores' CRCs are stamped into the manifest; the native
  encoder is asked for the manifest alone and the pointer is written here.

`binconvert` stays the reference implementation. Nothing in this module
reimplements a stage — it calls the native encoder, then the same manifest
and pointer helpers the Python path calls.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from . import zarrstore
from .discover import published_bundle_ids
from .errors import ConversionError
from .manifest import (
    build_bin_manifest,
    write_bin_manifest,
    write_pointer_to_manifest,
)
from .registry import document_json
from .sources import source_spec
from .zstdcli import DEFAULT_LEVEL as DEFAULT_ZSTD_LEVEL

LOG = logging.getLogger("xue.native")

# The wheel this module drives. Its major.minor tracks the crate, and the
# encoder's output is only guaranteed byte-identical within a release.
DISTRIBUTION = "xuepy"

# A manifest bundle entry's keys in the order the reference writes them
# (`report.bundle_manifest_entry`); the manifests are compared byte for
# byte.
_ENTRY_ORDER = ("variable", "grid", "path", "byteLength", "crc32", "variants", "poster", "zarr", "series")


def require() -> Any:
    """The native module, or a user-actionable error explaining how to get it."""
    try:
        import xue  # noqa: PLC0415  (optional at import time, by design)
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ConversionError(
            f"the native encoder needs the {DISTRIBUTION} wheel: pip install {DISTRIBUTION} "
            f"(or set XUE_ENCODER=python to use the CLI pipeline)"
        ) from exc
    if not hasattr(xue, "convert_bin"):  # pragma: no cover - a foreign 'xue'
        raise ConversionError(
            f"the importable 'xue' module is not {DISTRIBUTION}: it has no convert_bin"
        )
    return xue


def available() -> bool:
    """Whether the native encoder can be used at all."""
    try:
        require()
    except ConversionError:
        return False
    return True


def knows_source(model: str) -> bool:
    """Whether the installed wheel can build ``model`` from this tree's
    registry document, with every bundle the document publishes for it.

    The wheel carries no table of its own: it reads the registry document
    (:mod:`xuebuild.registry`, ``docs/registry.md``) and refuses it whole,
    before it looks at any input, when the document names a schema, a rule
    or a codebook type the wheel does not implement — which is what a wheel
    that predates such a change does. So an empty input list is enough to
    ask, and nothing is read or written. What a test that needs a wheel at
    least as new as some rule skips on, and what the scheduled builds ask
    before taking the native path. A table change — a new source, a new
    bundle, a new level of a known family — needs no new wheel: the
    document carries it."""
    if not available():
        return False
    try:
        require().convert_bin(
            [],
            Path(os.devnull),
            model=model,
            bundle_ids=list(published_bundle_ids(source_spec(model))),
            registry=document_json(),
        )
    except TypeError:
        # A wheel from before the document: its convert_bin has no
        # ``registry`` keyword, so it cannot build from this tree at all.
        return False
    except Exception as exc:  # noqa: BLE001 - the wheel raises a plain RuntimeError
        message = str(exc)
        return (
            "registry document" not in message
            and "unsupported model" not in message
            and "publishes" not in message
        )
    return True


def version() -> str | None:
    """The installed wheel's version, for the build report and the logs."""
    from importlib.metadata import PackageNotFoundError, version as distribution_version

    try:
        return distribution_version(DISTRIBUTION)
    except PackageNotFoundError:  # pragma: no cover - an editable/maturin dev build
        return None


def _run_time(manifest: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(manifest["runTime"]).replace("Z", "+00:00"))


def _canonical_metadata_json(text: str) -> str:
    """An embedded metadata string, re-serialized the way `json.dumps` does.

    The manifest's poster descriptors carry a variable's metadata as
    a string, and the reference writes it with `json.dumps` left at its
    defaults: spaced separators and every non-ASCII character escaped. Neither
    the bundles (compact, raw UTF-8) nor every released native encoder writes
    it that way, and the manifest is compared byte for byte, so the string is
    normalized here instead of trusted. The parse is identical either way.
    """
    return json.dumps(json.loads(text))


def _manifest_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """One manifest bundle entry as the wheel wrote it, with every key in the
    reference's order. The wheel
    already names the container (where one was written), the tiers and the
    stores."""
    entry = dict(entry)
    poster = entry.get("poster")
    if poster is not None:
        entry["poster"] = {
            **poster,
            "metadataJson": _canonical_metadata_json(poster["metadataJson"]),
        }
    ordered = {key: entry[key] for key in _ENTRY_ORDER if key in entry}
    ordered.update({key: value for key, value in entry.items() if key not in ordered})
    return ordered


def _stamp_store_crcs(entry: dict[str, Any], stamps: dict[str, str]) -> None:
    """Set the ``crc32`` of every store descriptor of one manifest entry or
    build report that names a finalized shared store (``stamps``: store
    directory name → its group document's CRC-32)."""
    for node in (entry, *entry.get("variants", [])):
        store = node.get("zarr")
        if isinstance(store, dict) and "path" in store and Path(store["path"]).name in stamps:
            store["crc32"] = stamps[Path(store["path"]).name]


def _finalize_shared_stores(output_dir: Path, bundle_order: list[str]) -> dict[str, str]:
    """Write the group document of every shared store the wheel left parts
    in, and return each store's CRC-32 by its directory name."""
    stamps: dict[str, str] = {}
    for store in sorted(output_dir.glob("bundles*.zarr")):
        if store.is_dir() and zarrstore.is_shared_store_path(store.name) and zarrstore.find_parts(store):
            stamps[store.name] = zarrstore.finalize_store(store, bundle_order, remove_parts=True)
    return stamps


def _wheel_manifest_path(manifest_path: Path) -> Path:
    """Where the wheel writes its manifest: beside the real one, so its
    paths resolve the same, but under a name no reader or glob takes, since
    the real one is written from it here (with the group documents' CRCs)
    under the caller's own ``force``."""
    return manifest_path.with_name(f".{manifest_path.name}.native")


def _rewrite_manifest(
    wheel_manifest: Path,
    manifest_path: Path,
    stamps: dict[str, str],
    *,
    require_core: bool,
    force: bool,
) -> dict[str, Any]:
    """The manifest from the one the native encoder wrote, with the shared
    stores' CRCs stamped.

    Rebuilt through `build_bin_manifest` rather than edited in place, so the
    result is validated and its keys land in the order the reference writes
    them. The run time and forecast hours come back off the manifest itself:
    the native encoder already derived them from the run's own axis.
    """
    existing = json.loads(wheel_manifest.read_text(encoding="utf-8"))
    wheel_manifest.unlink()
    for entry in existing["bundles"]:
        _stamp_store_crcs(entry, stamps)
    expected_hours = existing["forecastHours"]
    payload = build_bin_manifest(
        _run_time(existing),
        bundles=[
            _manifest_entry(entry)
            for entry in existing["bundles"]
        ],
        expected_hours=expected_hours,
        model=existing["model"],
        product=existing["product"],
        require_core_variables=require_core,
    )
    write_bin_manifest(
        manifest_path,
        payload,
        force=force,
        expected_hours=expected_hours,
        require_core_variables=require_core,
    )
    return payload


def convert_bin(
    input_path: Path | Sequence[Path],
    output_dir: Path,
    *,
    profile: str = "quality",
    work_root: Path | None = None,
    zstd_level: int = DEFAULT_ZSTD_LEVEL,
    require_complete: bool = False,
    expected_hours: int = 120,
    manifest_path: Path | None = None,
    latest_path: Path | None = None,
    run_id: str | None = None,
    force: bool = False,
    skip_variants: bool = False,
    model: str = "gfs",
    bbox: tuple[float, float, float, float] | None = None,
    bundle_ids: tuple[str, ...] | None = None,
    last_hour: int | None = None,
    zarr: bool = True,
    container: bool = False,
    series: bool = True,
    partial: bool = False,
) -> dict[str, Any]:
    """`binconvert.convert_bin`, run through the native encoder.

    Same arguments, same report. ``work_root`` is accepted and ignored: the
    native encoder holds its intermediate planes in memory and writes no
    scratch files.
    """
    if not container and not zarr:
        raise ConversionError("a build without the container needs the store: pass zarr=True")
    module = require()
    source = source_spec(model)
    inputs = (
        [Path(input_path)]
        if isinstance(input_path, (str, Path))
        else [Path(item) for item in input_path]
    )
    # A whole build owns the run's shared stores and starts them empty, as
    # the reference does; the wheel cannot tell a whole build from a
    # fanned-out job (which shares the stores with the run's other jobs),
    # so it only ever drops the group document, and the clearing is here.
    if zarr and not partial:
        for store in Path(output_dir).glob("bundles*.zarr"):
            if store.is_dir() and zarrstore.is_shared_store_path(store.name):
                shutil.rmtree(store)
    LOG.info("converting with the native encoder (%s %s)", DISTRIBUTION, version() or "dev")
    report = module.convert_bin(
        inputs,
        Path(output_dir),
        # The variables, bundles and sources of this tree, as the one
        # document the wheel builds from (docs/registry.md): the wheel
        # carries no table of its own.
        registry=document_json(),
        profile=profile,
        model=model,
        zstd_level=zstd_level,
        require_complete=require_complete,
        expected_hours=expected_hours,
        manifest_path=_wheel_manifest_path(Path(manifest_path)) if manifest_path is not None else None,
        # The pointer is written below, once the manifest is final: it
        # carries the manifest's CRC32 and the store stamps change it.
        latest_path=None,
        run_id=None,
        force=True,
        skip_variants=skip_variants,
        bbox=bbox,
        bundle_ids=list(bundle_ids) if bundle_ids is not None else None,
        last_hour=last_hour,
        verbose=LOG.isEnabledFor(logging.DEBUG),
        zarr=zarr,
        container=container,
        series_bundle_ids=list(source.series_bundle_ids) if series else [],
    )

    # The wheel built from the document this tree handed it, so the bundle
    # set should be this tree's; a scheduled publish pins the native path
    # precisely so a shortfall cannot pass unnoticed, and the sets are
    # compared here rather than trusted. (A vector bundle or a derived
    # scalar whose inputs a run turned out to lack is left out by both
    # encoders alike; that case never reaches this check because the inputs
    # were fetched off the same tables.)
    expected = tuple(
        bundle_id
        for bundle_id in published_bundle_ids(source)
        if bundle_ids is None or bundle_id in bundle_ids
    )
    written = tuple(bundle["variable"] for bundle in report["bundles"])
    if written != expected:
        raise ConversionError(
            f"{DISTRIBUTION} {version() or 'dev'} built {list(written)} where this "
            f"xuebuild publishes {list(expected)} for {source.manifest_model}; the "
            "wheel does not read the registry document the way this tree writes "
            "it — release or upgrade the wheel, or set XUE_ENCODER=python"
        )

    stamps: dict[str, str] = {}
    if zarr and not partial:
        stamps = _finalize_shared_stores(Path(output_dir), list(written))
        for bundle in report["bundles"]:
            _stamp_store_crcs(bundle, stamps)
        for variant in report.get("variants", []):
            _stamp_store_crcs(variant, stamps)

    if manifest_path is not None:
        require_core = bundle_ids is None
        payload = _rewrite_manifest(
            _wheel_manifest_path(Path(manifest_path)),
            Path(manifest_path),
            stamps,
            require_core=require_core,
            force=force,
        )
        LOG.info("wrote manifest %s", manifest_path)
        if latest_path is not None and run_id is not None:
            write_pointer_to_manifest(
                Path(latest_path),
                Path(manifest_path),
                run_id=run_id,
                run_time=_run_time(payload),
                model=source.manifest_model,
                product=source.product,
            )
            LOG.info("wrote live pointer %s -> run %s", latest_path, run_id)
    return report

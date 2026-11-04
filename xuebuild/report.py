"""What a build returns and publishes (mirrored in ``encode/report.rs``):
the manifest entries of its bundles and their tiers, in the key order the
native encoder writes, and the delivered size of each.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .grid import GridInfo


def bundle_manifest_entry(
    bundle: dict[str, Any],
    manifest_dir: Path,
    poster_report: dict[str, Any] | None,
    variant_reports: list[dict[str, Any]] | None = None,
    *,
    grid: GridInfo,
) -> dict[str, Any]:
    entry = {
        "variable": bundle["variable"],
        # The bundle's full grid, the same block the store's
        # ``attributes.xue.grid`` carries, so the STAC Item states the
        # authoritative grid instead of un-decimating a poster.
        "grid": grid.metadata(),
        **_container_fields(bundle, manifest_dir),
    }
    if variant_reports:
        # Resolution ladder: STREAM-INF style alternate renditions of the
        # same variable; the top-level entry stays the canonical full-res tier.
        entry["variants"] = [_variant_manifest_entry(variant, manifest_dir) for variant in variant_reports]
    if poster_report is not None:
        entry["poster"] = {
            "path": Path(poster_report["path"]).relative_to(manifest_dir).as_posix(),
            "width": poster_report["width"],
            "height": poster_report["height"],
            "byteLength": poster_report["byteLength"],
            "crc32": poster_report["crc32"],
            "metadataJson": poster_report["metadataJson"],
        }
    if "zarr" in bundle:
        entry["zarr"] = _zarr_descriptor(bundle["zarr"], manifest_dir)
    if "series" in bundle:
        entry["series"] = _zarr_descriptor(bundle["series"], manifest_dir)
    return entry


def _variant_manifest_entry(variant: dict[str, Any], manifest_dir: Path) -> dict[str, Any]:
    """One tier's manifest entry, in the key order the native encoder writes
    (the manifests are compared byte for byte): the container's `path` first
    and its `byteLength` / `crc32` after the grid, or none of the three on a
    tier built without a container."""
    container = _container_fields(variant, manifest_dir)
    return {
        **({"path": container["path"]} if container else {}),
        "width": variant["width"],
        "height": variant["height"],
        **({"byteLength": container["byteLength"], "crc32": container["crc32"]} if container else {}),
        "bandwidth": variant["bandwidth"],
        **({"zarr": _zarr_descriptor(variant["zarr"], manifest_dir)} if "zarr" in variant else {}),
    }


def _container_fields(report: dict[str, Any], manifest_dir: Path) -> dict[str, Any]:
    """The manifest's ``path`` / ``byteLength`` / ``crc32`` unit for a bundle
    or variant report — or nothing, for a report built without a container
    (``container: False``), whose store is the one delivery."""
    if not report.get("container", True):
        return {}
    return {
        "path": Path(report["output"]).relative_to(manifest_dir).as_posix(),
        "byteLength": report["byteLength"],
        "crc32": report["crc32"],
    }


def delivered_byte_length(report: dict[str, Any]) -> int:
    """What a bundle or tier report weighs as delivered: its container where
    one was written, its store otherwise."""
    if report.get("container", True):
        return int(report["byteLength"])
    return int(report["zarr"]["byteLength"])


def _zarr_descriptor(report: dict[str, Any], manifest_dir: Path) -> dict[str, Any]:
    """The manifest's ``zarr`` descriptor from a bundle report's ``zarr``
    block (the store's absolute path, its size and the CRC-32 of its root
    document, as `zarrstore.write_bundle_arrays` reports them)."""
    return {
        "path": Path(report["path"]).relative_to(manifest_dir).as_posix(),
        "byteLength": report["byteLength"],
        "crc32": report["crc32"],
    }

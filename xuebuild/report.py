"""What a build returns and publishes (mirrored in ``encode/report.rs``):
the build report, the manifest entries of its bundles and their tiers in the
key order the native encoder writes, the delivered size of each, and the
manifest and live pointer themselves.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .bundles import WIND_BUNDLE_ID, WIND_COMPONENT_IDS
from .discover import bundle_grid_family
from .grid import GridInfo
from .manifest import build_bin_manifest, write_bin_manifest, write_pointer_to_manifest
from .metadata import lead_hours
from .sources import SourceSpec

if TYPE_CHECKING:
    from .binconvert import PlaneStats

LOG = logging.getLogger(__name__)


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


def build_report(
    *,
    output_dir: Path,
    source: SourceSpec,
    grid: GridInfo,
    profile: str,
    zstd_level: int,
    zstd_version: Sequence[int],
    bundle_reports: list[dict[str, Any]],
    variant_reports: dict[str, list[dict[str, Any]]],
    poster_reports: dict[str, dict[str, Any]],
    stats: Sequence[PlaneStats],
    available_vector_ids: tuple[str, ...],
) -> dict[str, Any]:
    """The build statistics ``convert_bin`` returns, in the key order the
    native encoder's report writes."""
    report = {
        "outputDir": str(output_dir),
        "model": source.manifest_model,
        "grid": grid.metadata(),
        "profile": profile,
        "zstdLevel": zstd_level,
        "zstdVersion": ".".join(map(str, zstd_version)),
        "bundles": bundle_reports,
        "variants": [variant for variants in variant_reports.values() for variant in variants],
        "posters": list(poster_reports.values()),
        # Always empty: the report keeps the key the native encoder's
        # report still writes.
        "videos": [],
        "byteLength": sum(delivered_byte_length(bundle) for bundle in bundle_reports),
        "temperatureMaxAbsError": max((item.max_abs_error for item in stats if item.variable_id == "tmp2m"), default=0.0),
        "temperatureClampedPoints": sum(item.clamped_points for item in stats if item.variable_id == "tmp2m"),
        "precipitationOverflowPoints": sum(item.overflow_points for item in stats if item.variable_id == "prate"),
    }
    if WIND_BUNDLE_ID in available_vector_ids:
        report["windMaxAbsError"] = max(
            (item.max_abs_error for item in stats if item.variable_id in WIND_COMPONENT_IDS), default=0.0
        )
        report["windClampedPoints"] = sum(
            item.clamped_points for item in stats if item.variable_id in WIND_COMPONENT_IDS
        )
    return report


def write_manifest(
    manifest_path: Path,
    *,
    run_time: datetime,
    source: SourceSpec,
    bundle_reports: list[dict[str, Any]],
    poster_reports: dict[str, dict[str, Any]],
    variant_reports: dict[str, list[dict[str, Any]]],
    grids: Mapping[str | None, GridInfo],
    offsets: Sequence[int],
    unit_seconds: int,
    expected_hours: int,
    bundle_ids: tuple[str, ...] | None,
    force: bool,
    latest_path: Path | None,
    run_id: str | None,
) -> None:
    """Write the run's manifest over the bundles just built and, given a
    ``latest_path`` and ``run_id``, the live pointer aimed at it — last,
    so the pointer never names a manifest that is not yet written."""
    # The source's core bundles (sources.py core_bundle_ids: the
    # tmp2m/prate pair on a forecast, the reflectivity on a radar
    # mosaic) are what a complete run must publish. A restricted build
    # ships only the bundles it was asked for.
    require_core = bundle_ids is None
    payload = build_bin_manifest(
        run_time,
        bundles=[
            bundle_manifest_entry(
                bundle,
                manifest_path.parent,
                poster_reports.get(bundle["variable"]),
                variant_reports.get(bundle["variable"]),
                grid=grids[bundle_grid_family(source, bundle["variable"])],
            )
            for bundle in bundle_reports
        ],
        expected_hours=lead_hours(offsets[-1], unit_seconds) if len(offsets) > 1 else expected_hours,
        model=source.manifest_model,
        product=source.product,
        require_core_variables=require_core,
    )
    write_bin_manifest(
        manifest_path,
        payload,
        force=force,
        expected_hours=lead_hours(offsets[-1], unit_seconds) if len(offsets) > 1 else expected_hours,
        require_core_variables=require_core,
    )
    LOG.info("wrote manifest %s", manifest_path)
    if latest_path is not None and run_id is not None:
        write_pointer_to_manifest(
            latest_path,
            manifest_path,
            run_id=run_id,
            run_time=run_time,
            model=source.manifest_model,
            product=source.product,
        )
        LOG.info("wrote live pointer %s -> run %s", latest_path, run_id)

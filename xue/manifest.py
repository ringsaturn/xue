from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ManifestError
from .sources import MODEL_PRODUCTS


# Schema v5 bundle registry, in manifest order. The wind bundle packs both
# 10 m components into one two-variable .xue and is optional so
# runs built from wind-less inputs (and older manifests) stay valid; dswrf
# is optional because only the sflux source carries it.
BIN_BUNDLE_VARIABLES = ("tmp2m", "prate", "dswrf", "wind10m")
REQUIRED_BIN_BUNDLE_VARIABLES = ("tmp2m", "prate")


def iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ManifestError(f"{label} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{label} is not a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ManifestError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _write_json_atomic(path: Path, payload: dict[str, Any], *, force: bool) -> None:
    if path.exists() and not force:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"existing manifest is invalid, pass --force to replace it: {path}") from exc
        if existing == payload:
            return
        raise ManifestError(f"existing manifest describes different data, pass --force to replace it: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def build_bin_manifest(
    run_time: datetime,
    *,
    bundles: list[dict[str, Any]],
    expected_hours: int = 120,
    model: str = "GFS",
    product: str = "pgrb2.0p25",
    require_core_variables: bool = True,
) -> dict[str, Any]:
    """Build a schema v5 manifest describing one .xue bundle per variable.

    Each entry in ``bundles`` must carry ``variable``, ``path``, ``byteLength``
    and ``crc32``, and may optionally carry a ``video`` object describing an
    alternate WebCodecs-decodable artifact for that variable, a ``poster``
    descriptor, and a ``variants`` list of reduced-resolution renditions
    (HLS ``STREAM-INF`` semantics — the top-level path
    stays the canonical full-resolution tier).
    """
    payload: dict[str, Any] = {
        "schemaVersion": 5,
        "model": model,
        "product": product,
        "runTime": iso_z(run_time),
        "forecastHours": expected_hours,
        "bundles": [
            {
                "variable": bundle["variable"],
                "path": bundle["path"],
                "byteLength": bundle["byteLength"],
                "crc32": bundle["crc32"],
                **({"variants": bundle["variants"]} if "variants" in bundle else {}),
                **({"video": bundle["video"]} if "video" in bundle else {}),
                **({"poster": bundle["poster"]} if "poster" in bundle else {}),
            }
            for bundle in bundles
        ],
    }
    validate_bin_manifest(payload, expected_hours=expected_hours, require_core_variables=require_core_variables)
    return payload


def _validate_variant_descriptor(variant: object, variable: str, paths: set[str]) -> None:
    if not isinstance(variant, dict):
        raise ManifestError(f"manifest bundle variant descriptor must be an object for {variable}")
    path = variant.get("path")
    if (
        not isinstance(path, str)
        or not path.endswith(".xue")
        or path.startswith(("/", "http:", "https:"))
        or ".." in Path(path).parts
    ):
        raise ManifestError(f"manifest bundle variant path must be a relative .xue path for {variable}")
    if path in paths:
        raise ManifestError("manifest contains duplicate bundle paths")
    paths.add(path)
    for key in ("width", "height", "byteLength", "bandwidth"):
        value = variant.get(key)
        if not isinstance(value, int) or value <= 0:
            raise ManifestError(f"manifest bundle variant {key} must be a positive integer for {variable}")
    crc32 = variant.get("crc32")
    if not isinstance(crc32, str) or len(crc32) != 8 or any(ch not in "0123456789abcdef" for ch in crc32):
        raise ManifestError(f"manifest bundle variant crc32 must be 8 lowercase hex characters for {variable}")


def _validate_poster_descriptor(poster: object, variable: str, paths: set[str]) -> None:
    if not isinstance(poster, dict):
        raise ManifestError(f"manifest bundle poster descriptor must be an object for {variable}")
    path = poster.get("path")
    if (
        not isinstance(path, str)
        or not path.endswith(".poster.bin")
        or path.startswith(("/", "http:", "https:"))
        or ".." in Path(path).parts
    ):
        raise ManifestError(f"manifest bundle poster path must be a relative .poster.bin path for {variable}")
    if path in paths:
        raise ManifestError("manifest contains duplicate bundle paths")
    paths.add(path)
    for key in ("width", "height", "byteLength"):
        value = poster.get(key)
        if not isinstance(value, int) or value <= 0:
            raise ManifestError(f"manifest bundle poster {key} must be a positive integer for {variable}")
    crc32 = poster.get("crc32")
    if not isinstance(crc32, str) or len(crc32) != 8 or any(ch not in "0123456789abcdef" for ch in crc32):
        raise ManifestError(f"manifest bundle poster crc32 must be 8 lowercase hex characters for {variable}")
    metadata_json = poster.get("metadataJson")
    if not isinstance(metadata_json, str) or not metadata_json:
        raise ManifestError(f"manifest bundle poster metadataJson must be a non-empty string for {variable}")
    try:
        json.loads(metadata_json)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest bundle poster metadataJson is not valid JSON for {variable}") from exc


def _validate_video_descriptor(video: object, variable: str, paths: set[str]) -> None:
    if not isinstance(video, dict):
        raise ManifestError(f"manifest bundle video descriptor must be an object for {variable}")

    def _relative_path(key: str, suffix: str) -> None:
        value = video.get(key)
        if (
            not isinstance(value, str)
            or not value.endswith(suffix)
            or value.startswith(("/", "http:", "https:"))
            or ".." in Path(value).parts
        ):
            raise ManifestError(f"manifest bundle video {key} must be a relative {suffix} path for {variable}")
        if value in paths:
            raise ManifestError("manifest contains duplicate bundle paths")
        paths.add(value)

    _relative_path("streamPath", ".h264")
    _relative_path("indexPath", ".h264.index.json")

    byte_length = video.get("byteLength")
    if not isinstance(byte_length, int) or byte_length <= 0:
        raise ManifestError(f"manifest bundle video byteLength must be a positive integer for {variable}")
    crc32 = video.get("crc32")
    if not isinstance(crc32, str) or len(crc32) != 8 or any(ch not in "0123456789abcdef" for ch in crc32):
        raise ManifestError(f"manifest bundle video crc32 must be 8 lowercase hex characters for {variable}")
    codec = video.get("codec")
    if not isinstance(codec, str) or not codec:
        raise ManifestError(f"manifest bundle video codec must be a non-empty string for {variable}")
    for key in ("width", "height", "gop", "frameCount"):
        value = video.get(key)
        if not isinstance(value, int) or value <= 0:
            raise ManifestError(f"manifest bundle video {key} must be a positive integer for {variable}")
    metadata_json = video.get("metadataJson")
    if not isinstance(metadata_json, str) or not metadata_json:
        raise ManifestError(f"manifest bundle video metadataJson must be a non-empty string for {variable}")
    try:
        json.loads(metadata_json)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest bundle video metadataJson is not valid JSON for {variable}") from exc


def validate_bin_manifest(
    payload: dict[str, Any],
    *,
    expected_hours: int = 120,
    require_core_variables: bool = True,
) -> None:
    """Validate a schema v5 manifest.

    ``require_core_variables`` is what separates a full run from a showcase
    case: a run covering the whole globe always publishes the core tmp2m and
    prate pair, while a case ships only the bundles its event needs.
    """
    if payload.get("schemaVersion") != 5:
        raise ManifestError("manifest schemaVersion must be 5")
    model = payload.get("model")
    if model not in MODEL_PRODUCTS or payload.get("product") != MODEL_PRODUCTS[model]:
        raise ManifestError("manifest model and product must identify a registered forecast dataset")
    if payload.get("forecastHours") != expected_hours:
        raise ManifestError(f"manifest forecastHours must be {expected_hours}")
    _parse_time(payload.get("runTime"), "runTime")
    bundles = payload.get("bundles")
    if not isinstance(bundles, list) or not bundles:
        raise ManifestError("manifest bundles must be a non-empty list")
    variables: list[str] = []
    paths: set[str] = set()
    for bundle in bundles:
        if not isinstance(bundle, dict):
            raise ManifestError("manifest bundle must be an object")
        variable = bundle.get("variable")
        if not isinstance(variable, str) or variable not in BIN_BUNDLE_VARIABLES:
            raise ManifestError(f"manifest bundle variable is unsupported: {variable}")
        path = bundle.get("path")
        if (
            not isinstance(path, str)
            or not path.endswith(".xue")
            or path.startswith(("/", "http:", "https:"))
            or ".." in Path(path).parts
        ):
            raise ManifestError(f"manifest bundle path must be a relative .xue path for {variable}")
        if path in paths:
            raise ManifestError("manifest contains duplicate bundle paths")
        paths.add(path)
        byte_length = bundle.get("byteLength")
        if not isinstance(byte_length, int) or byte_length <= 0:
            raise ManifestError(f"manifest bundle byteLength must be a positive integer for {variable}")
        crc32 = bundle.get("crc32")
        if not isinstance(crc32, str) or len(crc32) != 8 or any(ch not in "0123456789abcdef" for ch in crc32):
            raise ManifestError(f"manifest bundle crc32 must be 8 lowercase hex characters for {variable}")
        if "variants" in bundle:
            variants = bundle["variants"]
            if not isinstance(variants, list) or not variants:
                raise ManifestError(f"manifest bundle variants must be a non-empty list for {variable}")
            for variant in variants:
                _validate_variant_descriptor(variant, variable, paths)
        if "video" in bundle:
            _validate_video_descriptor(bundle["video"], variable, paths)
        if "poster" in bundle:
            _validate_poster_descriptor(bundle["poster"], variable, paths)
        variables.append(variable)
    if variables != [item for item in BIN_BUNDLE_VARIABLES if item in variables]:
        raise ManifestError(f"manifest bundles must be unique and ordered as {list(BIN_BUNDLE_VARIABLES)}")
    if require_core_variables:
        for required in REQUIRED_BIN_BUNDLE_VARIABLES:
            if required not in variables:
                raise ManifestError(f"manifest is missing the required {required} bundle")


def write_bin_manifest(
    path: Path,
    payload: dict[str, Any],
    *,
    force: bool = False,
    expected_hours: int = 120,
    require_core_variables: bool = True,
) -> None:
    validate_bin_manifest(
        payload, expected_hours=expected_hours, require_core_variables=require_core_variables
    )
    _write_json_atomic(path, payload, force=force)


def build_latest_pointer(
    run_id: str,
    run_time: datetime,
    *,
    manifest_path: str,
    manifest_crc32: str,
    model: str = "GFS",
    product: str = "pgrb2.0p25",
) -> dict[str, Any]:
    """Build the tiny mutable ``latest.json`` live pointer.

    The pointer is the only mutable object in the dataset: it names the
    current run and where that run's immutable manifest lives (relative to the
    pointer itself), plus the manifest's CRC32 so clients can fetch the
    manifest through immutable ``?v=`` caching.
    """
    payload = {
        "schemaVersion": 1,
        "model": model,
        "product": product,
        "run": run_id,
        "runTime": iso_z(run_time),
        "manifestPath": manifest_path,
        "manifestCrc32": manifest_crc32,
    }
    validate_latest_pointer(payload)
    return payload


def validate_latest_pointer(payload: dict[str, Any]) -> None:
    if payload.get("schemaVersion") != 1:
        raise ManifestError("latest pointer schemaVersion must be 1")
    model = payload.get("model")
    if model not in MODEL_PRODUCTS or payload.get("product") != MODEL_PRODUCTS[model]:
        raise ManifestError("latest pointer model and product must identify a registered forecast dataset")
    run = payload.get("run")
    if not isinstance(run, str) or len(run) != 10 or not run.isdigit():
        raise ManifestError("latest pointer run must be a YYYYMMDDHH cycle id")
    run_time = _parse_time(payload.get("runTime"), "runTime")
    if run_time.strftime("%Y%m%d%H") != run:
        raise ManifestError("latest pointer runTime does not match the run id")
    manifest_path = payload.get("manifestPath")
    if (
        not isinstance(manifest_path, str)
        or not manifest_path.endswith("manifest.json")
        or manifest_path.startswith(("/", "http:", "https:"))
        or ".." in Path(manifest_path).parts
    ):
        raise ManifestError("latest pointer manifestPath must be a relative manifest.json path")
    crc32 = payload.get("manifestCrc32")
    if not isinstance(crc32, str) or len(crc32) != 8 or any(ch not in "0123456789abcdef" for ch in crc32):
        raise ManifestError("latest pointer manifestCrc32 must be 8 lowercase hex characters")


def write_latest_pointer(path: Path, payload: dict[str, Any]) -> None:
    """Atomically (over)write the live pointer. Unlike the manifests this file
    is mutable by design — replacing it is how a new run goes live — so there
    is no ``--force`` guard."""
    validate_latest_pointer(payload)
    _write_json_atomic(path, payload, force=True)

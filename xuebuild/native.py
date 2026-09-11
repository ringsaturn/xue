"""Run a build through the native encoder instead of the CLI pipeline.

`xuepy` is this repository's Rust encoder, published to PyPI and imported as
`xue`. It does the same work `binconvert.convert_bin` does — GDAL extraction,
quantization, temporal residuals, zstd, container, manifest — in one process
with GDAL, grib-rs and zstd linked in, and is held to the Python encoder by
byte-for-byte identical output (`docs/encoder.md`). Installing it is what
lets a build skip the per-plane `gdal_translate` and `zstd` subprocesses.

Two things the native encoder does not do, and this module supplies:

* the optional H.264 companions. It writes bundles, so the codes it wrote can
  be read straight back out of them with the decoder the same wheel carries,
  and handed to the existing ffmpeg encoder. That keeps the video artifacts on
  the Python side, where they started, without re-extracting anything.
* the live pointer. The pointer carries the manifest's CRC32, so it can only
  be written once the video descriptors are in the manifest; the native
  encoder is asked for the manifest alone and the pointer is written here.

`binconvert` stays the reference implementation. Nothing in this module
reimplements a stage — it calls the native encoder, then the same manifest,
pointer and video helpers the Python path calls.
"""

from __future__ import annotations

import json
import logging
import zlib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .binconvert import VIDEO_VARIABLE_IDS, published_bundle_ids
from .errors import ConversionError
from .manifest import (
    REQUIRED_BIN_BUNDLE_VARIABLES,
    build_bin_manifest,
    build_latest_pointer,
    write_bin_manifest,
    write_latest_pointer,
)
from .sources import source_spec
from .videoconvert import build_debug_playlist, encode_variable_video
from .zstdcli import DEFAULT_LEVEL as DEFAULT_ZSTD_LEVEL

LOG = logging.getLogger("xue.native")

# The wheel this module drives. Its major.minor tracks the crate, and the
# encoder's output is only guaranteed byte-identical within a release.
DISTRIBUTION = "xuepy"


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


def version() -> str | None:
    """The installed wheel's version, for the build report and the logs."""
    from importlib.metadata import PackageNotFoundError, version as distribution_version

    try:
        return distribution_version(DISTRIBUTION)
    except PackageNotFoundError:  # pragma: no cover - an editable/maturin dev build
        return None


def _run_time(manifest: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(manifest["runTime"]).replace("Z", "+00:00"))


def _video_reports(
    bundles: list[dict[str, Any]],
    output_dir: Path,
    module: Any,
) -> dict[str, dict[str, Any]]:
    """Encode one H.264 companion per single-variable bundle.

    Best-effort, exactly like the Python path: a missing ffmpeg or a failed
    encode drops that variable's artifact and leaves the .xue as the universal
    fallback. The codes come back out of the bundle that was just written, so
    they are the same bytes the container holds by construction.
    """
    reports: dict[str, dict[str, Any]] = {}
    for bundle in bundles:
        variable_id = bundle["variable"]
        reader = module.Bundle.open(Path(bundle["output"]))
        numeric_ids = reader.variable_ids
        if len(numeric_ids) != 1:
            # The wind pair ships as one two-variable bundle. The video path is
            # per scalar plane, and the particle layer never takes it.
            continue
        if variable_id not in VIDEO_VARIABLE_IDS:
            # Only the surface fields get a companion. The pressure family is
            # drawn as contour lines, which need the exact codes — an H.264
            # approximation would move every line — and the upper-air fills
            # are not worth an ffmpeg pass per level for an opt-in path.
            continue
        grid = reader.metadata["grid"]
        offsets = [int(offset) for offset in reader.frame_offsets]
        codes_by_offset = {
            offset: {variable_id: reader.decode(numeric_ids[0], offset)} for offset in offsets
        }
        reader.clear_cache()
        try:
            artifact = encode_variable_video(
                codes_by_offset, offsets, variable_id, width=grid["width"], height=grid["height"]
            )
        except ConversionError as exc:
            LOG.warning("skipping %s video artifact: %s", variable_id, exc)
            continue
        finally:
            # A whole variable's codes are a quarter of a gigabyte on a global
            # run. The next iteration builds its dict before rebinding this
            # name, so without the drop two of them are alive at once.
            del codes_by_offset
        output_dir.mkdir(parents=True, exist_ok=True)
        stream_path = output_dir / f"{variable_id}.h264"
        index_path = output_dir / f"{variable_id}.h264.index.json"
        playlist_path = output_dir / f"{variable_id}.h264.m3u8"
        stream_path.write_bytes(artifact.stream_bytes)
        index_path.write_text(json.dumps(artifact.index) + "\n", encoding="utf-8")
        playlist_path.write_text(
            build_debug_playlist(artifact.index["frames"], stream_path.name), encoding="utf-8"
        )
        reports[variable_id] = {
            "variable": variable_id,
            "streamPath": str(stream_path),
            "indexPath": str(index_path),
            "byteLength": len(artifact.stream_bytes),
            "crc32": f"{zlib.crc32(artifact.stream_bytes) & 0xFFFFFFFF:08x}",
            "codec": artifact.codec_string,
            "width": grid["width"],
            "height": grid["height"],
            "gop": artifact.index["gop"],
            "frameCount": artifact.index["frameCount"],
            # A single-variable bundle's own metadata is exactly what the
            # Python path builds for its video: same run, same axis, same
            # grid, same variable. Reading it back out of the bundle keeps the
            # two descriptions of one plane from ever disagreeing — but the
            # container stores it compact, and the manifest wants json.dumps
            # at its defaults, so it is re-serialized rather than copied.
            "metadataJson": _canonical_metadata_json(reader.metadata_json),
            "playlistPath": str(playlist_path),
        }
        LOG.info("wrote %s (%.2f MB)", stream_path, len(artifact.stream_bytes) / 1e6)
    return reports


def _canonical_metadata_json(text: str) -> str:
    """An embedded metadata string, re-serialized the way `json.dumps` does.

    The manifest's poster and video descriptors carry a variable's metadata as
    a string, and the reference writes it with `json.dumps` left at its
    defaults: spaced separators and every non-ASCII character escaped. Neither
    the bundles (compact, raw UTF-8) nor every released native encoder writes
    it that way, and the manifest is compared byte for byte, so the string is
    normalized here instead of trusted. The parse is identical either way.
    """
    return json.dumps(json.loads(text))


def _manifest_entry(
    entry: dict[str, Any], video: dict[str, Any] | None, manifest_dir: Path
) -> dict[str, Any]:
    """One manifest bundle entry, with its video descriptor folded in."""
    entry = dict(entry)
    poster = entry.get("poster")
    if poster is not None:
        entry["poster"] = {
            **poster,
            "metadataJson": _canonical_metadata_json(poster["metadataJson"]),
        }
    if video is not None:
        entry["video"] = {
            "streamPath": Path(video["streamPath"]).relative_to(manifest_dir).as_posix(),
            "indexPath": Path(video["indexPath"]).relative_to(manifest_dir).as_posix(),
            "byteLength": video["byteLength"],
            "crc32": video["crc32"],
            "codec": video["codec"],
            "width": video["width"],
            "height": video["height"],
            "gop": video["gop"],
            "frameCount": video["frameCount"],
            "metadataJson": video["metadataJson"],
        }
    return entry


def _rewrite_manifest(
    manifest_path: Path,
    videos: dict[str, dict[str, Any]],
    *,
    require_core: bool,
) -> dict[str, Any]:
    """Fold the video descriptors into the manifest the native encoder wrote.

    Rebuilt through `build_bin_manifest` rather than edited in place, so the
    result is validated and its keys land in the order the reference writes
    them. The run time and forecast hours come back off the manifest itself:
    the native encoder already derived them from the run's own axis.
    """
    existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_hours = existing["forecastHours"]
    payload = build_bin_manifest(
        _run_time(existing),
        bundles=[
            _manifest_entry(entry, videos.get(entry["variable"]), manifest_path.parent)
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
        force=True,
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
    skip_video: bool = False,
    skip_variants: bool = False,
    model: str = "gfs",
    bbox: tuple[float, float, float, float] | None = None,
    bundle_ids: tuple[str, ...] | None = None,
    last_hour: int | None = None,
) -> dict[str, Any]:
    """`binconvert.convert_bin`, run through the native encoder.

    Same arguments, same report. ``work_root`` is accepted and ignored: the
    native encoder holds its intermediate planes in memory and writes no
    scratch files.
    """
    module = require()
    source = source_spec(model)
    inputs = (
        [Path(input_path)]
        if isinstance(input_path, (str, Path))
        else [Path(item) for item in input_path]
    )
    LOG.info("converting with the native encoder (%s %s)", DISTRIBUTION, version() or "dev")
    report = module.convert_bin(
        inputs,
        Path(output_dir),
        profile=profile,
        model=model,
        zstd_level=zstd_level,
        require_complete=require_complete,
        expected_hours=expected_hours,
        manifest_path=manifest_path,
        # The pointer is written below, once the manifest is final: it
        # carries the manifest's CRC32 and the video descriptors change it.
        latest_path=None,
        run_id=None,
        force=force,
        skip_variants=skip_variants,
        bbox=bbox,
        bundle_ids=list(bundle_ids) if bundle_ids is not None else None,
        last_hour=last_hour,
        verbose=LOG.isEnabledFor(logging.DEBUG),
    )

    videos: dict[str, dict[str, Any]] = {}
    if not skip_video:
        videos = _video_reports(report["bundles"], Path(output_dir), module)
    report["videos"] = list(videos.values())

    if manifest_path is not None:
        require_core = bundle_ids is None and all(
            variable_id in published_bundle_ids(source)
            for variable_id in REQUIRED_BIN_BUNDLE_VARIABLES
        )
        payload = _rewrite_manifest(Path(manifest_path), videos, require_core=require_core)
        LOG.info("wrote manifest %s", manifest_path)
        if latest_path is not None and run_id is not None:
            manifest_bytes = Path(manifest_path).read_bytes()
            pointer = build_latest_pointer(
                run_id,
                _run_time(payload),
                manifest_path=Path(manifest_path).relative_to(Path(latest_path).parent).as_posix(),
                manifest_crc32=f"{zlib.crc32(manifest_bytes) & 0xFFFFFFFF:08x}",
                model=source.manifest_model,
                product=source.product,
            )
            write_latest_pointer(Path(latest_path), pointer)
            LOG.info("wrote live pointer %s -> run %s", latest_path, run_id)
    return report

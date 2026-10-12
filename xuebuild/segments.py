"""Cut a rolling window's stores into clock-aligned segments.

A window round rebuilds every bundle over the whole window, but between two
rounds only the newest frames change. Published as one store per bundle,
every round would re-upload the window. Instead each store is cut into
segments aligned to the clock — an hour for an imager or a radar, six hours
for an hourly feed — and each segment is a profile store of its own, put
under ``<model>.segments/<YYYYMMDDHHMM>/<digest>/`` beside the run
directories (docs/zarr-profile.md, "Segmented stores"). The digest is over
what the segment holds, so a completed hour comes out under the same path
round after round: the round's manifest names it again instead of writing
it again, and a viewer's cache keeps serving it.

This is a publishing step over finished stores, whichever encoder wrote
them; it decodes the codes and writes them back with the reference writer.
"""

from __future__ import annotations

import copy
import hashlib
import json
import posixpath
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from . import binformat, zarrstore
from .binconvert import _time_metadata, axis_unit_seconds
from .common import crc32_hex, iso_z
from .errors import ConversionError
from .manifest import _parse_time, validate_latest_pointer, write_bin_manifest, write_latest_pointer

SEGMENTS_SUFFIX = ".segments"
#: Hex digits of the content digest in a segment's path: enough that two
#: contents of one segment start never meet.
DIGEST_LENGTH = 8


def segments_directory_name(source_id: str) -> str:
    """The directory beside the run directories that holds a window
    source's segments."""
    return f"{source_id}{SEGMENTS_SUFFIX}"


def segment_seconds(times: list[datetime]) -> int:
    """How long one segment is: an hour for a sub-hourly feed, six for an
    hourly or slower one, so a segment holds a handful of frames or more
    and an hour boundary is always a segment boundary."""
    steps = [(after - before).total_seconds() for before, after in zip(times, times[1:])]
    cadence = min(steps) if steps else 0
    return 6 * 3600 if cadence >= 3600 else 3600


def segment_start(time: datetime, seconds: int) -> datetime:
    epoch = time.timestamp()
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=time.tzinfo)


@dataclass
class SegmentReport:
    """What cutting a round produced: the segment stores written for it, and
    how many it took over from the live round instead."""

    written: list[Path] = field(default_factory=list)
    reused: int = 0
    written_bytes: int = 0
    reused_bytes: int = 0

    def to_dict(self, data_root: Path) -> dict[str, Any]:
        return {
            "written": [path.relative_to(data_root).as_posix() for path in self.written],
            "reused": self.reused,
            "writtenBytes": self.written_bytes,
            "reusedBytes": self.reused_bytes,
        }


def _frame_times(metadata: dict[str, Any]) -> list[datetime]:
    unit_seconds, offsets = binformat.time_axis(metadata)
    run_time = _parse_time(metadata["runTime"], "runTime")
    return [run_time + timedelta(seconds=offset * unit_seconds) for offset in offsets]


def _segment_metadata(metadata: dict[str, Any], times: list[datetime]) -> dict[str, Any]:
    """The bundle's metadata over a run of its frames: the same document
    with the axis restarted at the segment's first frame."""
    leads = [int((time - times[0]).total_seconds()) for time in times]
    unit_seconds = axis_unit_seconds(leads)
    segment = copy.deepcopy(metadata)
    segment["runTime"] = iso_z(times[0])
    segment["time"] = _time_metadata([lead // unit_seconds for lead in leads], unit_seconds)
    return segment


def _content_digest(metadata: dict[str, Any], layout: tuple[bool, str], planes: dict[str, list[np.ndarray]]) -> str:
    """A digest over everything the written store is a function of: the
    metadata, the codec choices and the codes. Taken before writing, so a
    segment the live round already published is never compressed again."""
    digest = hashlib.sha256()
    digest.update(binformat.metadata_json_bytes(metadata))
    digest.update(json.dumps(layout).encode("utf-8"))
    for name in sorted(planes):
        digest.update(name.encode("utf-8"))
        for plane in planes[name]:
            digest.update(np.ascontiguousarray(plane, dtype=np.uint8).tobytes())
    return digest.hexdigest()[:DIGEST_LENGTH]


def _live_segments(live_manifest: dict[str, Any] | None, live_round: str | None) -> dict[str, dict[str, Any]]:
    """The live round's segment descriptors by their path from the data
    root, which is what makes a segment the same one in two rounds."""
    if live_manifest is None or live_round is None:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for bundle in live_manifest.get("bundles", []):
        for entry in (bundle, *bundle.get("variants", [])):
            for segment in (entry.get("zarr") or {}).get("segments", []):
                found[posixpath.normpath(posixpath.join(live_round, segment["path"]))] = segment
    return found


def cut_store(
    store: Path,
    *,
    round_directory: Path,
    data_root: Path,
    source_id: str,
    live: dict[str, dict[str, Any]],
    report: SegmentReport,
) -> dict[str, Any]:
    """Cut one finished store into segments and return the segmented
    descriptor that replaces its own. The store itself is left in place."""
    group = zarrstore.read_json(store / "zarr.json")
    metadata = group["attributes"]["xue"]
    names = [variable["id"] for variable in metadata["variables"]]
    times = _frame_times(metadata)
    layouts = {name: zarrstore._array_layout(store, name) for name in names}
    geometry = layouts[names[0]][0]
    delta = any(layout[1] for layout in layouts.values())
    index_location = layouts[names[0]][2]
    planes = {name: zarrstore.read_frames(store, name) for name in names}
    predictors = {name: "previous" if layouts[name][1] else "raw" for name in names}
    seconds = segment_seconds(times)

    runs: dict[datetime, list[int]] = {}
    for index, time in enumerate(times):
        runs.setdefault(segment_start(time, seconds), []).append(index)

    round_key = round_directory.relative_to(data_root).as_posix()
    segments: list[dict[str, Any]] = []
    for start, indices in runs.items():
        segment_metadata = _segment_metadata(metadata, [times[index] for index in indices])
        segment_planes = {name: [planes[name][index] for index in indices] for name in names}
        digest = _content_digest(segment_metadata, (delta, index_location), segment_planes)
        destination = data_root / segments_directory_name(source_id) / f"{start:%Y%m%d%H%M}" / digest / store.name
        path = posixpath.relpath(destination.relative_to(data_root).as_posix(), round_key)
        held = live.get(posixpath.normpath(posixpath.join(round_key, path)))
        if held is not None:
            descriptor = {"path": path, "byteLength": held["byteLength"], "crc32": held["crc32"]}
            report.reused += 1
            report.reused_bytes += held["byteLength"]
        else:
            source = zarrstore.CodesSource(
                segment_metadata, segment_planes, predictors, (geometry.tile_width, geometry.tile_height)
            )
            written = zarrstore.write_store(source, destination, delta=delta, index_location=index_location)
            descriptor = {"path": path, "byteLength": written.byte_length, "crc32": written.crc32}
            report.written.append(destination)
            report.written_bytes += written.byte_length
        segments.append(descriptor)
    return {"byteLength": sum(segment["byteLength"] for segment in segments), "segments": segments}


def segment_round(
    round_directory: Path,
    *,
    data_root: Path,
    source_id: str,
    live_manifest: dict[str, Any] | None = None,
    live_manifest_path: str | None = None,
) -> SegmentReport:
    """Replace every map store a round's manifest names with segments, and
    rewrite the manifest. ``live_manifest`` is the round the pointer names
    now and ``live_manifest_path`` its path from the data root (the
    pointer's ``manifestPath``): the segments it already published are
    named again rather than written. The round's own stores are deleted
    once their segments stand, so nothing of them is uploaded."""
    manifest_path = round_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    live_round = posixpath.dirname(live_manifest_path) if live_manifest_path else None
    live = _live_segments(live_manifest, live_round)
    report = SegmentReport()
    cut: list[Path] = []
    for bundle in manifest["bundles"]:
        for entry in (bundle, *bundle.get("variants", [])):
            store = entry.get("zarr")
            if store is None or "segments" in store:
                continue
            if "path" in entry:
                raise ConversionError("a window round ships its stores alone; segmenting it would orphan its containers")
            store_directory = round_directory / store["path"]
            entry["zarr"] = cut_store(
                store_directory,
                round_directory=round_directory,
                data_root=data_root,
                source_id=source_id,
                live=live,
                report=report,
            )
            cut.append(store_directory)
    write_bin_manifest(manifest_path, manifest, force=True, expected_hours=manifest["forecastHours"])
    for store_directory in cut:
        shutil.rmtree(store_directory)
    return report


def cut_round(
    round_directory: Path,
    *,
    data_root: Path,
    source_id: str,
    latest_path: Path | None,
    live_pointer: Path | None,
    live_manifest: Path | None,
) -> dict[str, Any]:
    """`build-bin --round --segments`: segment the round just built and
    point the local pointer at its rewritten manifest. The live pointer and
    manifest are read from files (``make live-pointer`` / ``make
    live-manifest``); either may be absent or empty on a source's first
    round, and then every segment is written."""
    pointer = _read_optional_json(live_pointer)
    manifest = _read_optional_json(live_manifest)
    report = segment_round(
        round_directory,
        data_root=data_root,
        source_id=source_id,
        live_manifest=manifest,
        live_manifest_path=pointer.get("manifestPath") if pointer and manifest else None,
    )
    if latest_path is not None and latest_path.is_file():
        # The pointer convert_bin wrote names the manifest by the CRC of the
        # bytes it had then; the segmented manifest is new bytes.
        local = json.loads(latest_path.read_text(encoding="utf-8"))
        local["manifestCrc32"] = crc32_hex((round_directory / "manifest.json").read_bytes())
        validate_latest_pointer(local)
        write_latest_pointer(latest_path, local)
    return report.to_dict(data_root)


def _read_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else None

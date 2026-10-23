"""Cut a rolling window's stores into clock-aligned segments.

A window round rebuilds every bundle over the whole window, but between two
rounds only the newest frames change. Published as one store per bundle,
every round would re-upload the window. Instead each store is cut into
segments aligned to the clock — an hour for an imager or a radar, six hours
for an hourly feed — and each segment is a profile store of its own, put
under ``<model>.segments/<YYYYMMDDHHMM>/<digest>/`` beside the run
directories (docs/zarr-profile.md, "Segmented stores"). A round's shared
store (``bundles[.<tier>].zarr``) is cut into shared segments, every bundle
of the tier that has frames in the interval in one segment store; a
one-bundle store of an older layout is cut into one-bundle segments. The
digest is over what the segment holds, so a completed hour comes out under
the same path round after round: the round's manifest names it again
instead of writing it again, and a viewer's cache keeps serving it.

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


def _shared_digest(members: list[tuple[str, tuple[str, str, str], dict[str, Any], tuple[bool, str], dict[str, list[np.ndarray]]]]) -> str:
    """A shared segment's digest: SHA-256 over, bundle by bundle in the
    store's ``xue_bundles`` order, the bundle id and a newline, its
    dimension names (``json.dumps`` of the list), and then exactly what
    :func:`_content_digest` takes for a one-bundle segment — its segment
    metadata (``binformat.metadata_json_bytes``), ``json.dumps([delta,
    index_location])``, and each variable id in sorted order followed by its
    planes' bytes in frame order. Every bundle of the segment, its rules and
    its codes are in it, so different contents never share a path."""
    digest = hashlib.sha256()
    for bundle_id, dimensions, metadata, layout, planes in members:
        digest.update(bundle_id.encode("utf-8") + b"\n")
        digest.update(json.dumps(list(dimensions)).encode("utf-8"))
        digest.update(binformat.metadata_json_bytes(metadata))
        digest.update(json.dumps(list(layout)).encode("utf-8"))
        for name in sorted(planes):
            digest.update(name.encode("utf-8"))
            for plane in planes[name]:
                digest.update(np.ascontiguousarray(plane, dtype=np.uint8).tobytes())
    return digest.hexdigest()[:DIGEST_LENGTH]


def _live_segments(
    live_manifest: dict[str, Any] | None, live_round: str | None
) -> dict[tuple[str, str], dict[str, Any]]:
    """The live round's segment descriptors by their path from the data
    root and the bundle naming them — a shared segment is named by several
    bundles, each with its own ``byteLength`` — which is what makes a
    segment the same one in two rounds."""
    if live_manifest is None or live_round is None:
        return {}
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for bundle in live_manifest.get("bundles", []):
        for entry in (bundle, *bundle.get("variants", [])):
            for segment in (entry.get("zarr") or {}).get("segments", []):
                found[(posixpath.normpath(posixpath.join(live_round, segment["path"])), bundle["variable"])] = segment
    return found


def cut_store(
    store: Path,
    *,
    round_directory: Path,
    data_root: Path,
    source_id: str,
    live: dict[tuple[str, str], dict[str, Any]],
    report: SegmentReport,
    bundle_id: str,
) -> dict[str, Any]:
    """Cut one finished one-bundle (profile 1) store into segments and
    return the segmented descriptor that replaces its own. The store itself
    is left in place."""
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
        held = live.get((posixpath.normpath(posixpath.join(round_key, path)), bundle_id))
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


@dataclass
class _Cut:
    """One bundle of a shared store, read back for cutting."""

    bundle_id: str
    metadata: dict[str, Any]
    names: list[str]
    times: list[datetime]
    dimensions: tuple[str, str, str]
    delta: bool
    index_location: str
    tile: tuple[int, int]
    planes: dict[str, list[np.ndarray]]
    predictors: dict[str, str]


def cut_shared_store(
    store: Path,
    bundle_ids: list[str],
    *,
    round_directory: Path,
    data_root: Path,
    source_id: str,
    live: dict[tuple[str, str], dict[str, Any]],
    report: SegmentReport,
) -> dict[str, dict[str, Any]]:
    """Cut one finished shared store into shared segments and return, per
    bundle of ``bundle_ids`` (the bundles the manifest names it with, in its
    order), the segmented descriptor that replaces its own.

    The segment length is the store's, from every bundle's frames together,
    so the bundles of one tier are cut at the same boundaries; a segment
    holds every bundle with a frame in its interval, each over its own
    frames there (its own ``runTime`` and ``time``), keeping its dimension
    names, codec choices and tile. A bundle's segment ``byteLength`` is its
    own arrays and coordinates in the segment; every bundle of a segment
    carries the segment's group CRC-32. The store itself is left in place."""
    cuts: list[_Cut] = []
    for bundle_id in bundle_ids:
        metadata = zarrstore.bundle_metadata(store, bundle_id)
        names = [variable["id"] for variable in metadata["variables"]]
        layouts = {name: zarrstore._array_layout(store, name) for name in names}
        geometry = layouts[names[0]][0]
        cuts.append(
            _Cut(
                bundle_id=bundle_id,
                metadata=metadata,
                names=names,
                times=_frame_times(metadata),
                dimensions=zarrstore.array_dimensions(store, names[0]),
                delta=any(layout[1] for layout in layouts.values()),
                index_location=layouts[names[0]][2],
                tile=(geometry.tile_width, geometry.tile_height),
                planes={name: zarrstore.read_frames(store, name) for name in names},
                predictors={name: "previous" if layouts[name][1] else "raw" for name in names},
            )
        )
    index_locations = {cut.index_location for cut in cuts}
    if len(index_locations) != 1:
        raise ConversionError(f"{store}: its arrays keep their shard index in different places")
    (index_location,) = index_locations
    seconds = segment_seconds(sorted({time for cut in cuts for time in cut.times}))

    runs: dict[datetime, dict[str, list[int]]] = {}
    for cut in cuts:
        for index, time in enumerate(cut.times):
            runs.setdefault(segment_start(time, seconds), {}).setdefault(cut.bundle_id, []).append(index)

    round_key = round_directory.relative_to(data_root).as_posix()
    descriptors: dict[str, list[dict[str, Any]]] = {cut.bundle_id: [] for cut in cuts}
    for start in sorted(runs):
        members = []
        for cut in cuts:
            indices = runs[start].get(cut.bundle_id)
            if not indices:
                continue
            members.append(
                (
                    cut,
                    _segment_metadata(cut.metadata, [cut.times[index] for index in indices]),
                    {name: [cut.planes[name][index] for index in indices] for name in cut.names},
                )
            )
        digest = _shared_digest(
            [
                (cut.bundle_id, cut.dimensions, metadata, (cut.delta, index_location), planes)
                for cut, metadata, planes in members
            ]
        )
        destination = data_root / segments_directory_name(source_id) / f"{start:%Y%m%d%H%M}" / digest / store.name
        path = posixpath.relpath(destination.relative_to(data_root).as_posix(), round_key)
        key = posixpath.normpath(posixpath.join(round_key, path))
        held = {cut.bundle_id: live.get((key, cut.bundle_id)) for cut, _, _ in members}
        if all(segment is not None for segment in held.values()):
            for bundle_id, segment in held.items():
                assert segment is not None
                descriptors[bundle_id].append(
                    {"path": path, "byteLength": segment["byteLength"], "crc32": segment["crc32"]}
                )
                report.reused_bytes += segment["byteLength"]
            report.reused += 1
            continue
        written = zarrstore.write_shared_store(
            destination,
            [
                zarrstore.SharedMember(
                    cut.bundle_id,
                    zarrstore.CodesSource(metadata, planes, cut.predictors, cut.tile),
                    cut.dimensions,
                    cut.delta,
                )
                for cut, metadata, planes in members
            ],
            index_location=index_location,
        )
        for bundle_id, bundle in written.items():
            descriptors[bundle_id].append({"path": path, "byteLength": bundle.byte_length, "crc32": bundle.crc32})
        report.written.append(destination)
        report.written_bytes += zarrstore.store_byte_length(destination)
    return {
        bundle_id: {"byteLength": sum(segment["byteLength"] for segment in segments), "segments": segments}
        for bundle_id, segments in descriptors.items()
    }


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
    # Every entry naming a store, by store path in manifest order; a shared
    # store is named by every bundle (or variant) of its tier.
    named: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for bundle in manifest["bundles"]:
        for entry in (bundle, *bundle.get("variants", [])):
            store = entry.get("zarr")
            if store is None or "segments" in store:
                continue
            if "path" in entry:
                raise ConversionError("a window round ships its stores alone; segmenting it would orphan its containers")
            named.setdefault(store["path"], []).append((bundle["variable"], entry))
    for path, entries in named.items():
        store_directory = round_directory / path
        options = dict(round_directory=round_directory, data_root=data_root, source_id=source_id, live=live, report=report)
        if zarrstore.is_shared_store_path(path):
            descriptors = cut_shared_store(store_directory, [bundle_id for bundle_id, _ in entries], **options)
            for bundle_id, entry in entries:
                entry["zarr"] = descriptors[bundle_id]
        else:
            ((bundle_id, entry),) = entries
            entry["zarr"] = cut_store(store_directory, bundle_id=bundle_id, **options)
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

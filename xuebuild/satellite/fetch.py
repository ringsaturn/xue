"""One window of a satellite source: list, fetch, warp, cache, stack.

The shape is the MRMS and JMA feeds' (``xuebuild/fetch.py``): a run is a
window named by its first hour, its slots every ``cadence_seconds`` from
that hour through ``hours`` past it, and a slot the bucket has not
finished (fewer tiles than the platform's count) or never wrote is left
out — the axis allows the gap, and the next round takes it if it lands.
What is new is the frame cache: a slot is fetched and warped once, into
``<raw_root>/<source>-frames/<channel>/``, and every round after that reads
the frame back, so a round costs one slot's tiles (26 MB for a Himawari
channel) however long the window. A producer (:mod:`producers`) runs on a
slot the same way: once, on the slot's warped channels, its outputs cached
as frames of their own; so a window of four channels and a composite costs
a round four channels' tiles and one composition.

The window's slots are every ``cadence_seconds`` of the *source*, which
may be coarser than the platform's scan cadence: Meteosat scans every ten
minutes and the source publishes the cycle on the hour alone, the one the
agency's data policy releases openly.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..errors import ConversionError, DownloadError
from . import assemble
from .platforms import Channel, Platform
from .producers import Producer
from .projector import PROJECTORS, TargetGrid
from .readers import SlotObject, reader_for

LOG = logging.getLogger(__name__)

#: Tiles fetched at once from the bucket; S3 answers bursts without
#: throttling and a tile is a few hundred kilobytes.
FETCH_CONCURRENCY = 8
#: Series written at once. Each is a `gdal_translate` subprocess that
#: deflates one variable's frames on a single thread, so the cores are the
#: bound; a 3000 x 3000 x 34 Int16 series is ~0.6 GB of frames streamed
#: band by band through GDAL's block cache, a few hundred MB resident.
SERIES_CONCURRENCY = max(1, min(os.cpu_count() or 1, 4))
#: How many of the newest listed slots `latest_slot` asks for their tiles
#: before trusting the listing.
RECENT_SLOTS = 3
#: Bilinear for a brightness temperature, a continuous quantity delivered
#: at 2 km onto a grid of about 4 km; a categorical product would take
#: ``near``.
RESAMPLING = "bilinear"
#: The block mean for a reflectance: the visible bands arrive at 0.5 and
#: 1 km, eight and four times finer than the grid, and a bilinear sample
#: of four source pixels out of sixty-four would speckle a cloud edge
#: that an average draws as the eye sees it from orbit.
REFLECTANCE_RESAMPLING = "average"


def resampling_for(channel: Channel) -> str:
    """How a channel's slot is put on the grid: by its kind."""
    return REFLECTANCE_RESAMPLING if channel.kind == "reflectance" else RESAMPLING


Region = tuple[float, float, float, float]
"""A box as ``(west, south, east, north)`` in degrees."""


def target_grid(platform: Platform, step: float, region: Region | None = None) -> TargetGrid:
    """The published grid: ``region`` at ``step``, the platform's whole
    region when the source names none."""
    west, south, east, north = region or platform.region
    return TargetGrid(west=west, south=south, east=east, north=north, step=step)


def window_slots(platform: Platform, start: datetime, hours: int, *, cadence_seconds: int | None = None) -> list[datetime]:
    """Every slot from ``start`` through ``hours`` past it, inclusive, at
    the source's cadence (the platform's scan cadence by default; a
    coarser one must be a whole number of scans)."""
    cadence = source_cadence(platform, cadence_seconds)
    end = start + timedelta(hours=hours)
    slots: list[datetime] = []
    slot = start
    while slot <= end:
        slots.append(slot)
        slot += timedelta(seconds=cadence)
    return slots


def source_cadence(platform: Platform, cadence_seconds: int | None) -> int:
    """The cadence a source publishes a platform at: the platform's own
    unless the source names a coarser one that is a multiple of it."""
    if cadence_seconds is None:
        return platform.cadence_seconds
    if cadence_seconds < platform.cadence_seconds or cadence_seconds % platform.cadence_seconds:
        raise ConversionError(
            f"{platform.spacecraft} scans every {platform.cadence_seconds} s; a source cannot publish it every {cadence_seconds} s"
        )
    return cadence_seconds


def on_cadence(slot: datetime, cadence_seconds: int) -> bool:
    return int(slot.timestamp()) % cadence_seconds == 0


def needed_tiles(platform: Platform, region: Region | None) -> frozenset[int] | None:
    """The tiles a frame on ``region`` needs, when the source names a
    region and the reader can narrow to it (:meth:`Reader.tiles_covering`);
    None for every tile of the slot."""
    if region is None:
        return None
    return reader_for(platform).tiles_covering(platform, region)


def slot_is_complete(platform: Platform, objects: list[SlotObject], *, region: Region | None = None) -> bool:
    """Whether the bucket holds every tile a frame needs: all of the
    platform's, or every one reaching into the source's region."""
    needed = needed_tiles(platform, region)
    if needed is None:
        return len(objects) >= platform.tile_count
    return needed <= {item.tile for item in objects}


def covering_objects(platform: Platform, objects: list[SlotObject], *, region: Region | None = None) -> list[SlotObject]:
    """The slot's objects a frame on ``region`` reads: those of the
    needed tiles, or all of them."""
    needed = needed_tiles(platform, region)
    return objects if needed is None else [item for item in objects if item.tile in needed]


def latest_slot(
    platform: Platform,
    channel: Channel,
    *,
    now: datetime | None = None,
    fetch: Callable[[str], str] | None = None,
    cadence_seconds: int | None = None,
    region: Region | None = None,
) -> datetime:
    """The newest slot whose tiles have all landed for the channel: the
    end of the live window. The reader lists the newest few slots in as
    few requests as its product allows (a day's slot directories at once
    for ISatSS, the newest hour directory for CMIPF, one search for FCI;
    yesterday's as well around midnight), and each is asked for its
    tiles, since a slot appears while its tiles are still being written.
    A source on a coarser cadence than the scans' asks for more and keeps
    the slots on its cadence. A source on a region asks only for the
    tiles reaching into it. Nothing in the last day is a feed that is
    down."""
    reader = reader_for(platform)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cadence = source_cadence(platform, cadence_seconds)
    # A slot's tiles land within a minute or two of the directory
    # appearing; a few slots back is as far as an incomplete run of them
    # plausibly reaches, and the listing beyond that is trusted.
    listed = reader.recent_slots(platform, current, limit=RECENT_SLOTS * (cadence // platform.cadence_seconds), fetch=fetch)
    slots = [slot for slot in listed if on_cadence(slot, cadence)][:RECENT_SLOTS]
    for index, slot in enumerate(slots):
        if index + 1 >= RECENT_SLOTS or slot_is_complete(
            platform, reader.list_slot(platform, channel, slot, fetch=fetch), region=region
        ):
            return slot
    raise DownloadError(f"{platform.spacecraft} lists no complete {channel.id} slot in the last two days")


@dataclass(frozen=True)
class FetchedFrame:
    slot: datetime
    path: Path
    tiles: int
    """How many tiles were fetched for it this round: 0 for a cache hit."""


def fetch_frame(
    platform: Platform,
    channel: Channel,
    slot: datetime,
    *,
    grid: TargetGrid,
    frames_dir: Path,
    tiles_dir: Path,
    force: bool = False,
    fetch: Callable[[str], str] | None = None,
    download: Callable[[str], bytes] | None = None,
    concurrency: int = FETCH_CONCURRENCY,
    projector: str = "gdalwarp",
    region: Region | None = None,
) -> FetchedFrame | None:
    """One slot of one channel as a frame on the target grid, from the
    cache when it is there, else from the bucket: listed, fetched when
    complete, opened by the reader, warped by the projector, and the tiles
    deleted once the frame is written. With ``region``, only the tiles
    reaching into it are needed and fetched. None when the bucket does not
    hold the whole slot (or the region's part of it) yet."""
    frame = assemble.frame_path(frames_dir, channel, slot)
    if frame.is_file() and not force:
        return FetchedFrame(slot=slot, path=frame, tiles=0)
    reader = reader_for(platform)
    objects = reader.list_slot(platform, channel, slot, fetch=fetch)
    if not slot_is_complete(platform, objects, region=region):
        if objects:
            needed = needed_tiles(platform, region)
            LOG.info(
                "%s %s %s: %d of %d tiles on the bucket, leaving the slot out",
                platform.spacecraft,
                channel.id,
                slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                len(covering_objects(platform, objects, region=region)),
                platform.tile_count if needed is None else len(needed),
            )
        return None
    objects = covering_objects(platform, objects, region=region)
    # A slot's files are fetched under a directory per channel when each
    # channel is its own files, and shared when one file carries them all;
    # the directory is the window's to remove once the slot is done
    # (`fetch_window`), so a second channel finds the shared files there.
    slot_dir = slot_directory(tiles_dir, slot) / (channel.id if reader.files_per_channel else "shared")
    files = reader.download(platform, objects, slot_dir, download=download, concurrency=concurrency)
    source = reader.open(files, slot_dir, channel=channel, missing_below=channel.missing_below)
    # The packing is the source's, read off one tile before the warp: a
    # mosaic or a warp carries a band's scale, offset and unit through only
    # on some GDAL versions (3.13 does, Ubuntu 24.04's 3.8 loses the unit).
    packing = assemble.dataset_packing(reader.packing_source(files, channel=channel))
    PROJECTORS[projector].to_grid(source, grid, nodata=assemble.NODATA, resampling=resampling_for(channel), out=frame)
    assemble.write_packing(
        frame,
        packing,
        slot=slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
        tiles=len(objects),
        keys=[item.key for item in objects],
        projector=projector,
        resampling=resampling_for(channel),
        kind=channel.kind,
    )
    if reader.files_per_channel:
        shutil.rmtree(slot_dir, ignore_errors=True)
    LOG.info("%s %s: warped %s from %d tiles", platform.spacecraft, channel.id, frame.name, len(objects))
    return FetchedFrame(slot=slot, path=frame, tiles=len(objects))


def slot_directory(tiles_dir: Path, slot: datetime) -> Path:
    """Where a slot's downloaded files live while it is being warped."""
    return tiles_dir / f"{slot:%Y%m%d%H%M}"


def produce_frames(
    platform: Platform,
    producer: Producer,
    slot: datetime,
    inputs: dict[str, Path],
    *,
    grid: TargetGrid,
    frames_dir: Path,
    ancillary_root: Path | None = None,
    force: bool = False,
) -> dict[str, Path]:
    """One slot's outputs of one producer as frames, from the cache when
    every output is there (and carries this producer's version), else
    computed from the slot's input frames and written beside them. The
    inputs must be every channel the producer reads; a producer that
    reads ancillary fields resolves them under ``ancillary_root`` first
    (:meth:`Producer.ancillary_for`), and what it read is named in the
    frames' sidecars. A cached frame is never recomputed for a newer
    ancillary: the first computation of a slot is the slot's."""
    outputs = {output_id: assemble.frame_path(frames_dir, output_id, slot) for output_id in producer.outputs}
    version = producer.version
    if not force and all(path.is_file() for path in outputs.values()):
        if all(assemble.frame_packing(path).producer == (producer.id, version) for path in outputs.values()):
            return outputs
        LOG.info("%s %s: the cached %s frames are another version's, recomposing", platform.spacecraft, slot.strftime("%Y-%m-%dT%H:%M:%SZ"), producer.bundle_id)
    needed = producer.inputs_for(platform)
    missing = [channel_id for channel_id in needed if channel_id not in inputs]
    if missing:
        raise ConversionError(f"the {producer.bundle_id} producer needs {list(needed)}; the slot lacks {missing}")
    ancillary = producer.ancillary_for(platform, slot, ancillary_root) if producer.ancillaries else {}
    planes = {channel_id: assemble.read_frame(inputs[channel_id], grid) for channel_id in needed}
    produced = producer.run(platform, planes, ancillary, slot=slot, grid=grid)
    if tuple(produced) != producer.outputs:
        raise ConversionError(f"the {producer.bundle_id} producer returned {list(produced)}, not {list(producer.outputs)}")
    packing = assemble.Packing(scale=PRODUCED_SCALE, offset=0.0, unit=PRODUCED_UNIT, producer=(producer.id, version))
    extra: dict[str, object] = {
        "slot": slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "inputs": [inputs[channel_id].name for channel_id in needed],
    }
    if ancillary:
        extra["ancillary"] = {name: path.name for name, path in ancillary.items()}
    for output_id, plane in produced.items():
        assemble.write_frame(plane, grid, packing, outputs[output_id], **extra)
    LOG.info("%s %s: composed %s from %s", platform.spacecraft, producer.bundle_id, slot.strftime("%Y-%m-%dT%H:%M:%SZ"), ", ".join(needed))
    return outputs


#: How a produced frame is packed: every producer's output is a plain
#: number in 0–1 (a gun, a confidence), stored to four decimals.
PRODUCED_SCALE = 0.0001
PRODUCED_UNIT = "1"


@dataclass(frozen=True)
class FetchedSlot:
    slot: datetime
    frames: dict[str, Path]
    """Every variable's frame for the slot, channels and produced alike."""
    tiles: int
    """How many tiles were fetched for it this round, over every channel:
    0 for a cache hit."""


@dataclass(frozen=True)
class FetchedWindow:
    series: dict[str, Path]
    """One series file per variable, by id, in the order the channels and
    then the producers' outputs were asked for."""
    slots: list[FetchedSlot]
    grid: TargetGrid


def fetch_window(
    platform: Platform,
    channels: tuple[Channel, ...],
    run_time: datetime,
    hours: int,
    *,
    grid: TargetGrid,
    raw_root: Path,
    destination: Path,
    series_stem: str,
    units: dict[str, str],
    producers: tuple[Producer, ...] = (),
    ancillary_root: Path | None = None,
    cadence_seconds: int | None = None,
    region: Region | None = None,
    source_id: str | None = None,
    force: bool = False,
    fetch: Callable[[str], str] | None = None,
    download: Callable[[str], bytes] | None = None,
    concurrency: int = FETCH_CONCURRENCY,
    series_concurrency: int = SERIES_CONCURRENCY,
) -> FetchedWindow:
    """The window's frames of every channel, each from the cache or the
    bucket, the producers' outputs composed from them per slot, and every
    variable stacked into its own NetCDF series at
    ``destination / <series_stem>.<variable>.nc``. A slot is in the window
    only when every channel is there whole, so every series carries the
    same axis. ``units`` is each channel's declared unit (the registry's
    output unit), which its frames must agree with; ``cadence_seconds`` the
    source's, when coarser than the platform's scans; ``ancillary_root``
    where a producer's ancillary fields are staged and cached
    (``<raw root>/ancillary``); ``region`` the source's box when it
    publishes less than the disk, so a slot fetches only the tiles reaching
    into it; ``source_id`` the source the frames are cached under (the
    platform's role, which is the full-disk source's id, by default)."""
    for producer in producers:
        missing = [channel_id for channel_id in producer.inputs_for(platform) if channel_id not in {channel.id for channel in channels}]
        if missing:
            raise ConversionError(f"the {producer.bundle_id} producer reads {missing}, which the window does not fetch")
    frames_dir = raw_root / assemble.frames_dirname(source_id or platform.role)
    tiles_dir = destination / "tiles"
    fetched: list[FetchedSlot] = []
    for slot in window_slots(platform, run_time, hours, cadence_seconds=cadence_seconds):
        frames: dict[str, Path] = {}
        tiles = 0
        try:
            for channel in channels:
                frame = fetch_frame(
                    platform,
                    channel,
                    slot,
                    grid=grid,
                    frames_dir=frames_dir,
                    tiles_dir=tiles_dir,
                    force=force,
                    fetch=fetch,
                    download=download,
                    concurrency=concurrency,
                    region=region,
                )
                if frame is None:
                    break
                frames[channel.id] = frame.path
                tiles += frame.tiles
        finally:
            # The slot's downloads (shared by its channels, or what a
            # failed channel left) go before the next slot's arrive: a
            # window of chunks would not fit a runner otherwise.
            shutil.rmtree(slot_directory(tiles_dir, slot), ignore_errors=True)
        if len(frames) != len(channels):
            continue
        for producer in producers:
            frames.update(
                produce_frames(platform, producer, slot, frames, grid=grid, frames_dir=frames_dir, ancillary_root=ancillary_root, force=force)
            )
        fetched.append(FetchedSlot(slot=slot, frames=frames, tiles=tiles))
    shutil.rmtree(tiles_dir, ignore_errors=True)
    if not fetched:
        raise DownloadError(
            f"{platform.spacecraft} holds no complete {', '.join(channel.id for channel in channels)} slot for the window from "
            f"{run_time.strftime('%Y-%m-%dT%H:%M:%SZ')} through +{hours} h"
        )
    destination.mkdir(parents=True, exist_ok=True)
    variables = [assemble.SeriesVariable.channel(platform, channel, units[channel.id]) for channel in channels]
    for producer in producers:
        variables += [
            assemble.SeriesVariable(
                id=output_id, unit=PRODUCED_UNIT, long_name=f"{producer.bundle_id} {output_id}, {producer.id} {producer.version}"
            )
            for output_id in producer.outputs
        ]
    def write(variable: assemble.SeriesVariable) -> Path:
        out = assemble.series_path(destination, series_stem, variable.id)
        assemble.write_series(
            [assemble.Frame(slot=item.slot, path=item.frames[variable.id]) for item in fetched],
            variable=variable,
            platform=platform,
            grid=grid,
            run_time=run_time,
            out=out,
        )
        return out

    # Each series is its own subprocess and its own files, so the order the
    # writes finish in changes nothing; the mapping keeps the variables'
    # order and the first failure in that order is the one raised.
    with ThreadPoolExecutor(max_workers=max(1, series_concurrency)) as pool:
        futures = [pool.submit(write, variable) for variable in variables]
        try:
            outs = [future.result() for future in futures]
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    series = {variable.id: out for variable, out in zip(variables, outs)}
    return FetchedWindow(series=series, slots=fetched, grid=grid)

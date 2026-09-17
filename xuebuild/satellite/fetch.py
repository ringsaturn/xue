"""One window of a satellite source: list, fetch, warp, cache, stack.

The shape is the MRMS and JMA feeds' (``xuebuild/fetch.py``): a run is a
window named by its first hour, its slots every ``cadence_seconds`` from
that hour through ``hours`` past it, and a slot the bucket has not
finished (fewer tiles than the platform's count) or never wrote is left
out — the axis allows the gap, and the next round takes it if it lands.
What is new is the frame cache: a slot is fetched and warped once, into
``<raw_root>/<role>-frames/<channel>/``, and every round after that reads
the frame back, so a round costs one slot's tiles (26 MB for a Himawari
channel) however long the window. A producer (:mod:`producers`) runs on a
slot the same way: once, on the slot's warped channels, its outputs cached
as frames of their own; so a window of four channels and a composite costs
a round four channels' tiles and one composition.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
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
#: How many of the newest listed slots `latest_slot` asks for their tiles
#: before trusting the listing.
RECENT_SLOTS = 3
#: Bilinear for a continuous quantity (brightness temperature,
#: reflectance); a categorical product would take ``near``.
RESAMPLING = "bilinear"


def target_grid(platform: Platform, step: float) -> TargetGrid:
    """The published grid: the platform's region at ``step``."""
    west, south, east, north = platform.region
    return TargetGrid(west=west, south=south, east=east, north=north, step=step)


def window_slots(platform: Platform, start: datetime, hours: int) -> list[datetime]:
    """Every scan slot from ``start`` through ``hours`` past it, inclusive."""
    end = start + timedelta(hours=hours)
    slots: list[datetime] = []
    slot = start
    while slot <= end:
        slots.append(slot)
        slot += timedelta(seconds=platform.cadence_seconds)
    return slots


def slot_is_complete(platform: Platform, objects: list[SlotObject]) -> bool:
    return len(objects) >= platform.tile_count


def latest_slot(
    platform: Platform,
    channel: Channel,
    *,
    now: datetime | None = None,
    fetch: Callable[[str], str] | None = None,
) -> datetime:
    """The newest slot whose tiles have all landed for the channel: the
    end of the live window. The reader lists the newest few slots in as
    few requests as its product allows (a day's slot directories at once
    for ISatSS, the newest hour directory for CMIPF; yesterday's as well
    around midnight), and each is asked for its tiles, since a slot
    appears while its tiles are still being written. Nothing in the last
    day is a feed that is down."""
    reader = reader_for(platform)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    # A slot's tiles land within a minute or two of the directory
    # appearing; a few slots back is as far as an incomplete run of them
    # plausibly reaches, and the listing beyond that is trusted.
    slots = reader.recent_slots(platform, current, limit=RECENT_SLOTS, fetch=fetch)
    for index, slot in enumerate(slots):
        if index + 1 >= RECENT_SLOTS or slot_is_complete(platform, reader.list_slot(platform, channel, slot, fetch=fetch)):
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
) -> FetchedFrame | None:
    """One slot of one channel as a frame on the target grid, from the
    cache when it is there, else from the bucket: listed, fetched when
    complete, opened by the reader, warped by the projector, and the tiles
    deleted once the frame is written. None when the bucket does not hold
    the whole slot yet."""
    frame = assemble.frame_path(frames_dir, channel, slot)
    if frame.is_file() and not force:
        return FetchedFrame(slot=slot, path=frame, tiles=0)
    reader = reader_for(platform)
    objects = reader.list_slot(platform, channel, slot, fetch=fetch)
    if not slot_is_complete(platform, objects):
        if objects:
            LOG.info(
                "%s %s %s: %d of %d tiles on the bucket, leaving the slot out",
                platform.spacecraft,
                channel.id,
                slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
                len(objects),
                platform.tile_count,
            )
        return None
    slot_dir = tiles_dir / f"{slot:%Y%m%d%H%M}" / channel.id
    files = reader.download(platform, objects, slot_dir, download=download, concurrency=concurrency)
    source = reader.open(files, slot_dir, missing_below=channel.missing_below)
    # The packing is the source's, read off one tile before the warp: a
    # mosaic or a warp carries a band's scale, offset and unit through only
    # on some GDAL versions (3.13 does, Ubuntu 24.04's 3.8 loses the unit).
    packing = assemble.dataset_packing(reader.packing_source(files))
    PROJECTORS[projector].to_grid(source, grid, nodata=assemble.NODATA, resampling=RESAMPLING, out=frame)
    assemble.write_packing(
        frame,
        packing,
        slot=slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
        tiles=len(objects),
        keys=[item.key for item in objects],
        projector=projector,
    )
    shutil.rmtree(slot_dir, ignore_errors=True)
    LOG.info("%s %s: warped %s from %d tiles", platform.spacecraft, channel.id, frame.name, len(objects))
    return FetchedFrame(slot=slot, path=frame, tiles=len(objects))


def produce_frames(
    platform: Platform,
    producer: Producer,
    slot: datetime,
    inputs: dict[str, Path],
    *,
    grid: TargetGrid,
    frames_dir: Path,
    force: bool = False,
) -> dict[str, Path]:
    """One slot's outputs of one producer as frames, from the cache when
    every output is there (and carries this producer's version), else
    computed from the slot's input frames and written beside them. The
    inputs must be every channel the producer reads."""
    outputs = {output_id: assemble.frame_path(frames_dir, output_id, slot) for output_id in producer.outputs}
    version = producer.version
    if not force and all(path.is_file() for path in outputs.values()):
        if all(assemble.frame_packing(path).producer == (producer.id, version) for path in outputs.values()):
            return outputs
        LOG.info("%s %s: the cached %s frames are another version's, recomposing", platform.spacecraft, slot.strftime("%Y-%m-%dT%H:%M:%SZ"), producer.bundle_id)
    missing = [channel_id for channel_id in producer.inputs if channel_id not in inputs]
    if missing:
        raise ConversionError(f"the {producer.bundle_id} producer needs {list(producer.inputs)}; the slot lacks {missing}")
    planes = {channel_id: assemble.read_frame(inputs[channel_id], grid) for channel_id in producer.inputs}
    produced = producer.run(platform, planes, {})
    if tuple(produced) != producer.outputs:
        raise ConversionError(f"the {producer.bundle_id} producer returned {list(produced)}, not {list(producer.outputs)}")
    packing = assemble.Packing(scale=PRODUCED_SCALE, offset=0.0, unit=PRODUCED_UNIT, producer=(producer.id, version))
    for output_id, plane in produced.items():
        assemble.write_frame(
            plane,
            grid,
            packing,
            outputs[output_id],
            slot=slot.strftime("%Y-%m-%dT%H:%M:%SZ"),
            inputs=[inputs[channel_id].name for channel_id in producer.inputs],
        )
    LOG.info("%s %s: composed %s from %s", platform.spacecraft, producer.bundle_id, slot.strftime("%Y-%m-%dT%H:%M:%SZ"), ", ".join(producer.inputs))
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
    force: bool = False,
    fetch: Callable[[str], str] | None = None,
    download: Callable[[str], bytes] | None = None,
    concurrency: int = FETCH_CONCURRENCY,
) -> FetchedWindow:
    """The window's frames of every channel, each from the cache or the
    bucket, the producers' outputs composed from them per slot, and every
    variable stacked into its own NetCDF series at
    ``destination / <series_stem>.<variable>.nc``. A slot is in the window
    only when every channel is there whole, so every series carries the
    same axis. ``units`` is each channel's declared unit (the registry's
    output unit), which its frames must agree with."""
    for producer in producers:
        missing = [channel_id for channel_id in producer.inputs if channel_id not in {channel.id for channel in channels}]
        if missing:
            raise ConversionError(f"the {producer.bundle_id} producer reads {missing}, which the window does not fetch")
    frames_dir = raw_root / assemble.frames_dirname(platform)
    tiles_dir = destination / "tiles"
    fetched: list[FetchedSlot] = []
    for slot in window_slots(platform, run_time, hours):
        frames: dict[str, Path] = {}
        tiles = 0
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
            )
            if frame is None:
                break
            frames[channel.id] = frame.path
            tiles += frame.tiles
        if len(frames) != len(channels):
            continue
        for producer in producers:
            frames.update(produce_frames(platform, producer, slot, frames, grid=grid, frames_dir=frames_dir, force=force))
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
    series: dict[str, Path] = {}
    for variable in variables:
        out = assemble.series_path(destination, series_stem, variable.id)
        assemble.write_series(
            [assemble.Frame(slot=item.slot, path=item.frames[variable.id]) for item in fetched],
            variable=variable,
            platform=platform,
            grid=grid,
            run_time=run_time,
            out=out,
        )
        series[variable.id] = out
    return FetchedWindow(series=series, slots=fetched, grid=grid)

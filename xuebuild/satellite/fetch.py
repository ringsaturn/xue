"""One window of a satellite source: list, fetch, warp, cache, stack.

The shape is the MRMS and JMA feeds' (``xuebuild/fetch.py``): a run is a
window named by its first hour, its slots every ``cadence_seconds`` from
that hour through ``hours`` past it, and a slot the bucket has not
finished (fewer tiles than the platform's count) or never wrote is left
out — the axis allows the gap, and the next round takes it if it lands.
What is new is the frame cache: a slot is fetched and warped once, into
``<raw_root>/<role>-frames/<channel>/``, and every round after that reads
the frame back, so a round costs one slot's tiles (26 MB for a Himawari
channel) however long the window.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..errors import DownloadError
from . import assemble
from .platforms import Channel, Platform
from .projector import PROJECTORS, TargetGrid
from .readers import SlotObject, reader_for

LOG = logging.getLogger(__name__)

#: Tiles fetched at once from the bucket; S3 answers bursts without
#: throttling and a tile is a few hundred kilobytes.
FETCH_CONCURRENCY = 8
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
    end of the live window. The bucket lists a day's slot directories in
    one request; the newest few are asked for their tiles, since a slot
    appears while its tiles are still being written. Today's day is
    tried, then yesterday's (the answer is the same either side of
    midnight); nothing in two days is a feed that is down."""
    reader = reader_for(platform)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    day = current.replace(hour=0, minute=0, second=0, microsecond=0)
    for listed_day in (day, day - timedelta(days=1)):
        slots = [slot for slot in reader.list_slots(platform, listed_day, fetch=fetch) if slot <= current]
        # A slot's tiles land within a minute or two of the directory
        # appearing; a few slots back is as far as an incomplete run of
        # them plausibly reaches, and the listing beyond that is trusted.
        for index, slot in enumerate(reversed(slots)):
            if index >= 3 or slot_is_complete(platform, reader.list_slot(platform, channel, slot, fetch=fetch)):
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


@dataclass(frozen=True)
class FetchedWindow:
    series: Path
    frames: list[FetchedFrame]
    grid: TargetGrid


def fetch_window(
    platform: Platform,
    channel: Channel,
    run_time: datetime,
    hours: int,
    *,
    grid: TargetGrid,
    raw_root: Path,
    destination: Path,
    series_name: str,
    unit: str,
    force: bool = False,
    fetch: Callable[[str], str] | None = None,
    download: Callable[[str], bytes] | None = None,
    concurrency: int = FETCH_CONCURRENCY,
) -> FetchedWindow:
    """The window's frames, each from the cache or the bucket, stacked
    into the NetCDF series at ``destination / series_name``."""
    frames_dir = raw_root / assemble.frames_dirname(platform)
    tiles_dir = destination / "tiles"
    fetched: list[FetchedFrame] = []
    for slot in window_slots(platform, run_time, hours):
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
        if frame is not None:
            fetched.append(frame)
    shutil.rmtree(tiles_dir, ignore_errors=True)
    if not fetched:
        raise DownloadError(
            f"{platform.spacecraft} holds no complete {channel.id} slot for the window from "
            f"{run_time.strftime('%Y-%m-%dT%H:%M:%SZ')} through +{hours} h"
        )
    destination.mkdir(parents=True, exist_ok=True)
    series = destination / series_name
    assemble.write_series(
        [assemble.Frame(slot=frame.slot, path=frame.path) for frame in fetched],
        channel=channel,
        platform=platform,
        grid=grid,
        run_time=run_time,
        unit=unit,
        out=series,
    )
    return FetchedWindow(series=series, frames=fetched, grid=grid)

"""Readers: how a platform's files are listed, fetched and opened.

A reader's responsibility ends at *one thing GDAL can open* — a dataset
with a geostationary CRS whose values are the physical quantity or carry a
scale and offset to it. Putting it on a regular grid is the projector's
job (:mod:`projector`), and nothing downstream of the projector knows
which reader produced the frame. That is the seam a second file format
goes through: the ISatSS tiles are one reader, the GOES ``CMIPF`` single
file another, the MTG ``FCI`` chunks (radiances in a grouped, filtered
netCDF that GDAL cannot georeference alone) a third, and the raw HSD
segments a fourth if the ISatSS product ever stops — each a class here,
chosen by :attr:`~platforms.Platform.reader`.

The bucket listing and the downloads go through the pipeline's own HTTP
helpers (:mod:`xuebuild.fetch`), imported lazily because that module
dispatches to this package, and the EUMETSAT Data Store through
:mod:`eumetsat`; every function takes a ``fetch`` / ``download`` callable
so the tests never touch the network.
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, Protocol
from xml.sax.saxutils import escape

import numpy as np

from ..errors import ConversionError, DownloadError
from ..gdal import require_command, run_command
from . import eumetsat
from .platforms import Channel, Platform

_S3_NAMESPACE = "{http://s3.amazonaws.com/doc/2006-03-01/}"


@dataclass(frozen=True)
class SlotObject:
    """One object of one slot on the bucket."""

    key: str
    size: int
    tile: int
    """The tile number within the slot (1 for a single-file product)."""
    created: datetime
    """When the producer wrote it, off the key: a reissued tile is the
    later one."""
    url: str = ""
    """Where to download it when that is not the bucket's key (a Data
    Store entry's own link); empty for an object on a bucket."""


@dataclass(frozen=True)
class SlotFiles:
    """One slot of one channel, on disk."""

    slot: datetime
    paths: tuple[Path, ...]


def bucket_url(bucket: str) -> str:
    return f"https://{bucket}.s3.amazonaws.com"


def _fetch_text(url: str) -> str:
    from ..fetch import fetch_text  # noqa: PLC0415 - fetch.py dispatches here

    return fetch_text(url)


def _download_bytes(url: str) -> bytes:
    from ..fetch import _read_response, _request  # noqa: PLC0415 - fetch.py dispatches here

    response = _request(url)
    with response:
        status = getattr(response, "status", None)
        if status != 200:
            raise DownloadError(f"expected HTTP 200 for {url}, received {status}")
        return _read_response(response)


def list_prefix(
    bucket: str, prefix: str, *, delimiter: str | None = None, fetch: Callable[[str], str] | None = None
) -> tuple[list[tuple[str, int]], list[str]]:
    """One S3 ``list-type=2`` listing under ``prefix``, every page: the
    objects as ``(key, size)`` and, with a delimiter, the common prefixes
    (the "directories") one level down."""
    import urllib.parse  # noqa: PLC0415

    fetch = fetch or _fetch_text
    objects: list[tuple[str, int]] = []
    prefixes: list[str] = []
    token: str | None = None
    while True:
        query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if delimiter:
            query["delimiter"] = delimiter
        if token is not None:
            query["continuation-token"] = token
        payload = fetch(f"{bucket_url(bucket)}/?{urllib.parse.urlencode(query)}")
        try:
            root = ET.fromstring(payload)
        except ET.ParseError as exc:
            raise DownloadError(f"{bucket} listing is not XML: {exc}") from exc
        for contents in root.iter(f"{_S3_NAMESPACE}Contents"):
            key = contents.findtext(f"{_S3_NAMESPACE}Key") or ""
            try:
                size = int(contents.findtext(f"{_S3_NAMESPACE}Size") or "0")
            except ValueError:
                size = 0
            objects.append((key, size))
        for common in root.iter(f"{_S3_NAMESPACE}CommonPrefixes"):
            prefixes.append(common.findtext(f"{_S3_NAMESPACE}Prefix") or "")
        token = None
        if (root.findtext(f"{_S3_NAMESPACE}IsTruncated") or "").lower() == "true":
            token = root.findtext(f"{_S3_NAMESPACE}NextContinuationToken") or None
            if token is None:
                raise DownloadError(f"{bucket} listing is truncated but carries no continuation token")
        if token is None:
            return objects, prefixes


class Reader(Protocol):
    """One product family's files, from the bucket to a GDAL dataset."""

    files_per_channel: ClassVar[bool]
    """Whether a slot's files are one channel's (ISatSS tiles, a CMIPF
    file), fetched under a directory per channel, or carry every channel
    (an FCI chunk), fetched once per slot and shared."""

    def list_slots(self, platform: Platform, day: datetime, *, fetch: Callable[[str], str] | None = None) -> list[datetime]:
        """The scan slots the bucket has a directory for on one UTC day,
        in time order; a slot listed may still be incomplete."""
        ...

    def recent_slots(
        self, platform: Platform, now: datetime, *, limit: int, fetch: Callable[[str], str] | None = None
    ) -> list[datetime]:
        """The newest ``limit`` slots at or before ``now`` that the bucket
        lists, newest first, in as few requests as the product's layout
        allows; a slot listed may still be incomplete. Nothing in the last
        day is a feed that is down."""
        ...

    def list_slot(
        self, platform: Platform, channel: Channel, slot: datetime, *, fetch: Callable[[str], str] | None = None
    ) -> list[SlotObject]:
        """The objects of one channel of one slot, one per tile (a
        reissued tile resolves to the later one), in tile order."""
        ...

    def download(
        self,
        platform: Platform,
        objects: list[SlotObject],
        into: Path,
        *,
        download: Callable[[str], bytes] | None = None,
        concurrency: int = 8,
    ) -> SlotFiles:
        """The objects on disk under ``into``, fetched unless already there
        at their listed size."""
        ...

    def open(
        self, files: SlotFiles, workdir: Path, *, channel: Channel | None = None, missing_below: float | None = None
    ) -> Path:
        """One GDAL-openable dataset over the slot's files — a connection
        string, not necessarily a file — written under ``workdir``. Cells
        below ``missing_below`` (in the quantity's unit) read as no data.
        ``channel`` says which channel to open when the files carry
        several; a reader whose files are one channel's ignores it."""
        ...

    def packing_source(self, files: SlotFiles, *, channel: Channel | None = None) -> Path:
        """One of the slot's files as GDAL opens it, for the band's scale,
        offset and unit: read off a source file directly, since a mosaic
        carries them through only on some GDAL versions."""
        ...


# OR_HFD-<res>-B<bits>-M1C<channel>-T<tile>_<spacecraft>_s<start>_c<created>.nc
_ISATSS_KEY = re.compile(
    r"OR_HFD-(?P<resolution>\d{3})-B(?P<bits>\d{2})-M1C(?P<channel>\d{2})-T(?P<tile>\d{3})"
    r"_(?P<spacecraft>[A-Z0-9]+)_s(?P<start>\d{14})_c(?P<created>\d{14})\.nc$"
)
_ISATSS_DIRECTORY = re.compile(r"/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/(?P<hhmm>\d{4})/$")


@dataclass(frozen=True)
class ISatSSKey:
    resolution: int
    """Hundreds of metres: 020 is 2 km."""
    bits: int
    channel: int
    tile: int
    spacecraft: str
    start: datetime
    created: datetime


def _stamp(text: str) -> datetime:
    """``YYYYDDDHHMMSSs``, the day of year and a tenth of a second."""
    return datetime.strptime(text[:13], "%Y%j%H%M%S").replace(tzinfo=UTC)


def parse_isatss_key(key: str) -> ISatSSKey | None:
    match = _ISATSS_KEY.search(key)
    if not match:
        return None
    try:
        return ISatSSKey(
            resolution=int(match.group("resolution")),
            bits=int(match.group("bits")),
            channel=int(match.group("channel")),
            tile=int(match.group("tile")),
            spacecraft=match.group("spacecraft"),
            start=_stamp(match.group("start")),
            created=_stamp(match.group("created")),
        )
    except ValueError:
        return None


class ISatSSReader:
    """NOAA's ISatSS product: each channel of each full-disk scan as 88
    Sectorized CMI NetCDF tiles (550 x 550 at 2 km), already calibrated —
    brightness temperature in kelvin, or reflectance — on the CF
    ``geostationary`` projection with x/y in microradians (GDAL warns about
    the unit and computes the geotransform right). The tiles' georeference
    is what mosaics them: a VRT over the tiles is the slot."""

    VARIABLE = "Sectorized_CMI"
    files_per_channel: ClassVar[bool] = True

    def slot_prefix(self, platform: Platform, channel: Channel, slot: datetime) -> str:
        """Every tile of the channel in the slot's directory shares this
        prefix; the bit depth field is the channel's own, so the listing
        never needs to guess it."""
        return (
            f"{platform.prefix}/{slot:%Y/%m/%d/%H%M}/"
            f"OR_HFD-{int(round(channel.resolution_km * 10)):03d}-B{channel.bit_depth:02d}-M1C{channel.band:02d}-"
        )

    def list_slots(self, platform: Platform, day: datetime, *, fetch: Callable[[str], str] | None = None) -> list[datetime]:
        """The day's full-disk slots. The prefix also holds the rapid
        scans (``OR_HR3-*``, the target area every two and a half minutes)
        under directories of their own — ``0002``, ``0004``, ``0007``,
        ``0009`` between ``0000`` and ``0010`` — so a directory counts only
        on the imager's full-disk cadence; the others never hold a full
        disk, and four of every five directories are theirs, so a newest-
        few listing that kept them would rarely reach a full disk."""
        _, prefixes = list_prefix(platform.bucket, f"{platform.prefix}/{day:%Y/%m/%d}/", delimiter="/", fetch=fetch)
        slots: list[datetime] = []
        for prefix in prefixes:
            match = _ISATSS_DIRECTORY.search(prefix)
            if not match:
                continue
            try:
                slot = datetime(
                    int(match.group("year")),
                    int(match.group("month")),
                    int(match.group("day")),
                    int(match.group("hhmm")[:2]),
                    int(match.group("hhmm")[2:]),
                    tzinfo=UTC,
                )
            except ValueError:
                continue
            if int(slot.timestamp()) % platform.cadence_seconds == 0:
                slots.append(slot)
        return sorted(slots)

    def recent_slots(
        self, platform: Platform, now: datetime, *, limit: int, fetch: Callable[[str], str] | None = None
    ) -> list[datetime]:
        """A day's slot directories are one listing: today's, and
        yesterday's when today has too few (the answer is the same either
        side of midnight)."""
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        slots: list[datetime] = []
        for listed_day in (day, day - timedelta(days=1)):
            slots += [slot for slot in reversed(self.list_slots(platform, listed_day, fetch=fetch)) if slot <= now]
            if len(slots) >= limit:
                break
        return slots[:limit]

    def list_slot(
        self, platform: Platform, channel: Channel, slot: datetime, *, fetch: Callable[[str], str] | None = None
    ) -> list[SlotObject]:
        objects, _ = list_prefix(platform.bucket, self.slot_prefix(platform, channel, slot), fetch=fetch)
        by_tile: dict[int, SlotObject] = {}
        for key, size in objects:
            parsed = parse_isatss_key(key)
            if parsed is None or parsed.channel != channel.band:
                continue
            candidate = SlotObject(key=key, size=size, tile=parsed.tile, created=parsed.created)
            held = by_tile.get(parsed.tile)
            if held is None or candidate.created > held.created:
                by_tile[parsed.tile] = candidate
        return [by_tile[tile] for tile in sorted(by_tile)]

    def download(
        self,
        platform: Platform,
        objects: list[SlotObject],
        into: Path,
        *,
        download: Callable[[str], bytes] | None = None,
        concurrency: int = 8,
    ) -> SlotFiles:
        if not objects:
            raise DownloadError(f"{platform.spacecraft}: nothing to download")
        download = download or _download_bytes
        into.mkdir(parents=True, exist_ok=True)
        base = bucket_url(platform.bucket)

        def one(item: SlotObject) -> Path:
            target = into / item.key.rsplit("/", 1)[-1]
            if target.is_file() and target.stat().st_size == item.size:
                return target
            payload = download(f"{base}/{item.key}")
            if item.size and len(payload) != item.size:
                raise DownloadError(f"{item.key}: received {len(payload)} bytes, listed {item.size}")
            temporary = target.with_suffix(target.suffix + ".part")
            temporary.write_bytes(payload)
            temporary.replace(target)
            return target

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            paths = tuple(pool.map(one, objects))
        slot = min(parse_isatss_key(item.key).start for item in objects)  # type: ignore[union-attr]
        return SlotFiles(slot=slot, paths=paths)

    def open(
        self, files: SlotFiles, workdir: Path, *, channel: Channel | None = None, missing_below: float | None = None
    ) -> Path:
        """A VRT mosaicking the tiles' ``Sectorized_CMI`` subdatasets by
        their georeference — the whole disk, or whatever tiles there are.
        With ``missing_below``, every source gets a lookup table that turns
        a raw code below that value into the fill, so a scan segment the
        product wrote as 0 K is no data to the warp rather than a cold
        edge the resampling smears into the neighbouring cells."""
        workdir.mkdir(parents=True, exist_ok=True)
        vrt = workdir / f"{files.slot:%Y%m%d%H%M%S}.vrt"
        sources = [f'NETCDF:"{path}":{self.VARIABLE}' for path in files.paths]
        run_command(
            ["gdalbuildvrt", "-q", "-overwrite", "-resolution", "highest", str(vrt), *sources],
            description=f"gdalbuildvrt {vrt.name}",
        )
        if missing_below is not None:
            from .assemble import NODATA, dataset_packing  # noqa: PLC0415 - assemble imports nothing from here

            packing = dataset_packing(self.packing_source(files))
            floor = int(math.floor((missing_below - packing.offset) / packing.scale))
            vrt.write_text(_mask_below(vrt.read_text(encoding="utf-8"), floor, NODATA), encoding="utf-8")
        return vrt

    def packing_source(self, files: SlotFiles, *, channel: Channel | None = None) -> Path:
        return Path(f'NETCDF:"{files.paths[0]}":{self.VARIABLE}')


def _mask_below(vrt: str, floor: int, nodata: int) -> str:
    """Rewrite a VRT's sources as ComplexSources whose lookup table maps a
    raw code at or below ``floor`` to ``nodata`` and leaves the rest as
    they are (GDAL interpolates a LUT linearly between its entries, so the
    identity is two entries and the fill one step below them)."""
    root = ET.fromstring(vrt)
    for band in root.iter("VRTRasterBand"):
        for source in list(band):
            if source.tag not in ("SimpleSource", "ComplexSource"):
                continue
            source.tag = "ComplexSource"
            if source.find("NODATA") is None:
                ET.SubElement(source, "NODATA").text = str(nodata)
            lut = source.find("LUT")
            if lut is None:
                lut = ET.SubElement(source, "LUT")
            lut.text = f"-32768:{nodata},{floor}:{nodata},{floor + 1}:{floor + 1},32767:32767"
    return ET.tostring(root, encoding="unicode") + "\n"


# OR_ABI-L2-CMIPF-M<mode>C<channel>_G<spacecraft>_s<start>_e<end>_c<created>.nc
_CMIPF_KEY = re.compile(
    r"OR_ABI-L2-CMIPF-M(?P<mode>\d)C(?P<channel>\d{2})_G(?P<spacecraft>\d{2})"
    r"_s(?P<start>\d{14})_e(?P<end>\d{14})_c(?P<created>\d{14})\.nc$"
)
_CMIPF_HOUR_DIRECTORY = re.compile(r"/(?P<year>\d{4})/(?P<day>\d{3})/(?P<hour>\d{2})/$")


@dataclass(frozen=True)
class CMIPFKey:
    mode: int
    """The ABI scan mode the file was taken in (6 today: a full disk every
    ten minutes); the listing accepts any, so a mode change never hides
    the files."""
    channel: int
    spacecraft: int
    """The number in ``G19``."""
    start: datetime
    end: datetime
    created: datetime


def parse_cmipf_key(key: str) -> CMIPFKey | None:
    match = _CMIPF_KEY.search(key)
    if not match:
        return None
    try:
        return CMIPFKey(
            mode=int(match.group("mode")),
            channel=int(match.group("channel")),
            spacecraft=int(match.group("spacecraft")),
            start=_stamp(match.group("start")),
            end=_stamp(match.group("end")),
            created=_stamp(match.group("created")),
        )
    except ValueError:
        return None


class CMIPFReader:
    """NOAA's ``ABI-L2-CMIPF`` product: each channel of each full-disk
    scan as one netCDF-4 file (``CMI``, 5424 x 5424 at 2 km for the
    infrared channels, unsigned 16-bit with the band's scale and offset
    to kelvin, 65535 where the disk is not), under
    ``ABI-L2-CMIPF/<year>/<day of year>/<hour>/``. GDAL opens
    ``NETCDF:"<file>":CMI`` on the geostationary projection (sweep x)
    with a metre geotransform straight off the file's radian coordinates;
    its warning about the radian axis unit is about the SRS's axis and
    not the georeference. A scan starts twenty seconds past the
    ten-minute mark and its slot is that mark; the file lands some ten
    minutes after the scan starts."""

    VARIABLE = "CMI"
    files_per_channel: ClassVar[bool] = True

    def hour_prefix(self, platform: Platform, hour: datetime) -> str:
        return f"{platform.prefix}/{hour:%Y/%j/%H}/"

    def slot_of(self, platform: Platform, start: datetime) -> datetime:
        """The slot a scan belongs to: its start, floored to the cadence."""
        seconds = int(start.timestamp()) // platform.cadence_seconds * platform.cadence_seconds
        return datetime.fromtimestamp(seconds, tz=UTC)

    def list_hour(
        self, platform: Platform, channel: Channel, hour: datetime, *, fetch: Callable[[str], str] | None = None
    ) -> dict[datetime, SlotObject]:
        """The channel's files under one hour directory, by slot, a
        reissued file resolving to the later ``c`` stamp."""
        prefix = f"{self.hour_prefix(platform, hour)}OR_ABI-L2-CMIPF-M"
        objects, _ = list_prefix(platform.bucket, prefix, fetch=fetch)
        by_slot: dict[datetime, SlotObject] = {}
        for key, size in objects:
            parsed = parse_cmipf_key(key)
            if parsed is None or parsed.channel != channel.band:
                continue
            slot = self.slot_of(platform, parsed.start)
            candidate = SlotObject(key=key, size=size, tile=1, created=parsed.created)
            held = by_slot.get(slot)
            if held is None or candidate.created > held.created:
                by_slot[slot] = candidate
        return by_slot

    def list_hours(self, platform: Platform, day: datetime, *, fetch: Callable[[str], str] | None = None) -> list[datetime]:
        """The hour directories the bucket lists for one UTC day, one
        request, in time order."""
        _, prefixes = list_prefix(platform.bucket, f"{platform.prefix}/{day:%Y/%j}/", delimiter="/", fetch=fetch)
        hours: list[datetime] = []
        for prefix in prefixes:
            match = _CMIPF_HOUR_DIRECTORY.search(prefix)
            if not match:
                continue
            try:
                hours.append(
                    datetime.strptime(f"{match.group('year')}{match.group('day')}{match.group('hour')}", "%Y%j%H").replace(tzinfo=UTC)
                )
            except ValueError:
                continue
        return sorted(hours)

    def list_slots(self, platform: Platform, day: datetime, *, fetch: Callable[[str], str] | None = None) -> list[datetime]:
        """The day's slots: every hour directory the bucket lists for the
        day, each listed for the platform's reference channel — up to
        twenty-five requests, so the live window's end asks
        :meth:`recent_slots`, which walks the hours newest first and
        stops."""
        channel = platform.channel(platform.reference_channel)
        slots: list[datetime] = []
        for hour in self.list_hours(platform, day, fetch=fetch):
            slots += list(self.list_hour(platform, channel, hour, fetch=fetch))
        return sorted(slots)

    def recent_slots(
        self, platform: Platform, now: datetime, *, limit: int, fetch: Callable[[str], str] | None = None
    ) -> list[datetime]:
        """One listing of the day's hour directories, then the hours
        newest first until ``limit`` slots are in hand: two or three
        requests in the ordinary case, yesterday's directory as well just
        after midnight or when the feed has stalled."""
        channel = platform.channel(platform.reference_channel)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        slots: list[datetime] = []
        for listed_day in (day, day - timedelta(days=1)):
            for hour in reversed(self.list_hours(platform, listed_day, fetch=fetch)):
                if hour > now:
                    continue
                slots += sorted((slot for slot in self.list_hour(platform, channel, hour, fetch=fetch) if slot <= now), reverse=True)
                if len(slots) >= limit:
                    return slots[:limit]
        return slots

    def list_slot(
        self, platform: Platform, channel: Channel, slot: datetime, *, fetch: Callable[[str], str] | None = None
    ) -> list[SlotObject]:
        held = self.list_hour(platform, channel, slot.replace(minute=0, second=0, microsecond=0), fetch=fetch).get(slot)
        return [held] if held is not None else []

    def download(
        self,
        platform: Platform,
        objects: list[SlotObject],
        into: Path,
        *,
        download: Callable[[str], bytes] | None = None,
        concurrency: int = 8,
    ) -> SlotFiles:
        if len(objects) != 1:
            raise DownloadError(f"{platform.spacecraft}: a CMIPF slot is one file, not {len(objects)}")
        download = download or _download_bytes
        into.mkdir(parents=True, exist_ok=True)
        item = objects[0]
        parsed = parse_cmipf_key(item.key)
        if parsed is None:
            raise DownloadError(f"{item.key} is not a CMIPF key")
        target = into / item.key.rsplit("/", 1)[-1]
        if not (target.is_file() and target.stat().st_size == item.size):
            payload = download(f"{bucket_url(platform.bucket)}/{item.key}")
            if item.size and len(payload) != item.size:
                raise DownloadError(f"{item.key}: received {len(payload)} bytes, listed {item.size}")
            temporary = target.with_suffix(target.suffix + ".part")
            temporary.write_bytes(payload)
            temporary.replace(target)
        return SlotFiles(slot=self.slot_of(platform, parsed.start), paths=(target,))

    def open(
        self, files: SlotFiles, workdir: Path, *, channel: Channel | None = None, missing_below: float | None = None
    ) -> Path:
        """A one-source VRT over the file's ``CMI`` variable, so the
        no-data floor is applied the way it is to a mosaic; the product's
        fill is already proper, so the floor only guards against a
        segment written as zero."""
        workdir.mkdir(parents=True, exist_ok=True)
        vrt = workdir / f"{files.slot:%Y%m%d%H%M%S}.vrt"
        run_command(
            ["gdalbuildvrt", "-q", "-overwrite", str(vrt), str(self.packing_source(files))],
            description=f"gdalbuildvrt {vrt.name}",
        )
        if missing_below is not None:
            from .assemble import NODATA, dataset_packing  # noqa: PLC0415 - assemble imports nothing from here

            packing = dataset_packing(self.packing_source(files))
            floor = int(math.floor((missing_below - packing.offset) / packing.scale))
            vrt.write_text(_mask_below(vrt.read_text(encoding="utf-8"), floor, NODATA), encoding="utf-8")
        return vrt

    def packing_source(self, files: SlotFiles, *, channel: Channel | None = None) -> Path:
        return Path(f'NETCDF:"{files.paths[0]}":{self.VARIABLE}')


# …FCI-1C-RRAD-FDHSI-FD--CHK-BODY---NC4E_C_EUMT_<created>_IDPFI_OPE_<start>_<end>_N__O_<cycle>_<chunk>.nc
_FCI_CHUNK = re.compile(
    r"FCI-1C-RRAD-FDHSI-FD--CHK-BODY---[A-Z0-9]+_C_EUMT_(?P<created>\d{14})_[A-Z0-9]+_[A-Z0-9]+"
    r"_(?P<start>\d{14})_(?P<end>\d{14})_N_[A-Z_]+_(?P<cycle>\d{4})_(?P<chunk>\d{4})\.nc$"
)


@dataclass(frozen=True)
class FCIChunkKey:
    created: datetime
    start: datetime
    """The chunk's own sensing start; the product's is the cycle's."""
    end: datetime
    cycle: int
    """The repeat cycle's number within the day."""
    chunk: int
    """1–40, a strip of rows from the south."""


def _fci_stamp(text: str) -> datetime:
    """``YYYYMMDDHHMMSS``."""
    return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def parse_fci_chunk(name: str) -> FCIChunkKey | None:
    match = _FCI_CHUNK.search(name)
    if not match:
        return None
    try:
        return FCIChunkKey(
            created=_fci_stamp(match.group("created")),
            start=_fci_stamp(match.group("start")),
            end=_fci_stamp(match.group("end")),
            cycle=int(match.group("cycle")),
            chunk=int(match.group("chunk")),
        )
    except ValueError:
        return None


#: The FCI Level 1c file's group per channel, in the platform table's band
#: order (``FCI_CHANNELS``): ``/data/<group>/measured/effective_radiance``
#: is the channel's radiance.
FCI_GROUPS: tuple[str, ...] = (
    "vis_04", "vis_05", "vis_06", "vis_08", "vis_09", "nir_13", "nir_16", "nir_22",
    "ir_38", "wv_63", "wv_73", "ir_87", "ir_97", "ir_105", "ir_123", "ir_133",
)
#: The full-disk 2 km grid the infrared channels are on: rows and columns,
#: and how the file packs a row or column number into a scan angle
#: (``y = offset + step × row``, radians; the same magnitudes for ``x``
#: with the sign reversed). A chunk is one fortieth of the rows; the
#: reader checks these against the file it opens, so a product change is
#: an error and not a silent misplacement.
FCI_IR_GRID = 5568
FCI_IR_ANGLE_STEP = 5.58871526031607e-05
FCI_IR_ANGLE_OFFSET = -0.155617776423501
#: How the reader packs a brightness temperature strip: Int16 steps of
#: 1/128 K above 100 K (180–340 K is codes 10 240–30 720). The step is
#: exact in binary on purpose: both encoders unscale a code as
#: ``code × scale + offset``, and a decimal step such as 0.01 K puts some
#: values an ulp either side of a codebook half-step depending on whether
#: the platform fuses the multiply and the add, which split the two
#: encoders' output by one code on one cell.
FCI_BT_SCALE = 0.0078125
FCI_BT_OFFSET = 100.0
#: The HDF5 filter every FCI chunk is compressed with (JPEG-LS, filter
#: 32018): a stock GDAL fails on it with "undefined filter"; the plugin
#: comes with the ``hdf5plugin`` distribution (the ``satellite`` group)
#: and is handed to each GDAL subprocess through ``HDF5_PLUGIN_PATH``.
HDF5_PLUGIN_VARIABLE = "HDF5_PLUGIN_PATH"


def hdf5_plugin_environment() -> dict[str, str]:
    """The environment that lets GDAL read an FCI chunk: ``hdf5plugin``'s
    plugin directory, unless the environment already names one."""
    import os  # noqa: PLC0415

    if os.environ.get(HDF5_PLUGIN_VARIABLE):
        return {}
    try:
        import hdf5plugin  # noqa: PLC0415 - the satellite dependency group
    except ImportError as exc:
        raise ConversionError(
            "reading an FCI chunk needs the hdf5plugin package's JPEG-LS filter: uv sync --group satellite"
        ) from exc
    return {HDF5_PLUGIN_VARIABLE: str(hdf5plugin.PLUGIN_PATH)}


def elevation_angle(latitude: float, *, height: float, semi_major: float, semi_minor: float) -> float:
    """The geostationary scan angle (radians) at which a point on the
    sub-satellite meridian at ``latitude`` is seen: the ``y`` of the CF
    ``geostationary`` projection for either sweep axis, since the point's
    ``x`` is zero there."""
    phi = math.radians(latitude)
    e2 = 1.0 - (semi_minor / semi_major) ** 2
    normal = semi_major / math.sqrt(1.0 - e2 * math.sin(phi) ** 2)
    x = normal * math.cos(phi)
    z = normal * (1.0 - e2) * math.sin(phi)
    return math.atan2(z, semi_major + height - x)


class FCIReader:
    """EUMETSAT's MTG FCI Level 1c full-disk product (FDHSI): one product
    per ten-minute repeat cycle, cut into forty netCDF-4 chunk files, each
    a strip of some 140 rows of the 5568 x 5568 2 km grid carrying every
    channel's *radiance* as 12-bit codes with a scale and offset
    (``/data/<channel>/measured/effective_radiance``), the grid mapping in
    a parent group and the rows running south to north, compressed with an
    HDF5 filter GDAL has to be handed. So the reader does what the other
    two leave to GDAL: it reads each chunk's radiance band through
    ``gdal_translate`` (with the filter plugin on the environment), turns
    it into brightness temperature with the channel's own Planck
    coefficients (``BT = (c2 ν / ln(1 + c1 ν³ / L) − b) / a``, the
    conversion the product documents), writes it as an Int16 strip with a
    hand-built geostationary georeference (the projection's height,
    ellipsoid and sweep off the file, the extent off GDAL's own reading of
    the strip's scan-angle coordinates, times the height), and mosaics the
    strips with ``gdalbuildvrt``; the projector warps that like any other
    slot. The files are found and fetched through the Data Store
    (:mod:`eumetsat`), the search anonymous and the download with a
    registered account's token.

    A chunk carries every channel, so a slot's files are shared across the
    channels (``files_per_channel`` is false) and only the chunks whose
    rows reach into the published ±60° of latitude are downloaded
    (:meth:`needed_chunks`).
    """

    files_per_channel: ClassVar[bool] = False

    def group(self, channel: Channel) -> str:
        try:
            return FCI_GROUPS[channel.band - 1]
        except IndexError as exc:
            raise ConversionError(f"FCI has no channel {channel.band}") from exc

    def slot_of(self, platform: Platform, start: datetime) -> datetime:
        """A cycle's slot: its sensing start, floored to the cadence
        (15:20:07 is the 15:20 slot)."""
        seconds = int(start.timestamp()) // platform.cadence_seconds * platform.cadence_seconds
        return datetime.fromtimestamp(seconds, tz=UTC)

    def products(
        self, platform: Platform, start: datetime, end: datetime, *, fetch: Callable[[str], str] | None = None
    ) -> dict[datetime, eumetsat.Product]:
        """The collection's products sensed in ``[start, end]`` by slot, a
        reissued product resolving to the later publication."""
        by_slot: dict[datetime, eumetsat.Product] = {}
        for product in eumetsat.search(platform.prefix, start, end, fetch=fetch):
            slot = self.slot_of(platform, product.start)
            held = by_slot.get(slot)
            if held is None or product.updated > held.updated:
                by_slot[slot] = product
        return by_slot

    def list_slots(self, platform: Platform, day: datetime, *, fetch: Callable[[str], str] | None = None) -> list[datetime]:
        """The day's cycles: one search over the day (two pages at most)."""
        return sorted(self.products(platform, day, day + timedelta(days=1) - timedelta(seconds=1), fetch=fetch))

    #: How far back the newest slots are searched for at least, so a feed
    #: with a gap still answers: one request either way.
    RECENT_SPAN = timedelta(hours=6)

    def recent_slots(
        self, platform: Platform, now: datetime, *, limit: int, fetch: Callable[[str], str] | None = None
    ) -> list[datetime]:
        """One search back over ``limit`` cadences (plus the product's
        usual lag, and six hours at least), newest first."""
        span = max(timedelta(seconds=limit * platform.cadence_seconds + platform.typical_lag_seconds), self.RECENT_SPAN)
        slots = [slot for slot in self.products(platform, now - span, now, fetch=fetch) if slot <= now]
        return sorted(slots, reverse=True)[:limit]

    def list_slot(
        self, platform: Platform, channel: Channel, slot: datetime, *, fetch: Callable[[str], str] | None = None
    ) -> list[SlotObject]:
        """The slot's chunk files, one object per chunk in chunk order,
        the same for every channel since a chunk carries them all; the
        product's ``CHK-TRAIL``, metadata and quicklook entries are not
        chunks and are left out."""
        product = self.products(platform, slot, slot + timedelta(seconds=platform.cadence_seconds - 1), fetch=fetch).get(slot)
        if product is None:
            return []
        by_chunk: dict[int, SlotObject] = {}
        for entry in product.entries:
            parsed = parse_fci_chunk(entry.name)
            if parsed is None:
                continue
            candidate = SlotObject(key=entry.name, size=0, tile=parsed.chunk, created=product.updated, url=entry.href)
            held = by_chunk.get(parsed.chunk)
            if held is None or parsed.created > (parse_fci_chunk(held.key) or parsed).created:
                by_chunk[parsed.chunk] = candidate
        return [by_chunk[chunk] for chunk in sorted(by_chunk)]

    def needed_chunks(self, platform: Platform) -> set[int]:
        """The chunks whose rows reach into the platform's region — the
        disk between ±60° of latitude — from the grid's row packing: a
        chunk is one fortieth of the rows, and the strip a row is in is
        known only once the file is open, so two rows of margin are
        allowed either side. The northern- and southernmost two chunks
        are past 60° and are never fetched."""
        _, south, _, north = platform.region
        bounds = [
            elevation_angle(latitude, height=35786400.0, semi_major=6378137.0, semi_minor=6356752.314245)
            for latitude in (south, north)
        ]
        low, high = min(bounds), max(bounds)
        first_row = (low - FCI_IR_ANGLE_OFFSET) / FCI_IR_ANGLE_STEP - 2.0
        last_row = (high - FCI_IR_ANGLE_OFFSET) / FCI_IR_ANGLE_STEP + 2.0
        rows_per_chunk = FCI_IR_GRID / platform.tile_count
        needed: set[int] = set()
        for chunk in range(1, platform.tile_count + 1):
            chunk_first = math.floor((chunk - 1) * rows_per_chunk) + 1
            chunk_last = math.floor(chunk * rows_per_chunk)
            if chunk_last >= first_row and chunk_first <= last_row:
                needed.add(chunk)
        return needed

    def download(
        self,
        platform: Platform,
        objects: list[SlotObject],
        into: Path,
        *,
        download: Callable[[str], bytes] | None = None,
        concurrency: int = 8,
    ) -> SlotFiles:
        """The needed chunks on disk under ``into`` (shared by the slot's
        channels), each fetched through the Data Store with the account's
        token unless already there whole. The listing gives no sizes, so a
        file present at all is taken as complete: a download lands under
        a ``.part`` name until it is."""
        if not objects:
            raise DownloadError(f"{platform.spacecraft}: nothing to download")
        needed = self.needed_chunks(platform)
        by_chunk = {item.tile: item for item in objects}
        missing = sorted(needed - set(by_chunk))
        if missing:
            raise DownloadError(f"{platform.spacecraft}: the product lists no chunk {missing}")
        download = download or eumetsat.download_bytes
        into.mkdir(parents=True, exist_ok=True)

        def one(chunk: int) -> Path:
            item = by_chunk[chunk]
            target = into / item.key
            if target.is_file() and target.stat().st_size > 0:
                return target
            payload = download(item.url or item.key)
            if not payload:
                raise DownloadError(f"{item.key}: received an empty file")
            temporary = target.with_suffix(target.suffix + ".part")
            temporary.write_bytes(payload)
            temporary.replace(target)
            return target

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            paths = tuple(pool.map(one, sorted(needed)))
        starts = [parsed.start for item in objects if (parsed := parse_fci_chunk(item.key)) is not None]
        return SlotFiles(slot=self.slot_of(platform, min(starts)), paths=paths)

    def strip_path(self, files: SlotFiles, channel: Channel, chunk_file: Path) -> Path:
        """Where a chunk's brightness-temperature strip of one channel is
        written: beside the chunks, under the channel's id."""
        parsed = parse_fci_chunk(chunk_file.name)
        chunk = parsed.chunk if parsed is not None else 0
        return chunk_file.parent / channel.id / f"{chunk:04d}.vrt"

    def open(
        self, files: SlotFiles, workdir: Path, *, channel: Channel | None = None, missing_below: float | None = None
    ) -> Path:
        """The channel's brightness temperature over the slot's chunks: one
        strip per chunk (radiance read, Planck-converted, packed,
        georeferenced by hand) and a VRT mosaicking them. ``workdir`` is
        where the mosaic goes; the strips sit beside the chunks so
        :meth:`packing_source` can find them from the files alone."""
        if channel is None:
            raise ConversionError("an FCI slot is opened for one channel")
        if channel.kind != "bt":
            raise ConversionError(f"the FCI reader converts infrared channels to brightness temperature; {channel.id} is not one")
        workdir.mkdir(parents=True, exist_ok=True)
        environment = hdf5_plugin_environment()
        planck = self.planck_coefficients(files.paths[0], channel, environment)
        strips = [self.write_strip(chunk_file, channel, planck, self.strip_path(files, channel, chunk_file), environment) for chunk_file in files.paths]
        vrt = workdir / f"{files.slot:%Y%m%d%H%M%S}.{channel.id}.vrt"
        run_command(
            ["gdalbuildvrt", "-q", "-overwrite", "-resolution", "highest", str(vrt), *map(str, strips)],
            description=f"gdalbuildvrt {vrt.name}",
        )
        if missing_below is not None:
            from .assemble import NODATA  # noqa: PLC0415 - assemble imports nothing from here

            floor = int(math.floor((missing_below - FCI_BT_OFFSET) / FCI_BT_SCALE))
            vrt.write_text(_mask_below(vrt.read_text(encoding="utf-8"), floor, NODATA), encoding="utf-8")
        return vrt

    def packing_source(self, files: SlotFiles, *, channel: Channel | None = None) -> Path:
        if channel is None:
            raise ConversionError("an FCI slot's packing is one channel's")
        return self.strip_path(files, channel, files.paths[0])

    def subdataset(self, chunk_file: Path, channel: Channel) -> str:
        return f'NETCDF:"{chunk_file}":/data/{self.group(channel)}/measured/effective_radiance'

    def planck_coefficients(self, chunk_file: Path, channel: Channel, environment: dict[str, str]) -> dict[str, float]:
        """The channel's radiance-to-temperature constants, scalar
        variables of its ``measured`` group read through ``gdalmdiminfo``
        (GDAL's raster side cannot open a variable without dimensions);
        the same for every chunk of a cycle, so read off one."""
        group = self.group(channel)
        values: dict[str, float] = {}
        for name in ("constant_c1", "constant_c2", "coefficient_a", "coefficient_b", "coefficient_wavenumber"):
            array = f"/data/{group}/measured/radiance_to_bt_conversion_{name}"
            result = run_command(
                [require_command("gdalmdiminfo"), str(chunk_file), "-array", array, "-detailed"],
                description=f"read {array} of {chunk_file.name}",
                env=environment,
            )
            try:
                value = json.loads(result.stdout)["values"]
                values[name] = float(value[0] if isinstance(value, list) else value)
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
                raise ConversionError(f"{chunk_file.name} carries no {array}") from exc
        if not all(math.isfinite(value) for value in values.values()) or values["coefficient_wavenumber"] <= 0:
            raise ConversionError(f"{chunk_file.name}: {group} carries no Planck coefficients (not an infrared channel?)")
        return values

    def write_strip(
        self, chunk_file: Path, channel: Channel, planck: dict[str, float], out: Path, environment: dict[str, str]
    ) -> Path:
        """One chunk's channel as a brightness-temperature strip: an Int16
        raw file and the VRT that georeferences it (a raw raster band with
        the geostationary SRS, the extent, the packing and the unit)."""
        subdataset = self.subdataset(chunk_file, channel)
        result = run_command(
            [require_command("gdalinfo"), "-json", subdataset], description=f"inspect {chunk_file.name}", env=environment
        )
        try:
            info = json.loads(result.stdout)
            width, height = (int(value) for value in info["size"])
            transform = [float(value) for value in info["geoTransform"]]
            band = info["bands"][0]
            metadata = info.get("metadata", {}).get("", {})
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ConversionError(
                f"GDAL did not read the scan-angle coordinates of {chunk_file.name}: an FCI chunk needs a GDAL that georeferences a group's variable by its x and y"
            ) from exc
        group = self.group(channel)
        packing = f"/data/{group}/measured/y#"
        try:
            step = float(metadata[packing + "scale_factor"])
            offset = float(metadata[packing + "add_offset"])
        except (KeyError, ValueError) as exc:
            raise ConversionError(f"{chunk_file.name} does not say how its rows are packed") from exc
        if not (math.isclose(step, FCI_IR_ANGLE_STEP, rel_tol=1e-6) and math.isclose(offset, FCI_IR_ANGLE_OFFSET, rel_tol=1e-6)) or width != FCI_IR_GRID:
            raise ConversionError(
                f"{chunk_file.name} is not on the FCI 2 km grid the reader knows ({width} columns, row step {step}, offset {offset})"
            )
        projection = "/data/mtg_geos_projection#"
        try:
            height_m = float(metadata[projection + "perspective_point_height"])
            semi_major = float(metadata[projection + "semi_major_axis"])
            semi_minor = float(metadata[projection + "semi_minor_axis"])
            longitude = float(metadata[projection + "longitude_of_projection_origin"])
            sweep = str(metadata[projection + "sweep_angle_axis"]).strip().strip("“”\"")
        except (KeyError, ValueError) as exc:
            raise ConversionError(f"{chunk_file.name} carries no geostationary grid mapping") from exc
        if sweep not in ("x", "y"):
            raise ConversionError(f"{chunk_file.name}: unexpected sweep axis {sweep!r}")
        scale = float(band.get("scale", 1.0) or 1.0)
        add = float(band.get("offset", 0.0) or 0.0)
        fill = band.get("noDataValue")
        valid_top = 4095
        valid_range = metadata.get(f"/data/{group}/measured/effective_radiance#valid_range", "")
        match = re.search(r"\{\s*-?\d+\s*,\s*(\d+)\s*\}", str(valid_range))
        if match:
            valid_top = int(match.group(1))
        raw = out.with_suffix(".codes.bin")
        header = raw.with_suffix(".hdr")
        out.parent.mkdir(parents=True, exist_ok=True)
        # The codes alone: the ENVI driver refuses the radian georeference
        # GDAL derives for the chunk (a west-to-east negative pixel width
        # it reports as a rotation), so the translate is told to write a
        # unit one and the georeference is rebuilt from `transform` below.
        run_command(
            [
                require_command("gdal_translate"),
                "-q",
                "-of",
                "ENVI",
                "-ot",
                "UInt16",
                "-a_srs",
                "",
                "-a_ullr",
                "0",
                str(height),
                str(width),
                "0",
                "-co",
                "INTERLEAVE=BSQ",
                subdataset,
                str(raw),
            ],
            description=f"read {chunk_file.name}",
            env=environment,
        )
        try:
            codes = np.fromfile(raw, dtype="<u2")
        finally:
            for leftover in (raw, header, raw.with_suffix(".bin.aux.xml")):
                leftover.unlink(missing_ok=True)
        if codes.size != width * height:
            raise ConversionError(f"{chunk_file.name} holds {codes.size} cells, not {width} x {height}")
        codes = codes.reshape(height, width)
        invalid = codes > valid_top
        if fill is not None:
            invalid |= codes == int(fill)
        radiance = codes.astype(np.float64) * scale + add
        invalid |= ~(radiance > 0.0)
        radiance[invalid] = 1.0
        temperature = brightness_temperature(radiance, planck)
        temperature[invalid] = np.nan
        # GDAL reads the strip in the file's own orientation and says which
        # way its axes run; the strip is written west to east and north to
        # south whatever that was, so the mosaic has one orientation.
        west = transform[0]
        north = transform[3]
        if transform[1] < 0:
            temperature = temperature[:, ::-1]
            west += transform[1] * width
        if transform[5] > 0:
            temperature = temperature[::-1, :]
            north += transform[5] * height
        pixel = abs(transform[1]) * height_m
        line = abs(transform[5]) * height_m
        finite = np.isfinite(temperature)
        packed = np.floor(np.where(finite, temperature - FCI_BT_OFFSET, 0.0) / FCI_BT_SCALE + 0.5)
        if finite.any() and (packed[finite].min() < -32766 or packed[finite].max() > 32767):
            raise ConversionError(f"{chunk_file.name}: brightness temperatures overflow the strip's packing")
        from .assemble import NODATA  # noqa: PLC0415 - assemble imports nothing from here

        strip = np.where(finite, packed, NODATA).astype("<i2")
        raw_strip = out.with_suffix(".bin")
        strip.tofile(raw_strip)
        srs = (
            f"+proj=geos +lon_0={longitude!r} +h={height_m!r} +x_0=0 +y_0=0 +a={semi_major!r} +b={semi_minor!r} "
            f"+sweep={sweep} +units=m +no_defs"
        )
        out.write_text(
            "\n".join(
                [
                    f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">',
                    f"  <SRS>{escape(srs)}</SRS>",
                    f"  <GeoTransform>{west * height_m!r}, {pixel!r}, 0, {north * height_m!r}, 0, {-line!r}</GeoTransform>",
                    '  <VRTRasterBand dataType="Int16" band="1" subClass="VRTRawRasterBand">',
                    f"    <NoDataValue>{NODATA}</NoDataValue>",
                    "    <UnitType>K</UnitType>",
                    f"    <Scale>{FCI_BT_SCALE!r}</Scale>",
                    f"    <Offset>{FCI_BT_OFFSET!r}</Offset>",
                    f'    <SourceFilename relativeToVRT="1">{escape(raw_strip.name)}</SourceFilename>',
                    "    <ImageOffset>0</ImageOffset>",
                    "    <PixelOffset>2</PixelOffset>",
                    f"    <LineOffset>{2 * width}</LineOffset>",
                    "    <ByteOrder>LSB</ByteOrder>",
                    "  </VRTRasterBand>",
                    "</VRTDataset>",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return out


def brightness_temperature(radiance: np.ndarray, planck: dict[str, float]) -> np.ndarray:
    """The FCI product's radiance-to-brightness-temperature conversion:
    ``BT = (c2 ν / ln(1 + c1 ν³ / L) − b) / a`` with the channel's
    constants ``c1``, ``c2``, its central wave number ``ν`` (cm⁻¹) and the
    band-correction coefficients ``a``, ``b``; ``L`` in the product's
    mW m⁻² sr⁻¹ (cm⁻¹)⁻¹."""
    nu = planck["coefficient_wavenumber"]
    c1 = planck["constant_c1"]
    c2 = planck["constant_c2"]
    effective = c2 * nu / np.log1p(c1 * nu**3 / radiance)
    return (effective - planck["coefficient_b"]) / planck["coefficient_a"]


READERS: dict[str, Reader] = {"isatss": ISatSSReader(), "cmipf": CMIPFReader(), "fci": FCIReader()}


def reader_for(platform: Platform) -> Reader:
    try:
        return READERS[platform.reader]
    except KeyError as exc:
        raise DownloadError(f"{platform.spacecraft}: no reader is implemented for {platform.reader!r}") from exc


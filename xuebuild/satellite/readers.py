"""Readers: how a platform's files are listed, fetched and opened.

A reader's responsibility ends at *one thing GDAL can open* — a dataset
with a geostationary CRS whose values are the physical quantity or carry a
scale and offset to it. Putting it on a regular grid is the projector's
job (:mod:`projector`), and nothing downstream of the projector knows
which reader produced the frame. That is the seam a second file format
goes through: the ISatSS tiles are the first reader, the GOES ``CMIPF``
single file the second, and the raw HSD segments a third if the ISatSS
product ever stops — each a class here, chosen by
:attr:`~platforms.Platform.reader`.

The bucket listing and the downloads go through the pipeline's own HTTP
helpers (:mod:`xuebuild.fetch`), imported lazily because that module
dispatches to this package; every function takes a ``fetch`` /
``download`` callable so the tests never touch the network.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from ..errors import DownloadError
from ..gdal import run_command
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

    def list_slots(self, platform: Platform, day: datetime, *, fetch: Callable[[str], str] | None = None) -> list[datetime]:
        """The scan slots the bucket has a directory for on one UTC day,
        in time order; a slot listed may still be incomplete."""
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

    def open(self, files: SlotFiles, workdir: Path, *, missing_below: float | None = None) -> Path:
        """One GDAL-openable dataset over the slot's files — a connection
        string, not necessarily a file — written under ``workdir``. Cells
        below ``missing_below`` (in the quantity's unit) read as no data."""
        ...

    def packing_source(self, files: SlotFiles) -> Path:
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

    def slot_prefix(self, platform: Platform, channel: Channel, slot: datetime) -> str:
        """Every tile of the channel in the slot's directory shares this
        prefix; the bit depth field is the channel's own, so the listing
        never needs to guess it."""
        return (
            f"{platform.prefix}/{slot:%Y/%m/%d/%H%M}/"
            f"OR_HFD-{int(round(channel.resolution_km * 10)):03d}-B{channel.bit_depth:02d}-M1C{channel.band:02d}-"
        )

    def list_slots(self, platform: Platform, day: datetime, *, fetch: Callable[[str], str] | None = None) -> list[datetime]:
        _, prefixes = list_prefix(platform.bucket, f"{platform.prefix}/{day:%Y/%m/%d}/", delimiter="/", fetch=fetch)
        slots: list[datetime] = []
        for prefix in prefixes:
            match = _ISATSS_DIRECTORY.search(prefix)
            if not match:
                continue
            try:
                slots.append(
                    datetime(
                        int(match.group("year")),
                        int(match.group("month")),
                        int(match.group("day")),
                        int(match.group("hhmm")[:2]),
                        int(match.group("hhmm")[2:]),
                        tzinfo=UTC,
                    )
                )
            except ValueError:
                continue
        return sorted(slots)

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

    def open(self, files: SlotFiles, workdir: Path, *, missing_below: float | None = None) -> Path:
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

    def packing_source(self, files: SlotFiles) -> Path:
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


READERS: dict[str, Reader] = {"isatss": ISatSSReader()}


def reader_for(platform: Platform) -> Reader:
    try:
        return READERS[platform.reader]
    except KeyError as exc:
        raise DownloadError(f"{platform.spacecraft}: no reader is implemented for {platform.reader!r}") from exc


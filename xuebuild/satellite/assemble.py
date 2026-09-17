"""From slots to frames, and from frames to the window's NetCDF series.

A **frame** is one slot of one channel on the target grid: an Int16
GeoTIFF the projector wrote, cached under
``data/raw/<role>-frames/<channel>/<channel>_<YYYYMMDDHHMMSS>.tif`` and
mirrored on the bucket by the rounds script (``make pull-r2-frames`` /
``push-r2-frames``), so a slot is warped once and every round after reads
the cache. A frame is immutable: its bytes depend on the tiles and the
GDAL version alone.

A **window** is the frames of one run, stacked into the NetCDF series the
observation ingest reads (:mod:`xuebuild.observation`): one variable per
channel over ``(time, lat, lon)``, packed Int16 with the product's own
scale and offset and fill, ``time`` in seconds since the run. The series
is written by GDAL itself — a VRT over the frames carrying the netCDF
driver's extra-dimension metadata, translated with ``gdal_translate -of
netCDF`` — so the reference pipeline keeps GDAL as its only tool here and
the producers (:mod:`producers`) read the same file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ..errors import ConversionError
from ..gdal import require_command, run_command
from .platforms import Channel, Platform
from .projector import TargetGrid

LOG = logging.getLogger(__name__)

FRAME_SUFFIX = ".tif"
#: The fill of every frame and of the series: ISatSS's own ``_FillValue``,
#: kept so a frame's raw codes are the product's.
NODATA = -32767


def frames_dirname(platform: Platform) -> str:
    """``<role>-frames``, beside the run directories under the raw root —
    the name the Makefile's frame-cache targets take from ``MODEL``."""
    return f"{platform.role}-frames"


def frame_name(channel: Channel, slot: datetime) -> str:
    return f"{channel.id}_{slot:%Y%m%d%H%M%S}{FRAME_SUFFIX}"


def frame_path(frames_dir: Path, channel: Channel, slot: datetime) -> Path:
    return frames_dir / channel.id / frame_name(channel, slot)


@dataclass(frozen=True)
class Frame:
    slot: datetime
    path: Path


@dataclass(frozen=True)
class Packing:
    """How a frame's Int16 codes map to the quantity: the product's own
    scale and offset, read off the first frame and required of the rest."""

    scale: float
    offset: float
    unit: str


def frame_packing(path: Path) -> Packing:
    # The system GDAL's gdalinfo, not `gdal.dataset_info`: the fetch side
    # runs on the GDAL that warped the frame (the wheel's carries no GeoTIFF
    # driver), and which encoder converts the series is a separate choice.
    result = run_command([require_command("gdalinfo"), "-json", str(path)], description=f"inspect {path}")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"GDAL returned invalid JSON for {path}") from exc
    bands = info.get("bands") or []
    if len(bands) != 1:
        raise ConversionError(f"{path} must carry exactly one band")
    band = bands[0]
    return Packing(
        scale=float(band.get("scale", 1.0) or 1.0),
        offset=float(band.get("offset", 0.0) or 0.0),
        unit=str(band.get("unit", "") or band.get("metadata", {}).get("", {}).get("units", "")).strip(),
    )


def write_series(
    frames: list[Frame],
    *,
    channel: Channel,
    platform: Platform,
    grid: TargetGrid,
    run_time: datetime,
    unit: str,
    out: Path,
) -> None:
    """Stack the frames of one channel into the window's NetCDF series.

    ``unit`` is what the variable is declared in (the registry's output
    unit, ``K``); the frames' own unit string must agree with it once
    normalized (ISatSS spells ``kelvin``). The time axis is seconds since
    ``run_time``, one value per frame, in frame order."""
    if not frames:
        raise ConversionError(f"{platform.spacecraft} {channel.id}: no frames to write")
    if any(later.slot <= earlier.slot for earlier, later in zip(frames, frames[1:])):
        raise ConversionError(f"{platform.spacecraft} {channel.id}: frames are not in time order")
    packing = frame_packing(frames[0].path)
    if _normalized_unit(packing.unit) != _normalized_unit(unit):
        raise ConversionError(f"{frames[0].path} is in {packing.unit or '<no unit>'}, not {unit}")
    offsets = [int((frame.slot - run_time).total_seconds()) for frame in frames]
    if offsets[0] < 0:
        raise ConversionError(f"{platform.spacecraft} {channel.id}: a frame precedes the run time")
    vrt = out.with_suffix(".vrt")
    vrt.write_text(_series_vrt(frames, offsets, packing, channel, platform, grid, run_time, unit), encoding="utf-8")
    temporary = out.with_suffix(out.suffix + ".part.nc")
    run_command(
        [
            "gdal_translate",
            "-q",
            "-of",
            "netCDF",
            "-co",
            "FORMAT=NC4",
            "-co",
            "COMPRESS=DEFLATE",
            "-co",
            "ZLEVEL=4",
            str(vrt),
            str(temporary),
        ],
        description=f"gdal_translate {out.name}",
    )
    temporary.replace(out)
    vrt.unlink(missing_ok=True)
    LOG.info("%s %s: %d frames in %s", platform.spacecraft, channel.id, len(frames), out)


def _normalized_unit(unit: str) -> str:
    text = unit.strip().lower()
    return {"kelvin": "k", "k": "k", "1": "1", "": ""}.get(text, text)


def _series_vrt(
    frames: list[Frame],
    offsets: list[int],
    packing: Packing,
    channel: Channel,
    platform: Platform,
    grid: TargetGrid,
    run_time: datetime,
    unit: str,
) -> str:
    """The VRT the netCDF driver turns into a ``(time, lat, lon)`` variable:
    ``NETCDF_DIM_EXTRA`` names the extra dimension, ``_DEF`` its length
    and type (4 is ``NC_INT``), ``_VALUES`` its coordinate, and each band's
    ``NETCDF_DIM_time`` places it; ``NETCDF_VARNAME`` names the variable."""
    epoch = run_time.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        f'<VRTDataset rasterXSize="{grid.width}" rasterYSize="{grid.height}">',
        '  <SRS dataAxisToSRSAxisMapping="2,1">EPSG:4326</SRS>',
        f"  <GeoTransform>{grid.west!r}, {grid.step!r}, 0, {grid.north!r}, 0, {-grid.step!r}</GeoTransform>",
        "  <Metadata>",
        '    <MDI key="NETCDF_DIM_EXTRA">{time}</MDI>',
        f'    <MDI key="NETCDF_DIM_time_DEF">{{{len(frames)},4}}</MDI>',
        f'    <MDI key="NETCDF_DIM_time_VALUES">{{{",".join(str(offset) for offset in offsets)}}}</MDI>',
        f'    <MDI key="time#units">seconds since {epoch}</MDI>',
        '    <MDI key="time#standard_name">time</MDI>',
        '    <MDI key="time#axis">T</MDI>',
        '    <MDI key="NC_GLOBAL#Conventions">CF-1.8</MDI>',
        f'    <MDI key="NC_GLOBAL#title">{escape(platform.spacecraft)} {escape(platform.instrument)} {escape(channel.id)}</MDI>',
        f'    <MDI key="NC_GLOBAL#platform">{escape(platform.spacecraft)}</MDI>',
        f'    <MDI key="NC_GLOBAL#satellite_number">{platform.satellite_number}</MDI>',
        f'    <MDI key="NC_GLOBAL#instrument">{escape(platform.instrument)}</MDI>',
        f'    <MDI key="NC_GLOBAL#sub_longitude">{platform.sub_longitude!r}</MDI>',
        f'    <MDI key="NC_GLOBAL#source_prefix">{escape(platform.bucket)}/{escape(platform.prefix)}</MDI>',
        '    <MDI key="NC_GLOBAL#projector">gdalwarp</MDI>',
        "  </Metadata>",
    ]
    long_name = f"{platform.instrument} band {channel.band}, {channel.wavelength_um} µm"
    standard_name = "brightness_temperature" if channel.kind == "bt" else "toa_bidirectional_reflectance"
    for number, (frame, offset) in enumerate(zip(frames, offsets), start=1):
        lines += [
            f'  <VRTRasterBand dataType="Int16" band="{number}">',
            "    <Metadata>",
            f'      <MDI key="NETCDF_VARNAME">{escape(channel.id)}</MDI>',
            f'      <MDI key="NETCDF_DIM_time">{offset}</MDI>',
            f'      <MDI key="units">{escape(unit)}</MDI>',
            f'      <MDI key="long_name">{escape(long_name)}</MDI>',
            f'      <MDI key="standard_name">{standard_name}</MDI>',
            "    </Metadata>",
            f"    <NoDataValue>{NODATA}</NoDataValue>",
            f"    <Offset>{packing.offset!r}</Offset>",
            f"    <Scale>{packing.scale!r}</Scale>",
            "    <SimpleSource>",
            f'      <SourceFilename relativeToVRT="0">{escape(str(frame.path))}</SourceFilename>',
            "      <SourceBand>1</SourceBand>",
            "    </SimpleSource>",
            "  </VRTRasterBand>",
        ]
    lines.append("</VRTDataset>")
    return "\n".join(lines) + "\n"

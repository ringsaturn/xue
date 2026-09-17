"""From slots to frames, and from frames to the window's NetCDF series.

A **frame** is one slot of one variable on the target grid: an Int16
GeoTIFF the projector wrote for a channel, or a producer's output written
by :func:`write_frame`, cached under
``data/raw/<role>-frames/<variable>/<variable>_<YYYYMMDDHHMMSS>.tif`` and
mirrored on the bucket by the rounds script (``make pull-r2-frames`` /
``push-r2-frames``), so a slot is warped — and composed — once and every
round after reads the cache. A frame is immutable: its bytes depend on
the tiles, the GDAL version and (for a produced one) the producer's
version alone.

A **window** is the frames of one run, stacked into the NetCDF series the
observation ingest reads (:mod:`xuebuild.observation`): one file per
variable, ``<stem>.<variable>.nc``, a ``(time, lat, lon)`` variable named
by its id, packed Int16 with the frames' own scale and offset and fill,
``time`` in seconds since the run, and on a produced variable the
``producer_id`` / ``producer_version`` attributes the converters carry
into the bundle metadata. One file per variable because GDAL's netCDF
driver stacks a VRT's bands into one extra-dimensioned variable only when
the bands are exactly that axis. The series is written by GDAL itself — a
VRT over the frames carrying the netCDF driver's extra-dimension
metadata, translated with ``gdal_translate -of netCDF`` — so the
reference pipeline keeps GDAL as its only tool here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

from ..errors import ConversionError
from ..gdal import require_command, run_command
from .platforms import Channel, Platform
from .projector import TargetGrid

LOG = logging.getLogger(__name__)

FRAME_SUFFIX = ".tif"
#: Beside every frame, the packing the projector read off the source: which
#: GDAL carries a band's scale, offset and unit through a warp is a matter
#: of version (3.13 does, Ubuntu 24.04's 3.8 drops the unit), and the
#: series must not depend on it.
PACKING_SUFFIX = ".json"
#: The fill of every frame and of the series: ISatSS's own ``_FillValue``,
#: kept so a frame's raw codes are the product's.
NODATA = -32767


def frames_dirname(platform: Platform) -> str:
    """``<role>-frames``, beside the run directories under the raw root —
    the name the Makefile's frame-cache targets take from ``MODEL``."""
    return f"{platform.role}-frames"


def _variable_id(variable: Channel | str) -> str:
    return variable if isinstance(variable, str) else variable.id


def frame_name(variable: Channel | str, slot: datetime) -> str:
    return f"{_variable_id(variable)}_{slot:%Y%m%d%H%M%S}{FRAME_SUFFIX}"


def frame_path(frames_dir: Path, variable: Channel | str, slot: datetime) -> Path:
    """A channel's frame, or a produced variable's, by id."""
    return frames_dir / _variable_id(variable) / frame_name(variable, slot)


def packing_path(frame: Path) -> Path:
    return frame.with_suffix(PACKING_SUFFIX)


@dataclass(frozen=True)
class Frame:
    slot: datetime
    path: Path


@dataclass(frozen=True)
class Packing:
    """How a frame's Int16 codes map to the quantity: the product's own
    scale and offset and its unit, read off the source before the warp and
    written beside the frame (:func:`write_packing`); the first frame's is
    required of the rest."""

    scale: float
    offset: float
    unit: str
    producer: tuple[str, str] | None = None
    """For a frame a producer wrote, its ``(id, version)``: stamped on the
    series variable and carried into the bundle metadata's ``producer``
    block, so the algorithm that made a picture is in the file."""

    def metadata(self) -> dict[str, object]:
        block: dict[str, object] = {"scale": self.scale, "offset": self.offset, "unit": self.unit}
        if self.producer is not None:
            block["producer"] = {"id": self.producer[0], "version": self.producer[1]}
        return block


def dataset_packing(name: Path) -> Packing:
    """The one band's scale, offset and unit as the system GDAL reports
    them for a dataset — the reader's VRT over the source tiles, or a frame
    written before packing sidecars existed."""
    # The system GDAL's gdalinfo, not `gdal.dataset_info`: the fetch side
    # runs on the GDAL that warped the frame (the wheel's carries no GeoTIFF
    # driver), and which encoder converts the series is a separate choice.
    result = run_command([require_command("gdalinfo"), "-json", str(name)], description=f"inspect {name}")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"GDAL returned invalid JSON for {name}") from exc
    bands = info.get("bands") or []
    if len(bands) != 1:
        raise ConversionError(f"{name} must carry exactly one band")
    band = bands[0]
    return Packing(
        scale=float(band.get("scale", 1.0) or 1.0),
        offset=float(band.get("offset", 0.0) or 0.0),
        unit=str(band.get("unit", "") or band.get("metadata", {}).get("", {}).get("units", "")).strip(),
    )


def write_packing(frame: Path, packing: Packing, **extra: object) -> Path:
    """The packing sidecar beside a frame, with whatever the fetch wants
    to note (the tiles the frame came from, the GDAL that warped it)."""
    path = packing_path(frame)
    path.write_text(json.dumps({**packing.metadata(), **extra}, indent=2) + "\n", encoding="utf-8")
    return path


def frame_packing(frame: Path) -> Packing:
    """A frame's packing: the sidecar written with it, else — for a frame
    cached before sidecars — what the GeoTIFF band itself carries."""
    sidecar = packing_path(frame)
    if sidecar.is_file():
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            producer = payload.get("producer")
            return Packing(
                scale=float(payload["scale"]),
                offset=float(payload["offset"]),
                unit=str(payload["unit"]),
                producer=(str(producer["id"]), str(producer["version"])) if producer is not None else None,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConversionError(f"{sidecar} is not a packing sidecar: {exc}") from exc
    return dataset_packing(frame)


def read_frame(frame: Path, grid: TargetGrid) -> np.ndarray:
    """A frame's plane in its quantity's unit: float64, ``(height, width)``
    of the target grid, NaN where the frame carries no data. The raw Int16
    codes are read through GDAL and unpacked here with the frame's own
    packing, so the arithmetic is this module's and not a GDAL version's."""
    raw = frame.with_suffix(".codes.bin")
    run_command(
        [require_command("gdal_translate"), "-q", "-of", "ENVI", "-ot", "Int16", "-co", "INTERLEAVE=BSQ", str(frame), str(raw)],
        description=f"read {frame.name}",
    )
    try:
        codes = np.fromfile(raw, dtype="<i2")
    finally:
        for leftover in (raw, raw.with_suffix(".hdr"), raw.with_suffix(".bin.aux.xml")):
            leftover.unlink(missing_ok=True)
    if codes.size != grid.width * grid.height:
        raise ConversionError(f"{frame} holds {codes.size} cells, not the {grid.width} x {grid.height} target grid")
    packing = frame_packing(frame)
    values = codes.reshape(grid.height, grid.width).astype(np.float64) * packing.scale + packing.offset
    values[codes.reshape(grid.height, grid.width) == NODATA] = np.nan
    return values


def write_frame(values: np.ndarray, grid: TargetGrid, packing: Packing, out: Path, **extra: object) -> Path:
    """A produced plane as a frame beside the channels': ``(height, width)``
    floats in the packing's unit, NaN where no data, packed to Int16 as
    ``floor(value / scale + 0.5)`` (round half up; the packing's offset is
    0 for every producer today) with :data:`NODATA` for NaN, written as
    the GeoTIFF the projector writes and the packing sidecar beside it.
    No raster library: the codes go through an ENVI header and
    ``gdal_translate``."""
    if values.shape != (grid.height, grid.width):
        raise ConversionError(f"{out.name}: a {values.shape} plane is not the {grid.width} x {grid.height} target grid")
    if packing.offset != 0.0:
        raise ConversionError(f"{out.name}: a produced frame's packing offset must be 0, not {packing.offset}")
    finite = np.isfinite(values)
    scaled = np.floor(np.where(finite, values, 0.0) / packing.scale + 0.5)
    if finite.any() and (scaled[finite].min() < -32766 or scaled[finite].max() > 32767):
        raise ConversionError(f"{out.name}: values overflow the Int16 packing at scale {packing.scale}")
    codes = np.where(finite, scaled, NODATA).astype("<i2")
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = out.with_suffix(".codes.bin")
    header = raw.with_suffix(".hdr")
    try:
        codes.tofile(raw)
        header.write_text(
            "ENVI\n"
            f"samples = {grid.width}\n"
            f"lines = {grid.height}\n"
            "bands = 1\n"
            "header offset = 0\n"
            "data type = 2\n"
            "interleave = bsq\n"
            "byte order = 0\n",
            encoding="ascii",
        )
        temporary = out.with_suffix(out.suffix + ".part.tif")
        run_command(
            [
                require_command("gdal_translate"),
                "-q",
                "-of",
                "GTiff",
                "-a_srs",
                "EPSG:4326",
                "-a_ullr",
                repr(grid.west),
                repr(grid.north),
                repr(grid.east),
                repr(grid.south),
                "-a_nodata",
                str(NODATA),
                "-co",
                "COMPRESS=DEFLATE",
                "-co",
                "PREDICTOR=2",
                "-co",
                "TILED=YES",
                str(raw),
                str(temporary),
            ],
            description=f"write {out.name}",
        )
        temporary.replace(out)
    finally:
        for leftover in (raw, header, raw.with_suffix(".bin.aux.xml")):
            leftover.unlink(missing_ok=True)
    write_packing(out, packing, **extra)
    return out


@dataclass(frozen=True)
class SeriesVariable:
    """What a series file's one variable is declared as."""

    id: str
    unit: str
    """The registry's output unit; the frames' own must agree once
    normalized (ISatSS spells ``kelvin``)."""
    long_name: str
    standard_name: str | None = None

    @classmethod
    def channel(cls, platform: Platform, channel: Channel, unit: str) -> SeriesVariable:
        return cls(
            id=channel.id,
            unit=unit,
            long_name=f"{platform.instrument} band {channel.band}, {channel.wavelength_um} µm",
            standard_name="brightness_temperature" if channel.kind == "bt" else "toa_bidirectional_reflectance",
        )


def series_path(destination: Path, stem: str, variable_id: str) -> Path:
    """The series file of one variable of a window: ``<stem>.<variable>.nc``
    (``himawari.2026091703.ir104.nc``)."""
    return destination / f"{stem}.{variable_id}.nc"


def write_series(
    frames: list[Frame],
    *,
    variable: SeriesVariable,
    platform: Platform,
    grid: TargetGrid,
    run_time: datetime,
    out: Path,
) -> None:
    """Stack the frames of one variable into its series file of the window.

    The frames' own unit string must agree with the variable's once
    normalized (ISatSS spells ``kelvin``), and every frame must carry the
    first one's packing — one producer, one version. The time axis is
    seconds since ``run_time``, one value per frame, in frame order."""
    if not frames:
        raise ConversionError(f"{platform.spacecraft} {variable.id}: no frames to write")
    if any(later.slot <= earlier.slot for earlier, later in zip(frames, frames[1:])):
        raise ConversionError(f"{platform.spacecraft} {variable.id}: frames are not in time order")
    packing = frame_packing(frames[0].path)
    if _normalized_unit(packing.unit) != _normalized_unit(variable.unit):
        raise ConversionError(f"{frames[0].path} is in {packing.unit or '<no unit>'}, not {variable.unit}")
    for frame in frames[1:]:
        other = frame_packing(frame.path)
        if (other.scale, other.offset, other.producer) != (packing.scale, packing.offset, packing.producer):
            raise ConversionError(f"{frame.path} is packed unlike {frames[0].path}; one series takes one packing")
    offsets = [int((frame.slot - run_time).total_seconds()) for frame in frames]
    if offsets[0] < 0:
        raise ConversionError(f"{platform.spacecraft} {variable.id}: a frame precedes the run time")
    vrt = out.with_suffix(".vrt")
    vrt.write_text(_series_vrt(frames, offsets, packing, variable, platform, grid, run_time), encoding="utf-8")
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
    LOG.info("%s %s: %d frames in %s", platform.spacecraft, variable.id, len(frames), out)


def _normalized_unit(unit: str) -> str:
    text = unit.strip().lower()
    return {"kelvin": "k", "k": "k", "1": "1", "": ""}.get(text, text)


def _series_vrt(
    frames: list[Frame],
    offsets: list[int],
    packing: Packing,
    variable: SeriesVariable,
    platform: Platform,
    grid: TargetGrid,
    run_time: datetime,
) -> str:
    """The VRT the netCDF driver turns into a ``(time, lat, lon)`` variable:
    ``NETCDF_DIM_EXTRA`` names the extra dimension, ``_DEF`` its length
    and type (4 is ``NC_INT``), ``_VALUES`` its coordinate, and each band's
    ``NETCDF_DIM_time`` places it; ``NETCDF_VARNAME`` names the variable.
    A band's other metadata keys become the variable's attributes, which
    is how the producer stamp travels."""
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
        f'    <MDI key="NC_GLOBAL#title">{escape(platform.spacecraft)} {escape(platform.instrument)} {escape(variable.id)}</MDI>',
        f'    <MDI key="NC_GLOBAL#platform">{escape(platform.spacecraft)}</MDI>',
        f'    <MDI key="NC_GLOBAL#satellite_number">{platform.satellite_number}</MDI>',
        f'    <MDI key="NC_GLOBAL#instrument">{escape(platform.instrument)}</MDI>',
        f'    <MDI key="NC_GLOBAL#sub_longitude">{platform.sub_longitude!r}</MDI>',
        f'    <MDI key="NC_GLOBAL#source_prefix">{escape(platform.bucket)}/{escape(platform.prefix)}</MDI>',
        '    <MDI key="NC_GLOBAL#projector">gdalwarp</MDI>',
        "  </Metadata>",
    ]
    attributes = [("units", variable.unit), ("long_name", variable.long_name)]
    if variable.standard_name:
        attributes.append(("standard_name", variable.standard_name))
    if packing.producer is not None:
        attributes += [("producer_id", packing.producer[0]), ("producer_version", packing.producer[1])]
    for number, (frame, offset) in enumerate(zip(frames, offsets), start=1):
        lines += [
            f'  <VRTRasterBand dataType="Int16" band="{number}">',
            "    <Metadata>",
            f'      <MDI key="NETCDF_VARNAME">{escape(variable.id)}</MDI>',
            f'      <MDI key="NETCDF_DIM_time">{offset}</MDI>',
            *(f'      <MDI key="{key}">{escape(value)}</MDI>' for key, value in attributes),
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

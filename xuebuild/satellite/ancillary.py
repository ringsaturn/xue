"""Ancillary fields a producer reads beside a slot's channels.

DEBRA (:class:`producers.DebraProducer`) needs two fields the imager does
not measure: a skin temperature to judge how cold a window channel reads
against the ground, and the ground's infrared emissivity to model what a
clear sky would read. Neither arrives as the algorithm was built against
(MERRA-2 lands weeks late, the CAMEL climatology sits behind Earthdata
credentials), so this module holds the two stand-ins the operational
DEBRA pipeline settled on, in the same shape:

- **Skin temperature** is GFS ``TMP:surface`` from NOAA's open data
  bucket: the one record of the 0.25° file nearest the slot's hour,
  located through the file's ``.idx`` sidecar and fetched as a byte
  range, a megabyte rather than half a gigabyte, from the newest cycle
  that reaches the hour (a cycle lands some four hours after its
  analysis, so the walk back over earlier cycles is the normal path).
  Cached under ``<ancillary root>/gfs/`` by cycle and forecast hour.
- **Emissivity** is the CAMEL monthly climatology (CAM5K30EM V003) as
  the operational pipeline stages it: one NetCDF per region and
  operational month, already interpolated to the DEBRA band centres
  (``emis_tir_86`` … ``emis_tir_123``), cropped to the region's box with
  a degree of margin, and named for the month it serves (the record ends
  in 2023, so the file's ``source_month`` says which year's same calendar
  month it carries). The pipeline reads the staged months from
  ``<ancillary root>/camel/<region>/<YYYYMM>.nc``, which
  ``make pull-r2-ancillary`` mirrors from the bucket; it never touches
  Earthdata. Whatever regions are staged are what DEBRA covers over land
  (:func:`emissivity_on_grid`); over water the algorithm needs no
  emissivity at all.

Both are regular latitude/longitude rasters read through GDAL like a
frame (:class:`LatLonField`), and both are put on the target grid by one
bilinear interpolation (:func:`regrid`) that works in the grid's own copy
of the world — a disk grid may run past 180°, and a global field is
re-based onto it and wrapped, so the antimeridian never falls inside a
cell. The regrid is this module's arithmetic, not a library's, so a
producer's output depends on its inputs alone.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

import numpy as np

from ..errors import ConversionError, DownloadError
from ..gdal import require_command, run_command
from ..idx import ByteRange, field_byte_range
from ..variables import VARIABLES
from .projector import TargetGrid

LOG = logging.getLogger(__name__)

#: NOAA's GFS open data bucket, the same object layout ``xuebuild.fetch``
#: reads whole runs from.
GFS_BUCKET_URL = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
#: The registered variable the skin temperature is: GFS ``TMP:surface``,
#: whose index phrase and codebook live in the registry.
SKIN_TEMPERATURE_ID = "tmpsfc"
#: GFS cycles every six hours and publishes hourly steps well past the
#: day a slot could need.
GFS_CYCLE_HOURS = 6
GFS_MAX_FORECAST_HOUR = 24
#: How many cycles back to look before giving up: the newest cycle is
#: routinely four hours behind its analysis, so the first one or two
#: candidates are absent in the ordinary course of things.
GFS_MAX_CYCLES_BACK = 5

#: The staged emissivity's variable names, the DEBRA band centres
#: ``shachen.io.emissivity.load_band_emissivity`` interpolates to; the
#: background reads the three infrared windows.
EMISSIVITY_VARIABLES = ("emis_tir_86", "emis_tir_104", "emis_tir_123")

_STAGED_NAME = re.compile(r"^(\d{4})(\d{2})\.nc$")


@dataclass(frozen=True)
class LatLonField:
    """A regular latitude/longitude raster: 1-D cell-centre coordinates
    (latitudes north to south as GDAL reads a raster, longitudes west to
    east in whatever convention the file uses) and its values, float64
    with NaN where the file carries none."""

    latitudes: np.ndarray
    longitudes: np.ndarray
    values: np.ndarray

    def __post_init__(self) -> None:
        if self.values.shape != (self.latitudes.size, self.longitudes.size):
            raise ConversionError(f"a {self.values.shape} raster does not match {self.latitudes.size} x {self.longitudes.size} coordinates")

    @property
    def extent(self) -> tuple[float, float, float, float]:
        """``(west, south, east, north)`` of the cell edges, in the file's
        longitude convention."""
        dlon = float(abs(self.longitudes[1] - self.longitudes[0])) if self.longitudes.size > 1 else 0.0
        dlat = float(abs(self.latitudes[1] - self.latitudes[0])) if self.latitudes.size > 1 else 0.0
        return (
            float(self.longitudes.min()) - dlon / 2,
            float(self.latitudes.min()) - dlat / 2,
            float(self.longitudes.max()) + dlon / 2,
            float(self.latitudes.max()) + dlat / 2,
        )


def read_raster(name: str, *, dtype: str = "Float64", options: tuple[str, ...] = ()) -> tuple[LatLonField, dict[str, str]]:
    """One band of a dataset GDAL opens on a regular latitude/longitude
    grid as a :class:`LatLonField`, with the band's metadata: the raster
    is read through ``gdal_translate`` into raw floats and the
    coordinates come from ``gdalinfo``'s geotransform, so the arithmetic
    is this module's rather than a driver's. ``options`` are GDAL
    ``--config`` pairs, spelled as ``("KEY=VALUE", …)``."""
    config: list[str] = []
    for option in options:
        key, _, value = option.partition("=")
        config += ["--config", key, value]
    result = run_command([require_command("gdalinfo"), *config, "-json", name], description=f"inspect {name}")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ConversionError(f"GDAL returned invalid JSON for {name}") from exc
    bands = info.get("bands") or []
    if len(bands) != 1:
        raise ConversionError(f"{name} must carry exactly one band, not {len(bands)}")
    width, height = (int(value) for value in info["size"])
    x0, dx, rx, y0, ry, dy = (float(value) for value in info["geoTransform"])
    if rx or ry or dx <= 0 or dy == 0:
        raise ConversionError(f"{name} is not on a north-up regular grid: {info['geoTransform']}")
    with tempfile.TemporaryDirectory(prefix="xue-ancillary-") as scratch:
        raw = Path(scratch) / "raster.bin"
        run_command(
            [require_command("gdal_translate"), *config, "-q", "-of", "ENVI", "-ot", dtype, "-co", "INTERLEAVE=BSQ", name, str(raw)],
            description=f"read {name}",
        )
        values = np.fromfile(raw, dtype={"Float64": "<f8", "Float32": "<f4"}[dtype]).astype(np.float64)
    if values.size != width * height:
        raise ConversionError(f"{name} holds {values.size} cells, not {width} x {height}")
    values = values.reshape(height, width)
    nodata = bands[0].get("noDataValue")
    if isinstance(nodata, (int, float)) and np.isfinite(nodata):
        values[values == float(nodata)] = np.nan
    longitudes = x0 + (np.arange(width, dtype=np.float64) + 0.5) * dx
    latitudes = y0 + (np.arange(height, dtype=np.float64) + 0.5) * dy
    if dy > 0:
        # A driver that hands the rows south to north: put north first.
        latitudes = latitudes[::-1]
        values = values[::-1]
    metadata = {str(key): str(value) for key, value in (bands[0].get("metadata", {}).get("", {}) or {}).items()}
    metadata.update({str(key): str(value) for key, value in (info.get("metadata", {}).get("", {}) or {}).items()})
    return LatLonField(latitudes=latitudes, longitudes=longitudes, values=values), metadata


# --------------------------------------------------------------------------
# Regridding
# --------------------------------------------------------------------------


def rebase_longitudes(longitudes: np.ndarray, west: float) -> np.ndarray:
    """Every longitude spelled in the copy of the world that starts at
    ``west``: ``west + ((lon − west) mod 360)``, so a grid that runs past
    180° (Himawari's 80.7 … 200.7) reads a global field's −170 as 190."""
    return west + np.mod(np.asarray(longitudes, dtype=np.float64) - west, 360.0)


def is_global(longitudes: np.ndarray) -> bool:
    """Whether a field's columns cover the whole parallel."""
    if longitudes.size < 2:
        return False
    step = float(np.median(np.diff(longitudes)))
    return float(longitudes[-1] - longitudes[0]) + step >= 360.0 - 1e-6


def longitudes_on_grid(field: LatLonField, grid: TargetGrid) -> tuple[np.ndarray, np.ndarray]:
    """The field's columns in the grid's copy of the world: ``(longitudes
    ascending, values)``. A global field is re-based column by column
    (:func:`rebase_longitudes`), sorted, and wrapped — its first column
    repeated a turn later — so the target cells between its last column
    and its first interpolate rather than fall outside; a regional one
    is moved whole by the turn (−360, 0 or 360) that lays most of it
    over the grid, since re-basing its columns one by one would cut it
    at the grid's western edge."""
    lons = np.asarray(field.longitudes, dtype=np.float64)
    values = field.values
    if is_global(lons):
        rebased = rebase_longitudes(lons, grid.west)
        order = np.argsort(rebased, kind="stable")
        lons = rebased[order]
        values = values[:, order]
        return np.append(lons, lons[0] + 360.0), np.concatenate([values, values[:, :1]], axis=1)
    west, east = float(lons.min()), float(lons.max())
    best = max(
        (-360.0, 0.0, 360.0),
        key=lambda turn: min(east + turn, grid.east) - max(west + turn, grid.west),
    )
    return lons + best, values


def extent_on_grid(field: LatLonField, grid: TargetGrid) -> tuple[float, float, float, float]:
    """:attr:`LatLonField.extent` in the grid's copy of the world (a
    global field's is the whole parallel)."""
    west, south, east, north = field.extent
    if is_global(field.longitudes):
        return (grid.west, south, grid.west + 360.0, north)
    lons, _ = longitudes_on_grid(LatLonField(field.latitudes, field.longitudes, field.values), grid)
    shift = float(lons[0] - field.longitudes[0])
    return (west + shift, south, east + shift, north)


def regrid(field: LatLonField, grid: TargetGrid) -> np.ndarray:
    """``field`` bilinearly interpolated onto ``grid``'s cell centres:
    ``(height, width)`` float64, NaN outside the field's extent and
    wherever a neighbouring source cell is NaN.

    Longitudes are compared in the grid's own copy of the world
    (:func:`longitudes_on_grid`). The interpolation is separable and
    exact for a linear field; nothing is extrapolated."""
    lons, values = longitudes_on_grid(field, grid)
    lats = field.latitudes
    if lats.size > 1 and lats[0] > lats[-1]:
        lats = lats[::-1]
        values = values[::-1]
    target_lons = grid.first_longitude + np.arange(grid.width, dtype=np.float64) * grid.step
    target_lats = grid.first_latitude - np.arange(grid.height, dtype=np.float64) * grid.step
    return _bilinear(lats, lons, values, target_lats, target_lons)


def _axis(source: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each target coordinate, the index of the source cell at or
    below it, the weight of the one above, and whether it lies inside
    the source axis at all (outside is never extrapolated)."""
    if source.size < 2:
        raise ConversionError("an ancillary field needs at least two cells along each axis")
    upper = np.searchsorted(source, target, side="right")
    inside = (target >= source[0]) & (target <= source[-1])
    lower = np.clip(upper - 1, 0, source.size - 2)
    span = source[lower + 1] - source[lower]
    weight = np.where(inside, (target - source[lower]) / span, 0.0)
    return lower, np.clip(weight, 0.0, 1.0), inside


def _bilinear(lats: np.ndarray, lons: np.ndarray, values: np.ndarray, target_lats: np.ndarray, target_lons: np.ndarray) -> np.ndarray:
    j0, wy, in_y = _axis(lats, target_lats)
    i0, wx, in_x = _axis(lons, target_lons)
    wy = wy[:, None]
    wx = wx[None, :]
    j0 = j0[:, None]
    i0 = i0[None, :]
    out = (
        values[j0, i0] * (1.0 - wy) * (1.0 - wx)
        + values[j0 + 1, i0] * wy * (1.0 - wx)
        + values[j0, i0 + 1] * (1.0 - wy) * wx
        + values[j0 + 1, i0 + 1] * wy * wx
    )
    out[~in_y, :] = np.nan
    out[:, ~in_x] = np.nan
    return out


# --------------------------------------------------------------------------
# Skin temperature: GFS TMP:surface
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SkinTemperatureSource:
    """Which GFS field a slot's skin temperature is, for the log and the
    frame's sidecar."""

    path: Path
    cycle: datetime
    forecast_hour: int

    @property
    def valid_time(self) -> datetime:
        return self.cycle + timedelta(hours=self.forecast_hour)

    def metadata(self) -> dict[str, object]:
        return {
            "source": "GFS 0.25° TMP:surface",
            "cycle": self.cycle.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "forecastHour": self.forecast_hour,
            "validTime": self.valid_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def skin_temperature_candidates(slot: datetime) -> list[tuple[datetime, int]]:
    """``(cycle, forecast hour)`` pairs that reach the whole hour nearest
    ``slot``, newest cycle first, so the field taken is the shortest
    forecast the bucket has rather than the first that happens to exist.
    Mirrors the operational pipeline's walk."""
    slot = slot.astimezone(UTC)
    valid = (slot + timedelta(minutes=30)).replace(minute=0, second=0, microsecond=0)
    latest = valid.replace(hour=valid.hour - valid.hour % GFS_CYCLE_HOURS)
    candidates: list[tuple[datetime, int]] = []
    for back in range(GFS_MAX_CYCLES_BACK):
        cycle = latest - timedelta(hours=GFS_CYCLE_HOURS * back)
        forecast_hour = round((valid - cycle).total_seconds() / 3600)
        if 0 <= forecast_hour <= GFS_MAX_FORECAST_HOUR:
            candidates.append((cycle, forecast_hour))
    return candidates


def gfs_object_url(cycle: datetime, forecast_hour: int) -> str:
    return f"{GFS_BUCKET_URL}/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/gfs.t{cycle:%H}z.pgrb2.0p25.f{forecast_hour:03d}"


def skin_temperature_name(cycle: datetime, forecast_hour: int) -> str:
    return f"gfs_{SKIN_TEMPERATURE_ID}_{cycle:%Y%m%d%H}_f{forecast_hour:03d}.grib2"


def fetch_skin_temperature(
    slot: datetime,
    out_dir: Path,
    *,
    fetch_text: Callable[[str], str] | None = None,
    fetch_range: Callable[[str, ByteRange], bytes] | None = None,
    exists: Callable[[str], bool] | None = None,
) -> SkinTemperatureSource:
    """The GFS ``TMP:surface`` record covering ``slot``, on disk: the
    cached one when a candidate's file is already there, else the first
    candidate cycle whose index the bucket serves, its one record fetched
    by byte range and written atomically. Nothing in the whole walk back
    is a :class:`DownloadError`."""
    if fetch_text is None or fetch_range is None or exists is None:
        from .. import fetch as _fetch  # noqa: PLC0415 - the fetch module imports this package

        fetch_text = fetch_text or _fetch.fetch_text
        fetch_range = fetch_range or _fetch.fetch_range
        exists = exists or _fetch.remote_exists
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidates = skin_temperature_candidates(slot)
    for cycle, forecast_hour in candidates:
        cached = out_dir / skin_temperature_name(cycle, forecast_hour)
        if cached.is_file():
            return SkinTemperatureSource(cached, cycle, forecast_hour)
    variable = VARIABLES[SKIN_TEMPERATURE_ID]
    tried: list[str] = []
    for cycle, forecast_hour in candidates:
        url = gfs_object_url(cycle, forecast_hour)
        tried.append(f"{cycle:%Y-%m-%d %H}Z f{forecast_hour:03d}")
        if not exists(url + ".idx"):
            continue
        byte_range = field_byte_range(
            fetch_text(url + ".idx"),
            variable.index_field,
            excluded_phrases=variable.excluded_index_phrases,
            alternate_fields=variable.alternate_index_fields,
        )
        payload = fetch_range(url, byte_range)
        target = out_dir / skin_temperature_name(cycle, forecast_hour)
        partial = target.with_name(target.name + ".part")
        partial.write_bytes(payload)
        partial.replace(target)
        LOG.info("skin temperature for %s: GFS %s f%03d (%d bytes)", slot.strftime("%Y-%m-%dT%H:%M:%SZ"), cycle.strftime("%Y%m%d%H"), forecast_hour, len(payload))
        return SkinTemperatureSource(target, cycle, forecast_hour)
    raise DownloadError(f"no GFS surface temperature reaches {slot:%Y-%m-%d %H:%M}Z; tried {', '.join(tried)}")


def skin_temperature_source(path: Path) -> SkinTemperatureSource:
    """The cycle and forecast hour a cached record was named for."""
    match = re.search(rf"gfs_{SKIN_TEMPERATURE_ID}_(\d{{10}})_f(\d{{3}})\.grib2$", Path(path).name)
    if match is None:
        raise ConversionError(f"{path} is not a cached GFS skin-temperature record")
    return SkinTemperatureSource(Path(path), datetime.strptime(match.group(1), "%Y%m%d%H").replace(tzinfo=UTC), int(match.group(2)))


def read_skin_temperature(path: Path) -> LatLonField:
    """A cached GFS record as skin temperature in kelvin on its own
    0.25° grid. GDAL is told not to normalise the unit (it would hand the
    field back in Celsius) and shifts the file's 0 … 360° longitudes onto
    −180 … 180 itself, raster and all; the record must be the surface
    temperature it was asked for."""
    field, metadata = read_raster(str(path), dtype="Float32", options=("GRIB_NORMALIZE_UNITS=NO",))
    element = metadata.get("GRIB_ELEMENT", "")
    short_name = metadata.get("GRIB_SHORT_NAME", "")
    unit = metadata.get("GRIB_UNIT", "")
    if element != "TMP" or short_name != "0-SFC":
        raise ConversionError(f"{path} is {element or '?'} at {short_name or '?'}, not the surface temperature")
    if unit.strip("[]") != "K":
        raise ConversionError(f"{path} reads in {unit or 'no unit'}, not kelvin")
    return field


# --------------------------------------------------------------------------
# Emissivity: the staged CAMEL months
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StagedEmissivity:
    """One staged CAMEL month of one region: the file, what it says of
    itself, and the three infrared windows' emissivity on its grid."""

    region: str
    path: Path
    month: str
    """The operational month the file is named for (``YYYY-MM``)."""
    source_month: str | None
    """The climatology month it carries, when the file says."""
    fields: dict[str, LatLonField]

    @property
    def extent(self) -> tuple[float, float, float, float]:
        return self.fields[EMISSIVITY_VARIABLES[0]].extent


def camel_directory(ancillary_root: Path) -> Path:
    return Path(ancillary_root) / "camel"


def gfs_directory(ancillary_root: Path) -> Path:
    return Path(ancillary_root) / "gfs"


def staged_months(camel_dir: Path) -> dict[str, list[str]]:
    """Every staged month by region, oldest first: ``{"gobi": ["202608",
    "202609"]}``."""
    months: dict[str, list[str]] = {}
    if not Path(camel_dir).is_dir():
        return months
    for region_dir in sorted(path for path in Path(camel_dir).iterdir() if path.is_dir()):
        staged = sorted(path.name[:6] for path in region_dir.iterdir() if _STAGED_NAME.match(path.name))
        if staged:
            months[region_dir.name] = staged
    return months


def staged_emissivity(camel_dir: Path, slot: datetime) -> list[StagedEmissivity]:
    """The staged CAMEL file of every region for the slot's month, read.

    A region with no file for that month is served by its newest staged
    month instead, with a warning: the product is a climatology, a month
    off is the same kind of substitution the staging itself makes across
    years, and a region dropping out of coverage at a month's turn would
    be the worse failure. A region with nothing staged is not a region.
    No staged region at all is an error: DEBRA over water alone is not
    the product."""
    wanted = slot.astimezone(UTC).strftime("%Y%m")
    regions: list[StagedEmissivity] = []
    for region, months in staged_months(camel_dir).items():
        month = wanted if wanted in months else months[-1]
        if month != wanted:
            LOG.warning("no %s emissivity is staged for %s; using its %s file", region, wanted, month)
        regions.append(read_staged_emissivity(Path(camel_dir) / region / f"{month}.nc", region=region))
    if not regions:
        raise ConversionError(f"no CAMEL emissivity is staged under {camel_dir}; run `make pull-r2-ancillary`")
    return regions


def read_staged_emissivity(path: Path, *, region: str | None = None) -> StagedEmissivity:
    fields: dict[str, LatLonField] = {}
    metadata: dict[str, str] = {}
    for variable in EMISSIVITY_VARIABLES:
        field, metadata = read_raster(f'NETCDF:"{path}":{variable}')
        fields[variable] = field
    shapes = {field.values.shape for field in fields.values()}
    if len(shapes) != 1:
        raise ConversionError(f"{path}: the emissivity variables are on different grids: {shapes}")
    month = metadata.get("NC_GLOBAL#month") or f"{path.stem[:4]}-{path.stem[4:6]}"
    return StagedEmissivity(
        region=region or metadata.get("NC_GLOBAL#region") or path.parent.name,
        path=Path(path),
        month=month,
        source_month=metadata.get("NC_GLOBAL#source_month"),
        fields=fields,
    )


def emissivity_on_grid(regions: list[StagedEmissivity], grid: TargetGrid) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """The staged regions' emissivity on the target grid, and where it is
    staged: one plane per window, NaN where no region reaches (and where
    a region carries no retrieval, which is water), and a boolean plane
    that is true inside the extent of any staged file. A later region
    fills only what an earlier one left NaN, so overlapping stagings
    agree wherever they both answer."""
    planes = {variable: np.full((grid.height, grid.width), np.nan) for variable in EMISSIVITY_VARIABLES}
    staged = np.zeros((grid.height, grid.width), dtype=bool)
    target_lons = grid.first_longitude + np.arange(grid.width, dtype=np.float64) * grid.step
    target_lats = grid.first_latitude - np.arange(grid.height, dtype=np.float64) * grid.step
    for region in regions:
        west, south, east, north = extent_on_grid(region.fields[EMISSIVITY_VARIABLES[0]], grid)
        inside = ((target_lats >= south) & (target_lats <= north))[:, None] & ((target_lons >= west) & (target_lons <= east))[None, :]
        if not inside.any():
            LOG.info("staged %s emissivity lies outside the grid", region.region)
            continue
        staged |= inside
        for variable in EMISSIVITY_VARIABLES:
            interpolated = regrid(region.fields[variable], grid)
            plane = planes[variable]
            take = np.isnan(plane) & np.isfinite(interpolated)
            plane[take] = interpolated[take]
    return planes, staged

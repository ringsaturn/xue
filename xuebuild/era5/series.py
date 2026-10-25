"""Reading a Climate Data Store ERA5 download, and writing the series files."""

from __future__ import annotations

import logging
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import XueError
from ..sources import source_spec
from ..variables import variable_spec

LOG = logging.getLogger(__name__)

INSTALL_HINT = "install the era5 dependency group: uv sync --group era5 --inexact"

MODEL = "era5"
#: The variables one window yields, in file order.
VARIABLE_IDS = ("tmp2m", "dpt2m", "ugrd10m", "vgrd10m", "gust")
#: The Data Store's request name and NetCDF short name of each. ``fg10`` is
#: the maximum 3 s gust over the hour ending at the valid time ("since
#: previous post-processing", which is hourly in ERA5), so it is declared a
#: maximum (``statistical_processes``) like ECMWF's open-data gust.
CDS_VARIABLES = {
    "tmp2m": ("2m_temperature", "t2m"),
    "dpt2m": ("2m_dewpoint_temperature", "d2m"),
    "ugrd10m": ("10m_u_component_of_wind", "u10"),
    "vgrd10m": ("10m_v_component_of_wind", "v10"),
    "gust": ("10m_wind_gust_since_previous_post_processing", "fg10"),
}
#: Temperatures arrive in kelvin; the series carry the registry's degrees
#: Celsius, so neither encoder learns another unit.
KELVIN = {"tmp2m", "dpt2m"}
SERIES_UNITS = {"°C": "degC", "m/s": "m s-1"}
LONG_NAMES = {
    "tmp2m": "2 m temperature",
    "dpt2m": "2 m dew point temperature",
    "ugrd10m": "10 m eastward wind",
    "vgrd10m": "10 m northward wind",
    "gust": "10 m wind gust, maximum over the hour ending at the valid time",
}
DATASET = "reanalysis-era5-single-levels"
CDS_URL = "https://cds.climate.copernicus.eu/api"
#: ERA5's regular grid as the Data Store serves it: 0.25° cells centred on
#: whole multiples of the step.
STEP = 0.25


class Era5Error(XueError):
    """An ERA5 download this tool cannot turn into a series."""


def _netcdf4() -> Any:
    try:
        import netCDF4  # noqa: PLC0415 - the era5 group, optional everywhere else
    except ImportError as exc:
        raise Era5Error(f"reading and writing ERA5 NetCDF needs netCDF4 ({exc}); {INSTALL_HINT}") from exc
    return netCDF4


def _cds_key() -> str:
    """The Data Store key: ``CDSAPI_KEY``, else the ``key:`` line of
    ``~/.cdsapirc``. The key is one per account across the Copernicus data
    stores, so an rc file written for another store (EWDS) still holds it;
    its ``url`` is ignored."""
    key = os.environ.get("CDSAPI_KEY")
    if key:
        return key
    rc = Path(os.environ.get("CDSAPI_RC", Path.home() / ".cdsapirc"))
    match = re.search(r"^key:\s*(\S+)", rc.read_text(), re.MULTILINE) if rc.is_file() else None
    if match is None:
        raise Era5Error(f"no Climate Data Store key: set CDSAPI_KEY or put a key: line in {rc}")
    return match.group(1)


def request(start: datetime, hours: int, bbox: tuple[float, float, float, float], target: Path) -> Path:
    """Ask the Data Store for every hour from ``start`` through ``start +
    hours`` over ``bbox`` (padded by a cell, so the crop's outer cells are
    there) and save the answer to ``target``: a NetCDF file, or a zip of
    them. The request is whole days; :func:`convert` keeps the window."""
    try:
        import cdsapi  # noqa: PLC0415 - the era5 group, optional everywhere else
    except ImportError as exc:
        raise Era5Error(f"asking the Climate Data Store needs cdsapi ({exc}); {INSTALL_HINT}") from exc
    end = start + timedelta(hours=hours)
    days = []
    day = start.replace(hour=0)
    while day <= end:
        days.append(day)
        day += timedelta(days=1)
    west, south, east, north = bbox
    query = {
        "product_type": ["reanalysis"],
        "variable": [CDS_VARIABLES[variable_id][0] for variable_id in VARIABLE_IDS],
        # One month per request is what the store accepts as a date list
        # of year/month/day; a window across a month end names both.
        "year": sorted({f"{d:%Y}" for d in days}),
        "month": sorted({f"{d:%m}" for d in days}),
        "day": sorted({f"{d:%d}" for d in days}),
        "time": [f"{hour:02d}:00" for hour in range(24)],
        "area": [north + STEP, west - STEP, south - STEP, east + STEP],
        "data_format": "netcdf",
        "download_format": "unarchived",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("asking the Climate Data Store for %s, %d days from %s", DATASET, len(days), days[0].date())
    cdsapi.Client(url=CDS_URL, key=_cds_key(), quiet=True).retrieve(DATASET, query, str(target))
    return target


@dataclass(frozen=True)
class Field:
    """One variable of the download: its valid times and its planes, rows
    as the file gives them, with the file's coordinates."""

    times: list[datetime]
    latitudes: np.ndarray
    longitudes: np.ndarray
    values: np.ndarray


def _times(dataset: Any, path: Path) -> tuple[str, list[datetime]]:
    netcdf4 = _netcdf4()
    name = next((name for name in ("valid_time", "time") if name in dataset.variables), None)
    if name is None:
        raise Era5Error(f"{path} carries no valid_time or time coordinate")
    variable = dataset[name]
    stamps = netcdf4.num2date(variable[:], variable.units, getattr(variable, "calendar", "standard"), only_use_cftime_datetimes=False)
    times = [datetime(s.year, s.month, s.day, s.hour, s.minute, s.second, tzinfo=UTC) for s in np.atleast_1d(stamps)]
    return name, times


def read_fields(path: Path) -> dict[str, Field]:
    """Every variable of :data:`VARIABLE_IDS` one download holds, by Xue id.
    ``path`` is a NetCDF file, a zip of them, or a directory of them."""
    if not path.exists():
        raise Era5Error(f"ERA5 download not found: {path}")
    if path.is_dir():
        files = sorted(item for item in path.iterdir() if item.suffix == ".nc")
        return _read_files(files, path)
    if zipfile.is_zipfile(path):
        with tempfile.TemporaryDirectory(prefix="era5-") as scratch, zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if name.endswith(".nc")]
            for name in members:
                archive.extract(name, scratch)
            return _read_files([Path(scratch) / name for name in members], path)
    return _read_files([path], path)


def _read_files(files: list[Path], origin: Path) -> dict[str, Field]:
    netcdf4 = _netcdf4()
    if not files:
        raise Era5Error(f"{origin} holds no NetCDF file")
    short_names = {short: variable_id for variable_id, (_, short) in CDS_VARIABLES.items()}
    fields: dict[str, Field] = {}
    for file in files:
        with netcdf4.Dataset(file) as dataset:
            dataset.set_auto_mask(True)
            time_name, times = _times(dataset, file)
            latitudes = np.asarray(dataset["latitude"][:], dtype=np.float64)
            longitudes = np.asarray(dataset["longitude"][:], dtype=np.float64)
            for short, variable_id in short_names.items():
                if short not in dataset.variables:
                    continue
                if variable_id in fields:
                    raise Era5Error(f"{origin} holds {short} twice")
                variable = dataset[short]
                dimensions = tuple(variable.dimensions)
                if dimensions != (time_name, "latitude", "longitude"):
                    raise Era5Error(f"{file} {short} is laid out {dimensions}, not ({time_name}, latitude, longitude)")
                values = np.ma.filled(np.ma.asarray(variable[:], dtype=np.float64), np.nan)
                unit = str(getattr(variable, "units", ""))
                expected = "K" if variable_id in KELVIN else "m s**-1"
                if unit != expected:
                    raise Era5Error(f"{file} {short} is in {unit or '<no unit>'}, not {expected}")
                if variable_id in KELVIN:
                    values = values - 273.15
                fields[variable_id] = Field(times, latitudes, longitudes, values)
    missing = [variable_id for variable_id in VARIABLE_IDS if variable_id not in fields]
    if missing:
        raise Era5Error(
            f"{origin} lacks {', '.join(CDS_VARIABLES[variable_id][1] for variable_id in missing)} "
            f"({', '.join(missing)})"
        )
    return fields


@dataclass(frozen=True)
class Window:
    """The cells written: the rows and columns of the download whose centres
    lie in the box, in the series' order (latitudes ascending)."""

    rows: np.ndarray
    columns: np.ndarray
    latitudes: np.ndarray
    longitudes: np.ndarray

    @classmethod
    def of(cls, field: Field, bbox: tuple[float, float, float, float] | None) -> Window:
        latitudes, longitudes = field.latitudes, np.where(field.longitudes >= 180.0, field.longitudes - 360.0, field.longitudes)
        for name, axis in (("latitudes", latitudes), ("longitudes", longitudes)):
            if axis.size > 1 and not np.allclose(np.abs(np.diff(axis)), STEP, atol=1e-6):
                raise Era5Error(f"the download's {name} are not on ERA5's {STEP}° grid")
            if axis.size and abs(axis[0] / STEP - round(axis[0] / STEP)) > 1e-6:
                raise Era5Error(f"the download's {name} are not centred on whole multiples of {STEP}°")
        if longitudes.size > 1 and np.any(np.diff(longitudes) <= 0):
            raise Era5Error("the download's longitudes do not run west to east (a box across the antimeridian is not read)")
        rows = np.arange(latitudes.size)
        columns = np.arange(longitudes.size)
        if bbox is not None:
            west, south, east, north = bbox
            if not (west < east and south < north):
                raise Era5Error(f"bbox {bbox} must be west, south, east, north with west < east and south < north")
            tolerance = 1e-6
            rows = rows[(latitudes >= south - tolerance) & (latitudes <= north + tolerance)]
            columns = columns[(longitudes >= west - tolerance) & (longitudes <= east + tolerance)]
            if not rows.size or not columns.size:
                raise Era5Error(f"bbox {bbox} holds no cell of the download")
        rows = rows[np.argsort(latitudes[rows])]
        return cls(rows, columns, latitudes[rows], longitudes[columns])

    @property
    def width(self) -> int:
        return int(self.columns.size)

    @property
    def height(self) -> int:
        return int(self.rows.size)

    def take(self, plane: np.ndarray) -> np.ndarray:
        return plane[np.ix_(self.rows, self.columns)]


@dataclass(frozen=True)
class SeriesSummary:
    variable_id: str
    path: Path
    frames: int
    width: int
    height: int
    minimum: float
    maximum: float

    def line(self) -> str:
        return (
            f"{self.variable_id:9s} {self.frames} frames, {self.width}x{self.height} cells, "
            f"{self.minimum:.3f}..{self.maximum:.3f} {variable_spec(self.variable_id).output_unit}"
        )


def write_series(
    path: Path, variable_id: str, run: datetime, hours: list[int], window: Window, frames: np.ndarray, attributes: dict[str, str]
) -> None:
    """One variable's CF series in the shape ``xuebuild.wrf.series.write_series``
    writes: ``time`` in hours from the run, ``step`` beside it, the run as
    ``forecast_reference_time``, ascending latitudes, float32 with a NaN
    fill, the variable named by its Xue id and in the registry's unit."""
    netcdf4 = _netcdf4()
    epoch = run.strftime("%Y-%m-%d %H:%M:%S")
    with netcdf4.Dataset(path, "w", format="NETCDF4") as dataset:
        dataset.setncatts(attributes)
        dataset.createDimension("time", None)
        dataset.createDimension("latitude", window.height)
        dataset.createDimension("longitude", window.width)
        time = dataset.createVariable("time", "f8", ("time",))
        time.setncatts({"standard_name": "time", "long_name": "valid time", "units": f"hours since {epoch}", "calendar": "standard", "axis": "T"})
        time[:] = np.asarray(hours, dtype=np.float64)
        step = dataset.createVariable("step", "f8", ("time",))
        step.setncatts({"standard_name": "forecast_period", "long_name": "time since forecast_reference_time", "units": "hours"})
        step[:] = np.asarray(hours, dtype=np.float64)
        reference = dataset.createVariable("forecast_reference_time", "f8", ())
        reference.setncatts({"standard_name": "forecast_reference_time", "long_name": "start of the reanalysis window", "units": "seconds since 1970-01-01 00:00:00", "calendar": "standard"})
        reference[...] = run.timestamp()
        latitude = dataset.createVariable("latitude", "f8", ("latitude",))
        latitude.setncatts({"standard_name": "latitude", "long_name": "latitude", "units": "degrees_north", "axis": "Y"})
        latitude[:] = window.latitudes
        longitude = dataset.createVariable("longitude", "f8", ("longitude",))
        longitude.setncatts({"standard_name": "longitude", "long_name": "longitude", "units": "degrees_east", "axis": "X"})
        longitude[:] = window.longitudes
        data = dataset.createVariable(variable_id, "f4", ("time", "latitude", "longitude"), zlib=True, complevel=1, fill_value=np.float32(np.nan))
        unit = SERIES_UNITS[variable_spec(variable_id).output_unit]
        data.setncatts({"long_name": LONG_NAMES[variable_id], "units": unit, "coordinates": "forecast_reference_time step"})
        data[:] = frames.astype(np.float32)


def convert(
    download: Path,
    out: Path,
    *,
    start: datetime,
    hours: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    force: bool = False,
    command: str = "xue era5-series",
) -> list[SeriesSummary]:
    """Turn one Data Store download into ``<out>/era5.<YYYYMMDDHH>.<id>.nc``,
    one per :data:`VARIABLE_IDS`, with a frame every hour from ``start``
    through ``start + hours`` (the source's whole axis by default). Hours
    outside the window are dropped; an hour of it the download lacks is an
    error."""
    spec = source_spec(MODEL)
    hours = spec.horizon_hours if hours is None else hours
    try:
        wanted = spec.forecast_hours(hours)
    except XueError as exc:
        raise Era5Error(str(exc)) from exc
    if start.tzinfo is None or start.minute or start.second or start.microsecond:
        raise Era5Error(f"--start {start.isoformat()} must be a whole UTC hour")
    start = start.astimezone(UTC)

    fields = read_fields(download)
    reference = fields[VARIABLE_IDS[0]]
    window = Window.of(reference, bbox)
    for variable_id, field in fields.items():
        if field.latitudes.shape != reference.latitudes.shape or not np.array_equal(field.latitudes, reference.latitudes) or not np.array_equal(field.longitudes, reference.longitudes):
            raise Era5Error(f"{download} {CDS_VARIABLES[variable_id][1]} is on another grid than {CDS_VARIABLES[VARIABLE_IDS[0]][1]}")

    stem = f"{MODEL}.{start:%Y%m%d%H}"
    out.mkdir(parents=True, exist_ok=True)
    paths = {variable_id: out / f"{stem}.{variable_id}.nc" for variable_id in VARIABLE_IDS}
    existing = [path for path in paths.values() if path.exists()]
    if existing and not force:
        raise Era5Error(f"{out} already holds {len(existing)} series of {stem} (use --force to replace them)")

    attributes = {
        "Conventions": "CF-1.10",
        "title": "ERA5 hourly reanalysis on single levels",
        "institution": "European Centre for Medium-Range Weather Forecasts (ECMWF), Copernicus Climate Change Service",
        "source": f"{download.name}: {DATASET} ({', '.join(CDS_VARIABLES[v][1] for v in VARIABLE_IDS)}), 0.25° regular grid",
        "references": "https://doi.org/10.24381/cds.adbb2d47",
        "license": "CC-BY-4.0; contains modified Copernicus Climate Change Service information",
        "history": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {command}",
        "forecast_reference_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    summaries = []
    for variable_id in VARIABLE_IDS:
        field = fields[variable_id]
        index = {time: position for position, time in enumerate(field.times)}
        missing = [hour for hour in wanted if start + timedelta(hours=hour) not in index]
        if missing:
            raise Era5Error(
                f"{download} lacks {CDS_VARIABLES[variable_id][1]} at +{', +'.join(map(str, missing[:6]))}"
                f"{' ...' if len(missing) > 6 else ''} h from {start:%Y-%m-%dT%HZ}"
            )
        frames = np.stack([window.take(field.values[index[start + timedelta(hours=hour)]]) for hour in wanted])
        if not np.isfinite(frames).all():
            raise Era5Error(f"{download} {CDS_VARIABLES[variable_id][1]} has missing cells in the window")
        write_series(paths[variable_id], variable_id, start, list(wanted), window, frames, attributes)
        summaries.append(
            SeriesSummary(variable_id, paths[variable_id], len(wanted), window.width, window.height, float(frames.min()), float(frames.max()))
        )
    LOG.info("%s: %d frames, %dx%d cells", stem, len(wanted), window.width, window.height)
    return summaries

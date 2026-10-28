"""Reading an Atmosphere Data Store CAMS download, and writing the series files."""

from __future__ import annotations

import logging
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from ..era5.series import Field, SeriesSummary, Window, _cds_key, write_series
from ..errors import XueError
from ..sources import source_spec
from ..variables import variable_spec

LOG = logging.getLogger(__name__)

INSTALL_HINT = "install the era5 dependency group: uv sync --group era5 --inexact"

MODEL = "cams"
#: The variables one window yields, in file order.
VARIABLE_IDS = ("aod", "aodorg", "pm25")
#: The Data Store's request name, NetCDF short name, the unit the file
#: reports and the factor that turns it into the registry's unit.
ADS_VARIABLES = {
    "aod": ("total_aerosol_optical_depth_550nm", "aod550", "~", 1.0),
    "aodorg": ("organic_matter_aerosol_optical_depth_550nm", "omaod550", "~", 1.0),
    "pm25": ("particulate_matter_2.5um", "pm2p5", "kg m**-3", 1e9),
}
LONG_NAMES = {
    "aod": "total aerosol optical depth at 550 nm",
    "aodorg": "organic matter aerosol optical depth at 550 nm",
    "pm25": "particulate matter of diameter up to 2.5 um, surface concentration",
}
DATASET = "cams-global-atmospheric-composition-forecasts"
ADS_URL = "https://ads.atmosphere.copernicus.eu/api"
#: The two cycles a day and the steps of each that the window is made of:
#: every cycle's analysis and the short forecasts up to the next one, so
#: each frame is the newest the system had at its valid time.
CYCLES = (0, 12)
LEADS = (0, 3, 6, 9)
#: CAMS's regular grid as the Data Store serves it: 0.4° cells centred on
#: whole multiples of the step.
STEP = 0.4


class CamsError(XueError):
    """A CAMS download this tool cannot turn into a series."""


def _netcdf4() -> Any:
    try:
        import netCDF4  # noqa: PLC0415 - the era5 group, optional everywhere else
    except ImportError as exc:
        raise CamsError(f"reading and writing CAMS NetCDF needs netCDF4 ({exc}); {INSTALL_HINT}") from exc
    return netCDF4


def request(start: datetime, hours: int, bbox: tuple[float, float, float, float], target: Path) -> Path:
    """Ask the Data Store for every cycle's analysis and first steps from
    ``start`` through ``start + hours`` over ``bbox`` (padded by a cell, so
    the crop's outer cells are there) and save the answer to ``target``.
    The request is whole days; :func:`convert` keeps the window."""
    try:
        import cdsapi  # noqa: PLC0415 - the era5 group, optional everywhere else
    except ImportError as exc:
        raise CamsError(f"asking the Atmosphere Data Store needs cdsapi ({exc}); {INSTALL_HINT}") from exc
    end = start + timedelta(hours=hours)
    first, last = start.date(), end.date()
    west, south, east, north = bbox
    query = {
        "variable": [ADS_VARIABLES[variable_id][0] for variable_id in VARIABLE_IDS],
        # Unlike the Climate Data Store's year/month/day lists, a date range
        # names exactly the days between.
        "date": [f"{first:%Y-%m-%d}/{last:%Y-%m-%d}"],
        "time": [f"{hour:02d}:00" for hour in CYCLES],
        "leadtime_hour": [str(lead) for lead in LEADS],
        "type": ["forecast"],
        "area": [north + STEP, west - STEP, south - STEP, east + STEP],
        "data_format": "netcdf_zip",
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    LOG.info("asking the Atmosphere Data Store for %s, %s to %s", DATASET, first, last)
    cdsapi.Client(url=ADS_URL, key=_cds_key(), quiet=True).retrieve(DATASET, query, str(target))
    return target


def read_fields(path: Path) -> dict[str, Field]:
    """Every variable of :data:`VARIABLE_IDS` one download holds, by Xue id,
    its frames ordered by valid time and in the registry's unit. ``path``
    is a NetCDF file, a zip of them, or a directory of them."""
    if not path.exists():
        raise CamsError(f"CAMS download not found: {path}")
    if path.is_dir():
        return _read_files(sorted(item for item in path.iterdir() if item.suffix == ".nc"), path)
    if zipfile.is_zipfile(path):
        with tempfile.TemporaryDirectory(prefix="cams-") as scratch, zipfile.ZipFile(path) as archive:
            members = [name for name in archive.namelist() if name.endswith(".nc")]
            for name in members:
                archive.extract(name, scratch)
            return _read_files([Path(scratch) / name for name in members], path)
    return _read_files([path], path)


def _valid_times(dataset: Any, file: Path) -> tuple[list[datetime], list[int]]:
    """The valid time of every (cycle, lead) pair, cycle-major, and the lead
    of each: a frame's lead decides which of two cycles reaching the same
    hour is kept."""
    netcdf4 = _netcdf4()
    for name in ("forecast_reference_time", "forecast_period"):
        if name not in dataset.variables:
            raise CamsError(f"{file} carries no {name} coordinate")
    reference = dataset["forecast_reference_time"]
    stamps = netcdf4.num2date(reference[:], reference.units, only_use_cftime_datetimes=False)
    cycles = [datetime(s.year, s.month, s.day, s.hour, tzinfo=UTC) for s in np.atleast_1d(stamps)]
    period = dataset["forecast_period"]
    if str(getattr(period, "units", "")) != "hours":
        raise CamsError(f"{file} forecast_period is not counted in hours")
    leads = [int(round(float(value))) for value in np.atleast_1d(period[:])]
    times = [cycle + timedelta(hours=lead) for cycle in cycles for lead in leads]
    return times, [lead for _ in cycles for lead in leads]


def _read_files(files: list[Path], origin: Path) -> dict[str, Field]:
    netcdf4 = _netcdf4()
    if not files:
        raise CamsError(f"{origin} holds no NetCDF file")
    short_names = {short: variable_id for variable_id, (_, short, _, _) in ADS_VARIABLES.items()}
    fields: dict[str, Field] = {}
    for file in files:
        with netcdf4.Dataset(file) as dataset:
            dataset.set_auto_mask(True)
            times, leads = _valid_times(dataset, file)
            # The store writes 0.4° multiples with float noise (-1.2000000000000026);
            # rounded, the grid's first cell is the number the step names.
            latitudes = np.round(np.asarray(dataset["latitude"][:], dtype=np.float64), 6)
            longitudes = np.round(np.asarray(dataset["longitude"][:], dtype=np.float64), 6)
            for short, variable_id in short_names.items():
                if short not in dataset.variables:
                    continue
                if variable_id in fields:
                    raise CamsError(f"{origin} holds {short} twice")
                variable = dataset[short]
                dimensions = tuple(variable.dimensions)
                expected_dimensions = ("forecast_period", "forecast_reference_time", "latitude", "longitude")
                if dimensions != expected_dimensions:
                    raise CamsError(f"{file} {short} is laid out {dimensions}, not {expected_dimensions}")
                _, _, unit, factor = ADS_VARIABLES[variable_id]
                if str(getattr(variable, "units", "")) != unit:
                    raise CamsError(f"{file} {short} is in {getattr(variable, 'units', '') or '<no unit>'}, not {unit}")
                values = np.ma.filled(np.ma.asarray(variable[:], dtype=np.float64), np.nan) * factor
                # (lead, cycle, y, x) → cycle-major, the order of the times.
                planes = values.transpose(1, 0, 2, 3).reshape(-1, *values.shape[2:])
                # Where two cycles reach the same hour (a 12-hour step of
                # the earlier against the next analysis) the shorter lead
                # is the newer word on it.
                kept: dict[datetime, tuple[int, int]] = {}
                for position, (time, lead) in enumerate(zip(times, leads, strict=True)):
                    if time not in kept or lead < kept[time][1]:
                        kept[time] = (position, lead)
                order = sorted(kept)
                fields[variable_id] = Field(order, latitudes, longitudes, planes[[kept[time][0] for time in order]])
    missing = [variable_id for variable_id in VARIABLE_IDS if variable_id not in fields]
    if missing:
        raise CamsError(
            f"{origin} lacks {', '.join(ADS_VARIABLES[variable_id][1] for variable_id in missing)} ({', '.join(missing)})"
        )
    return fields


def convert(
    download: Path,
    out: Path,
    *,
    start: datetime,
    hours: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    force: bool = False,
    command: str = "xue cams-series",
) -> list[SeriesSummary]:
    """Turn one Data Store download into ``<out>/cams.<YYYYMMDDHH>.<id>.nc``,
    one per :data:`VARIABLE_IDS`, with a frame every three hours from
    ``start`` through ``start + hours`` (the source's whole axis by
    default). Frames outside the window are dropped; one of it the download
    lacks is an error."""
    spec = source_spec(MODEL)
    hours = spec.horizon_hours if hours is None else hours
    try:
        wanted = spec.forecast_hours(hours)
    except XueError as exc:
        raise CamsError(str(exc)) from exc
    if start.tzinfo is None or start.minute or start.second or start.microsecond or start.hour % spec.cycle_hours:
        raise CamsError(f"--start {start.isoformat()} must be a UTC hour on the {spec.cycle_hours}-hourly axis")
    start = start.astimezone(UTC)

    fields = read_fields(download)
    reference = fields[VARIABLE_IDS[0]]
    window = Window.of(reference, bbox, STEP)
    for variable_id, field in fields.items():
        if not np.array_equal(field.latitudes, reference.latitudes) or not np.array_equal(field.longitudes, reference.longitudes):
            raise CamsError(f"{download} {ADS_VARIABLES[variable_id][1]} is on another grid than {ADS_VARIABLES[VARIABLE_IDS[0]][1]}")

    stem = f"{MODEL}.{start:%Y%m%d%H}"
    out.mkdir(parents=True, exist_ok=True)
    paths = {variable_id: out / f"{stem}.{variable_id}.nc" for variable_id in VARIABLE_IDS}
    existing = [path for path in paths.values() if path.exists()]
    if existing and not force:
        raise CamsError(f"{out} already holds {len(existing)} series of {stem} (use --force to replace them)")

    attributes = {
        "Conventions": "CF-1.10",
        "title": "CAMS global atmospheric composition analyses and short forecasts",
        "institution": "European Centre for Medium-Range Weather Forecasts (ECMWF), Copernicus Atmosphere Monitoring Service",
        "source": f"{download.name}: {DATASET} ({', '.join(ADS_VARIABLES[v][1] for v in VARIABLE_IDS)}), "
        f"the {' and '.join(f'{c:02d}Z' for c in CYCLES)} cycles at +{', +'.join(map(str, LEADS))} h, 0.4° regular grid",
        "references": "https://doi.org/10.24381/04a0b097",
        "license": "CC-BY-4.0; contains modified Copernicus Atmosphere Monitoring Service information",
        "history": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {command}",
        "forecast_reference_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    summaries = []
    for variable_id in VARIABLE_IDS:
        field = fields[variable_id]
        index = {time: position for position, time in enumerate(field.times)}
        missing = [hour for hour in wanted if start + timedelta(hours=hour) not in index]
        if missing:
            raise CamsError(
                f"{download} lacks {ADS_VARIABLES[variable_id][1]} at +{', +'.join(map(str, missing[:6]))}"
                f"{' ...' if len(missing) > 6 else ''} h from {start:%Y-%m-%dT%HZ}"
            )
        frames = np.stack([window.take(field.values[index[start + timedelta(hours=hour)]]) for hour in wanted])
        if not np.isfinite(frames).all():
            raise CamsError(f"{download} {ADS_VARIABLES[variable_id][1]} has missing cells in the window")
        write_series(
            paths[variable_id],
            variable_id,
            start,
            list(wanted),
            window,
            frames,
            attributes,
            long_name=LONG_NAMES[variable_id],
            unit=variable_spec(variable_id).output_unit,
            reference_name="start of the analysis window",
        )
        summaries.append(
            SeriesSummary(variable_id, paths[variable_id], len(wanted), window.width, window.height, float(frames.min()), float(frames.max()))
        )
    LOG.info("%s: %d frames, %dx%d cells", stem, len(wanted), window.width, window.height)
    return summaries

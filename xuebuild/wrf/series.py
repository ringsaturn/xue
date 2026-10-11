"""Writing the series files, and the whole conversion of one domain."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from ..variables import CLOUD_WATER_LEVELS_M, CLOUD_WATER_VARIABLE_IDS, variable_spec
from .derive import derive, rain_total
from .reader import WrfError, WrfRun, _netcdf4, open_run, read_frame
from .regrid import Sampler, build_sampler, place_grid

LOG = logging.getLogger(__name__)

#: The variables one run yields, in file order.
VARIABLE_IDS = (
    "tmp2m", "dpt2m", "tmpsfc", "ugrd10m", "vgrd10m", "apcp", "tcdc", "lcdc", "mcdc", "hcdc", "hpbl", "dswrf", "orog",
    *CLOUD_WATER_VARIABLE_IDS,
)  # fmt: skip

#: The unit each series declares: the registry's output unit in the
#: udunits spelling ``observation.accepted_series_units`` admits.
SERIES_UNITS = {
    "°C": "degC",
    "m/s": "m s-1",
    "W/m²": "W m-2",
}

LONG_NAMES = {
    "tmp2m": "2 m temperature",
    "dpt2m": "2 m dew point temperature",
    "tmpsfc": "surface skin temperature",
    "ugrd10m": "10 m eastward wind",
    "vgrd10m": "10 m northward wind",
    "apcp": "precipitation over the past hour",
    "tcdc": "total cloud cover",
    "lcdc": "low cloud cover",
    "mcdc": "middle cloud cover",
    "hcdc": "high cloud cover",
    "hpbl": "planetary boundary layer height",
    "dswrf": "downward shortwave radiation at the surface",
    "orog": "terrain height",
    **{
        variable_id: f"cloud water mixing ratio at {level / 1000:g} km MSL"
        for variable_id, level in zip(CLOUD_WATER_VARIABLE_IDS, CLOUD_WATER_LEVELS_M)
    },
}


def series_unit(variable_id: str) -> str:
    unit = variable_spec(variable_id).output_unit
    return SERIES_UNITS.get(unit, unit)


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
            f"{self.minimum:.3f}..{self.maximum:.3f} {series_unit(self.variable_id)}"
        )


def write_series(
    path: Path,
    variable_id: str,
    cycle: datetime,
    lead_hours: list[int],
    sampler: Sampler,
    frames: np.ndarray,
    attributes: dict[str, str],
) -> None:
    """One variable's CF series: ``time`` counted in hours from the cycle,
    ascending latitudes, float32 with a NaN fill. The file is named by the
    Xue id, and so is the variable inside it (the converter reads a series
    variable under its id on every source but the Open-Meteo one,
    ``observation.series_variable_name``)."""
    netcdf4 = _netcdf4()
    epoch = cycle.strftime("%Y-%m-%d %H:%M:%S")
    with netcdf4.Dataset(path, "w", format="NETCDF4") as dataset:
        dataset.setncatts(attributes)
        dataset.createDimension("time", None)
        dataset.createDimension("latitude", sampler.height)
        dataset.createDimension("longitude", sampler.width)
        time = dataset.createVariable("time", "f8", ("time",))
        time.setncatts({"standard_name": "time", "long_name": "valid time", "units": f"hours since {epoch}", "calendar": "standard", "axis": "T"})
        time[:] = np.asarray(lead_hours, dtype=np.float64)
        step = dataset.createVariable("step", "f8", ("time",))
        step.setncatts({"standard_name": "forecast_period", "long_name": "time since forecast_reference_time", "units": "hours"})
        step[:] = np.asarray(lead_hours, dtype=np.float64)
        reference = dataset.createVariable("forecast_reference_time", "f8", ())
        reference.setncatts({"standard_name": "forecast_reference_time", "long_name": "initial time of forecast", "units": "seconds since 1970-01-01 00:00:00", "calendar": "standard"})
        reference[...] = cycle.timestamp()
        latitude = dataset.createVariable("latitude", "f8", ("latitude",))
        latitude.setncatts({"standard_name": "latitude", "long_name": "latitude", "units": "degrees_north", "axis": "Y"})
        latitude[:] = sampler.latitudes
        longitude = dataset.createVariable("longitude", "f8", ("longitude",))
        longitude.setncatts({"standard_name": "longitude", "long_name": "longitude", "units": "degrees_east", "axis": "X"})
        longitude[:] = sampler.longitudes
        data = dataset.createVariable(variable_id, "f4", ("time", "latitude", "longitude"), zlib=True, complevel=1, fill_value=np.float32(np.nan))
        data.setncatts({"long_name": LONG_NAMES[variable_id], "units": series_unit(variable_id), "coordinates": "forecast_reference_time step"})
        data[:] = frames.astype(np.float32)


def convert_run(run_dir: Path, domain: str, out: Path, *, step: float = 0.005, force: bool = False, command: str = "xue wrf-series") -> list[SeriesSummary]:
    """Turn one domain of a run into ``<out>/woof.<YYYYMMDDHH>.<id>.nc``,
    one per :data:`VARIABLE_IDS`, with forecast hours 1..N. The analysis
    (hour 0) is the driving model interpolated to the nest, not a WOOF
    forecast, so it is only the left endpoint of the first hour's rain."""
    run = open_run(run_dir, domain)
    if len(run.files) < 2:
        raise WrfError(f"{domain} holds the analysis alone; a series needs at least one forecast hour")
    stem = f"woof.{run.grid.start:%Y%m%d%H}"
    out.mkdir(parents=True, exist_ok=True)
    paths = {variable_id: out / f"{stem}.{variable_id}.nc" for variable_id in VARIABLE_IDS}
    existing = [path for path in paths.values() if path.exists()]
    if existing and not force:
        raise WrfError(f"{out} already holds {len(existing)} series of {stem} (use --force to replace them)")

    sampler: Sampler | None = None
    previous_rain: np.ndarray | None = None
    planes: dict[str, list[np.ndarray]] = {variable_id: [] for variable_id in VARIABLE_IDS}
    lead_hours: list[int] = []
    for index, path in enumerate(run.files):
        valid, fields = read_frame(path)
        expected = run.grid.start + timedelta(hours=index)
        if valid != expected:
            raise WrfError(f"{path} is valid at {valid.isoformat()}, its name says {expected.isoformat()}")
        if sampler is None:
            mass = place_grid(run.grid, fields["XLAT"], fields["XLONG"])
            sampler = build_sampler(mass, step)
            LOG.info("%s %s: %dx%d mass points at %.0f m onto %dx%d cells at %g°", run.name, domain, run.grid.nx, run.grid.ny, run.grid.dx, sampler.width, sampler.height, step)
        if previous_rain is None:
            previous_rain = rain_total(fields)
            continue
        derived = derive(fields, previous_rain)
        previous_rain = rain_total(fields)
        for variable_id in VARIABLE_IDS:
            planes[variable_id].append(sampler.take(derived[variable_id]))
        lead_hours.append(index)
    assert sampler is not None

    attributes = {
        "Conventions": "CF-1.10",
        "title": f"Recast WOOF (WRF-ARW) forecast, domain {domain}",
        "source": (
            f"Recast run {run.name}, gpuwm {run.gpuwm_version}, domain {domain}: "
            f"{run.grid.nx}x{run.grid.ny} mass points at {run.grid.dx:.0f} m, Lambert conformal "
            f"({run.grid.truelat1}, {run.grid.truelat2}, {run.grid.stand_lon}), sampled bilinearly onto {step}°"
        ),
        "history": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {command}",
        "forecast_reference_time": run.grid.start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    summaries = []
    for variable_id in VARIABLE_IDS:
        frames = np.stack(planes[variable_id])
        write_series(paths[variable_id], variable_id, run.grid.start, lead_hours, sampler, frames, attributes)
        summaries.append(
            SeriesSummary(variable_id, paths[variable_id], len(lead_hours), sampler.width, sampler.height, float(np.nanmin(frames)), float(np.nanmax(frames)))
        )
    return summaries

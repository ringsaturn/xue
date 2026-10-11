"""Reading a WRF run: which ``wrfout`` files make up one domain's hourly
axis, the grid the header declares, and the fields of one output time."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import XueError

INSTALL_HINT = "install the wrf group (uv sync --group wrf)"

#: The fields one output time is read for. ``Times`` is the valid time.
FIELDS = (
    "T2", "Q2", "PSFC", "U10", "V10", "TSK", "HGT", "PBLH", "SWDOWN",
    "RAINNC", "RAINC", "RAINSH", "PH", "PHB", "U", "V", "CLDFRA", "QCLOUD",
    "COSALPHA", "SINALPHA", "XLAT", "XLONG",
)  # fmt: skip

_FILE_RE = re.compile(r"^wrfout_(d\d\d)_(\d{4}-\d{2}-\d{2}_\d{2}_\d{2}_\d{2})$")
_WRF_TIME = "%Y-%m-%d_%H_%M_%S"
_GPUWM_VERSION = re.compile(r"gpuwm-(\d+\.\d+\.\d+)")


class WrfError(XueError):
    """A WRF run this tool cannot turn into a series."""


def _netcdf4() -> Any:
    try:
        import netCDF4  # noqa: PLC0415 - the wrf group, optional everywhere else
    except ImportError as exc:
        raise WrfError(f"reading wrfout files needs netCDF4 ({exc}); {INSTALL_HINT}") from exc
    return netCDF4


@dataclass(frozen=True)
class WrfGrid:
    """The mass grid of one domain as its header declares it: a Lambert
    conformal conic on WRF's 6 370 km sphere (``MAP_PROJ`` 1)."""

    domain: str
    nx: int
    """Mass points west to east (``west_east``)."""
    ny: int
    """Mass points south to north (``south_north``)."""
    dx: float
    dy: float
    truelat1: float
    truelat2: float
    stand_lon: float
    moad_cen_lat: float
    cen_lat: float
    cen_lon: float
    start: datetime


@dataclass(frozen=True)
class WrfRun:
    """One domain's hourly output files in time order, starting at the
    simulation start."""

    run_dir: Path
    name: str
    gpuwm_version: str
    grid: WrfGrid
    files: tuple[Path, ...]


def parse_wrf_time(text: str) -> datetime:
    """A WRF time stamp: ``Times`` and the header write ``_HH:MM:SS``, a
    file name spells the same stamp with underscores."""
    return datetime.strptime(text.strip().replace(":", "_"), _WRF_TIME).replace(tzinfo=UTC)


def read_grid(path: Path) -> WrfGrid:
    """The domain grid out of one file's global attributes."""
    with _netcdf4().Dataset(path) as dataset:
        attribute = dataset.getncattr
        if int(attribute("MAP_PROJ")) != 1:
            raise WrfError(f"{path}: MAP_PROJ {attribute('MAP_PROJ')} is not Lambert conformal (1)")
        return WrfGrid(
            domain=f"d{int(attribute('GRID_ID')):02d}",
            nx=len(dataset.dimensions["west_east"]),
            ny=len(dataset.dimensions["south_north"]),
            dx=float(attribute("DX")),
            dy=float(attribute("DY")),
            truelat1=float(attribute("TRUELAT1")),
            truelat2=float(attribute("TRUELAT2")),
            stand_lon=float(attribute("STAND_LON")),
            moad_cen_lat=float(attribute("MOAD_CEN_LAT")),
            cen_lat=float(attribute("CEN_LAT")),
            cen_lon=float(attribute("CEN_LON")),
            start=parse_wrf_time(str(attribute("SIMULATION_START_DATE"))),
        )


def read_frame(path: Path) -> tuple[datetime, dict[str, np.ndarray]]:
    """The valid time and every field of :data:`FIELDS` at the file's first
    (its only) output time, as float64 arrays with the ``Time`` axis gone."""
    with _netcdf4().Dataset(path) as dataset:
        times = dataset.variables["Times"][:]
        valid = parse_wrf_time(times[0].tobytes().decode("ascii"))
        fields: dict[str, np.ndarray] = {}
        for name in FIELDS:
            if name not in dataset.variables:
                raise WrfError(f"{path} carries no {name}")
            fields[name] = np.asarray(dataset.variables[name][0], dtype=np.float64)
    return valid, fields


def open_run(run_dir: Path, domain: str) -> WrfRun:
    """The domain's output files, checked to be one contiguous hourly axis
    from the simulation start, and what ``experiment.toml`` and the event
    log say about the run."""
    experiment = run_dir / "experiment.toml"
    if not experiment.is_file():
        raise WrfError(f"{run_dir} holds no experiment.toml")
    name = str(tomllib.loads(experiment.read_text()).get("experiment", {}).get("name", run_dir.name))
    events = run_dir / "events.jsonl"
    match = _GPUWM_VERSION.search(events.read_text()) if events.is_file() else None
    gpuwm_version = match.group(1) if match else "unknown"

    wrfout = run_dir / "run" / "wrfout"
    by_time: dict[datetime, Path] = {}
    for path in wrfout.iterdir() if wrfout.is_dir() else ():
        found = _FILE_RE.match(path.name)
        if found and found.group(1) == domain:
            by_time[parse_wrf_time(found.group(2))] = path
    if not by_time:
        raise WrfError(f"{wrfout} holds no wrfout_{domain}_* file")
    files = [by_time[time] for time in sorted(by_time)]
    grid = read_grid(files[0])
    times = sorted(by_time)
    if times[0] != grid.start:
        raise WrfError(f"{domain} starts at {times[0].isoformat()}, not the simulation start {grid.start.isoformat()}")
    for earlier, later in zip(times, times[1:]):
        if later - earlier != timedelta(hours=1):
            raise WrfError(f"{domain} output is not hourly between {earlier.isoformat()} and {later.isoformat()}")
    return WrfRun(run_dir=run_dir, name=name, gpuwm_version=gpuwm_version, grid=grid, files=tuple(files))

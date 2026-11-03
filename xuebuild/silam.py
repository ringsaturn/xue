"""The SILAM global air-quality forecast as the Finnish Meteorological
Institute publishes it on AWS open data, fetched into the series files both
encoders read (``docs/contribution/sources.md``, "silam").

FMI runs SILAM v6.1 once a day from 00Z on a 0.2° global grid and writes
the surface fields to the ``fmi-opendata-silam-surface-netcdf`` bucket:
``global/<YYYYMMDD>/silam_glob_v6_1_<YYYYMMDD>_<FIELD>_d<N>.nc``, one
NetCDF-4 file per field and forecast day ``N`` (0–4), 24 hourly frames
each, the first at +1 h. A day's file is about 35 MB of deflated HDF5, so it
is fetched whole and read with netCDF4 (the ``silam`` dependency group,
which only its build job syncs); each variable's run is written as one
classic NetCDF series (``silam.<run>.<id>.nc``) by
:func:`xuebuild.cmadust.write_series`, so the converters read it like any
series.

Each value is the mean over the hour that ends at its time (``cell_methods
= "time: mean"``), so a three-hourly frame is still the last hour's mean.
The file's time unit is counted from a reference two days before the
folder's date; only the valid times matter, and they must be the run's.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from .cmadust import write_series
from .common import S3_NAMESPACE, s3_list_pages, s3_list_query
from .errors import DownloadError
from .model import GfsRun
from .sources import SourceSpec

LOG = logging.getLogger(__name__)

MODEL = "silam"

BASE_URL = os.environ.get("XUE_SILAM_BASE_URL", "https://fmi-opendata-silam-surface-netcdf.s3.amazonaws.com").rstrip("/")

#: The file's name for each published variable; both are in µg/m³.
FILE_VARIABLES = {"pm25": "PM25", "pm10": "PM10"}
FILE_UNIT = "ug/m3"
SERIES_UNIT = "ug/m3"
LONG_NAMES = {"pm25": "Surface PM2.5 concentration", "pm10": "Surface PM10 concentration"}

#: The model's regular grid as the file holds it: 1800 x 897 cells of 0.2°,
#: centres from -179.8° to 180.0° and from -89.6° to 89.6°, latitudes
#: ascending. The series roll the 180° column to the west edge so the
#: longitudes run -180.0 to 179.8, the GFS layout.
FILE_WEST = -179.8
GRID_WEST = -180.0
GRID_SOUTH = -89.6
GRID_STEP = 0.2
GRID_WIDTH = 1800
GRID_HEIGHT = 897
#: The file's coordinates are float32 values widened to double.
COORDINATE_TOLERANCE = 1e-3

HOURS_PER_FILE = 24
FORECAST_DAYS = 5

#: A day's file is some 35 MB.
FILE_TIMEOUT = 120
FILE_MAX_ELAPSED = 900

FETCH_FILENAME = "fetch.json"
INSTALL_HINT = "uv sync --group silam"

#: The HDF5 library under netCDF4 is not thread-safe: the day files come
#: down in parallel, but one is read at a time.
_READ_LOCK = threading.Lock()

_EPOCH_RE = re.compile(r"^seconds since (\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\s*(?:UTC|Z|\+00:?00))?$")


def day_of(hour: int) -> int:
    """The forecast day file holding lead ``hour`` (+1..+24 is day 0)."""
    if hour < 1 or hour > HOURS_PER_FILE * FORECAST_DAYS:
        raise ValueError(f"SILAM publishes +1 to +{HOURS_PER_FILE * FORECAST_DAYS} h, not +{hour}")
    return (hour - 1) // HOURS_PER_FILE


def days_for(hours: list[int]) -> list[int]:
    return sorted({day_of(hour) for hour in hours})


def file_key(run: GfsRun, variable_id: str, day: int) -> str:
    """The object key of one field's forecast day."""
    date = f"{run.time:%Y%m%d}"
    return f"global/{date}/silam_glob_v6_1_{date}_{FILE_VARIABLES[variable_id]}_d{day}.nc"


def file_url(key: str) -> str:
    return f"{BASE_URL}/{key}"


def list_run(run: GfsRun, *, fetch: Callable[[str], str]) -> set[str]:
    """Every object key of one run's folder. A listing that cannot be read
    is an error, never an empty run."""
    prefix = f"global/{run.time:%Y%m%d}/"
    keys: set[str] = set()
    pages = s3_list_pages(
        lambda token: fetch(f"{BASE_URL}/?{s3_list_query(prefix, token)}"), f"the SILAM listing of {prefix}", max_pages=20
    )
    for root in pages:
        if root.tag != f"{S3_NAMESPACE}ListBucketResult":
            raise DownloadError(f"the SILAM listing of {prefix} is {root.tag}, not an S3 listing")
        for contents in root.iter(f"{S3_NAMESPACE}Contents"):
            key = contents.findtext(f"{S3_NAMESPACE}Key")
            if key:
                keys.add(key)
    return keys


def missing_files(spec: SourceSpec, run: GfsRun, hours: int, keys: set[str], variable_ids: tuple[str, ...] | None = None) -> list[str]:
    days = days_for(spec.forecast_hours(hours))
    return [
        file_key(run, variable_id, day)
        for variable_id in (variable_ids or spec.input_variable_ids)
        for day in days
        if file_key(run, variable_id, day) not in keys
    ]


def run_is_complete(spec: SourceSpec, run: GfsRun, hours: int, *, fetch: Callable[[str], str]) -> bool:
    """Whether the folder holds every field's day files through ``hours``."""
    return not missing_files(spec, run, hours, list_run(run, fetch=fetch))


# -- One day's file --------------------------------------------------------


def file_coordinates() -> tuple[np.ndarray, np.ndarray]:
    latitudes = np.round(GRID_SOUTH + GRID_STEP * np.arange(GRID_HEIGHT), 6)
    longitudes = np.round(FILE_WEST + GRID_STEP * np.arange(GRID_WIDTH), 6)
    return latitudes, longitudes


def grid_coordinates() -> tuple[np.ndarray, np.ndarray]:
    """The cell centres as the series carry them: latitudes ascending, the
    file's own order, longitudes from -180°."""
    latitudes = np.round(GRID_SOUTH + GRID_STEP * np.arange(GRID_HEIGHT), 6)
    longitudes = np.round(GRID_WEST + GRID_STEP * np.arange(GRID_WIDTH), 6)
    return latitudes, longitudes


def _netcdf4() -> Any:
    try:
        import netCDF4  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DownloadError(f"reading a SILAM file needs netCDF4 ({exc}); {INSTALL_HINT}") from exc
    return netCDF4


def _attribute(variable: Any, name: str) -> Any:
    return variable.getncattr(name) if name in variable.ncattrs() else None


def _epoch(units: str, label: str) -> datetime:
    match = _EPOCH_RE.match(units.strip())
    if not match:
        raise DownloadError(f"{label}: time is in {units or '<no unit>'}, not seconds since a UTC instant")
    return datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}").replace(tzinfo=UTC)


def read_day(
    payload: bytes,
    run: GfsRun,
    variable_id: str,
    hours: list[int],
    *,
    label: str,
    latitudes: np.ndarray | None = None,
    longitudes: np.ndarray | None = None,
    roll: bool = True,
) -> dict[int, np.ndarray]:
    """The planes of ``hours`` in one day's file, held to the product: its
    coordinates the grid's as the file holds them (or the ``latitudes`` /
    ``longitudes`` given, for a box), each wanted hour's valid time in it,
    the field float32 in µg/m³. The fill becomes NaN and, on the whole
    grid, the 180° column moves to the west edge."""
    with _READ_LOCK:
        return _read_day(payload, run, variable_id, hours, label=label, latitudes=latitudes, longitudes=longitudes, roll=roll)


def _read_day(
    payload: bytes,
    run: GfsRun,
    variable_id: str,
    hours: list[int],
    *,
    label: str,
    latitudes: np.ndarray | None,
    longitudes: np.ndarray | None,
    roll: bool,
) -> dict[int, np.ndarray]:
    netCDF4 = _netcdf4()
    if latitudes is None or longitudes is None:
        latitudes, longitudes = file_coordinates()
    try:
        dataset = netCDF4.Dataset("silam.nc", mode="r", memory=payload)
    except OSError as exc:
        raise DownloadError(f"{label} is not a NetCDF-4 file ({exc})") from exc
    with dataset:
        dataset.set_auto_mask(False)
        dataset.set_auto_scale(False)
        for name, expected in (("lat", latitudes), ("lon", longitudes)):
            if name not in dataset.variables:
                raise DownloadError(f"{label} carries no {name}")
            actual = np.asarray(dataset.variables[name][:], dtype=np.float64)
            if actual.shape != expected.shape or not np.allclose(actual, expected, rtol=0, atol=COORDINATE_TOLERANCE):
                raise DownloadError(f"{label}: {name} is not the product's 0.2° grid from {expected[0]:g}")
        time = dataset.variables.get("time")
        if time is None:
            raise DownloadError(f"{label} carries no time")
        epoch = _epoch(str(_attribute(time, "units") or ""), label)
        valid = [epoch + timedelta(seconds=float(value)) for value in np.asarray(time[:]).ravel()]
        index = {moment: position for position, moment in enumerate(valid)}
        name = FILE_VARIABLES[variable_id]
        variable = dataset.variables.get(name)
        if variable is None:
            raise DownloadError(f"{label} carries no {name}")
        unit = str(_attribute(variable, "units") or "").strip()
        if unit != FILE_UNIT:
            raise DownloadError(f"{label}: {name} is in {unit or '<no unit>'}, not {FILE_UNIT}")
        if variable.dtype != np.float32:
            raise DownloadError(f"{label}: {name} is {variable.dtype}, not float32")
        if tuple(variable.dimensions) != ("time", "lat", "lon"):
            raise DownloadError(f"{label}: {name} is {variable.dimensions}, not (time, lat, lon)")
        fills = [np.float32(fill) for fill in (_attribute(variable, "_FillValue"), _attribute(variable, "missing_value")) if fill is not None]
        planes: dict[int, np.ndarray] = {}
        for hour in hours:
            moment = run.time + timedelta(hours=hour)
            if moment not in index:
                raise DownloadError(f"{label} holds no frame valid at {moment:%Y-%m-%dT%H:%MZ} (+{hour} h of run {run.id})")
            plane = np.asarray(variable[index[moment]], dtype=np.float32)
            for fill in fills:
                plane = np.where(plane == fill, np.float32(np.nan), plane)
            if roll:
                plane = np.roll(plane, 1, axis=-1)
            planes[hour] = np.ascontiguousarray(plane, dtype=np.float32)
    return planes


# -- The series ------------------------------------------------------------


def series_name(run: GfsRun, variable_id: str) -> str:
    return f"{MODEL}.{run.id}.{variable_id}.nc"


def series_attributes(run: GfsRun, command: str) -> dict[str, str]:
    return {
        "Conventions": "CF-1.10",
        "title": "SILAM v6.1 global air-quality forecast",
        "institution": "Finnish Meteorological Institute",
        "source": "SILAM v6.1 global surface fields from the FMI AWS open data bucket fmi-opendata-silam-surface-netcdf, 0.2 degree regular grid",
        "license": "CC BY 4.0, Finnish Meteorological Institute",
        "history": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {command}",
        "forecast_reference_time": run.time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def fetch_run(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool,
    input_ids: tuple[str, ...] | None,
    fetch_text: Callable[[str], str],
    fetch_bytes: Callable[[str], bytes],
) -> list[Path]:
    """Fetch one run into ``raw_root/silam.<run>/``: one series per
    variable and a ``fetch.json``. A series already on disk is kept unless
    ``force``. A day file the folder lacks makes the run incomplete, never a
    gap in the axis."""
    if input_ids is not None and any(variable_id not in spec.input_variable_ids for variable_id in input_ids):
        raise DownloadError(f"{spec.manifest_model} publishes {list(spec.input_variable_ids)}, not {list(input_ids)}")
    variable_ids = tuple(v for v in spec.input_variable_ids if input_ids is None or v in input_ids)
    if not variable_ids:
        raise DownloadError(f"a {spec.manifest_model} fetch needs at least one variable")
    destination = raw_root / f"{spec.id}.{run.id}"
    destination.mkdir(parents=True, exist_ok=True)
    paths = {variable_id: destination / series_name(run, variable_id) for variable_id in variable_ids}
    wanted = tuple(variable_id for variable_id in variable_ids if force or not paths[variable_id].is_file())
    for variable_id in variable_ids:
        if variable_id not in wanted:
            LOG.info("reusing %s", paths[variable_id])
    axis = spec.forecast_hours(hours)
    position = {hour: index for index, hour in enumerate(axis)}
    if wanted:
        missing = missing_files(spec, run, hours, list_run(run, fetch=fetch_text), wanted)
        if missing:
            raise DownloadError(
                f"{spec.manifest_model} run {run.id} is incomplete: the bucket holds no "
                f"{', '.join(missing[:6])}{' ...' if len(missing) > 6 else ''}"
            )
        frames = {variable_id: np.empty((len(axis), GRID_HEIGHT, GRID_WIDTH), dtype=np.float32) for variable_id in wanted}
        jobs = [(variable_id, day) for variable_id in wanted for day in days_for(axis)]

        def one(job: tuple[str, int]) -> None:
            variable_id, day = job
            key = file_key(run, variable_id, day)
            day_hours = [hour for hour in axis if day_of(hour) == day]
            planes = read_day(fetch_bytes(file_url(key)), run, variable_id, day_hours, label=f"SILAM {key}")
            for hour, plane in planes.items():
                frames[variable_id][position[hour]] = plane

        with ThreadPoolExecutor(max_workers=max(1, spec.fetch_concurrency)) as executor:
            list(executor.map(one, jobs))
        latitudes, longitudes = grid_coordinates()
        attributes = series_attributes(run, f"xue fetch --model {spec.id} --run {run.id} --hours {hours}")
        for variable_id in wanted:
            write_series(
                paths[variable_id],
                variable_id,
                run.time,
                axis,
                latitudes,
                longitudes,
                frames[variable_id],
                attributes,
                long_name=LONG_NAMES[variable_id],
                units=SERIES_UNIT,
            )
            LOG.info("wrote %s (%d frames)", paths[variable_id], len(axis))
    record = {
        "model": spec.id,
        "run": run.id,
        "hours": hours,
        "frames": len(axis),
        "variables": [
            {
                "variable": variable_id,
                "files": [file_key(run, variable_id, day) for day in days_for(axis)],
                "series": paths[variable_id].name,
                "fetched": variable_id in wanted,
            }
            for variable_id in variable_ids
        ],
    }
    (destination / FETCH_FILENAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    LOG.info("%s run %s: %d series in %s", spec.manifest_model, run.id, len(paths), destination)
    return list(paths.values())

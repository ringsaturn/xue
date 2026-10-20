"""Reading an EFAS GRIB and its static maps, and writing the series files."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import XueError
from ..sources import source_spec
from ..variables import EFAS_SERIES_FILL, RIVER_VARIABLE_IDS, variable_spec

LOG = logging.getLogger(__name__)

INSTALL_HINT = "install the efas dependency group: uv sync --group efas --inexact"

#: The variables one run yields, in file order.
VARIABLE_IDS = RIVER_VARIABLE_IDS

#: The EFAS v5 grid: 4530 x 2970 cells of 1/60°, the outer edges at
#: 25.25°W, 50.25°E, 22.75°N and 72.25°N, so every coordinate is a cell
#: centre half a step in. GRIB rows run north to south.
WIDTH, HEIGHT = 4530, 2970
WEST_EDGE, NORTH_EDGE = -25.25, 72.25
STEP = 1.0 / 60.0
#: Not a river: the GRIB's bitmap leaves three cells in five out. eccodes
#: hands a bitmapped point back as the message's ``missingValue`` (9999 in
#: the file, a discharge a large river reaches), so the reader sets it to
#: the series' fill before taking the values, and a real 9999 m³/s stays a
#: value.
SERIES_FILL = EFAS_SERIES_FILL
#: The step of ``dis06``: the mean over the six hours ending at the frame.
STEP_HOURS = 6

#: EFAS v5.1's flood thresholds, by return period in years as the file
#: names spell them (``flood_threshold_efas_v5.1_rl_<period>.nc``, variable
#: ``rl_<period>``): class 1 at or above the first, ... class 4 at or above
#: the last.
RETURN_PERIODS = ("1.5", "2.0", "5.0", "20.0")
THRESHOLD_FILE = "flood_threshold_efas_v5.1_rl_{period}.nc"
#: EFAS evaluates a threshold only where the upstream area is over 50 km²
#: and the 1.5-year return level over 1 m³/s; elsewhere the class is the
#: series' fill, which the encoders read as class 0, "not evaluated".
UPSTREAM_MINIMUM_KM2 = 50.0
LOWEST_THRESHOLD_MINIMUM = 1.0
UPSTREAM_VARIABLE = "ec_upArea"

#: The ``producer.version`` the two derived series are stamped with
#: (``producer.id`` is the registry's, ``xue``). Bump it when the class
#: rule or the upstream conversion changes.
PRODUCER_VERSION = "1"

#: What each model's messages carry: the HRES forecast is a forecast
#: (GRIB2 code table 4.3 ``typeOfGeneratingProcess`` 2), the water balance
#: an analysis (0).
GENERATING_PROCESS = {"efas": 2, "efaswb": 0}
TITLES = {
    "efas": "EFAS v5 HRES river discharge forecast",
    "efaswb": "EFAS v5 water balance river discharge",
}
LONG_NAMES = {
    "dis06": "river discharge, mean over the six hours ending at the valid time",
    "disrp": "return-period class of the discharge against the EFAS v5.1 flood thresholds (1 below 1.5 y, 2 >= 1.5 y, 3 >= 2 y, 4 >= 5 y, 5 >= 20 y; the fill where EFAS evaluates none)",
    "upstream": "upstream area",
}


class EfasError(XueError):
    """An EFAS input this tool cannot turn into a series."""


def _eccodes() -> Any:
    try:
        import eccodes  # noqa: PLC0415 - the efas group, optional everywhere else
    except (ImportError, RuntimeError) as exc:
        raise EfasError(f"reading the EFAS GRIB needs the eccodes bindings ({exc}); {INSTALL_HINT}") from exc
    return eccodes


def _netcdf4() -> Any:
    try:
        import netCDF4  # noqa: PLC0415 - the efas group, optional everywhere else
    except ImportError as exc:
        raise EfasError(f"reading and writing EFAS NetCDF needs netCDF4 ({exc}); {INSTALL_HINT}") from exc
    return netCDF4


@dataclass(frozen=True)
class Window:
    """The cells written: a row and a column range of the EFAS grid (rows
    counted from the north), the whole grid by default."""

    row_start: int = 0
    row_stop: int = HEIGHT
    column_start: int = 0
    column_stop: int = WIDTH

    @classmethod
    def from_bbox(cls, bbox: tuple[float, float, float, float] | None) -> Window:
        """The cells whose centres lie inside ``(west, south, east, north)``."""
        if bbox is None:
            return cls()
        west, south, east, north = bbox
        if not (west < east and south < north):
            raise EfasError(f"bbox {bbox} must be west, south, east, north with west < east and south < north")
        columns = np.flatnonzero((longitudes_of(0, WIDTH) >= west) & (longitudes_of(0, WIDTH) <= east))
        rows = np.flatnonzero((latitudes_of(0, HEIGHT) >= south) & (latitudes_of(0, HEIGHT) <= north))
        if not columns.size or not rows.size:
            raise EfasError(f"bbox {bbox} holds no cell of the EFAS grid")
        return cls(int(rows[0]), int(rows[-1]) + 1, int(columns[0]), int(columns[-1]) + 1)

    @property
    def width(self) -> int:
        return self.column_stop - self.column_start

    @property
    def height(self) -> int:
        return self.row_stop - self.row_start

    def take(self, plane: np.ndarray) -> np.ndarray:
        """The window of one north-to-south plane, flipped to the series'
        ascending latitudes."""
        if plane.shape != (HEIGHT, WIDTH):
            raise EfasError(f"a plane of {plane.shape[1]}x{plane.shape[0]} cells is not on the EFAS grid")
        return plane[self.row_start : self.row_stop, self.column_start : self.column_stop][::-1]

    @property
    def latitudes(self) -> np.ndarray:
        """Cell centres, ascending (south to north)."""
        return latitudes_of(self.row_start, self.row_stop)[::-1]

    @property
    def longitudes(self) -> np.ndarray:
        return longitudes_of(self.column_start, self.column_stop)


def latitudes_of(start: int, stop: int) -> np.ndarray:
    """The centres of rows ``start``..``stop`` counted from the north."""
    return NORTH_EDGE - (np.arange(start, stop, dtype=np.float64) + 0.5) * STEP


def longitudes_of(start: int, stop: int) -> np.ndarray:
    return WEST_EDGE + (np.arange(start, stop, dtype=np.float64) + 0.5) * STEP


def return_period_class(
    discharge: np.ndarray, thresholds: tuple[np.ndarray, ...], upstream_km2: np.ndarray
) -> np.ndarray:
    """The return-period class of each cell: 1 below the first threshold,
    then 2, 3, 4, 5 at or above each of :data:`RETURN_PERIODS`'. NaN where
    EFAS evaluates no threshold — an upstream area of 50 km² or less, a
    1.5-year level of 1 m³/s or less, or no river (no discharge, threshold
    or area) — so the class says nothing there."""
    if len(thresholds) != len(RETURN_PERIODS):
        raise EfasError(f"the class needs {len(RETURN_PERIODS)} thresholds, got {len(thresholds)}")
    discharge = np.asarray(discharge, dtype=np.float64)
    evaluated = np.isfinite(discharge) & np.isfinite(upstream_km2) & (upstream_km2 > UPSTREAM_MINIMUM_KM2)
    for threshold in thresholds:
        evaluated &= np.isfinite(threshold)
    evaluated &= np.where(np.isfinite(thresholds[0]), thresholds[0], 0.0) > LOWEST_THRESHOLD_MINIMUM
    classes = np.ones(discharge.shape, dtype=np.float32)
    with np.errstate(invalid="ignore"):
        for threshold in thresholds:
            classes += (discharge >= threshold).astype(np.float32)
    classes[~evaluated] = np.nan
    return classes


def _check_coordinates(path: Path, latitudes: np.ndarray, longitudes: np.ndarray) -> None:
    """Hold a static map to the EFAS grid, north row first. The threshold
    maps carry float32 coordinates, so the tolerance is a fraction of a
    cell, not a bit."""
    if latitudes.shape != (HEIGHT,) or longitudes.shape != (WIDTH,):
        raise EfasError(f"{path} is {longitudes.size}x{latitudes.size} cells, not the EFAS grid's {WIDTH}x{HEIGHT}")
    tolerance = STEP / 100
    if np.abs(latitudes - latitudes_of(0, HEIGHT)).max() > tolerance:
        raise EfasError(f"{path} latitudes are not the EFAS grid's, north row first")
    if np.abs(longitudes - longitudes_of(0, WIDTH)).max() > tolerance:
        raise EfasError(f"{path} longitudes are not the EFAS grid's")


def read_static(path: Path, variable: str) -> np.ndarray:
    """One static map as a north-to-south float64 plane, its fill as NaN."""
    netcdf4 = _netcdf4()
    if not path.is_file():
        raise EfasError(f"static map not found: {path}")
    with netcdf4.Dataset(path) as dataset:
        if variable not in dataset.variables:
            raise EfasError(f"{path} holds no {variable}")
        names = ("lat", "latitude"), ("lon", "longitude")
        latitude, longitude = (next((dataset[name] for name in pair if name in dataset.variables), None) for pair in names)
        if latitude is None or longitude is None:
            raise EfasError(f"{path} carries no lat/lon coordinates")
        _check_coordinates(path, np.asarray(latitude[:], dtype=np.float64), np.asarray(longitude[:], dtype=np.float64))
        values = dataset[variable][:]
    plane = np.ma.filled(np.ma.asarray(values, dtype=np.float64), np.nan)
    return np.squeeze(plane)


def read_thresholds(directory: Path) -> tuple[np.ndarray, ...]:
    """The four return levels, m³/s, north to south."""
    return tuple(
        read_static(directory / THRESHOLD_FILE.format(period=period), f"rl_{period}") for period in RETURN_PERIODS
    )


def read_upstream(path: Path) -> np.ndarray:
    """The upstream area in km² (the map is in m² and declares no unit)."""
    return read_static(path, UPSTREAM_VARIABLE) / 1.0e6


@dataclass(frozen=True)
class Message:
    """One ``dis06`` field: its reference time, the end of its six-hour
    interval when the message says (None when its dates are not real), and
    the north-to-south plane in m³/s with NaN off the rivers."""

    index: int
    reference: datetime | None
    valid: datetime | None
    generating_process: int
    plane: np.ndarray


def _message_time(get: Any, date_key: str, time_key: str) -> datetime | None:
    try:
        date, time = int(get(date_key)), int(get(time_key))
    except Exception:  # noqa: BLE001 - eccodes raises its own zoo for an invalid date
        return None
    if date < 10000101:
        return None
    try:
        return datetime.strptime(f"{date:08d}{time:04d}", "%Y%m%d%H%M").replace(tzinfo=UTC)
    except ValueError:
        return None


def read_messages(path: Path, *, values: bool = True) -> Iterator[Message]:
    """Every message of one EFAS GRIB, in file order, held to ``dis06`` on
    the EFAS grid; with ``values`` False the planes are left empty."""
    eccodes = _eccodes()
    if not path.is_file():
        raise EfasError(f"GRIB not found: {path}")
    with path.open("rb") as handle:
        index = 0
        while (message := eccodes.codes_grib_new_from_file(handle)) is not None:
            try:
                get = lambda key: eccodes.codes_get(message, key)  # noqa: E731
                where = f"{path} message {index + 1}"
                identity = (get("discipline"), get("parameterCategory"), get("parameterNumber"))
                if identity != (1, 0, 7):
                    raise EfasError(f"{where} is {'/'.join(map(str, identity))}, not discharge (1/0/7)")
                if get("typeOfStatisticalProcessing") != 0 or get("lengthOfTimeRange") != STEP_HOURS or get("indicatorOfUnitForTimeRange") != 1:
                    raise EfasError(f"{where} is not a {STEP_HOURS} h mean")
                if (get("Ni"), get("Nj")) != (WIDTH, HEIGHT) or get("gridType") != "regular_ll":
                    raise EfasError(f"{where} is not on the {WIDTH}x{HEIGHT} EFAS grid")
                if get("iScansNegatively") != 0 or get("jScansPositively") != 0:
                    raise EfasError(f"{where} does not scan west to east, north to south")
                first = (get("longitudeOfFirstGridPointInDegrees"), get("latitudeOfFirstGridPointInDegrees"))
                if abs(first[0] - longitudes_of(0, 1)[0]) > 1e-3 or abs(first[1] - latitudes_of(0, 1)[0]) > 1e-3:
                    raise EfasError(f"{where} starts at {first}, not the EFAS grid's first cell")
                reference = _message_time(get, "dataDate", "dataTime")
                valid = _message_time(get, "validityDate", "validityTime")
                process = int(get("typeOfGeneratingProcess"))
                plane = np.empty(0)
                if values:
                    if get("bitmapPresent"):
                        eccodes.codes_set(message, "missingValue", SERIES_FILL)
                    plane = np.asarray(eccodes.codes_get_values(message), dtype=np.float64).reshape(HEIGHT, WIDTH)
                    plane[plane == SERIES_FILL] = np.nan
            finally:
                eccodes.codes_release(message)
            yield Message(index, reference, valid, process, plane)
            index += 1


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
        unit = variable_spec(self.variable_id).output_unit
        return (
            f"{self.variable_id:9s} {self.frames} frames, {self.width}x{self.height} cells, "
            f"{self.minimum:.3f}..{self.maximum:.3f} {unit}"
        )


def _create_series(
    path: Path, variable_id: str, run: datetime, window: Window, attributes: dict[str, str]
) -> Any:
    """Open one variable's CF series for appending, in the shape
    ``xuebuild.wrf.series.write_series`` writes: ``time`` in hours from the
    run, ``step`` beside it, the run as ``forecast_reference_time``,
    ascending latitudes, float32 with the declared -9999 fill (``EFAS_SERIES_FILL``,
    each variable's ``fill_values``) rather than a NaN, the variable named by its
    Xue id and in the registry's own unit. A derived variable carries the
    producer stamp the converter requires."""
    netcdf4 = _netcdf4()
    epoch = run.strftime("%Y-%m-%d %H:%M:%S")
    dataset = netcdf4.Dataset(path, "w", format="NETCDF4")
    try:
        dataset.setncatts(attributes)
        dataset.createDimension("time", None)
        dataset.createDimension("latitude", window.height)
        dataset.createDimension("longitude", window.width)
        time = dataset.createVariable("time", "f8", ("time",))
        time.setncatts({"standard_name": "time", "long_name": "valid time", "units": f"hours since {epoch}", "calendar": "standard", "axis": "T"})
        step = dataset.createVariable("step", "f8", ("time",))
        step.setncatts({"standard_name": "forecast_period", "long_name": "time since forecast_reference_time", "units": "hours"})
        reference = dataset.createVariable("forecast_reference_time", "f8", ())
        reference.setncatts({"standard_name": "forecast_reference_time", "long_name": "initial time of forecast", "units": "seconds since 1970-01-01 00:00:00", "calendar": "standard"})
        reference[...] = run.timestamp()
        latitude = dataset.createVariable("latitude", "f8", ("latitude",))
        latitude.setncatts({"standard_name": "latitude", "long_name": "latitude", "units": "degrees_north", "axis": "Y"})
        latitude[:] = window.latitudes
        longitude = dataset.createVariable("longitude", "f8", ("longitude",))
        longitude.setncatts({"standard_name": "longitude", "long_name": "longitude", "units": "degrees_east", "axis": "X"})
        longitude[:] = window.longitudes
        data = dataset.createVariable(
            variable_id,
            "f4",
            ("time", "latitude", "longitude"),
            zlib=True,
            complevel=1,
            chunksizes=(1, window.height, window.width),
            fill_value=np.float32(SERIES_FILL),
        )
        spec = variable_spec(variable_id)
        variable_attributes = {"long_name": LONG_NAMES[variable_id], "units": spec.output_unit, "coordinates": "forecast_reference_time step"}
        if spec.producer_id is not None:
            variable_attributes |= {"producer_id": spec.producer_id, "producer_version": PRODUCER_VERSION}
        data.setncatts(variable_attributes)
    except BaseException:
        dataset.close()
        raise
    return dataset


def _frame_times(messages: list[Message], model: str, start: datetime | None) -> tuple[datetime, list[datetime]]:
    """The run and each message's valid time. A forecast's run is the
    messages' own reference time (``--start`` may only repeat it) and a
    message is valid at the end of its interval. The water balance has no
    run: ``--start`` names it, and a message is valid where its own dates
    say or, where they are not real dates, six hours after the previous
    one from ``start`` on — the order the store delivers them in."""
    if model == "efas":
        references = {message.reference for message in messages}
        if len(references) != 1 or None in references:
            raise EfasError(f"the forecast's messages name {len(references)} reference times; a series is one cycle")
        run = references.pop()
        assert run is not None
        if start is not None and start != run:
            raise EfasError(f"--start {start.isoformat()} is not the forecast's cycle {run.isoformat()}")
        valid = []
        for message in messages:
            if message.valid is None:
                raise EfasError(f"forecast message {message.index + 1} carries no valid time")
            valid.append(message.valid)
        return run, valid
    if start is None:
        raise EfasError("the water balance has no cycle: name the run with --start")
    valid = [
        message.valid if message.valid is not None else start + timedelta(hours=STEP_HOURS * (message.index + 1))
        for message in messages
    ]
    return start, valid


def convert(
    grib: Path,
    thresholds: Path,
    upstream: Path,
    out: Path,
    *,
    model: str,
    start: datetime | None = None,
    hours: int | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    force: bool = False,
    command: str = "xue efas-series",
) -> list[SeriesSummary]:
    """Turn one EFAS GRIB into ``<out>/<model>.<YYYYMMDDHH>.<id>.nc``, one
    per :data:`VARIABLE_IDS`, with frames at the source's forecast hours
    from f006 through ``hours`` (the whole 120 h axis by default).
    ``thresholds`` is the directory holding the four
    ``flood_threshold_efas_v5.1_rl_<period>.nc``; ``upstream`` the static
    upstream area map. Messages outside the axis are skipped; a frame of
    the axis missing from the file is an error."""
    if model not in GENERATING_PROCESS:
        raise EfasError(f"model must be one of {', '.join(GENERATING_PROCESS)}, not {model}")
    spec = source_spec(model)
    hours = spec.horizon_hours if hours is None else hours
    try:
        wanted = spec.forecast_hours(hours)
    except XueError as exc:
        raise EfasError(str(exc)) from exc
    window = Window.from_bbox(bbox)

    # Pass one: identities and times only, so nothing is written for a file
    # that will be refused. The planes are read again in pass two.
    headers = list(read_messages(grib, values=False))
    if not headers:
        raise EfasError(f"{grib} holds no message")
    processes = {message.generating_process for message in headers}
    if processes != {GENERATING_PROCESS[model]}:
        raise EfasError(
            f"{grib} carries generating process {sorted(processes)}, not {model}'s {GENERATING_PROCESS[model]} "
            "(2 a forecast, 0 the water balance)"
        )
    run, valid = _frame_times(headers, model, start)
    leads = [(time - run) / timedelta(hours=1) for time in valid]
    by_lead: dict[int, int] = {}
    for message, lead in zip(headers, leads):
        if lead != int(lead) or (by_lead and lead <= max(by_lead)):
            raise EfasError(f"{grib} message {message.index + 1} is valid at +{lead} h, out of order")
        by_lead[int(lead)] = message.index
    missing = [hour for hour in wanted if hour not in by_lead]
    if missing:
        raise EfasError(f"{grib} lacks the frames at +{', +'.join(map(str, missing))} h of the {model} run {run:%Y%m%d%H}")
    keep = {by_lead[hour]: hour for hour in wanted}

    stem = f"{model}.{run:%Y%m%d%H}"
    out.mkdir(parents=True, exist_ok=True)
    paths = {variable_id: out / f"{stem}.{variable_id}.nc" for variable_id in VARIABLE_IDS}
    existing = [path for path in paths.values() if path.exists()]
    if existing and not force:
        raise EfasError(f"{out} already holds {len(existing)} series of {stem} (use --force to replace them)")

    upstream_km2 = read_upstream(upstream)
    levels = read_thresholds(thresholds)
    upstream_plane = window.take(upstream_km2).astype(np.float32)
    level_planes = tuple(window.take(level) for level in levels)
    LOG.info("%s: %d of %d messages, %dx%d cells from row %d, column %d", stem, len(keep), len(headers), window.width, window.height, window.row_start, window.column_start)

    attributes = {
        "Conventions": "CF-1.10",
        "title": TITLES[model],
        "institution": "European Centre for Medium-Range Weather Forecasts (ECMWF), Copernicus Emergency Management Service",
        "source": (
            f"{grib.name}: EFAS v5 dis06 (GRIB2 1/0/7, template 4.72, {STEP_HOURS} h mean) on the 1/60° EFAS grid; "
            f"return-period class against {', '.join(THRESHOLD_FILE.format(period=p) for p in RETURN_PERIODS)}; "
            f"upstream area from {upstream.name}"
        ),
        "references": "https://ewds.climate.copernicus.eu",
        "license": "CEMS-FLOODS; contains modified Copernicus Emergency Management Service information",
        "history": f"{datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ} {command}",
        "forecast_reference_time": run.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    datasets = {variable_id: _create_series(paths[variable_id], variable_id, run, window, attributes) for variable_id in VARIABLE_IDS}
    extremes = {variable_id: [np.inf, -np.inf] for variable_id in VARIABLE_IDS}
    written = 0
    try:
        for message in read_messages(grib):
            hour = keep.get(message.index)
            if hour is None:
                continue
            discharge = window.take(message.plane)
            planes = {
                "dis06": discharge.astype(np.float32),
                "disrp": return_period_class(discharge, level_planes, upstream_plane.astype(np.float64)),
                "upstream": upstream_plane,
            }
            for variable_id, plane in planes.items():
                dataset = datasets[variable_id]
                dataset["time"][written] = hour
                dataset["step"][written] = hour
                # NaN is the tool's own "no value"; the file says it with
                # its declared fill, which the registry's ``fill_values``
                # names, never with a NaN.
                dataset[variable_id][written] = np.where(np.isfinite(plane), plane, np.float32(SERIES_FILL)).astype(np.float32)
                if np.isfinite(plane).any():
                    low, high = extremes[variable_id]
                    extremes[variable_id] = [min(low, float(np.nanmin(plane))), max(high, float(np.nanmax(plane)))]
            written += 1
    finally:
        for dataset in datasets.values():
            dataset.close()
    if written != len(keep):
        raise EfasError(f"{grib} changed between its two reads ({written} of {len(keep)} frames)")
    return [
        SeriesSummary(variable_id, paths[variable_id], written, window.width, window.height, *extremes[variable_id])
        for variable_id in VARIABLE_IDS
    ]

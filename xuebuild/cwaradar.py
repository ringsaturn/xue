"""The Central Weather Administration's radar composite over Taiwan, fetched
into the series file both encoders read (``docs/contribution/sources.md``,
"cwa").

The CWA publishes its open data twice: behind an API key on the open data
platform, and anonymously on its own S3 bucket, ``cwaopendata`` in
``ap-northeast-1``, one object per dataset id. The composite reflectivity of
the island's seven radars is dataset ``O-A0059-001``, one JSON document
replaced every ten minutes: the newest mosaic only, no listing of past ones
and no object versions. A header names the mosaic's time (Taiwan time,
UTC+8), its grid — 921 x 881 points at 0.0125°, the first at 115.0°E
18.0°N, rows running west to east and then south to north — and the unit,
dBZ; the values follow as one comma-separated string of numbers in
scientific notation. ``-99`` is a point inside coverage with no echo,
``-999`` a point outside coverage or removed by quality control.

Both sentinels become NaN here: the composite's codebook paints "no echo"
and "no radar" alike (``cref`` in xuebuild/variables.py), and the CMA
mosaic's series says missing the same way.

The grid is on TWD67, the island's old geodetic datum, not WGS84: the
product's own description says so, and the station tables the same bucket
serves give every station in both datums. Over the 2,576 stations of
``O-A0001``, ``O-A0002`` and ``O-A0003`` the WGS84 position is 0.0081°
east and 0.0018° south of the TWD67 one (standard deviations 0.0005° and
0.0009°), some 820 m and 200 m, two thirds of a cell east. The fetch moves
the grid by that one offset rather than resampling it, so every value keeps
its own cell and only the coordinates change; over the mosaic's 11.5° the
datum shift varies by much less than a cell.

Only the newest mosaic is served, so a window is grown from a frame cache,
as the aurora feed grows its own (:mod:`xuebuild.aurora`): each round writes
the mosaic into ``data/raw/cwa-frames/cref/`` as one small NetCDF, the cache
is mirrored on the bucket (``make pull-r2-frames`` / ``push-r2-frames``), and
the window is whatever the cache holds between the run's hour and its end.
A ten-minute mosaic that no round fetched is lost: the axis lists the
frames it has.

This module is the pure half: parse the document, read and write the cache,
and assemble the window's frames into the one NetCDF series
:mod:`xuebuild.observation` reads. The network call is handed in, so the
module imports no HTTP.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from .errors import DownloadError
from .model import GfsRun
from .sources import SourceSpec

LOG = logging.getLogger(__name__)

MODEL = "cwa"

URL = os.environ.get(
    "XUE_CWA_RADAR_URL",
    "https://cwaopendata.s3.ap-northeast-1.amazonaws.com/Observation/O-A0059-001.json",
)
DATASET_ID = "O-A0059-001"

#: The published grid: 881 rows (south to north) by 921 columns (west to
#: east), the first point at 115.0°E 18.0°N on TWD67, 0.0125° apart. A
#: document on another grid is refused by the fetch (a crop is still
#: parsed, for the tests).
GRID_SHAPE: tuple[int, int] = (881, 921)
GRID_ORIGIN_TWD67: tuple[float, float] = (115.0, 18.0)
STEP = 0.0125

#: TWD67 to WGS84 over the island, in degrees east and north: the mean
#: offset between the two positions of every station the bucket lists.
DATUM_SHIFT: tuple[float, float] = (0.0081, -0.0018)

#: The no-echo and out-of-coverage sentinels.
SENTINELS: tuple[float, ...] = (-99.0, -999.0)

CADENCE_SECONDS = 600

FRAMES_DIRNAME = "cwa-frames"
VARIABLE_DIR = "cref"
FETCH_FILENAME = "fetch.json"
INSTALL_HINT = "install the cwa group (uv sync --group cwa)"

#: The cache and the series store dBZ as int16 tenths, the CMA mosaic's
#: packing; 32767 is missing.
SCALE = 0.1
MISSING = np.int16(32767)


@dataclass(frozen=True)
class Mosaic:
    """One composite: its time, and its values on its own grid, rows
    ascending in latitude, NaN where there is no echo or no radar."""

    valid_time: datetime
    values: np.ndarray
    first_longitude: float
    """TWD67, as published."""
    first_latitude: float
    step: float

    @property
    def longitudes(self) -> np.ndarray:
        """WGS84 point longitudes, west to east."""
        return wgs84_axis(self.first_longitude, DATUM_SHIFT[0], self.step, self.values.shape[1])

    @property
    def latitudes(self) -> np.ndarray:
        """WGS84 point latitudes, south to north."""
        return wgs84_axis(self.first_latitude, DATUM_SHIFT[1], self.step, self.values.shape[0])


def wgs84_axis(first: float, shift: float, step: float, count: int) -> np.ndarray:
    """One axis of point coordinates moved onto WGS84. The values are
    whole ten-thousandths of a degree divided out at the end, so the axis
    is the same doubles however it is computed."""
    start, stride = (first + shift) * 10000, step * 10000
    if not (np.isclose(start, round(start)) and np.isclose(stride, round(stride))):
        raise DownloadError(f"the CWA radar grid at {first} by {step} is not on whole ten-thousandths")
    return (round(start) + np.arange(count, dtype=np.int64) * round(stride)) / 10000.0


def parse_time(text: str) -> datetime:
    """The header's ``DateTime`` (ISO 8601 with its offset) in UTC, which
    must sit on a ten-minute mark."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise DownloadError(f"the CWA radar time {text!r} is not ISO 8601") from exc
    if moment.tzinfo is None:
        raise DownloadError(f"the CWA radar time {text!r} carries no offset")
    moment = moment.astimezone(UTC)
    if int(moment.timestamp()) % CADENCE_SECONDS or moment.microsecond:
        raise DownloadError(f"the CWA radar time {text!r} is off the ten-minute marks")
    return moment


def _number(parameters: dict[str, object], key: str) -> float:
    try:
        return float(str(parameters[key]))
    except (KeyError, ValueError) as exc:
        raise DownloadError(f"the CWA radar header lacks a numeric {key}") from exc


def parse(payload: bytes | str) -> Mosaic:
    """The dataset's JSON document as a :class:`Mosaic`. Every point is
    required: a short or long value string is a damaged download, refused
    rather than padded."""
    try:
        document = json.loads(payload)
        dataset = document["cwaopendata"]["dataset"]
        parameters = dataset["datasetInfo"]["parameterSet"]
        content = dataset["contents"]["content"]
    except (TypeError, ValueError, KeyError) as exc:
        raise DownloadError(f"the CWA radar document is not the {DATASET_ID} shape: {exc}") from exc
    if str(parameters.get("Reflectivity", "")).strip() != "dBZ":
        raise DownloadError(f"the CWA radar document is in {parameters.get('Reflectivity')!r}, not dBZ")
    width = int(_number(parameters, "GridDimensionX"))
    height = int(_number(parameters, "GridDimensionY"))
    step = _number(parameters, "GridResolution")
    first_longitude = _number(parameters, "StartPointLongitude")
    first_latitude = _number(parameters, "StartPointLatitude")
    valid_time = parse_time(str(parameters.get("DateTime", "")))
    try:
        values = np.array(str(content).split(","), dtype=np.float64)
    except ValueError as exc:
        raise DownloadError(f"the CWA radar values are not numbers: {exc}") from exc
    if values.size != width * height:
        raise DownloadError(f"the CWA radar document holds {values.size} values, not {width} x {height}")
    values = values.reshape(height, width)
    values[np.isin(values, SENTINELS)] = np.nan
    return Mosaic(valid_time, values, first_longitude, first_latitude, step)


def check_grid(mosaic: Mosaic) -> None:
    """Refuse a mosaic off the published grid: the source's grid is fixed
    in the registry, and a change at the agency is a change here."""
    if (
        mosaic.values.shape != GRID_SHAPE
        or (mosaic.first_longitude, mosaic.first_latitude) != GRID_ORIGIN_TWD67
        or mosaic.step != STEP
    ):
        raise DownloadError(
            f"the CWA radar grid moved: {mosaic.values.shape[1]} x {mosaic.values.shape[0]} at "
            f"{mosaic.step}° from {mosaic.first_longitude}E {mosaic.first_latitude}N, not "
            f"{GRID_SHAPE[1]} x {GRID_SHAPE[0]} at {STEP}° from {GRID_ORIGIN_TWD67[0]}E {GRID_ORIGIN_TWD67[1]}N"
        )


# -- the frame cache ----------------------------------------------------------


def frame_name(valid_time: datetime) -> str:
    """The cache file of one mosaic, in the ``<name>_<YYYYMMDDHHMMSS>.nc``
    shape the Makefile's frame globs key on."""
    return f"cwa_{valid_time:%Y%m%d%H%M%S}.nc"


def frame_time(path: Path) -> datetime:
    stem = path.stem
    if not stem.startswith("cwa_"):
        raise ValueError(f"not a CWA radar frame: {path.name}")
    try:
        return datetime.strptime(stem[len("cwa_") :], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"not a CWA radar frame: {path.name}") from exc


def cached_frames(frames_dir: Path) -> list[tuple[datetime, Path]]:
    """Every frame in the cache, oldest first."""
    variable_dir = frames_dir / VARIABLE_DIR
    if not variable_dir.is_dir():
        return []
    frames: list[tuple[datetime, Path]] = []
    for path in variable_dir.glob("cwa_*.nc"):
        try:
            frames.append((frame_time(path), path))
        except ValueError:
            continue
    return sorted(frames)


def window_frames(frames_dir: Path, start: datetime, hours: int) -> list[tuple[datetime, Path]]:
    """The cached frames from ``start`` through ``hours`` past it,
    inclusive, oldest first."""
    end = start + timedelta(hours=hours)
    return [(slot, path) for slot, path in cached_frames(frames_dir) if start <= slot <= end]


def window_start(frames_dir: Path, start: datetime, newest: datetime) -> datetime:
    """Where a live window starts: the plain rule's ``start``, unless the
    cache does not reach back that far, when it is the hour of the
    earliest cached frame (or of the newest mosaic, for an empty cache), so
    the run id is the hour of the window's first frame."""
    frames = cached_frames(frames_dir)
    if not frames:
        return newest.replace(minute=0, second=0, microsecond=0)
    return max(start, frames[0][0].replace(minute=0, second=0, microsecond=0))


def _xarray() -> object:
    try:
        import xarray as xr  # noqa: PLC0415 - the cwa group, optional everywhere else
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DownloadError(f"the CWA radar cache needs xarray and netCDF4 ({exc}); {INSTALL_HINT}") from exc
    return xr


def _dataset(times: list[datetime], frames: np.ndarray, latitudes: np.ndarray, longitudes: np.ndarray) -> object:
    xr = _xarray()
    stamps = np.array([np.datetime64(moment.replace(tzinfo=None), "ns") for moment in times])
    return xr.Dataset(  # type: ignore[attr-defined]
        {
            "cref": (
                ("time", "lat", "lon"),
                frames.astype(np.float32),
                {
                    "long_name": "composite radar reflectivity",
                    "standard_name": "equivalent_reflectivity_factor",
                    "units": "dBZ",
                    "grid_mapping": "crs",
                },
            ),
            "crs": (
                (),
                np.int32(0),
                {
                    "grid_mapping_name": "latitude_longitude",
                    "comment": "Regular 0.0125-degree grid, moved from TWD67 onto WGS84 by one offset.",
                },
            ),
        },
        coords={
            "time": ("time", stamps, {"long_name": "time", "axis": "T"}),
            "lat": ("lat", latitudes, {"long_name": "latitude", "standard_name": "latitude", "units": "degrees_north"}),
            "lon": ("lon", longitudes, {"long_name": "longitude", "standard_name": "longitude", "units": "degrees_east"}),
        },
        attrs={
            "Conventions": "CF-1.8",
            "title": "CWA radar composite reflectivity over Taiwan",
            "institution": "Central Weather Administration",
            "source": f"{DATASET_ID} on the cwaopendata bucket; no echo and no coverage as missing",
            "license": "Open Government Data License, version 1.0 (Taiwan); Central Weather Administration",
        },
    )


def _write(dataset: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial.nc")
    dataset.to_netcdf(  # type: ignore[attr-defined]
        temporary,
        encoding={
            "cref": {"dtype": "int16", "scale_factor": SCALE, "_FillValue": MISSING, "zlib": True, "complevel": 4},
            "time": {"units": "seconds since 1970-01-01T00:00:00Z"},
        },
    )
    temporary.replace(path)


def write_frame(frames_dir: Path, mosaic: Mosaic) -> Path:
    """Write one mosaic into the cache, atomically, and return its path. A
    re-fetch of the same mosaic overwrites it."""
    path = frames_dir / VARIABLE_DIR / frame_name(mosaic.valid_time)
    _write(_dataset([mosaic.valid_time], mosaic.values[None], mosaic.latitudes, mosaic.longitudes), path)
    return path


def read_frame(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """A cached frame's values and its latitude and longitude axes."""
    xr = _xarray()
    with xr.open_dataset(path) as dataset:  # type: ignore[attr-defined]
        values = np.asarray(dataset["cref"].values, dtype=np.float64)
        latitudes = np.asarray(dataset["lat"].values, dtype=np.float64)
        longitudes = np.asarray(dataset["lon"].values, dtype=np.float64)
    if values.ndim == 3 and values.shape[0] == 1:
        values = values[0]
    if values.shape != (latitudes.size, longitudes.size):
        raise DownloadError(f"cached CWA radar frame {path.name} is {values.shape}, not its axes")
    return values, latitudes, longitudes


def write_window(frames_dir: Path, start: datetime, hours: int, output: Path) -> list[datetime]:
    """Assemble the window's cached frames into the series the observation
    ingest reads, ``cref(time, lat, lon)`` in dBZ, and return the slots it
    holds, oldest first. Every frame must be on one grid; a window with no
    cached frame is refused."""
    frames = window_frames(frames_dir, start, hours)
    if not frames:
        raise DownloadError(f"the CWA radar cache holds no frame for {start.isoformat()} through +{hours} h")
    planes: list[np.ndarray] = []
    axes: tuple[np.ndarray, np.ndarray] | None = None
    for _, path in frames:
        values, latitudes, longitudes = read_frame(path)
        if axes is None:
            axes = (latitudes, longitudes)
        elif not (np.array_equal(axes[0], latitudes) and np.array_equal(axes[1], longitudes)):
            raise DownloadError(f"cached CWA radar frame {path.name} is on another grid than the window's first")
        planes.append(values)
    assert axes is not None
    _write(_dataset([slot for slot, _ in frames], np.stack(planes), axes[0], axes[1]), output)
    return [slot for slot, _ in frames]


# -- one round ------------------------------------------------------------------


#: The header, ``DateTime`` included, sits in the document's first kilobyte
#: or so; a round asks for this much of it to learn the newest slot.
HEAD_BYTES = 4096
_DATETIME = re.compile(r'"DateTime"\s*:\s*"([^"]+)"')


def parse_head(text: str) -> datetime:
    """The mosaic's time off the start of the document."""
    match = _DATETIME.search(text)
    if not match:
        raise DownloadError(f"the first {len(text)} characters of the CWA radar document name no DateTime")
    return parse_time(match.group(1))


def latest_slot(*, fetch_head: Callable[[int], bytes]) -> datetime:
    """The newest mosaic's time, the end of the live window, off a range
    read of the document's head: the whole document is some 9 MB."""
    return parse_head(fetch_head(HEAD_BYTES).decode("utf-8", errors="replace"))


def series_name(run: GfsRun) -> str:
    return f"{MODEL}.{run.id}.nc"


def fetch_run(
    spec: SourceSpec,
    run: GfsRun,
    hours: int,
    raw_root: Path,
    *,
    force: bool,
    input_ids: tuple[str, ...] | None,
    fetch_bytes: Callable[[str], bytes],
) -> list[Path]:
    """One window: write the newest mosaic into the frame cache, then
    assemble the window's cached frames into ``raw_root/cwa.<run>/``'s
    series, with a ``fetch.json`` in the shape the other windows leave (one
    entry per frame with its slot). ``force`` never clears the cache: past
    mosaics cannot be fetched again."""
    del force
    if input_ids is not None and any(variable_id not in spec.input_variable_ids for variable_id in input_ids):
        raise DownloadError(f"{spec.manifest_model} publishes {list(spec.input_variable_ids)}, not {list(input_ids)}")
    frames_dir = raw_root / FRAMES_DIRNAME
    mosaic = parse(fetch_bytes(URL))
    check_grid(mosaic)
    path = write_frame(frames_dir, mosaic)
    LOG.info("%s newest mosaic %s written to %s", spec.manifest_model, f"{mosaic.valid_time:%Y-%m-%dT%H:%MZ}", path)
    destination = raw_root / f"{spec.id}.{run.id}"
    output = destination / series_name(run)
    slots = write_window(frames_dir, run.time, hours, output)
    record = {
        "model": spec.id,
        "run": run.id,
        "hours": hours,
        "cadenceSeconds": spec.cadence_seconds,
        "grid": {"width": GRID_SHAPE[1], "height": GRID_SHAPE[0]},
        "series": output.name,
        "frames": [{"path": output.name, "slot": slot.strftime("%Y-%m-%dT%H:%M:%SZ")} for slot in slots],
    }
    (destination / FETCH_FILENAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    LOG.info("%s run %s: %d frames in %s", spec.manifest_model, run.id, len(slots), output)
    return [output]

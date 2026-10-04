"""NOAA SWPC OVATION aurora probability: the model's JSON grid to a NetCDF
series.

The Space Weather Prediction Center's OVATION model publishes the chance, in
percent, that aurora is visible overhead, as one global grid every five
minutes at ``json/ovation_aurora_latest.json`` (the product page is
<https://www.swpc.noaa.gov/products/aurora-30-minute-forecast>). The grid is
regular 1°: 360 longitudes ``0…359`` and 181 latitudes ``−90…90``, the
``MultiPoint`` coordinates a flat ``[lon, lat, percent]`` list — exactly the
global lat/lon convention every other source here uses, only coarser.

The live service keeps **only the newest grid**: there is no list of past
grids, and the 24-hour animation it does keep is JPEG frames, not values
(plans/027-aurora.md §2). So a rolling window cannot be fetched in one go
the way the JMA nowcast's three-hour listing is. Instead the fetch writes
each round's grid into a frame cache — one small NetCDF per valid time, the
way the satellite feeds cache a warped slot and the JMA feed caches a
decoded one — and the window is whatever the cache holds between the run's
hour and its end. The cache is mirrored on the bucket by the same
``make pull-r2-frames`` / ``push-r2-frames`` every cached feed uses, so a
window fills over hours and survives a fresh runner.

This module is the pure half: parse the JSON, snap a valid time to the
five-minute mark, read and write the cache, and assemble the window's
frames into the one NetCDF series :mod:`xuebuild.observation` reads. The
network call and the run orchestration live in :mod:`xuebuild.fetch`, so
this module imports no HTTP and no frontend.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from .errors import DownloadError

UTC = timezone.utc

#: The OVATION live grid: one grid, nowcast valid at its ``Forecast Time``,
#: refreshed every five minutes.
OVATION_URL = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
#: The published grid: 181 latitudes (90°S…90°N) by 360 longitudes (0…359°E).
GRID_SHAPE: tuple[int, int] = (181, 360)
#: The frame cache under ``data/raw/``; the paired bucket prefix is
#: ``<prefix>/aurora-frames/`` (the Makefile's ``$(MODEL)-frames``).
FRAMES_DIRNAME = "aurora-frames"
#: The cache's variable subdirectory, the shape every cached feed uses
#: (``<model>-frames/<variable>/``): aurora publishes one variable, but the
#: Makefile's pull and prune globs are written for the two-level layout.
VARIABLE_DIR = "aurora"
#: What a missing optional NetCDF dependency means to the operator.
INSTALL_HINT = "install the aurora group (uv sync --group aurora)"

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class OvationFrame:
    """One OVATION grid: the valid time it is stamped with and the
    probability plane, ``(lat, lon)``, ascending latitude and longitude,
    0–100 percent."""

    valid_time: datetime
    values: np.ndarray


def snap_slot(moment: datetime) -> datetime:
    """A valid time snapped **down** to the five-minute mark, as the
    container's axis requires: ``04:41`` is the frame at ``04:40``. Naive
    input is read as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    seconds = (moment.minute % 5) * 60 + moment.second
    return moment - timedelta(seconds=seconds, microseconds=moment.microsecond)


def parse_ovation(payload: bytes | str) -> OvationFrame:
    """The model's JSON grid as an :class:`OvationFrame`.

    The payload is the ``MultiPoint`` document the service serves: a
    ``Forecast Time`` header and 360 × 181 ``[lon, lat, percent]`` triples.
    Every cell is required — a partial grid is a truncated download, not a
    field with gaps, and is refused rather than silently filled."""
    try:
        document = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise DownloadError(f"OVATION grid is not JSON: {exc}") from exc
    try:
        stamp = datetime.strptime(str(document["Forecast Time"]), _TIME_FORMAT).replace(tzinfo=UTC)
        coordinates = document["coordinates"]
    except (KeyError, TypeError, ValueError) as exc:
        raise DownloadError(f"OVATION grid lacks its forecast time or coordinates: {exc}") from exc
    height, width = GRID_SHAPE
    values = np.full(GRID_SHAPE, np.nan, dtype=np.float64)
    for entry in coordinates:
        try:
            lon, lat, probability = entry
        except (TypeError, ValueError) as exc:
            raise DownloadError(f"OVATION coordinate is not a [lon, lat, value] triple: {entry!r}") from exc
        column, row = int(round(float(lon))), int(round(float(lat))) + 90
        if not (0 <= column < width and 0 <= row < height):
            raise DownloadError(f"OVATION coordinate is off the {height} x {width} grid: {entry!r}")
        values[row, column] = float(probability)
    if not np.isfinite(values).all():
        missing = int((~np.isfinite(values)).sum())
        raise DownloadError(f"OVATION grid is missing {missing} of {height * width} cells")
    return OvationFrame(valid_time=snap_slot(stamp), values=values)


def frame_name(valid_time: datetime) -> str:
    """The cache file for one valid time; the slot is already snapped. The
    Makefile's ``pull-r2-frames`` / ``prune-r2-frames`` globs key on
    ``<name>_<YYYYMMDDHHMMSS>.<ext>``, so the name is that shape."""
    return f"aurora_{valid_time:%Y%m%d%H%M%S}.nc"


def frame_time(path: Path) -> datetime:
    """The valid time a cache file is named for:
    ``aurora_20261003044000.nc``."""
    stem = path.stem
    if not stem.startswith("aurora_"):
        raise ValueError(f"not an aurora frame: {path.name}")
    stamp = stem[len("aurora_") :]
    try:
        return datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError(f"not an aurora frame: {path.name}") from exc


def cached_frames(frames_dir: Path) -> list[tuple[datetime, Path]]:
    """Every frame in the cache, oldest first."""
    variable_dir = frames_dir / VARIABLE_DIR
    if not variable_dir.is_dir():
        return []
    frames: list[tuple[datetime, Path]] = []
    for path in variable_dir.glob("aurora_*.nc"):
        try:
            frames.append((frame_time(path), path))
        except ValueError:
            continue
    return sorted(frames)


def window_frames(frames_dir: Path, start: datetime, hours: int) -> list[tuple[datetime, Path]]:
    """The cached frames of one window: from ``start`` through ``hours``
    past it, inclusive, oldest first. The run is the window's first hour,
    so ``start`` is on the hour; the frames are on the five-minute mark."""
    end = start + timedelta(hours=hours)
    return [(slot, path) for slot, path in cached_frames(frames_dir) if start <= slot <= end]


def latest_slot(frames_dir: Path, *, fallback: datetime | None = None) -> datetime:
    """The newest slot the cache holds — the end of the live window. The
    fetch writes the newest grid before this is asked, so a cache with no
    frame is a fetch that failed and ``fallback`` (the grid just parsed)
    stands in."""
    frames = cached_frames(frames_dir)
    if frames:
        return frames[-1][0]
    if fallback is not None:
        return fallback
    raise DownloadError("the aurora frame cache holds nothing")


def _write_dataset(dataset: object, path: Path) -> None:
    """Write one NetCDF file, failing with the operator's hint when the
    optional NetCDF dependency is absent."""
    try:
        dataset.to_netcdf(  # type: ignore[attr-defined]
            path,
            encoding={
                "aurora": {"dtype": "uint8", "scale_factor": 0.5, "_FillValue": 255, "zlib": True, "complevel": 4},
                "time": {"units": "seconds since 1970-01-01T00:00:00Z"},
            },
        )
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DownloadError(f"writing the aurora series needs xarray and netCDF4 ({exc}); {INSTALL_HINT}") from exc


def _coords() -> tuple[np.ndarray, np.ndarray]:
    height, width = GRID_SHAPE
    latitudes = np.arange(-90, 91, dtype=np.float64)
    longitudes = np.arange(0, width, dtype=np.float64)
    assert latitudes.size == height, latitudes.size
    return latitudes, longitudes


def _dataset(frames: list[tuple[datetime, np.ndarray]]) -> object:
    try:
        import xarray as xr  # noqa: PLC0415 - the aurora group, optional everywhere else
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DownloadError(f"writing the aurora series needs xarray and netCDF4 ({exc}); {INSTALL_HINT}") from exc
    latitudes, longitudes = _coords()
    stack = np.stack([values for _, values in frames], axis=0).astype(np.float32)
    times = np.array([np.datetime64(moment.replace(tzinfo=None), "ns") for moment, _ in frames])
    return xr.Dataset(
        {
            "aurora": (
                ("time", "lat", "lon"),
                stack,
                {
                    "long_name": "aurora visibility probability",
                    "units": "%",
                    "grid_mapping": "crs",
                },
            ),
            "crs": (
                (),
                np.int32(0),
                {
                    "grid_mapping_name": "latitude_longitude",
                    "comment": "Regular 1-degree global grid, 0-359E and 90S-90N.",
                },
            ),
        },
        coords={
            "time": ("time", times, {"long_name": "time", "axis": "T"}),
            "lat": ("lat", latitudes, {"long_name": "latitude", "standard_name": "latitude", "units": "degrees_north"}),
            "lon": ("lon", longitudes, {"long_name": "longitude", "standard_name": "longitude", "units": "degrees_east"}),
        },
        attrs={
            "Conventions": "CF-1.8",
            "title": "NOAA SWPC OVATION aurora probability",
            "institution": "NOAA Space Weather Prediction Center",
            "comment": "Probability, in percent, that aurora is visible overhead; one frame per five-minute model run.",
        },
    )


def write_frame(frames_dir: Path, frame: OvationFrame) -> Path:
    """Write one grid into the cache, atomically, and return its path. The
    valid time is snapped first so a re-fetch of the same slot overwrites
    rather than aliases."""
    variable_dir = frames_dir / VARIABLE_DIR
    variable_dir.mkdir(parents=True, exist_ok=True)
    path = variable_dir / frame_name(snap_slot(frame.valid_time))
    temporary = path.with_name(path.name + ".partial.nc")
    _write_dataset(_dataset([(snap_slot(frame.valid_time), frame.values)]), temporary)
    temporary.replace(path)
    return path


def write_window(frames_dir: Path, start: datetime, hours: int, output: Path) -> list[datetime]:
    """Assemble the window's cached frames into the one NetCDF series the
    observation ingest reads (``aurora(time, lat, lon)``, percent), and
    return the slots it holds, oldest first. A window with no cached frame
    is refused: there is nothing to publish."""
    frames = window_frames(frames_dir, start, hours)
    if not frames:
        raise DownloadError(f"the aurora cache holds no frame for {start.isoformat()} through +{hours} h")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".partial.nc")
    _write_dataset(_dataset([(slot, _read_frame(path)) for slot, path in frames]), temporary)
    temporary.replace(output)
    return [slot for slot, _ in frames]


def _read_frame(path: Path) -> np.ndarray:
    try:
        import xarray as xr  # noqa: PLC0415 - the aurora group, optional everywhere else
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DownloadError(f"reading the aurora cache needs xarray and netCDF4 ({exc}); {INSTALL_HINT}") from exc
    with xr.open_dataset(path) as dataset:
        values = np.asarray(dataset["aurora"].values, dtype=np.float64)
    if values.ndim == 3 and values.shape[0] == 1:
        values = values[0]
    if values.shape != GRID_SHAPE:
        raise DownloadError(f"cached aurora frame {path.name} is {values.shape}, not {GRID_SHAPE}")
    return values

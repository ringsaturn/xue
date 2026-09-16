"""The CMA radar mosaic's archive, read a window at a time.

The mosaic reaches Xue through a private archive: one Zarr v3 store per UTC
day at ``<archive>/<product>/z<zoom>/<YYYY>/<YYYY-MM-DD>.zarr``, filled by
a sync job of its own from the agency's data portal. Each store carries a
complete ``time`` axis of the day's 240 six-minute slots, a ``slot_status``
saying which slots were written (1), never fetched (0) or never published
by the portal (2), the ``lat`` / ``lon`` pixel centres of the portal's
plate carrée tile grid, and ``cref``, the day's frames as one sharded
int16 array (dBZ × 10, 32767 missing) whose inner chunks are whole frames,
so a slot is one range request after the shard index.

This module is the reverse trip. It lists the slots the stores hold for a
UTC window, reads the written ones (one orthogonal selection per day
store, so the shard index is read once and each frame once) and writes
them as the NetCDF series the observation ingest has always read
(:mod:`xuebuild.observation`): ``cref(time, lat, lon)`` in dBZ, int16
with ``scale_factor`` 0.1 and ``_FillValue`` 32767, ``time`` in seconds
since the epoch. The series is the hand-off between the fetch and the
converter, which reads it through GDAL the way both encoders do; it is not
a format of its own.

The archive is plain Zarr, so it is read with zarr-python over s3fs (an
``s3://`` prefix) or from a directory the stores were copied to, and the
series is written with xarray — the ``cma`` dependency group, imported
here and nowhere else, so ``xuebuild`` keeps NumPy as its only runtime
dependency for every other source. The archive's location is private and
comes from :data:`ARCHIVE_VARIABLE` alone; its credentials are the
``R2_*`` variables (``R2_ENDPOINT`` or ``R2_ACCOUNT_ID`` /
``CLOUDFLARE_ACCOUNT_ID``, ``R2_ACCESS_KEY_ID``, ``R2_SECRET_ACCESS_KEY``)
with the ``AWS_*`` pair the bucket uploads use as the fallback.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from .errors import DownloadError

LOG = logging.getLogger(__name__)

ARCHIVE_VARIABLE = "XUE_CMA_ARCHIVE"
PRODUCT = "RADAR_L3_MST_CREF_GISJPG_Tiles_CR"
#: The finest zoom the portal publishes; 360 / (256 * 2^5) = 0.0439° a cell.
ZOOM = 5
GRID_STEP = 360.0 / (256 * 2**ZOOM)
MISSING = np.int16(32767)
SCALE = 0.1
SLOT_SECONDS = 360
STATUS_PENDING, STATUS_WRITTEN, STATUS_UNPUBLISHED = 0, 1, 2
INSTALL_HINT = "install the archive readers with: uv sync --group cma"


def archive_base() -> str:
    """The archive base (an ``s3://bucket/prefix`` or a directory), from
    :data:`ARCHIVE_VARIABLE`. Unset is an error the operator fixes, not a
    default: the location is private."""
    base = os.environ.get(ARCHIVE_VARIABLE, "").strip().rstrip("/")
    if not base:
        raise DownloadError(f"set {ARCHIVE_VARIABLE} to the CMA archive base (an s3:// prefix or a directory)")
    return base


def store_url(base: str, day: date, *, product: str = PRODUCT, zoom: int = ZOOM) -> str:
    """The daily store under ``base``."""
    return f"{base.rstrip('/')}/{product}/z{zoom}/{day:%Y}/{day:%Y-%m-%d}.zarr"


def _utc(time: datetime) -> datetime:
    return time.replace(tzinfo=UTC) if time.tzinfo is None else time.astimezone(UTC)


def window_days(start: datetime, end: datetime) -> list[date]:
    """The UTC days whose stores a window touches, first to last."""
    first, last = _utc(start).date(), _utc(end).date()
    days, day = [], first
    while day <= last:
        days.append(day)
        day += timedelta(days=1)
    return days


def _zarr() -> Any:
    try:
        import zarr  # noqa: PLC0415 - the cma group, optional everywhere else
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DownloadError(f"reading the CMA archive needs zarr-python ({exc}); {INSTALL_HINT}") from exc
    return zarr


def _storage_options() -> dict[str, Any]:
    """s3fs options for the archive bucket (Cloudflare R2), from the
    environment: the ``R2_*`` variables first, the ``AWS_*`` pair the
    dataset uploads already use as the fallback."""
    endpoint = os.environ.get("R2_ENDPOINT", "").strip()
    if not endpoint:
        account = os.environ.get("R2_ACCOUNT_ID", "").strip() or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        if not account:
            raise DownloadError("set R2_ENDPOINT, R2_ACCOUNT_ID or CLOUDFLARE_ACCOUNT_ID to reach the CMA archive bucket")
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
    key = os.environ.get("R2_ACCESS_KEY_ID", "").strip() or os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    secret = os.environ.get("R2_SECRET_ACCESS_KEY", "").strip() or os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    if not key or not secret:
        raise DownloadError("set R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY (or the AWS_* pair) to read the CMA archive bucket")
    return {
        "key": key,
        "secret": secret,
        "endpoint_url": endpoint,
        "client_kwargs": {"region_name": "auto"},
        # R2 rejects botocore's newer default of always sending CRC32 trailers.
        "config_kwargs": {
            "request_checksum_calculation": "when_required",
            "response_checksum_validation": "when_required",
        },
    }


def _open_store(url: str) -> Any:
    zarr = _zarr()
    if "://" not in url:
        return zarr.storage.LocalStore(url)
    if not url.startswith("s3://"):
        raise DownloadError(f"the CMA archive must be an s3:// prefix or a directory, not {url}")
    try:
        return zarr.storage.FsspecStore.from_url(url, storage_options=_storage_options())
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DownloadError(f"reading the CMA archive over s3 needs s3fs ({exc}); {INSTALL_HINT}") from exc


def _open_day(url: str) -> Any:
    """The day's group, or None when the archive has no store for it. A
    transport failure is raised, never mistaken for an absent day."""
    zarr = _zarr()
    try:
        return zarr.open_group(store=_open_store(url), mode="r")
    except (FileNotFoundError, ValueError):
        # zarr raises GroupNotFoundError (a ValueError) when nothing is there.
        return None


@dataclass(frozen=True)
class StoredSlot:
    """One slot of a daily store: its UTC time, its status and where it
    lives, so a reader can take the written ones of a window in one
    orthogonal read per day."""

    time: datetime
    status: int
    store: str
    index: int

    @property
    def written(self) -> bool:
        return self.status == STATUS_WRITTEN


def stored_slots(base: str, start: datetime, end: datetime) -> list[StoredSlot]:
    """Every slot the daily stores hold between ``start`` and ``end``
    inclusive (UTC), written or not, in time order. A day without a store
    contributes nothing; only the two small ``time`` and ``slot_status``
    arrays of each store are read."""
    start, end = _utc(start), _utc(end)
    slots: list[StoredSlot] = []
    for day in window_days(start, end):
        url = store_url(base, day)
        root = _open_day(url)
        if root is None:
            continue
        times = np.asarray(root["time"][:], dtype=np.int64)
        status = np.asarray(root["slot_status"][:], dtype=np.int8)
        for index, (seconds, flag) in enumerate(zip(times.tolist(), status.tolist())):
            time = datetime.fromtimestamp(seconds, tz=UTC)
            if start <= time <= end:
                slots.append(StoredSlot(time=time, status=int(flag), store=url, index=index))
    return slots


def written_slots(base: str, start: datetime, end: datetime) -> list[datetime]:
    """The times of the written slots between ``start`` and ``end``."""
    return [slot.time for slot in stored_slots(base, start, end) if slot.written]


@dataclass(frozen=True)
class Window:
    """A window read out of the archive: the frames as dBZ (NaN where
    missing), their times, and the grid's pixel centres."""

    frames: np.ndarray
    """``(time, lat, lon)`` float32."""
    times: list[datetime]
    latitudes: np.ndarray
    longitudes: np.ndarray

    @property
    def grid(self) -> dict[str, Any]:
        return {
            "nlat": int(self.latitudes.size),
            "nlon": int(self.longitudes.size),
            "step": GRID_STEP,
            "north": float(self.latitudes[0]),
            "south": float(self.latitudes[-1]),
            "west": float(self.longitudes[0]),
            "east": float(self.longitudes[-1]),
        }


def unpack(frame: np.ndarray) -> np.ndarray:
    """The float dBZ mosaic of one stored int16 frame."""
    out = frame.astype(np.float32) * np.float32(SCALE)
    out[frame == MISSING] = np.nan
    return out


def read_window(base: str, start: datetime, end: datetime) -> Window | None:
    """The written slots between ``start`` and ``end`` inclusive (UTC), or
    None when the stores hold none. The frames of one day are read with one
    orthogonal selection; the grid comes from the first store's coordinate
    arrays, and every store of the window must agree with it."""
    slots = [slot for slot in stored_slots(base, start, end) if slot.written]
    if not slots:
        return None
    by_store: dict[str, list[StoredSlot]] = {}
    for slot in slots:
        by_store.setdefault(slot.store, []).append(slot)
    frames: list[np.ndarray] = []
    latitudes = longitudes = None
    for url, day_slots in by_store.items():
        root = _open_day(url)
        if root is None:  # pragma: no cover - listed a moment ago
            raise DownloadError(f"the CMA archive store for {url.rsplit('/', 1)[-1]} vanished while it was being read")
        day_latitudes = np.asarray(root["lat"][:], dtype=np.float64)
        day_longitudes = np.asarray(root["lon"][:], dtype=np.float64)
        if latitudes is None:
            latitudes, longitudes = day_latitudes, day_longitudes
        elif not (np.array_equal(latitudes, day_latitudes) and np.array_equal(longitudes, day_longitudes)):
            raise DownloadError("the CMA archive's daily stores of this window are not on one grid")
        indices = np.array([slot.index for slot in day_slots], dtype=np.intp)
        packed = np.asarray(root["cref"].oindex[indices, :, :], dtype=np.int16)
        frames.extend(unpack(frame) for frame in packed)
    assert latitudes is not None and longitudes is not None
    return Window(frames=np.stack(frames), times=[slot.time for slot in slots], latitudes=latitudes, longitudes=longitudes)


def write_series(window: Window, path: Path) -> None:
    """Write a window as the NetCDF series the observation ingest reads:
    ``cref(time, lat, lon)`` int16-packed at 0.1 dBZ with 32767 missing,
    ``time`` in seconds since the epoch, CF attributes on every axis."""
    try:
        import xarray as xr  # noqa: PLC0415 - the cma group, optional everywhere else
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise DownloadError(f"writing the CMA series needs xarray and netCDF4 ({exc}); {INSTALL_HINT}") from exc
    times = np.array([np.datetime64(time.replace(tzinfo=None), "ns") for time in window.times])
    dataset = xr.Dataset(
        {
            "cref": (
                ("time", "lat", "lon"),
                window.frames,
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
                    "comment": "Equirectangular (plate carree) tile grid with uniform lon/lat pixel spacing.",
                },
            ),
        },
        coords={
            "time": ("time", times, {"long_name": "time", "axis": "T"}),
            "lat": ("lat", window.latitudes, {"long_name": "latitude", "standard_name": "latitude", "units": "degrees_north"}),
            "lon": ("lon", window.longitudes, {"long_name": "longitude", "standard_name": "longitude", "units": "degrees_east"}),
        },
        attrs={
            "Conventions": "CF-1.8",
            "title": "CMA weather radar level-3 mosaic composite reflectivity",
            "institution": "China Meteorological Administration",
            "comment": "Six-minute composite reflectivity mosaics on the plate carree tile grid; a slot the "
            "archive never held is absent from the time axis.",
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    dataset.to_netcdf(
        path,
        encoding={
            "cref": {"dtype": "int16", "scale_factor": SCALE, "_FillValue": MISSING, "zlib": True, "complevel": 4},
            "time": {"units": "seconds since 1970-01-01T00:00:00Z"},
        },
    )

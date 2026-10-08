"""Crop-area weights on a published grid: ``indicators/weights/<grid>.zarr``.

One Zarr v3 group per grid (``0p25`` for GFS / ECMWF / AIFS, ``0p1`` for
IFS HRES, ``t126`` for CFSv2), written once by the offline weights script
from SPAM 2020 soybean physical area and Natural Earth admin-1 polygons
(``docs/indicators.md`` §7 is normative):

- group attributes: ``version``, ``spam``, ``grid``, ``lon_convention``
  and ``regions`` (region id → ``{"rows": [row0, row1], "cols": [col0,
  col1]}``, the half-open box of the region's non-zero cells);
- ``weights``: float32 ``[region, lat, lon]`` over the whole published
  grid, zero outside the region, each region summing to 1; one shard per
  region (``sharding_indexed``, index at the end, CRC-32C), inner chunks of
  ``[1, 180, 180]`` under ``[bytes little-endian, zstd]``;
- ``region``, ``lat``, ``lon``: one-dimensional coordinates.

Grid convention: rows and columns are the published grid's own, the ones a
bundle's metadata describes — row-major, north to south, west to east — so
``lon[i] = firstLongitude + i * longitudeStep`` (cell centres from -180
degrees, ascending) and ``lat[j] = firstLatitude + j * latitudeStep``
(descending). :func:`check_grid` holds a store to the grid of the bundle
store it is applied to.

The reader is NumPy and the standard library: two documents, then per
region one suffix range for its shard's index and the ranges of the inner
chunks its box overlaps, through the same helpers the forecast store
reader uses (:mod:`.store`).
"""

from __future__ import annotations

import json
import re
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .. import zarrstore, zstdcli
from ..errors import XueError
from .schema import IndicatorsError
from .store import Fetch, fetch_for, load_range, paste_tile, plan_ranges, read_shard_index, tiles_for

GRID_ID = re.compile(r"^[a-z0-9]+$")
ARRAY = "weights"
DIMENSIONS = ["region", "lat", "lon"]
_SUM_TOLERANCE = 1e-3
_COORDINATE_TOLERANCE = 1e-6


@dataclass(frozen=True)
class RegionWeights:
    rows: slice
    cols: slice
    weights: np.ndarray
    """float64 over the box, normalised to sum to what float64 makes of 1."""

    @property
    def shape(self) -> tuple[int, int]:
        return self.rows.stop - self.rows.start, self.cols.stop - self.cols.start


@dataclass(frozen=True)
class Weights:
    version: str
    spam: str
    grid: str
    lon_convention: str
    shape: tuple[int, int]
    lon: np.ndarray
    lat: np.ndarray
    regions: dict[str, RegionWeights]
    crc32: str
    """CRC-32 of the group and array documents followed by every loaded
    region's boxed float32 weights, little-endian, in store order: what the
    lines record, so a change of weights shows in them."""

    def __getitem__(self, region_id: str) -> tuple[slice, slice, np.ndarray]:
        entry = self.regions[region_id]
        return entry.rows, entry.cols, entry.weights


def _document(fetch: Fetch, url: str) -> tuple[dict[str, Any], bytes]:
    raw = fetch(url, None)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IndicatorsError(f"{url} is not JSON") from exc
    if not isinstance(payload, dict) or payload.get("zarr_format") != 3:
        raise IndicatorsError(f"{url} is not a Zarr v3 document")
    return payload, raw


def _text(attributes: dict[str, Any], key: str, where: str) -> str:
    value = attributes.get(key)
    if not isinstance(value, str) or not value:
        raise IndicatorsError(f"{where}: attribute {key!r} must be a non-empty string")
    return value


def _codec_names(codecs: object) -> list[str]:
    return [codec.get("name") for codec in codecs] if isinstance(codecs, list) else []


def _little_endian(codec: dict[str, Any]) -> bool:
    return codec.get("configuration", {}).get("endian", "little") == "little"


def _vector_dtype(data_type: object, where: str) -> np.dtype:
    if data_type == "float64":
        return np.dtype("<f8")
    if isinstance(data_type, dict) and data_type.get("name") == "fixed_length_utf32":
        length = data_type.get("configuration", {}).get("length_bytes")
        if isinstance(length, int) and length > 0 and length % 4 == 0:
            return np.dtype(f"<U{length // 4}")
    raise IndicatorsError(f"{where}: unsupported coordinate data type {data_type!r}")


def _read_vector(fetch: Fetch, root: str, name: str) -> np.ndarray:
    """A one-dimensional, unsharded coordinate array: regular chunks under
    ``[bytes]`` or ``[bytes, zstd]``."""
    where = f"{root}{name}"
    metadata, _raw = _document(fetch, f"{where}/zarr.json")
    shape = metadata.get("shape")
    chunks = metadata.get("chunk_grid", {}).get("configuration", {}).get("chunk_shape")
    if not (isinstance(shape, list) and len(shape) == 1 and isinstance(chunks, list) and len(chunks) == 1):
        raise IndicatorsError(f"{where}: not a one-dimensional regular array")
    codecs = metadata.get("codecs", [])
    names = _codec_names(codecs)
    if names not in (["bytes"], ["bytes", "zstd"]) or not _little_endian(codecs[0]):
        raise IndicatorsError(f"{where}: codecs must be [bytes little-endian] with an optional zstd")
    dtype = _vector_dtype(metadata.get("data_type"), where)
    encoding = metadata.get("chunk_key_encoding", {})
    separator = encoding.get("configuration", {}).get("separator", "/" if encoding.get("name") == "default" else ".")
    prefix = "c" + separator if encoding.get("name", "default") == "default" else ""
    length, step = int(shape[0]), int(chunks[0])
    parts = []
    for chunk in range(-(-length // step)):
        raw = fetch(f"{where}/{prefix}{chunk}", None)
        if names[-1] == "zstd":
            raw = zstdcli.decompress(raw, expected_length=step * dtype.itemsize)
        parts.append(np.frombuffer(raw, dtype=dtype))
    vector = np.concatenate(parts)[:length] if parts else np.empty(0, dtype)
    if vector.shape != (length,):
        raise IndicatorsError(f"{where}: chunks do not make up the array")
    return vector


def _array_layout(metadata: dict[str, Any], where: str) -> tuple[zarrstore.ArrayGeometry, str]:
    if metadata.get("data_type") != "float32" or metadata.get("dimension_names") != DIMENSIONS:
        raise IndicatorsError(f"{where}: weights must be float32 over {DIMENSIONS}")
    try:
        geometry = zarrstore.ArrayGeometry.from_metadata(metadata)
    except XueError as exc:
        raise IndicatorsError(f"{where}: {exc}") from exc
    configuration = metadata["codecs"][0].get("configuration", {})
    inner = configuration.get("codecs", [])
    index = configuration.get("index_codecs", [])
    if _codec_names(inner) != ["bytes", "zstd"] or not _little_endian(inner[0]):
        raise IndicatorsError(f"{where}: inner codecs must be [bytes little-endian, zstd]")
    if _codec_names(index) != ["bytes", "crc32c"] or not _little_endian(index[0]):
        raise IndicatorsError(f"{where}: index codecs must be [bytes little-endian, crc32c]")
    if geometry.time_chunk != 1 or geometry.shard_frames != 1:
        raise IndicatorsError(f"{where}: one shard per region, one region per inner chunk")
    if metadata.get("fill_value") not in (0, 0.0):
        raise IndicatorsError(f"{where}: fill_value must be 0")
    location = configuration.get("index_location", "end")
    if location not in zarrstore.INDEX_LOCATIONS:
        raise IndicatorsError(f"{where}: unexpected index location {location!r}")
    return geometry, location


def _box(entry: object, region_id: str, shape: tuple[int, int], where: str) -> tuple[slice, slice]:
    if not isinstance(entry, dict) or set(entry) != {"rows", "cols"}:
        raise IndicatorsError(f"{where}: regions.{region_id} must carry rows and cols")
    bounds = []
    for key, size in (("rows", shape[0]), ("cols", shape[1])):
        pair = entry[key]
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(value, int) and not isinstance(value, bool) for value in pair)
            or not 0 <= pair[0] < pair[1] <= size
        ):
            raise IndicatorsError(f"{where}: regions.{region_id}.{key} must be [start, stop) inside the grid")
        bounds.append(slice(pair[0], pair[1]))
    return bounds[0], bounds[1]


def load(location: str | Path, *, regions: Iterable[str] | None = None) -> Weights:
    """Read and validate a weights store, a directory or an HTTP(S) URL;
    ``regions`` limits what is read (default: every region)."""
    root, fetch = fetch_for(location)
    try:
        group, group_raw = _document(fetch, f"{root}zarr.json")
        if group.get("node_type") != "group":
            raise IndicatorsError(f"{root}: not a Zarr group")
        attributes = group.get("attributes") or {}
        version = _text(attributes, "version", root)
        spam = _text(attributes, "spam", root)
        grid = _text(attributes, "grid", root)
        lon_convention = _text(attributes, "lon_convention", root)
        if not GRID_ID.match(grid):
            raise IndicatorsError(f"{root}: grid {grid!r} must be a lowercase id")
        boxes = attributes.get("regions")
        if not isinstance(boxes, dict) or not boxes:
            raise IndicatorsError(f"{root}: attribute 'regions' must name at least one region")
        metadata, array_raw = _document(fetch, f"{root}{ARRAY}/zarr.json")
        geometry, location_in_shard = _array_layout(metadata, f"{root}{ARRAY}")
        shape = (geometry.height, geometry.width)
        lat = _read_vector(fetch, root, "lat")
        lon = _read_vector(fetch, root, "lon")
        if lat.shape != (shape[0],) or lon.shape != (shape[1],) or lat.dtype.kind != "f" or lon.dtype.kind != "f":
            raise IndicatorsError(f"{root}: lat must be float64 [ny] and lon float64 [nx]")
        order = list(boxes)
        names = _read_vector(fetch, root, "region")
        if names.dtype.kind == "U":
            order = [str(name) for name in names]
            if sorted(order) != sorted(boxes):
                raise IndicatorsError(f"{root}: the region coordinate and the attributes name different regions")
        if len(order) != geometry.frame_count:
            raise IndicatorsError(f"{root}: {len(order)} regions but the weights array holds {geometry.frame_count}")
        wanted = set(order) if regions is None else set(regions)
        checksum = zlib.crc32(array_raw, zlib.crc32(group_raw))
        loaded: dict[str, RegionWeights] = {}
        for shard, region_id in enumerate(order):
            if region_id not in wanted:
                continue
            rows, cols = _box(boxes[region_id], region_id, shape, root)
            url = f"{root}{ARRAY}/c/{shard}/0/0"
            index = read_shard_index(fetch, url, geometry.chunks_per_shard, location_in_shard, f"{root}{ARRAY}")
            boxed = np.zeros((1, rows.stop - rows.start, cols.stop - cols.start), dtype="<f4")
            for span in plan_ranges(index, 0, tiles_for(geometry, rows, cols)):
                for tile, block in load_range(
                    fetch, url, span, geometry.inner_shape, np.dtype("<f4"), require_checksum=False, label=url
                ):
                    paste_tile(boxed, rows, cols, geometry, tile, block)
            checksum = zlib.crc32(boxed.tobytes(), checksum)
            values = boxed[0].astype(np.float64)
            if not np.isfinite(values).all() or (values < 0).any():
                raise IndicatorsError(f"{root}: {region_id} weights must be finite and non-negative")
            total = float(values.sum())
            if abs(total - 1.0) > _SUM_TOLERANCE:
                raise IndicatorsError(f"{root}: {region_id} weights sum to {total} inside their box, not 1")
            # Stored as float32, the weights sum to 1 only to float32's
            # precision; dividing by the float64 sum once, here, makes a
            # constant field come back as itself.
            loaded[region_id] = RegionWeights(rows, cols, values / total)
    except XueError as exc:
        if isinstance(exc, IndicatorsError):
            raise
        raise IndicatorsError(f"weights {location}: {exc}") from exc
    missing = wanted - set(loaded)
    if regions is not None and missing:
        raise IndicatorsError(f"{root}: no weights for {', '.join(sorted(missing))}")
    return Weights(
        version=version,
        spam=spam,
        grid=grid,
        lon_convention=lon_convention,
        shape=shape,
        lon=lon.astype(np.float64),
        lat=lat.astype(np.float64),
        regions=loaded,
        crc32=f"{checksum & 0xFFFFFFFF:08x}",
    )


def check_grid(weights: Weights, grid: dict[str, Any]) -> None:
    """Refuse to apply ``weights`` to a store whose grid (a bundle
    metadata ``grid`` block) is not the one they were cut on."""
    width, height = grid.get("width"), grid.get("height")
    if (height, width) != weights.shape:
        raise IndicatorsError(
            f"weights {weights.grid} are {weights.shape[0]} x {weights.shape[1]}, the store is {height} x {width}"
        )
    lon = grid["firstLongitude"] + np.arange(width, dtype=np.float64) * grid["longitudeStep"]
    lat = grid["firstLatitude"] + np.arange(height, dtype=np.float64) * grid["latitudeStep"]
    if not np.allclose(lon, weights.lon, rtol=0.0, atol=_COORDINATE_TOLERANCE) or not np.allclose(
        lat, weights.lat, rtol=0.0, atol=_COORDINATE_TOLERANCE
    ):
        raise IndicatorsError(f"weights {weights.grid} cell centres do not match the store's grid")

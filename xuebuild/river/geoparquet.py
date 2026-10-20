"""The reaches' geometry as GeoParquet 1.1 (``docs/zarr-profile.md``,
"Network store"): WKB LineStrings in WGS84 with a ``bbox`` covering column,
rows Hilbert-sorted by centroid and cut into row groups of exactly
``rowGroupRows`` rows, so row group ``g`` holds the reaches of the network
store's feature chunk ``g``.

pyarrow is imported on use: only a river build writes or reads GeoParquet,
and nothing else in the build needs it.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..errors import XueError

GEO_VERSION = "1.1.0"
HILBERT_ORDER = 16
"""Bits per axis of the Hilbert grid: 65 536 cells a side, finer than any
reach's centroid needs to be told apart."""

COLUMNS = ("feature_id", "order", "gage", "geometry", "bbox")


class GeoParquetError(XueError):
    """Geometry that cannot be written as, or read back from, GeoParquet."""


def _pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise GeoParquetError("writing GeoParquet needs pyarrow (pip install pyarrow)") from exc
    return pa, pq


# -- WKB ------------------------------------------------------------------------


def linestring_coordinates(wkb: bytes) -> np.ndarray:
    """The ``[n, 2]`` coordinates of one WKB LineString (2D, either byte
    order). Anything else is refused: a reach is one line."""
    if len(wkb) < 9:
        raise GeoParquetError("WKB too short")
    order = "<" if wkb[0] == 1 else ">" if wkb[0] == 0 else None
    if order is None:
        raise GeoParquetError("WKB byte order must be 0 or 1")
    kind, count = struct.unpack_from(f"{order}II", wkb, 1)
    if kind != 2:
        raise GeoParquetError(f"WKB geometry type {kind} is not a 2D LineString")
    if count < 2 or len(wkb) != 9 + 16 * count:
        raise GeoParquetError("WKB LineString length disagrees with its point count")
    return np.frombuffer(wkb, dtype=f"{order}f8", count=2 * count, offset=9).reshape(count, 2).astype(np.float64)


def centroid(coordinates: np.ndarray) -> tuple[float, float]:
    """The length-weighted centroid of a line (its first point when it has
    no length)."""
    starts, ends = coordinates[:-1], coordinates[1:]
    lengths = np.hypot(*(ends - starts).T)
    total = lengths.sum()
    if total <= 0:
        return float(coordinates[0, 0]), float(coordinates[0, 1])
    middle = (starts + ends) / 2
    return float((middle[:, 0] * lengths).sum() / total), float((middle[:, 1] * lengths).sum() / total)


def bounds32(coordinates: np.ndarray) -> tuple[float, float, float, float]:
    """The line's box in float32, rounded outward so the stored box always
    contains the line."""
    xmin, ymin = coordinates.min(axis=0)
    xmax, ymax = coordinates.max(axis=0)
    down = lambda value: np.float32(value) if np.float32(value) <= value else np.nextafter(np.float32(value), np.float32(-np.inf))  # noqa: E731
    up = lambda value: np.float32(value) if np.float32(value) >= value else np.nextafter(np.float32(value), np.float32(np.inf))  # noqa: E731
    return float(down(xmin)), float(down(ymin)), float(up(xmax)), float(up(ymax))


# -- Hilbert --------------------------------------------------------------------


def hilbert_index(x: np.ndarray, y: np.ndarray, order: int = HILBERT_ORDER) -> np.ndarray:
    """The distance along the Hilbert curve of cells ``(x, y)`` on a
    ``2^order`` grid (the classic rotate-and-flip walk, vectorised)."""
    x = np.asarray(x, dtype=np.int64).copy()
    y = np.asarray(y, dtype=np.int64).copy()
    side = 1 << order
    if (x < 0).any() or (y < 0).any() or (x >= side).any() or (y >= side).any():
        raise GeoParquetError("Hilbert cell outside the grid")
    distance = np.zeros_like(x)
    s = side >> 1
    while s > 0:
        rx = (x & s) > 0
        ry = (y & s) > 0
        distance += s * s * ((3 * rx) ^ ry)
        # Rotate the quadrant so the curve inside it runs the canonical way.
        flip = ~ry & rx
        x = np.where(flip, side - 1 - x, x)
        y = np.where(flip, side - 1 - y, y)
        swap = ~ry
        x, y = np.where(swap, y, x), np.where(swap, x, y)
        s >>= 1
    return distance


def hilbert_order(points: np.ndarray, order: int = HILBERT_ORDER) -> np.ndarray:
    """The permutation sorting ``[n, 2]`` points along the Hilbert curve
    over their own extent; ties keep their input order."""
    low = points.min(axis=0)
    span = np.maximum(points.max(axis=0) - low, 1e-12)
    cells = np.minimum(((points - low) / span * (1 << order)).astype(np.int64), (1 << order) - 1)
    return np.argsort(hilbert_index(cells[:, 0], cells[:, 1], order), kind="stable")


# -- writing ----------------------------------------------------------------------


@dataclass
class Reaches:
    """The reaches of one network, in some row order."""

    feature_id: np.ndarray
    order: np.ndarray
    gage: list[str | None]
    wkb: list[bytes]

    def __len__(self) -> int:
        return len(self.feature_id)

    def take(self, permutation: np.ndarray) -> Reaches:
        return Reaches(
            feature_id=self.feature_id[permutation],
            order=self.order[permutation],
            gage=[self.gage[i] for i in permutation],
            wkb=[self.wkb[i] for i in permutation],
        )


@dataclass
class GeoParquetReport:
    path: Path
    byte_length: int
    rows: int
    row_group_rows: int
    footer_bytes: int
    row_group_bytes: list[int] = field(default_factory=list)
    bbox: list[float] = field(default_factory=list)


def read_reaches(path: Path) -> Reaches:
    """``feature_id``, ``order``, ``gage`` and WKB ``geometry`` from a
    GeoParquet (the case's work file or one this module wrote)."""
    _pa, pq = _pyarrow()
    table = pq.read_table(path, columns=["feature_id", "order", "gage", "geometry"])
    gage = [None if value is None or not str(value).strip() else str(value).strip() for value in table.column("gage").to_pylist()]
    return Reaches(
        feature_id=np.asarray(table.column("feature_id").to_numpy(), dtype=np.int64),
        order=np.asarray(table.column("order").to_numpy(), dtype=np.int8),
        gage=gage,
        wkb=[bytes(value) for value in table.column("geometry").to_pylist()],
    )


def hilbert_permutation(reaches: Reaches) -> np.ndarray:
    """The row order the GeoParquet is written in: Hilbert by centroid."""
    points = np.array([centroid(linestring_coordinates(wkb)) for wkb in reaches.wkb], dtype=np.float64)
    return hilbert_order(points)


def footer_length(path: Path) -> int:
    """The Thrift footer's length, from the eight bytes before the end."""
    with path.open("rb") as handle:
        handle.seek(-8, 2)
        tail = handle.read(8)
    if tail[4:] != b"PAR1":
        raise GeoParquetError(f"{path} does not end in PAR1")
    return struct.unpack("<I", tail[:4])[0]


def write_reaches(path: Path, reaches: Reaches, *, row_group_rows: int) -> GeoParquetReport:
    """Write ``reaches`` in the order given as GeoParquet 1.1, row groups of
    exactly ``row_group_rows`` rows (the last shorter), zstd."""
    pa, pq = _pyarrow()
    if not len(reaches):
        raise GeoParquetError("no reaches to write")
    boxes = np.array([bounds32(linestring_coordinates(wkb)) for wkb in reaches.wkb], dtype=np.float32)
    bbox_type = pa.struct([(name, pa.float32()) for name in ("xmin", "ymin", "xmax", "ymax")])
    table = pa.table(
        {
            "feature_id": pa.array(reaches.feature_id, type=pa.int64()),
            "order": pa.array(reaches.order, type=pa.int8()),
            "gage": pa.array(reaches.gage, type=pa.string()),
            "geometry": pa.array(reaches.wkb, type=pa.binary()),
            "bbox": pa.StructArray.from_arrays([pa.array(boxes[:, i]) for i in range(4)], fields=list(bbox_type)),
        }
    )
    extent = [
        float(boxes[:, 0].min()),
        float(boxes[:, 1].min()),
        float(boxes[:, 2].max()),
        float(boxes[:, 3].max()),
    ]
    geo = {
        "version": GEO_VERSION,
        "primary_column": "geometry",
        "columns": {
            "geometry": {
                # No "crs": GeoParquet's default, OGC:CRS84 (WGS84, lon/lat).
                "encoding": "WKB",
                "geometry_types": ["LineString"],
                "bbox": extent,
                "covering": {
                    "bbox": {
                        "xmin": ["bbox", "xmin"],
                        "ymin": ["bbox", "ymin"],
                        "xmax": ["bbox", "xmax"],
                        "ymax": ["bbox", "ymax"],
                    }
                },
            }
        },
    }
    table = table.replace_schema_metadata({"geo": json.dumps(geo, separators=(",", ":"))})
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        table,
        path,
        row_group_size=row_group_rows,
        compression="zstd",
        write_statistics=True,
    )
    return describe(path, row_group_rows)


def describe(path: Path, row_group_rows: int) -> GeoParquetReport:
    """Measure a GeoParquet and hold it to the layout: the columns, the
    ``geo`` metadata and row groups of exactly ``row_group_rows`` rows."""
    _pa, pq = _pyarrow()
    handle = pq.ParquetFile(path)
    metadata = handle.metadata
    names = handle.schema_arrow.names
    if tuple(names) != COLUMNS:
        raise GeoParquetError(f"{path}: columns must be {COLUMNS}, not {tuple(names)}")
    geo = json.loads((handle.schema_arrow.metadata or {}).get(b"geo", b"null"))
    if not isinstance(geo, dict) or geo.get("primary_column") != "geometry":
        raise GeoParquetError(f"{path}: no GeoParquet metadata")
    column = geo["columns"]["geometry"]
    if column.get("encoding") != "WKB" or "bbox" not in column.get("covering", {}):
        raise GeoParquetError(f"{path}: geometry must be WKB with a bbox covering")
    sizes = []
    for group in range(metadata.num_row_groups):
        rows = metadata.row_group(group).num_rows
        last = group == metadata.num_row_groups - 1
        if rows != row_group_rows and not (last and 0 < rows < row_group_rows):
            raise GeoParquetError(f"{path}: row group {group} holds {rows} rows, not {row_group_rows}")
        sizes.append(sum(metadata.row_group(group).column(i).total_compressed_size for i in range(metadata.row_group(group).num_columns)))
    return GeoParquetReport(
        path=path,
        byte_length=path.stat().st_size,
        rows=metadata.num_rows,
        row_group_rows=row_group_rows,
        footer_bytes=footer_length(path),
        row_group_bytes=sizes,
        bbox=list(column.get("bbox", [])),
    )


def row_feature_ids(path: Path) -> np.ndarray:
    """The ``feature_id`` column in row order."""
    _pa, pq = _pyarrow()
    return np.asarray(pq.read_table(path, columns=["feature_id"]).column("feature_id").to_numpy(), dtype=np.int64)


def geo_metadata(path: Path) -> dict[str, Any]:
    _pa, pq = _pyarrow()
    return json.loads(pq.ParquetFile(path).schema_arrow.metadata[b"geo"])

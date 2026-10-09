"""Build the soybean area-weight rasters of the indicators product.

Offline: a person runs this once (and again only when the crop mask or the
region table changes), then uploads the result with
``make upload-r2-indicators-weights``. Nothing in ``xuebuild`` imports it, and
the packages it needs live in the optional ``indicators`` dependency group::

    uv sync --group indicators
    .venv/bin/python scripts/indicators_weights.py \\
        --spam path/to/spam2020V2r2_global_physical_area/spam2020_v2r2_global_A_SOYB_A.tif \\
        --admin1 path/to/ne_10m_admin_1_states_provinces.zip

Inputs, both local paths (nothing is downloaded):

* The IFPRI SPAM 2020 v2r2 soybean physical-area GeoTIFF, band ``SOYB_A``
  (hectares per 5 arc-minute cell, CC BY 4.0, DOI 10.7910/DVN/SWPENT). The
  dataset sits behind a Harvard Dataverse guestbook: the native API answers
  an anonymous ``GET /api/access/datafile/13827041`` (the physical-area
  GeoTIFF archive) with HTTP 400 "You may not download this file without the
  required Guestbook response", so download
  ``spam2020V2r2_global_physical_area.geotiff.zip`` in a browser from
  https://doi.org/10.7910/DVN/SWPENT, accept the form, and unzip it.
* The Natural Earth 10 m admin-1 states and provinces archive (public
  domain)::

      curl -LO https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_1_states_provinces.zip

Output: ``data/indicators/weights/<grid>.zarr`` (a Zarr v3 store) for the
grids ``0p25`` (GFS, ECMWF, AIFS), ``0p1`` (IFS HRES) and ``t126`` (CFSv2).

Method. Each region is the union of the admin-1 polygons named in
``REGIONS``. The polygons are rasterised onto the SPAM grid by cell centre
and multiplied by the SPAM area. That area is then moved onto each published
grid by exact overlap. Two regular lon/lat grids overlap in a rectangle per
cell pair, so the fraction of a SPAM cell that falls into a target cell is a
longitude overlap times a latitude overlap, each divided by the SPAM cell's
own extent. Longitude is measured in degrees, latitude as the sine of the
latitude, which is proportional to area on the sphere, so no mass is created
or lost and the total is conserved. The matrix product ``Ly @ A @ Lx.T``
does the whole resampling with NumPy; no raster library is involved at that
step. The result is cropped to the region's bounding box and normalised to
sum to 1.

Published-grid convention (docs/format.md, ``grid`` block): the published
values are samples at the grid points, the first column is longitude -180
(Greenwich-first grids are rolled), rows run north to south, and a point's
cell is the rectangle of one step around it. So for ``0p25`` the longitudes
are ``-180 + 0.25 * i`` (i < 1440) and the latitudes ``90 - 0.25 * j``
(j < 721); the polar rows own half a cell. ``t126`` is the CFSv2 Gaussian
grid treated as equidistant, as GDAL reports it (a uniform geotransform
between the first and last true Gaussian latitudes): 384 longitudes
``-180 + 0.9375 * i`` and 190 latitudes evenly spaced from the northernmost
Gaussian latitude to its mirror.

Layout of each store (one Zarr v3 group, no consolidated metadata):

* Group attributes: ``version`` (weights revision, SPAM and Natural Earth
  versions), ``spam`` (``"SPAM 2020 v2r2 SOYB_A"``), ``citation`` and
  ``notice`` (what IFPRI's terms of use ask for), ``grid``, ``regions``
  (region id -> ``{"rows": [row0, row1], "cols": [col0, col1]}``, the
  half-open bounding box of the non-zero cells) and ``lon_convention``.
* ``weights`` float32 ``[region, lat, lon]`` on the full published grid, zero
  outside the region, each region summing to 1. One shard per region (the
  grid rounded up to whole inner chunks, as a shard must be a multiple of
  them) cut into ``[1, 180, 180]`` inner chunks, little-endian
  bytes + zstd, the shard index at the end.
* ``region`` (ids), ``lat`` float64 ``[ny]`` and ``lon`` float64 ``[nx]``
  (the grid points in the published order, see above).

Opens as a dataset with ``xarray.open_zarr(path, consolidated=False)``.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
import zipfile
from pathlib import Path

import numpy as np

from xuebuild.indicators.weights import SPAM_CITATION, SPAM_NOTICE

SPAM_LABEL = "SPAM 2020 v2r2 SOYB_A"

# Member admin-1 codes (ISO 3166-2, as the Natural Earth ``iso_3166_2`` field
# spells them). The parents are the unions of their members; the sub-regions
# are the leading states of each country.
US = ["US-IA", "US-IL", "US-MN", "US-IN", "US-NE", "US-OH", "US-MO", "US-SD", "US-ND", "US-KS", "US-WI", "US-MI", "US-AR"]
BR = ["BR-MT", "BR-PR", "BR-RS", "BR-GO", "BR-MS", "BR-MG", "BR-BA", "BR-MA", "BR-TO", "BR-PI"]
AR = ["AR-B", "AR-X", "AR-S", "AR-E", "AR-L"]  # Buenos Aires, Cordoba, Santa Fe, Entre Rios, La Pampa
REGIONS: dict[str, list[str]] = {
    "us-soy": US,
    "br-soy": BR,
    "ar-soy": AR,
    "us-ia": ["US-IA"],
    "us-il": ["US-IL"],
    "us-mn": ["US-MN"],
    "us-in": ["US-IN"],
    "us-ne": ["US-NE"],
    "br-mt": ["BR-MT"],
    "br-pr": ["BR-PR"],
    "br-rs": ["BR-RS"],
    "br-go": ["BR-GO"],
    "br-ms": ["BR-MS"],
    "ar-ba": ["AR-B"],
    "ar-cb": ["AR-X"],
    "ar-sf": ["AR-S"],
}


def grid_axes(name: str) -> tuple[np.ndarray, np.ndarray]:
    """Grid-point longitudes and latitudes of a published grid."""
    if name == "0p25":
        step, nx, ny = 0.25, 1440, 721
    elif name == "0p1":
        step, nx, ny = 0.1, 3600, 1801
    elif name == "t126":
        nx, ny = 384, 190
        x, _ = np.polynomial.legendre.leggauss(ny)
        first = float(np.degrees(np.arcsin(x.max())))
        return -180.0 + (360.0 / nx) * np.arange(nx), np.linspace(first, -first, ny)
    else:
        raise SystemExit(f"unknown grid {name!r}")
    return -180.0 + step * np.arange(nx), 90.0 - step * np.arange(ny)


def cell_edges(centres: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Lower and upper edges of the one-step cell around each grid point."""
    half = abs(float(centres[1] - centres[0])) / 2.0
    return centres - half, centres + half


# --- Natural Earth shapefile (the one reader the dependency group lacks) ---


def _dbf_codes(data: bytes) -> list[str]:
    count, header, record = struct.unpack("<IHH", data[4:12])
    offset, at, field = 1, 32, None
    while data[at] != 0x0D:
        name = data[at : at + 11].split(b"\0")[0].decode()
        length = data[at + 16]
        if name == "iso_3166_2":
            field = (offset, length)
        offset += length
        at += 32
    if field is None:
        raise SystemExit("the admin-1 archive has no iso_3166_2 field")
    start, length = field
    return [
        data[header + i * record + start : header + i * record + start + length].decode("utf-8", "replace").strip("\0 ")
        for i in range(count)
    ]


def _polygon_records(data: bytes):
    """Yield the list of rings of each record of a polygon .shp, in order."""
    at = 100
    while at < len(data):
        (words,) = struct.unpack(">i", data[at + 4 : at + 8])
        body = data[at + 8 : at + 8 + 2 * words]
        at += 8 + 2 * words
        (kind,) = struct.unpack("<i", body[:4])
        if kind == 0:
            yield []
            continue
        if kind != 5:
            raise SystemExit(f"unexpected shape type {kind}; this reads polygons only")
        parts, points = struct.unpack("<ii", body[36:44])
        starts = list(struct.unpack(f"<{parts}i", body[44 : 44 + 4 * parts])) + [points]
        xy = np.frombuffer(body, dtype="<f8", count=2 * points, offset=44 + 4 * parts).reshape(points, 2)
        yield [xy[starts[i] : starts[i + 1]] for i in range(parts)]


def read_admin1(path: Path, codes: set[str]) -> tuple[dict[str, object], str]:
    """Return ``{iso_3166_2: shapely geometry}`` for the wanted codes, and the
    Natural Earth version the archive declares."""
    import shapely

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()

        def member(suffix: str) -> bytes:
            return archive.read(next(n for n in names if n.endswith(suffix)))

        dbf, shp = member(".dbf"), member(".shp")
        try:
            version = member(".VERSION.txt").decode().strip()
        except StopIteration:
            version = "unknown"
    labels = _dbf_codes(dbf)
    found: dict[str, list] = {}
    for label, rings in zip(labels, _polygon_records(shp), strict=True):
        if label not in codes:
            continue
        outers, holes = [], []
        for ring in rings:
            x, y = ring[:, 0], ring[:, 1]
            signed = 0.5 * float(np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))
            (outers if signed < 0 else holes).append(shapely.Polygon(ring))  # shapefile outers are clockwise
        for outer in outers:
            inside = [h for h in holes if outer.contains(h.representative_point())]
            found.setdefault(label, []).append(shapely.Polygon(outer.exterior, [h.exterior for h in inside]))
    missing = sorted(codes - set(found))
    if missing:
        raise SystemExit(f"admin-1 codes not in the archive: {missing}")
    return {code: shapely.union_all(parts) for code, parts in found.items()}, version


# --- resampling ---


def axis_overlap(
    src_edges: np.ndarray, lo: np.ndarray, hi: np.ndarray, measure=lambda v: v, shifts: tuple[float, ...] = (0.0,)
) -> np.ndarray:
    """[target, source] fraction of each source interval covered by each target
    interval: the overlap in ``measure`` space over the source's own extent.
    ``src_edges`` ascend; ``shifts`` re-test the target interval displaced by
    those offsets (a full turn of longitude)."""
    s_lo, s_hi = measure(src_edges[:-1]), measure(src_edges[1:])
    out = np.zeros((lo.size, s_lo.size))
    for shift in shifts:
        a, b = measure(lo + shift)[:, None], measure(hi + shift)[:, None]
        out += np.clip(np.minimum(b, s_hi[None]) - np.maximum(a, s_lo[None]), 0.0, None)
    return out / (s_hi - s_lo)[None]


def resample(area: np.ndarray, lon_edges: np.ndarray, lat_edges: np.ndarray, grid: str) -> np.ndarray:
    """Move ``area`` (rows north to south on the SPAM grid, cell edges given)
    onto the published grid by exact overlap; returns the full [ny, nx]."""
    lon, lat = grid_axes(grid)
    lon_lo, lon_hi = cell_edges(lon)
    lat_lo, lat_hi = cell_edges(lat)
    lat_lo, lat_hi = np.clip(lat_lo, -90.0, 90.0), np.clip(lat_hi, -90.0, 90.0)
    lx = axis_overlap(lon_edges, lon_lo, lon_hi, shifts=(-360.0, 0.0, 360.0))
    # Source rows run north to south; flip to ascending latitude for the overlap.
    ly = axis_overlap(lat_edges[::-1], lat_lo, lat_hi, measure=lambda v: np.sin(np.radians(v)))
    return ly @ area[::-1] @ lx.T


LON_CONVENTION = "Grid points at cell centres: longitudes ascend from -180, latitudes run north to south."


def write_grid(grid: str, area_by_region: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]], version: str, path: Path) -> None:
    import zarr
    from zarr.codecs import BytesCodec, ZstdCodec

    lon, lat = grid_axes(grid)
    ny, nx = lat.size, lon.size
    ids = list(area_by_region)
    if path.exists():
        shutil.rmtree(path)
    root = zarr.open_group(path, mode="w", zarr_format=3)
    weights = root.create_array(
        "weights",
        shape=(len(ids), ny, nx),
        dtype="float32",
        chunks=(1, 180, 180),
        shards=(1, -(-ny // 180) * 180, -(-nx // 180) * 180),
        fill_value=0.0,
        dimension_names=("region", "lat", "lon"),
        serializer=BytesCodec(endian="little"),
        compressors=ZstdCodec(level=9, checksum=False),
    )
    boxes: dict[str, dict[str, list[int]]] = {}
    for k, region in enumerate(ids):
        area, lon_edges, lat_edges = area_by_region[region]
        full = resample(area, lon_edges, lat_edges, grid)
        total = float(full.sum())
        if total <= 0.0:
            raise SystemExit(f"{region}: no soybean area lands on {grid}")
        rows, cols = np.nonzero(full.sum(axis=1))[0], np.nonzero(full.sum(axis=0))[0]
        r0, r1, c0, c1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
        weights[k] = (full / total).astype(np.float32)
        boxes[region] = {"rows": [r0, r1], "cols": [c0, c1]}
        print(
            f"  {grid} {region}: rows {r0}:{r1} cols {c0}:{c1} ({r1 - r0}x{c1 - c0}), "
            f"area {area.sum():.4g} ha -> {total:.4g} ha ({total / area.sum():.6f})"
        )
    for name, values, dim in (("region", np.array(ids), "region"), ("lat", lat, "lat"), ("lon", lon, "lon")):
        root.create_array(name, data=values, dimension_names=(dim,), chunks=values.shape)
    root.attrs.update(
        {
            "version": version,
            "spam": SPAM_LABEL,
            "citation": SPAM_CITATION,
            "notice": SPAM_NOTICE,
            "grid": grid,
            "regions": boxes,
            "lon_convention": LON_CONVENTION,
        }
    )


def spam_regions(spam: Path, admin1: Path) -> tuple[dict, str]:
    import rasterio
    from pyproj import CRS
    from rasterio import features

    codes = {c for members in REGIONS.values() for c in members}
    shapes, ne_version = read_admin1(admin1, codes)
    with rasterio.open(spam) as src:
        if src.crs is None or not CRS.from_user_input(src.crs).is_geographic:
            raise SystemExit(f"{spam}: expected a geographic (lon/lat) raster, got {src.crs}")
        t = src.transform
        if t.b != 0 or t.d != 0 or t.a <= 0 or t.e >= 0:
            raise SystemExit(f"{spam}: expected a north-up raster")
        data = src.read(1, masked=True).astype(np.float64).filled(0.0)
        height, width = data.shape
    data = np.where(np.isfinite(data) & (data > 0), data, 0.0)
    lon_edges = t.c + t.a * np.arange(width + 1)
    lat_edges = t.f + t.e * np.arange(height + 1)

    masks = {
        code: features.rasterize([(shapes[code], 1)], out_shape=data.shape, transform=t, fill=0, dtype="uint8").astype(bool)
        for code in codes
    }
    regions = {}
    for region, members in REGIONS.items():
        mask = np.zeros(data.shape, dtype=bool)
        for code in members:
            mask |= masks[code]
        area = np.where(mask, data, 0.0)
        if area.sum() <= 0.0:
            raise SystemExit(f"{region}: no soybean area inside the admin-1 polygons")
        regions[region] = (area, lon_edges, lat_edges)
        print(f"{region}: {area.sum():.6g} ha on the SPAM grid")
    return regions, ne_version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--spam", type=Path, required=True, help="SPAM 2020 v2r2 SOYB_A GeoTIFF")
    parser.add_argument("--admin1", type=Path, required=True, help="Natural Earth 10 m admin-1 zip")
    parser.add_argument("--out-dir", type=Path, default=Path("data/indicators"))
    parser.add_argument("--grids", default="0p25,0p1,t126")
    parser.add_argument("--weights-version", default="1", help="revision of the weights; bump it when they change")
    args = parser.parse_args()

    regions, ne_version = spam_regions(args.spam, args.admin1)
    version = f"{args.weights_version}+spam2020v2r2+ne{ne_version}"
    target = args.out_dir / "weights"
    target.mkdir(parents=True, exist_ok=True)
    for grid in args.grids.split(","):
        path = target / f"{grid}.zarr"
        write_grid(grid, regions, version, path)
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        print(f"wrote {path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Read a run's column store (`columns.zarr`): a cell's whole profile over the
whole axis in one chunk per quantity, for model soundings, time-height
sections and vertical cross-sections.

Library and CLI. Needs numpy and requests, plus a Zstandard decoder: the
standard library's `compression.zstd` (Python 3.14+), or `zstandard`, or
`numcodecs` (installed with zarr). Its blocks carry the `xue.delta` codec,
which this module decodes itself; xarray opens the store only after the codec
is registered with zarr-python (references/stac-access.md).

CLI:
  xue_columns.py info [gfs]                                 arrays, levels, axis, size of the live column store
  xue_columns.py profile 35.7 139.7 --time 2026-11-26T12Z   one model sounding: levels + indices (PW, K, LCL, 0–6 km shear …)
  xue_columns.py timeheight 35.7 139.7 [--every 6] [--hours 120]   the point's column through the run, one row per step
  xue_columns.py section 30 120 45 145 --time 2026-11-26T12Z [--npz out.npz]   cross-section along the great circle A→B

Which sources ship one: `has_columns(source)` (the live Item has a `columns`
asset); today `gfs`: 21 isobaric levels 1000–100 hPa of tmp, rh, hgt, ugrd,
vgrd and the surface arrays tmp2m, dpt2m, prmsl, orog, ugrd10m, vgrd10m, cape,
cin. A surface quantity a source does not ship reads as NaN. The store is the map
stores' codes cut differently, so a value is the one the map shows for the
cell (same codebook, same rounding: hgt to 10 m, tmp to 0.5 °C, rh to 1 %).

Cost: one cell's column is one block per array, 13 range reads of 50–200 kB;
the 8 × 8 cells of a block come with it. A section reads every block its line
and the line's bilinear neighbours cross — a 3000 km line on GFS about 20–40
blocks per array, 10–40 MB in all, 10–40 s.

Library:
  cs = ColumnStore.live("gfs")
  col = cs.column(35.7, 139.7)          # dict of arrays over (time, level) / (time,), see `column`
  prof = profile_at(col, "2026-11-26T12Z")
  sec = cs.section((30, 120), (45, 145), times=["2026-11-26T12Z"])
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import xue_stac as X  # noqa: E402

KAPPA = 0.2857
KELVIN = 273.15
G = 9.80665
EARTH_RADIUS_KM = 6371.0
LEVELLED = ("tmp", "rh", "hgt", "ugrd", "vgrd")
SURFACE = ("tmp2m", "dpt2m", "prmsl", "orog", "ugrd10m", "vgrd10m", "cape", "cin")
#: A level this close above the ground (hPa) is the ground: the 2 m point takes its place.
GROUND_CLEARANCE_HPA = 1.0
MAX_SECTION_POINTS = 512
MAX_SECTION_BLOCKS = 160


# --- codecs ----------------------------------------------------------------------

def _zstd_decompress():
    try:
        from compression import zstd  # Python 3.14+
        return zstd.decompress
    except ImportError:
        pass
    try:
        import zstandard
        d = zstandard.ZstdDecompressor()
        return d.decompress
    except ImportError:
        pass
    try:
        from numcodecs import Zstd
        return Zstd().decode
    except ImportError:
        raise SystemExit("need a Zstandard decoder: Python 3.14+, or `pip install zstandard` (or numcodecs)")


_CRC32C = []


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli), table-driven; the shard index's checksum."""
    if not _CRC32C:
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ 0x82F63B78 if c & 1 else c >> 1
            _CRC32C.append(c)
    c = 0xFFFFFFFF
    t = _CRC32C
    for b in data:
        c = t[(c ^ b) & 0xFF] ^ (c >> 8)
    return c ^ 0xFFFFFFFF


# --- thermodynamics (Bolton 1980, as the viewer's sounding code) -----------------

def saturation(t_c):
    """Saturation vapour pressure over water, hPa."""
    return 6.112 * np.exp(17.67 * t_c / (t_c + 243.5))


def dewpoint_from_rh(t_c, rh):
    rh = np.clip(rh, 1.0, 100.0)
    e = rh / 100.0 * saturation(t_c)
    x = np.log(e / 6.112)
    return 243.5 * x / (17.67 - x)


def rh_from_dewpoint(t_c, td_c):
    return np.where(td_c >= t_c, 100.0, 100.0 * saturation(td_c) / saturation(t_c))


def theta(t_c, p):
    return (t_c + KELVIN) * (1000.0 / p) ** KAPPA


def theta_e(t_c, td_c, p):
    """Equivalent potential temperature, K (Bolton eq. 43 with eq. 15)."""
    T = t_c + KELVIN
    D = np.minimum(td_c, t_c) + KELVIN
    e = saturation(D - KELVIN)
    r = 0.622 * e / (p - e)
    tl = 1.0 / (1.0 / (D - 56.0) + np.log(T / D) / 800.0) + 56.0
    return T * (1000.0 / p) ** (0.2854 * (1 - 0.28 * r)) * np.exp((3376.0 / tl - 2.54) * r * (1 + 0.81 * r))


def mixing_ratio(td_c, p):
    e = saturation(td_c)
    return 0.622 * e / (p - e)


def surface_pressure(p_levels, z_levels, prmsl, orog):
    """The pressure at the terrain height, hPa: ln p linear in height between
    the neighbouring (z, p) pairs of the levels' heights and (0, MSLP), as the
    viewer's `surfacePressure` does. Works on 1-D level arrays."""
    if orog is None or not np.isfinite(orog):
        return np.nan
    zs = [0.0] if np.isfinite(prmsl) and prmsl > 0 else []
    lp = [math.log(prmsl)] if zs else []
    for p, z in zip(p_levels, z_levels):
        if np.isfinite(z) and p > 0:
            zs.append(float(z)); lp.append(math.log(p))
    if len(zs) < 2:
        return np.nan
    order = np.argsort(zs, kind="stable")
    zs = np.asarray(zs)[order]; lp = np.asarray(lp)[order]
    upper = int(np.searchsorted(zs, orog, side="left"))
    upper = min(max(upper, 1), len(zs) - 1)
    z0, z1 = zs[upper - 1], zs[upper]
    if z1 == z0:
        return float(math.exp(lp[upper - 1]))
    w = (orog - z0) / (z1 - z0)
    return float(math.exp(lp[upper - 1] + w * (lp[upper] - lp[upper - 1])))


def wind_from(u, v):
    """(direction the wind comes from, °; speed m/s)."""
    return (np.degrees(np.arctan2(-u, -v)) % 360.0), np.hypot(u, v)


# --- the store ---------------------------------------------------------------------

def has_columns(source: str) -> bool:
    return "columns" in X.live_item(source)["assets"]


class ColumnStore:
    """One run's column store. `url` is the `columns.zarr` directory."""

    def __init__(self, url: str, run: str | None = None):
        self.url = url.rstrip("/") + "/"
        self.run = run
        doc = X.get_json(self.url + "zarr.json")
        attrs = doc["attributes"]
        if "xue_columns" not in attrs:
            raise ValueError(f"{url} is not a column store (no xue_columns block)")
        self.block = attrs["xue_columns"]
        if self.block.get("version") != 1:
            raise ValueError(f"column store version {self.block.get('version')} is not implemented")
        self.meta = doc["consolidated_metadata"]["metadata"]
        self.grid = self.block["grid"]
        self.arrays = {a["id"]: a for a in self.block["arrays"]}
        self.run_time = np.datetime64(self.block["runTime"].replace("Z", ""), "s")
        self._index: dict[tuple[str, int], np.ndarray] = {}
        self._blocks: dict[tuple[str, int, int], np.ndarray] = {}
        self._decompress = _zstd_decompress()
        self._lat = self._lon = None

    @classmethod
    def live(cls, source: str = "gfs") -> "ColumnStore":
        item = X.live_item(source)
        if "columns" not in item["assets"]:
            raise SystemExit(f"{source} ({item['id']}) has no column store; only sources listed by `info` ship one")
        return cls(X.asset_url(item, "columns"), item["id"])

    # geometry
    def times(self, array: str | None = None) -> np.ndarray:
        t = self.arrays[array].get("time", self.block["time"]) if array else self.block["time"]
        if "frameOffsets" in t:
            off = np.asarray(t["frameOffsets"], dtype=np.int64)
        else:
            off = t["firstFrameOffset"] + t.get("frameStep", 1) * np.arange(t["frameCount"], dtype=np.int64)
        return self.run_time + (off * t["unitSeconds"]).astype("timedelta64[s]")

    def levels(self, array: str = "tmp") -> np.ndarray:
        return np.asarray(self.arrays[array]["levels"], dtype=float)

    def _coords(self):
        if self._lat is None:
            g = self.grid
            if "gaussian" in g:
                import zarr  # the coordinate arrays are plain Zarr
                grp = zarr.open_group(self.url, mode="r", use_consolidated=False)
                self._lat = np.asarray(grp["latitude"][:], dtype=float)
            else:
                self._lat = g["firstLatitude"] + g["latitudeStep"] * np.arange(g["height"])
            self._lon = g["firstLongitude"] + g["longitudeStep"] * np.arange(g["width"])
        return self._lat, self._lon

    def fractional_cell(self, lat: float, lon: float) -> tuple[float, float]:
        """(row, column) as floats, for bilinear reads; column wraps on a global grid."""
        g = self.grid
        lats, _ = self._coords()
        if "gaussian" in g:
            row = float(np.interp(lat, lats[::-1], np.arange(len(lats))[::-1]))
        else:
            row = (lat - g["firstLatitude"]) / g["latitudeStep"]
        col = (lon - g["firstLongitude"]) / g["longitudeStep"]
        if g.get("wrapLongitude"):
            col %= g["width"]
        return row, col

    def cell(self, lat: float, lon: float) -> tuple[int, int]:
        r, c = self.fractional_cell(lat, lon)
        g = self.grid
        r = int(min(max(round(r), 0), g["height"] - 1))
        c = int(round(c)) % g["width"] if g.get("wrapLongitude") else int(min(max(round(c), 0), g["width"] - 1))
        return r, c

    def cell_latlon(self, row: int, col: int) -> tuple[float, float]:
        lats, lons = self._coords()
        lon = float(lons[col])
        return float(lats[row]), (lon - 360 if lon > 180 else lon)

    def _shape(self, array: str):
        doc = self.meta[array]
        sh = doc["codecs"][0]["configuration"]
        outer = doc["chunk_grid"]["configuration"]["chunk_shape"]
        inner = sh["chunk_shape"]
        return doc, sh, outer, inner

    def _block_of(self, array: str, row: int, col: int) -> tuple[int, int, int]:
        _, _, outer, inner = self._shape(array)
        bh, bw = inner[-2], inner[-1]
        s = row // outer[-2]
        entry = ((row - s * outer[-2]) // bh) * (outer[-1] // bw) + col // bw
        return s, entry, (row % bh) * bw + (col % bw)

    def _shard_url(self, array: str, s: int) -> str:
        levelled = len(self.meta[array]["shape"]) == 4
        return f"{self.url}{array}/c/0/{'0/' if levelled else ''}{s}/0"

    def _shard_index(self, array: str, s: int) -> np.ndarray:
        key = (array, s)
        if key not in self._index:
            doc, sh, outer, inner = self._shape(array)
            n = (outer[-2] // inner[-2]) * (outer[-1] // inner[-1])
            size = 16 * n + 4
            url = self._shard_url(array, s)
            if sh.get("index_location", "end") == "start":
                raw = X._session.get(url, headers={"Range": f"bytes=0-{size - 1}"}, timeout=60)
            else:
                raw = X._session.get(url, headers={"Range": f"bytes=-{size}"}, timeout=60)
            raw.raise_for_status()
            data = raw.content
            if raw.status_code == 200:  # a proxy that ignored Range
                data = data[:size] if sh.get("index_location") == "start" else data[-size:]
            if crc32c(data[:-4]) != int.from_bytes(data[-4:], "little"):
                raise ValueError(f"shard index CRC-32C mismatch: {url}")
            self._index[key] = np.frombuffer(data[:-4], dtype="<u8").reshape(n, 2)
        return self._index[key]

    def _decode(self, array: str, payload: bytes | None) -> np.ndarray:
        doc, sh, _, inner = self._shape(array)
        if payload is None:
            return np.full(inner, doc["fill_value"], dtype=np.uint8)
        cube = np.frombuffer(self._decompress(payload), dtype=np.uint8).reshape(inner)
        for codec in sh["codecs"]:
            if codec["name"] == "xue.delta":
                cube = np.cumsum(cube, axis=codec["configuration"]["axis"], dtype=np.uint8)
        return cube

    def fetch_blocks(self, wanted: dict[str, set[tuple[int, int]]], workers: int = 8):
        """Fetch and decode every (shard, entry) block of each array not yet
        cached. Blocks of one shard that sit back to back are one request."""
        jobs = []
        for array, blocks in wanted.items():
            by_shard: dict[int, list[int]] = {}
            for s, e in blocks:
                if (array, s, e) not in self._blocks:
                    by_shard.setdefault(s, []).append(e)
            for s, entries in by_shard.items():
                idx = self._shard_index(array, s)
                runs, cur = [], []
                for e in sorted(set(entries)):
                    off, nb = idx[e]
                    if off == 2**64 - 1:
                        self._blocks[(array, s, e)] = self._decode(array, None)
                        continue
                    if cur and int(off) == int(idx[cur[-1]][0] + idx[cur[-1]][1]) and int(off) - int(idx[cur[0]][0]) < 8 << 20:
                        cur.append(e)
                    else:
                        if cur:
                            runs.append(cur)
                        cur = [e]
                if cur:
                    runs.append(cur)
                jobs += [(array, s, run, idx) for run in runs]

        def get(job):
            array, s, run, idx = job
            start = int(idx[run[0]][0]); end = int(idx[run[-1]][0] + idx[run[-1]][1])
            r = X._session.get(self._shard_url(array, s), headers={"Range": f"bytes={start}-{end - 1}"}, timeout=120)
            r.raise_for_status()
            data = r.content if r.status_code == 206 else r.content[start:end]
            out = {}
            for e in run:
                off, nb = int(idx[e][0]) - start, int(idx[e][1])
                out[(array, s, e)] = self._decode(array, data[off:off + nb])
            return out

        with ThreadPoolExecutor(workers) as ex:
            for got in ex.map(get, jobs):
                self._blocks.update(got)

    def _values(self, array: str, codes: np.ndarray) -> np.ndarray:
        """Codes (…, frame[, level]) → physical values with NaN at nodata, each
        level by its own member's codebook."""
        levelled = "levels" in self.arrays[array]
        out = np.empty(codes.shape, dtype=np.float32)
        for i, m in enumerate(self.arrays[array]["members"]):
            q = m["variable"]["quantization"]
            if q["type"] != "linear":
                raise ValueError(f"{array}: codebook {q['type']} is not handled")
            c = codes[..., i] if levelled else codes
            v = q["offset"] + q["scale"] * c.astype(np.float32)
            v[c == q["nodataCode"]] = np.nan
            if levelled:
                out[..., i] = v
            else:
                out[...] = v
        return out

    def _align(self, array: str, values: np.ndarray) -> np.ndarray:
        """(cell, own frame[, level]) → (cell, store frame[, level]): an array on
        an axis of its own (a field with no analysis frame) is NaN at the
        store's frames it does not have. `orog` (one frame) is left as it is."""
        if "time" not in self.arrays[array] or array == "orog":
            return values
        main, own = self.times(), self.times(array)
        out = np.full((values.shape[0], len(main), *values.shape[2:]), np.nan, dtype=np.float32)
        where = {t: i for i, t in enumerate(own)}
        for i, t in enumerate(main):
            if t in where:
                out[:, i] = values[:, where[t]]
        return out

    def cells(self, cells: list[tuple[int, int]], arrays=None) -> dict[str, np.ndarray]:
        """Values of several cells: array id → (cell, frame[, level]) on the store's axis."""
        arrays = [a for a in (arrays or self.arrays) if a in self.arrays]
        loc = {a: [self._block_of(a, r, c) for r, c in cells] for a in arrays}
        self.fetch_blocks({a: {(s, e) for s, e, _ in loc[a]} for a in arrays})
        out = {}
        for a in arrays:
            picks = []
            for s, e, k in loc[a]:
                cube = self._blocks[(a, s, e)]  # (frame[, level], bh, bw)
                picks.append(cube.reshape(*cube.shape[:-2], -1)[..., k])
            out[a] = self._align(a, self._values(a, np.stack(picks)))
            if "levels" in self.arrays[a] and self.arrays[a]["levels"] != self.arrays["tmp"]["levels"]:
                raise ValueError(f"{a} is on levels other than tmp's; this reader assumes one level set")
        for a in SURFACE:  # a quantity the source does not ship reads as NaN
            if a not in out and a != "orog" and (arrays is None):
                out[a] = np.full((len(cells), len(self.times())), np.nan, dtype=np.float32)
        return out

    def column(self, lat: float, lon: float) -> dict:
        """The nearest cell's column over the whole axis: `time` (datetime64),
        `p` (hPa), levelled `tmp` °C / `rh` % / `hgt` m / `ugrd` `vgrd` m/s as
        (time, level), the surface arrays as (time,) (`orog` a scalar), and
        `psfc` (time,) — the pressure at the model's terrain, hPa."""
        r, c = self.cell(lat, lon)
        vals = self.cells([(r, c)])
        col = {k: v[0] for k, v in vals.items()}
        col["orog"] = float(col["orog"].ravel()[0]) if "orog" in col else np.nan
        col["time"] = self.times()
        col["p"] = self.levels()
        col["psfc"] = np.array([surface_pressure(col["p"], col["hgt"][t], col["prmsl"][t], col["orog"]) for t in range(len(col["time"]))])
        col["cell"] = self.cell_latlon(r, c)
        return col

    # section
    def section(self, a: tuple[float, float], b: tuple[float, float], times=None, points: int | None = None) -> dict:
        """A vertical cross-section along the great circle a→b ((lat, lon) each),
        sampled about half a cell apart (at most 512 points), each point read
        bilinearly from its four cells, at `times` (valid times, default every
        frame). Returns `dist` km, `lat` `lon` per point, `bearing` (rad, the line's
        direction at the point), `p` hPa, `time`, `psfc` (time, point), and
        (time, level, point) `tmp` `rh` `hgt` `ugrd` `vgrd` plus the derived
        `td` `theta` `thetae` `speed` `across` (positive to the right of a→b,
        i.e. out of the page with a on the left) and `along`; the surface arrays
        as (time, point). Levels under the ground are NaN."""
        length = _km(a[0], a[1], b[0], b[1])
        g = self.grid
        step_km = min(abs(g["latitudeStep"]), g["longitudeStep"]) * 111.2 / 2
        n = points or int(min(MAX_SECTION_POINTS, max(16, math.ceil(length / step_km) + 1)))
        lat, lon, bearing = great_circle(a, b, n)
        frac = [self.fractional_cell(la, lo) for la, lo in zip(lat, lon)]
        need, weights = set(), []
        for r, c in frac:
            r0 = int(math.floor(r)); c0 = int(math.floor(c)); fr = r - r0; fc = c - c0
            four = []
            for dr, dc, w in ((0, 0, (1 - fr) * (1 - fc)), (0, 1, (1 - fr) * fc), (1, 0, fr * (1 - fc)), (1, 1, fr * fc)):
                rr = min(max(r0 + dr, 0), g["height"] - 1)
                cc = (c0 + dc) % g["width"] if g.get("wrapLongitude") else min(max(c0 + dc, 0), g["width"] - 1)
                if w > 1e-9:
                    need.add((rr, cc)); four.append(((rr, cc), w))
            weights.append(four)
        blocks = {self._block_of("tmp", r, c)[:2] for r, c in need}
        if len(blocks) > MAX_SECTION_BLOCKS:
            raise SystemExit(f"the line crosses {len(blocks)} blocks (> {MAX_SECTION_BLOCKS}); shorten it")
        cells = sorted(need)
        pos = {cell: i for i, cell in enumerate(cells)}
        vals = self.cells(cells)
        all_t = self.times()
        ti = np.arange(len(all_t)) if times is None else np.array([_nearest(all_t, t) for t in times])
        out = {"time": all_t[ti], "p": self.levels(), "lat": lat, "lon": lon, "bearing": bearing,
               "dist": np.linspace(0, length, n), "a": np.array(a), "b": np.array(b)}
        W = np.zeros((n, len(cells)), dtype=np.float64)
        for i, four in enumerate(weights):
            tot = sum(w for _, w in four)
            for cell, w in four:
                W[i, pos[cell]] = w / tot
        def interpolate(v):  # (cell, …) → (…, point); a nodata cell drops out of the weights
            ok = np.isfinite(v)
            num = np.tensordot(W, np.where(ok, v, 0.0), axes=(1, 0))
            den = np.tensordot(W, ok.astype(float), axes=(1, 0))
            return np.moveaxis(np.where(den > 0.5, num / np.maximum(den, 1e-9), np.nan), 0, -1)
        for k, v in vals.items():
            out[k] = interpolate(v.reshape(len(cells), -1)[:, 0]) if k == "orog" else interpolate(v[:, ti])
        p = out["p"]
        nt = len(ti)
        out["psfc"] = np.array([[surface_pressure(p, out["hgt"][t, :, i], out["prmsl"][t, i], out["orog"][i]) for i in range(n)] for t in range(nt)])
        P = p[None, :, None]
        below = P > (out["psfc"][:, None, :] - GROUND_CLEARANCE_HPA)
        for k in LEVELLED:
            out[k] = np.where(below, np.nan, out[k])
        out["td"] = dewpoint_from_rh(out["tmp"], out["rh"])
        out["theta"] = theta(out["tmp"], P)
        out["thetae"] = theta_e(out["tmp"], out["td"], P)
        out["speed"] = np.hypot(out["ugrd"], out["vgrd"])
        br = bearing[None, None, :]
        out["across"] = out["ugrd"] * np.cos(br) - out["vgrd"] * np.sin(br)
        out["along"] = out["ugrd"] * np.sin(br) + out["vgrd"] * np.cos(br)
        return out


# --- geometry ------------------------------------------------------------------

def _km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(d)))


def great_circle(a, b, n: int):
    """n points evenly along the great circle a→b; (lat, lon, bearing rad clockwise from north)."""
    def vec(lat, lon):
        p, l = math.radians(lat), math.radians(lon)
        return np.array([math.cos(p) * math.cos(l), math.cos(p) * math.sin(l), math.sin(p)])
    va, vb = vec(*a), vec(*b)
    omega = math.acos(float(np.clip(va @ vb, -1, 1)))
    f = np.linspace(0, 1, n)
    pts = (np.sin((1 - f) * omega)[:, None] * va + np.sin(f * omega)[:, None] * vb) / math.sin(omega)
    lat = np.degrees(np.arcsin(np.clip(pts[:, 2], -1, 1)))
    lon = np.degrees(np.arctan2(pts[:, 1], pts[:, 0]))
    normal = np.cross(va, vb)
    along = np.cross(normal, pts)
    lam, phi = np.radians(lon), np.radians(lat)
    east = -np.sin(lam) * along[:, 0] + np.cos(lam) * along[:, 1]
    north = -np.sin(phi) * np.cos(lam) * along[:, 0] - np.sin(phi) * np.sin(lam) * along[:, 1] + np.cos(phi) * along[:, 2]
    return lat, lon, np.arctan2(east, north) % (2 * math.pi)


def _nearest(times: np.ndarray, t) -> int:
    t = X.to_time(t)
    i = int(np.argmin(np.abs(times - t)))
    if abs(times[i] - t) > np.timedelta64(3, "h"):
        raise ValueError(f"{t} is outside the axis {times[0]} .. {times[-1]}")
    return i


# --- diagnostics on a column ----------------------------------------------------

def profile_at(col: dict, t) -> dict:
    """One frame of a column as a model sounding standing on the model's ground:
    levels above the ground (p, z, t, td, rh, wd, ws, theta, thetae) with the 2 m /
    10 m point first at `psfc`, and the indices: PW mm, freezing level m AMSL,
    LCL m AGL, 850–500 lapse K/km, K index, total totals, 0–6 km bulk shear m/s,
    and the model's own CAPE / CIN."""
    i = _nearest(col["time"], t)
    p = col["p"]; psfc = col["psfc"][i]
    keep = p < psfc - GROUND_CLEARANCE_HPA if np.isfinite(psfc) else np.ones_like(p, bool)
    tt, rh, z = col["tmp"][i][keep], col["rh"][i][keep], col["hgt"][i][keep]
    u, v = col["ugrd"][i][keep], col["vgrd"][i][keep]
    pp = p[keep]
    t2, td2 = col["tmp2m"][i], col["dpt2m"][i]
    u10, v10 = col["ugrd10m"][i], col["vgrd10m"][i]
    ground = np.isfinite(psfc) and np.isfinite(t2)
    if ground:
        pp = np.r_[psfc, pp]; tt = np.r_[t2, tt]; z = np.r_[col["orog"], z]; u = np.r_[u10, u]; v = np.r_[v10, v]
        td = np.r_[min(td2, t2), dewpoint_from_rh(tt[1:], rh)]; rh = np.r_[rh_from_dewpoint(t2, td2), rh]
    else:
        td = dewpoint_from_rh(tt, rh)
    wd, ws = wind_from(u, v)
    out = {"time": str(col["time"][i])[:16] + "Z", "cell": col.get("cell"), "psfc": psfc, "orog": col["orog"],
           "p": pp, "z": z, "t": tt, "td": td, "rh": rh, "wd": wd, "ws": ws, "u": u, "v": v,
           "theta": theta(tt, pp), "thetae": theta_e(tt, td, pp),
           "cape": float(col["cape"][i]), "cin": float(col["cin"][i]), "prmsl": float(col["prmsl"][i])}
    q = mixing_ratio(td, pp); q = q / (1 + q)
    out["pw"] = float(np.trapezoid(q, -pp * 100) / G) if len(pp) > 1 else np.nan
    below0 = np.where((tt[:-1] > 0) & (tt[1:] <= 0))[0]
    if tt[0] <= 0:
        out["freezing"] = float(z[0])
    elif len(below0):
        k = below0[0]; out["freezing"] = float(z[k] + (z[k + 1] - z[k]) * tt[k] / (tt[k] - tt[k + 1]))
    else:
        out["freezing"] = np.nan
    out["lcl"] = float(125 * (t2 - td2)) if ground else np.nan

    def at(P, a):
        j = np.where(np.isclose(pp, P))[0]
        return float(a[j[0]]) if len(j) and (not ground or j[0] > 0) else np.nan
    t850, t700, t500 = at(850, tt), at(700, tt), at(500, tt)
    d850, d700 = at(850, td), at(700, td)
    z850, z500 = at(850, z), at(500, z)
    out["lapse"] = (t850 - t500) / (z500 - z850) * 1000 if np.isfinite(z500 - z850) else np.nan
    out["k"] = t850 - t500 + d850 - (t700 - d700)
    out["tt"] = t850 + d850 - 2 * t500
    top = z[0] + 6000
    if z[-1] >= top:
        out["shear06"] = float(np.hypot(np.interp(top, z, u) - u[0], np.interp(top, z, v) - v[0]))
    else:
        out["shear06"] = np.nan
    return out


def profile_table(prof: dict) -> str:
    lat, lon = prof["cell"] or (np.nan, np.nan)
    lines = [f"model sounding {prof['time']}, cell {lat:.2f},{lon:.2f}, terrain {prof['orog']:.0f} m, "
             f"surface {prof['psfc']:.0f} hPa, MSLP {prof['prmsl']:.0f} hPa",
             f"{'p hPa':>6} {'z m':>6} {'T °C':>6} {'Td °C':>6} {'RH %':>5} {'wind':>9} {'θ K':>6} {'θe K':>6}"]
    for k in range(len(prof["p"])):
        lines.append(f"{prof['p'][k]:6.0f} {prof['z'][k]:6.0f} {prof['t'][k]:6.1f} {prof['td'][k]:6.1f} {prof['rh'][k]:5.0f} "
                     f"{prof['wd'][k]:4.0f}/{prof['ws'][k]:4.1f} {prof['theta'][k]:6.1f} {prof['thetae'][k]:6.1f}")
    f = lambda x, s="{:.0f}": "-" if x is None or not np.isfinite(x) else s.format(x)  # noqa: E731
    lines.append(f"PW {f(prof['pw'], '{:.1f}')} mm · freezing level {f(prof['freezing'])} m · LCL {f(prof['lcl'])} m AGL · "
                 f"850–500 lapse {f(prof['lapse'], '{:.1f}')} K/km · K {f(prof['k'])} · TT {f(prof['tt'])} · "
                 f"0–6 km shear {f(prof['shear06'], '{:.1f}')} m/s · CAPE {f(prof['cape'])} J/kg · CIN {f(prof['cin'])} J/kg")
    return "\n".join(lines)


def timeheight_rows(col: dict, every_hours: int = 6, hours: int | None = None) -> list[dict]:
    t0 = col["time"][0]
    rows = []
    for t in col["time"]:
        h = int((t - t0) / np.timedelta64(1, "h"))
        if h % every_hours or (hours is not None and h > hours):
            continue
        pr = profile_at(col, t)
        def lev(P, key):
            j = np.where(np.isclose(pr["p"], P))[0]
            return float(pr[key][j[-1]]) if len(j) and not (j[-1] == 0 and np.isfinite(pr["psfc"])) else np.nan
        rows.append({"time": pr["time"], "psfc": pr["psfc"], "t850": lev(850, "t"), "rh850": lev(850, "rh"), "rh700": lev(700, "rh"),
                     "rh500": lev(500, "rh"), "w850": (lev(850, "wd"), lev(850, "ws")), "w500": (lev(500, "wd"), lev(500, "ws")),
                     "w250": (lev(250, "wd"), lev(250, "ws")), "freezing": pr["freezing"], "pw": pr["pw"], "k": pr["k"],
                     "shear06": pr["shear06"], "cape": pr["cape"]})
    return rows


def timeheight_table(rows: list[dict]) -> str:
    f = lambda x, s="{:.0f}": "-" if not np.isfinite(x) else s.format(x)  # noqa: E731
    w = lambda d: "-" if not np.isfinite(d[1]) else f"{d[0]:03.0f}/{d[1]:.0f}"  # noqa: E731
    out = [f"{'valid':16} {'psfc':>5} {'T850':>5} {'RH850':>5} {'RH700':>5} {'RH500':>5} {'w850':>8} {'w500':>8} {'w250':>8} {'0°C m':>6} {'PW':>5} {'K':>4} {'shr6':>4} {'CAPE':>5}"]
    for r in rows:
        out.append(f"{r['time']:16} {f(r['psfc']):>5} {f(r['t850'], '{:.1f}'):>5} {f(r['rh850']):>5} {f(r['rh700']):>5} {f(r['rh500']):>5} "
                   f"{w(r['w850']):>8} {w(r['w500']):>8} {w(r['w250']):>8} {f(r['freezing']):>6} {f(r['pw'], '{:.1f}'):>5} {f(r['k']):>4} {f(r['shear06']):>4} {f(r['cape']):>5}")
    return "\n".join(out)


def baseline_gradient(a: np.ndarray, dist: np.ndarray, baseline_km: float = 150.0) -> np.ndarray:
    """d(a)/d(dist) as a centred difference over about `baseline_km`: the points
    are ~14 km apart on GFS, where one 0.5 °C codebook step between neighbours
    would read as 3–4 K/100 km. NaN where either end is under the ground."""
    step = float(dist[1] - dist[0]) if len(dist) > 1 else 1.0
    h = max(1, int(round(baseline_km / 2 / step)))
    g = np.full(a.shape, np.nan)
    if len(a) > 2 * h:
        g[h:-h] = (a[2 * h:] - a[:-2 * h]) / (dist[2 * h:] - dist[:-2 * h])
    return g


def section_summary(sec: dict, ti: int = 0) -> str:
    """What a forecaster reads off a section, in numbers: the jet core (strongest
    across-line wind), the frontal zone (strongest along-line θ and θe gradient
    at 850 and 700 hPa), the deepest moist column (RH ≥ 90 %) and the terrain."""
    p, d = sec["p"], sec["dist"]
    lines = [f"section {sec['a'][0]:.2f},{sec['a'][1]:.2f} → {sec['b'][0]:.2f},{sec['b'][1]:.2f}: {d[-1]:.0f} km, {len(d)} points, valid {str(sec['time'][ti])[:16]}Z"]
    sp = sec["across"][ti]
    for sign, name in ((1, "out of page (right of A→B)"), (-1, "into page (left of A→B)")):
        v = sign * sp
        if np.isfinite(v).any() and np.nanmax(v) > 0:
            k, i = np.unravel_index(np.nanargmax(v), v.shape)
            lines.append(f"  strongest across-line wind {name}: {np.nanmax(v):.0f} m/s at {p[k]:.0f} hPa, {d[i]:.0f} km ({sec['lat'][i]:.1f},{sec['lon'][i]:.1f})")
    s = sec["speed"][ti]
    if np.isfinite(s).any():
        k, i = np.unravel_index(np.nanargmax(s), s.shape)
        lines.append(f"  strongest total wind: {np.nanmax(s):.0f} m/s at {p[k]:.0f} hPa, {d[i]:.0f} km")
    for P in (850, 700):
        if P not in p:
            continue
        k = int(np.where(p == P)[0][0])
        for key in ("theta", "thetae"):
            a = sec[key][ti, k]
            grad = baseline_gradient(a, d) * 100  # K per 100 km
            if np.isfinite(grad).any():
                i = int(np.nanargmax(np.abs(grad)))
                lines.append(f"  {P} hPa {key}: {np.nanmin(a):.0f}–{np.nanmax(a):.0f} K, strongest gradient {grad[i]:+.1f} K/100 km at {d[i]:.0f} km ({sec['lat'][i]:.1f},{sec['lon'][i]:.1f})")
    rh = sec["rh"][ti]
    depth = np.zeros(len(d)); base = np.full(len(d), np.nan); top = np.full(len(d), np.nan)
    for i in range(len(d)):
        run = []  # contiguous levels with RH ≥ 90 %, bottom up
        for k in range(len(p)):
            if np.isfinite(rh[k, i]) and rh[k, i] >= 90:
                run.append(k)
                if k == len(p) - 1 or not (np.isfinite(rh[k + 1, i]) and rh[k + 1, i] >= 90):
                    if len(run) > 1 and p[run[0]] - p[run[-1]] > depth[i]:
                        depth[i], base[i], top[i] = p[run[0]] - p[run[-1]], p[run[0]], p[run[-1]]
                    run = []
    if depth.max() > 0:
        i = int(np.argmax(depth))
        lines.append(f"  deepest contiguous RH ≥ 90 % layer: {base[i]:.0f}–{top[i]:.0f} hPa at {d[i]:.0f} km ({sec['lat'][i]:.1f},{sec['lon'][i]:.1f}); "
                     f"a saturated layer ≥ 100 hPa deep in {np.mean(depth >= 100) * 100:.0f} % of the columns")
    ps = sec["psfc"][ti]
    if np.isfinite(ps).any():
        i = int(np.nanargmin(ps))
        lines.append(f"  highest ground: {sec['orog'][i]:.0f} m ({ps[i]:.0f} hPa) at {d[i]:.0f} km")
    return "\n".join(lines)


def _flat(prefix: str, d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if k == "time":
            v = np.asarray(v).astype("datetime64[s]").astype(np.int64)
        out[prefix + k] = np.asarray(v)
    return out


def _unflat(npz, prefix: str) -> dict:
    out = {}
    for k in npz.files:
        if k.startswith(prefix):
            v = npz[k]
            out[k[len(prefix):]] = v.astype("datetime64[s]") if k == prefix + "time" else (v.item() if v.ndim == 0 else v)
    if "cell" in out:
        out["cell"] = tuple(float(x) for x in out["cell"])
    return out


def save_section(path, sec: dict):
    np.savez_compressed(path, **_flat("", sec))


def save_case(path, column: dict | None, sections: list[dict]):
    """`columns.npz` of a case directory: `col:<key>` (the point's column, every
    frame) and `sec<i>:<key>` (section i at the case's times)."""
    data = _flat("col:", column) if column else {}
    for i, sec in enumerate(sections):
        data.update(_flat(f"sec{i}:", sec))
    np.savez_compressed(path, **data)


def load_case(path) -> tuple[dict | None, list[dict]]:
    npz = np.load(path)
    column = _unflat(npz, "col:") or None
    sections, i = [], 0
    while any(k.startswith(f"sec{i}:") for k in npz.files):
        sections.append(_unflat(npz, f"sec{i}:")); i += 1
    return column, sections


# --- CLI -------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info"); p.add_argument("source", nargs="?", default="gfs")
    p = sub.add_parser("profile"); p.add_argument("lat", type=float); p.add_argument("lon", type=float); p.add_argument("--time", required=True); p.add_argument("--source", default="gfs")
    p = sub.add_parser("timeheight"); p.add_argument("lat", type=float); p.add_argument("lon", type=float)
    p.add_argument("--every", type=int, default=6); p.add_argument("--hours", type=int); p.add_argument("--source", default="gfs")
    p = sub.add_parser("section"); [p.add_argument(k, type=float) for k in ("lat1", "lon1", "lat2", "lon2")]
    p.add_argument("--time", required=True, help="comma-separated valid times"); p.add_argument("--source", default="gfs"); p.add_argument("--npz")
    a = ap.parse_args(argv)
    cs = ColumnStore.live(a.source)
    if a.cmd == "info":
        t = cs.times()
        print(f"{cs.run}: {cs.url}\n  axis {str(t[0])[:16]}Z .. {str(t[-1])[:16]}Z, {len(t)} frames; grid {cs.grid['width']}x{cs.grid['height']}")
        for arr in cs.block["arrays"]:
            v = arr["members"][0]["variable"]
            lv = f"{len(arr['levels'])} levels {arr['levels'][0]}..{arr['levels'][-1]} hPa" if "levels" in arr else "surface"
            print(f"  {arr['id']:8} {lv:28} {v['unit']:6} step {v['quantization']['scale']}")
    elif a.cmd == "profile":
        print(profile_table(profile_at(cs.column(a.lat, a.lon), a.time)))
    elif a.cmd == "timeheight":
        col = cs.column(a.lat, a.lon)
        print(f"{cs.run} column at cell {col['cell'][0]:.2f},{col['cell'][1]:.2f}, terrain {col['orog']:.0f} m (wind = from°/m/s)")
        print(timeheight_table(timeheight_rows(col, a.every, a.hours)))
    else:
        times = a.time.split(",")
        sec = cs.section((a.lat1, a.lon1), (a.lat2, a.lon2), times=times)
        for i in range(len(times)):
            print(section_summary(sec, i))
        if a.npz:
            save_section(a.npz, sec); print(f"wrote {a.npz}")


if __name__ == "__main__":
    main()

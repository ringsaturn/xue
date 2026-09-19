#!/usr/bin/env python3
"""Read Xue's public STAC catalog and the data it describes.

Library and CLI in one file. Needs: numpy, xarray, zarr>=3, fsspec, aiohttp,
requests (`pip install xarray "zarr>=3" fsspec aiohttp requests`). pystac is
optional; this module walks the catalog with plain JSON so it stays light.

CLI:
  xue_stac.py sources                      list every Collection and its live Item
  xue_stac.py item gfs                     the live Item of one source (id, run, axis, bbox, variables)
  xue_stac.py point gfs prmsl 35.7 139.7   one cell's whole series
  xue_stac.py box gfs prmsl 20 60 100 160 --time 2026-09-20T00:00Z   a cropped frame's statistics
  xue_stac.py storms                       the tropical cyclone index headline
  xue_stac.py sounding 47646               one radiosonde station's newest ascent (WMO number)
  xue_stac.py airport RJTT                 one airport's newest METARs and its TAF
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from urllib.parse import urljoin

import numpy as np
import requests

ROOT = "https://dataset.ringsaturn.me/xue/"
FORECAST = ("gfs", "ecmwf", "aifs", "ifshres", "sflux", "hrrr")
OBSERVATION = ("mrms", "jma", "cma", "himawari", "goeseast", "goeswest", "meteosat")
POINT = ("sounding", "airport", "tc")

_session = requests.Session()
_session.headers["User-Agent"] = "xue-synoptic-analysis-skill/1"


def get_json(url: str, *, mutable: bool = False) -> dict:
    """Fetch a JSON document. Mutable documents (pointers, live Items) are
    fetched with caching disabled, the way the viewer polls them."""
    headers = {"Cache-Control": "no-cache"} if mutable else {}
    r = _session.get(url, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()


# --- catalog -----------------------------------------------------------------

@lru_cache(maxsize=None)
def catalog() -> dict:
    return get_json(urljoin(ROOT, "catalog.json"), mutable=True)


def source_ids() -> list[str]:
    return [l["href"].split("/")[0] for l in catalog()["links"] if l["rel"] == "child"]


def collection(source: str) -> dict:
    return get_json(urljoin(ROOT, f"{source}/collection.json"), mutable=True)


def live_item(source: str) -> dict:
    """The live Item at a stable path: its hrefs already reach into the run
    directory, so resolve every asset against `<root>/<source>/`."""
    item = get_json(urljoin(ROOT, f"{source}/item.json"), mutable=True)
    item["_base"] = urljoin(ROOT, f"{source}/")
    return item


def asset_url(item: dict, key: str) -> str:
    return urljoin(item["_base"], item["assets"][key]["href"])


def store_assets(item: dict) -> dict[str, str]:
    """Bundle id -> store URL, full tier only. Keys are the manifest's bundle
    ids (`wind10m`, not `ugrd10m`)."""
    return {k: asset_url(item, k) for k, a in item["assets"].items()
            if a.get("xue:kind") == "store" and a.get("xue:tier", "full") == "full"}


def variables(item: dict) -> dict:
    return item["properties"].get("cube:variables", {})


# --- stores ------------------------------------------------------------------

def open_store(url: str):
    """xarray Dataset of one bundle store. Every array is dequantized to its
    physical unit (float32, NaN where nodata), including the log1p codebook
    that xarray's CF decoding cannot express."""
    import xarray as xr

    ds = xr.open_zarr(url, consolidated=True)
    meta = ds.attrs["xue"]
    if isinstance(meta, str):
        meta = json.loads(meta)
    out = {}
    for var in meta["variables"]:
        da = ds[var["id"]]
        out[var["id"]] = _dequantize(da, var)
    res = xr.Dataset(out, coords=ds.coords, attrs={"xue": meta})
    return res


def _dequantize(da, var: dict):
    q = var["quantization"]
    nodata = q["nodataCode"]
    # xarray applied scale_factor/add_offset for linear codebooks and masked
    # _FillValue; for log1p it returned raw codes with nodata masked.
    if q["type"] == "linear":
        return da.astype("float32").assign_attrs(units=var["unit"], long_name=var["label"])
    if q["type"] == "log1p":
        import xarray as xr

        codes = da  # float with NaN at nodata
        span = q["maximumCode"] - 1
        lo = np.log1p(q["trace"] / q["scale"])
        hi = np.log1p(q["maximum"] / q["scale"])
        unit = (codes - 1) / span
        rate = q["scale"] * np.expm1(lo + unit * (hi - lo))
        rate = xr.where(codes == q["zeroCode"], 0.0, rate)
        return rate.astype("float32").assign_attrs(units=var["unit"], long_name=var["label"])
    raise ValueError(f"unknown codebook {q['type']}")


def open_bundle(source: str, bundle: str):
    item = live_item(source)
    return open_store(store_assets(item)[bundle]), item


def sel_box(da, south: float, north: float, west: float, east: float):
    """Crop a (time, latitude, longitude) array to a box. Latitude runs
    north-to-south in every store, so the slice is (north, south). A box that
    crosses the antimeridian is spelled with east < west on a wrapping grid
    (west=150, east=-150) and taken in two pieces; a satellite disk's
    longitudes already continue past 180."""
    lat = slice(north, south)
    lons = da.longitude.values
    if east >= west or lons.max() > 180:
        return da.sel(latitude=lat, longitude=slice(west, east if east >= west else east + 360))
    import xarray as xr

    a = da.sel(latitude=lat, longitude=slice(west, 180))
    b = da.sel(latitude=lat, longitude=slice(-180, east))
    b = b.assign_coords(longitude=b.longitude + 360)
    return xr.concat([a, b], dim="longitude")


def to_time(t):
    """ISO string (UTC, 'Z' or offset) -> numpy datetime64, for .sel(time=…)."""
    if isinstance(t, str):
        t = t.strip().replace("Z", "")
        if len(t) == 13:  # 2026-09-20T00
            t += ":00"
        return np.datetime64(t)
    return t


def sel_time(da, t):
    """The frame nearest a valid time; raises if it is outside the axis."""
    t = to_time(t)
    times = da.time.values
    if t < times[0] or t > times[-1]:
        raise ValueError(f"{t} is outside the axis {times[0]} .. {times[-1]}")
    return da.sel(time=t, method="nearest")


def sel_point(da, lat: float, lon: float):
    lons = da.longitude.values
    if lons.max() > 180 and lon < lons.min():
        lon += 360
    return da.sel(latitude=lat, longitude=lon, method="nearest")


# --- point products ----------------------------------------------------------

def point_index(product: str) -> tuple[dict, dict]:
    item = live_item(product)
    idx = get_json(asset_url(item, "index"))
    return idx, item


def _range_line(url: str, offset: int, length: int) -> dict:
    r = _session.get(url, headers={"Range": f"bytes={offset}-{offset + length - 1}"}, timeout=60)
    r.raise_for_status()
    body = r.content if r.status_code == 206 else r.content[offset:offset + length]
    return json.loads(body)


def sounding(station: str) -> dict | None:
    """One station's line by WMO number or WIGOS id; None if absent."""
    idx, item = point_index("sounding")
    url = asset_url(item, "soundings")
    for s in idx["stations"]:
        if s["id"] == station or s["wmo"] == station:
            return _range_line(url, s["offset"], s["length"])
    return None


def nearest_soundings(lat: float, lon: float, n: int = 3) -> list[dict]:
    idx, _ = point_index("sounding")
    rows = sorted(idx["stations"], key=lambda s: _km(lat, lon, s["lat"], s["lon"]))[:n]
    return [dict(r, distance_km=round(_km(lat, lon, r["lat"], r["lon"]))) for r in rows]


AIRPORT_COLUMNS = ("icao", "lat", "lon", "elev", "obsTime", "t", "td", "wd", "ws",
                   "gust", "vis", "qnh", "category", "tafPresent", "offset", "length")


def airport(icao: str) -> dict | None:
    idx, item = point_index("airport")
    url = asset_url(item, "history")
    for row in idx["stations"]:
        if row[0] == icao.upper():
            r = dict(zip(AIRPORT_COLUMNS, row))
            return _range_line(url, r["offset"], r["length"])
    return None


def nearest_airports(lat: float, lon: float, n: int = 3) -> list[dict]:
    idx, _ = point_index("airport")
    rows = [dict(zip(AIRPORT_COLUMNS, r)) for r in idx["stations"]]
    rows.sort(key=lambda r: _km(lat, lon, r["lat"], r["lon"]))
    return [dict(r, distance_km=round(_km(lat, lon, r["lat"], r["lon"]))) for r in rows[:n]]


def storms() -> tuple[list[dict], dict]:
    idx, item = point_index("tc")
    return idx["storms"], item


def storm(storm_id: str) -> dict:
    _, item = point_index("tc")
    return get_json(asset_url(item, storm_id))


def _km(lat1, lon1, lat2, lon2) -> float:
    p = np.pi / 180
    a = (np.sin((lat2 - lat1) * p / 2) ** 2
         + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * 6371 * np.arcsin(np.sqrt(a))


# --- CLI ---------------------------------------------------------------------

def _print(obj):
    print(json.dumps(obj, indent=1, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sources")
    p = sub.add_parser("item"); p.add_argument("source")
    p = sub.add_parser("point"); p.add_argument("source"); p.add_argument("bundle"); p.add_argument("lat", type=float); p.add_argument("lon", type=float)
    p = sub.add_parser("box"); p.add_argument("source"); p.add_argument("bundle")
    p.add_argument("south", type=float); p.add_argument("north", type=float); p.add_argument("west", type=float); p.add_argument("east", type=float)
    p.add_argument("--time", help="valid time (ISO); default the first frame")
    sub.add_parser("storms")
    p = sub.add_parser("storm"); p.add_argument("id")
    p = sub.add_parser("sounding"); p.add_argument("station")
    p = sub.add_parser("airport"); p.add_argument("icao")
    a = ap.parse_args(argv)

    if a.cmd == "sources":
        for sid in source_ids():
            c = collection(sid)
            live = c.get("xue:live")
            ext = c["extent"]["temporal"]["interval"][0]
            print(f"{sid:10} {c.get('title',''):45} live={live} {ext[0]} .. {ext[1]}")
    elif a.cmd == "item":
        it = live_item(a.source)
        p = it["properties"]
        _print({"id": it["id"], "bbox": it["bbox"], "datetime": p.get("datetime"),
                "start": p.get("start_datetime"), "end": p.get("end_datetime"),
                "reference": p.get("forecast:reference_datetime"), "horizon": p.get("forecast:horizon"),
                "observation": p.get("xue:observation"), "frames": p.get("xue:frameCount"),
                "dimensions": p.get("cube:dimensions"),
                "stores": sorted(store_assets(it)),
                "variables": {k: f"{v.get('description')} [{v.get('unit')}]" for k, v in variables(it).items()}})
    elif a.cmd == "point":
        ds, it = open_bundle(a.source, a.bundle)
        for name, da in ds.data_vars.items():
            s = sel_point(da, a.lat, a.lon).compute()
            print(f"# {it['id']} {name} [{da.attrs.get('units')}] at {float(s.latitude):.3f},{float(s.longitude):.3f}")
            for t, v in zip(s.time.values, s.values):
                print(f"{np.datetime_as_string(t, unit='m')}  {v:.2f}")
    elif a.cmd == "box":
        ds, it = open_bundle(a.source, a.bundle)
        for name, da in ds.data_vars.items():
            fr = sel_time(da, a.time) if a.time else da.isel(time=0)
            crop = sel_box(fr, a.south, a.north, a.west, a.east).compute()
            v = crop.values
            print(f"# {it['id']} {name} [{da.attrs.get('units')}] valid {np.datetime_as_string(crop.time.values, unit='m')} shape {v.shape}")
            print(f"min {np.nanmin(v):.2f}  p10 {np.nanpercentile(v,10):.2f}  median {np.nanmedian(v):.2f}  p90 {np.nanpercentile(v,90):.2f}  max {np.nanmax(v):.2f}  nan {np.isnan(v).mean():.1%}")
            i = np.unravel_index(np.nanargmin(v), v.shape); j = np.unravel_index(np.nanargmax(v), v.shape)
            print(f"min at {float(crop.latitude[i[0]]):.2f},{float(crop.longitude[i[1]]):.2f}   max at {float(crop.latitude[j[0]]):.2f},{float(crop.longitude[j[1]]):.2f}")
    elif a.cmd == "storms":
        rows, it = storms()
        print(f"# {it['id']}")
        for s in rows:
            pos = s.get("position") or {}
            print(f"{s['id']:20} {s['level']} {s['basin']} {str(s.get('name')):12} {pos.get('time','-'):20} {pos.get('lat','-')!s:>6} {pos.get('lon','-')!s:>7} vmax={s.get('vmax')} m/s pmin={s.get('pmin')} class={s.get('class')} agencies={s.get('agencies')} models={s.get('models')}")
    elif a.cmd == "storm":
        _print(storm(a.id))
    elif a.cmd == "sounding":
        _print(sounding(a.station))
    elif a.cmd == "airport":
        _print(airport(a.icao))


if __name__ == "__main__":
    main()

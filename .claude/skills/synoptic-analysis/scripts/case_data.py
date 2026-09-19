#!/usr/bin/env python3
"""Fetch one weather case from the Xue catalog into a directory, then run the
routine diagnostics on it offline.

  case_data.py fetch DIR --box 22 48 125 155 --times 2026-09-21T00Z,2026-09-21T12Z \
      [--sources gfs,ecmwf] [--bundles prmsl,hgt500,...] \
      [--point 35.55 139.78] [--point-sources gfs,ecmwf,ifshres] \
      [--storm WP242026] [--soundings 47646,47678 | --soundings-near 35.55,139.78,12] \
      [--airport RJTT] [--satellite himawari[:ir104]] [--radar jma] [--nowcast-box S N W E]
      every store read is a few range requests; a full case is 1-3 minutes.
  case_data.py soundings DIR [--time 2026-09-19T00Z] [--tendency]
      mandatory-level table for every fetched station, and the 12/24 h changes.
  case_data.py verify DIR [--source gfs] [--time 2026-09-19T00Z]
      model analysis (or forecast) at the sounding stations against the ascents.
  case_data.py ensemble DIR LAT LON [--times ...] [--from T --to T]
      member positions relative to a point, closest approach per member.
  case_data.py point DIR [--tz 9] [--day 2026-09-21]
      daily summary and one day's hourly table of the point series, in a zone.

The directory holds: meta.json (what was asked, the run ids), fields.npz
(`<src>:<bundle>:<var>:<time>` 2-D arrays plus `<src>:<bundle>:lat|lon`),
point.npz (`<src>:<bundle>:<var>` series plus `<src>:<bundle>:time` as epoch
seconds), storm.json (the tc product's record), ens.json (its ensembles
decoded to per-member tracks), soundings.json (station lines keyed by WMO
number), airport.json, nowcast.npz (one satellite and/or radar frame).
`case_plots.py` draws the standard figure set from the same directory, and
the functions below are importable for anything else.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import xue_stac as X  # noqa: E402

MISSING = -32768
DEFAULT_BUNDLES = ["prmsl", "hgt500", "tmp500", "wind250", "wind850", "qflux850", "thetae850",
                   "tmp850", "rh700", "vvel700", "prate", "wind10m", "gust", "tmp2m", "cape"]
DEFAULT_POINT_BUNDLES = ["prmsl", "tmp2m", "dpt2m", "prate", "gust", "wind10m", "tcdc", "vis", "cape"]
MANDATORY = [100000, 92500, 85000, 70000, 50000, 40000, 30000, 25000, 20000, 15000, 10000]


# --- small helpers -----------------------------------------------------------

def norm_time(t: str) -> str:
    """'2026-09-21T06Z' | '2026-09-21T06:00Z' | '2026-09-21T06:00:00Z' -> '2026-09-21T06:00Z'."""
    t = t.strip().rstrip("Z")
    if len(t) == 13:
        t += ":00"
    return t[:16] + "Z"


def parse_time(t: str) -> datetime:
    return datetime.strptime(norm_time(t), "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)


def km(lat1, lon1, lat2, lon2) -> float:
    return float(X._km(lat1, lon1, lat2, lon2))


def direction_from(u, v):
    return (270 - np.degrees(np.arctan2(v, u))) % 360


def _load_json(d: Path, name: str):
    p = d / name
    return json.loads(p.read_text()) if p.exists() else None


# --- fetch --------------------------------------------------------------------

def fetch_fields(sources, bundles, box, times, out: dict, meta: dict):
    for src in sources:
        item = X.live_item(src)
        stores = X.store_assets(item)
        meta["runs"][src] = item["id"]
        meta["reference"][src] = item["properties"].get("forecast:reference_datetime") or item["properties"].get("datetime")
        for b in bundles:
            if b not in stores:
                print(f"  {src}: no {b}", file=sys.stderr)
                continue
            ds = X.open_store(stores[b])
            for v in ds.data_vars:
                da = X.sel_box(ds[v], *box)
                for T in times:
                    try:
                        fr = X.sel_time(da, T)
                    except ValueError:
                        continue
                    out[f"{src}:{b}:{v}:{T}"] = fr.values.astype("float32")
                out[f"{src}:{b}:lat"] = da.latitude.values
                out[f"{src}:{b}:lon"] = da.longitude.values
            print(f"  {src} {b}", file=sys.stderr)


def fetch_point(sources, bundles, lat, lon, out: dict, meta: dict):
    for src in sources:
        item = X.live_item(src)
        stores = X.store_assets(item)
        meta["runs"][src] = item["id"]
        for b in bundles:
            if b not in stores:
                continue
            ds = X.open_store(stores[b])
            for v in ds.data_vars:
                s = X.sel_point(ds[v], lat, lon)
                out[f"{src}:{b}:{v}"] = s.values.astype("float32")
                out[f"{src}:{b}:time"] = s.time.values.astype("datetime64[s]").astype("int64")
                meta["point_cell"][src] = [float(s.latitude), float(s.longitude)]
        print(f"  point {src}", file=sys.stderr)


def decode_ensembles(storm: dict) -> dict:
    """The tc product's flattened ensembles (member-major, ×100 / ×10 fixed
    point) as {model: {run, base, tracks: [{member, points: [...]}]}}."""
    ens = {}
    for k, v in storm.get("models", {}).items():
        if "members" not in v:
            continue
        leads, members, nl = v["leads"], v["members"], len(v["leads"])
        base = datetime.fromisoformat(v["base"].replace("Z", "+00:00"))
        tracks = []
        for mi, m in enumerate(members):
            pts = []
            for li, lead in enumerate(leads):
                i = mi * nl + li
                la, lo, vm, pm = v["lat"][i], v["lon"][i], v["vmax"][i], v["pmin"][i]
                if la == MISSING:
                    continue
                pts.append({"time": (base + timedelta(seconds=lead)).strftime("%Y-%m-%dT%H:%MZ"),
                            "lead": lead, "lat": la / 100, "lon": lo / 100,
                            "vmax": None if vm == MISSING else vm / 10,
                            "pmin": None if pm == MISSING else pm / 10})
            tracks.append({"member": m, "points": pts})
        ens[k] = {"run": v.get("run"), "base": v["base"], "tracks": tracks}
    return ens


def fetch_soundings(ids: list[str]) -> dict:
    out = {}
    for w in ids:
        try:
            s = X.sounding(w)
        except Exception as e:  # noqa: BLE001
            print(f"  sounding {w}: {e}", file=sys.stderr)
            continue
        if s:
            out[s["wmo"] or w] = s
        else:
            print(f"  sounding {w}: absent", file=sys.stderr)
    return out


def fetch_nowcast(sat: str | None, radar: str | None, box, out: dict, meta: dict):
    if sat:
        src, _, var = sat.partition(":")
        var = var or "ir104"
        item = X.live_item(src)
        ds = X.open_store(X.store_assets(item)[var])
        da = X.sel_box(ds[var], *box) if box else ds[var]
        fr = da.isel(time=-1)
        out["sat"] = fr.values.astype("float32"); out["sat:lat"] = da.latitude.values; out["sat:lon"] = da.longitude.values
        meta["nowcast"]["sat"] = {"source": src, "variable": var, "run": item["id"], "time": str(fr.time.values)[:16] + "Z"}
        print(f"  {src} {var} {meta['nowcast']['sat']['time']}", file=sys.stderr)
    if radar:
        src, _, var = radar.partition(":")
        var = var or ("cref" if src in ("mrms", "cma") else "prate")
        item = X.live_item(src)
        ds = X.open_store(X.store_assets(item)[var])
        da = X.sel_box(ds[var], *box) if box else ds[var]
        fr = da.isel(time=-1)
        out["radar"] = fr.values.astype("float32"); out["radar:lat"] = da.latitude.values; out["radar:lon"] = da.longitude.values
        meta["nowcast"]["radar"] = {"source": src, "variable": var, "run": item["id"], "time": str(fr.time.values)[:16] + "Z"}
        print(f"  {src} {var} {meta['nowcast']['radar']['time']}", file=sys.stderr)


def cmd_fetch(a):
    d = Path(a.dir); d.mkdir(parents=True, exist_ok=True)
    times = [norm_time(t) for t in a.times.split(",")] if a.times else []
    meta = {"fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "box": a.box, "times": times,
            "sources": a.sources.split(","), "runs": {}, "reference": {}, "point": a.point,
            "point_sources": a.point_sources.split(",") if a.point else [], "point_cell": {},
            "storm": a.storm, "airport": a.airport, "nowcast": {}}
    if a.box and times:
        fields = {}
        fetch_fields(meta["sources"], a.bundles.split(","), a.box, times, fields, meta)
        np.savez_compressed(d / "fields.npz", **fields)
    if a.point:
        pt = {}
        fetch_point(meta["point_sources"], a.point_bundles.split(","), a.point[0], a.point[1], pt, meta)
        np.savez_compressed(d / "point.npz", **pt)
    if a.storm:
        storm = X.storm(a.storm)
        (d / "storm.json").write_text(json.dumps(storm))
        (d / "ens.json").write_text(json.dumps(decode_ensembles(storm)))
        print(f"  storm {a.storm}: agencies {list(storm.get('agencies', {}))}, models {list(storm.get('models', {}))}", file=sys.stderr)
    ids = []
    if a.soundings:
        ids += a.soundings.split(",")
    if a.soundings_near:
        lat, lon, n = a.soundings_near.split(",")
        ids += [s["wmo"] for s in X.nearest_soundings(float(lat), float(lon), int(n))]
    if ids:
        snd = fetch_soundings(list(dict.fromkeys(ids)))
        (d / "soundings.json").write_text(json.dumps(snd))
        print(f"  soundings: {len(snd)} stations", file=sys.stderr)
    if a.airport:
        ap = X.airport(a.airport)
        (d / "airport.json").write_text(json.dumps(ap))
        print(f"  airport {a.airport}: {len(ap['metars']) if ap else 0} METARs", file=sys.stderr)
    if a.satellite or a.radar:
        nc = {}
        fetch_nowcast(a.satellite, a.radar, a.nowcast_box or a.box, nc, meta)
        np.savez_compressed(d / "nowcast.npz", **nc)
    (d / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"wrote {d}", file=sys.stderr)


# --- soundings -----------------------------------------------------------------

def level(ascent: dict, P: int, tol: int = 500) -> dict | None:
    """The ascent's values at the level nearest P (Pa, within tol): z gpm,
    t / td °C, wd °, ws m/s; None where a field is missing."""
    best = None
    for i, p in enumerate(ascent["p"]):
        if p == MISSING:
            continue
        if best is None or abs(p - P) < abs(ascent["p"][best] - P):
            best = i
    if best is None or abs(ascent["p"][best] - P) > tol:
        return None
    g = lambda k: (None if ascent[k][best] == MISSING else ascent[k][best])  # noqa: E731
    return {"p": ascent["p"][best] / 100, "z": g("z"),
            "t": None if g("t") is None else g("t") / 100 - 273.15,
            "td": None if g("td") is None else g("td") / 100 - 273.15,
            "wd": g("wd"), "ws": None if g("ws") is None else g("ws") / 10}


def ascent_at(station: dict, time: str) -> dict | None:
    t = norm_time(time)
    for a in station["soundings"]:
        if norm_time(a["time"]) == t:
            return a
    return None


def profile(ascent: dict, p_min: int = 10000):
    """Arrays (p hPa, z, t °C, td °C, wd, ws m/s) with NaN for missing, above p_min."""
    p = np.array(ascent["p"], float); ok = (p != MISSING) & (p >= p_min)
    def f(k, scale, off=0.0):
        a = np.array(ascent[k], float)[ok]
        return np.where(a == MISSING, np.nan, a / scale + off)
    return p[ok] / 100, f("z", 1), f("t", 100, -273.15), f("td", 100, -273.15), f("wd", 1), f("ws", 10)


def _fmt(x, f="{:.1f}"):
    return "-" if x is None else f.format(x)


def _wind(r):
    return "-" if r is None or r["wd"] is None or r["ws"] is None else f"{r['wd']:03.0f}/{r['ws']:.0f}"


def cmd_soundings(a):
    d = Path(a.dir); snd = _load_json(d, "soundings.json") or {}
    times = sorted({norm_time(x["time"]) for s in snd.values() for x in s["soundings"]})
    T = norm_time(a.time) if a.time else times[-1]
    print(f"# ascents at {T}   (z gpm · T °C · Td °C · wind °/m/s; PW mm, FL m, lapse K/km)")
    print(f"{'wmo':6s} {'lat':>5s} {'lon':>6s} | {'850: z T Td wind':22s} | {'700: z T Td wind':22s} | {'500: z T Td wind':22s} | {'300: z T wind':16s} | {'250: z wind':12s} | {'200: z wind':12s} | derived")
    for w, s in sorted(snd.items(), key=lambda kv: -kv[1]["lat"]):
        asc = ascent_at(s, T)
        if not asc:
            continue
        cells = []
        for P, keys in ((85000, "tdw"), (70000, "tdw"), (50000, "tdw"), (30000, "tw"), (25000, "w"), (20000, "w")):
            r = level(asc, P)
            if r is None:
                cells.append("(none)"); continue
            seg = [_fmt(r["z"], "{:.0f}")]
            if "t" in keys: seg.append(_fmt(r["t"]))
            if "d" in keys: seg.append(_fmt(r["td"]))
            if "w" in keys: seg.append(_wind(r))
            cells.append(" ".join(seg))
        dv = asc.get("derived", {})
        print(f"{w:6s} {s['lat']:5.1f} {s['lon']:6.1f} | " + " | ".join(f"{c:22s}" if i < 3 else f"{c:16s}" if i == 3 else f"{c:12s}" for i, c in enumerate(cells))
              + f" | PW {dv.get('pw')} FL {dv.get('freezingLevel')} lapse {dv.get('lapse850_500')} n={asc['n']} launched {asc.get('launched', '')[11:16]}Z")
    if a.tendency:
        print(f"\n# tendency (z500 T500 wind500 | wind250 | 850 T Td wind | PW FL)")
        for w, s in sorted(snd.items(), key=lambda kv: -kv[1]["lat"]):
            rows = []
            for asc in sorted(s["soundings"], key=lambda x: x["time"]):
                r5, r2, r8 = level(asc, 50000), level(asc, 25000), level(asc, 85000)
                dv = asc.get("derived", {})
                rows.append(f"   {norm_time(asc['time'])[5:13]}  z500 {_fmt(r5 and r5['z'], '{:.0f}')} T500 {_fmt(r5 and r5['t'])} w500 {_wind(r5)} | w250 {_wind(r2)} | 850 T {_fmt(r8 and r8['t'])} Td {_fmt(r8 and r8['td'])} w {_wind(r8)} | PW {dv.get('pw')} FL {dv.get('freezingLevel')}")
            print(w); print("\n".join(rows))


# --- verification -----------------------------------------------------------

def field_at(fields, src, bundle, var, T, lat, lon):
    a = fields[f"{src}:{bundle}:{var}:{T}"]; la = fields[f"{src}:{bundle}:lat"]; lo = fields[f"{src}:{bundle}:lon"]
    return float(a[np.abs(la - lat).argmin(), np.abs(lo - lon).argmin()])


def cmd_verify(a):
    d = Path(a.dir); meta = _load_json(d, "meta.json"); snd = _load_json(d, "soundings.json") or {}
    fields = np.load(d / "fields.npz")
    T = norm_time(a.time) if a.time else meta["times"][0]
    srcs = a.source.split(",") if a.source else meta["sources"]
    s, n, w, e = meta["box"]
    print(f"# {'/'.join(srcs)} valid {T} vs ascents at {T}   ({', '.join(meta['runs'][x] for x in srcs)})")
    head = ["wmo", "obs z500"] + [f"{x} z500" for x in srcs] + ["obs T500"] + [f"{x}" for x in srcs] + ["obs T850"] + [f"{x}" for x in srcs] + ["obs w250"] + [f"{x}" for x in srcs]
    print(" | ".join(head))
    dz, dt = [], []
    for wmo, st in sorted(snd.items(), key=lambda kv: -kv[1]["lat"]):
        if not (s <= st["lat"] <= n and w <= st["lon"] <= e):
            continue
        asc = ascent_at(st, T)
        if not asc:
            continue
        r5, r8, r2 = level(asc, 50000), level(asc, 85000), level(asc, 25000)
        row = [wmo, _fmt(r5 and r5["z"], "{:.0f}")]
        for x in srcs:
            try:
                z = field_at(fields, x, "hgt500", "hgt500", T, st["lat"], st["lon"]); row.append(f"{z:.0f}")
                if r5 and r5["z"] is not None: dz.append(z - r5["z"])
            except KeyError:
                row.append("-")
        row.append(_fmt(r5 and r5["t"]))
        for x in srcs:
            try:
                t = field_at(fields, x, "tmp500", "tmp500", T, st["lat"], st["lon"]); row.append(f"{t:.1f}")
                if r5 and r5["t"] is not None: dt.append(t - r5["t"])
            except KeyError:
                row.append("-")
        row.append(_fmt(r8 and r8["t"]))
        for x in srcs:
            try:
                row.append(f"{field_at(fields, x, 'tmp850', 'tmp850', T, st['lat'], st['lon']):.1f}")
            except KeyError:
                row.append("-")
        row.append(_wind(r2))
        for x in srcs:
            try:
                u = field_at(fields, x, "wind250", "ugrd250", T, st["lat"], st["lon"]); v = field_at(fields, x, "wind250", "vgrd250", T, st["lat"], st["lon"])
                row.append(f"{direction_from(u, v):03.0f}/{math.hypot(u, v):.0f}")
            except KeyError:
                row.append("-")
        print(" | ".join(row))
    if dz:
        print(f"\nz500 model−obs: mean {np.mean(dz):+.1f} gpm, max |{np.max(np.abs(dz)):.0f}|;  T500: mean {np.mean(dt):+.2f} K, max |{np.max(np.abs(dt)):.1f}|  (n={len(dz)})")


# --- ensembles ---------------------------------------------------------------

def ensemble_spread(ens: dict, lat: float, lon: float, times: list[str], t_from: str | None, t_to: str | None, box=None):
    """Per model: for each time the members' position spread and distance to
    the point, and each member's closest approach inside [t_from, t_to].
    Members whose track is outside `box` (S N W E) are dropped (the tc
    product keeps a member that wandered to another system under the same id)."""
    out = {}
    for k, v in ens.items():
        tracks = v["tracks"]
        if box:
            s, n, w, e = box
            tracks = [t for t in tracks if any(s <= p["lat"] <= n and w <= p["lon"] <= e for p in t["points"])]
        res = {"run": v["run"], "members": len(v["tracks"]), "tracked": len(tracks), "at": {}, "closest": []}
        for T in times:
            T = norm_time(T)
            rows = [(km(lat, lon, p["lat"], p["lon"]), p) for t in tracks for p in t["points"] if p["time"] == T]
            if not rows:
                continue
            rows.sort(key=lambda r: r[0]); n_ = len(rows)
            lats = sorted(r[1]["lat"] for r in rows); lons = sorted(r[1]["lon"] for r in rows); ds = [r[0] for r in rows]
            vm = sorted(r[1]["vmax"] for r in rows if r[1]["vmax"] is not None); pm = sorted(r[1]["pmin"] for r in rows if r[1]["pmin"] is not None)
            res["at"][T] = {"n": n_, "lat": (lats[0], lats[n_ // 2], lats[-1]), "lon": (lons[0], lons[n_ // 2], lons[-1]),
                            "dist_km": (ds[0], ds[n_ // 2], ds[-1]), "within_150": sum(x < 150 for x in ds), "within_300": sum(x < 300 for x in ds),
                            "vmax_med": vm[len(vm) // 2] if vm else None, "pmin_med": pm[len(pm) // 2] if pm else None}
        for t in tracks:
            pts = [p for p in t["points"] if (not t_from or p["time"] >= norm_time(t_from)) and (not t_to or p["time"] <= norm_time(t_to))]
            if not pts:
                continue
            best = min(pts, key=lambda p: km(lat, lon, p["lat"], p["lon"]))
            res["closest"].append({"member": t["member"], "dist_km": km(lat, lon, best["lat"], best["lon"]), "time": best["time"], "vmax": best["vmax"], "pmin": best["pmin"]})
        res["closest"].sort(key=lambda r: r["dist_km"])
        out[k] = res
    return out


def cmd_ensemble(a):
    d = Path(a.dir); ens = _load_json(d, "ens.json"); meta = _load_json(d, "meta.json")
    times = a.times.split(",") if a.times else meta["times"]
    res = ensemble_spread(ens, a.lat, a.lon, times, a.t_from, a.t_to, box=meta.get("box"))
    for k, r in res.items():
        print(f"### {k} run {r['run']}: {r['tracked']} of {r['members']} members tracked inside the box")
        for T, s in r["at"].items():
            print(f" {T} n={s['n']:2d} lat {s['lat'][0]:.1f}/{s['lat'][1]:.1f}/{s['lat'][2]:.1f} lon {s['lon'][0]:.1f}/{s['lon'][1]:.1f}/{s['lon'][2]:.1f} "
                  f"dist min/med/max {s['dist_km'][0]:.0f}/{s['dist_km'][1]:.0f}/{s['dist_km'][2]:.0f} km  <150 km {s['within_150']}  <300 km {s['within_300']}  vmax med {s['vmax_med']} pmin med {s['pmin_med']}")
        c = r["closest"]
        if c:
            ds = [x["dist_km"] for x in c]
            print(f" closest approach: median {ds[len(ds) // 2]:.0f} km; " + ", ".join(f"{x['dist_km']:.0f}@{x['time'][5:13]}" for x in c[:12]) + (" …" if len(c) > 12 else ""))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1))


# --- point series --------------------------------------------------------------

def point_series(pt, src: str, bundle: str, var: str, tz_hours: float):
    tz = timezone(timedelta(hours=tz_hours))
    t = pt[f"{src}:{bundle}:time"].astype("int64")
    return [datetime.fromtimestamp(int(x), tz=timezone.utc).astimezone(tz) for x in t], pt[f"{src}:{bundle}:{var}"]


def point_frame(pt, src: str, tz_hours: float) -> dict:
    """One dict of aligned columns for a source: t (local datetimes), p, t2, td, ws, wd, gust, rate, cloud."""
    keys = {k.split(":")[1] for k in pt.files if k.startswith(src + ":")}
    if "wind10m" not in keys and "prate" not in keys:
        return {}
    base = "wind10m" if "wind10m" in keys else "prate"
    t, _ = point_series(pt, src, base, "ugrd10m" if base == "wind10m" else "prate", tz_hours)
    def col(b, v):
        if b not in keys:
            return np.full(len(t), np.nan)
        tt, s = point_series(pt, src, b, v, tz_hours)
        if len(tt) == len(t):
            return s
        m = {x: y for x, y in zip(tt, s)}
        return np.array([m.get(x, np.nan) for x in t])
    u, v = col("wind10m", "ugrd10m"), col("wind10m", "vgrd10m")
    return {"t": t, "p": col("prmsl", "prmsl"), "t2": col("tmp2m", "tmp2m"), "td": col("dpt2m", "dpt2m"),
            "ws": np.hypot(u, v), "wd": direction_from(u, v), "gust": col("gust", "gust"), "rate": col("prate", "prate"),
            "cloud": col("tcdc", "tcdc"), "vis": col("vis", "vis"), "cape": col("cape", "cape")}


def accumulate(t, rate):
    ts = np.array([x.timestamp() for x in t]); acc = np.zeros(len(rate))
    for i in range(1, len(rate)):
        acc[i] = acc[i - 1] + (0 if np.isnan(rate[i]) else rate[i] * (ts[i] - ts[i - 1]) / 3600)
    return acc


def daily(fr: dict) -> list[dict]:
    """Per local calendar day: precipitation (mm), Tmin/Tmax, max wind and gust, frames."""
    rows = []
    ts = np.array([x.timestamp() for x in fr["t"]])
    for day in sorted({x.date() for x in fr["t"]}):
        idx = [i for i, x in enumerate(fr["t"]) if x.date() == day]
        acc = sum(fr["rate"][i] * (ts[i] - ts[i - 1]) / 3600 for i in idx if i > 0 and not np.isnan(fr["rate"][i]))
        rows.append({"day": day.isoformat(), "precip_mm": round(float(acc), 1), "tmin": float(np.nanmin(fr["t2"][idx])) if not np.all(np.isnan(fr["t2"][idx])) else None,
                     "tmax": float(np.nanmax(fr["t2"][idx])) if not np.all(np.isnan(fr["t2"][idx])) else None,
                     "wind_max": float(np.nanmax(fr["ws"][idx])), "gust_max": float(np.nanmax(fr["gust"][idx])) if not np.all(np.isnan(fr["gust"][idx])) else None, "frames": len(idx)})
    return rows


def cmd_point(a):
    d = Path(a.dir); meta = _load_json(d, "meta.json"); pt = np.load(d / "point.npz")
    tz = a.tz
    print(f"# point {meta['point']} · times in UTC{tz:+g}")
    for src in meta["point_sources"]:
        fr = point_frame(pt, src, tz)
        if not fr:
            continue
        print(f"\n## {src} {meta['runs'].get(src)}  cell {meta['point_cell'].get(src)}")
        print("day | precip mm | Tmin | Tmax | max wind | max gust | frames")
        for r in daily(fr):
            print(f" {r['day']} | {r['precip_mm']:6.1f} | {_fmt(r['tmin'])} | {_fmt(r['tmax'])} | {r['wind_max']:.1f} | {_fmt(r['gust_max'])} | {r['frames']}")
        if a.day:
            print(f"hourly {a.day}: time | MSLP | dir/speed | gust | rate | T | Td | cloud")
            for i, x in enumerate(fr["t"]):
                if x.date().isoformat() == a.day:
                    print(f"  {x.strftime('%d %H:%M')} | {fr['p'][i]:.0f} | {fr['wd'][i]:03.0f}°/{fr['ws'][i]:.1f} | {fr['gust'][i]:.1f} | {fr['rate'][i]:.1f} | {fr['t2'][i]:.1f} | {fr['td'][i]:.1f} | {fr['cloud'][i]:.0f}")


# --- CLI ------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch"); p.add_argument("dir")
    p.add_argument("--box", type=float, nargs=4, metavar=("S", "N", "W", "E"))
    p.add_argument("--times", help="comma-separated valid times, UTC")
    p.add_argument("--sources", default="gfs,ecmwf"); p.add_argument("--bundles", default=",".join(DEFAULT_BUNDLES))
    p.add_argument("--point", type=float, nargs=2, metavar=("LAT", "LON"))
    p.add_argument("--point-sources", default="gfs,ecmwf,ifshres"); p.add_argument("--point-bundles", default=",".join(DEFAULT_POINT_BUNDLES))
    p.add_argument("--storm"); p.add_argument("--soundings"); p.add_argument("--soundings-near", metavar="LAT,LON,N")
    p.add_argument("--airport"); p.add_argument("--satellite", metavar="SOURCE[:VAR]"); p.add_argument("--radar", metavar="SOURCE[:VAR]")
    p.add_argument("--nowcast-box", type=float, nargs=4, metavar=("S", "N", "W", "E"))
    p.set_defaults(fn=cmd_fetch)
    p = sub.add_parser("soundings"); p.add_argument("dir"); p.add_argument("--time"); p.add_argument("--tendency", action="store_true"); p.set_defaults(fn=cmd_soundings)
    p = sub.add_parser("verify"); p.add_argument("dir"); p.add_argument("--source"); p.add_argument("--time"); p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("ensemble"); p.add_argument("dir"); p.add_argument("lat", type=float); p.add_argument("lon", type=float)
    p.add_argument("--times"); p.add_argument("--from", dest="t_from"); p.add_argument("--to", dest="t_to"); p.add_argument("--json"); p.set_defaults(fn=cmd_ensemble)
    p = sub.add_parser("point"); p.add_argument("dir"); p.add_argument("--tz", type=float, default=0); p.add_argument("--day"); p.set_defaults(fn=cmd_point)
    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

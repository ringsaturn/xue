#!/usr/bin/env python3
"""Standard synoptic diagnostics from a Xue forecast run, as text.

  synoptic_report.py region gfs --box 20 50 105 150 --times 2026-09-20T00Z,2026-09-21T00Z
      the synoptic situation over a box (south north west east) at each valid
      time: pressure centres, 500 hPa pattern, jet, low-level thermal and
      moisture structure, forcing, instability, precipitation and wind.
  synoptic_report.py meteogram gfs 35.7 139.7 [--hours 120]
      a point's forecast as a table (every frame) and a daily summary.
  synoptic_report.py compare 35.7 139.7 [--sources gfs,ecmwf,aifs,ifshres]
      the same headline numbers from several models at one point.

Options: --json writes the numbers as JSON beside the text. Every number is
read from the live run's Zarr store through xue_stac.py; nothing is cached.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import xue_stac as X  # noqa: E402

KM_PER_DEG = 111.2


# --- helpers -----------------------------------------------------------------

def _load(source: str, bundle: str, cache: dict):
    if bundle not in cache:
        item = cache["_item"]
        stores = X.store_assets(item)
        cache[bundle] = X.open_store(stores[bundle]) if bundle in stores else None
    return cache[bundle]


def _frame(ds, var: str, time, box):
    da = X.sel_time(ds[var], time)
    return X.sel_box(da, *box).compute()


def _speed(u, v):
    return np.hypot(u, v)


def _direction_from(u, v):
    """Meteorological direction the flow comes from, degrees."""
    return (270 - np.degrees(np.arctan2(v, u))) % 360


def _extrema(field, kind: str, window: int, limit: int = 6, min_prominence: float = 0.0):
    """Local minima/maxima of a 2-D DataArray with a (2w+1)^2 neighbourhood.
    Cells on the box boundary are excluded (a gradient running out of the
    box is not a centre), and extrema of one quantization plateau closer
    than 2w cells are reported once. Returns [(lat, lon, value)] sorted by
    value, deepest low / highest high first."""
    v = field.values.astype(float)
    if np.all(np.isnan(v)) or min(v.shape) < 2 * window + 3:
        return []
    fill = np.nanmax(v) + 1 if kind == "min" else np.nanmin(v) - 1
    a = np.where(np.isnan(v), fill, v)
    pad = np.pad(a, window, mode="edge")
    ext = a.copy(); opp = a.copy()
    for dy in range(-window, window + 1):
        for dx in range(-window, window + 1):
            sh = pad[window + dy:window + dy + a.shape[0], window + dx:window + dx + a.shape[1]]
            ext = np.minimum(ext, sh) if kind == "min" else np.maximum(ext, sh)
            opp = np.maximum(opp, sh) if kind == "min" else np.minimum(opp, sh)
    hit = (a == ext) & ~np.isnan(v) & (np.abs(opp - a) >= min_prominence)
    hit[:window, :] = hit[-window:, :] = False
    hit[:, :window] = hit[:, -window:] = False
    ys, xs = np.where(hit)
    rows = [(y, x, float(v[y, x])) for y, x in zip(ys, xs)]
    rows.sort(key=lambda r: r[2], reverse=(kind == "max"))
    kept = []
    for y, x, val in rows:
        if all(abs(y - ky) > 2 * window or abs(x - kx) > 2 * window for ky, kx, _ in kept):
            kept.append((y, x, val))
    return [(float(field.latitude[y]), float(field.longitude[x]), val) for y, x, val in kept[:limit]]


def _step(field):
    return abs(float(field.longitude[1] - field.longitude[0])) if field.longitude.size > 1 else 1.0


def _gradient_per_100km(field):
    """|∇field| per 100 km on a lat/lon grid."""
    v = field.values.astype(float)
    lat = np.radians(field.latitude.values)
    dlat = abs(float(field.latitude[1] - field.latitude[0])) * KM_PER_DEG
    dlon = abs(float(field.longitude[1] - field.longitude[0])) * KM_PER_DEG * np.cos(lat)[:, None]
    gy, gx = np.gradient(v)
    return np.hypot(gy / dlat, gx / dlon) * 100


def _where_max(field, mask=None):
    v = field.values.astype(float)
    if mask is not None:
        v = np.where(mask, v, np.nan)
    if np.all(np.isnan(v)):
        return None
    i = np.unravel_index(np.nanargmax(v), v.shape)
    return float(field.latitude[i[0]]), float(field.longitude[i[1]]), float(v[i])


def _where_min(field):
    v = field.values.astype(float)
    if np.all(np.isnan(v)):
        return None
    i = np.unravel_index(np.nanargmin(v), v.shape)
    return float(field.latitude[i[0]]), float(field.longitude[i[1]]), float(v[i])


def _fmt_pos(lat, lon):
    lon = ((lon + 180) % 360) - 180
    return f"{abs(lat):.1f}°{'N' if lat >= 0 else 'S'} {abs(lon):.1f}°{'E' if lon >= 0 else 'W'}"


def _area_share(field, cond):
    v = field.values
    ok = ~np.isnan(v)
    return float(np.sum(cond & ok) / max(np.sum(ok), 1))


# --- region ------------------------------------------------------------------

def region_report(source: str, box, times, out: dict):
    cache = {"_item": X.live_item(source)}
    item = cache["_item"]
    p = item["properties"]
    print(f"# {item['id']}  reference {p.get('forecast:reference_datetime') or p.get('xue:runTime')}  "
          f"axis {p['start_datetime']} .. {p['end_datetime']}  box S{box[0]} N{box[1]} W{box[2]} E{box[3]}")
    stores = X.store_assets(item)
    out["run"] = item["id"]; out["reference"] = p.get("forecast:reference_datetime"); out["times"] = {}
    south, north, west, east = box
    span_deg = max(north - south, (east - west) % 360 or 360)
    window = max(2, int(round(3.0 / _grid_step(stores, cache))))  # ~3° neighbourhood

    for t in times:
        print(f"\n## valid {t}")
        rec = out["times"][t] = {}

        if "prmsl" in stores:
            f = _frame(_load(source, "prmsl", cache), "prmsl", t, box)
            lows = _extrema(f, "min", window, min_prominence=3); highs = _extrema(f, "max", window, min_prominence=3)
            print(f"MSLP  range {np.nanmin(f.values):.0f}–{np.nanmax(f.values):.0f} hPa")
            for la, lo, v in lows:
                print(f"  L {v:.0f} hPa at {_fmt_pos(la, lo)}")
            for la, lo, v in highs:
                print(f"  H {v:.0f} hPa at {_fmt_pos(la, lo)}")
            rec["lows"] = lows; rec["highs"] = highs
            g = _gradient_per_100km(f)
            print(f"  strongest pressure gradient {np.nanmax(g):.1f} hPa/100 km")

        if "hgt500" in stores:
            f = _frame(_load(source, "hgt500", cache), "hgt500", t, box)
            v = f.values
            print(f"H500  range {np.nanmin(v):.0f}–{np.nanmax(v):.0f} gpm")
            lows = _extrema(f, "min", window, 4, min_prominence=30); highs = _extrema(f, "max", window, 3, min_prominence=30)
            for la, lo, hv in lows:
                print(f"  500 hPa low/cut-off centre {hv:.0f} gpm at {_fmt_pos(la, lo)}")
            for la, lo, hv in highs:
                print(f"  500 hPa ridge centre {hv:.0f} gpm at {_fmt_pos(la, lo)}")
            # subtropical high: 5880 gpm line's western/northern reach
            m = v >= 5880
            if m.any():
                ys, xs = np.where(m)
                print(f"  5880 gpm area: westernmost {float(f.longitude[xs.min()]):.1f}°, northernmost {float(f.latitude[ys.min()]):.1f}°, "
                      f"{_area_share(f, m):.0%} of box")
                rec["h5880"] = {"west": float(f.longitude[xs.min()]), "north": float(f.latitude[ys.min()])}
            else:
                print("  no 5880 gpm contour in box")
            rec["hgt500_lows"] = lows; rec["hgt500_highs"] = highs

        if "wind250" in stores:
            ds = _load(source, "wind250", cache)
            u = _frame(ds, "ugrd250", t, box); vv = _frame(ds, "vgrd250", t, box)
            spd = u.copy(data=_speed(u.values, vv.values))
            m = _where_max(spd)
            if m:
                d = _direction_from(u.values, vv.values)
                i = np.unravel_index(np.nanargmax(spd.values), spd.shape)
                print(f"JET250 max {m[2]:.0f} m/s ({m[2]*1.944:.0f} kt) at {_fmt_pos(m[0], m[1])}, from {d[i]:.0f}°; "
                      f"≥30 m/s over {_area_share(spd, spd.values >= 30):.0%} of box")
                rec["jet"] = m

        if "tmp850" in stores:
            f = _frame(_load(source, "tmp850", cache), "tmp850", t, box)
            g = _gradient_per_100km(f)
            mx = _where_max(f.copy(data=g))
            print(f"T850  range {np.nanmin(f.values):.1f}–{np.nanmax(f.values):.1f} °C; "
                  f"strongest gradient {mx[2]:.1f} K/100 km at {_fmt_pos(mx[0], mx[1])}"
                  f"{'  (frontal zone: > 1.5 K/100 km)' if mx[2] > 1.5 else ''}")
            rec["t850_gradient"] = mx

        if "thetae850" in stores:
            f = _frame(_load(source, "thetae850", cache), "thetae850", t, box)
            g = _gradient_per_100km(f)
            mx = _where_max(f.copy(data=g)); hi = _where_max(f)
            print(f"θe850 max {hi[2]:.0f} K at {_fmt_pos(hi[0], hi[1])}; strongest θe gradient {mx[2]:.1f} K/100 km at {_fmt_pos(mx[0], mx[1])}")
            rec["thetae850"] = {"max": hi, "gradient": mx}

        if "qflux850" in stores:
            ds = _load(source, "qflux850", cache)
            u = _frame(ds, "uqflx850", t, box); vv = _frame(ds, "vqflx850", t, box)
            mag = u.copy(data=_speed(u.values, vv.values))
            m = _where_max(mag)
            i = np.unravel_index(np.nanargmax(mag.values), mag.shape)
            d = _direction_from(u.values, vv.values)[i]
            print(f"QFLX850 max {m[2]:.1f} g/(cm·hPa·s) at {_fmt_pos(m[0], m[1])}, from {d:.0f}° "
                  f"(moist transport ≥ 15 is a strong low-level jet / atmospheric river signal)")
            rec["qflux850"] = m

        if "rh700" in stores:
            f = _frame(_load(source, "rh700", cache), "rh700", t, box)
            print(f"RH700 ≥ 80 % over {_area_share(f, f.values >= 80):.0%} of box (mid-level cloud / rain shield)")

        for lev in ("vvel700", "vvel500"):
            if lev in stores:
                f = _frame(_load(source, lev, cache), lev, t, box)
                mn = _where_min(f)
                floor = " (codebook floor, actual ascent may be stronger)" if mn[2] <= -6.3 else ""
                print(f"{lev.upper()} strongest ascent {mn[2]:.2f} Pa/s{floor} at {_fmt_pos(mn[0], mn[1])}; "
                      f"ascent < −0.3 Pa/s over {_area_share(f, f.values < -0.3):.0%} of box")
                rec[lev] = mn

        if "cape" in stores:
            f = _frame(_load(source, "cape", cache), "cape", t, box)
            mx = _where_max(f)
            print(f"CAPE  max {mx[2]:.0f} J/kg at {_fmt_pos(mx[0], mx[1])}; ≥ 1000 over {_area_share(f, f.values >= 1000):.0%}, ≥ 2500 over {_area_share(f, f.values >= 2500):.0%}")
            rec["cape"] = mx

        if "prate" in stores:
            f = _frame(_load(source, "prate", cache), "prate", t, box)
            mx = _where_max(f)
            print(f"PRATE max {mx[2]:.1f} mm/h at {_fmt_pos(mx[0], mx[1])}; ≥ 1 mm/h over {_area_share(f, f.values >= 1):.0%}, ≥ 10 mm/h over {_area_share(f, f.values >= 10):.0%}")
            rec["prate"] = mx

        if "wind10m" in stores:
            ds = _load(source, "wind10m", cache)
            u = _frame(ds, "ugrd10m", t, box); vv = _frame(ds, "vgrd10m", t, box)
            spd = u.copy(data=_speed(u.values, vv.values))
            m = _where_max(spd)
            print(f"WIND10 max {m[2]:.0f} m/s at {_fmt_pos(m[0], m[1])}; ≥ 17 m/s (gale) over {_area_share(spd, spd.values >= 17):.1%} of box")
            rec["wind10m"] = m
        if "gust" in stores:
            f = _frame(_load(source, "gust", cache), "gust", t, box)
            mx = _where_max(f)
            print(f"GUST  max {mx[2]:.0f} m/s at {_fmt_pos(mx[0], mx[1])}")
        if "tmp2m" in stores:
            f = _frame(_load(source, "tmp2m", cache), "tmp2m", t, box)
            print(f"T2M   range {np.nanmin(f.values):.1f}–{np.nanmax(f.values):.1f} °C")


def _grid_step(stores, cache):
    ds = _load(None, next(iter(stores)), cache)
    return abs(float(ds.longitude[1] - ds.longitude[0]))


# --- accumulations -----------------------------------------------------------

def accumulate_mm(rate, times):
    """Integrate a rate series (mm/h at each frame) over the axis.
    A frame's rate is taken to hold until the next frame (a step function),
    which matches how the models publish a rate valid over the step ending
    at the frame closely enough for totals; the first frame of a
    de-accumulated series is already the first step's mean."""
    t = times.astype("datetime64[s]").astype("int64")
    dt_h = np.diff(t) / 3600.0
    r = np.nan_to_num(rate[1:])
    return np.concatenate([[0.0], np.cumsum(r * dt_h)])


# --- meteogram ---------------------------------------------------------------

def meteogram(source: str, lat: float, lon: float, hours: int | None, out: dict):
    cache = {"_item": X.live_item(source)}
    item = cache["_item"]; stores = X.store_assets(item)
    p = item["properties"]
    cols = {}
    def series(bundle, var):
        ds = _load(source, bundle, cache)
        if ds is None:
            return None
        s = X.sel_point(ds[var], lat, lon).compute()
        cols["_lat"], cols["_lon"] = float(s.latitude), float(s.longitude)
        return s
    prmsl = series("prmsl", "prmsl") if "prmsl" in stores else None
    t2 = series("tmp2m", "tmp2m"); td = series("dpt2m", "dpt2m") if "dpt2m" in stores else None
    u = series("wind10m", "ugrd10m"); v = series("wind10m", "vgrd10m")
    gust = series("gust", "gust") if "gust" in stores else None
    pr = series("prate", "prate")
    tcdc = series("tcdc", "tcdc") if "tcdc" in stores else None
    cape = series("cape", "cape") if "cape" in stores else None
    base = t2 if t2 is not None else pr
    times = base.time.values
    if hours:
        keep = times <= times[0] + np.timedelta64(hours, "h")
    else:
        keep = np.ones(len(times), bool)

    def at(s, t):
        if s is None:
            return np.nan
        i = np.where(s.time.values == t)[0]
        return float(s.values[i[0]]) if i.size else np.nan

    print(f"# {item['id']}  reference {p.get('forecast:reference_datetime') or p.get('xue:runTime')}  cell {cols['_lat']:.3f},{cols['_lon']:.3f}")
    print("valid(UTC)         MSLP   T2m   Td2m  wind(dir/spd m/s)  gust   rate(mm/h) accum(mm) cloud%  CAPE")
    pr_acc = accumulate_mm(np.array([at(pr, t) for t in times]), times) if pr is not None else np.full(len(times), np.nan)
    rows = []
    for k, t in enumerate(times):
        if not keep[k]:
            break
        uu, vv = at(u, t), at(v, t)
        spd = _speed(uu, vv); d = _direction_from(uu, vv)
        row = dict(time=str(np.datetime_as_string(t, unit="m")), mslp=at(prmsl, t), t2m=at(t2, t), td2m=at(td, t),
                   wdir=d, wspd=spd, gust=at(gust, t), rate=at(pr, t), accum=pr_acc[k], cloud=at(tcdc, t), cape=at(cape, t))
        rows.append(row)
        print(f"{row['time']:17} {row['mslp']:6.0f} {row['t2m']:5.1f} {row['td2m']:6.1f}   {d:3.0f}°/{spd:4.1f}       {row['gust']:5.1f}   {row['rate']:6.2f}    {row['accum']:6.1f}   {row['cloud']:4.0f}  {row['cape']:5.0f}")
    out["rows"] = rows
    # daily summary
    print("\nday (UTC)    Tmin  Tmax  precip(mm)  max wind  max gust  mean cloud")
    days = {}
    for r in rows:
        days.setdefault(r["time"][:10], []).append(r)
    daily = []
    for d, rs in days.items():
        acc = rs[-1]["accum"] - (daily[-1]["accum_end"] if daily else 0.0)
        rec = dict(day=d, tmin=np.nanmin([r["t2m"] for r in rs]), tmax=np.nanmax([r["t2m"] for r in rs]), precip=acc,
                   wmax=np.nanmax([r["wspd"] for r in rs]), gmax=np.nanmax([r["gust"] for r in rs]), cloud=np.nanmean([r["cloud"] for r in rs]),
                   accum_end=rs[-1]["accum"], frames=len(rs))
        daily.append(rec)
        print(f"{d}  {rec['tmin']:5.1f} {rec['tmax']:5.1f}  {rec['precip']:8.1f}    {rec['wmax']:6.1f}   {rec['gmax']:6.1f}     {rec['cloud']:5.0f}   ({rec['frames']} frames)")
    out["daily"] = daily


def compare(lat: float, lon: float, sources: list[str], out: dict):
    print(f"# point {lat},{lon}: headline per model (first 5 days where the axis allows)")
    print("source    run            Tmin   Tmax   precip 0–120h  max wind  MSLP min")
    for src in sources:
        try:
            cache = {"_item": X.live_item(src)}
        except Exception as e:  # a source without a live run
            print(f"{src:9} unavailable ({e})"); continue
        stores = X.store_assets(cache["_item"])
        def s(b, v):
            ds = _load(src, b, cache)
            return X.sel_point(ds[v], lat, lon).compute() if ds is not None else None
        t2 = s("tmp2m", "tmp2m"); pr = s("prate", "prate"); u = s("wind10m", "ugrd10m"); v = s("wind10m", "vgrd10m")
        pm = s("prmsl", "prmsl") if "prmsl" in stores else None
        end = t2.time.values[0] + np.timedelta64(120, "h")
        k = t2.time.values <= end
        acc = accumulate_mm(pr.values.astype(float), pr.time.values)
        kp = pr.time.values <= end
        spd = _speed(u.values, v.values)[u.time.values <= end]
        rec = dict(run=cache["_item"]["id"], tmin=float(np.nanmin(t2.values[k])), tmax=float(np.nanmax(t2.values[k])),
                   precip=float(acc[kp][-1]), wmax=float(np.nanmax(spd)), mslp_min=float(np.nanmin(pm.values[pm.time.values <= end])) if pm is not None else np.nan)
        out[src] = rec
        print(f"{src:9} {rec['run']:14} {rec['tmin']:5.1f}  {rec['tmax']:5.1f}   {rec['precip']:8.1f}       {rec['wmax']:6.1f}   {rec['mslp_min']:7.0f}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("region"); p.add_argument("source")
    p.add_argument("--box", nargs=4, type=float, required=True, metavar=("S", "N", "W", "E"))
    p.add_argument("--times", required=True, help="comma-separated valid times (ISO, UTC)")
    p.add_argument("--json")
    p = sub.add_parser("meteogram"); p.add_argument("source"); p.add_argument("lat", type=float); p.add_argument("lon", type=float)
    p.add_argument("--hours", type=int); p.add_argument("--json")
    p = sub.add_parser("compare"); p.add_argument("lat", type=float); p.add_argument("lon", type=float)
    p.add_argument("--sources", default="gfs,ecmwf,aifs,ifshres"); p.add_argument("--json")
    a = ap.parse_args(argv)
    out = {}
    if a.cmd == "region":
        region_report(a.source, a.box, [t.strip() for t in a.times.split(",")], out)
    elif a.cmd == "meteogram":
        meteogram(a.source, a.lat, a.lon, a.hours, out)
    elif a.cmd == "compare":
        compare(a.lat, a.lon, a.sources.split(","), out)
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, default=float))
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()

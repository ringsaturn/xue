#!/usr/bin/env python3
"""The standard figure set from a case directory written by case_data.py.

  case_plots.py DIR [--out DIR/figures] [--tz 9] [--lang zh|en]
      [--figures tracks,upper,verify,surface,moisture,meteogram,skewt,nowcast,ensemble]
      [--map-times T1,T2] [--point-name 羽田] [--mark 2026-09-21T08Z] [--window T1,T2]
      [--span T1,T2] [--skewt 47646,47678,47971] [--map-box S N W E] [--font "Hiragino Sans GB"]

Each figure is drawn only when the directory has what it needs (tracks and
ensemble need storm.json / ens.json, meteogram needs point.npz, skewt needs
soundings.json, nowcast needs nowcast.npz, the maps need fields.npz with the
bundles they read: prmsl, hgt500, wind250, wind850, qflux850, thetae850,
prate, wind10m, gust). Needs matplotlib. The coastline is Natural Earth 50 m,
downloaded once into ~/.cache/xue-synoptic/.
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
import case_data as C  # noqa: E402

COAST_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_coastline.geojson"
RAIN_LEVELS = [0.5, 1, 2, 5, 10, 20, 40, 80]
RAIN_COLORS = ["#d9f0ff", "#a6d8ff", "#66b3ff", "#2a80ff", "#ffe066", "#ff9d1a", "#e0261c", "#8e0d8e"]
MODEL_COLORS = {"gfs": "#c0392b", "ecmwf": "#1f5fbf", "ifshres": "#2e8b57", "aifs": "#8e0d8e", "hrrr": "#d35400", "sflux": "#7f8c8d"}
MODEL_NAMES = {"gfs": "GFS", "ecmwf": "ECMWF IFS", "ifshres": "IFS HRES", "aifs": "AIFS", "hrrr": "HRRR", "sflux": "GFS sflux"}
ENS_COLORS = {"gefs": "#f4a6a6", "ecmwfens": "#a6c8f4"}
ENS_NAMES = {"gefs": "GEFS", "ecmwfens": "ECMWF ENS"}

L = {
    "zh": {"tracks": "{storm} 路径：机构预报、确定性模式与集合成员", "r34": "{ag} 34 kt 大风半径（{t}）", "members": "{m} 成员 ({n})",
           "upper": "500 hPa 位势高度（等值线 gpm，红线 = 5880）与 250 hPa 急流（色阶、风羽）", "jet": "250 hPa 风速 m/s", "valid": "有效 {t}",
           "verify500": "{run} 分析：500 hPa 高度 + 250 hPa 风（数字/风羽 = 探空实测 {t}）", "verify850": "{run} 分析：850 hPa 风 + 500 hPa 高度（数字 = 850 hPa T °C / PW mm）",
           "surface": "海平面气压（等值线 hPa）、10 m 风（风羽 kt）、降水率（色阶）；红虚线 = 10 m 风 ≥ 17.2 m/s", "rain": "降水率 mm/h",
           "moist": "{run} 850 hPa 水汽通量 + θe", "gust": "{run} 10 m 阵风 + 海平面气压", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "阵风 m/s",
           "mslp": "海平面气压 hPa", "wind": "风速 m/s", "rate": "降水率 mm/h", "accum": "累积降水 mm（自各模式起报）", "w10": " 10 m 风", "gustl": " 阵风",
           "gale": "17.2 m/s (烈风)", "strong": "10.8 m/s (强风)", "meteo": "{name}（{lat:.2f}°N {lon:.2f}°E 最近格点）模式气象图，时间 UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "温度 °C（斜线坐标）", "pw": "PW {pw} mm · 冻结高度 {fl} m", "lapse": "850–500 递减率 {lr} K/km · 对流层顶 {tp} m",
           "sat": "{src} {var}  {t}Z（{loc}）", "radar": "{src} {var}  {t}Z（{loc}）", "centre": "{id} 中心 ({ag} {t})",
           "ens_d": "成员路径距 {name} 最近距离 km", "ens_n": "成员数", "ens_t": "最近距离出现时刻（UTC{tz:+g}）", "ens_title": "集合预报：中心最接近 {name} 的距离与时刻（只统计留在区域内的成员）"},
    "en": {"tracks": "{storm} track: agency forecast, deterministic models, ensemble members", "r34": "{ag} 34 kt wind radius ({t})", "members": "{m} members ({n})",
           "upper": "500 hPa height (contours gpm, red = 5880) and 250 hPa jet (shading, barbs)", "jet": "250 hPa wind m/s", "valid": "valid {t}",
           "verify500": "{run} analysis: 500 hPa height + 250 hPa wind (numbers/barbs = soundings {t})", "verify850": "{run} analysis: 850 hPa wind + 500 hPa height (numbers = 850 hPa T °C / PW mm)",
           "surface": "MSLP (contours hPa), 10 m wind (barbs kt), rain rate (shading); red dashed = 10 m wind ≥ 17.2 m/s", "rain": "rain rate mm/h",
           "moist": "{run} 850 hPa moisture flux + θe", "gust": "{run} 10 m gust + MSLP", "qflux": "|q·V/g| g/(cm·hPa·s)", "gustcb": "gust m/s",
           "mslp": "MSLP hPa", "wind": "wind m/s", "rate": "rain rate mm/h", "accum": "accumulated mm (from each run's start)", "w10": " 10 m wind", "gustl": " gust",
           "gale": "17.2 m/s (gale)", "strong": "10.8 m/s (strong)", "meteo": "{name} ({lat:.2f}°N {lon:.2f}°E nearest cell) model meteogram, times UTC{tz:+g}", "mark": " {t}",
           "skewt_x": "temperature °C (skewed)", "pw": "PW {pw} mm · freezing level {fl} m", "lapse": "850–500 lapse {lr} K/km · tropopause {tp} m",
           "sat": "{src} {var}  {t}Z ({loc})", "radar": "{src} {var}  {t}Z ({loc})", "centre": "{id} centre ({ag} {t})",
           "ens_d": "closest approach to {name}, km", "ens_n": "members", "ens_t": "time of closest approach (UTC{tz:+g})", "ens_title": "Ensembles: closest approach to {name} (members that stayed in the box)"},
}


class Case:
    def __init__(self, d: Path, tz: float, lang: str):
        self.d = d; self.tz = timezone(timedelta(hours=tz)); self.tzh = tz; self.T = L[lang]
        self.meta = json.loads((d / "meta.json").read_text())
        self.fields = np.load(d / "fields.npz") if (d / "fields.npz").exists() else None
        self.point = np.load(d / "point.npz") if (d / "point.npz").exists() else None
        self.now = np.load(d / "nowcast.npz") if (d / "nowcast.npz").exists() else None
        self.storm = C._load_json(d, "storm.json"); self.ens = C._load_json(d, "ens.json"); self.snd = C._load_json(d, "soundings.json")
        self.coast = load_coast()

    def local(self, T: str, fmt="%m/%d %H") -> str:
        return C.parse_time(T).astimezone(self.tz).strftime(fmt) + (f" UTC{self.tzh:+g}" if self.tzh else "Z")

    def run_label(self, src: str) -> str:
        run = self.meta["runs"].get(src, src)
        return f"{MODEL_NAMES.get(src, src)} {run.split('.')[1][:8]}/{run.split('.')[1][8:10]}Z" if "." in run else run

    def fld(self, src, b, v, T):
        f = self.fields
        return f[f"{src}:{b}:{v}:{T}"], f[f"{src}:{b}:lat"], f[f"{src}:{b}:lon"]

    def has(self, src, b, T):
        return self.fields is not None and any(k.startswith(f"{src}:{b}:") and k.endswith(T) for k in self.fields.files)


def load_coast():
    cache = Path.home() / ".cache" / "xue-synoptic" / "ne_50m_coastline.geojson"
    if not cache.exists():
        import requests
        cache.parent.mkdir(parents=True, exist_ok=True)
        r = requests.get(COAST_URL, timeout=120); r.raise_for_status(); cache.write_bytes(r.content)
    g = json.loads(cache.read_text())
    lines = []
    for feat in g["features"]:
        geom = feat["geometry"]
        for ln in (geom["coordinates"] if geom["type"] == "MultiLineString" else [geom["coordinates"]]):
            lines.append(np.asarray(ln))
    return lines


# --- map scaffolding ----------------------------------------------------------

def setmap(ax, box, case: Case, title=None, marks=()):
    s, n, w, e = box
    for a in case.coast:
        if a[:, 0].max() < w - 5 or a[:, 0].min() > e + 5 or a[:, 1].max() < s - 5 or a[:, 1].min() > n + 5:
            continue
        ax.plot(a[:, 0], a[:, 1], color="0.25", lw=0.6, zorder=5)
    ax.set_xlim(w, e); ax.set_ylim(s, n)
    ax.set_aspect(1 / math.cos(math.radians((s + n) / 2)))
    step = 5 if e - w <= 40 else 10
    ax.set_xticks(np.arange(math.ceil(w / step) * step, e + 0.1, step)); ax.set_yticks(np.arange(math.ceil(s / step) * step, n + 0.1, step))
    ax.set_xticklabels([f"{int(x)}°E" if x <= 180 else f"{int(360 - x)}°W" for x in ax.get_xticks()], fontsize=7)
    ax.set_yticklabels([f"{int(y)}°N" if y >= 0 else f"{int(-y)}°S" for y in ax.get_yticks()], fontsize=7)
    ax.grid(True, lw=0.3, color="0.7", alpha=0.6)
    for lat, lon in marks:
        ax.plot(lon, lat, marker="*", ms=11, color="gold", mec="k", mew=0.8, zorder=12)
    if title:
        ax.set_title(title, fontsize=9)


def barbs(ax, lon, lat, u, v, st, **kw):
    ax.barbs(lon[::st], lat[::st], u[::st, ::st] * 1.944, v[::st, ::st] * 1.944, length=kw.pop("length", 4.2), linewidth=kw.pop("linewidth", 0.4), color=kw.pop("color", "0.3"), zorder=6, **kw)


# --- figures ---------------------------------------------------------------------

def fig_tracks(case: Case, out: Path, box, marks, plt):
    st = case.storm; T = case.T
    fig, ax = plt.subplots(figsize=(8.2, 6.6))
    setmap(ax, box, case, T["tracks"].format(storm=f"{st['id']} ({st.get('name') or ''})"), marks)
    for k, v in (case.ens or {}).items():
        for t in v["tracks"]:
            pts = [p for p in t["points"] if box[0] - 5 <= p["lat"] <= box[1] + 5 and box[2] - 5 <= p["lon"] <= box[3] + 5]
            if len(pts) >= 3:
                ax.plot([p["lon"] for p in pts], [p["lat"] for p in pts], color=ENS_COLORS.get(k, "0.8"), lw=0.6, alpha=0.8, zorder=3)
        ax.plot([], [], color=ENS_COLORS.get(k, "0.8"), lw=1, label=T["members"].format(m=ENS_NAMES.get(k, k), n=len(v["tracks"])))
    for k, v in st.get("models", {}).items():
        if "members" in v:
            continue
        pts = [p for p in v["points"] if p["lead"] <= 120 * 3600]
        col = MODEL_COLORS.get(k, "0.4")
        ax.plot([p["lon"] for p in pts], [p["lat"] for p in pts], color=col, lw=1.6, zorder=8, label=f"{MODEL_NAMES.get(k, k)} {v.get('run', '')}")
        for p in pts:
            if p["lead"] % 43200 == 0:
                ax.plot(p["lon"], p["lat"], "o", ms=3.5, color=col, zorder=9)
    for ag, v in st.get("agencies", {}).items():
        pts = v["points"]
        ax.plot([p["lon"] for p in pts], [p["lat"] for p in pts], color="k", lw=2.0, zorder=10, label=f"{ag.upper()} #{v.get('number', '')} ({v['issued'][5:16]}Z)")
        for p in pts:
            ax.plot(p["lon"], p["lat"], "s", ms=5, color="k", zorder=11)
            t = datetime.strptime(p["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(case.tz)
            vm = f"\n{p['vmax'] * 1.944:.0f} kt" if p.get("vmax") else ""
            ax.annotate(f"{t.strftime('%d/%H')}{vm}", (p["lon"], p["lat"]), xytext=(6, -4), textcoords="offset points", fontsize=6.5, zorder=12,
                        bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75))
        # 34 kt radii at the point closest to the map's first mark
        if marks:
            near = min((p for p in pts if p.get("radii", {}).get("34")), key=lambda p: C.km(marks[0][0], marks[0][1], p["lat"], p["lon"]), default=None)
            if near:
                r = near["radii"]["34"]; th = np.linspace(0, 2 * np.pi, 181)
                quad = [r[int(a // (np.pi / 2)) % 4] for a in th]
                ys = [near["lat"] + q / 111 * np.cos(a) for q, a in zip(quad, th)]
                xs = [near["lon"] + q / (111 * math.cos(math.radians(near["lat"]))) * np.sin(a) for q, a in zip(quad, th)]
                ax.plot(xs, ys, color="k", lw=0.9, ls="--", zorder=9, label=T["r34"].format(ag=ag.upper(), t=case.local(near["time"][:16] + "Z")))
        break
    ax.legend(loc="lower right", fontsize=7, framealpha=0.9)
    fig.tight_layout(); fig.savefig(out / "fig_tracks.png"); plt.close(fig)


def fig_upper(case: Case, out: Path, box, marks, times, plt):
    T = case.T; srcs = [s for s in case.meta["sources"] if all(case.has(s, "hgt500", t) and case.has(s, "wind250", t) for t in times)]
    if not srcs:
        return
    fig, axs = plt.subplots(len(srcs), len(times), figsize=(5.5 * len(times), 4.1 * len(srcs)), squeeze=False)
    for i, src in enumerate(srcs):
        for j, t in enumerate(times):
            ax = axs[i, j]; z, lat, lon = case.fld(src, "hgt500", "hgt500", t); u, _, _ = case.fld(src, "wind250", "ugrd250", t); v, _, _ = case.fld(src, "wind250", "vgrd250", t)
            setmap(ax, box, case, f"{case.run_label(src)}\n{T['valid'].format(t=case.local(t))}", marks)
            cf = ax.contourf(lon, lat, np.hypot(u, v), levels=[30, 40, 50, 60, 70, 80], cmap="Purples", alpha=0.75, extend="max", zorder=1)
            cs = ax.contour(lon, lat, z, levels=np.arange(4800, 6040, 40), colors="k", linewidths=0.6, zorder=6); ax.clabel(cs, fmt="%d", fontsize=6)
            ax.contour(lon, lat, z, levels=[5880], colors="#c0392b", linewidths=1.6, zorder=7)
            barbs(ax, lon, lat, u, v, max(1, len(lon) // 20))
    fig.colorbar(cf, ax=axs, shrink=0.6, label=T["jet"], pad=0.02); fig.suptitle(T["upper"], fontsize=10)
    fig.savefig(out / "fig_upper.png", bbox_inches="tight"); plt.close(fig)


def fig_verify(case: Case, out: Path, box, marks, plt):
    T = case.T; src = case.meta["sources"][0]; t = case.meta["times"][0]
    if not (case.snd and case.has(src, "hgt500", t)):
        return
    fig, axs = plt.subplots(1, 2, figsize=(12, 5.2))
    for ax, lev in zip(axs, ("500", "850")):
        z, lat, lon = case.fld(src, "hgt500", "hgt500", t)
        bw = "wind250" if lev == "500" else "wind850"
        if not case.has(src, bw, t):
            continue
        u, _, _ = case.fld(src, bw, "ugrd" + bw[4:], t); v, _, _ = case.fld(src, bw, "vgrd" + bw[4:], t)
        setmap(ax, box, case, T["verify" + lev].format(run=case.run_label(src), t=case.local(t)), marks)
        cs = ax.contour(lon, lat, z, levels=np.arange(4800, 6040, 20), colors="0.5", linewidths=0.5, zorder=4); ax.clabel(cs, levels=np.arange(4800, 6040, 40), fmt="%d", fontsize=6)
        ax.contour(lon, lat, z, levels=[5880], colors="#c0392b", linewidths=1.4, zorder=5)
        barbs(ax, lon, lat, u, v, max(1, len(lon) // 24), color="0.55", linewidth=0.35, length=4)
        for w, s in case.snd.items():
            asc = C.ascent_at(s, t)
            if not asc:
                continue
            r = C.level(asc, 50000 if lev == "500" else 85000); rw = C.level(asc, 25000 if lev == "500" else 85000)
            if r is None:
                continue
            ax.plot(s["lon"], s["lat"], "o", ms=3, color="#1f5fbf", zorder=10)
            txt = f"{r['z']}" if lev == "500" else f"{r['t']:.0f}/{asc.get('derived', {}).get('pw', '')}"
            ax.annotate(txt, (s["lon"], s["lat"]), xytext=(4, 3), textcoords="offset points", fontsize=7, color="#1f5fbf", zorder=11, fontweight="bold")
            if rw and rw["wd"] is not None and rw["ws"] is not None:
                ang = math.radians(rw["wd"]); sp = rw["ws"] * 1.944
                ax.barbs([s["lon"]], [s["lat"]], [-sp * math.sin(ang)], [-sp * math.cos(ang)], length=5.5, linewidth=0.9, color="#1f5fbf", zorder=11)
    fig.tight_layout(); fig.savefig(out / "fig_verify.png"); plt.close(fig)


def fig_surface(case: Case, out: Path, box, marks, times, plt):
    from matplotlib.colors import BoundaryNorm, ListedColormap
    T = case.T; cm = ListedColormap(RAIN_COLORS); norm = BoundaryNorm(RAIN_LEVELS, cm.N)
    srcs = [s for s in case.meta["sources"] if all(case.has(s, "prmsl", t) and case.has(s, "wind10m", t) for t in times)]
    if not srcs:
        return
    fig, axs = plt.subplots(len(srcs), len(times), figsize=(5.5 * len(times), 4.5 * len(srcs)), squeeze=False)
    for i, src in enumerate(srcs):
        for j, t in enumerate(times):
            ax = axs[i, j]; p, lat, lon = case.fld(src, "prmsl", "prmsl", t); u, _, _ = case.fld(src, "wind10m", "ugrd10m", t); v, _, _ = case.fld(src, "wind10m", "vgrd10m", t)
            setmap(ax, box, case, f"{case.run_label(src)}\n{T['valid'].format(t=case.local(t))}", marks)
            if case.has(src, "prate", t):
                r, _, _ = case.fld(src, "prate", "prate", t)
                cf = ax.contourf(lon, lat, r, levels=RAIN_LEVELS, cmap=cm, norm=norm, extend="max", zorder=1)
            cs = ax.contour(lon, lat, p, levels=np.arange(900, 1060, 4), colors="k", linewidths=0.6, zorder=6); ax.clabel(cs, fmt="%d", fontsize=6)
            barbs(ax, lon, lat, u, v, max(1, len(lon) // 22), color="0.2")
            ii, kk = np.unravel_index(np.nanargmin(p), p.shape)
            ax.text(lon[kk], lat[ii], "L", fontsize=13, fontweight="bold", color="#c0392b", ha="center", va="center", zorder=12)
            ax.text(lon[kk], lat[ii] - (box[1] - box[0]) / 20, f"{p[ii, kk]:.0f}", fontsize=7, color="#c0392b", ha="center", zorder=12)
            ax.contour(lon, lat, np.hypot(u, v), levels=[17.2], colors="#c0392b", linewidths=1.0, linestyles="--", zorder=7)
    fig.colorbar(cf, ax=axs, shrink=0.6, label=T["rain"], pad=0.02); fig.suptitle(T["surface"], fontsize=10)
    fig.savefig(out / "fig_surface.png", bbox_inches="tight"); plt.close(fig)


def fig_moisture(case: Case, out: Path, box, marks, times, plt):
    T = case.T; t1 = times[0]; t2 = times[-1]
    srcs = [s for s in case.meta["sources"] if case.has(s, "qflux850", t1) and case.has(s, "gust", t2) and case.has(s, "prmsl", t2)]
    if not srcs:
        return
    fig, axs = plt.subplots(len(srcs), 2, figsize=(11, 4.5 * len(srcs)), squeeze=False)
    for i, src in enumerate(srcs):
        ax = axs[i, 0]; qu, lat, lon = case.fld(src, "qflux850", "uqflx850", t1); qv, _, _ = case.fld(src, "qflux850", "vqflx850", t1)
        setmap(ax, box, case, f"{T['moist'].format(run=case.run_label(src))}\n{T['valid'].format(t=case.local(t1))}", marks)
        cf = ax.contourf(lon, lat, np.hypot(qu, qv), levels=[10, 15, 20, 30, 40, 60, 80], cmap="YlGnBu", extend="max", zorder=1)
        st = max(1, len(lon) // 22)
        ax.quiver(lon[::st], lat[::st], qu[::st, ::st], qv[::st, ::st], scale=900, width=0.0025, color="0.25", zorder=6)
        if case.has(src, "thetae850", t1):
            te, _, _ = case.fld(src, "thetae850", "thetae850", t1)
            cs = ax.contour(lon, lat, te, levels=np.arange(280, 370, 4), colors="#8e0d8e", linewidths=0.6, zorder=5); ax.clabel(cs, fmt="%d", fontsize=6)
        ax = axs[i, 1]; g, lat, lon = case.fld(src, "gust", "gust", t2); p, _, _ = case.fld(src, "prmsl", "prmsl", t2)
        setmap(ax, box, case, f"{T['gust'].format(run=case.run_label(src))}\n{T['valid'].format(t=case.local(t2))}", marks)
        cg = ax.contourf(lon, lat, g, levels=[10, 15, 20, 25, 30, 35, 40, 50], cmap="OrRd", extend="max", zorder=1)
        cs = ax.contour(lon, lat, p, levels=np.arange(900, 1060, 4), colors="k", linewidths=0.5, zorder=6); ax.clabel(cs, fmt="%d", fontsize=6)
    fig.colorbar(cf, ax=axs[:, 0], shrink=0.6, label=T["qflux"], pad=0.02); fig.colorbar(cg, ax=axs[:, 1], shrink=0.6, label=T["gustcb"], pad=0.02)
    fig.savefig(out / "fig_moisture_gust.png", bbox_inches="tight"); plt.close(fig)


def fig_meteogram(case: Case, out: Path, name, mark, window, span, plt):
    import matplotlib.dates as mdates
    T = case.T; pt = case.point; lat, lon = case.meta["point"]
    fig, axs = plt.subplots(4, 1, figsize=(10, 10.5), sharex=True)
    t0 = t1 = None
    for src in case.meta["point_sources"]:
        fr = C.point_frame(pt, src, case.tzh)
        if not fr:
            continue
        col = MODEL_COLORS.get(src, "0.4"); lab = case.run_label(src); t = fr["t"]
        t0 = min(t0, t[0]) if t0 else t[0]; t1 = max(t1, t[-1]) if t1 else t[-1]
        axs[0].plot(t, fr["p"], color=col, lw=1.4, label=lab)
        axs[1].plot(t, fr["ws"], color=col, lw=1.4, label=lab + T["w10"]); axs[1].plot(t, fr["gust"], color=col, lw=1.0, ls="--", label=lab + T["gustl"])
        axs[2].plot(t, fr["rate"], color=col, lw=1.4, label=lab, drawstyle="steps-post")
        axs[3].plot(t, C.accumulate(t, fr["rate"]), color=col, lw=1.4, label=lab)
    axs[0].set_ylabel(T["mslp"]); axs[1].set_ylabel(T["wind"]); axs[2].set_ylabel(T["rate"]); axs[3].set_ylabel(T["accum"])
    axs[1].axhline(17.2, color="0.4", lw=0.6, ls=":"); axs[1].text(0.01, 17.6, T["gale"], fontsize=7, color="0.3", transform=axs[1].get_yaxis_transform())
    axs[1].axhline(10.8, color="0.6", lw=0.6, ls=":"); axs[1].text(0.01, 11.2, T["strong"], fontsize=7, color="0.4", transform=axs[1].get_yaxis_transform())
    for ax in axs:
        if window:
            ax.axvspan(C.parse_time(window[0]).astimezone(case.tz), C.parse_time(window[1]).astimezone(case.tz), color="gold", alpha=0.25, lw=0)
        if mark:
            ax.axvline(C.parse_time(mark).astimezone(case.tz), color="k", lw=1.0, ls="-.")
        ax.grid(True, lw=0.3, alpha=0.6); ax.legend(fontsize=7, loc="upper left", ncol=2 if ax is axs[1] else 1)
    if mark:
        m = C.parse_time(mark).astimezone(case.tz); axs[0].text(m, axs[0].get_ylim()[0] + 1, T["mark"].format(t=m.strftime("%d %H:%M")), fontsize=8, va="bottom")
    axs[0].set_title(T["meteo"].format(name=name, lat=lat, lon=lon, tz=case.tzh), fontsize=9)
    axs[3].xaxis.set_major_formatter(mdates.DateFormatter("%d/%H", tz=case.tz)); axs[3].xaxis.set_major_locator(mdates.HourLocator(byhour=[0, 6, 12, 18], tz=case.tz) if ((C.parse_time(span[1]) - C.parse_time(span[0])).days if span else (t1 - t0).days) <= 5 else mdates.DayLocator(tz=case.tz))
    if span:
        t0, t1 = (C.parse_time(x).astimezone(case.tz) for x in span)
    plt.setp(axs[3].get_xticklabels(), rotation=45, ha="right", fontsize=7); axs[3].set_xlim(t0, t1)
    fig.tight_layout(); fig.savefig(out / "fig_meteogram.png"); plt.close(fig)


def skewt(ax, asc: dict, title: str, T: dict, skew=35.0):
    p, z, tt, td, wd, ws = C.profile(asc)
    X = lambda temp, pp: temp + skew * (np.log(1000) - np.log(pp))  # noqa: E731
    for T0 in range(-100, 60, 10):
        ax.plot([X(T0, 1000), X(T0, 100)], [np.log(1000), np.log(100)], color="0.85", lw=0.5, zorder=1)
    for th in range(250, 460, 20):
        pp = np.linspace(1000, 100, 50); ax.plot(X(th * (pp / 1000) ** 0.2857 - 273.15, pp), np.log(pp), color="#e8c9a0", lw=0.5, zorder=1)
    y = np.log(p)
    ax.plot(X(tt, p), y, color="#c0392b", lw=1.6, zorder=5, label="T"); ax.plot(X(td, p), y, color="#2e8b57", lw=1.6, zorder=5, label="Td")
    ticks = [1000, 925, 850, 700, 500, 400, 300, 250, 200, 150, 100]
    ax.set_yticks(np.log(ticks)); ax.set_yticklabels(ticks, fontsize=7); ax.set_ylim(np.log(1050), np.log(100)); ax.set_xlim(-40, 45)
    ax.set_xticks(range(-40, 50, 10)); ax.set_xticklabels(range(-40, 50, 10), fontsize=7); ax.set_xlabel(T["skewt_x"], fontsize=8); ax.set_ylabel("hPa", fontsize=8)
    sel = [i for i in range(len(p)) if not np.isnan(wd[i]) and not np.isnan(ws[i]) and (p[i] in ticks or i % 6 == 0)]
    for i in sel:
        ang = math.radians(wd[i]); sp = ws[i] * 1.944
        ax.barbs([41], [y[i]], [-sp * math.sin(ang)], [-sp * math.cos(ang)], length=5, linewidth=0.7, color="0.2", zorder=6, clip_on=False)
    d = asc.get("derived", {})
    ax.set_title(title, fontsize=9)
    ax.text(0.02, 0.02, T["pw"].format(pw=d.get("pw"), fl=d.get("freezingLevel")) + "\n" + T["lapse"].format(lr=d.get("lapse850_500"), tp=d.get("tropopause")),
            transform=ax.transAxes, fontsize=7, va="bottom", bbox=dict(fc="white", ec="0.7", alpha=0.9))
    ax.legend(fontsize=7, loc="upper right")


def fig_skewt(case: Case, out: Path, stations, time, plt):
    snd = case.snd; ids = stations or list(snd)[:3]
    picks = [(w, C.ascent_at(snd[w], time) if time else max(snd[w]["soundings"], key=lambda a: a["time"])) for w in ids if w in snd]
    picks = [(w, a) for w, a in picks if a]
    if not picks:
        return
    fig, axs = plt.subplots(1, len(picks), figsize=(4.7 * len(picks), 5.6), squeeze=False)
    for ax, (w, a) in zip(axs[0], picks):
        s = snd[w]
        skewt(ax, a, f"{s.get('name') or w} ({s['lat']:.1f}°N {s['lon']:.1f}°E)\n{case.local(C.norm_time(a['time']), '%Y-%m-%d %H')}", case.T)
    fig.tight_layout(); fig.savefig(out / "fig_skewt.png"); plt.close(fig)


def fig_nowcast(case: Case, out: Path, marks, plt):
    from matplotlib.colors import BoundaryNorm, ListedColormap
    T = case.T; nc = case.now; meta = case.meta["nowcast"]; panels = [k for k in ("sat", "radar") if k in nc.files]
    if not panels:
        return
    fig, axs = plt.subplots(1, len(panels), figsize=(6.5 * len(panels), 5.6), squeeze=False)
    for ax, k in zip(axs[0], panels):
        a = nc[k]; lat = nc[f"{k}:lat"]; lon = nc[f"{k}:lon"]; m = meta[k]
        box = (float(lat.min()), float(lat.max()), float(lon.min()), float(lon.max()))
        setmap(ax, box, case, T[k].format(src=m["source"], var=m["variable"], t=m["time"][:16], loc=case.local(m["time"])), marks)
        step = max(1, a.shape[0] // 800)
        if k == "sat":
            im = ax.pcolormesh(lon[::step], lat[::step], a[::step, ::step], cmap="gray_r", vmin=190, vmax=300, shading="auto", zorder=0)
            ax.contour(lon[::step], lat[::step], a[::step, ::step], levels=[210, 230], colors=["#8e0d8e", "#1f5fbf"], linewidths=0.6, zorder=3)
            if case.storm:
                for ag, v in case.storm.get("agencies", {}).items():
                    p0 = v["points"][0]; ax.plot(p0["lon"], p0["lat"], "x", ms=9, mew=2, color="#c0392b", zorder=10)
                    ax.text(p0["lon"] + 0.5, p0["lat"] + 0.2, T["centre"].format(id=case.storm["id"], ag=ag.upper(), t=p0["time"][5:13] + "Z"), fontsize=7, color="#c0392b", zorder=10); break
            fig.colorbar(im, ax=ax, shrink=0.75, label="K")
        else:
            cm = ListedColormap(RAIN_COLORS); norm = BoundaryNorm(RAIN_LEVELS, cm.N)
            aa = np.where(a[::step, ::step] <= 0.1, np.nan, a[::step, ::step])
            if m["variable"] == "cref":
                im = ax.pcolormesh(lon[::step], lat[::step], np.where(a[::step, ::step] < 5, np.nan, a[::step, ::step]), cmap="turbo", vmin=5, vmax=70, shading="auto", zorder=2); fig.colorbar(im, ax=ax, shrink=0.75, label="dBZ")
            else:
                im = ax.pcolormesh(lon[::step], lat[::step], aa, cmap=cm, norm=norm, shading="auto", zorder=2); fig.colorbar(im, ax=ax, shrink=0.75, label="mm/h")
    fig.tight_layout(); fig.savefig(out / "fig_nowcast.png"); plt.close(fig)


def fig_ensemble(case: Case, out: Path, name, t_from, t_to, mark, plt):
    T = case.T; lat, lon = case.meta["point"]
    res = C.ensemble_spread(case.ens, lat, lon, [], t_from, t_to, box=case.meta.get("box"))
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.2))
    hours = []
    for k, r in res.items():
        ds = [c["dist_km"] for c in r["closest"]]; ts = [C.parse_time(c["time"]).astimezone(case.tz) for c in r["closest"]]
        if not ds:
            continue
        lab = f"{ENS_NAMES.get(k, k)} {r['run']} (n={len(ds)}/{r['members']})"
        axs[0].hist(ds, bins=np.arange(0, max(ds) + 50, 50), alpha=0.5, color=MODEL_COLORS.get(k.replace("ens", "").replace("gefs", "gfs"), "0.5"), label=lab)
        h = [t.timestamp() / 3600 for t in ts]; hours += h
        axs[1].hist(h, bins=np.arange(math.floor(min(h) / 6) * 6, max(h) + 6, 6), alpha=0.5, color=MODEL_COLORS.get(k.replace("ens", "").replace("gefs", "gfs"), "0.5"), label=lab)
    axs[0].set_xlabel(T["ens_d"].format(name=name)); axs[0].set_ylabel(T["ens_n"]); axs[0].legend(fontsize=7); axs[0].grid(alpha=0.4, lw=0.3)
    axs[1].set_xlabel(T["ens_t"].format(tz=case.tzh)); axs[1].legend(fontsize=7); axs[1].grid(alpha=0.4, lw=0.3)
    if hours:
        ticks = np.arange(math.floor(min(hours) / 6) * 6, max(hours) + 6, 6)
        axs[1].set_xticks(ticks); axs[1].set_xticklabels([datetime.fromtimestamp(h * 3600, tz=timezone.utc).astimezone(case.tz).strftime("%d/%H") for h in ticks], rotation=45, ha="right", fontsize=7)
    if mark:
        axs[1].axvline(C.parse_time(mark).timestamp() / 3600, color="k", ls="-.", lw=1)
    fig.suptitle(T["ens_title"].format(name=name), fontsize=9)
    fig.tight_layout(); fig.savefig(out / "fig_ensemble.png"); plt.close(fig)


# --- CLI ------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir"); ap.add_argument("--out"); ap.add_argument("--tz", type=float, default=0); ap.add_argument("--lang", default="zh", choices=list(L))
    ap.add_argument("--figures", default="all"); ap.add_argument("--map-times", help="two valid times for the map figures (default: first and last of the case)")
    ap.add_argument("--map-box", type=float, nargs=4, metavar=("S", "N", "W", "E"), help="map extent (default: the case box)")
    ap.add_argument("--point-name", default="point"); ap.add_argument("--mark", help="a valid time to mark on the meteogram and ensemble timing")
    ap.add_argument("--window", help="T1,T2 shaded on the meteogram"); ap.add_argument("--span", help="T1,T2 x-limits of the meteogram (default: the whole axis)"); ap.add_argument("--skewt", help="WMO ids for the skew-T panel"); ap.add_argument("--skewt-time")
    ap.add_argument("--ens-from"); ap.add_argument("--ens-to"); ap.add_argument("--font", default="Hiragino Sans GB,Noto Sans CJK SC,Arial Unicode MS,DejaVu Sans")
    a = ap.parse_args(argv)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = a.font.split(","); plt.rcParams["axes.unicode_minus"] = False; plt.rcParams["figure.dpi"] = 130
    import logging; logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)

    case = Case(Path(a.dir), a.tz, a.lang)
    out = Path(a.out) if a.out else Path(a.dir) / "figures"; out.mkdir(parents=True, exist_ok=True)
    box = tuple(a.map_box) if a.map_box else tuple(case.meta["box"]) if case.meta.get("box") else None
    marks = [tuple(case.meta["point"])] if case.meta.get("point") else []
    times = [C.norm_time(t) for t in a.map_times.split(",")] if a.map_times else ([case.meta["times"][0], case.meta["times"][-1]] if len(case.meta.get("times", [])) > 1 else case.meta.get("times", []))
    want = set(a.figures.split(",")) if a.figures != "all" else {"tracks", "upper", "verify", "surface", "moisture", "meteogram", "skewt", "nowcast", "ensemble"}
    window = a.window.split(",") if a.window else None
    done = []
    if "tracks" in want and case.storm and box:
        fig_tracks(case, out, box, marks, plt); done.append("tracks")
    if "upper" in want and case.fields is not None and times:
        fig_upper(case, out, box, marks, times, plt); done.append("upper")
    if "verify" in want and case.fields is not None and case.snd:
        fig_verify(case, out, box, marks, plt); done.append("verify")
    if "surface" in want and case.fields is not None and times:
        fig_surface(case, out, box, marks, times, plt); done.append("surface")
    if "moisture" in want and case.fields is not None and times:
        fig_moisture(case, out, box, marks, times, plt); done.append("moisture")
    if "meteogram" in want and case.point is not None:
        fig_meteogram(case, out, a.point_name, a.mark, window, a.span.split(",") if a.span else None, plt); done.append("meteogram")
    if "skewt" in want and case.snd:
        fig_skewt(case, out, a.skewt.split(",") if a.skewt else None, a.skewt_time, plt); done.append("skewt")
    if "nowcast" in want and case.now is not None:
        fig_nowcast(case, out, marks, plt); done.append("nowcast")
    if "ensemble" in want and case.ens and case.meta.get("point"):
        fig_ensemble(case, out, a.point_name, a.ens_from, a.ens_to, a.mark, plt); done.append("ensemble")
    print(f"wrote {out}: {', '.join(done)}")


if __name__ == "__main__":
    main()

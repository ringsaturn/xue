---
name: synoptic-analysis
description: Analyse the current and forecast weather situation (synoptic analysis, 天气形势分析) for any place or region from Xue's public STAC catalog at dataset.ringsaturn.me/xue — live GFS, ECMWF IFS, AIFS, IFS HRES and HRRR runs as Zarr stores, radar and geostationary satellite windows, radiosonde soundings, METAR/TAF and tropical cyclone tracks. Use this whenever the user asks what the weather pattern is or will be, about a trough, ridge, subtropical high, front, jet stream, typhoon/hurricane, cold surge, heat wave, monsoon or heavy-rain outlook, wants a forecast discussion or briefing for a city/region/date, wants models compared at a point, wants the latest radar/satellite/sounding read, or asks how to read the xue catalog or its stores — even when they do not say "synoptic", "STAC" or "Zarr".
---

# Synoptic analysis from the Xue catalog

The catalog is a static STAC 1.1.0 tree over live numerical forecasts,
observations and point products, all plain files with open CORS. Nothing is
interpolated or pre-analysed for you: you read fields, find the systems,
and write the discussion. The bundled scripts do the reading and the
routine diagnostics so the analysis, not the plumbing, gets the effort.

## Setup

Python 3.11+ with `numpy xarray "zarr>=3" fsspec aiohttp requests`
(`uv pip install …` or `pip install …`), plus `matplotlib` for the figures.
`pystac` is optional. In this repository install into `.venv` and run with
`.venv/bin/python`; a `uv sync` drops these extras, so reinstall after one.
Check with:

```sh
python scripts/xue_stac.py sources        # every Collection, its live run and axis
```

Paths below are relative to this skill's directory. Each store read is a
few HTTP range requests; a full `region` report at two valid times takes
about a minute and a half, a `meteogram` 10–30 s per model.

## Workflow

### 1. Turn the question into a box, a set of valid times, and sources

- **Region**: south, north, west, east in degrees. For a city, use about
  ±15° (synoptic scale, the scale of the systems that make its weather);
  for a national-scale outlook 30–45° wide. A box crossing the
  antimeridian is spelled with east < west on a global model.
- **Times**: valid times in UTC. The user's local time matters for the
  answer; convert both ways and state the zone. For "the next few days"
  read 00Z and 12Z of each day; for an event, the hours around it.
- **Sources**: `references/sources.md` says what each source carries, its
  resolution, cycle and lead. Default to GFS *and* ECMWF for a synoptic
  outlook, IFS HRES for local surface detail, HRRR for the US next 18 h,
  AIFS/IFS HRES beyond day 10. Note the run's age: `item` shows
  `reference` (the cycle); a run is 4–8 h old when it lands.

### 2. Read the situation

```sh
python scripts/synoptic_report.py region gfs --box 20 50 105 150 \
    --times 2026-09-20T00Z,2026-09-21T00Z --json /tmp/gfs.json
python scripts/synoptic_report.py region ecmwf --box 20 50 105 150 --times …
```

Per valid time it prints: MSLP range and centres (L/H with prominence,
strongest gradient), 500 hPa lows/ridges and the 5880 gpm reach, the 250 hPa
jet maximum, the 850 hPa thermal gradient (frontal zones), θe850 maximum
and gradient, the 850 hPa moisture flux maximum and its direction, RH700
shield area, ω700/ω500 ascent, CAPE, rain rate, 10 m wind, gust, 2 m
temperature range. Centres are sorted by depth; the weak ones at the end
are noise on a quantized field, name only what has a closed circulation
or a gradient behind it.

For a place, add the point view and the model spread:

```sh
python scripts/synoptic_report.py meteogram ecmwf 35.7 139.7 --hours 120
python scripts/synoptic_report.py compare 35.7 139.7 --sources gfs,ecmwf,aifs,ifshres
```

For the present state (nowcast, verification of the analysis):

```sh
python scripts/xue_stac.py storms                       # every tracked TC with headline
python scripts/xue_stac.py storm WP242026               # best track + agency + model forecasts
python scripts/xue_stac.py sounding 47646               # newest ascent, fixed point (references/sources.md)
python scripts/xue_stac.py airport RJTT                 # 24 h of METARs and the TAF
python scripts/xue_stac.py box himawari ir104 20 45 120 150   # cloud-top statistics (K)
python scripts/xue_stac.py box mrms cref 30 45 -100 -80 --time 2026-09-19T05:00Z
```

Anything else is a few lines of xarray through `xue_stac.open_bundle`,
`sel_box`, `sel_time`, `sel_point` (`references/stac-access.md` has the
store facts: latitude runs north→south, `prate` needs the log codebook,
disks past the antimeridian, cost per read). Read the `full` tier for
numbers.

### 2b. Save the case and run the diagnostics offline

For anything beyond a quick look (a written report, figures, a second pass
later) fetch once into a directory and work from it; every diagnostic and
figure below then costs no network and the numbers stay consistent across
the report.

A case lives in one directory named by its date and a short slug,
`tmp/<yyyy-mm-dd>-<case>/` in this repository (`tmp/` is gitignored;
`2026-09-19-tokyo-typhoon`, `2026-09-19-dust-case`): the fetched data under
`case/`, the figures under `figures/` (`figures-<lang>/` when the report is
written in more than one language, since the labels are localized), and beside them the report itself as
`<yyyy-mm-dd>-<case>.<lang>.md` with its PDF `<yyyy-mm-dd>-<case>.<lang>.pdf`
— the same stem as the directory, the language tag being the viewer's locale
code (`zh`, `en`, `ja`, …), so `tmp/2026-09-19-tokyo-typhoon/2026-09-19-tokyo-typhoon.ja.md`.
Never name the report `report.md`. The examples below abbreviate the
directory to `<dir>`:

```sh
python scripts/case_data.py fetch <dir>/case --box 22 48 125 155 \
    --times 2026-09-19T00Z,2026-09-21T06Z,2026-09-21T12Z --sources gfs,ecmwf \
    --point 35.553 139.781 --point-sources gfs,ecmwf,ifshres \
    --storm WP242026 --soundings-near 35.55,139.78,10 --soundings 47971,54857 \
    --airport RJTT --satellite himawari --radar jma --nowcast-box 30 40 132 146
python scripts/case_data.py soundings <dir>/case --tendency      # mandatory levels per station + 12 h changes
python scripts/case_data.py verify <dir>/case                    # model analysis at the stations vs the ascents
python scripts/case_data.py ensemble <dir>/case 35.553 139.781 --times 2026-09-21T06Z,2026-09-21T12Z \
    --from 2026-09-20T12Z --to 2026-09-22T12Z                   # member spread and closest approach
python scripts/case_data.py point <dir>/case --tz 9 --day 2026-09-21   # daily summary + one day hourly, local zone
python scripts/case_plots.py <dir>/case --out <dir>/figures --tz 9 --lang zh --point-name 羽田 \
    --map-times 2026-09-21T06Z,2026-09-21T12Z --map-box 26 44 130 150 \
    --mark 2026-09-21T08Z --window 2026-09-21T03Z,2026-09-21T08Z --span 2026-09-19T00Z,2026-09-22T12Z \
    --skewt 47646,47678,47971 --ens-from 2026-09-20T12Z --ens-to 2026-09-22T12Z
```

The first valid time should be the run's analysis frame (or the ascents'
time) so `verify` and the `verify` figure compare like with like; the map
figures use `--map-times` (default: the first and last fetched). The
directory layout and the importable functions (`level`, `ascent_at`,
`profile`, `decode_ensembles`, `ensemble_spread`, `point_frame`, `daily`,
`accumulate`) are documented in `case_data.py`'s docstring. `case_plots.py`
writes `fig_tracks`, `fig_upper`, `fig_verify`, `fig_surface`,
`fig_moisture_gust`, `fig_meteogram`, `fig_skewt`, `fig_nowcast` and
`fig_ensemble` (PNG) into `--out` (default `<case dir>/figures/`), each only when the directory
holds what it needs; labels follow the xue viewer's eleven UI locales (`--lang zh|zh-Hant|en|ja|ko|de|fr|es|pt|tr|ru`,
`zh` by default), times in the `--tz` zone. The report that references those files
compiles with `pandoc <yyyy-mm-dd>-<case>.<lang>.md -o <yyyy-mm-dd>-<case>.<lang>.pdf --pdf-engine=xelatex`
from the case directory; for
Chinese text put `documentclass: ctexart` in the YAML header (ctex's default
fonts), for Japanese `documentclass: bxjsarticle` with `classoption: [pandoc,
ja=standard, jafont=hiragino-pron]`, and map the few glyphs Latin Modern lacks with `newunicodechar`
(`→ ≥ ≤ θ µ ½`; ctex's STSong also lacks the katakana middle dot `・`, map it to
`·`), and read the build log for `Missing character` before shipping a PDF.

### 3. Analyse top-down, then attribute every claim

Synoptic method, in this order, because the upper pattern explains the
surface and the surface explains the weather:

1. **500 hPa**: long-wave pattern (zonal or meridional), trough and ridge
   axes, cut-off lows, the subtropical high's 5880 line and western ridge
   point, blocking. Compare two times to get movement and deepening.
2. **Jet (250 hPa)**: position of the streak relative to the surface low;
   left-exit / right-entrance ascent.
3. **Surface**: the lows and highs, their pressure tendency between the
   valid times, the gradient (wind), the fronts from the 850 hPa
   temperature and θe gradients and the wind shift.
4. **Moisture and forcing**: where the 850 hPa moisture flux points, the
   RH700 shield, ω ascent, CAPE and the 850–500 lapse; this is where the
   rain and storms will be.
5. **Sensible weather**: rain totals from the meteogram accumulation (not
   peak rates), temperature extremes, wind and gust, visibility, with
   thresholds from `references/variables.md`.
6. **Observations**: does the newest sounding, METAR, radar or IR image
   agree with the run's analysis frame? A disagreement lowers confidence.
7. **Spread**: where GFS and ECMWF disagree on a centre's position by more
   than ~300 km, its depth by more than ~5 hPa, or a day's rain by a factor
   of two, say so and lean on ECMWF for days 3–7 unless the observations
   side with GFS.

Every statement names its field, level, valid time and model
("ECMWF 12Z run: 500 hPa trough axis 125°E at 00Z Saturday, digging to
5580 gpm"). Round to the codebook step (`references/variables.md`). Say
"about" for anything from a 0.25° grid inside 100 km. Do not read a TC's
central pressure or peak wind from the model grid; take them from the `tc`
product and use the grid for the environment (steering flow from the
5880 line, shear from wind250 vs wind850, moisture from qflux850).

### 4. Write the report

Write it to `tmp/<yyyy-mm-dd>-<case>/<yyyy-mm-dd>-<case>.<lang>.md` (§2b),
one file per language asked for. Use this shape; drop a section only when
nothing applies.

```
# Synoptic situation: <region>, <period> (issued <now UTC / local>)
Data: <model runs with cycle times>, <observations with their times>

## Overview            — 3–5 sentences: the pattern, the systems that matter, the headline hazard
## Upper-level pattern — 500 hPa and jet, with movement between the valid times
## Surface systems     — centres, fronts, pressure tendency, wind regimes
## Moisture, forcing, instability — where and when it rains or storms, and why
## Day by day          — a table: day (local) | sky | precipitation (mm) | T min/max | wind | hazards
## <Place> outlook     — only when a point was asked: the daily meteogram summary
## Confidence          — model agreement, run age, observation check, what would change the forecast
## Sources             — runs, products, attribution lines from references/sources.md, and viewer links
```

Viewer links let the reader see the maps: `https://xue.ringsaturn.me/?model=ecmwf&type=precip&lines=hgt500#map=4/35/135`,
`?type=wind850`, `?type=thetae850`, `?model=himawari&type=infrared`,
`?type=temp&tc=<storm id>`. Put one or two next to the sections they show.

## Pitfalls

- Runs are replaced within hours and the old URL 404s: fetch the live
  Item at the start of a task and reuse its hrefs; do not reuse a run
  directory from an earlier conversation.
- An observation Item's `datetime` is the window's first hour; the
  newest frame is `end_datetime`. Satellite disks that cross the
  antimeridian use longitudes above 180.
- `prate` is a rate per step; total it with `accumulate_mm`, and read a
  peak rate after the step change (GFS F120, ECMWF F144) as a step mean.
  ECMWF/AIFS/IFS HRES precipitation, gust and radiation have no analysis
  frame.
- ω clamps at ±6.35 Pa/s, visibility at 25.4 km, CAPE at 6350 J/kg; a
  value at the clamp is "at least".
- A quantized field has plateaus: a "maximum" on a 1 hPa MSLP plateau has
  no precise position. Quote the plateau's extent or its centre as
  approximate.
- The JMA nowcast publishes classes, not measured rates; MRMS repeats a
  frame until the next scan.
- HRRR's rectangle has off-domain corners that repeat the edge cell.
- Sounding values are fixed point (K×100, m/s×10, Pa); wind direction is
  where the wind comes from, everywhere in the catalog.
- Attribution is a licence condition for ECMWF, EUMETSAT and JMA data;
  put the lines in the Sources section of anything the user will publish.

## References

- `references/sources.md` — every source: fields, grid, cadence, latency, model choice, attribution
- `references/variables.md` — each variable's meaning, unit, precision, precipitation semantics, diagnostic thresholds
- `references/stac-access.md` — the document layout, Item fields, opening stores by hand, Range reads of the point products
- `scripts/xue_stac.py` — catalog + store + point-product reader (library and CLI)
- `scripts/synoptic_report.py` — `region`, `meteogram`, `compare` diagnostics
- `scripts/case_data.py` — fetch one case into a directory; `soundings`, `verify`, `ensemble`, `point` diagnostics on it
- `scripts/case_plots.py` — the standard figure set from a case directory

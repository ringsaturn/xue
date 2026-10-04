# Sources in the Xue catalog

Root: `https://dataset.ringsaturn.me/xue/catalog.json`. One Collection per
source at `<source>/collection.json`; its live run is `<source>/item.json`
(stable path, hrefs reaching into `../<source>.<run>/`). Only the newest run
per source is online; superseded runs are deleted within minutes to hours,
so never cache a run URL across a session.

## Forecast models

| id | model | grid | cycles | axis (frames) | lands after cycle | isobaric levels |
|---|---|---|---|---|---|---|
| `gfs` | NOAA GFS | 0.25° global, 1440×721 | 00/06/12/18Z | hourly to F120, 3-hourly to F240 (161) | ~4 h | 925/850/700/500/250 (see below) |
| `ecmwf` | ECMWF IFS open data | 0.25° global | 00/06/12/18Z | 3-hourly to F144, 6-hourly to F240 (65) | ~7–8 h | same set |
| `aifs` | ECMWF AIFS Single (data-driven) | 0.25° global | 00/06/12/18Z | 6-hourly to F360 (61) | ~5.5 h | same set, no RH |
| `ifshres` | ECMWF IFS HRES 9 km via Open-Meteo | 0.1° global, 3600×1801 | 00/12Z only | hourly to F90, 3-hourly to F144, 6-hourly to F360 (145) | ~6.5 h | none |
| `sflux` | GFS surface fluxes | ~0.117° Gaussian | 00/06/12/18Z | as GFS | ~4 h | none; only `tmp2m prate dswrf wind10m` |
| `hrrr` | NOAA HRRR 3 km | 0.03° over CONUS, 2441×1051, no wrap | every hour | hourly to F18 (19) | ~1.5 h | 925/850/700/500 heights and temps |

Bundles (store keys) per model:

- **GFS** (37): `tmp2m prate prmsl hgt850 hgt700 hgt500 hgt250 tmp925 tmp850 tmp500 rh850 rh700 rh500 gust tcdc lcdc mcdc hcdc cape vis dpt2m aptmp2m vvel850 vvel700 vvel500 thetae850 tmpsfc icec icetk htsgw perpw` + vectors `wind10m wind925 wind850 wind250 qflux850 wave`.
- **ECMWF** (31): GFS less `lcdc mcdc hcdc vis aptmp2m icec`. Its `gust` is the interval maximum ending at the frame and has no analysis frame; `cape` is most-unstable CAPE; `perpw` is the peak period.
- **AIFS** (27): ECMWF less `rh850 rh700 rh500 gust cape icetk perpw`, plus `lcdc mcdc hcdc`. `prate` is a six-hour mean rate.
- **IFS HRES** (15): `tmp2m prate dswrf prmsl gust tcdc lcdc mcdc hcdc cape vis dpt2m tmpsfc icetk wind10m`. `prate`, `dswrf`, `gust` have no analysis frame (series starts at F1).
- **SFLUX** (4): `tmp2m prate dswrf wind10m` (the only source with solar radiation).
- **HRRR** (22): `tmp2m prate prmsl hgt850 hgt700 hgt500 tmp925 tmp850 tmp500 gust tcdc lcdc mcdc hcdc cape vis dpt2m cref` + `wind10m wind925 wind850 wind250`. `cref` is *forecast* composite reflectivity. Corners of the rectangle outside the Lambert domain repeat the edge cell and are not forecast.

Which model to use:

- Global synoptic pattern out to 10 days: GFS (hourly, most fields) and
  ECMWF (usually the better medium-range skill). Read both and report where
  they disagree; that is the confidence statement.
- Beyond day 10: only AIFS and IFS HRES reach F360. AIFS is smoother
  (data-driven) and has no RH or CAPE; IFS HRES has no upper air.
- Surface detail (coastal wind, temperature in terrain, precipitation
  gradients): IFS HRES at 0.1°, and HRRR at 3 km over the US for the next
  18 h.
- Solar radiation: SFLUX only.

## Observations (rolling windows, `xue:observation: true`)

| id | what | grid | cadence | window | latency |
|---|---|---|---|---|---|
| `mrms` | NOAA MRMS radar mosaic, CONUS: `cref` (dBZ), `prate` (mm/h) | 0.02°, 130°W–60°W, 20–55°N | 2 min (scans every 4–6 min) | last 3–4 h | ~3 min |
| `jma` | JMA precipitation nowcast analysis, Japan: `prate` only, class representative rates 0.5/3/7.5/15/25/40/65/100 mm/h | 0.005°, 121–149°E, 20.5–45.5°N | 5 min | last 2–3 h | minutes |
| `cma` | CMA radar mosaic, China: `cref` | 0.0439°, 67.5–146.25°E, 11.25–56.25°N | 6 min | last 2–3 h | 20–30 min |
| `himawari` | Himawari-9 AHI (140.7°E): `ir104` (K), `dustrgb` (3 guns), `dustcf` | 0.04°, 80.7°E–200.7°E (past 180), 60°S–60°N | 10 min | last 5–6 h | 15–20 min |
| `goeseast` | GOES-19 ABI (75.2°W): same three | 0.04°, 135.2°W–15.2°W | 10 min | last 5–6 h | ~15 min |
| `goeswest` | GOES-18 ABI (137°W): same three | 0.04°, 163°E–283°E (past 180) | 10 min | last 5–6 h | ~15 min |
| `meteosat` | Meteosat-12 FCI (0°): `ir104`, `dustrgb` | 0.04°, 60°W–60°E | **hourly** | last 24 h | ~1 h |

An observation Item's `datetime` / `xue:runTime` is the window's *first*
hour, not an analysis time; the `time` coordinate is the observation time.
The window's newest frame is `end_datetime`. Rolling-window Items live one
directory deeper (`mrms.<run>/<HHMM>/`), which the live Item's hrefs
already handle. Satellite stores carry longitudes past 180 on the disks
that cross the antimeridian; select with 180–360 longitudes there.

## Point products

| id | what | files | cadence |
|---|---|---|---|
| `sounding` | radiosonde ascents from the WIS2 GTS gateways, ~490 stations, thinned TEMP levels | `index.json` (stations, headline t500/td500/freezing level/PW, byte spans), `soundings.jsonl` | hourly issue, ascents at 00/12Z |
| `airport` | METAR (24 h) + TAF from NOAA AWC, ~5500 airports | `index.json` (16-value rows), `history.jsonl` | every 10 min |
| `tc` | tropical cyclone tracks: best tracks (IBTrACS + NHC/JTWC working), agency forecasts, GFS/ECMWF/GEFS/ENS model tracks | `index.json` (storms), `<storm id>.json` | hourly |

A station's line is one HTTP Range request (`offset`/`length` in the
index); `xue_stac.py` does it. Sounding values are fixed point: `p` Pa,
`z` gpm, `t`/`td` K×100, `ws` m/s×10, `−32768` missing. TC `vmax` is m/s
(1-min for NHC/JTWC, 10-min for other agencies), `pmin` hPa; storm ids are
ATCF (`WP242026`) or `x-<basin>-<hour>-<n>` for unnumbered systems.

## Showcase

`showcase/collection.json` lists historical cases (heat dome 2021, Ida
2021, quad-state tornado 2021, ...). Each case Item has the same asset shape
as a run, `xue:eventTime`, `xue:tags`, and an English `description` written
by the maintainer. Use a case only when the user asks about that past event.

## Attribution (required in any published output)

NOAA GFS / HRRR / MRMS / GOES: public domain, name the source. ECMWF open
data and AIFS: CC BY 4.0, "Contains modified ECMWF open data". IFS HRES:
"Adapted from ECMWF IFS by ECMWF, licensed under CC BY 4.0", via
Open-Meteo. JMA nowcast: 出典：気象庁ホームページ, regridded and
reclassified. Himawari: JMA imagery distributed by NOAA. Meteosat: "Contains
modified EUMETSAT Meteosat data <year>". CMA radar: CMA / NMC product.
Soundings: WMO Unified Data Policy. Airports: NWS. Tracks: the agencies'
own terms plus CC BY 4.0. None of the agencies endorses the derivative.

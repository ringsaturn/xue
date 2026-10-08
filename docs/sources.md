# Sources

What each source publishes. Every source is an independent dataset under
`<model>.<run>/` on the data root, taken live by its pointer
(`latest.json` for GFS, `latest-<model>.json` for the rest). The id in each
heading is the `--model` value and the `?model=` value. Time axes with
mixed steps are listed outright in the bundle metadata
([`format.md`](format.md), schema v3).

The geostationary mosaic (`?model=geo`) is a view, not a dataset: the
viewer opens whichever imager runs answer, clips each disk at the
midpoints between sub-satellite longitudes and plays them on one timeline
by valid time.

Every level of the isobaric families (1000 / 925 / 850 / 700 / 500 / 300 /
250 / 200 hPa) is registered; publishing one more is a source-table change,
not a format change.

## Forecasts

### NOAA GFS (`gfs`)

0.25° global, hourly to F120 and 3-hourly to F240, four cycles a day.

- 2 m temperature, precipitation rate, categorical precipitation type
  (derived from the rain / freezing-rain / ice-pellet / snow flags), 10 m
  and 100 m wind.
- Surface diagnostics: gust, total / low / middle / high cloud cover, CAPE
  and CIN, visibility, 2 m dew point, apparent temperature, precipitable
  water, boundary layer height, the model orography (one static frame).
- Vertical velocity at 850 / 700 / 500 hPa, 850 hPa equivalent potential
  temperature (derived).
- Ocean: skin temperature, sea ice cover and thickness, significant wave
  height and primary period (GFS-Wave), and a derived wave vector (height
  laid along the direction of travel).
- Mean sea level pressure; geopotential height, temperature, relative
  humidity and wind on all eight isobaric surfaces; 850 hPa water vapour
  flux (`q·V/g`, derived).

### ECMWF IFS open data (`ecmwf`, alias `ifs`)

0.25° global, 3-hourly to F144 and 6-hourly to F240. The subset of the GFS
set that open data carries, under the same identities so a model switch
keeps the layer. `tp` is differenced into an interval-mean rate and the gust
is an interval maximum, so both series start at F003. Not available: layer
cloud covers, visibility, sea ice cover, apparent temperature.

### ECMWF AIFS Single (`aifs`)

0.25° global, 6-hourly to F360 from every cycle, landing about 5.5 h after
it. ECMWF's data-driven model from the same open data service, including
the layer clouds IFS open data lacks. No isobaric relative humidity, gust,
CAPE, ice thickness or wave period.

### ECMWF IFS HRES via Open-Meteo (`ifshres`, aliases `hres`, `ifs9km`)

0.1° global, hourly to F090, 3-hourly to F144, 6-hourly to F360, from the
00Z and 12Z cycles only (complete about 6.5 h after the cycle). ECMWF's
~9 km operational forecast as Open-Meteo forwards it, resampled once from
the reduced Gaussian O1280 grid (nearest neighbour) by
[om2nc](https://github.com/ringsaturn/om2nc). Fifteen surface bundles with
the GFS ids and charts: 2 m temperature and dew point, precipitation, 10 m
wind, shortwave radiation, sea level pressure, gust, four cloud covers,
CAPE, visibility, skin temperature, sea ice thickness. No isobaric levels,
no waves. Precipitation, radiation and gust have no analysis frame.

### GFS surface flux (`sflux`)

~13 km Gaussian global, the GFS axis. 2 m temperature, precipitation
(de-averaged from window means, from F001), 10 m wind and instantaneous
downward shortwave radiation.

### NOAA HRRR (`hrrr`)

0.03° contiguous US, hourly to F18, a cycle every hour. The core pair, sea
level pressure, 850 / 700 / 500 hPa heights, 925 / 850 / 500 hPa
temperature, 10 m / 925 / 850 / 250 hPa winds, the surface diagnostics and
forecast composite reflectivity (`cref`). The Lambert conformal model grid
is resampled onto a regular 0.03° grid (bilinear); the viewer clips to the
model's footprint.

### NOAA GEFS-Aerosols (`gefsaero`)

0.25° global, 3-hourly to F120, complete about 6 h after the cycle. Aerosol
optical depth at 550 nm in total (`aod`) and for dust, sea salt, sulphate,
organic and black carbon (`aoddust`, `aodsalt`, `aodsulf`, `aodorg`,
`aodbc`), and surface PM2.5, PM10 and dust PM10 in µg/m³ (`pm25`, `pm10`,
`pm10dust`). The aerosol type and size / wavelength intervals are written
as the `aerosol` block of the bundle metadata.

### NCEP CFSv2 (`cfs`)

Seasonal: ensemble member 01 of the 00Z and 12Z cycles, 6-hourly from F6 to
F6552 (39 weeks, 1092 frames), landing about 11:30Z and 23:25Z. Surface
fields on the T126 Gaussian grid (0.9375°): the core pair, total cloud
cover, shortwave radiation, surface temperature, two sea ice fields and
10 m wind. Upper air on a regular 1° grid: sea level pressure, height,
temperature, 500 hPa vertical velocity, wind at seven levels and water
vapour flux at four. Precipitation, radiation and cloud cover are six-hour
means ending at the frame (`typeOfStatisticalProcessing` 0 in the
metadata).

## Observations (rolling windows)

A rolling window is rebuilt every few minutes; each build is a round under
`<model>.<run>/<HHMM>/`.

### NOAA MRMS radar (`mrms`)

0.02° contiguous US, every 2 min, a 4 h window. Composite reflectivity
(`cref`) and radar precipitation rate (`prate`), thinned two to one by
block maximum.

### JMA precipitation nowcast (`jma`)

0.005° over Japan (121–149°E, 20.5–45.5°N), every 5 min, a 3 h window. The
agency's high-resolution precipitation nowcast (高解像度降水ナウキャスト)
publishes intensity classes, not reflectivity, so it ships `prate` alone at
each class's representative rate (0.5, 3, 7.5, 15, 25, 40, 65, 100 mm/h).
Fetched through [jma-radar](https://github.com/ringsaturn/jma-radar).

### CMA radar mosaic (`cma`)

0.0439° over China (67.5–146.25°E, 11.25–56.25°N), every 6 min, a 3 h
window, ending 20–30 min behind real time. The level-3 composite
reflectivity mosaic (RADAR_L3_MST_CREF) as `cref`, read from an archive
named by `XUE_CMA_ARCHIVE`.

### Geostationary imagers (`himawari`, `goeseast`, `goeswest`, `meteosat`)

| Source | Spacecraft | Disk | Cadence and window |
|---|---|---|---|
| `himawari` | Himawari-9 at 140.7°E (NOAA `noaa-himawari9`) | 80.7–200.7°E | 10 min, 6 h |
| `goeseast` | GOES-19 at 75.2°W (`noaa-goes19`) | 135.2°W–15.2°W | 10 min, 6 h |
| `goeswest` | GOES-18 at 137°W (`noaa-goes18`) | 163–283°E | 10 min, 6 h |
| `meteosat` | Meteosat-12 at 0° (EUMETSAT Data Store) | 60°W–60°E | hourly, 24 h |

All on a 0.04° plate carrée grid, 60°S–60°N, with half, quarter and eighth
reduced tiers. Each publishes:

- `ir104`: 10.4 µm brightness temperature (180–332 K at 0.6 K; shown in
  °C).
- `dustrgb`: the Dust RGB composite (red 12.3 − 10.4 µm, green 11.2 −
  8.6 µm, blue 10.4 µm), computed per scan by
  [shachen](https://github.com/ringsaturn/shachen); dust reads pink to
  magenta, day and night. Code 0 means no data in every gun. FCI has no
  11.2 µm channel, so Meteosat's green gun is 10.5 − 8.7 µm.
- `dustcf` (not Meteosat): the DEBRA dust confidence (Miller et al. 2017),
  0–1, over water and inside staged emissivity regions (the Gobi through
  Japan, the US Southwest, the Saharan corridor). See
  [`satellite.md`](satellite.md).

The live windows end 15–20 min behind real time. **Meteosat publishes only
the cycle on each hour**: EUMETSAT releases that cycle under CC BY 4.0 and
the cycles between under terms that do not allow this use. Building it
needs `EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET` (free at
<https://api.eumetsat.int/api-key>).

### NOAA SWPC aurora (`aurora`)

1° global (360 × 181), every 5 min, a 12 h window. The OVATION model's
probability (percent) that aurora is visible overhead. A frame's valid
time is the model's forecast time, about an hour after the solar-wind
observation driving it, so the window is a sequence of successive
forecasts. The feed carries only its newest grid, so the window grows from
a frame cache.

## Point products

Not runs, but published beside them and drawn as marks in the viewer:
tropical cyclone tracks ([`tc.md`](tc.md), hourly), airport METAR / TAF
([`airport.md`](airport.md), every ten minutes) and radiosonde soundings
([`sounding.md`](sounding.md), hourly).

## Archive depth

A showcase case can only be cut where the upstream archive reaches: GFS and
sflux to about 2021-01, ECMWF open data to about 2024-02, Himawari-9's
ISatSS tiles to 2022-12. See [`showcase/README.md`](../showcase/README.md).

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).

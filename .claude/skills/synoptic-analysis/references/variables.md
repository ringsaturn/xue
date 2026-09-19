# Variables: meaning, unit, precision, thresholds

Every store array is quantized uint8. `xue_stac.open_store` returns physical
units (float32, NaN = no data). Precision below is the codebook step, which
is also the smallest difference worth reporting: do not quote a 2 m
temperature to a tenth, or an MSLP to a tenth of a hPa.

| id | quantity | unit | step | range | notes |
|---|---|---|---|---|---|
| `tmp2m` | 2 m temperature | °C | 0.5 | −60..50 | |
| `dpt2m` | 2 m dew point | °C | 0.5 | −70..40 | dew-point depression < 2 °C: fog/low cloud risk |
| `aptmp2m` | apparent temperature | °C | 1 | −90..60 | GFS only |
| `tmpsfc` | skin temperature (SST over sea) | °C | 0.5 | | |
| `prmsl` | mean sea level pressure | hPa | 1 | 870.5..1124.5 | codebook is offset half a hPa so standard isobars fall between codes |
| `prate` | precipitation rate | mm/h | log: ~5 % of value, 0.01 trace, 128 cap | | see semantics below |
| `gust` | 10 m wind gust | m/s | 0.5 | 0..127 | GFS: instantaneous; ECMWF/IFS HRES: max over the step |
| `tcdc lcdc mcdc hcdc` | total/low/mid/high cloud cover | % | 1 | 0..100 | |
| `cape` | CAPE (ECMWF: most-unstable) | J/kg | 25 | 0..6350 | |
| `vis` | visibility | km | 0.1 | 0..25.4 | 25.4 is the cap, read as "unlimited" |
| `hgt850 hgt700 hgt500 hgt250` | geopotential height | gpm | 6/6/8/12 | | contour interval 30/30/60/120 gpm in analyses |
| `tmp925 tmp850 tmp500` | temperature | °C | 0.5 | | |
| `rh850 rh700 rh500` | relative humidity | % | 1 | | |
| `vvel850 vvel700 vvel500` | vertical velocity ω | Pa/s | 0.05 | −6.35..6.35 | negative = ascent; ±6.35 is the clamp |
| `thetae850` | equivalent potential temperature (Bolton 1980) | K | 0.5 | 230..357 | |
| `icec` | sea ice cover | % | 1 | | GFS only |
| `icetk` | sea ice thickness | m | 0.02 | | land = codebook bottom |
| `htsgw` | significant wave height | m | 0.1 | | land = 0 |
| `perpw` | primary wave period | s | 0.1 | | |
| `dswrf` | downward shortwave | W/m² | 5 | | sflux, ifshres |
| `cref` | composite reflectivity | dBZ | 0.5 | 0..80 | radar (observed) or HRRR (forecast) |
| `ir104` | 10.4 µm brightness temperature | K | 0.6 | 180..331.8 | show in °C; cold = high cloud top |
| `dustr dustg dustb` | Dust RGB guns | 1 | 0.004 | 0..1 | code 0 = no data |
| `dustcf` | DEBRA dust confidence | 1 | 0.004 | 0..1 | 0 = no dust evidence; defined over water and staged land only |

Vector bundles carry two arrays each (u east-positive, v north-positive):

| bundle | arrays | unit | step | derived |
|---|---|---|---|---|
| `wind10m` | `ugrd10m vgrd10m` | m/s | 0.5 | speed, direction *from* = (270 − atan2(v,u)) mod 360 |
| `wind925 wind850 wind250` | `ugrd<p> vgrd<p>` | m/s | 1 | |
| `qflux850` | `uqflx850 vqflx850` | g/(cm·hPa·s) | 0.5 | moisture flux q·V/g at 850 hPa |
| `wave` | `uwave vwave` | m | 0.2 | height along direction of travel; height = hypot, direction from = atan2(−u, −v) |

## Precipitation semantics (read before summing)

`prate` at a frame is the rate valid over the *step ending at that frame*
on every model that de-accumulates (ECMWF `tp`, AIFS `tp`, IFS HRES
interval totals) and an instantaneous rate on GFS/HRRR/SFLUX (GFS's is the
last hour's average in practice). To total precipitation, multiply each
frame's rate by the step preceding it and sum (`synoptic_report.accumulate_mm`).
Where the step changes (GFS 1 h → 3 h at F120, ECMWF 3 → 6 h at F144) the
rate is the mean over the longer step and the totals stay right; the
*peak* rate reads lower there. A de-accumulated series has no analysis
frame. Radar `prate` (MRMS) and the JMA class rates are observations, and a
JMA value is a class representative, not a measurement.

## Diagnostics and thresholds

Rules of thumb for mid-latitude synoptic analysis; adjust for season and
latitude and say so when a threshold is borderline.

- **Pressure centres.** A low worth naming is ≥ 3 hPa below its
  surroundings; < 980 hPa mid-latitude is a deep cyclone, < 950 is
  exceptional. Pressure gradient ≥ 5 hPa/100 km implies gale-force
  gradient wind at sea. An intense TC will show as the MSLP minimum but the
  0.25° grid under-resolves its core: quote the `tc` product's `pmin`/`vmax`
  for the storm itself, the model field for its environment.
- **500 hPa.** Troughs and ridges by the height field; a closed contour at
  the minimum is a cut-off low. The 5880 gpm contour bounds the western
  Pacific subtropical high in summer (its western ridge point steers
  typhoons and sets the Meiyu/Baiu front). 5640 gpm and below at 30°N is a
  cold vortex. Height falls of ≥ 60 gpm/12 h ahead of a trough are strong
  forcing.
- **Jet.** 250 hPa wind ≥ 30 m/s is the jet; ≥ 50 m/s a strong streak.
  Ascent is favoured in the left exit and right entrance regions (NH).
- **Fronts and thermal structure.** 850 hPa temperature gradient
  ≥ 1.5 K/100 km marks a baroclinic zone; ≥ 3 is a sharp front. The
  θe850 gradient locates the moist frontal boundary (Meiyu front, dryline);
  a θe850 ≥ 340 K tongue is warm-sector moist air; θe decreasing with
  height (lapse850_500 from soundings, or tmp850 − tmp500 ≥ 30 K) is
  convective instability.
- **Moisture transport.** |qflux850| ≥ 15 g/(cm·hPa·s) marks a strong
  low-level jet / atmospheric river; ≥ 30 is extreme. Its direction says
  where the moisture comes from (a southwesterly monsoon surge, a
  southeasterly ahead of a typhoon).
- **Forcing.** ω700 ≤ −0.3 Pa/s is strong synoptic ascent; ≤ −1 is
  convective-scale (and clamps at −6.35). RH700 ≥ 80 % is the mid-level
  cloud/precipitation shield; RH500 ≥ 70 % deep moisture.
- **Instability.** CAPE 1000–2500 J/kg moderate, ≥ 2500 strong. Pair with
  the 850–500 lapse and the low-level moisture (dpt2m ≥ 20 °C in summer).
- **Precipitation.** ≥ 10 mm/h heavy; 50 mm/3 h or 100 mm/24 h are
  warning-class totals in East Asia. Report totals over the window, not
  peak rates alone.
- **Wind.** 10 m ≥ 17.2 m/s gale (Beaufort 8), ≥ 24.5 storm (10), ≥ 32.7
  hurricane (12). Gusts run 1.3–1.6× the mean over land.
- **Heat/cold.** tmp2m ≥ 35 °C with dpt2m ≥ 24 °C is dangerous heat;
  tmp850 ≥ 20 °C in summer is a heat-dome signature; tmp850 ≤ −10 °C in
  winter at 35°N is a strong cold-air outbreak.
- **Satellite.** ir104 ≤ −50 °C cloud tops are deep convection (overshoots
  ≤ −70 °C); a warm ring/eye in a TC; the Dust RGB's magenta is lofted
  dust, dark red thick ice cloud.
- **Radar.** cref ≥ 40 dBZ convective cores, ≥ 50 hail/severe likely;
  lines and bow echoes read off the field's shape.

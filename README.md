# 雪 / Xue

[![test](https://github.com/ringsaturn/xue/actions/workflows/test.yml/badge.svg)](https://github.com/ringsaturn/xue/actions/workflows/test.yml)
[![docs](https://github.com/ringsaturn/xue/actions/workflows/docs-rust.yml/badge.svg)](https://ringsaturn.github.io/xue/)

> Xue (雪, pronounced /ɕɥɛ/, roughly "shweh"), Chinese for snow.

**Live weather models, radar and satellite, played back in the browser:
<https://xue.ringsaturn.me>**

- Forecasts from GFS, ECMWF IFS and AIFS, HRRR and CFSv2: temperature,
  precipitation, wind particles, isobars, upper-air charts, clear-air
  turbulence; skew-T soundings and point meteograms anywhere.
- Observations: MRMS, NEXRAD, JMA and CMA radar; Himawari, GOES and
  Meteosat imagery with dust products; METAR, soundings, typhoon tracks.
- A globe with terrain, 3D radar volumes, and replays of past events
  ([showcase](https://xue.ringsaturn.me/showcase.html)).

The title in the top-left corner switches between sources.

There is no server behind it. Every run is a set of per-variable Zarr v3
stores laid out for playback (quantized single-byte planes, small spatial
tiles × six consecutive steps per chunk, one shard per variable;
[`docs/zarr-profile.md`](docs/zarr-profile.md)) with a STAC catalog,
published as static files on a CDN. The page reads them with HTTP Range
requests: a Rust WebAssembly worker decodes chunks on demand, and a WebGL2
layer projects and colours them on the GPU. The same stores open in
Python with `xarray` or the `xuepy` wheel. Code is MIT or Apache-2.0.
The earlier single-file container, `.xue` ([`docs/format.md`](docs/format.md)),
is no longer published and every decoder still reads it.

## Sources

| Source | Grid · cadence | Newest run (UTC) |
|---|---|---|
| NOAA GFS (`gfs`) | 0.25° global · hourly to F120, 3-hourly to F240 | <a href="https://dataset.ringsaturn.me/xue/latest.json"><img alt="the newest gfs run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest.json&query=%24.runTime&label=&color=0b7cbd&style=flat-square&cacheSeconds=600" width="170"></a> |
| GFS surface flux (`sflux`) | ~13 km Gaussian global · as GFS | <a href="https://dataset.ringsaturn.me/xue/latest-sflux.json"><img alt="the newest sflux run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-sflux.json&query=%24.runTime&label=&color=2b6cb0&style=flat-square&cacheSeconds=600" width="170"></a> |
| ECMWF IFS open data (`ecmwf`) | 0.25° global · 3-hourly to F144, 6-hourly to F240 | <a href="https://dataset.ringsaturn.me/xue/latest-ecmwf.json"><img alt="the newest ecmwf run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-ecmwf.json&query=%24.runTime&label=&color=1f6f8b&style=flat-square&cacheSeconds=600" width="170"></a> |
| ECMWF AIFS Single (`aifs`) | 0.25° global · 6-hourly to F360 | <a href="https://dataset.ringsaturn.me/xue/latest-aifs.json"><img alt="the newest aifs run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-aifs.json&query=%24.runTime&label=&color=3c7a89&style=flat-square&cacheSeconds=600" width="170"></a> |
| ECMWF IFS HRES via Open-Meteo (`ifshres`) | 0.1° global · hourly to F090, then 3- and 6-hourly to F360; 00Z/12Z | <a href="https://dataset.ringsaturn.me/xue/latest-ifshres.json"><img alt="the newest ifshres run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-ifshres.json&query=%24.runTime&label=&color=2a9d8f&style=flat-square&cacheSeconds=600" width="170"></a> |
| NOAA HRRR (`hrrr`) | 0.03° contiguous US · hourly to F18, hourly cycles | <a href="https://dataset.ringsaturn.me/xue/latest-hrrr.json"><img alt="the newest hrrr run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-hrrr.json&query=%24.runTime&label=&color=7b4ea3&style=flat-square&cacheSeconds=600" width="170"></a> |
| Recast WOOF-WRF (`woof`) | 0.005° (500 m) nest over Mount Fuji · hourly from F1 | showcase cases only, no live run |
| NOAA GEFS-Aerosols (`gefsaero`) | 0.25° global · 3-hourly to F120 | <a href="https://dataset.ringsaturn.me/xue/latest-gefsaero.json"><img alt="the newest gefsaero run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-gefsaero.json&query=%24.runTime&label=&color=8a6d3b&style=flat-square&cacheSeconds=600" width="170"></a> |
| NCEP CFSv2 (`cfs`) | T126 Gaussian / 1° global · 6-hourly F6–F6552 (39 weeks); 00Z/12Z | <a href="https://dataset.ringsaturn.me/xue/latest-cfs.json"><img alt="the newest cfs run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-cfs.json&query=%24.runTime&label=&color=4f7942&style=flat-square&cacheSeconds=600" width="170"></a> |
| NOAA MRMS radar (`mrms`) | 0.02° contiguous US · 2 min, rolling 4 h | <a href="https://dataset.ringsaturn.me/xue/latest-mrms.json"><img alt="the newest mrms run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-mrms.json&query=%24.runTime&label=&color=b7472a&style=flat-square&cacheSeconds=600" width="170"></a> |
| JMA precipitation nowcast (`jma`) | 0.005° Japan · 5 min, rolling 3 h | <a href="https://dataset.ringsaturn.me/xue/latest-jma.json"><img alt="the newest jma run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-jma.json&query=%24.runTime&label=&color=c25b2c&style=flat-square&cacheSeconds=600" width="170"></a> |
| CMA radar mosaic (`cma`) | 0.0439° China · 6 min, rolling 3 h | <a href="https://dataset.ringsaturn.me/xue/latest-cma.json"><img alt="the newest cma run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-cma.json&query=%24.runTime&label=&color=b03a5b&style=flat-square&cacheSeconds=600" width="170"></a> |
| Himawari-9 (`himawari`) | 0.04° disk at 140.7°E · 10 min, rolling 6 h | <a href="https://dataset.ringsaturn.me/xue/latest-himawari.json"><img alt="the newest himawari run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-himawari.json&query=%24.runTime&label=&color=6d597a&style=flat-square&cacheSeconds=600" width="170"></a> |
| GOES-19 East (`goeseast`) | 0.04° disk at 75.2°W · 10 min, rolling 6 h | <a href="https://dataset.ringsaturn.me/xue/latest-goeseast.json"><img alt="the newest goeseast run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-goeseast.json&query=%24.runTime&label=&color=5c677d&style=flat-square&cacheSeconds=600" width="170"></a> |
| GOES-18 West (`goeswest`) | 0.04° disk at 137°W · 10 min, rolling 6 h | <a href="https://dataset.ringsaturn.me/xue/latest-goeswest.json"><img alt="the newest goeswest run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-goeswest.json&query=%24.runTime&label=&color=5c677d&style=flat-square&cacheSeconds=600" width="170"></a> |
| Meteosat-12 (`meteosat`) | 0.04° disk at 0° · hourly, rolling 24 h | <a href="https://dataset.ringsaturn.me/xue/latest-meteosat.json"><img alt="the newest meteosat run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-meteosat.json&query=%24.runTime&label=&color=4a5d8f&style=flat-square&cacheSeconds=600" width="170"></a> |
| NOAA SWPC aurora (`aurora`) | 1° global · 5 min, rolling 12 h | <a href="https://dataset.ringsaturn.me/xue/latest-aurora.json"><img alt="the newest aurora run" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdataset.ringsaturn.me%2Fxue%2Flatest-aurora.json&query=%24.runTime&label=&color=2e7d6f&style=flat-square&cacheSeconds=600" width="170"></a> |

The last column reads the source's live pointer: a forecast's cycle, or
the hour a rolling window starts at. Variables, grids and caveats per
source are in [`docs/sources.md`](docs/sources.md). `?model=geo` is a
mosaic of the geostationary imagers, a view rather than a dataset.

## Quick start

Requirements: Python ≥ 3.12 with [uv](https://docs.astral.sh/uv/), Node.js
≥ 22, a Rust toolchain with `wasm-pack`, and GDAL ≥ 3.8 with the GRIB
driver. Some sources need more (eccodes, om2nc, ffmpeg, credentials); see
[`docs/contribution/setup.md`](docs/contribution/setup.md).

```sh
uv sync                   # creates .venv with NumPy and the xuepy wheel
make check                # verifies tool versions
make mvp                  # builds the latest GFS run and the frontend
make mvp MODEL=ecmwf      # or another forecast: aifs, ifshres, sflux, hrrr, gefsaero, cfs
make serve                # serves the result on 127.0.0.1
```

The public NOAA and ECMWF buckets need no credentials.

## Using the data

Everything is static files on `https://dataset.ringsaturn.me/xue/`, read
with HTTP Range requests.

- **STAC catalog**: `catalog.json` at the data root, one Collection per
  source with a stable live Item, an Item per run, and the showcase cases
  ([`docs/stac.md`](docs/stac.md)). The root's `index.html` lists them.
- **Zarr stores**: each bundle is `<bundle>.zarr/`, a standard Zarr v3
  store with CF `scale_factor` / `add_offset` / `_FillValue` and
  `time` / `latitude` / `longitude` coordinates, so `xarray.open_zarr`
  reads physical values ([`docs/zarr-profile.md`](docs/zarr-profile.md)).

  ```python
  import pystac, xarray as xr
  run = next(pystac.Catalog.from_file("https://dataset.ringsaturn.me/xue/catalog.json").get_child("gfs").get_items())
  ds = xr.open_zarr(run.assets["tmp2m"].get_absolute_href())
  ```

- **Data API** (experimental: no compatibility, reliability or freshness
  guarantee): a read-only JSON API at <https://xue-api.ringsaturn.me>
  (`/v1/catalog`, `/v1/sources/{source}`,
  `/v1/point?source=&lat=&lon=&variables=` with an optional ISO 8601
  `time` or a pinned `run`; OpenAPI at `/openapi.json`; contract in
  [`docs/api.md`](docs/api.md)). The site does not depend on it.
- **Decoders**: `pip install xuepy` (`import xue`) and the
  [`xue` crate](https://crates.io/crates/xue)
  ([API docs](https://ringsaturn.github.io/xue/)) read both containers.
- **Point products**: tropical cyclone tracks ([`docs/tc.md`](docs/tc.md)),
  airport METAR / TAF ([`docs/airport.md`](docs/airport.md)) and
  radiosonde soundings ([`docs/sounding.md`](docs/sounding.md)), each a
  pointer, an index and one file per issue.

## URL state

| Parameter | Values |
|---|---|
| `model` | a source id from the table (aliases such as `ifs`, `hres`, `radar`, `goes-east`), or `geo` |
| `type` | the field: `tmp2m`, `prate`, `wind10m`, `solar`, `radar`, `hgt500`, `tmp850` / `t850`, `rh700`, `qflux850` / `vapor850`, … (each isobaric field names its own level) |
| `lines` | a pressure surface drawn as contours over the field (`pressure`, `hgt500`) |
| `case` | a showcase case id; pins its dataset and run |
| `res` | `half` (alias `low`): smallest reduced tier; `full` (alias `high`): full resolution |
| `particles` | `off` hides the wind particles (also remembered per browser) |
| `stations` | `snd`, `apt`, `snd,apt` or `off`: sounding and airport marks |
| `tc`, `tcagency`, `tcmodel`, `tcmembers` | a tropical cyclone (`<id>` or `off`) and which tracks to draw |
| `backend` | `xue` reads the `.xue` container where a run still ships one |
| `use_h264` | `true` opts into the WebCodecs H.264 companions |
| `lang`, `theme` | one of eleven locales; `light` or `dark` |

Values are case-insensitive and unrecognized ones fall back to defaults.
The camera is in the fragment, `#map=<zoom>/<lat>/<lon>`; a link without
one opens on the dataset's region. Clicking the map pins a point: the
field's value, a meteogram from the same run, a skew-T from a radiosonde
within 150 km and the METARs and TAF of an airport within 40 km.

## Comparing models at a point

`/compare.html?lat=<deg>&lon=<deg>` lays every live forecast model at one
point on one clock: a block per model, a row per field
(`?fields=tmp2m,prate,wind10m`), five days hourly or fifteen days
three-hourly (`?span=360`), with the nearest airport's METARs above. Each
value is the model's nearest grid cell, never interpolated. The viewer's
point probe links here.

## Historical showcase

Cases are past weather events cropped to their region and hours, listed at
`/showcase.html` and played at `/?case=<id>`. A case is a checked-in JSON
file under `showcase/cases/`, built with `make showcase CASE=<id>` and
published permanently. See [`showcase/README.md`](showcase/README.md).

## Documentation

- Specifications: [`docs/format.md`](docs/format.md) (container and
  bundle metadata), [`docs/zarr-profile.md`](docs/zarr-profile.md),
  [`docs/stac.md`](docs/stac.md), [`docs/api.md`](docs/api.md) (data API),
  [`docs/encoder.md`](docs/encoder.md)
  (native encoder), [`docs/satellite.md`](docs/satellite.md),
  [`docs/tc.md`](docs/tc.md), [`docs/airport.md`](docs/airport.md),
  [`docs/sounding.md`](docs/sounding.md).
- Sources: [`docs/sources.md`](docs/sources.md).
- Developer documentation: [`docs/contribution/`](docs/contribution/README.md)
  (architecture, pipeline, setup, testing, publishing, releasing).
- Contributing: [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Data and Licensing

Three things are licensed separately.

- **Code** is dual-licensed under MIT and Apache-2.0
  ([LICENSE-MIT](LICENSE-MIT) / [LICENSE-APACHE](LICENSE-APACHE)); use
  either at your option.
- **Specifications**, the documents under [`docs/`](docs/README.md) (the
  container, the Zarr profile, the STAC catalog, the point products, the
  data API), are licensed under [CC BY 4.0](LICENSE-CC-BY), as the Zarr
  specifications are. Implement them in anything; credit "the Xue
  specification (github.com/ringsaturn/xue)".
- **Published data** on `https://dataset.ringsaturn.me/xue/` (the stores,
  the catalog, pointers, indexes and point products) is derived from the
  sources below and keeps each source's terms, which every STAC Collection
  names under `license` and `providers`. Xue's own contribution to those
  objects (the layout, the quantization, the catalog) is licensed under
  [CC BY 4.0](LICENSE-CC-BY): credit the source as it asks and add "via
  Xue (xue.ringsaturn.me)", for example *Contains modified ECMWF open data
  (CC BY 4.0), via Xue*.

The sources. Weather data comes from
[NOAA GFS](https://registry.opendata.aws/noaa-gfs-bdp-pds/),
[NOAA GEFS](https://registry.opendata.aws/noaa-gefs-bdp-pds/) (the
GEFS-Aerosols member) and
[NCEP CFSv2](https://registry.opendata.aws/noaa-cfs/) (all three works of
the United States government and so in the public domain; NOAA requests
attribution and does not endorse this use)
and [ECMWF open data](https://www.ecmwf.int/en/forecasts/datasets/open-data),
the IFS and the AIFS (CC BY 4.0, © European Centre for Medium-Range Weather
Forecasts; this project distributes converted derivatives: "Contains
modified ECMWF open data"). The IFS HRES surface fields (`ifshres`) are
ECMWF's ~9 km operational forecast as [Open-Meteo](https://open-meteo.com)
forwards it under CC BY 4.0: "Adapted from ECMWF IFS by ECMWF, licensed
under CC BY 4.0", via Open-Meteo. Neither ECMWF nor Open-Meteo endorses
this use.

The other sources:

- HRRR, MRMS, GOES-East / GOES-West imagery and the SWPC OVATION aurora
  model are NOAA products, works of the United States government in the
  public domain; NOAA requests attribution and does not endorse this use.
- JMA precipitation nowcast: Source: Japan Meteorological Agency website
  (出典：気象庁ホームページ), regridded and reclassified by
  [jma-radar](https://github.com/ringsaturn/jma-radar).
- CMA radar mosaic: a product of the China Meteorological Administration
  (中国气象局国家气象中心, www.nmc.cn).
- Himawari-9 imagery is the Japan Meteorological Agency's, distributed by
  NOAA; reprojected here, no endorsement implied by either agency.
- Meteosat: Contains modified EUMETSAT Meteosat data 2026 (the year of
  distribution is stamped wherever the attribution is shown). Only the
  hourly Level 1 cycle, released by EUMETSAT under CC BY 4.0, is
  published; no endorsement implied.
- Radiosonde soundings are WMO core data under the WMO Unified Data Policy
  — free and unrestricted, attribution of the original source requested.
  The originating national meteorological and hydrological services are
  the source of every ascent; the WMO WIS2 Global Cache is the
  distribution.
- Airport METARs and TAFs come from the NOAA Aviation Weather Center's
  cache files (public domain).

The basemap is [Protomaps](https://protomaps.com)-hosted vector tiles,
© [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.

## Acknowledgments

This project is built with [Claude Code](https://claude.com/claude-code),
supported by a Claude Max (20x) subscription provided through Anthropic's
[Claude for OSS](https://claude.com/contact-sales/claude-for-oss) program.
Thank you.

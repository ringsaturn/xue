# Changelog

Changes to the published contract: the specifications under `docs/`, the
formats and their schema versions, the catalog and the data API. Code
releases are the `v*` tags (the `xue` crate and the `xuepy` wheel); their
changes are in `git log`.

## 2026-10-11

- New source `woof` (manifest model `WOOF-WRF`, product `nest`): a WRF-ARW
  nest run on Recast's WOOF service, read from its `wrfout` files by the
  `xue wrf-series` tool into CF series on a regular 0.005° grid and
  published only as showcase cases (no live pointer). Hourly from f001;
  `tmp2m`, `dpt2m`, `tmpsfc`, `wind10m`, `wind80m`, `prate`, `tcdc`,
  `lcdc`/`mcdc`/`hcdc` (cloud fraction in 0–2 / 2–6 / >6 km MSL bands),
  `hpbl`, `dswrf`, `orog`, `prmsl` and the `cloud3d` volume.
- New volume bundle `cloud3d`: the cloud water mixing ratio (GRIB2 0/1/22,
  g/kg) on 24 altitudes from 250 m to 12 km, variables `clw<m>` in the
  `volume-registry.json` codebook (linear, 0–2.53 g/kg); the same shape as
  `refl3d`. Below the model terrain the value is 0, not a no-data code.
- Showcase case definitions take an optional `view.volume`, a volume
  bundle among the case's variables that the case opens drawn over its
  field; `?volume=` on the link outranks it.
- The viewer's `?contours=on` draws elevation contour lines cut in the
  browser from the terrain tiles; `?volume=<bundle>` draws a volume over
  the field; the MP4 export of the loop is new beside the GIF.

- The data API reads the point products (`docs/api.md` § Point products):
  `/v1/soundings[/{station}]`, `/v1/airports[/{icao}]`,
  `/v1/synop[/{station}]` and `/v1/storms[/{storm}]`, each a list of the
  live issue's stations and one station's record sliced out of the
  product's file by byte range. Lists take `lat`/`lon` (nearest first,
  `distanceKm`), `radius`, `bbox` and `limit`; records take `time`
  (nearest); every route takes `issue=` to pin an issue. Soundings' levels
  and storms' ensembles are decoded from fixed point. `/v1/sources/{source}`
  gains `kind` and `endpoint`; `/v1/point` on a point product is `400
  not_a_grid`. New error codes `unknown_issue`, `unknown_station`,
  `unknown_network`, `unknown_storm`.

## 2026-10-08

- New product: the agriculture indicators (`indicators/`, `docs/indicators.md`,
  `schemaVersion` 1): crop-area-weighted daily temperature, precipitation,
  dry and hot area fractions and degree days over sixteen soybean regions,
  one line per forecast run appended to a monthly file and indexed by byte
  range; the weights are a Zarr v3 store per grid.
- HRRR publishes `wind80m`, the 80 m wind pair (GRIB2 surface type 103,
  value 80), beside `wind10m`; a new bundle id, no schema change.
- Series companion stores on HRRR (`tmp2m`, `wind10m`, `gust`, `tcdc`,
  `wind80m`) and sflux (`tmp2m`, `dswrf`, `wind10m`), as on GFS.
- Licensing stated in three parts: the code MIT / Apache-2.0, the
  specifications CC BY 4.0 (`LICENSE-CC-BY`), the published data under each
  source's terms with Xue's own contribution CC BY 4.0.
- `docs/README.md` lists the specifications, the versions in use and the
  compatibility rules they follow.

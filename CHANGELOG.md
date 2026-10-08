# Changelog

Changes to the published contract: the specifications under `docs/`, the
formats and their schema versions, the catalog and the data API. Code
releases are the `v*` tags (the `xue` crate and the `xuepy` wheel); their
changes are in `git log`.

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

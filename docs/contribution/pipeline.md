# Encoder pipeline (`xuebuild/`)

The reference encoder ([architecture.md](architecture.md)): fetch exact
records → GDAL extract → crop, convert units → quantize → temporal
residuals → zstd → `.xue` → Zarr store → manifest → pointer. Per-source
behaviour is in [sources.md](sources.md).

## Registries

**`sources.py` (`SourceSpec`)** — one row per source: data location, time
axis as `(last_hour, step)` segments ending at `horizon_hours` (the
`--hours` default), `cycle_hours`, inputs, published bundles, grid, ladder,
fetch concurrency. Adding a model starts here; mirrors are
`rust/xue/src/encode/sources.rs` and `FORECAST_MODELS` in
`web/src/manifest.ts`.

**`variables.py`** — one entry per variable in GRIB2 terms (triple, surface,
label, unit) plus matching hints (`.idx` phrase, ECMWF param,
`ecmwf_alternate_params` for fields spelled differently along the axis). It
feeds both record matching and the v3 `parameter` block; it assigns no
container id.

- Another centre's record matches a variable through `grib2_aliases`
  (another triple, same surface) or `grib2_alternates` (a `RecordAlternate`:
  surface, value, statistical process, GDAL unit). Both encoders match by
  GRIB2 header index and by GDAL band metadata; aliases are never written.
- Input-only entries are read, never published: ECMWF `tp` and sflux
  `prate_ave` (into `prate`), `dirpw`, `spfh<level>`, and GFS's
  `crain`/`cfrzr`/`cicep`/`csnow` (into `ptype`, `binconvert.derive_ptype`).
- Isobaric families come from one eight-level table;
  `isobaric_variable(id)` returns `(family, level)`.

## Registration is not publication

`SourceSpec.bundle_scalar_ids` / `bundle_vector_ids` list what a source
ships, and a listed bundle ships only when its inputs were fetched:

- vectors (`wind10m`, `wind<level>`, …): every input in
  `binconvert.vector_input_ids`;
- derived scalars (`binconvert.DERIVED_SCALARS`: `thetae`, Bolton 1980 in a
  fixed operation order the native side must repeat) and derived vectors
  (`binconvert.DERIVED_VECTORS`: `qflux` = q·V/g; `wave` = height along the
  direction of travel in wind convention, `(-h sin θ, -h cos θ)`).

Overlapping sources publish in GFS order so a model switch keeps the layer.
A variable empty at the analysis (`optional_at_analysis`) is listed by
`binconvert.analysis_optional_ids` (mirrored in `convert.rs`) and starts at
the first step.

## Companion files and fill values

- `SourceSpec.companion_files`: a second file family of the cycle (GFS
  `gfswave.*`, ECMWF `wave`). Its records are appended after the primary
  ones, so a frame stays one GRIB, and a run is complete only when they are
  up. `CompanionFile.repack` (repack with `grib_set`; `bundle-groups` flags
  the job `eccodes`) exists but nothing uses it. WAVEWATCH III is JPEG 2000:
  the wheel's GDAL needs the OpenJPEG `scripts/build-gdal-minimal.sh` links.
- `VariableSpec.fill_values`: GDAL nodata becomes the codebook bottom
  **before** unit conversion, in both encoders. The format has no bitmap.

## Registry fixtures

`tests/fixtures/*-registry.json` hold the three implementations to one set
of ids and codebooks. Widening a source's inputs means recutting its crop
fixture (`gfs.*.crop.grib2` with the same run and `-srcwin`; the two-frame
`ecmwf.*` / `aifs.*` pairs) and regenerating the registry fixtures
([testing.md](testing.md)).

## Modules

- **`fetch.py` → `idx.py` / `grib2.py`**: byte-range fetches of exact GRIB
  records, one `.idx` + range set per family. ECMWF's CCSDS packing is
  repacked to `grid_simple` with `grib_set` at fetch time.
- **`binconvert.py`**: grid discovery, `crop_grid`, units, de-accumulation,
  quantization, grouping, bundles, ladder, posters, video, manifest entries.
  Rules mirrored in `rust/xue/src/encode/`:
  - a global grid snaps to `360 / width` (WAVEWATCH III's last longitude is
    off, and wave bundles must share the pgrb2 grid; `grid.rs`);
  - ladder `SourceSpec.variant_factors` (`(2,)` default, `(2, 4, 8)` on
    satellites): every f-th row and column of the codes, tile divided by f
    (`variant_tier`, `_variant_grid`, `_bundle_tile`); `variants` are in
    ascending factor order and nothing keys on the suffix;
  - video only for surface fields where `SourceSpec.video` is on.
- **`quantize.py` / `temporal.py` / `binformat.py`**: codebooks, modulo-256
  residual prediction, container I/O.
- **`manifest.py`**: manifest and pointer, both validated on write.
- **`observation.py`** (mirrored by `observation.rs`): NetCDF series into
  the same `SourceFrame` list the GRIB path yields, plus a `PlaneSource`.
- **`showcase.py`**: cases → cropped bundles → `showcase.json`
  (`showcase/README.md`). `title` / `summary` carry all eleven locales
  (`showcase.LOCALES`, held to `web/src/i18n.ts` by
  `tests/fixtures/locales.json`). A case is a forecast (`run` + `hours`), a
  local-file observation (`dataset`, under `XUE_OBSERVATION_ROOT`) or a
  fetched window (`run` = first hour); a window the archive cannot fill is
  refused. `showcase refresh` rewrites a sidecar without a rebuild.
- **`assemble.py`**: `publish.yml` fans a run out: `bundle-groups` packs
  bundles into at most `max_jobs` jobs of similar cost; each job runs
  `build-bin --bundles …` into `data/raw/partial/<group>/` and writes
  `manifest.part.<group>.json` (never uploaded, no STAC); finalize runs
  `assemble-run` (each bundle once, core set required) and writes the
  pointer. A top-up of a live cycle builds only missing bundles
  (`--base-manifest` against `make live-manifest`); `force` rebuilds all.
  `tests/test_assemble.py` holds split and top-up builds byte-identical to a
  whole one, so **nothing cross-variable may enter a bundle or its manifest
  entry**.

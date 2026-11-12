# Encoder pipeline (`xuebuild/`)

The reference encoder ([architecture.md](architecture.md)): fetch exact
records → GDAL extract → crop, convert units → quantize → Zarr store
(zstd per inner chunk) → manifest → pointer. The `.xue` (temporal residuals
→ zstd → container) is written beside the store only when asked for, from
the same codes. Per-source
behaviour is in [sources.md](sources.md).

## Registries

**`sources.py` (`SourceSpec`)** — one row per source: data location, time
axis as `(last_hour, step)` segments ending at `horizon_hours` (the
`--hours` default), `cycle_hours`, inputs, published bundles, grid, ladder,
fetch concurrency. Adding a model starts here; the native encoder reads
`rust/xue/registry.json`, and the shell's mirror is `FORECAST_MODELS` in
`web/src/manifest.ts`.

**`variables.py`** — one entry per variable in GRIB2 terms (triple, surface,
label, unit) plus matching hints (`.idx` phrase, ECMWF param,
`ecmwf_alternate_params` for fields spelled differently along the axis) and
every rule the encoders apply to it, named out of the closed sets of
`rules.py` ([`docs/registry.md`](../registry.md)): `conversion` (the unit
rule: which spellings a record may carry and the arithmetic to the
published unit), `gdal_rules` (how its record is told apart among a GRIB's
bands; derived from the identity by `default_gdal_rules` unless set),
`predictor` (`delta` / `raw`), `decimation` (`sample` / `peak`) and
`companions` (`poster`; `video` names the retired H.264 companion, which
no encoder builds). Nothing in the encoders selects behaviour
by a variable's id: a new quantity sets the fields that differ from the
defaults and the rest follows. It feeds both record matching and the v3
`parameter` block; it assigns no container id.

- Another centre's record matches a variable through `grib2_aliases`
  (another triple, same surface) or `grib2_alternates` (a `RecordAlternate`:
  surface, value, statistical process, GDAL unit). Both encoders match by
  GRIB2 header index and by GDAL band metadata (`gdal_rules`, evaluated in
  `gdal.py` and `inspect.rs`); aliases are never written.
- An ensemble product (templates 4.2/4.12 derived, 4.6/4.10 percentile,
  4.5/4.9 probability) is accepted by a `template-ensemble` rule alone,
  which compares the record's statistic with the rule's `ensemble` block;
  every other rule refuses those templates, so a deterministic source never
  picks up an ensemble record by its triple. A statistic of its own (a
  quartile, a spread, a probability) is a variable carrying an `ensemble`
  block (`grib2_ensemble`); a shared id read as an ensemble's centre
  (`tmp2m` as the median) gets the block from the source
  (`SourceSpec.ensembles`, `metadata.py`), so GFS's `tmp2m` stays without
  one. GDAL converts nothing in a derived record that is not a mean and
  writes a label (`[spread]`, …) in place of the unit
  (`gdal.DERIVED_UNIT_LABELS`, mirrored in `inspect.rs`): its values are
  in the parameter's GRIB unit, read as the variable's `gdal_unit`.
- Input-only entries are read, never published: ECMWF `tp` and sflux
  `prate_ave` (into `prate`), `dirpw`, `spfh<level>`, and GFS's
  `crain`/`cfrzr`/`cicep`/`csnow` (into `ptype`, `derive.derive_ptype`).
- Isobaric families come from one eight-level table (`cat` uses only
  300/250/200, `isobaric_family_levels`); `isobaric_variable(id)` returns
  `(family, level)`.

**`bundles.py` (`BundleSpec`)** — every bundle that is more than the scalar
of the variable of the same id: a vector's two members, a composite's
members and producer, a volume's levels, and a derivation (`kind`, `inputs`,
`level_hpa`) for the bundles the converter computes. The converter resolves
a bundle's inputs and algorithm off the entry; the per-kind views
(`VECTOR_BUNDLES`, `DERIVED_VECTORS`, `COMPOSITE_BUNDLES`, `VOLUME_BUNDLES`,
`DERIVED_SCALARS`) are for readers that walk one kind.

**`registry.py`** — serialises the four tables as the registry document
(`xue registry`), the one description the native encoder reads; the
committed snapshot `rust/xue/registry.json` is held to the tables by
`tests/test_registry.py`.

## Registration is not publication

`SourceSpec.bundle_scalar_ids` / `bundle_vector_ids` list what a source
ships, and a listed bundle ships only when its inputs were fetched:

- vectors (`wind10m`, `wind<level>`, …): every input in
  `bundles.vector_input_ids`;
- derived scalars (a `BundleSpec` with a `derivation`: `theta-e`, Bolton
  1980 in a fixed operation order the native side must repeat;
  `clear-air-turbulence`, Ellrod TI1 projected onto EDR with the source's
  `cat_calibration`, the one derivation that reads neighbours, so
  `_extract_planes` hands it the uncropped planes and the crop is cut
  afterwards; `precipitation-type`) and derived vectors (`vapour-flux`:
  `qflux` = q·V/g; `wave-vector`: `wave`, `windwave`, `swell` = a height
  along its direction of travel in wind convention, `(-h sin θ, -h cos θ)`).
  The `prate` bundle's `precipitation-rate` derivation takes whichever of
  its inputs the source lists, by the source's precipitation flags
  (`derive.precipitation_mode`).

Overlapping sources publish in GFS order so a model switch keeps the layer.
A variable empty at the analysis (`optional_at_analysis`) is listed by
`discover.analysis_optional_ids` (mirrored in `convert.rs`) and starts at
the first step. A variable a source writes only from some hour on
(`SourceSpec.variable_first_hours`: CMA-GEPS's 24-hour probabilities, from
F024) is listed there too and starts at that hour: earlier frames are
neither asked for it (`fetch.py`, `assemble.py`) nor read, and a build
whose hours end before it leaves the bundle out with a warning.
`SourceSpec.interval_precipitation_pairs` derives further rates from
totals exactly as `interval_precipitation` derives `prate` from `apcp`
(the precipitation quartiles).

## Companion files and fill values

- `SourceSpec.companion_files`: a second file family of the cycle (GFS
  `gfswave.*`, ECMWF `wave`). Its records are appended after the primary
  ones, so a frame stays one GRIB, and a run is complete only when they are
  up. A family is fetched in the packing it is published in: WAVEWATCH
  III is JPEG 2000, which the wheel's GDAL reads through the OpenJPEG
  `scripts/build-gdal-minimal.sh` links.
- `VariableSpec.fill_values`: GDAL nodata becomes the codebook bottom
  **before** unit conversion, in both encoders. The format has no bitmap.

## Registry snapshots

`rust/xue/registry.json` is the whole registry document and
`web/src/registry.ts` the shell's trimmed copy, both held to the tables by
`tests/test_registry.py` (regenerate with `xue registry --output
rust/xue/registry.json --shell web/src/registry.ts` and say so in the
commit). Widening a source's inputs means recutting its crop fixture
(`gfs.*.crop.grib2` with the same run and `-srcwin`; the two-frame
`ecmwf.*` / `aifs.*` pairs) and regenerating the snapshots
([testing.md](testing.md)).

## Modules

- **`fetch.py` → `idx.py` / `grib2.py`**: byte-range fetches of exact GRIB
  records, one `.idx` + range set per family. Which code fetches a source
  is its `SourceSpec.feed`, looked up in `feeds.py`
  ([sources.md](sources.md), "Feeds"). A source whose `packing` is `ccsds`
  (ECMWF open data, ICON-EU, ARPEGE) or `jpeg2000` (CMA GRAPES GEPS) is
  repacked to `grid_simple` with `grib_set` at fetch time.
- **The conversion**, one Python module per module of
  `rust/xue/src/encode/`, each mirrored by its namesake there:
  - `binconvert.py` (`convert.rs`): `convert_bin` as a run of named stages,
    plane extraction, units and fill, quantization, the container and the
    store writes;
  - `discover.py` (`discover.rs`): which bundles a source publishes and
    their inputs, the frames of a run and the records each must carry
    (`discover_series`, `discover_records`), the store dimensions;
  - `grid.py` (`grid.rs`): `GridInfo`, `grid_info`, the snaps, `crop_grid`;
  - `derive.py` (`derive.rs`): the derived vectors and scalars and the
    precipitation-rate arithmetic;
  - `ladder.py` (`ladder.rs`), `metadata.py` (`metadata.rs`), `poster.py`
    (`poster.rs`), `report.py` (`report.rs`): the resolution ladder, the
    bundle metadata, the first-frame poster, the build report and the
    manifest.

  Rules mirrored in `rust/xue/src/encode/`:
  - a global grid snaps to `360 / width` (WAVEWATCH III's last longitude is
    off, and wave bundles must share the pgrb2 grid; `grid.rs`);
  - ladder `SourceSpec.variant_factors` (`(2,)` default, `(2, 4)` on
    satellites and the 0.1° IFS grids): every f-th row and column of the codes, tile divided by f
    (`ladder.py`: `variant_tier`, `variant_grid`, `bundle_tile`); `variants` are in
    ascending factor order and nothing keys on the suffix;
  - the chunk predictor and the ladder's decimation are the variable's
    `predictor` and `decimation`.
- **`gdal.py`** (mirrored by `inspect.rs`): `gdalinfo` in, `SourceFrame`s
  out. A band is a variable's record when any of its `gdal_rules` accepts
  it (one evaluator per rule name); the unit is checked by the variable's
  `conversion` (`rules.expression`, the arithmetic alone, which the header
  index and the gdalinfo pass compare for identity).
- **`quantize.py` / `temporal.py` / `binformat.py`**: codebooks, modulo-256
  residual prediction, the container writer and the bundle metadata
  validator (`BundleMetadata`, which the store writers and readers apply
  too). Nothing in `xuebuild` decodes a container: a build that writes one
  reads it back through the wheel's `xue.Bundle` and decodes every plane
  before reporting it.
- **`manifest.py`**: manifest and pointer, both validated on write.
- **`observation.py`** (mirrored by `observation.rs`): NetCDF series into
  the same `SourceFrame` list the GRIB path yields, plus a `PlaneSource`.
- **`showcase.py`**: cases → cropped bundles → `showcase.json`
  (`showcase/README.md`). `title` / `summary` carry all eleven locales
  (`showcase.LOCALES`, held to `web/src/i18n.ts` by
  `tests/fixtures/locales.json`). A case is a forecast (`run` + `hours`), a
  local-file observation (`dataset`, under `XUE_OBSERVATION_ROOT`) or a
  fetched window (`run` = first hour); a window the archive cannot fill is
  refused. `showcase refresh` rewrites a sidecar without a rebuild. A
  fetched case is built by `localrun.build_local_run` plus a catalog row.
- **`zarrstore.py`**: the store writers and NumPy readers. A run's map
  stores are shared ([`zarr-profile.md`](../zarr-profile.md), profile 2):
  `convert_bin` writes each bundle's arrays and the coordinates its
  dimension names call for into `bundles[.<tier>].zarr`
  (`write_bundle_arrays`, concurrently), then the part over the build's
  bundles (`write_part`, `zarr.part.<first bundle>.json`) and, unless
  `partial`, the group document from it (`finalize_store`), whose CRC-32
  every entry naming the store carries. The dimension names are
  `discover.store_dimensions`, a function of the source registry alone
  (`time_<bundle id>` for an analysis-optional scalar, including one with
  a first hour, or a static field,
  `latitude_<family>` / `longitude_<family>` off the primary grid), so a
  subset build writes the objects a whole build does. Series stores stay
  one-bundle (profile 1, `write_store`); the readers open both profiles,
  a shared one by bundle id (`bundle_metadata`). Every writer takes its
  codes from a `CodesSource`, the planes the build holds in memory; no
  store is derived from a finished `.xue`.
- **`assemble.py`**: `publish.yml` fans a run out: `bundle-groups` packs
  bundles into at most `max_jobs` jobs of similar cost; each job runs
  `build-bin --bundles …` (`partial=True`) into `data/raw/partial/<group>/`
  and writes `manifest.part.<group>.json` and each shared store's part
  (never uploaded, no STAC); finalize runs `assemble-run` (each bundle once,
  core set required), which merges the parts into the manifest and each
  store's group document and stamps its CRC-32 on every entry naming it,
  and writes the pointer. A top-up of a live cycle builds only missing
  bundles (`--base-manifest` against `make live-manifest`, `--base-stores`
  against `make live-store-groups`), rewriting the group documents under a
  new `?v=`; `force` rebuilds all. `tests/test_assemble.py` holds split and
  top-up builds byte-identical to a whole one, store and group documents
  included, so **nothing cross-variable may enter a bundle's arrays or its
  manifest entry**: the group document is the one object several bundles
  share, and it is a merge of per-bundle parts.

## Local and archived runs

`build-local` (`localrun.py`, `make run-local`) rebuilds one archived cycle
for local use, such as a backtest, and uploads nothing:

```sh
make run-local MODEL=hrrr RUN=2025011512 HOURS=48 BBOX=-107,25.5,-93,37 BUNDLES="tmp2m wind10m"
.venv/bin/python -m xuebuild build-local --model sflux --run 2025011506 --hours 48 \
  --bbox=-107,25.5,-93,37 --bundles tmp2m dswrf wind10m
```

- `--run` is a `YYYYMMDDHH` cycle; `latest` is refused, since the live
  cycle is `build-bin`'s. `--hours` defaults to the source's whole axis,
  `--bbox W,S,E,N` (write it `--bbox=…` when west is negative) to the whole
  domain, and `--bundles` to every bundle the source publishes. Only those
  bundles' inputs are fetched.
- The output, `data/local/<model>.<run>/`, is the manifest and the shared
  Zarr store `bundles.zarr` holding every bundle, plus
  `<bundle>.series.zarr` for the source's `series_bundle_ids` (`--no-series` drops them) and the posters the
  manifest names. There is no `.xue`, reduced-resolution variant,
  live pointer or STAC.
- Raw GRIB goes to `data/raw/local/<model>.<run>/`, apart from the live
  `data/raw/<model>.<run>/`, for the same reason the showcase and the
  fanned-out jobs keep their own: a narrowed record set must never pass for
  a full fetch of the run. Delete it once the run is encoded when replaying
  many cycles.
- The encoder follows `XUE_ENCODER` as everywhere else. The installed
  `xuepy` wheel can lag `sources.py` and `variables.py` by a release, so a
  bundle registered since the last release builds only with
  `XUE_ENCODER=python`.

---
name: add-source
description: Add a new data source to Xue end to end — a forecast model, an observation feed (radar, satellite, rolling window) or a series-file source — across the Python encoder, the native Rust encoder, tests, STAC prose, the frontend, the publish workflow and the deploy order. Use when asked to add, onboard or integrate a new model, feed, satellite or dataset.
---

# Adding a source

Read first: `docs/contribution/sources.md` (pick the existing source of the
same kind as the template), `.claude/rules/encoder.md`,
`.claude/rules/delivery-and-deploy.md`. Check licensing before any
code: open access only, and record the attribution the licence requires.

## Pick the template

| Kind | Closest existing source |
|---|---|
| Global GRIB forecast over HTTP byte ranges | `gfs`, `ecmwf`, `aifs` |
| Regional / projected forecast | `hrrr` (regrid in both encoders) |
| Forecast delivered as NetCDF series | `ifshres` |
| Rolling observation window listed off a bucket | `mrms` |
| Rolling window from an external fetch tool | `jma` |
| Feed with only the newest frame | `aurora` (frame cache) |
| Archive read | `cma` |
| Geostationary imager | `himawari`, `goeseast`, `meteosat` (`docs/satellite.md`) |

## Checklist

1. **Registry**: a `SourceSpec` in `xuebuild/sources.py` (axis or window,
   cadence, inputs, published and core bundles, grid, tile, ladder, video,
   fetch concurrency). Put new variables in `xuebuild/variables.py` and their
   codebooks in `xuebuild/quantize.py` for every profile.
2. **Fetch**: set `SourceSpec.feed` (and `packing="ccsds"` when the
   records arrive CCSDS packed). A source on a service xue already reads
   names that family. A new service is a fetcher in `xuebuild/feeds.py`,
   registered in `FEEDS`, covering the frame URLs or the fetch tool, the
   completeness check and, for an observation, the latest slot; its code
   goes in `xuebuild/fetch.py`. Never add an `if spec.id == …` branch.
   An external tool stays a subprocess (`*cli.py`). An optional dependency
   goes in its own `pyproject.toml` group. Add the model to the CLI help in
   `xuebuild/cli.py`.
3. **Registry**: the native encoder has no tables; it reads the registry
   document (`docs/registry.md`). Regenerate both snapshots (`xue registry --output rust/xue/registry.json --shell web/src/registry.ts`,
   said in the commit): the shell's copy is what the frontend reads its
   ids, parameter blocks and codebooks from. Only a *rule* the
   closed sets lack (a new `conversion`, `match.gdal` rule, derivation,
   codebook type or ingest path) is Rust work: `xuebuild/rules.py` first,
   then `rust/xue/src/encode/{units,inspect,convert}.rs`, then a line in
   `docs/registry.md` and `docs/encoder.md`.
4. **Tests**: a small crop fixture (`tests/fixtures/<id>.*`, documented in
   `tests/fixtures/README.md` with the exact cut command) and
   `tests/test_<id>.py`, which runs it through both encoders byte for byte.
5. **STAC**: licence and provider prose in `xuebuild/stac_prose.json`.
   `tests/test_stac.py` holds it to the registry.
6. **Shell**: the source's id, label, product, pointer, observation flag,
   core bundles and cadence reach `FORECAST_MODELS` and `ForecastModelId`
   from the regenerated `web/src/registry.ts`; nothing of that is typed by
   hand. What the shell needs beyond it is chart knowledge: the source's
   `MODEL_CHARTS` row in `web/src/manifest.ts` (the type check fails
   without one: `railCore`, `defaultVariable`, `region` / `domain` for a
   regional grid, `subLongitude` for an imager), and for a new quantity a
   `web/src/variables.ts` row, a palette in `palettes.ts`, a chart ceiling
   in `levels.ts` where it needs one, and copy in all eleven locales
   (`en.ts` first); `identity.ts` needs a rule only when the quantity's
   parameter block is one no existing rule reads. `tests/web/registries.test.ts`
   fails for a published bundle with no `variables.ts` row. Also add the
   static metadata in `web/index.html`. If the source exceeds a manifest limit (as
   `forecastHours` was for CFS), widen it in all three validators and in
   `docs/format.md`.
7. **Workflow**: `publish-<id>.yml`. A forecast calls `publish.yml` and is
   added to its model allow-list. An observation copies the nearest rounds
   workflow (`publish-jma.yml` shape), sets `FRAME_CACHE` if it caches
   frames, and takes its `Resolve the encoder` step from
   `native.knows_source`.
8. **Discovery and docs**: `web/public/llms.txt`, the README sources table
   (with its run badge), and `docs/contribution/sources.md`.

## Verify

```sh
.venv/bin/python -m unittest tests.test_<id> tests.test_stac -v
make encoder-rust-test
npx vitest run tests/web
.venv/bin/python -m xuebuild build-bin --model <id> --run latest --hours <small>  # a real build, then `make serve`
```

## Ship, in this order

1. Merge the frontend half (the `FORECAST_MODELS` row and any charts) to
   `main` and **let the Pages shell deploy**: an older shell rejects the
   model id.
2. If the source needed a new rule (step 3), release on a `release/0.N`
   branch (`release` skill): the change, the release commit, the tag on
   the branch, PyPI, the relock, then a fast-forward into `main`.
   `publish.yml` pins the native encoder, so such a source publishes on
   schedule only once the relocked wheel is on `main`. A source built from
   existing rules needs no release: merge to `main`.
3. Dispatch the workflow once by hand and check the live pointer, then let
   the cron take over.

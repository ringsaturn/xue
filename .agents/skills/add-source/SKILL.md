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
   codebooks in `xuebuild/quantize.py` for every profile. A new family also
   regenerates its registry fixture under `tests/fixtures/`.
2. **Fetch**: the dispatch in `xuebuild/fetch.py`, which covers the frame
   URLs or the fetch tool, the completeness check and, for an observation,
   the latest-slot resolver wired into `latest_observation_slot` /
   `resolve_run`. An external tool stays a subprocess (`*cli.py`). An
   optional dependency goes in its own `pyproject.toml` group. Add the model
   to the CLI help in `xuebuild/cli.py`.
3. **Native mirror**: the same source, variables and codebooks in
   `rust/xue/src/encode/sources.rs`, `variables.rs` and `quantize.rs` (plus
   `grid.rs` / `inspect.rs` / `observation.rs` for a new rule), and a line in
   `docs/encoder.md`. The Python side changes first and the native side
   follows.
4. **Tests**: a small crop fixture (`tests/fixtures/<id>.*`, documented in
   `tests/fixtures/README.md` with the exact cut command) and
   `tests/test_<id>.py`, which runs it through both encoders byte for byte.
5. **STAC**: licence and provider prose in `xuebuild/stac.py::_source_prose`
   and `tests/fixtures/stac-prose.json`. `tests/test_stac.py` holds them to
   the registry.
6. **Shell**: a row in `FORECAST_MODELS` (and the `ForecastModelId` union)
   in `web/src/manifest.ts`, with `domain` / `region` for a regional grid.
   Any new quantity goes in `web/src/variables.ts`, `identity.ts` and
   `palettes.ts`, with copy in all eleven locales (`en.ts` first). Also add
   the static metadata in `web/index.html` and a case in
   `tests/web/manifest.test.ts`. If the source exceeds a manifest limit (as
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

1. Merge the code.
2. **Deploy the Pages shell** (an older `FORECAST_MODELS` rejects the model
   id).
3. Release the `xuepy` wheel that carries the source (`release` skill), then
   relock. `publish.yml` pins the native encoder, so a forecast source
   publishes on schedule only after that.
4. Dispatch the workflow once by hand and check the live pointer, then let
   the cron take over.

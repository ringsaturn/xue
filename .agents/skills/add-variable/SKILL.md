---
name: add-variable
description: Add a bundle (a published field) to an existing Xue source — a new isobaric level of a known family, a new surface quantity, or a derived scalar/vector — through the Python encoder, the native encoder, the registry fixtures and optionally the frontend. Use when asked to publish another variable, level, or derived field for gfs/ecmwf/aifs/hrrr/… .
---

# Adding a variable or bundle

Read `.claude/rules/encoder.md` and
`docs/contribution/pipeline.md` first.

## Decide which case it is

- **A level of a known family** (e.g. `tmp700`): the isobaric families are
  generated from one table in `xuebuild/variables.py`
  (`isobaric_variable(id)`). Usually two source-table lines and **no
  frontend change**.
- **A new quantity**: a `VariableSpec` registration in GRIB2 terms
  (parameter triple, fixed surface, unit, matching hints such as the `.idx`
  phrase or the ECMWF param, and `grib2_aliases` / `grib2_alternates` for
  another centre's spelling), a codebook in `quantize.py`, and optionally
  frontend chart knowledge.
- **Derived**: `binconvert.DERIVED_SCALARS` / `DERIVED_VECTORS` (fixed
  operation order, mirrored in `rust/xue/src/encode/convert.rs`). It ships
  only when all of its inputs are fetched.

## Steps

1. `xuebuild/variables.py`: register it (or confirm the family covers it).
2. `xuebuild/sources.py`: add its inputs to the source's fetched variables
   and the id to `bundle_scalar_ids` / `bundle_vector_ids`, in the source's
   existing order. ECMWF follows GFS order, so a model switch keeps the
   layer.
3. Native mirror: `rust/xue/src/encode/variables.rs`, `sources.rs`,
   `quantize.rs` (and `convert.rs` for a derivation).
4. Fixtures:
   - recut the source's crop GRIB if it needs new records
     (`tests/fixtures/README.md` has the exact `gdal_translate -srcwin`);
   - regenerate the affected registry fixture
     (`tests/fixtures/*-registry.json`); the failing registry test names it.
5. Frontend (optional; an unknown id renders generically):
   `web/src/variables.ts` (one `VariableSpec` row), `levels.ts` for a family
   member, a palette, an i18n label in `web/src/locales/en.ts` and the
   other ten locales.
6. `web/public/llms.txt` if what the model publishes changed. Check the STAC
   prose in `xuebuild/stac.py` if a licence or provider changes.

## Verify and ship

```sh
.venv/bin/python -m unittest tests.test_native tests.test_<source> -v
make encoder-rust-test
npx vitest run tests/web
```

The `xuepy` floor must cover a wheel whose source table publishes the new
bundle, otherwise native builds refuse. So: commit → `release` skill →
relock → the next scheduled run publishes it. For a new chart, deploy the
shell before the data.

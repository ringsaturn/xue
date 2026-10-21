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
  another centre's spelling), its rules where the defaults do not fit
  (`conversion`: the unit rule and the spellings a record may carry;
  `gdal_rules`: how its band is told apart, derived from the identity
  unless set; `predictor`, `decimation`, `companions` — every name out of
  the closed sets of `xuebuild/rules.py`, `docs/registry.md`), a codebook
  in `quantize.py`, and optionally frontend chart knowledge. Never a
  branch on the id: the encoders read the entry.
- **Derived**: a `BundleSpec` in `xuebuild/bundles.py` with a `derivation`
  (one of the six kinds; its arithmetic in `binconvert.py` has a fixed
  operation order, mirrored in `rust/xue/src/encode/convert.rs`). It
  ships only when all of its inputs are fetched.

## Steps

1. `xuebuild/variables.py`: register it (or confirm the family covers it),
   setting the rule fields that differ from the defaults; a vector,
   composite, volume or derived bundle in `xuebuild/bundles.py`.
2. `xuebuild/sources.py`: add its inputs to the source's fetched variables
   and the id to `bundle_scalar_ids` / `bundle_vector_ids`, in the source's
   existing order. ECMWF follows GFS order, so a model switch keeps the
   layer.
3. Native mirror: `rust/xue/src/encode/variables.rs`, `sources.rs`,
   `quantize.rs` (and `convert.rs` for a derivation).
4. Fixtures:
   - recut the source's crop GRIB if it needs new records
     (`tests/fixtures/README.md` has the exact `gdal_translate -srcwin`);
   - regenerate the registry snapshot
     (`xue registry --output rust/xue/registry.json`, said in the
     commit) and the affected family fixture
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
bundle, otherwise native builds refuse it. So do the encoder change on a
`release/0.N` branch and follow the `release` skill: the change, the
release commit, the tag, PyPI, the relock, then a fast-forward into
`main`. The next scheduled run publishes the bundle. For a new chart,
deploy the shell from `main` before the release lands.

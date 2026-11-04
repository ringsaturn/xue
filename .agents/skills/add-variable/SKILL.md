---
name: add-variable
description: Add a bundle (a published field) to an existing Xue source — a new isobaric level of a known family, a new surface quantity, or a derived scalar/vector — through the Python encoder, the native encoder, the registry snapshots and the frontend's chart knowledge. Use when asked to publish another variable, level, or derived field for gfs/ecmwf/aifs/hrrr/… .
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
  (one of the six kinds; its arithmetic in `derive.py` has a fixed
  operation order, mirrored in `rust/xue/src/encode/derive.rs`). It
  ships only when all of its inputs are fetched.

## Steps

1. `xuebuild/variables.py`: register it (or confirm the family covers it),
   setting the rule fields that differ from the defaults; a vector,
   composite, volume or derived bundle in `xuebuild/bundles.py`.
2. `xuebuild/sources.py`: add its inputs to the source's fetched variables
   and the id to `bundle_scalar_ids` / `bundle_vector_ids`, in the source's
   existing order. ECMWF follows GFS order, so a model switch keeps the
   layer.
3. Native encoder: no mirror to write. It reads `rust/xue/registry.json`
   (step 4). Rust changes only for a rule or derivation the closed sets
   lack (`rust/xue/src/encode/{units,inspect,convert}.rs`; see the
   `add-source` skill, step 3).
4. Fixtures:
   - recut the source's crop GRIB if it needs new records
     (`tests/fixtures/README.md` has the exact `gdal_translate -srcwin`);
   - regenerate both registry snapshots (`xue registry --output rust/xue/registry.json --shell web/src/registry.ts`,
     said in the commit); `tests/test_registry.py` fails until it is done.
     The ids, parameter blocks, codebooks, bundle members and family lists
     reach the shell from `web/src/registry.ts`: no TS table is edited for
     them.
5. Frontend chart knowledge: `web/src/variables.ts` (one `VariableSpec`
   row), `levels.ts` for a family member or a chart ceiling, a palette,
   an i18n label in `web/src/locales/en.ts` and the other ten locales;
   `identity.ts` only when no existing rule reads the parameter block. A
   published bundle without a row fails `tests/web/registries.test.ts`;
   one meant to draw generically goes into its `GENERIC_BUNDLE_IDS`.
6. `web/public/llms.txt` if what the model publishes changed. Check the STAC
   prose in `xuebuild/stac.py` if a licence or provider changes.

## Verify and ship

```sh
.venv/bin/python -m unittest tests.test_native tests.test_<source> -v
make encoder-rust-test
npx vitest run tests/web
```

A bundle, level or variable built from existing rules is a commit to `main`
with the regenerated snapshots, and no release; the next scheduled run
publishes it. Only a new rule is encoder work and follows the `release`
skill. For a new chart, deploy the shell from `main` first.

---
paths:
  - "xuebuild/**/*.py"
  - "rust/xue/src/**/*.rs"
  - "rust/xue-wasm/**/*.rs"
  - "web/src/{manifest,identity,sessionkeys}.ts"
  - "web/src/zarr/**"
  - "docs/{format,zarr-profile}.md"
  - "tests/**/*.py"
  - "tests/fixtures/**"
---

# Cross-implementation rules

One format, three implementations: the Python encoder (`xuebuild/`), the
Rust decoder and native encoder (`rust/xue`, `rust/xue-wasm`, `rust/xue-py`),
and the TypeScript frontend (`web/src/`). They must agree. Background:
[`docs/contribution/architecture.md`](../../docs/contribution/architecture.md).

## Order of change

1. The **Python encoder is the reference**. A format or encoding change goes
   there first, and the native encoder (`rust/xue/src/encode/`) follows.
2. A format change means changing **the spec (`docs/format.md` /
   `docs/zarr-profile.md`), both encoders, every reader and the fixtures
   together**, in one change.
3. The native encoder must produce **byte-for-byte identical** output.
   `tests/test_native.py` and the per-source tests (`test_ecmwf`, `test_hrrr`,
   `test_aifs`, …) build through both and compare every artifact. Never relax
   such a test to make a change pass. Find the divergence.

## Pairs that must stay in step

| Concern | Schema (`docs/schemas/`) | Python | Rust | Web |
|---|---|---|---|---|
| Bundle metadata (schema v3, v4 on a projected grid) | `bundle-metadata` | `metadata.py::build_metadata`, `binformat.py::_parse_metadata` | `decode/metadata.rs` | `manifest.ts::parseBundleMetadata` |
| Container byte layout | — | `binformat.py` | `format.rs` (the only place it is packed or unpacked) | via wasm |
| Manifest / pointer validation (all three validators) | `manifest`, `pointer` | `manifest.py` | `encode/manifest.rs` | `manifest.ts` |
| Grid snapping, native grids (Lambert window and winds, Gaussian rows), downsample | — | `grid.py`, `reproject.py` | `encode/grid.rs`, `encode/reproject.rs`, `projection.rs` | `domain.ts` |
| Observation ingest | — | `observation.py` | `encode/observation.rs` | — |
| Composite inputs, analysis-optional ids | — | `discover.py` | `encode/convert.rs` | — |
| GRIB matching / template parsing | — | `gdal.py`, `grib2.py` | `inspect.rs`, `gribindex.rs` | — |
| Zarr group and array documents | `store-group`, `store-array`, `polar-group`, `network-group` | `zarrstore.py`, `nexrad/store.py`, `river/network.py` (writers) | `zarr.rs`, `network.rs`, `encode/zarrstore.rs` | `zarr/shard.ts`, `river/store.ts` |
| Zarr shard reading | — | `zarrstore.py` (writer) | `zarr.rs` | `zarr/shard.ts` |
| Tropical cyclone archive and scores | `tc-archive-catalog`, `tc-archive-month-index`, `tc-archive-season-index`, `tc-archive-overview`, `tc-archive-forecast-line`, `tc-archive-best-line`, `tc-scores`, `tc-errors-line` | `tc/archive.py`, `tc/score.py` (writers) | — | `tc/archive.ts` |
| Alerts archive | `alerts-archive-catalog`, `alerts-archive-index`, `alerts-archive-line` | `alerts/archive.py` (writer) | — | — |

A schema is normative for shape. `tests/test_schemas.py` holds every
writer's goldens to it (the committed ones, and the stores and manifests it
builds without GDAL); the shell runs the validator generated from it
(`make schemas`) after its version gate and before the rules a schema
cannot say; the Rust tests (`schema_tie_tests`) tie every field name the
code reads or writes to a property the schema names. Changing a shape means
changing the schema with the code, and `make schemas`.

Registries are held to one set of ids, codebooks and rules by committed
fixtures: `rust/xue/registry.json` is the whole registry document
(`docs/registry.md`: every variable's identity, codebooks, unit rule, GDAL
rules, predictor, decimation and companions; every bundle; every source),
printed by `xue registry` and read by the native encoder in place of a
table of its own; `web/src/registry.ts` is its trimmed copy for the shell,
written by the same command (`--shell`), which the frontend reads in place
of tables of its own.
The Python encoder never selects behaviour by a variable or bundle id: a
rule is a field of `VariableSpec` / `BundleSpec`, named out of the closed
sets of `xuebuild/rules.py`. When a test reports that a registry moved,
regenerate both snapshots deliberately (`xue registry --output
rust/xue/registry.json --shell web/src/registry.ts`, said in the commit)
and change every implementation that reads them, never only a snapshot.

## Invariants that are easy to break

- A decoder **always keeps reading** container v1, v2 and the `.xue`
  container, plus every older metadata schema. Published runs and showcase
  cases are never rebuilt.
- A metadata `schemaVersion` must be the *lowest* version that fits, and
  `unitSeconds` the coarsest unit that fits. Readers reject over-declaration,
  so each file has exactly one valid encoding.
- `numericId` / `variableId` is **file-local** (1..n in bundle order). Never
  key anything across sessions or bundles by it. Identity is the GRIB2
  `parameter` block, plus `aerosol` / `band` / `producer` where present.
- Nothing cross-variable may enter a bundle or its manifest entry. Split
  builds and top-ups are held byte-identical to whole builds
  (`tests/test_assemble.py`).
- The STAC documents are pure functions of manifest + catalog row +
  `sources.py`: no timestamps, no hostnames, relative links only.
- All arithmetic on file values in the decoder is checked, and nothing is
  allocated from a file value before it is validated.
- Byte-identity across encoders holds for the same libzstd version; the
  parity tests skip when the two differ.

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

| Concern | Python | Rust | Web |
|---|---|---|---|
| Bundle metadata (schema v3) | `binconvert.py::build_metadata`, `binformat.py::_parse_metadata` | `decode/metadata.rs` | `manifest.ts::parseBundleMetadata` |
| Container byte layout | `binformat.py` | `format.rs` (the only place it is packed or unpacked) | via wasm |
| Manifest / pointer validation (all three validators) | `manifest.py` | `encode/manifest.rs` | `manifest.ts` |
| Grid snapping, regrid, downsample | `binconvert.py`, `reproject.py` | `encode/grid.rs`, `reproject.rs` | `domain.ts` |
| Observation ingest | `observation.py` | `encode/observation.rs` | — |
| Composite inputs, analysis-optional ids | `binconvert.py` | `encode/convert.rs` | — |
| GRIB matching / template parsing | `gdal.py`, `grib2.py` | `inspect.rs`, `gribindex.rs` | — |
| Zarr shard reading | `zarrstore.py` (writer) | `zarr.rs` | `zarr/shard.ts` |

Registries are held to one set of ids and codebooks by committed fixtures:
`tests/fixtures/{isobaric,pressure,surface,ocean,aerosol,satellite,tc}-registry.json`.
When a test reports that a registry moved, regenerate the fixture
deliberately and change every implementation that reads it, never only the
fixture.

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
- zstd in-process (Python ≥ 3.14) and the zstd CLI are interchangeable on
  decode but **not byte-identical on encode**. Byte-identity tests compare
  like with like.

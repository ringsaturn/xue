# Architecture

Xue packs weather runs and observation windows into per-variable Zarr v3
stores and plays them back in a static browser page. The layout began as a
single-file container, `.xue`: no longer published, but still the encoders'
intermediate (every store is derived from one) and read by every decoder
forever, since published runs and cases carry it and are never rebuilt.

Normative specs, which win over this page: [format.md](../format.md),
[zarr-profile.md](../zarr-profile.md), [encoder.md](../encoder.md),
[stac.md](../stac.md).

## Map

- `xuebuild/` — Python pipeline and **reference encoder** ([pipeline.md](pipeline.md)).
  `sources.py` / `variables.py` registries; `fetch.py` + `idx.py` +
  `grib2.py` range fetches; `binconvert.py` the conversion; `quantize.py`,
  `temporal.py`, `binformat.py` codebooks, residuals, container I/O;
  `observation.py` NetCDF ingest; `model.py` shared shapes (`SourceFrame`,
  `PlaneSource`); `manifest.py` manifest + pointer; `zarrstore.py` store
  export ([delivery.md](delivery.md)); `encoder.py` / `native.py` encoder
  dispatch; `assemble.py` split builds; `stac.py` catalog; `showcase.py`
  cases; `satellite/` ([sources.md](sources.md)); `tc/`, `airport/`,
  `sounding/` ([point-products.md](point-products.md)); `gdal.py`,
  `zstdcli.py`, `ffmpegcli.py`, `eccodescli.py`, `om2nccli.py`, `jmacli.py`
  tool wrappers.
- `rust/xue/` — decoder (`src/decode/`), byte layout (`src/format.rs`), Zarr
  read geometry (`src/zarr.rs`), native encoder (`src/encode/`, `encoder`
  feature).
- `rust/xue-wasm/` — wasm bindings, built into `web/src/wasm/` (generated).
- `rust/xue-py/` — the `xuepy` wheel, imported as `xue`: native encoder,
  decoder, `gdal_info`.
- `rust/xue-worker/` — read-only data API ([delivery.md](delivery.md#data-api-rustxue-worker)).
- `web/src/` — frontend ([frontend.md](frontend.md)).
- `tests/` — Python tests, fixtures and `prepare_*.py` generators;
  `tests/web/` vitest, `tests/e2e/` Playwright ([testing.md](testing.md)).

## Three implementations, one format

Encoder, Rust decoder and TypeScript frontend must stay in agreement.

- **The Python encoder is the reference.** A format change goes there first,
  with the spec; native encoder, decoders and fixtures follow in the same
  change.
- The native encoder (`rust/xue/src/encode/`, linking GDAL, grib-rs, zstd)
  must write output byte-identical to Python's; `tests/test_native.py`
  compares every artifact of one build against the other.
- `xuebuild/encoder.py` converts through the wheel by default; `XUE_ENCODER`
  = `auto` | `native` | `python` overrides. Scheduled workflows pin `native`
  so a silent fallback fails. A workflow whose source may be newer than the
  PyPI wheel asks `native.knows_source` (the whole published bundle set)
  and otherwise takes the reference pipeline with a `::warning::`
  ([publishing.md](publishing.md)).
- The native encoder writes no video and no pointer. `native.py` reads the
  codes back out of the wheel's bundles, encodes video, derives the stores,
  folds the descriptors into the manifest, and only then writes the pointer,
  whose CRC32 covers the finished manifest.
- The `encoder` feature is off by default because it links GDAL and the
  decoder must not. Profiles in `rust/Cargo.toml` (Cargo reads them only
  from the workspace root): `release` is tuned for the wasm decoder,
  `encoder` inherits it at `opt-level = 3`.

## Delivery contract

- The **live pointer** (`latest.json` for GFS, `latest-<model>.json`
  otherwise; schema v1) is the only mutable object per source. Uploading it
  takes a run live.
- Everything it names, `manifest.json` (schema v5) and every artifact under
  `<model>.<run>/`, is immutable and addressed with `?v=<crc32>`.
- Manifest paths resolve relative to the manifest URL, so a run can be
  served from the site or the bucket (`VITE_DATA_BASE_URL`,
  `web/.env.deploy`). Production hostnames reach the frontend only through
  `web/src/site.ts`.
- **Shell first.** A new shell accepts old manifests; an old cached shell
  rejects new ones. Deploy the Pages shell before data that widens the
  manifest schema, bumps the container or bundle metadata version, or uses
  a source id `FORECAST_MODELS` does not know.
- **The bundle set is not schema.** The three validators
  (`xuebuild/manifest.py`, `rust/xue/src/encode/manifest.rs`,
  `web/src/manifest.ts`) admit a `variable` by shape alone
  (`^[a-z][a-z0-9]*$`, unique). The only closed set is the source's core
  (`SourceSpec.core_bundle_ids`, mirrored as `MODEL_CORE_BUNDLES` and
  `coreBundles`), required on a live run. The shell draws an unknown bundle
  generically.
- **`numericId` / `variableId` is file-local:** variables are numbered 1..n
  in bundle order (vector: 1 = u, 2 = v). Identity is the GRIB2 `parameter`
  block (plus `aerosol` where species share a triple), read by
  `web/src/identity.ts`; the id string is only a naming convention. Never key
  across sessions by `numericId` (`web/src/sessionkeys.ts` scopes keys per
  session).

## Container versions

`FixedHeader.version` ([format.md](../format.md) §"Container v2"):

- **v1** plane-major: a payload is one plane of one frame (`IDX1` +
  `PlaneEntry`).
- **v2** tiled: a payload is one tile × one temporal group × one variable
  (`IDX2` + variable, group and chunk tables). Order group → tile → variable
  makes a group, a viewport's tile row, and a cell's series (one chunk per
  group) cheap range reads.

Tiling changes storage only, not metadata, codebooks or residual arithmetic.
Encoders write v2; **decoders must keep reading v1**.

## Bundle metadata schema versions

`schemaVersion` in the bundle metadata JSON is the lowest version a reader
must implement (separate from manifest v5 and pointer v1). v1/v2 are legacy
whole-hour axes; v3, written now, adds the `parameter` block and a
unit-neutral axis (`unitSeconds` + `firstFrameOffset` + `frameStep` |
`frameOffsets`).

- `unitSeconds` must be the coarsest unit that fits (3600 for forecasts, an
  observation's `cadence_seconds` otherwise). Decoders reject unimplemented
  **and** overdeclared versions, so each file has exactly one valid encoding.
- Optional v3 blocks `band`, `producer`, `aerosol` are validated by all
  readers when present, raise no floor and are ignored by older readers, so
  adding such a block is one-sided.
- A plane is keyed by frame offset, never forecast hour
  (`PlaneEntry.frame_offset`, the worker's `frameOffset`,
  `SourceFrame.lead_seconds`).
- Must agree: `binconvert.py::build_metadata`,
  `binformat.py::_parse_metadata`, `rust/xue/src/decode/metadata.rs`,
  `web/src/manifest.ts::parseBundleMetadata`.

## Decoder

`rust/xue/src/decode/`: `Bundle` (whole file) and `StreamingBundle`
(structural prefix, payloads fed in as ranges arrive) in `bundle.rs`, over
`metadata.rs` → `structure.rs` → `core.rs` (residual replay; its
`decode_chunk` is also the Zarr decode path in browser and Worker). All
arithmetic on file values is checked; nothing is allocated from a file value
before validation.

`rust/xue/src/format.rs` holds the byte layout and nothing else. The decoder
unpacks and the native encoder packs through it, so fields cannot drift.

## External tools

GDAL, zstd, ffmpeg, eccodes and om2nc run as subprocesses; NumPy is the only
Python runtime dependency. Exceptions and boundaries:

- zstd runs in process (`compression.zstd`) on Python ≥ 3.14, else the CLI:
  interchangeable on decode, **not byte-identical on encode**.
- `gdal.dataset_info` reads through the wheel's GDAL (`xue.gdal_info`) when
  converting natively, the subprocess otherwise, following `XUE_ENCODER`, so
  a run never mixes two GDALs and the scheduled workflows need no GDAL
  (`tests/test_gdalinfo.py` diffs the two). `gdal_translate` has no in-wheel
  path: `XUE_ENCODER=python` needs a system GDAL.
- om2nc (`XUE_OM2NC`) is GPL-2.0-only: an executable on the PATH only; no om
  dependency may enter `pyproject.toml`, `Cargo.toml` or `web/`.

Errors the user must fix subclass `XueError` (`xuebuild/errors.py`); the CLI
prints `error: …` and exits 2. Any other exception is a bug.

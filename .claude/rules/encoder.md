---
paths:
  - "xuebuild/**"
  - "rust/xue/src/encode/**"
  - "rust/xue-py/**"
  - "rust/Cargo.toml"
  - "pyproject.toml"
  - "tests/test_*.py"
  - "tests/fixtures/**"
---

# Encoder rules (`xuebuild/`, `rust/xue/src/encode/`)

Background: [`docs/contribution/pipeline.md`](../../docs/contribution/pipeline.md),
[`sources.md`](../../docs/contribution/sources.md), [`docs/encoder.md`](../../docs/encoder.md).

- **Runtime dependencies stay NumPy + `xuepy`.** GDAL, zstd, ffmpeg, eccodes
  and om2nc are CLI subprocesses (`gdal.py`, `zstdcli.py`, `ffmpegcli.py`,
  `eccodescli.py`, `om2nccli.py`). Anything heavier goes into a dependency
  group (`zarr`, `cma`, `aurora`, `satellite`) that only its workflow syncs.
- **om2nc is a licence boundary** (GPL-2.0-only): an executable on PATH only.
  No om crate, package or vendored code may reach `pyproject.toml`,
  `Cargo.toml` or `web/`.
- `gdal.dataset_info` is the one entry point to `gdalinfo`. It follows
  `XUE_ENCODER`, so a run never mixes two GDAL installs.
- Errors that the user must fix subclass `XueError` (`xuebuild/errors.py`).
  The CLI prints `error: …` and exits 2. Anything else is a bug: let it
  raise.
- **Registration is not publication.** `variables.py` registers a quantity
  in GRIB2 terms. `SourceSpec.bundle_scalar_ids` / `bundle_vector_ids` /
  `bundle_composite_ids` decide what a source ships. Input-only variables
  (`tp`, `apcp`, `dirpw`, the `c*` ptype flags) are read, never published.
- Order of operations is part of the output: fill values → unit conversion
  → downsample → crop. Derivations (Bolton θe, qflux, wave vector, ptype)
  have a fixed operation order mirrored in Rust. Do not reorder for style.
- The `encoder` cargo feature is off by default because it links GDAL. The
  decoder, the wasm build and `make test-rust` must not need GDAL.
- Cargo profiles live in `rust/Cargo.toml` (the workspace root): `release`
  is tuned for wasm, `encoder` inherits it at `opt-level = 3`.
- Widening a source's input list means recutting its crop fixture
  (`tests/fixtures/*.crop.grib2`, same `-srcwin`; see `tests/fixtures/README.md`)
  and regenerating the registry fixtures.
- A new source or bundle: use the `add-source` / `add-variable` skills.

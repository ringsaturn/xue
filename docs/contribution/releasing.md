# Releasing

One `v*` tag releases two artifacts built from `rust/`:

- the `xue` crate (decoder, plus the off-by-default `encoder` feature) to
  crates.io, by [`release-crate.yml`](../../.github/workflows/release-crate.yml);
- the `xuepy` wheels (the decoder and the native encoder through PyO3, each
  carrying a minimal GDAL) to PyPI and the GitHub release, by
  [`release-xuepy.yml`](../../.github/workflows/release-xuepy.yml).

`xuebuild` itself is never published (its classifier makes PyPI refuse
it); it depends on the `xuepy` wheel.

## Versions

| Where | Field |
|---|---|
| `rust/xue/Cargo.toml` | `version`: the crate, and the tag |
| `rust/xue-py/Cargo.toml` | `version`: tracks the crate; maturin takes the wheel version from it (`dynamic = ["version"]` in `rust/xue-py/pyproject.toml`) |
| `rust/Cargo.lock` | both entries |
| `pyproject.toml` | `xuepy>=0.N,<0.N+1`: the floor `xuebuild` installs |

`xue-wasm` and `xue-worker` keep `0.1.0` and are never published.

## The flow

Everything happens on a `release/0.N` branch, and `main` receives the
release only once it is complete. So `main` never carries an encoder or
source-table change without the wheel that matches it, and the scheduled
publishes keep working throughout the release window.

1. **Branch**: `git checkout -b release/0.N main`. Commit the encoder or
   source-table change there, or merge it in. `test.yml` runs on
   `release/**`, and its Python job builds the wheel from the tree
   (`make encoder-py`) instead of installing PyPI's, so native parity is
   checked against the code being released.
2. **Release commit** on the branch, `chore: release 0.N.0`: bump `version`
   in `rust/xue/Cargo.toml` and `rust/xue-py/Cargo.toml`, and refresh
   `rust/Cargo.lock`. Nothing else goes in this commit.
3. **Tag** the release commit `v0.N.0` on the branch, then push the branch
   and the tag. Both workflows first check that the tag equals the manifest
   version and refuse otherwise. `release-crate.yml` publishes the crate and
   creates the GitHub release. `release-xuepy.yml` builds wheels on macOS
   arm64 and Linux x86_64 / aarch64 (manylinux_2_39), smoke-tests each one
   (it encodes the GRIB fixture and the JPEG 2000 wave fixture and decodes
   them back), publishes to PyPI and attaches the wheels to the release.
   Both authenticate with Trusted Publishing (OIDC), so no token is stored.
   No sdist is published, because a source build needs GDAL, libclang and
   cmake.
4. **Relock commit** on the branch, `chore: relock on the published xuepy
   0.N.0`, once the wheels are on PyPI: raise the floor in `pyproject.toml`
   to `"xuepy>=0.N,<0.N+1"` and run `uv lock --refresh-package xuepy`.
   Commit `pyproject.toml` and `uv.lock` together.
5. **Merge into `main`** with a fast-forward (`git merge --ff-only
   release/0.N`), so the change, the release commit and the relock land on
   `main` together and keep their history. Then delete the branch.

A manual dispatch of either workflow is a rehearsal: `cargo publish
--dry-run`, or wheels uploaded as workflow artifacts only.

Gate each step on its exit code. Do not chain a commit or push behind a
pipe such as `uv lock | tail`, which hides a failure.

## Why the floor tracks the current release

The wheel carries its own copy of the source table (`xuebuild/sources.py`
mirrored in `rust/xue/src/encode/`). A wheel whose table publishes a
different bundle set for a source refuses to build it, and the scheduled
forecast workflows pin `XUE_ENCODER=native`, so an older wheel fails the
build instead of quietly publishing less. The floor is therefore the first
release whose table matches this tree's `sources.py`, and the upper bound
keeps an unrelocked tree from picking up a newer table.

## Why the release lives on a branch

If an encoder or source-table change merged to `main` before its wheel
shipped, `main` would still install the old locked wheel. Then
`tests/test_native.py` (byte parity between the two encoders) and the
scheduled publishes of the affected sources, which pin
`XUE_ENCODER=native`, would fail until the relock. Raising the floor before
the wheel is on PyPI is worse: `uv sync` fails everywhere, because the
version cannot be resolved. Keeping steps 1–4 on `release/0.N` confines
both failure windows to the branch.

Workflows that publish a source a released wheel may not know yet
(`publish-jma.yml` and the other rolling windows except MRMS) resolve the
encoder at job time with `native.knows_source`, and fall back to the
reference pipeline with a warning.

## Deploy order

The Pages shell ([`deploy-pages.yml`](../../.github/workflows/deploy-pages.yml))
deploys automatically on every push to `main` that touches `web/`,
`rust/`, the build configuration or the documents `llms-full.txt` is built
from. A new shell accepts old data, but an old cached shell rejects new
data, so the shell must be live **before** the bucket carries:

- a manifest in a widened schema (validator change);
- a container version bump or a bundle metadata `schemaVersion` bump;
- a new source id (an older `FORECAST_MODELS` does not know it).

In practice: merge the frontend change, let `deploy-pages.yml` finish,
then release the wheel and let the publishes run. A tab whose validator
rejects a live manifest or store reloads itself once per manifest, which
covers viewers holding the old shell. A new bundle of a known shape needs
no shell change: the shell renders an unknown field generically.

The data API Worker (`rust/xue-worker`) deploys separately with
`make deploy-api` / `deploy-api.yml`.

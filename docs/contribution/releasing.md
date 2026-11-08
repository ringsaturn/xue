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
release only once it is complete. So `main` never carries an encoder
change without the wheel that matches it, and the scheduled publishes keep
working throughout the release window.

A change to the registry tables alone (`xuebuild/variables.py`,
`quantize.py`, `sources.py`, the bundle tables of `bundles.py`) is not
an encoder change and takes none of this: it is a commit to `main` with
the regenerated `rust/xue/registry.json` (`xue registry --output
rust/xue/registry.json`), and the next build hands the new document to the wheel
already installed (see "Why the floor tracks the current release").

1. **Branch**: `git checkout -b release/0.N main`. Commit the encoder
   change there, or merge it in. `test.yml` runs on `release/**`, and its
   Python job builds the wheel from the tree (`make encoder-py`) instead of
   installing PyPI's, so native parity is checked against the code being
   released.
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
4. **Relock commit**, made by CI. When `release-xuepy.yml` has published
   the wheels, [`release-relock.yml`](../../.github/workflows/release-relock.yml)
   waits until PyPI's simple index lists the version, then runs
   [`scripts/relock_release.sh v0.N.0`](../../scripts/relock_release.sh)
   on the branch that holds the tag: the floor in `pyproject.toml` becomes
   `"xuepy>=0.N,<0.N+1"`, `uv lock --refresh-package xuepy` runs, the lock
   is checked to hold exactly 0.N.0, and `chore: relock on the published
   xuepy 0.N.0` is pushed to `release/0.N`. A push with the workflow token
   starts no workflow, so the job then dispatches `test.yml` on the branch;
   wait for that run too. If the job failed, run the script by hand from a
   fetched checkout, or dispatch the workflow with its `relock_tag` input
   (`dry_run` shows the commit without pushing).
5. **Merge into `main`** with a fast-forward, so the change, the release
   commit and the relock land on `main` together and keep their history.
   If `main` moved on meanwhile, rebase the branch onto it first (the tag
   stays on the published release commit), push it and wait for its
   tests. Then delete the branch.

A manual dispatch of either workflow is a rehearsal: `cargo publish
--dry-run`, or wheels uploaded as workflow artifacts only.

Gate each step on its exit code. Do not chain a commit or push behind a
pipe such as `uv lock | tail`, which hides a failure.

## Why the floor tracks the current release

The wheel carries no table of variables, bundles or sources. It reads the
registry document this tree hands it on every call
(`xuebuild.registry.document_json()`, `docs/registry.md`), and what it
carries instead is the set of *rules* that document can name: the
`registrySchema` it implements, the `conversion` and `match.gdal` rule
names, the `derivation.kind`s and the codebook types (`docs/encoder.md`
§"The registry document"). A wheel handed a document that names a rule it
does not implement refuses the whole document before reading any input,
and the scheduled forecast workflows pin the native encoder, so an older
wheel fails the build instead of quietly building less.

The floor is therefore the first release that implements every rule name
this tree's document uses and its `registrySchema`, and the upper bound
keeps an unrelocked tree from picking up a wheel whose rules have moved
on. A table change — a new source, a new bundle, a new level, a changed
codebook or input list — moves no rule and no schema: it is a commit to
`main` with the regenerated snapshots, and no release follows.

## Why the release lives on a branch

If an encoder change (a new rule, derivation, codebook type or ingest path,
or a schema bump) merged to `main` before its wheel shipped, `main` would
still install the old locked wheel, which refuses the document. Then
`tests/test_native.py` (byte parity between the two encoders) and the
scheduled publishes, which pin the native encoder, would fail until the
relock. Raising the floor before the wheel is indexed is worse: `uv sync`
fails everywhere, because the version cannot be resolved. Keeping steps
1–4 on `release/0.N` confines both failure windows to the branch.

Workflows that publish a source whose document a released wheel may not
implement yet (`publish-jma.yml` and the other rolling windows except
MRMS) resolve the encoder at job time with `native.knows_source`, which
hands the wheel the document and reads its answer, and fall back to the
reference pipeline with a warning.

## Where xuebuild gets the wheel

`xuepy` comes from PyPI like every other dependency, and `uv.lock` holds
its `files.pythonhosted.org` URLs and hashes. The wheels attached to the
GitHub release are the same files, kept as a record of the release; no
build downloads them.

## Deploy order

The Pages shell ([`deploy-pages.yml`](../../.github/workflows/deploy-pages.yml))
deploys automatically on every push to `main` that touches `web/`,
`rust/`, the build configuration or the documents `llms-full.txt` is built
from. A new shell accepts old data, but an old cached shell rejects new
data, so the shell must be live **before** the bucket carries a widened
manifest schema, a container or bundle metadata version bump, or a new
source id (the rule, in
[`delivery-and-deploy.md`](../../.claude/rules/delivery-and-deploy.md)).

In practice: merge the frontend change, let `deploy-pages.yml` finish,
then release the wheel and let the publishes run. A tab whose validator
rejects a live manifest or store reloads itself once per manifest, which
covers viewers holding the old shell. A new bundle of a known shape needs
no shell change: the shell renders an unknown field generically.

The data API Worker (`rust/xue-worker`) deploys separately with
`make deploy-api` / `deploy-api.yml`.

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
relock. Raising the floor before the wheel is on PyPI is worse: `uv sync`
fails everywhere, because the version cannot be resolved. Keeping steps
1–4 on `release/0.N` confines both failure windows to the branch.

Workflows that publish a source whose document a released wheel may not
implement yet (`publish-jma.yml` and the other rolling windows except
MRMS) resolve the encoder at job time with `native.knows_source`, which
hands the wheel the document and reads its answer, and fall back to the
reference pipeline with a warning.

## The own index

PyPI is the public channel for `xuepy`. The repository also keeps its own
[PEP 503](https://peps.python.org/pep-0503/) index of the wheels attached
to its GitHub releases, for its own builds:
<https://ringsaturn.github.io/xue/simple/>. Each link points at the
release asset with its `sha256`, and these are the files PyPI received, so
both indexes resolve to the same hashes. The index starts at `v0.3.0`, the
first release that carries wheels.

How it stays current:

1. `release-xuepy.yml` attaches the wheels to the release.
2. When it finishes, [`release-wheels-csv.yml`](../../.github/workflows/release-wheels-csv.yml)
   runs `scripts/build_simple_index.py`, which records the new assets in
   [`.github/release_wheels.csv`](../../.github/release_wheels.csv) (only
   those uploaded by `github-actions[bot]`), commits the file to `main`
   and dispatches `docs-rust.yml`.
3. [`docs-rust.yml`](../../.github/workflows/docs-rust.yml) renders the
   index from the CSV alone into `simple/` beside the rustdoc preview and
   deploys both. A GitHub Pages deployment replaces the whole site, so this
   one workflow owns every path on it. The site carries HTML only, never a
   wheel; the build refuses an artifact that holds one.

A dispatch of `release-wheels-csv.yml` refreshes the CSV by hand. To
rebuild it from every release, run the script with the workflow's
arguments plus `--full-fetch` and commit the result.

To install from it:

```sh
uv pip install xuepy \
  --index-url https://ringsaturn.github.io/xue/simple/ \
  --extra-index-url https://pypi.org/simple
```

`pyproject.toml` carries the `[[tool.uv.index]]` (`explicit = true`) and
`[tool.uv.sources]` blocks that pin `xuepy` to this index, commented out.
Enabling them changes xuepy's source in `uv.lock`, so it is a commit of
its own: uncomment both blocks, `uv lock --refresh-package xuepy`, check
that only xuepy's `source` line changed (the hashes are the same), and
commit `pyproject.toml` with `uv.lock`. Do it only once the index lists the
locked version; an explicit index that does not list it fails resolution.

Until then the release flow above is unchanged: the relock waits for the
wheel on PyPI. PyPI stays required for anyone installing `xuepy` without
this index, so every `v*` tag still publishes there.

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

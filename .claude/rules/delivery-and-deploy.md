---
paths:
  - ".github/workflows/**"
  - "Makefile"
  - "mk/**"
  - "scripts/**"
  - "xuebuild/{manifest,stac,stacindex,assemble,native}.py"
  - "rust/xue-worker/**"
  - "rust/**/Cargo.toml"
  - "pyproject.toml"
  - "wrangler.jsonc"
---

# Delivery, deploy and release rules

Background: [`docs/contribution/delivery.md`](../../docs/contribution/delivery.md),
[`publishing.md`](../../docs/contribution/publishing.md),
[`releasing.md`](../../docs/contribution/releasing.md).

## The data contract

- Per source, the only mutable object is the live pointer
  (`latest.json` for GFS, `latest-<model>.json` otherwise). Uploading it is
  what takes a run live, so it is **always written last**, after every
  artifact and the manifest it names.
- Everything else is immutable and addressed with `?v=<crc32>`. **Never
  overwrite an object under an unchanged `?v=`**: viewers' range requests
  would decode the wrong bytes. Rolling windows publish each rebuild under
  a new `--round HHMM` directory for this reason.
- Manifest paths resolve relative to the manifest URL. Production hostnames
  appear in the frontend only through `web/src/site.ts` (and are mirrored in
  `xuebuild/stacindex.py`).
- R2 bills per operation: publish few, large objects. Point products are
  one JSONL per issue plus byte offsets in the index, never one object per
  station. Keep the multipart threshold in `scripts/aws-config`.

## Two-sided deploys: shell first

An old cached shell rejects what it does not know. **Deploy the Pages shell
before publishing data** that:

- widens the manifest schema, bumps the container version or bumps the
  bundle metadata version;
- comes from a **new source id** (`FORECAST_MODELS` in `web/src/manifest.ts`
  must know it);
- needs new frontend chart knowledge to render well. (An unknown bundle id
  is rendered generically, so it is not strictly required.)

## Encoder selection and releases

- `publish.yml` (every forecast) and `publish-mrms.yml` pin
  `XUE_ENCODER=native`, so a silent fallback to the slow path fails loudly.
  Do not unpin them. The other rounds workflows (`jma`, `cma`, `aurora`,
  the satellites, `showcase.yml`) resolve the encoder with
  `native.knows_source`. The installed wheel may predate a source, so they
  fall back to the reference pipeline with a `::warning::`.
- `pyproject.toml`'s `xuepy` floor tracks the current release, because the
  wheel's source table must match `sources.py`. After an encoder or
  source-table change merges, `main` still has the old wheel locked, so
  native parity tests and the affected scheduled publishes stay red until
  the relock. This is expected. The sequence is release commit → `v*` tag →
  wait for PyPI → relock commit, which raises the floor and runs
  `uv lock --refresh-package xuepy`. Never raise the floor before the wheel
  is on PyPI. Use the `release` skill.
- Never chain a commit or push behind a pipe that hides an exit code
  (`make test | tail`, `uv lock | grep`). Gate on the command's own status.

## Never

- Never name the private CMA archive's sync repository or its bucket in
  this repo, its docs or any published metadata (`XUE_CMA_ARCHIVE` is the
  only handle).
- Never reference the private `plans/` symlink in commits, code or docs.
- Never commit generated or fetched data (`data/`, `dist*/`,
  `web/public/data/<model>.<run>/`, `web/src/wasm/`,
  `tests/fixtures/generated/`).
- Never run upload, prune or deploy targets without being asked: they act on
  the production bucket.

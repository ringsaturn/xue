# AGENTS.md

Guidance for coding agents (Codex, Claude Code, …) working in this
repository. `CLAUDE.md` is a symlink to this file.

## What this is

Xue packs global weather forecast runs and observation windows into
per-variable Zarr v3 stores laid out for playback, and plays them back in a
static browser page (MapLibre + a WebGL2 layer + a Rust/wasm decoder). The
single-file `.xue` container is no longer published but is still the
encoders' intermediate and is read by every decoder forever.

| Path | What |
|---|---|
| `xuebuild/` | Python build pipeline and **reference encoder** (fetch → GDAL → quantize → residuals → zstd → container → Zarr store → manifest → STAC) |
| `rust/xue` | Rust decoder; native encoder behind the off-by-default `encoder` feature |
| `rust/xue-wasm`, `rust/xue-py`, `rust/xue-worker` | wasm bindings (→ `web/src/wasm/`), PyO3 wheel `xuepy` (imported as `xue`), read-only data API Worker |
| `web/src/` | TypeScript frontend: manifest resolution, decode workers, WebGL layers, playback |
| `tests/` | Python unit/parity tests, `tests/web` (vitest), `tests/e2e` (Playwright) |
| `docs/` | Normative specs (`format.md`, `zarr-profile.md`, `stac.md`, `encoder.md`, products) |
| `docs/contribution/` | Developer docs: architecture, pipeline, sources, frontend, publishing, releasing |
| `.claude/rules/` | The rules below, in detail; path-scoped (Claude Code loads each when you touch matching files; other agents follow the table below) |
| `.agents/skills/` | Task playbooks (also reachable as `.claude/skills/`) |

## Commands

```sh
uv sync && make check            # toolchain
make wasm                        # wasm decoder into web/src/wasm/ (needed before any frontend build)
make test                        # rust + python + web unit tests (incl. encoder parity)
make mvp MODEL=gfs && make serve # build one run end to end and preview it
npm run dev                      # vite dev server
make help                        # every target
```

Single tests and the full matrix: [`.claude/rules/workflow.md`](.claude/rules/workflow.md).

## Read before you change…

| You are touching | Read |
|---|---|
| anything in more than one of `xuebuild/`, `rust/`, `web/src/`; the format; a registry | [`.claude/rules/cross-implementation.md`](.claude/rules/cross-implementation.md) |
| `xuebuild/`, `rust/xue/src/encode/`, a source or variable | [`.claude/rules/encoder.md`](.claude/rules/encoder.md), [`docs/contribution/sources.md`](docs/contribution/sources.md) |
| `web/src/` | [`.claude/rules/frontend.md`](.claude/rules/frontend.md), [`docs/contribution/frontend.md`](docs/contribution/frontend.md) |
| manifests, pointers, R2, workflows, releases, deploys | [`.claude/rules/delivery-and-deploy.md`](.claude/rules/delivery-and-deploy.md) |
| tests, commits, docs | [`.claude/rules/workflow.md`](.claude/rules/workflow.md) |

## Rules that always apply

1. **Three implementations, one format.** The Python encoder is the
   reference. The native encoder must match it byte for byte. A format
   change updates spec + both encoders + all readers + fixtures together.
2. **Decoders never drop a version.** Published `.xue` (v1/v2) and older
   metadata schemas stay readable forever.
3. **Pointer last, never overwrite under an unchanged `?v=`.**
4. **Shell before data.** Deploy the Pages shell before publishing a widened
   schema or a new source id.
5. Use `.venv/bin/python`. Do not run Playwright locally. Do not run upload,
   prune or deploy targets unless asked.
6. Never commit generated data. Never reference `plans/`. Never name the
   private CMA archive's repository or bucket.
7. Small, lowercase, imperative commits (`web: …`, `fix: …`). Do not hide
   exit codes behind pipes when gating a commit or push.

## Skills

| Skill | Use for |
|---|---|
| `add-source` | a new model or observation feed end to end |
| `add-variable` | a new bundle, level or derived field on an existing source |
| `format-change` | anything that changes bytes on disk or the manifest/metadata schema |
| `release` | cutting a `xuepy` / crate release and relocking |
| `synoptic-analysis` | reading the published catalog to analyse the weather (user-facing) |

---
name: release
description: Cut a Xue release — bump the xue crate and xuepy wheel versions, tag v*, let CI publish to crates.io and PyPI, then relock xuebuild on the new wheel. Use when asked to release, publish a new xuepy/crate version, ship encoder or source-table changes to the scheduled workflows, or relock after a release.
---

# Releasing `xue` / `xuepy`

The full background is in `docs/contribution/releasing.md`. One `v*` tag
releases the crate (`release-crate.yml`) and the wheels
(`release-xuepy.yml`). Both refuse a tag that differs from the manifest
version.

## Preconditions

- The encoder or source-table change is on `main` (or was proven on a
  `release/**` branch, where `test.yml` builds the wheel from the tree).
- If the release carries a new source id or a widened schema, the Pages
  shell with that knowledge is **already deployed**.
- The working tree is clean. Find the current version with
  `grep -m1 '^version' rust/xue/Cargo.toml`.

## Steps

1. **Release commit**: set `version = "0.N.0"` in `rust/xue/Cargo.toml` and
   `rust/xue-py/Cargo.toml`, then refresh the lock:
   ```sh
   (cd rust && cargo update --workspace)
   git add rust/xue/Cargo.toml rust/xue-py/Cargo.toml rust/Cargo.lock
   git commit -m "chore: release 0.N.0"
   ```
   Commit nothing else in it.
2. **Tag and push** (ask before pushing: it publishes):
   ```sh
   git tag v0.N.0 && git push origin main v0.N.0
   ```
3. **Wait for PyPI**: watch both workflows (`gh run list --workflow release-xuepy.yml`,
   `gh run watch <id>`) and confirm with `pip index versions xuepy` or the
   PyPI page. Do not continue on a red run.
4. **Relock commit**:
   ```sh
   # pyproject.toml: "xuepy>=0.N,<0.N+1"
   uv lock --refresh-package xuepy
   git add pyproject.toml uv.lock
   git commit -m "chore: relock on the published xuepy 0.N.0"
   ```
   Then `uv sync` and `make test`. Push after the tests pass.

## Rules

- Gate every step on its own exit status. Never `uv lock | tail` or
  `make test | grep` in front of a commit or push.
- Never raise the floor before the wheel is on PyPI: `uv sync` would fail
  everywhere.
- Between merging an encoder change and the relock, `tests/test_native.py`
  and the scheduled forecast publishes (which pin `XUE_ENCODER=native`) are
  expected to fail. Say so when you report, rather than "fixing" it.
- A manual dispatch of either workflow is a rehearsal (dry-run / artifacts
  only) and is safe to run.

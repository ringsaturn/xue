---
name: release
description: Cut a Xue release on a release/0.N branch — bump the xue crate and xuepy wheel versions, tag v* on the branch, let CI publish to crates.io and PyPI and relock xuebuild on the new wheel, then fast-forward main. Use when asked to release, publish a new xuepy/crate version, ship encoder or source-table changes to the scheduled workflows, or relock after a release.
---

# Releasing `xue` / `xuepy`

The full background is in `docs/contribution/releasing.md`. One `v*` tag
releases the crate (`release-crate.yml`) and the wheels
(`release-xuepy.yml`). Both refuse a tag that differs from the manifest
version.

**Everything happens on `release/0.N`. `main` only receives the finished
release**: the change, the release commit and the relock, merged
together. `main` never holds an encoder change without its wheel, so
scheduled publishes do not fail during the release window.

## Preconditions

- If the release carries a new source id or a widened schema, the Pages
  shell with that knowledge is **already deployed** from `main`.
- The working tree is clean. Find the current version with
  `grep -m1 '^version' rust/xue/Cargo.toml`.

## Steps

1. **Branch** off `main` and bring the change in:
   ```sh
   git checkout -b release/0.N main
   # commit, cherry-pick or merge the encoder / source-table change here
   git push -u origin release/0.N
   ```
   `test.yml` runs on `release/**` and builds the wheel from the tree, so
   native parity is checked against this code. Wait for it to pass.
2. **Release commit** on the branch:
   ```sh
   # set version = "0.N.0" in rust/xue/Cargo.toml and rust/xue-py/Cargo.toml
   (cd rust && cargo update --workspace)
   git add rust/xue/Cargo.toml rust/xue-py/Cargo.toml rust/Cargo.lock
   git commit -m "chore: release 0.N.0"
   ```
   Commit nothing else in it.
3. **Tag on the branch and push.** Ask before pushing: it publishes.
   ```sh
   git tag v0.N.0 && git push origin release/0.N v0.N.0
   ```
4. **Wait for the publish**: `gh run list --workflow release-xuepy.yml`,
   `gh run watch <id>`, and the same for `release-crate.yml`. Do not
   continue on a red run.
5. **Wait for the relock**, which CI makes: `release-relock.yml` runs
   when `release-xuepy.yml` finishes, waits until PyPI lists the wheels,
   pushes `chore: relock on the published xuepy 0.N.0` to `release/0.N`,
   then dispatches `test.yml` on the branch. Watch both:
   ```sh
   gh run list --workflow release-relock.yml
   gh run list --workflow test.yml --branch release/0.N
   git pull   # the relock commit
   ```
   If the relock job failed, fix the cause and either dispatch the workflow
   with `relock_tag=v0.N.0` or run `scripts/relock_release.sh v0.N.0` from
   a fetched checkout. Never write the relock commit by hand while the job
   may still push one.
6. **Merge into `main`** as a fast-forward, which keeps the history:
   ```sh
   git checkout main && git merge --ff-only release/0.N && git push origin main
   git branch -d release/0.N && git push origin --delete release/0.N
   ```
   If `main` moved on in the meantime, rebase the branch onto `main` first
   (the tag stays on the published release commit), push it with
   `--force-with-lease`, wait for `test.yml`, then fast-forward.

## Rules

- Never commit the release or relock directly on `main`, and never merge
  an encoder change to `main` ahead of its release.
- Gate every step on its own exit status. Never put `uv lock | tail` or
  `make test | grep` in front of a commit or push.
- Never raise the floor before the wheel is indexed: `uv sync` would fail
  everywhere. The relock job does it at the right moment.
- A manual dispatch of either release workflow is a rehearsal (a dry run,
  or workflow artifacts only) and is safe to run.

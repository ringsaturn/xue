# Contributing

## Setup

```sh
uv sync            # .venv with NumPy and the xuepy wheel; always use .venv/bin/python
npm ci
make check         # GDAL, zstd, node, wasm-pack versions
make wasm          # the browser decoder; the frontend does not build without it
make mvp           # one GFS run and the frontend; `make serve` to view it
```

Full requirements: [docs/contribution/setup.md](docs/contribution/setup.md).

## Three implementations, one format

The Python encoder (`xuebuild/`), the Rust decoder and native encoder
(`rust/`) and the TypeScript frontend (`web/src/`) must agree. The Python
encoder is the reference. The rules for changing them together (order of
change, parity, shell before data) are in
[`.claude/rules/cross-implementation.md`](.claude/rules/cross-implementation.md)
and
[`.claude/rules/delivery-and-deploy.md`](.claude/rules/delivery-and-deploy.md);
the deploy steps are in
[releasing](docs/contribution/releasing.md#deploy-order).

## Tests

```sh
make test          # Rust + Python + web unit tests, including encoder parity
```

Single tests and what each suite holds:
[docs/contribution/testing.md](docs/contribution/testing.md). Browser tests
(`make test-e2e`) run in CI.

## Commits

- Subjects are lowercase and imperative, optionally with a scope:
  `web: keep the playhead on a new run`, `ci: …`, `docs: …`, `fix: …`.
- Never commit generated or fetched data: `data/raw/`, `data/work/`,
  `dist/`, `dist-deploy/`, `web/public/data/<model>.<run>/`,
  `web/src/wasm/`, `tests/fixtures/generated/`.

## More

Architecture, pipeline, sources, frontend, publishing and releasing:
[docs/contribution/](docs/contribution/README.md).
Coding agents: [AGENTS.md](AGENTS.md) and `.claude/rules/`.

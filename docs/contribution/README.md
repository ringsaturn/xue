# Developer documentation

How Xue is built, tested, published and released. For using the data, start
at the [README](../../README.md); for contribution rules, at
[CONTRIBUTING.md](../../CONTRIBUTING.md).

| File | What it covers |
|---|---|
| [architecture.md](architecture.md) | The three implementations, the delivery contract, container and metadata versions |
| [architecture-rationale.md](architecture-rationale.md) | Why the format looks like this: the size comparison, container vs store |
| [pipeline.md](pipeline.md) | The Python encoder (`xuebuild/`): registries, conversion, assembly |
| [sources.md](sources.md) | Engineering notes per source: fetch stages, grids, quirks |
| [delivery.md](delivery.md) | Zarr stores, the STAC catalog, the data API, R2 uploads |
| [frontend.md](frontend.md) | The shell (`web/src/`): sessions, layers, rail, probe, compare page |
| [point-products.md](point-products.md) | Tropical cyclones, airports, soundings and the station marks |
| [setup.md](setup.md) | Requirements and building a run locally |
| [testing.md](testing.md) | Test commands, what each suite holds, CI |
| [publishing.md](publishing.md) | Uploading runs, rolling windows, point products, STAC, showcase |
| [releasing.md](releasing.md) | Crate and wheel releases, the `xuepy` floor, deploy order |

Normative specifications, which win over anything here:

- [`docs/format.md`](../format.md): the `.xue` container and bundle metadata
- [`docs/zarr-profile.md`](../zarr-profile.md): the Zarr store profile
- [`docs/encoder.md`](../encoder.md): the native encoder and its parity rule
- [`docs/stac.md`](../stac.md): the STAC catalog
- [`docs/satellite.md`](../satellite.md): the satellite fetch stage and producers
- [`docs/tc.md`](../tc.md), [`docs/airport.md`](../airport.md),
  [`docs/sounding.md`](../sounding.md): the point products

Rules for coding agents are in [`AGENTS.md`](../../AGENTS.md) and
`.claude/rules/`; agent skills are in `.agents/skills/`.

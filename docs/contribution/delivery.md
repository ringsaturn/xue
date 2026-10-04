# Delivery: stores, catalog, API, uploads

The pointer/manifest contract is in
[architecture.md](architecture.md#delivery-contract); running uploads is in
[publishing.md](publishing.md).

## Zarr store

Each bundle is published as `<bundle>.zarr/`; [zarr-profile.md](../zarr-profile.md)
is normative. Choices the spec does not explain:

- **One shard per array** (whole axis, whole grid). A bucket bills per
  object: per-time-chunk shards made a GFS run ~5 000 objects. The shard
  index, one suffix range, is the whole index. Readers take the shard length
  from the chunk shape, so earlier per-time-chunk stores still read.
- **Inner chunks are regular six-frame chunks** of the bundle's tiles. They
  match the container's temporal groups only up to the first step change,
  so every chunk is re-encoded from codes (the export reports
  `comparableChunks` / `identicalChunks`).

Writing:

- `zarrstore.export_bundle` derives the store from the finished `.xue`
  (NumPy only). Both encoder paths call it (`binconvert` per bundle job,
  `native.py::_zarr_reports` after the wheel), so stores are identical;
  `tests/test_native.py` compares them byte for byte.
- `build-bin --zarr` / `XUE_ZARR=1` turns it on. `--no-xue` /
  `XUE_CONTAINER=0` (`container: false` in `publish.yml`) retires the `.xue`
  after the store **and the video** have been derived from it
  (`binconvert.retire_container`); the manifest then names the store alone.
- `xue export-zarr <bundle.xue> [--delta] [--index-location start|end]` by
  hand. The `zarr` dependency group is for tests and delta stores only.

Manifest: a bundle entry and each variant may carry `zarr: {path,
byteLength, crc32}` (`crc32` of the group `zarr.json`, also the store's
`?v=`), validated by all three validators and carried through
`assemble-run`. The container fields are present whole or absent whole; an
entry with neither delivery is refused.

Browser (`web/src/zarr/`, the default channel unless `?backend=xue`):

- Fallback order: store by ranges → `.xue` by ranges → `.xue` whole → store
  by whole objects (only when there is no container). Probes stream or fail.
- `zarr/worker.ts` speaks the `worker.ts` protocol; `shard.ts` parses the
  CRC-32C-checked index once per shard and maps frame + tile to a span (a
  chunk may straddle container groups); `store.ts` coalesces one shard's
  ranges per microtask (gap ≤ 64 KB). Decoding is wasm `decodeChunk`.
- Compressed chunks sit in an LRU under `payloadBudgetBytes`
  (`main.ts::zarrPayloadBudgetBytes`), bounding memory by the prefetch
  window rather than the axis; a decode re-fetches what eviction took.
- A tab that refuses a live manifest or cannot open a live store reloads
  once per manifest (`main.ts::reloadForNewerShell`), the insurance behind
  shell-first deploys.
- `tests/web/zarr.test.ts` holds frames and series byte-identical to the
  `.xue` path; `npm run measure:backends` compares backends.

## STAC catalog (`xuebuild/stac.py`)

A static STAC 1.1.0 catalog beside the shell's JSON; the frontend never
reads it. [stac.md](../stac.md) defines the documents, including the point
products'.

- Every document is a **pure function** of the manifest (or a point
  product's `index.json`), the catalog row and `sources.py`: no timestamps,
  no hosts, relative links. So the split-build and top-up identity tests
  cover them.
- Written by the CLI, not the converter: `build-bin` (whole run),
  `assemble-run`, `xue stac`. A `--bundles` piece writes none. Point
  products write theirs in their own build, after the index and the
  pointer (`write_point_product_documents`).
- The live Item `<source>/item.json` is the run's Item relocated
  (`relocate_item`) so its path outlives the run.
- Licence and provider prose: `_source_prose`, `_point_product_prose`, held
  to the registry by `tests/test_stac.py`. `stacindex.py` renders the root
  `index.html` from the same prose; its hostnames mirror `web/src/site.ts`.
- Uploads: the run Item no-cache with the manifest; Collection, live Item
  and root through `upload-r2-stac-collection` (run by
  `upload-r2-pointer`); showcase documents by `upload-r2-showcase`; a point
  product's by `upload-r2-<product>`.

## Data API (`rust/xue-worker`)

A read-only JSON Worker (`xue-api.ringsaturn.me`): adds no format, writes
nothing, and the shell does not depend on it. Endpoints `/health`,
`/v1/catalog`, `/v1/sources/{source}`, `/v1/point` (`time`, `run`).

- Resolves Collection → pointer → manifest, reads stores by R2 range
  (`DATA` binding) and decodes with the crate's `decode_chunk`.
- Read geometry lives IO-free in `rust/xue/src/zarr.rs`. The browser's
  `web/src/zarr/shard.ts` is a second implementation of it; both are held
  to [zarr-profile.md](../zarr-profile.md).
- The binding is `remote: true` (honoured by `wrangler dev` only), so
  `make api-dev` runs locally against the real bucket; `make api-dev-cdn`
  (`DATA_SOURCE=cdn`) reads the public origin when the network blocks that.

## Uploads to R2 (`scripts/aws-config`)

All uploads go through the AWS CLI (`$(S3)` in `mk/r2.mk`).

- **One operation per object.** R2 bills each multipart step as Class A, and
  the CLI's 8 MiB default split large shards into ~7. `AWS_CONFIG_FILE`
  points at `scripts/aws-config`, which sets `s3.multipart_threshold` to
  5 GB. It names no profile; credentials stay in the environment. The
  workflows' credential probe is a `head-object` (Class B), not a listing.
- **Order:** `upload-r2-bundles` (artifacts), then `upload-r2-manifest`
  (`check-pointer`, manifest + run Item, then `upload-r2-pointer`);
  `upload-r2` is both. The pointer goes last because it takes the run live;
  `check-pointer` refuses a pointer that names another run or whose CRC32
  disagrees with the manifest on disk.
- **Never overwrite an object under an unchanged `?v=`:** viewers' range
  requests would decode the wrong bytes.

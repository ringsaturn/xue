# Web test fixtures

`river.ts` writes the synthetic river-case data the river tests read, in
memory, from Node built-ins alone (no Python, no pyarrow, no network):

- `buildReachesParquet(reaches, rowGroupRows)`: a GeoParquet of reaches in
  row groups of `rowGroupRows` rows — `feature_id` (INT64), `geometry` (WKB
  LineString) and the `bbox` covering struct (four FLOATs with per-row-group
  min/max statistics) — hand-encoded with a thrift-compact footer and
  PLAIN, uncompressed data pages. `sampleReaches` lays the row groups out
  west to east so that a view box selects some of them.
- `buildNetworkStore(options)`: a network store (`docs/zarr-profile.md`,
  "Network store") over the same feature count — the group with its
  `xue_network` block and consolidated metadata, the one-shard `streamflow`
  array with or without the `xue.delta` chain under Node's zstd (with the
  content checksum), optionally with inner chunks the shard never holds,
  and the `time` and `baseflow` side arrays. `fixtureCode(frame, feature)`
  is the code it writes at each cell.

The tests import the builders (`river-parquet.test.ts`,
`river-store.test.ts`). To look at the bytes, write them to a directory
(Node 22.15 or later):

```sh
node tests/web/fixtures/river.ts /tmp/river-fixture
```

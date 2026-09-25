# xue data API — MVP

A small read-only HTTP API over the data already on the public bucket. It adds
no format, no artifact and no server state: it resolves a run the way the STAC
catalog does (Collection → live pointer → manifest), reads the bundle's Zarr
store over range requests, and decodes with the same `rust/xue-wasm`
`decodeChunk` the browser uses.

Performance is deliberately not the goal of this MVP: a point series issues one
range read per time chunk (27 for a GFS run) plus the metadata reads, with no
caching beyond the CDN. See `plans/025-cf-worker-api.md` for the ranked
optimizations (a series-major companion file turns the 27 reads into one).

## Run

```sh
npm run api          # bundle with esbuild, then serve on http://127.0.0.1:8788
PORT=9000 npm run api
XUE_DATA_ORIGIN=http://localhost:4173/data/ npm run api   # a local build instead of the bucket
```

`npm run api:build` only bundles (`api/dist/server.mjs`); `node api/dist/server.mjs`
runs it. The wasm is read from `web/src/wasm/` (run `make wasm` if it is missing),
overridable with `XUE_WASM_DIR`.

## Endpoints

| Path | What |
| --- | --- |
| `GET /health` | liveness |
| `GET /v1/catalog` | the 19 collections (id, title, href) |
| `GET /v1/sources/{source}` | the live run's summary **and its full variable list** (`bundles`, from the live STAC Item) |
| `GET /v1/point?source=&lat=&lon=&variables=&time=&run=` | one cell's series |

`variables` is a comma-separated list of **bundle ids** (the keys of
`/v1/sources/{source}`'s `bundles`, e.g. `tmp2m`, `wind10m`); it defaults to the
run's first bundle. `time` is an optional ISO 8601 valid time; without it the
whole axis is returned.

`run` optionally names the run instead of following the live pointer. It accepts
the STAC Item id (`gfs.2026092506`, `himawari.2026092507.1226`), the run
directory (`himawari.2026092507/1226`), or a plain run id, and skips reading the
Collection and pointer:

- **with `run`** the answer is stable for that run, so it is served
  `Cache-Control: public, max-age=86400` — a good cache key.
- **without `run`** it follows the live run and is served `max-age=30`. The
  response carries `x-xue-run` (also the `run` field) so a client can pin it.

An unknown run is `404 unknown_run`; an unknown source, `404 unknown_source`.

```sh
curl "http://127.0.0.1:8788/v1/point?source=gfs&lat=35.7&lon=139.7&variables=tmp2m&time=2026-09-25T18:00:00Z"
```

```json
{
  "source": "gfs",
  "run": "gfs.2026092506",
  "runTime": "2026-09-25T06:00:00Z",
  "request": { "lat": 35.7, "lon": 139.7, "variables": ["tmp2m"] },
  "cell": { "column": 1279, "row": 217, "longitude": 139.75, "latitude": 35.75 },
  "time": { "unitSeconds": 3600, "times": ["2026-09-25T18:00:00.000Z"] },
  "variables": { "tmp2m": { "label": "2 meter temperature", "unit": "°C", "values": [21.5] } }
}
```

A vector bundle returns its components under `components`; a point outside a
regional model's grid is `422 point_off_grid`.

## Shape

`api/lib/api.ts` holds the platform-neutral `handle(Request) → Response` and the
routing; `api/server.ts` bridges it to `node:http`. The same `handle` can be
dropped into a Cloudflare Pages Function later, with `api/lib/bucket.ts` (the
only place that knows the data origin) switched to an R2 binding.

`api/lib/grid.ts` and `api/lib/quantize.ts` are small mirrors of
`web/src/probe.ts` and `web/src/palettes.ts`, kept inline so the API does not
pull the frontend's i18n/palette chain; those modules are the reference.
`api/lib/point.ts` reuses `web/src/zarr/shard.ts` unchanged.

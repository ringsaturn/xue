# The data API

> **Experimental.** The API may change or break without notice, including
> routes, parameters and response shapes, and it has no versioning promise
> beyond the `/v1` prefix. It runs on a best-effort basis with no
> guarantee of availability or of freshness: a run can reach the API late,
> or not at all. For anything that must keep working, read the static
> catalog and the Zarr stores directly (`docs/stac.md`,
> `docs/zarr-profile.md`), which are the published data contract.

`https://xue-api.ringsaturn.me` is a read-only JSON service over the
published dataset. It resolves a run the way the STAC catalog does
(Collection → live pointer → manifest), reads the bundle's Zarr store with
ranged requests and decodes the quantized codes with the same `xue` crate
the viewer's wasm decoder uses. It writes nothing. The viewer does not
depend on it, so the site and the catalog work when it is down.

This document is the contract. `rust/xue-worker/static/openapi.json` is
its machine copy, served at `/v1/openapi.json` with the live source list
filled in and rendered at `/docs` (Swagger UI). A test holds the two to the
same set of routes.

Everything a response says comes from the bucket at request time: the
sources, a run's variables, grids and codebooks. A new source, variable or
run needs no deploy of the API.

## Endpoints

All are `GET` (and `HEAD`), answer `application/json; charset=utf-8` and
carry `access-control-allow-origin: *`. `OPTIONS` answers the CORS
preflight. Unknown query parameters are ignored.

| Route | Answers | `Cache-Control` |
|---|---|---|
| `GET /health` | `{status, build, now}`; never touches the bucket | `no-store` |
| `GET /v1/openapi.json` | the OpenAPI 3.1 contract, its `source` enum the live Collections (also at `/openapi.json`) | `max-age=60` |
| `GET /v1/catalog` | `{dataOrigin, collections: [{id, title, href}]}`, one entry per child of the root catalog | `max-age=60` |
| `GET /v1/sources/{source}` | one source's live run (§ Sources) | `max-age=60` |
| `GET /v1/point` | one cell's series for one or more bundles (§ Point) | `max-age=30`, or a day with `run=` |

Every response carries `x-xue-api` (the build) and `x-xue-ms` (the
Worker's own wall time, a diagnostic, not part of the contract).

## Point

| Parameter | Required | Meaning |
|---|---|---|
| `source` | yes | a Collection id: `gfs`, `ecmwf`, `mrms`, `himawari`, … |
| `lat` | yes | degrees, −90 to 90 |
| `lon` | yes | degrees, −180 to 360; a longitude past 180 wraps onto the same cell |
| `variables` | no | bundle ids, comma-separated (`tmp2m,wind10m`) or repeated (`variables=tmp2m&variables=wind10m`); the run's first bundle when absent |
| `time` | no | an ISO 8601 instant; the frame nearest to it is returned. Without it the whole axis is |
| `run` | no | pin a run instead of following the live pointer: a STAC Item id (`gfs.2026092506`, `mrms.2026092509.1455`), a run directory (`mrms.2026092509/1455`) or a run id |

The response:

```json
{
  "source": "gfs",
  "run": "gfs.2026100400",
  "runTime": "2026-10-04T00:00:00Z",
  "request": { "lat": 35.7, "lon": 139.7, "variables": ["tmp2m", "wind10m"] },
  "timezone": "Asia/Tokyo",
  "cell": { "column": 1279, "row": 217, "longitude": 139.75, "latitude": 35.75 },
  "time": { "unitSeconds": 3600, "times": ["2026-10-04T00:00:00.000Z", "…"] },
  "variables": {
    "tmp2m":   { "bundle": "tmp2m",   "label": "2 metre temperature", "unit": "°C",  "values": [21.5, "…"] },
    "ugrd10m": { "bundle": "wind10m", "label": "10 metre u wind",     "unit": "m/s", "values": [3.1, "…"] },
    "vgrd10m": { "bundle": "wind10m", "label": "10 metre v wind",     "unit": "m/s", "values": [-1.2, "…"] }
  }
}
```

- `variables` is a flat map keyed by variable id, whatever was asked for.
  A vector bundle answers as its components, each naming its `bundle`. The
  API does not derive speed or direction.
- `values` follows `time.times` one to one. `null` is a missing code (the
  variable's `nodataCode`, or a chunk the store never wrote). Values are
  the codebook's: a linear field is quantized to its step (0.5 °C for
  `tmp2m`), a log1p field (`prate`, the aerosol fields) to its scale.
- `times` are UTC with milliseconds. On an observation window they are the
  frames' own times; on the aurora window they are valid times about an
  hour ahead of the clock (`docs/sources.md`).
- `cell` is the grid cell the point falls in, by the rule the viewer's
  probe and shader use (`xue::zarr::probe_cell`): nearest cell, longitude
  wrapped onto the grid's origin.
- `timezone` is the IANA zone of the requested point (not of the cell: on
  a coarse grid the cell centre can sit across a border from it), looked up
  in the `tzf-rs` polygon index compiled into the Worker. Open sea answers
  the nautical zone (`Etc/GMT+10`, whose sign is POSIX-inverted); `null`
  only where no polygon covers the point, which the bundled data does not
  have. `times` stay UTC; the zone is for the client to convert them with.
- A bundle whose manifest entry carries a `series` companion
  (`docs/zarr-profile.md`, "Series store") is read through it: one chunk
  for the whole axis. Otherwise its map store is read, one chunk per time
  chunk. The response is the same either way.

## Sources

`GET /v1/sources/{source}` reads the source's Collection and its live STAC
Item:

| Field | From |
|---|---|
| `source`, `title`, `description`, `license` | the Collection |
| `run` | the Item id, else the Collection's `xue:live` |
| `runTime` | the Item's `forecast:reference_datetime`, else its `datetime` |
| `bbox`, `time` | the Item's `bbox` and `cube:dimensions.time` |
| `variables` | the Item's `cube:variables` as a flat table: `{<variable id>: {bundle, label, unit}}` |

The ids `/v1/point?variables=` accepts are the distinct `bundle` values of
`variables`. A source with no live Item answers an empty table.

## Errors

```json
{ "error": { "code": "unknown_variable", "message": "…", "detail": { "available": ["tmp2m", "…"] } } }
```

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_parameter` | `source`, `lat` or `lon` missing or out of range; `time` not ISO 8601; `run` not a run name |
| 404 | `unknown_source` | no such Collection |
| 404 | `no_live_run` | the Collection names no live pointer, or the run publishes no bundles |
| 404 | `unknown_run` | `run=` names no manifest |
| 404 | `unknown_variable` | the run has no such bundle (`detail.available` lists those it has), or the bundle ships no store |
| 404 | `not_found` | no such route |
| 405 | `method_not_allowed` | anything but `GET`, `HEAD`, `OPTIONS` |
| 422 | `point_off_grid` | the point is outside a regional grid (HRRR, a satellite disk) |
| 502 | `upstream_failed` | a bucket read failed, or a document in it is not what the profile says |

Errors are not cached.

## Caching and freshness

- A `200` from `/v1/catalog`, `/v1/sources/{source}` or `/v1/point` is
  stored in the edge cache of the data center that served it, keyed by the
  full URL, for its `max-age`.
- A live answer can therefore lag a new run by up to 30 s (60 s for the
  catalog and source summaries), and two data centers can briefly answer
  from different runs. Pin `run=` for a stable answer; it caches for a day
  because a run's objects never change under it.
- Inside an isolate, the manifest is held by its pointer's crc, and store
  documents and shard indices by `path#crc32`. The Collection and the
  pointer are read on every uncached request, so the API never names a run
  `prune-r2` has deleted.

## Limits and attribution

- Requests are rate-limited per IP at the zone. The rule lives in the
  Cloudflare dashboard, not in this repository.
- There is no key, quota or SLA (see the experimental notice above).
- The data keep their sources' licences. NOAA products are public domain
  (attribution requested); ECMWF, EUMETSAT and JMA require attribution. The
  Collection's `license` and its `providers` (`/v1/sources/{source}`, or
  the STAC Collection) say which applies to a source.

## Not in this contract

- Spatial aggregates, area statistics, long-history stitching: those are
  the encoder's job, published as products, not computed per request.
- Point products (soundings, airports, storms) are read straight from the
  catalog for now (`docs/sounding.md`, `docs/airport.md`, `docs/tc.md`).

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).

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
the viewer's wasm decoder uses. The point products — soundings, airports,
surface stations, tropical cyclones — it reads through their own pointer
and index, one station per ranged read, and answers in physical units. It
writes nothing. The viewer does not depend on it, so the site and the
catalog work when it is down.

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
| `GET /v1/soundings` | the radiosonde stations of the live issue (§ Soundings) | `max-age=60`, or a day with `issue=` |
| `GET /v1/soundings/{station}` | one station's ascents | same |
| `GET /v1/airports` | every airport's newest observation (§ Airports) | same |
| `GET /v1/airports/{icao}` | one airport's METARs and TAF | same |
| `GET /v1/synop` | every surface station's current values (§ Surface stations) | same |
| `GET /v1/synop/{station}` | one station's 24-hour window | same |
| `GET /v1/storms` | the tropical systems of the live issue (§ Storms) | same |
| `GET /v1/storms/{storm}` | one storm's tracks | same |

Every response carries `x-xue-api` (the build) and `x-xue-ms` (the
Worker's own wall time, a diagnostic, not part of the contract). A point
read carries `x-xue-run`, a product read `x-xue-issue`: what the request
resolved to.

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

`kind` says what the source is and `endpoint` which route reads it:
`grid` and `/v1/point` for a run or an observation window, `point` and the
product's list route (`/v1/soundings`, `/v1/airports`, `/v1/synop`,
`/v1/storms`) for a point product; `null` for a point product the API only
names (`nexrad`). Asking `/v1/point` for a point product is `400
not_a_grid`, whose `detail.endpoint` is the right route.

## Point products

The soundings (`docs/sounding.md`), airports (`docs/airport.md`), surface
stations (`docs/synop.md`) and tropical cyclones (`docs/tc.md`) share one
shape on the bucket — a mutable `latest-<product>.json` naming an immutable
`<product>.<issue>/index.json`, the records beside it addressed by byte
span — and one shape here: a **list** route that answers the index, and a
**record** route that answers one station or storm. Each read costs the
pointer, the index (held in the isolate by its crc) and, for a record, one
ranged request of exactly that record's bytes.

Every product answer opens with the same four fields:

| Field | Meaning |
|---|---|
| `product` | `sounding`, `airport`, `synop`, `tc` |
| `issue` | the issue's STAC Item id: `sounding.2026091402`, `airport.202609161440` |
| `issued` | the aggregation hour, or the ten-minute round |
| `generated` | when the build ran |

and takes the same optional parameter:

| Parameter | Meaning |
|---|---|
| `issue` | pin an issue instead of following the live pointer: `<product>.<YYYYMMDDHH>` for the hourly products, `<product>.<YYYYMMDDHHMM>` for the ten-minute rounds. The hourly products' issues are found under their UTC day (`sounding/2026/09/14/sounding.2026091402/`), then flat; a pruned issue is `404 unknown_issue` |

The three station lists (`/v1/soundings`, `/v1/airports`, `/v1/synop`)
take the same spatial selection, and echo it as `request`:

| Parameter | Meaning |
|---|---|
| `lat`, `lon` | together: sort the stations by great-circle distance from this point, nearest first, and add `distanceKm` (to a tenth) to each |
| `radius` | with `lat`/`lon`: keep only stations within this many km |
| `bbox` | `west,south,east,north` in degrees; `west > east` spans the antimeridian |
| `limit` | at most this many stations: the nearest with `lat`/`lon`, the first in index order otherwise |

and `count` is the number of stations answered. The three record routes
take `time`, an ISO 8601 instant: only the record nearest to it is
answered (one ascent, one METAR, one observation time); without it the
whole window is. A station's `timezone` is its IANA zone, as on `/v1/point`.
The lists are the index with its byte spans removed and nothing added but
`distanceKm`; `sources` is the build's account of its inputs, verbatim.

### Soundings

`GET /v1/soundings` answers `stations[]`, each
`{id, wmo, name, lat, lon, elev, latest, times, headline}` as the index has
them (`docs/sounding.md` §4): `id` is the WIGOS identifier, `wmo` the
five-digit number, `headline` the newest ascent's `t500`/`td500` (°C),
`freezingLevel` (gpm), `pw` (mm) and `levels`.

`GET /v1/soundings/{station}` takes the WIGOS id or the WMO number and
answers:

```json
{
  "product": "sounding", "issue": "sounding.2026091402", "issued": "2026-09-14T02:00:00Z", "generated": "…",
  "station": { "id": "0-20000-0-47401", "wmo": "47401", "name": null, "lat": 45.415, "lon": 141.679, "elev": 2.9, "timezone": "Asia/Tokyo" },
  "units": { "p": { "label": "pressure", "unit": "hPa" }, "t": { "label": "temperature", "unit": "°C" }, "…": "…" },
  "soundings": [
    {
      "time": "2026-09-14T00:00:00Z", "launched": "2026-09-14T00:16:00Z", "sondeType": 41,
      "bulletin": "IUSC01 RJTD 140000", "gateway": "jp-jma-gts-to-wis2", "arrived": "2026-09-14T01:23:50Z",
      "n": 15, "reported": 15,
      "levels": { "p": [1008, 1000, "…"], "z": [null, 70, "…"], "t": [22, 20.6, "…"], "td": [17.2, "…"], "wd": [210, "…"], "ws": [4.6, "…"], "sig": [131072, "…"] },
      "derived": { "freezingLevel": 3292, "pw": 26.3, "lapse850_500": 6.1, "tropopause": null }
    }
  ]
}
```

- `soundings` is newest first, at most four nominal times (`time=` keeps
  one). The ascent's fields are the product's (`docs/sounding.md` §5).
- `levels` are the product's seven fixed-point arrays decoded: `p` Pa →
  hPa, `t` and `td` K × 100 → °C, `ws` m/s × 10 → m/s, `z` (gpm), `wd`
  (degrees the wind comes from) and `sig` (the BUFR 0 08 042 flag word)
  unchanged. `-32768` is `null`. The arrays are parallel, `n` long, and
  `p` is the axis: present on every level, strictly descending.
- `derived` is the product's, computed by the publisher from the fixed-point
  levels (`docs/sounding.md` §7), not recomputed here.

### Airports

`GET /v1/airports` answers `stations[]`, the index's compact rows named:
`{icao, lat, lon, elev, obsTime, t, td, wd, ws, gust, vis, qnh, category,
tafPresent}` (`docs/airport.md` §4), the values the newest observation's,
`tafPresent` a boolean. `category=` keeps one flight category (`VFR`,
`MVFR`, `IFR`, `LIFR`). `units` names each value's unit.

`GET /v1/airports/{icao}` takes the ICAO id in any case and answers
`station` (`icao, name, lat, lon, elev, iata, wmo, timezone`), `units`,
`metars` — the last 24 hours newest first, each a `Metar` of
`docs/airport.md` §5 verbatim (`time, raw, t, td, wd, ws, gust, vis, qnh,
slp, wx, cloud, category, auto, type`) — and `taf`, the current `Taf` or
`null`. The product's conventions hold: `vis` 10000 means at least 10 km,
`wd` is `null` for a variable wind, `raw` is the authority.

### Surface stations

`GET /v1/synop` answers `networks[]` (each network's entry of the index
without its file descriptor: `id, name, cadence, prPeriod, attribution,
license, url, latest`), `elements` (the element table, each
`{label, unit}`) and `stations[]`: `{id, network, lat, lon, elev, rank,
name, obsTime, values}`, `values` keyed by element with the station's
current value or `null`. `network=` keeps one network (`404
unknown_network` otherwise, `detail.available` listing them).
`attribution` is the credit a display of that network's data must show.

`GET /v1/synop/{station}` takes `<network>:<number>` and answers in the
shape `/v1/point` does, so a client reads a station and a grid cell with
one type:

```json
{
  "product": "synop", "issue": "synop.202610050010", "issued": "2026-10-05T00:10:00Z", "generated": "…",
  "station": { "id": "amedas:11016", "network": "amedas", "name": "Wakkanai", "names": { "ja": "稚内", "ja-Kana": "ワッカナイ" },
               "lat": 45.415, "lon": 141.6783, "elev": 3.0, "wmo": null, "rank": 0, "timezone": "Asia/Tokyo" },
  "network": { "id": "amedas", "name": "JMA AMeDAS", "cadence": 600, "prPeriod": 600, "attribution": "出典：気象庁ホームページ (Japan Meteorological Agency)", "license": "PDL-1.0", "url": "…", "latest": "…" },
  "time": { "unitSeconds": 600, "times": ["2026-10-04T23:50:00.000Z", "2026-10-05T00:00:00.000Z", "2026-10-05T00:10:00.000Z"] },
  "variables": {
    "t":    { "label": "air temperature", "unit": "°C",  "values": [15.0, 15.4, 15.7] },
    "gust": { "label": "gust",            "unit": "m/s", "values": [null, null, null] },
    "…": "…"
  }
}
```

- `time.times` is the station's window, oldest first, the product's epoch
  seconds written as ISO instants; `unitSeconds` is the network's cadence.
- `variables` has one entry per element of the index, whether or not the
  station reports it (an element it never reported is all `null`), with
  `values` following `times` one to one. There is no `bundle` field.

### Storms

`GET /v1/storms` answers `storms[]`, the index's rows (`docs/tc.md` §4)
without their file descriptors: `{id, level, basin, name, aliases, position,
vmax, pmin, class, classAgency, agencies, models, best}`, and `crosswalk`,
old id → current id. `basin=` and `level=` keep one basin (`AL`, `EP`,
`CP`, `WP`, `IO`, `SH`) or level (`A`, `B`, `C`), case-insensitive.

`GET /v1/storms/{storm}` takes a storm id, an id the issue's crosswalk maps
onto a current one, or one of a storm's aliases (`EP97`, `ecmwf:70W`), and
answers the storm document (`docs/tc.md` §5) with `resolvedFrom` naming
the id the request used when it was not the storm's own:
`id, level, basin, name, sid, intl, aliases`, then `best`, `agencies`,
`models`, `alert`, `impact` and `sources`. `include=` (comma-separated
section names) answers only those sections; the seven identity fields are
always present.

A model's ensemble (`docs/tc.md`'s `Ensemble`: `leads`, `members` and
flattened fixed-point arrays) is unpacked into one track per member:

```json
"gefs": {
  "issued": "…", "base": "2026-09-12T00:00:00Z", "run": "2026091200",
  "leads": [0, 21600, "…"],
  "members": [
    { "member": 0, "points": [ { "lead": 0, "time": "2026-09-12T00:00:00.000Z", "lat": 16.9, "lon": -126.0, "vmax": 21.6, "pmin": 996.0 }, "…" ] },
    { "member": 1, "points": [ "…" ] }
  ],
  "mean": { "…": "a Forecast, or null" }
}
```

`lat`/`lon` are degrees, `vmax` m/s, `pmin` hPa, `time` is `base + lead`;
a lead at which a member has no position is left out of its track, so a
member that never found the system has an empty `points`. Deterministic
forecasts and best tracks are passed through as the product writes them.

## Errors

```json
{ "error": { "code": "unknown_variable", "message": "…", "detail": { "available": ["tmp2m", "…"] } } }
```

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_parameter` | `source`, `lat` or `lon` missing or out of range; `time` not ISO 8601; `run` not a run name; `issue` not `<product>.<digits>`; `lat` without `lon`, `radius` without them, a malformed `bbox`, `limit` under 1; an `include` section that does not exist |
| 400 | `not_a_grid` | `/v1/point` asked for a point product; `detail.endpoint` is the route that reads it |
| 404 | `unknown_source` | no such Collection |
| 404 | `no_live_run` | the Collection names no live pointer, the run publishes no bundles, or a product publishes no live issue |
| 404 | `unknown_run` | `run=` names no manifest |
| 404 | `unknown_issue` | `issue=` names no index (pruned, or never published) |
| 404 | `unknown_variable` | the run has no such bundle (`detail.available` lists those it has), or the bundle ships no store |
| 404 | `unknown_station` | the issue has no such sounding station, airport or surface station |
| 404 | `unknown_network` | `network=` names no network of the issue (`detail.available` lists them) |
| 404 | `unknown_storm` | no storm, crosswalk entry or alias by that id (`detail.available` lists the storms) |
| 404 | `not_found` | no such route |
| 405 | `method_not_allowed` | anything but `GET`, `HEAD`, `OPTIONS` |
| 422 | `point_off_grid` | the point is outside a regional grid (HRRR, a satellite disk) |
| 502 | `upstream_failed` | a bucket read failed, or a document in it is not what the profile says |

Errors are not cached.

## Caching and freshness

- A `200` from any `/v1/` data route is stored in the edge cache of the
  data center that served it, keyed by the full URL, for its `max-age`.
- A live answer can therefore lag a new run by up to 30 s (60 s for the
  catalog, the source summaries and the point products), and two data
  centers can briefly answer from different runs. Pin `run=` or `issue=`
  for a stable answer; it caches for a day because a run's or an issue's
  objects never change under it.
- Inside an isolate, the manifest and a product's index are held by their
  pointer's crc, store documents and shard indices by `path#crc32`, and a
  station's byte span by `path#crc32:offset+length`. The Collection and
  the pointer are read on every uncached request, so the API never names a
  run or an issue the prune jobs have deleted.

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
- Derived quantities over a product's records (a skew-T index not in
  `derived`, a METAR decoded further than the service decodes it, a storm's
  impact area): the products define what is published and the API returns
  it; a client computes the rest.
- Anything older than the live issue that the bucket no longer holds. The
  API reads what the prune jobs leave (seven days of hourly issues, three
  hours of ten-minute rounds); the archive is not its concern.
- The single-site radar (`nexrad`): a polar Zarr product, named in
  `/v1/sources/nexrad` and read from the catalog (`docs/nexrad.md`).

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).

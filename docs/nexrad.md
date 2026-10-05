# The nexrad product

A single weather radar sees what no mosaic shows: the wind along its own
beams, and the reflectivity close to the antenna at a quarter of a
kilometre. The US WSR-88D network publishes both for every site as Level 3
products, and `nexrad` carries two of them for every site — the lowest
sweep's reflectivity (`n0b`) and radial velocity (`n0g`) — as a rolling
three-hour window of [polar stores](zarr-profile.md#polar-store), one
immutable store per product per five-minute round. Reflectivity mosaics
stay the `mrms` source's; velocity cannot be mosaicked at all, since each
radar measures it along its own beams. This document is the normative
description of the product, schema v1.

## 1. Delivery

Everything is a static file under the data root, beside the run pointers
and the point products.

- `latest-nexrad.json` is the one mutable object; fetch it with caching
  disabled. It names the newest round's window manifest:

  ```json
  {"schemaVersion": 1, "product": "nexrad", "issued": "2026-10-05T08:05:00Z",
   "path": "nexrad.202610050805/index.json", "byteLength": 61234, "crc32": "1c0ffee5"}
  ```

  The shape and the `?v=<crc32>` rule are the point products' pointer
  ([`tc.md`](tc.md) §1); `path` is always `nexrad.<round>/index.json`.
- `nexrad.<YYYYMMDDHHMM>/` is one immutable directory per round: the UTC
  minute the round was built for, a multiple of five. It holds one polar
  store per product, `n0b.zarr/` and `n0g.zarr/`, the round's
  `index.json` (§4) and its STAC `item.json`. Nothing in it changes once
  the pointer has named it. Rounds are flat under the data root.
- A round's stores hold the sweeps that became available since the
  previous round (§3), not the whole window: a round is written once and
  never rebuilt, and the window is the list of rounds its manifest names.
  Rounds older than the window are pruned after the pointer has moved past
  them.

Deployment is one-sided: a new pointer and new directories, which a shell
that does not know the product never requests.

A **showcase case** is a closed window, rebuilt never: it is written as
one [window store](zarr-profile.md#polar-store) per product for the whole
case instead of one round store per round, beside one window manifest:
`showcase/<id>/radar/n0b.zarr/`, `n0g.zarr/` and `index.json`. The rounds
are the same rounds (§3), replayed over the case's interval with a window
as long as the case; only where their sweeps are stored differs (§4). A
case has no pointer and no per-round directories or STAC items: the
showcase catalog names its `index.json`.

## 2. Products, codes and geometry

| product | source | quantity | unit | beams × gates | `quantization` | reserved codes |
|---|---|---|---|---|---|---|
| `n0b` | Level 3 N0B (153) | base reflectivity, lowest sweep | dBZ | 720 × 1840 | linear, `scale` 0.5, `offset` −33.0, codes 2–255 (−32.0 … 94.5 dBZ) | 0 below threshold, 1 range folded |
| `n0g` | Level 3 N0G (154) | base radial velocity, lowest sweep | m/s | 720 × 1200 | linear, `scale` 0.5, `offset` −64.5, codes 2–255 (−63.5 … 63.0 m/s) | 0 below threshold, 1 range folded |

- The codes are the source's own: the product's data levels are copied
  byte for byte, so the codebook above is the source's (`value = −32.0 +
  0.5 × (code − 2)` for N0B, `−63.5 + 0.5 × (code − 2)` for N0G, written
  as `code × scale + offset`). A file whose threshold header describes any
  other codebook is refused rather than requantized.
- Velocity sign: **positive is motion away from the radar**
  (`signConvention: "positive_away"`). The source's velocity is already
  dealiased; range-folded gates keep code 1.
- Geometry: beams every 0.5° from 0° (true north, clockwise), each source
  radial binned by its centre azimuth; gates every 0.25 km of slant range
  from the antenna, the first gate starting at 0. N0B therefore reaches
  460 km and N0G 300 km.
- `scan_time` is the sweep's own start time from the product header. For a
  supplemental low-level sweep (SAILS / MESO-SAILS) it is that sweep's
  start, not the volume's, so every sweep of a site has a distinct time;
  it equals the time in the source object's key.
- `elevation` is the sweep's elevation angle from the product header
  (nominally 0.5°).

## 3. Rounds

A round `R` (UTC, minute divisible by five) takes, per site and product,
every source sweep with `scan_time ≤ R` that no earlier round of the
window carries — the window manifest records each site's newest sweep, and
the next round starts after it. A sweep that arrives late therefore lands
in the next round rather than being lost, and no sweep is in two rounds.
`n0b` and `n0g` are assembled independently; a site may have a sweep in
one product's store and not yet in the other's.

A round's store lists only the sites with at least one sweep in it, in the
order of the window's site table (§4). A round in which no site has a new
sweep for a product writes no store for it.

## 4. `index.json`, the window manifest

A round's `index.json` is the window manifest: a read accelerator over the
round stores, which are complete without it. It takes the place a point
product's index takes, so the pointer is the point products' exactly.

```json
{
  "schemaVersion": 1,
  "issued": "2026-10-05T08:05:00Z",
  "generated": "2026-10-05T08:06:41Z",
  "windowSeconds": 10800,
  "sites": [["TLX", "KTLX", 35.33306, -97.27750, 384.0], …],
  "rounds": [
    {"round": "2026-10-05T05:10:00Z", "path": "../nexrad.202610050510/",
     "n0b": {"group": {"byteLength": 2104, "crc32": "…"},
             "shard": {"byteLength": 52114096, "crc32": "…"},
             "chunks": [[0, 0, 604642, 2], [3, 604642, 598210, 1], …],
             "scans": [[0, [1791133536, 1791133692]], [3, [1791133701]], …]},
     "n0g": { … }},
    …
  ],
  "sources": [ … ]
}
```

- `sites` is the window's site table: `[id, icao, latitude, longitude,
  height]`, the source's three-letter id, the four-letter ICAO, the
  antenna's WGS84 position and its height above mean sea level in metres.
  Every index below is a position in it.
- `rounds` is oldest first, every round inside `windowSeconds` before
  `issued`. `path` is the directory holding the round's stores, relative
  to this manifest: `../nexrad.<round>/` for a round store, `./` for a
  window store beside the manifest (a case, §1).
- Per product: `group` measures the store's root `zarr.json` (its `?v=`),
  `shard` the data array's one shard object (`<product>/c/0/0/0/0`), and
  `chunks` gives one `[site, offset, length, sweeps]` per site the store
  holds, in store order: the byte span of that site's inner chunk in the
  shard and how many of its `scan` slots are real sweeps. A reader fetches
  `[offset, offset + length)` of the shard directly, without the shard
  index, decompresses it to `depth × 720 × gates` bytes and keeps the
  first `sweeps` slots. `depth` is the store's inner chunk depth: in a
  round store the largest `sweeps` of the round's rows; in a window store
  the product block says it as `"depth": 3`, the same in every round,
  since the store's chunk is padded to its busiest site's busiest round.
  In a window store every round's `group` and `shard` measure the same
  two objects, and a round's `chunks` are spans in that one shard.
- `scans` gives, per site in the store, the sweeps' start times (Unix
  seconds UTC, ascending) — the store's `scan_time` row without its
  padding, so a player builds a site's timeline from the manifest alone.
  It is per product, since the two products of a round are assembled
  independently (§3).
- A product with no store in a round (no site had a new sweep) is absent
  from that round's entry.
- `sources` is the point products' `sources[]` (`tc.md` §1): one entry per
  source with `ok`, `error` where not ok, and `reports` (sweeps read).

Reading one site's window: the pointer, the manifest, then one range per
round per product — 37 requests for three hours of one product at a
five-minute cadence, about 7 MB of `n0b` for a typical site.

## 5. Sources

`unidata-nexrad-level3` (AWS Open Data, us-east-1, maintained by Unidata),
keys `<SITE>_<PRODUCT>_<YYYY>_<MM>_<DD>_<HH>_<MM>_<SS>`: the source id
`unidata-n0b` / `unidata-n0g`. Only the WSR-88D sites are read: the
terminal Doppler radars share the bucket under airport codes and never
publish N0B or N0G. Sites and positions come from the NCEI station list
(`nexrad-stations.txt`, station type NEXRAD); the product header's own
position is used where the list has none.

Terms: NOAA data disseminated through the NOAA Open Data Dissemination
program, open to the public with attribution requested and no endorsement
implied (<https://registry.opendata.aws/noaa-nexrad/>). Attribution:
"NOAA / NWS NEXRAD Level III via Unidata".

## 6. Validation

The builder validates every store against the [polar store](zarr-profile.md#polar-store)
rules and every manifest against §4 as it writes them; a reader validates
the same shapes on read, refuses a `schemaVersion` or `xue_polar` version
above the one it implements, and treats an unknown site id or source id as
data, not an error.

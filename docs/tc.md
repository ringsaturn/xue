# The tropical cyclone product

The `.xue` container is a raster container. A storm track — a few dozen
points from each centre, a few thousand from an ensemble — is not a
raster, so tropical cyclones are a second, smaller product published
beside the runs: plain JSON, a few hundred kilobytes an hour, aggregated
by `xue tc-build` (`xuebuild/tc/`) from the agencies' and the models'
own track files, and taken live by the same pointer → immutable directory
contract the runs use. This document is the normative description of
that product, **schema v1**. The design it implements — the sources, the
three notions of an impact area, the phases — is in the project's
private notes; what is here is the contract a reader can hold the
publisher to.

## 1. Delivery

Everything is a static file under the data root, the same root as the
run pointers (`latest.json`, `latest-<model>.json`) and `showcase.json`.

- **`latest-tc.json`** — the one mutable object. Fetch it with caching
  disabled. It names the newest issue directory and the index inside it:

  ```json
  {"schemaVersion": 1, "product": "tc", "issued": "2026-09-13T01:00:00Z",
   "path": "tc.2026091301/index.json", "byteLength": 15930, "crc32": "5ca38836"}
  ```

  `path` is relative to the pointer and always `tc.<issued hour>/index.json`;
  `byteLength` and `crc32` (zlib CRC32 of the file's bytes, eight lowercase
  hex digits) are the index's, so it can be requested as
  `<path>?v=<crc32>` through an immutable cache.
- **`tc.<YYYYMMDDHH>/`** — one immutable directory per **issue**: the UTC
  hour the product was aggregated at, not any centre's issue time. It holds
  `index.json` and one `<storm id>.json` per system. Once the pointer
  names it nothing in it changes; a manual rebuild of an hour
  (`tc-build --issue … --force`) writes the directory again, and every
  file is addressed by its CRC, so a cache never serves one hour's bytes
  under another's key. Directories are pruned after 30 days
  (`make prune-r2-tc`).
- Paths inside `index.json` are file names beside it; request each as
  `<name>?v=<crc32>` with the CRC the index carries.

The publisher builds every hour at twenty past (`publish-tc.yml`). A
source that is late or unreachable is *recorded* — see `sources` — and
the hour publishes without it. The pointer is withheld only when no
agency and no model contributed at all, in which case the previous hour
stays live. An hour the pointer already names is finished: a run that
resolves to it (a dispatch landed in the same hour, or the schedule ran
late) builds nothing, and the workflow's `force` input is the manual
rebuild.

Deployment is one-sided: the product is new files under a new pointer, so
a shell that does not know it never asks for it. A `schemaVersion` bump
follows the runs' rule — ship the shell first — while an added optional
field does not bump the version.

## 2. Units and conventions

SI throughout, converted at the parser so no reader meets a knot:

| quantity | unit | note |
|---|---|---|
| wind (`vmax`, `gust`) | m/s | on whatever averaging the source uses: one-minute for NHC and JTWC, ten-minute for JMA and the other RSMCs, the model's own for a model |
| pressure (`pmin`) | hPa | |
| radii (`radii`, `rmw`, `cone`, alert `halfWidth`) | km | |
| position | degrees, longitude in (−180, 180] | a track crossing the antimeridian is split by the reader, not the file |
| time | ISO 8601, UTC, `Z` suffix | |
| `lead` | seconds | from the forecast's `base` |

Wind radii are keyed by the threshold in knots the centres name them by —
`"34"`, `"50"`, `"64"` — as four quadrant values `[ne, se, sw, nw]` in km.
A quadrant not reported is `null`; a quadrant reported as zero is a real
"no such wind there". A threshold a source never reports is absent from
the object; a point with no radii at all has `radii: null`. ECMWF reports
at 18 / 26 / 33 m/s, the same three thresholds to within a knot, and is
keyed the same way.

`class` is the source's own intensity string, verbatim (`TS`, `HU`,
`TY`, JMA's grade digit, CMA's category…). A reader colours it by the
source's own scale (`registry.py` names the scale per agency) and never
converts between scales.

Numbers are JSON numbers; a value not reported is `null`. The files are
written compactly with ASCII escaping, and a file's identity is its
bytes: the CRC32 in the index is of the file exactly as served.

## 3. Identity

A system has one id for its whole life in the product:

- **A-level, numbered**: the ATCF id — basin, two-digit number, season
  (`EP142026`). JTWC, NHC, the NCEP tracker, ECMWF (`14E` ⇔ `EP14`) and
  IBTrACS (`USA_ATCF_ID`) all carry it; nothing is inferred.
- **B-level, an unnumbered disturbance a centre tracks** (an invest, a
  tropical depression under a formation alert): a synthetic id
  `x-<basin>-<first seen YYYYMMDDHH>-<n>` (basin lowercase, `n` from 1).
  The invest number (`EP97`) is an *alias* with a validity window, because
  the numbers are reused within a season; the same alias seen again within
  48 hours is the same system.
- **C-level, a potential system only a model has found**: the same
  synthetic shape, named after the run that first found it. ECMWF numbers
  such systems afresh every run (`70W`, `71W`…), so those names never
  carry across; a model's system is attached to a known system when it
  lies within 333 km (three degrees of arc) of that system's position
  nearest in time, and otherwise stands alone, matched the same way to
  the previous hour's C-level systems so its id holds while the model
  keeps finding it.

When an invest is numbered, its B-level system merges into the A-level
storm: the previous hour's B or C system that no source names any more,
whose last position lies within 333 km and 48 hours of a current A-level
storm, becomes an alias of it, and `index.json`'s `crosswalk` maps the old
id to the new for as long as the new one is published. A reader holding an
old id follows the crosswalk before giving up.

Only positions are compared, never intensities. Binary cyclones within
333 km at the same hour would be merged; the ATCF key taking precedence
wherever any source supplies it is what keeps that rare.

## 4. `index.json`

```json
{
  "schemaVersion": 1,
  "issued": "2026-09-13T01:00:00Z",
  "generated": "2026-09-13T01:54:00Z",
  "storms": [
    {
      "id": "EP142026", "level": "A", "basin": "EP", "name": "NORBERT",
      "path": "EP142026.json", "byteLength": 75212, "crc32": "a0cd91de",
      "aliases": {"ecmwf:70E": {"from": "2026-09-12T18:00:00Z", "to": "2026-09-12T18:00:00Z"}},
      "position": {"time": "2026-09-13T00:00:00Z", "lat": 17.8, "lon": -130.0},
      "vmax": 23.1, "pmin": 1000.0, "class": "TS", "classAgency": "nhc",
      "agencies": ["jtwc", "nhc"], "models": ["gfs", "gefs", "ecmwf", "ecmwfens"], "best": ["usa"]
    }
  ],
  "crosswalk": {"x-ep-2026091012-1": "EP152026"},
  "sources": [ … ]
}
```

- `issued` is the aggregation hour, `generated` when the build ran.
- `storms[]` is sorted by level, basin, id. `level` is `A`, `B` or `C`
  (§3); `basin` an ATCF basin (`AL`, `EP`, `CP`, `WP`, `IO`, `SH`);
  `name` the name the newest source gives, or `null`.
- `position`, `vmax`, `pmin`, `class`, `classAgency` are the headline:
  the last point of a best track when there is one, else the first point
  of an agency forecast, else of a model's; `classAgency` says whose.
  `position` may be `null` for a system with nothing observed.
- `agencies`, `models`, `best` list the keys present in the storm file, so
  a reader can decide what to fetch before fetching.
- `sources[]` is the same list every storm file carries (§6).

## 5. `<storm id>.json`

```
{
  schemaVersion: 1,
  id, level, basin, name,
  sid:   IBTrACS storm id ("2026251N13251") | null,
  intl:  the international (RSMC Tokyo) number | null,    — reserved, null in v1
  aliases: { alias: {from, to} },
  best:     { <agency key>: Track },
  agencies: { <agency id>: Forecast },
  models:   { <model id>: Forecast | Ensemble },
  impact:   { },                                           — reserved for the impact areas
  alert:    { time, line: [[lat, lon], [lat, lon]] | null, halfWidth: km | null, center: [lat, lon] | null } | null,
  sources:  [ … ]
}

Point    = { time, lead?, lat, lon, vmax, pmin, radii, class, rmw, gust, cone }
Forecast = { issued, base, run?, number?, points: Point[] }
Track    = { source, provisional, points: Point[] }
Ensemble = { issued, base, run?, leads: int[], members: int[], lat: int[], lon: int[], vmax: int[], pmin: int[], mean: Forecast | null }
```

**Forecast.** `base` is the time the leads count from — a model's cycle,
a centre's synoptic time (NHC's 03Z advisory forecasts from the 00Z
position, its first point at lead 3 h). `issued` is when it was released,
equal to `base` where a source does not say. A point's valid time is
`base + lead`, also written as `time`; leads are strictly increasing. A
reader aligns tracks with the raster's playhead **by valid time**, never
by `base` or `issued`. `run` (`YYYYMMDDHH`) is present on a model's
forecast and is what the raster manifest calls `run`; `number` is a
centre's advisory or warning number, verbatim.

**Track.** A best track: points by `time`, strictly increasing, no leads.
`source` says where it came from — `ibtracs`, or the centre whose working
file it is (`nhc` for its b-deck, `jtwc` for a warning's history block);
`provisional` is true for every working track of a live storm. Keys of
`best` are IBTrACS' agency keys: `usa` (the NHC / JTWC joint best track),
`jma`, `cma`, `hko`, `kma`, `imd`, `mfr`, `bom`, `fms`, `mnz`. When more
than one source supplies the same key, the fresher working file wins
(`nhc`, then `jtwc`, then `ibtracs`).

**Ensemble.** A member set on one lead axis, as fixed-point integer arrays
flattened member-major: `index = member position × len(leads) + lead
position`. `lat` and `lon` are degrees × 100, `vmax` m/s × 10, `pmin`
hPa × 10; **−32768** is missing (a member that has not found, or has
lost, the system). `members` lists the member ids in array order (GEFS:
0 for the control, 1–30; ECMWF ENS: 1–51 as the BUFR numbers them).
`mean` is the ensemble mean track when the source publishes one (GEFS'
`AEMN`), else `null`.

**Keys.** Agency and model ids are `^[a-z][a-z0-9]*$` and are admitted
structurally: a key the reader's registry does not know is drawn in a
neutral colour, not rejected. The ids the publisher writes today are in
`tests/fixtures/tc-registry.json`, the golden the Python registry and the
frontend's are both held to.

**Alert.** JTWC's tropical cyclone formation alert, when one is in force
for a B-level system: the formation line's two ends, its half-width and
the estimated centre.

## 6. `sources[]`

One entry per source the build was asked for, in build order:

```json
{"id": "ecmwfens", "ok": true, "fetched": "2026-09-13T01:53:53Z",
 "url": "https://…/20260912180000-144h-enfo-tf.bufr", "cycle": "2026091218", "systems": 42}
{"id": "nhc", "ok": false, "fetched": "2026-09-13T01:53:45Z", "error": "request failed for …: HTTP Error 503"}
```

`ok: false` always carries `error`. `cycle` is the model run a model
source was read from; the ensemble's cycle lagging the deterministic
one (ECMWF's `enfo` lands later than `oper`) is the normal state, not a
fault. The list is repeated in every storm file so a storm can be read on
its own.

Source ids in v1: `jtwc` (JMV 3.0 warnings and formation alerts, via the
RSS feed), `nhc` (`CurrentStorms.json`, the a-deck's `OFCL` lines, the
b-deck), `gfs` (the NCEP tracker's `avno` file), `gefs` (its `ac00`,
`ap01`–`ap30` members and the `aemn` mean), `ecmwf` (open data `oper`
`tf` BUFR), `ecmwfens` (`enfo` `tf` BUFR), `ibtracs` (the v04r01 active
list).

## 7. Validation

`xuebuild/tc/schema.py` validates every file as it is written, and
`tests/test_tc.py` holds a build from the fetched fixture
(`tests/fixtures/tc/tc.2026091206/`) to the committed golden
(`tests/fixtures/tc/expected/`), regenerated by
`tests/prepare_tc_golden.py`. A reader validates the same shapes on
read and rejects a `schemaVersion` above the one it implements.

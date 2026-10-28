# The tropical cyclone product

The `.xue` container is a raster container. A storm track (a few dozen
points from each centre, a few thousand from an ensemble) is not a raster,
so tropical cyclones are a second, smaller product published beside the
runs: plain JSON, a few hundred kilobytes an hour, aggregated by `xue
tc-build` (`xuebuild/tc/`) from the agencies' and the models' own track
files, and taken live by the same pointer → immutable directory contract
the runs use. This document is the normative description of that product,
schema v1. The design it implements (the sources, the three notions of an
impact area, the phases) is in the project's private notes; what is here is
the contract a reader can hold the publisher to.

## 1. Delivery

Everything is a static file under the data root, the same root as the run
pointers (`latest.json`, `latest-<model>.json`) and `showcase.json`.

- `latest-tc.json` is the one mutable object. Fetch it with caching
  disabled. It names the newest issue directory and the index inside it:

  ```json
  {"schemaVersion": 1, "product": "tc", "issued": "2026-09-13T01:00:00Z",
   "path": "tc/2026/09/13/tc.2026091301/index.json", "byteLength": 15930, "crc32": "5ca38836"}
  ```

  `path` is relative to the pointer and always
  `tc/<YYYY>/<MM>/<DD>/tc.<issued hour>/index.json`, the date being the
  issued hour's. A pointer written before that layout names the flat
  `tc.<issued hour>/index.json`, and a reader accepts both. `byteLength` and `crc32` (zlib CRC32 of the file's bytes, eight lowercase
  hex digits) are the index's, so it can be requested as `<path>?v=<crc32>`
  through an immutable cache.
- `tc/<YYYY>/<MM>/<DD>/tc.<YYYYMMDDHH>/` is one immutable directory per
  issue, filed under its UTC day so the archive can be kept for years
  without any listing level growing past a few dozen prefixes. The hour is the UTC hour the
  product was aggregated at, not any centre's issue time. It holds
  `index.json` and one `<storm id>.json` per system, and beside them
  `item.json`, the issue's STAC Item — a pure function of the index, read
  by a catalog client and by nothing in the product (`docs/stac.md`
  §"Point products"). Once the pointer names
  it nothing in it changes; a manual rebuild of an hour (`tc-build --issue
  … --force`) writes the directory again, and every file is addressed by
  its CRC, so a cache never serves one hour's bytes under another's key.
  Directories are pruned after a year (`make prune-r2-tc`).
- Paths inside `index.json` are file names beside it; request each as
  `<name>?v=<crc32>` with the CRC the index carries.

The publisher builds every hour at twenty past (`publish-tc.yml`). A source
that is late or unreachable is recorded (see `sources`) and the hour
publishes without it. The pointer is withheld only when no agency and no
model contributed at all, in which case the previous hour stays live. An
hour the pointer already names is finished: a run that resolves to it (a
dispatch landed in the same hour, or the schedule ran late) builds nothing,
and the workflow's `force` input is the manual rebuild.

Deployment is one-sided: the product is new files under a new pointer, so a
shell that does not know it never asks for it. A `schemaVersion` bump
follows the runs' rule (ship the shell first); an added optional field does
not bump the version.

## 2. Units and conventions

SI throughout, converted at the parser:

| quantity | unit | note |
|---|---|---|
| wind (`vmax`, `gust`) | m/s | on whatever averaging the source uses: one-minute for NHC and JTWC, ten-minute for JMA and the other RSMCs, the model's own for a model |
| pressure (`pmin`) | hPa | |
| radii (`radii`, `rmw`, `cone`, alert `halfWidth`) | km | |
| position | degrees, longitude in (−180, 180] | a track crossing the antimeridian is split by the reader, not the file |
| time | ISO 8601, UTC, `Z` suffix | |
| `lead` | seconds | from the forecast's `base` |

Wind radii are keyed by the threshold in knots the centres name them by
(`"34"`, `"50"`, `"64"`) as four quadrant values `[ne, se, sw, nw]` in km. A
quadrant not reported is `null`; a quadrant reported as zero means no such
wind there. A threshold a source never reports is absent from the object; a
point with no radii at all has `radii: null`. ECMWF reports at 18 / 26 /
33 m/s, the same three thresholds to within a knot, and is keyed the same
way.

`class` is the source's own intensity string, verbatim (`TS`, `HU`, `TY`,
JMA's grade digit, CMA's category…). A reader colours it by the source's
own scale (`registry.py` names the scale per agency) and never converts
between scales.

Numbers are JSON numbers; a value not reported is `null`. The files are
written compactly with ASCII escaping, and a file's identity is its bytes:
the CRC32 in the index is of the file exactly as served.

## 3. Identity

A system has one id for its whole life in the product:

- A-level, numbered: the ATCF id, basin, two-digit number, season
  (`EP142026`). JTWC, NHC, the NCEP tracker, ECMWF (`14E` ⇔ `EP14`) and
  IBTrACS (`USA_ATCF_ID`) all carry it; nothing is inferred. The season
  is the calendar year, except in the southern hemisphere, whose season
  runs July to June and is named after the year it ends in (`SH022027`
  in October 2026). The NCEP tracker's sub-basin codes are read as the
  basin JTWC numbers in: `AA` and `BB` as `IO`, `SI` and `SP` as `SH`.
  ECMWF's storm numbers are JTWC's and NHC's except in the western
  Pacific, where they are its own (its `36W` was JTWC's `WP29`): a
  western Pacific ECMWF system is therefore placed by position, as a
  C-level one is, and never names a storm.
- B-level, an unnumbered disturbance a centre tracks (an invest, a tropical
  depression under a formation alert): a synthetic id `x-<basin>-<first
  seen YYYYMMDDHH>-<n>` (basin lowercase, `n` from 1). The invest number
  (`EP97`) is an alias with a validity window, because the numbers are
  reused within a season; the same alias seen again within 48 hours is the
  same system.
- C-level, a potential system only a model has found: the same synthetic
  shape, named after the run that first found it. ECMWF numbers such
  systems afresh every run (`70W`, `71W`…), so those names never carry
  across; a model's system is attached to a known system when it lies
  within 333 km (three degrees of arc) of that system's position nearest in
  time, and otherwise stands alone, matched the same way to the previous
  hour's C-level systems so its id holds while the model keeps finding it.

When an invest is numbered, its B-level system merges into the A-level
storm: the previous hour's B or C system that no source names any more,
whose last position lies within 333 km and 48 hours of a current A-level
storm, becomes an alias of it, and `index.json`'s `crosswalk` maps the old
id to the new for as long as the new one is published. A reader holding an
old id follows the crosswalk before giving up.

Only positions are compared, never intensities. Binary cyclones within
333 km at the same hour would be merged; the ATCF key takes precedence
wherever any source supplies it, which keeps that rare.

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
  (§3); `basin` an ATCF basin (`AL`, `EP`, `CP`, `WP`, `IO`, `SH`); `name`
  the name the newest source gives, or `null`.
- `position`, `vmax`, `pmin`, `class`, `classAgency` are the headline: the
  last point of a best track when there is one, else the first point of an
  agency forecast, else of a model's; `classAgency` says whose. `position`
  may be `null` for a system with nothing observed.
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

Forecast. `base` is the time the leads count from: a model's cycle, or a
centre's synoptic time (NHC's 03Z advisory forecasts from the 00Z position,
its first point at lead 3 h). `issued` is when it was released, equal to
`base` where a source does not say. A point's valid time is `base + lead`,
also written as `time`; leads are strictly increasing. A reader aligns
tracks with the raster's playhead by valid time, never by `base` or
`issued`. `run` (`YYYYMMDDHH`) is present on a model's forecast and is what
the raster manifest calls `run`; `number` is a centre's advisory or warning
number, verbatim.

Track. A best track: points by `time`, strictly increasing, no leads.
`source` says where it came from: `ibtracs`, or the centre whose working
file it is (`nhc` for its b-deck, `jtwc` for a warning's history block);
`provisional` is true for every working track of a live storm. Keys of
`best` are IBTrACS' agency keys: `usa` (the NHC / JTWC joint best track),
`jma`, `cma`, `hko`, `kma`, `imd`, `mfr`, `bom`, `fms`, `mnz`. When more
than one source supplies the same key, the fresher working file is taken
(`nhc`, then `jtwc`, then `ibtracs`).

Ensemble. A member set on one lead axis, as fixed-point integer arrays
flattened member-major: `index = member position × len(leads) + lead
position`. `lat` and `lon` are degrees × 100, `vmax` m/s × 10, `pmin` hPa ×
10; −32768 is missing (a member that has not found, or has lost, the
system). `members` lists the member ids in array order (GEFS: 0 for the
control, 1–30; ECMWF ENS: 1–51 as the BUFR numbers them). `mean` is the
ensemble mean track when the source publishes one (GEFS' `AEMN`), else
`null`.

Keys. Agency and model ids are `^[a-z][a-z0-9]*$` and are admitted
structurally: a key the reader's registry does not know is drawn in a
neutral colour, not rejected. The ids the publisher writes today are in
`tests/fixtures/tc-registry.json`, the golden the Python registry and the
frontend's are both held to.

Alert. JTWC's tropical cyclone formation alert, when one is in force for a
B-level system: the formation line's two ends, its half-width and the
estimated centre.

## 6. `sources[]`

One entry per source the build was asked for, in build order:

```json
{"id": "ecmwfens", "ok": true, "fetched": "2026-09-13T01:53:53Z",
 "url": "https://…/20260912180000-144h-enfo-tf.bufr", "cycle": "2026091218", "systems": 42}
{"id": "nhc", "ok": false, "fetched": "2026-09-13T01:53:45Z", "error": "request failed for …: HTTP Error 503"}
```

`ok: false` always carries `error`. `cycle` is the model run a model source
was read from; the ensemble's cycle lagging the deterministic one (ECMWF's
`enfo` lands later than `oper`) is the normal state. The list is repeated
in every storm file so a storm can be read on its own.

Source ids in v1: `jtwc` (JMV 3.0 warnings and formation alerts, via the
RSS feed), `nhc` (`CurrentStorms.json`, the a-deck's `OFCL` lines, the
b-deck), `gfs` (the NCEP tracker's `avno` file), `gefs` (its `ac00`,
`ap01`–`ap30` members and the `aemn` mean), `ecmwf` (open data `oper` `tf`
BUFR), `ecmwfens` (`enfo` `tf` BUFR), `ibtracs` (the v04r01 active list).

## 7. Validation

`xuebuild/tc/schema.py` validates every file as it is written, and
`tests/test_tc.py` holds a build from the fetched fixture
(`tests/fixtures/tc/tc.2026091206/`) to the committed golden
(`tests/fixtures/tc/expected/`), regenerated by
`tests/prepare_tc_golden.py`. A reader validates the same shapes on read
and rejects a `schemaVersion` above the one it implements.

## 8. The archive

The hourly directories are the live view. The archive is the record: every
distinct forecast of a numbered system, once, filed by month, and every
season's best tracks — what a track-error score is computed from. It is a
separate tree under the data root, written once a day
(`archive-tc.yml`, `xue tc-archive`) and never pruned.

### 8.1 Layout

```
tc/archive/catalog.json                   the one mutable name: every month and season
tc/archive/<YYYY>/<MM>/forecasts.jsonl    the month's forecasts, one per line
tc/archive/<YYYY>/<MM>/index.json         each storm's byte span in that file
tc/archive/<YYYY>/best.jsonl              the season's best tracks, one storm per line
tc/archive/<YYYY>/index.json              each storm's byte span in that file, and its months
```

- `catalog.json` is fetched with caching disabled. Each entry names an
  index by `path` (relative to the catalog), `byteLength` and `crc32`;
  each index names its `.jsonl` the same way. Every file but the catalog
  is requested as `<path>?v=<crc32>`.
- A month is the UTC month of a forecast's `base`. A season is the ATCF
  season of the storm id (its last four digits): the calendar year, except
  that the southern hemisphere's season runs July to June and is named
  after the year it ends in (`SH012026` forms after 1 July 2025).
- A month or season file is rewritten in place when a record reaches it
  (the month in progress daily, an earlier one by a late arrival or a
  backfill), and its CRC32 changes with it. Nothing in the files records
  when they were built: the same records give the same bytes.
- Lines are sorted by storm, then source, then `base`, so one storm's
  records in a month are one byte span — one Range request.

### 8.2 Records

A line of `forecasts.jsonl`:

```json
{"storm": "WP302026", "basin": "WP", "name": "FUNG-WONG", "source": "jtwc", "kind": "agency",
 "origin": "live", "forecast": { … }}
```

- `storm` is the ATCF id. Only numbered systems are archived; a system
  the product first carried under a synthetic id (§3) is filed under the
  ATCF id the crosswalk later gave it, forecasts before numbering
  included. A system never numbered is not archived.
- `source` is an agency or model id (§5); `kind` is `agency`, `model` or
  `ensemble`; `forecast` is a §5 `Forecast`, or an `Ensemble` when `kind`
  is `ensemble`. `name` is the name the source gave, or `null`.
- `origin` says where the record came from: `live` (the hourly product),
  `nhc-atcf` (NHC's a-deck archive), `ucar-atcf` (the UCAR RAL mirror of
  the ATCF a-decks), `ecmwf-aws` (ECMWF open data's `tf` BUFR as kept on
  AWS).
- A record is identified by `(storm, source, base)`. Of two records under
  one key the later replaces the earlier, except that a `live` record is
  never replaced by a backfilled one.
- Ensemble member ids are the origin's own: in a backfilled a-deck the
  control is 0 and a perturbed member its tech number (`AP07` → 7,
  `EE12` → 12); ECMWF's BUFR numbers 1–51.

A line of `best.jsonl` holds one storm:

```json
{"storm": "WP302026", "basin": "WP", "name": "FUNG-WONG", "sid": "2026290N10140",
 "atcf": ["WP302026"], "best": {"usa": Track, "jma": Track, …}}
```

`atcf` lists every ATCF id the storm carried (one that crossed into another
basin's numbering has two). For each agency key the archive keeps
IBTrACS' track once it is final, and until then whichever of IBTrACS' and
the centre's working file (§5 `Track.source`) reaches later.

### 8.3 Indexes

A month's `index.json`:

```json
{"archiveSchemaVersion": 1, "product": "tc", "month": "2026-10",
 "forecasts": {"path": "forecasts.jsonl", "byteLength": 812345, "crc32": "…"},
 "storms": [{"id": "WP302026", "basin": "WP", "name": "FUNG-WONG",
             "offset": 0, "length": 40312, "records": 56, "sources": ["ecmwf", "gfs", "jtwc"]}]}
```

A season's `index.json` carries `season`, `best` (the file) and `storms`,
each with `id`, `basin`, `name`, `sid`, `atcf`, its span in `best.jsonl`
(`offset` and `length`, both `null` for a storm with forecasts but no best
track yet), the agency keys of its tracks (`best`), the first and last
time any of them places it (`first`, `last`) and its peak wind (`vmax`,
m/s, on the `usa` track when there is one, else on the first key that has
a wind; all three `null` without a best track), the `months` it has
forecasts in and the `sources` of those forecasts. `catalog.json` lists `months` (`month`, `path`, `byteLength`,
`crc32`, `storms`, `records`) and `seasons` (`season`, `path`, `byteLength`,
`crc32`, `storms`). Storms and entries are sorted and unique; the spans tile
their file exactly. A reader rejects an `archiveSchemaVersion` above the
one it implements.

### 8.4 Producers

- The daily harvest reads the last 48 hourly issues from the data origin,
  follows their crosswalk, and for every system it renames reads back ten
  days for the forecasts it carried before it was numbered; hourly
  directories are therefore kept at least that long. It then merges
  IBTrACS' last three years into the seasons.
- The backfill (`xue tc-archive --backfill <sources> --from YYYY-MM`)
  reads `nhc` (`OFCL` → `nhc`, `AVNO` → `gfs`, `AC00`/`AP01`–`AP30`/`AEMN`
  → `gefs`, `EMX` → `ecmwf`, `EC00`/`EE01`–`EE50`/`EEMN` → `ecmwfens`;
  AL, EP, CP), `ucar` (`AVNO`, or `AVNX` where it is alone, → `gfs`, and
  the GEFS techs; WP, IO, SH), `ecmwf` (`oper`/`scda` and `enfo` `tf`
  BUFR from 2023-01-18) and `ibtracs` (`since1980`). An ECMWF system
  whose number is not JTWC's or NHC's (every western Pacific one, and
  every `7x`) is filed under the season's best-track storm whose synoptic
  position at the forecast's start lies within 333 km of the start — the
  nearest when two do — or not at all; a storm's ECMWF forecasts from
  before it was numbered are kept this way. `--rekey --from --to` applies
  the same placement to the ECMWF western Pacific records already
  archived, dropping those that start on no storm. JTWC's and the Asian
  centres' forecasts have no public archive: they exist from the day the
  live harvest kept them.

## 9. Track-error scores

Each season of the archive carries the errors of its forecasts against its
best tracks, written by the same daily run (`xue tc-archive`, which scores
every season whose inputs changed; `--rescore` forces one):

```
tc/archive/<YYYY>/scores.json    the season's summary, and each storm's span in errors.jsonl
tc/archive/<YYYY>/errors.jsonl   one storm per line: every verified (forecast, lead)
```

`catalog.json` names them under `scores` (`season`, `path`, `byteLength`,
`crc32`, `from`). `from` is what they were computed from — the season
index's CRC32 and that of every month its storms appear in — so a reader
can tell scores that lag the archive, and a run knows which to redo.

### 9.1 Verification

- Truth is a best track of the season record (§8.2), each key scored on its
  own: `usa` in every basin, `jma` and `cma` in the western Pacific. They
  are never mixed, and intensity errors against `jma` and `cma` compare a
  source's winds with ten-minute ones.
- Only the best track's synoptic points (00, 06, 12, 18 UTC) are truth.
  A forecast is verified at a lead when the best track has a point both at
  its `base` and at `base + lead`, and the system is a tropical or
  subtropical cyclone at both: `usa` status `TD`, `TS`, `TY`, `ST`, `STY`,
  `TC`, `HU`, `HR`, `MH`, `SD` or `SS`; JMA grade 2–5 or 9; CMA category
  1–6; and where a point carries no class, a wind of at least 25 kt.
- Leads are 0, 12, 24, 36, 48, 72, 96, 120, 144, 168, 192, 216 and 240 h;
  a forecast point verifies only at exactly these.
  A forecast with no point past its start — a system the model lost,
  which ECMWF still writes, often at a stale position — is not verified.
- An ensemble is verified by its mean position: the centroid of the members
  that carry the system at that lead, only where they are at least half of
  the members. Members not following the system the others follow (within
  333 km at their first position) are left out.
- Errors: `track`, the great-circle distance in km; `along` and `cross`,
  its components along the best track's motion through the verifying time
  (positive ahead) and across it (positive to the right); `vmax` and
  `pmin`, forecast minus truth, `null` where either is missing.

### 9.2 `scores.json`

```json
{"scoreSchemaVersion": 1, "product": "tc", "season": 2026,
 "from": {"season": "…", "months": {"2026-10": "…"}},
 "leads": [0, 12, …, 240],
 "errors": {"path": "errors.jsonl", "byteLength": 123456, "crc32": "…"},
 "storms": [{"id": "WP302026", "offset": 0, "length": 9876}],
 "summary": [{"basin": "WP", "truth": "usa", "source": "jtwc", "lead": 48, "n": 31,
              "track": 92.4, "trackMedian": 81.0, "along": -12.3, "cross": 8.1,
              "nVmax": 31, "vmaxBias": 1.2, "vmaxMae": 5.6, "nPmin": 30, "pminBias": -2.1, "pminMae": 6.0}],
 "homogeneous": [{"basin": "WP", "truth": "usa", "reference": "jtwc", "source": "ecmwf", "lead": 48,
                  "n": 28, "track": 95.0, "referenceTrack": 90.1, "skill": -0.054}]}
```

`summary` is every source on its own sample; `homogeneous` compares each
source with a basin's references — the official forecast (`nhc` in AL,
EP, CP; `jtwc` in WP, IO, SH) and `ecmwf` — on exactly the (storm, base,
lead) cases both verified, with `skill` = 1 − track ÷ reference track.
A line of `errors.jsonl` is `{storm, basin, truths: {<truth>: {<source>:
{base: [], lead: [], track: [], along: [], cross: [], vmax: [], pmin: []}}}}`,
one column per field and one row per verified case, `lead` in hours.

The official forecasts are compared with the models of the same `base`,
which ran with the same data but finished after the official forecast
was written; the centres' own verification compares them with the
previous cycle's models instead, so these skills favour the models.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).

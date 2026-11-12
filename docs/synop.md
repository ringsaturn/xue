# The synop product

A weather station's record is a short table of numbers every ten minutes
to every hour. A national network is a few hundred to a few thousand such
stations, a few megabytes a day — not a raster — so surface stations are a
point product published beside the runs, the airports (`docs/airport.md`)
and the soundings (`docs/sounding.md`). `synop` is the name for surface
weather observations in general, not only the WMO FM-12 SYNOP code: each
national network enters through an adapter that reads it into one set of
elements and SI units, and the product carries every network in the same
shape. The networks are aggregated every ten minutes by `xue synop-build`
(`xuebuild/synop/`) and taken live by the same pointer → immutable
directory contract the other point products use. This document is the
normative description of that product, index schema v2.

## 1. Delivery

Everything is a static file under the data root, beside the run pointers
and the other point products' pointers.

- `latest-synop.json` is the one mutable object. Request it with
  `Cache-Control: no-cache` (in a browser, `fetch(url, {cache: "no-cache"})`):
  without it a browser cache may answer with a pointer up to four hours
  old. It names the newest round's directory and the index inside it:

  ```json
  {"schemaVersion": 1, "product": "synop", "issued": "2026-10-05T00:10:00Z",
   "path": "synop/2026/10/05/synop.202610050010/index.json", "byteLength": 4312, "crc32": "1c0ffee5"}
  ```

  `path` is relative to the pointer and always
  `synop/<YYYY>/<MM>/<DD>/synop.<round>/index.json`, the date being the
  round's. A pointer written before that layout names the flat
  `synop.<round>/index.json`, and a reader accepts both. `byteLength` and `crc32` (zlib CRC32 of the
  file's bytes, eight lowercase hex digits) are the index's, so it can be
  requested as `<path>?v=<crc32>` through an immutable cache. The
  pointer's `schemaVersion` is its own and stays 1; the index's is §4's.
- `synop/<YYYY>/<MM>/<DD>/synop.<YYYYMMDDHHMM>/` is one immutable
  directory per round, filed under its UTC day so the data root does not
  carry a prefix per round: the UTC minute the product was aggregated at, always a multiple of ten. It holds
  `index.json` (§4), per network a `<network>.stations.json` (§5) and a
  `<network>.jsonl` (§6), and `item.json`, the round's STAC Item
  (`docs/stac.md` §"Point products"). Nothing in it changes once the
  pointer names it. Each round carries the whole window and lives three
  hours (`make prune-r2-synop`, `SYNOP_KEEP=18`).
- Paths inside `index.json` are file names beside it; request each as
  `<name>?v=<crc32>` with the CRC the index carries.

Three ways to read a network:

- **A map.** Fetch the index, then the stations file of each network to
  be shown: every station's position and current values, a few hundred
  kilobytes for a thousand stations.
- **One station.** Take the station's row in its network's stations file,
  which ends in the byte `offset` and `length` of its line in the
  network's `.jsonl`, and issue
  `GET <round directory>/<network>.jsonl?v=<crc32>` with
  `Range: bytes=<offset>-<offset + length - 1>`. The bytes are that
  station's JSON object exactly, without the newline, so they parse on
  their own.
- **A whole network.** Fetch `<network>.jsonl` and read it line by line:
  one station per line, sorted by id.

The publisher builds every ten minutes (`publish-synop.yml`). Each network
is asked for what it holds after its newest time in the previous round's
index, so a network that was unreachable catches up, on its next answer,
as far back as its own retention reaches. A network that fails is recorded
(§7) and keeps its history; the round publishes with the rest. The pointer
is withheld only when *no* network's observations arrived, in which case
the previous round stays live. A round the pointer already names is
finished; the workflow's `force` input is the manual rebuild.

Upload order is every `<network>.jsonl`, then every
`<network>.stations.json`, then `index.json` and `item.json`, and the
pointer last, once R2 reports the size of every file (`make
upload-r2-synop`). `make live-synop-index` pulls the live index and both
files of every network, checking each against the CRC32 the index states.

Deployment is one-sided: the product is new files under a new pointer, so
a shell that does not know it never asks for it. A new network is a new
entry in the index and two new files, which a reader of this schema admits
(§8); an index `schemaVersion` bump follows the runs' rule (ship the
shell first).

## 2. Elements and units

Every network is converted to one table of elements, SI throughout, at
the adapter. The index lists them in this order (`elements`):

| key | quantity | unit | type |
|---|---|---|---|
| `t` | air temperature | °C, to a tenth | number, −100 … 70 |
| `td` | dew point | °C, to a tenth | number, −100 … 60 |
| `rh` | relative humidity | % | integer, 0 … 100 |
| `p` | station pressure | hPa, to a tenth | number, 300 … 1100 |
| `slp` | sea-level pressure | hPa, to a tenth | number, 800 … 1100 |
| `wd` | wind direction | degrees true | integer, 0 … 360; `null` when calm or variable |
| `ws` | mean wind speed | m/s, to a tenth | number, 0 … 150 |
| `gust` | gust | m/s, to a tenth | number, 0 … 150 |
| `pr` | precipitation over the network's `prPeriod` ending at the time | mm, to a tenth | number, 0 … 1000 |
| `pr1h` | precipitation over the hour ending at the time | mm, to a tenth | number, 0 … 1000 |
| `sun` | sunshine over the network's `prPeriod` ending at the time | minutes | number, 0 … 60 |
| `snow` | snow depth | cm | integer, 0 … 2000 |
| `vis` | visibility | metres | integer, ≥ 0; each network's ceiling in §9 |
| `tide` | water level at a tide gauge, above the network's chart datum | m, to a hundredth | number, −2 … 20 |

- A network publishes the elements it measures and leaves the others
  `null`; nothing is derived — no dew point from temperature and humidity,
  no humidity from dew point, no sea-level reduction. Observations only.
  `td` appears where the network reports a dew point.
- `wd: 0` never occurs; north is `360`. With `ws: 0`, `wd` is `null`.
- `pr` and `sun` are totals over the period the network reports at, which
  is the network's `prPeriod` in seconds (§4); `pr1h` is the network's own
  hourly total, published where the network states one, because adding up
  shorter periods across a gap would understate it.
- A value the network marks as doubtful, missing or suspended, or one
  outside the range above, is `null`. So is a snow depth the network
  codes as negative (BUFR's −0.01 m "a little" and −0.02 m "patchy" are
  not depths). Each network's section (§9) says which of its quality
  flags are kept.
- `tide` is the height of the sea surface at a tide gauge above the
  chart datum the network states (§9) — the zero of its tide tables and
  nautical charts, near the lowest astronomical tide — not above mean sea
  level and not a surge residual: the astronomical tide is in it. Datums
  differ between networks, so two networks' values are not comparable
  without the offsets §9 gives. The range admits the largest tidal
  ranges on Earth above such a datum.
- Times in the network files are **epoch seconds, UTC**; times in the
  index and the stations files are ISO 8601 UTC with a `Z` suffix.
- Positions are decimal degrees to four places; `elev` is metres above
  sea level of the ground at the station, from the network's station
  table, or `null`.

The index carries this table as `elementTable` (§4), so a reader need
not hard-code it.

The files are written compactly with ASCII escaping, and a file's identity
is its bytes: the CRC32 in the index, and the pointer's, is of the file
exactly as served. Given the same inputs a round is byte-identical; only
the index's `generated`, the sources' `fetched` and the `builder`'s
`revision` depend on when and from what the build ran.

## 3. Identity

A station is `<network>:<the network's own station identifier>`
(`amedas:50066` is Mt. Fuji). The network id is lowercase,
`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`; the station part is the network's own
identifier, unchanged, of the characters `[0-9A-Za-z_.-]`. The id is the
key everywhere: the stations files' rows are sorted by it, each network
file's lines are sorted by it, and every line repeats it as `id` so a
slice read by range identifies itself. A WMO station number, where the
network gives one, is carried as `wmo` but is not the key — most
national-network stations have none.

## 4. `index.json`

The shape — every field, its type and range — is
[`schemas/synop-index.schema.json`](schemas/synop-index.schema.json) (v1,
§8: [`schemas/synop-index-v1.schema.json`](schemas/synop-index-v1.schema.json));
the pointer's is [`schemas/pointer.schema.json`](schemas/pointer.schema.json).
This section says what the fields mean and the rules across them.

```json
{
  "schemaVersion": 2,
  "issued": "2026-10-05T00:10:00Z",
  "generated": "2026-10-05T00:15:30Z",
  "networks": [
    {"id": "amedas", "name": "JMA AMeDAS", "cadence": 600, "prPeriod": 600, "wsPeriod": 600,
     "attribution": "出典：気象庁ホームページ (Japan Meteorological Agency)",
     "license": "PDL-1.0", "licenseUrl": "https://www.jma.go.jp/jma/kishou/info/coment.html",
     "url": "https://www.jma.go.jp/bosai/amedas/",
     "latest": "2026-10-05T00:10:00Z",
     "file": {"path": "amedas.jsonl", "byteLength": 6779928, "crc32": "3b0b746f"},
     "stations": {"path": "amedas.stations.json", "byteLength": 182000, "crc32": "0a1b2c3d", "count": 1286}}
  ],
  "elements": ["t", "td", "rh", "p", "slp", "wd", "ws", "gust", "pr", "pr1h", "sun", "snow", "vis", "tide"],
  "elementTable": {"t": {"unit": "°C", "cell": "point", "period": null}, …,
                   "pr": {"unit": "mm", "cell": "sum", "period": "network"}, …},
  "sources": [ … ],
  "builder": {"name": "xuebuild", "revision": "56ea0e8"}
}
```

- `issued` is the round, `generated` when the build ran.
- `networks[]` is one entry per network that has a station in the window,
  in the publisher's order (`amedas`, `dwd`, `hko`): its id and display
  `name`; `cadence`, the seconds between a station's reports; `prPeriod`,
  the seconds `pr` and `sun` accumulate over (or `null`); `wsPeriod`
  (optional), the seconds `ws` and `wd` are means over; the
  `attribution` a display must show, the `license` the network's data is
  reused under, `licenseUrl` (optional), the terms page that licence
  string names (§9), and the network's `url`; `latest`, the newest observation
  time in the network's files; `file`, the network file's name (always
  `<id>.jsonl`), length and CRC32; and `stations`, the stations file's
  name (always `<id>.stations.json`), length, CRC32 and `count`, the
  number of rows in it.
- `elements` is the element order of the stations files' rows. It is
  authoritative: each stations file repeats it and must be equal.
- `elementTable` (optional) describes each of `elements`, one entry per
  element in the same order: `unit`, the unit of §2's table; `cell`, how
  the value stands for its time — `point` (an instantaneous reading),
  `mean`, `sum`, `max` or `min`; and `period`, the seconds the value
  covers, ending at its time — a number, `"network"` where it is the
  network entry's field (`wsPeriod` for `ws` and `wd`, `prPeriod` for `pr`
  and `sun`), or `null` for an instantaneous value. `gust` is the `max`
  of a window that varies by network and report (§9), so its `period` is
  `null`. A reader that finds no entry for an element falls back to §2.
- `sources[]` is §7.
- `builder` (optional) names the code that built the round:
  `{"name": "xuebuild", "revision": <commit hash or null>}`. The
  publisher sets the commit (`XUE_BUILD_REVISION`); an offline build
  leaves it `null`.

The index carries no stations: a map reads the stations files of the
networks it shows.

## 5. `<network>.stations.json`

The shape is [`schemas/synop-stations.schema.json`](schemas/synop-stations.schema.json).

```json
{"schemaVersion": 2, "network": "amedas", "issued": "2026-10-05T00:10:00Z",
 "elements": ["t", "td", "rh", "p", "slp", "wd", "ws", "gust", "pr", "pr1h", "sun", "snow", "vis", "tide"],
 "columns": ["id", "lat", "lon", "elev", "rank", "name", "obsTime", "t", "td", "rh", "p", "slp",
             "wd", "ws", "gust", "pr", "pr1h", "sun", "snow", "vis", "tide", "offset", "length"],
 "stations": [
   ["amedas:50066", 35.36, 138.7267, 3775.0, 0, "Mt. Fuji", "2026-10-05T00:10:00Z",
    5.8, null, 98, 646.7, null, null, null, null, null, null, null, null, null, null, 3563, 283]
 ]}
```

- `schemaVersion`, `issued` and `elements` are the index's; `network` is
  the network's id.
- `columns` names the rows' columns, and a reader finds a column by its
  name there. In v2 it is always the layout below — the seven leading
  columns, the `elements` in their order, then `offset` and `length` — so
  a reader that indexes by position reads it too. It is optional: rounds
  published before it (every synop round up to 2026-11-04) do not carry
  it, and a reader takes such a file's rows in the layout below.
- `stations[]` is one compact row per station of the network, sorted by
  id and unique, as many as the index's `stations.count`:

  | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 … | last two |
  |---|---|---|---|---|---|---|---|---|
  | `id` | `lat` | `lon` | `elev` | `rank` | `name` | `obsTime` | one value per `elements` | `offset`, `length` |

  Every id's prefix is the file's network. `rank` is how prominent the station
  is, which a map thins by: `0` a principal station (a staffed office, a
  summit, a remote island, a WMO station), `1` a multi-element automatic
  station, `2` a single-purpose gauge: one that measures precipitation
  alone, or the tide alone. `name` is the
  station's name in Latin script, or `null`. `obsTime` is the station's
  newest time. The element values are the station's **current** values:
  for each element, its newest non-null value no more than an hour before
  `obsTime` — some elements arrive less often than the station reports
  (AMeDAS sends Mt. Fuji's humidity and pressure on the hour only) —
  except that `pr` and `sun`, which are totals over a period ending at
  their time, are only ever taken at `obsTime`, and `pr1h`, a rolling
  hour, no more than 20 minutes before it (a network may stamp its rain
  gauges on another beat than its other sensors, §9.3). `offset` and
  `length` are the byte span of the station's line in `<network>.jsonl`,
  the JSON object alone; the spans are in row order, do not overlap, and
  lie inside that file's `byteLength`.

## 6. `<network>.jsonl`

Each line's shape is [`schemas/synop-line.schema.json`](schemas/synop-line.schema.json),
in v1 and v2 alike. One line per station of that network, sorted by id, each the compact JSON
object below followed by `\n`:

```
{"id": "amedas:50066", "name": "Mt. Fuji" | null,
 "names": {"ja": "富士山", "ja-Kana": "フジサン"} | null,
 "lat": 35.36, "lon": 138.7267, "elev": 3775.0 | null, "wmo": null,
 "wigos": "0-20000-0-10393",              — optional, where the network gives one
 "rank": 0,
 "time": [1791158400, 1791159000, …],     — epoch seconds UTC, strictly increasing
 "obs": {"t": [5.5, 5.8, …], "rh": [98, null, …], "p": [646.7, null, …]},
 "qc": {"rh": [1, 6, …]}}                 — optional, where the network has quality codes
```

- `names` maps BCP 47 language tags to the network's own spellings;
  a display picks the viewer's language and falls back to `name`.
- `time` is the station's window, oldest first: every time in the last
  24 hours before the round at which the station reported anything.
- `obs` holds one array per element the station reported at least once in
  the window, each as long as `time`, `null` where that element was not
  reported at that time. An element the station never reported in the
  window is absent, and reads as all `null`.
- `wigos` (optional) is the station's WIGOS identifier
  (`<series>-<issuer>-<issue number>-<local identifier>`), present where
  the network's reports state one (`dwd`, §9.2). It is not the key (§3).
- `qc` (optional) carries the network's own quality code of each value,
  for a network that has them (`amedas`, §9.1): per element an array as
  long as `time`, the code as the network gives it, `null` where there
  was no reading. `obs` keeps its rule — a value whose code the network
  says is not good is `null` there (§2) — and `qc` says why. A column is
  written only where it says more than `obs` does: an element without
  one had code 0 wherever its value is not `null` and no reading where it
  is, and a line with nothing to add has no `qc`. An element can have a
  `qc` column and no `obs` column (suspended all day).

A station's window is a merge, not a snapshot. Each round lays the
network's new observations over the previous round's file keyed by
(station, time) — a network that revises a value is believed — and drops
what is older than 24 hours before the round. For most networks a new
report replaces the old one at that time whole. A network whose report
for one time arrives in pieces — several messages, several files, several
rounds (`dwd`, `hko`) — is merged element by element instead: a value the
new piece carries replaces the old one, and an element it leaves out keeps
what an earlier piece said. A station that did not report keeps its
history; one with nothing left in the window leaves the files until it
reports again. Station metadata comes from the network's current table
each round (for `dwd`, the reports themselves). A network with no station
left is not listed.

## 7. `sources[]`

One entry per source, every network's in turn, in build order:

```json
{"id": "jma-amedas-map", "ok": true, "fetched": "2026-10-05T00:13:34Z",
 "url": "https://www.jma.go.jp/bosai/amedas/data/map", "snapshots": 1, "missing": 0, "reports": 1284}
{"id": "dwd-synop-bufr", "ok": true, "fetched": "2026-10-05T00:13:36Z",
 "url": "https://opendata.dwd.de/weather/weather_reports/synoptic/germany/", "files": 2, "missing": 0,
 "reports": 2310, "unplaced": 7}
```

`sha256` (optional) is the SHA-256, lowercase hex, of the raw bytes the
build read from the source, file after file in the order they were
fetched, so a round can be traced to its input; it is absent when the
source contributed no bytes. `ok: false` always carries `error`. `reports` is what the source
contributed — observations read (one per station and time), stations in a
table. `files` is how many files were read, `missing` how many were gone
when asked for, `unplaced` how many reports named no station the network
can place. A network's own fields are described in its section.

## 8. Validation and versions

The JSON Schemas of §4–§6 are normative for shape: a file that does not
match them is not a synop file. `tests/test_schemas.py` holds the
committed golden (`tests/fixtures/synop/expected/`, two consecutive rounds
built from the fetched fixture `tests/fixtures/synop/synop.202610050000/`
and `…0010/`, the raw rounds, which stay flat under the raw root, by
`tests/prepare_synop_golden.py`) and the v1 rounds of
`tests/fixtures/synop/v1/` to them, and the shell's validators are
generated from them (`web/src/schema/generated/`).
`xuebuild/synop/schema.py` validates every file as it is written, and
`tests/test_synop.py` holds the build to the golden and checks every
stations-file row's span against the network file it addresses. What a
schema cannot say is each reader's own check: file names, unique networks
and elements, the element table in the elements' order, a stations
file's `network`, `issued`, `elements` and `columns` against its index,
its row width and `count`, rows sorted under the network's prefix, each
element of §2 in its range in a row (a line's `obs` carries the ranges in
its schema), the spans in row order inside the network file, and a line's
strictly increasing `time` with every `obs` and `qc` array as long.
Admission is structural: a network, a station or an element key a reader
has never seen is not an error — the row width follows the `elements`
list. A reader rejects an index `schemaVersion` above the one it
implements.

Schema v1, the first, kept every station in the index: an index
`stations[]` of rows `[id, network, lat, lon, elev, rank, name, obsTime,
…values, offset, length]`, `network` being the position in `networks[]`,
and no `stations` block in a network entry; its `elements` had no `td`.
The network files were the same. Readers keep reading v1 — the live round
at the switch is one, and the first v2 round merges onto its network
files.

## 9. Networks

### 9.1 `amedas` — JMA AMeDAS

The Japan Meteorological Agency's Automated Meteorological Data
Acquisition System: about 1 300 stations over Japan and its islands, every
ten minutes. `cadence`, `prPeriod` and `wsPeriod` (`wind` is the
ten-minute mean) are 600.

Source. The JSON the agency's own AMeDAS map page reads; there is no
published API and no service commitment.

- `jma-amedas-table` — `https://www.jma.go.jp/bosai/amedas/const/amedastable.json`,
  the station table (position in degrees and minutes, altitude, type,
  names in kanji, kana and English), fetched at most once a day with
  `If-Modified-Since`. A failure over a copy already on disk stays `ok`
  with the error noted.
- `jma-amedas-map` — `https://www.jma.go.jp/bosai/amedas/data/latest_time.txt`
  for the newest time, then `data/map/<YYYYMMDDHHmm>00.json` (a JST
  minute) for every ten-minute time after the network's previous `latest`,
  at most the 144 times of the window and none after the round. The agency
  keeps about seven days of these. `snapshots` is how many were read,
  `missing` how many were already gone (HTTP 404).

Reading. Each value arrives as `[value, quality]`. A value is kept when
its quality is 0 (normal) or 1 (quasi-normal) and it is not null — the
agency's own page's rule; 2–4 (insufficient data), 5 (suspended) and 6
(missing) become `null`. Each line's `qc` (§6) carries these codes as
they arrive, per element, for the key below that element is read from:
0 normal, 1 quasi-normal, 2–4 insufficient data, 5 suspended, 6 missing;
a code outside 0–255 or not an integer is `null`. The elements:

| AMeDAS key | element | conversion |
|---|---|---|
| `temp` | `t` | — |
| `humidity` | `rh` | — |
| `pressure` | `p` | — |
| `normalPressure` | `slp` | — |
| `windDirection` | `wd` | 16-point code × 22.5, rounded half up; code 0 (静穏, calm) is `null` |
| `wind` | `ws` | — |
| `precipitation10m` | `pr` | — |
| `precipitation1h` | `pr1h` | — |
| `sun10m` | `sun` | minutes |
| `snow` | `snow` | — |
| `visibility` | `vis` | metres |

`td` and `gust` are not in the snapshots and stay `null`. `vis` reaches
20 000 m and no further in the snapshots examined: read 20 000 as
"20 km or more". The hourly
snapshot (on the hour) carries more than the others: several stations,
Mt. Fuji among them, report humidity and pressure only then, and snow
depth and the present-weather code appear only then (the weather code is
not published).

Station identity: `amedas:<the five-digit AMeDAS number>`; `name` is the
table's English name, `names` its kanji (`ja`) and kana (`ja-Kana`).
`rank` is 0 for the table's types A, B, D, E, F and G (staffed offices,
special stations, and single sites such as Mt. Fuji and Minamitorishima),
2 for a type-C station whose element mask has neither temperature nor
wind (a precipitation gauge), and 1 otherwise. A snapshot entry for a
station the table does not list is dropped.

Terms. The agency's website content is reusable, commercially included,
under the Public Data License v1.0 (the `licenseUrl` below) with the source credited —
「出典：気象庁ホームページ」 — and, for edited content, a statement that it
was edited; it must not be presented as the agency's own publication
(<https://www.jma.go.jp/jma/kishou/info/coment.html>). The `attribution`
string carries the credit, and a display shows it with the data.

### 9.2 `dwd` — Deutscher Wetterdienst national SYNOP

Deutscher Wetterdienst's national ten-minute SYNOP reports in BUFR: about
1 000 German stations — some 220 WMO stations, about 300 national stations
with the full set of elements and about 470 that measure precipitation
alone. `cadence`, `prPeriod` and `wsPeriod` are 600 (`ws` and `wd` are
the −10 min means). The network is `partial` (§6).

Source. `dwd-synop-bufr` —
<https://opendata.dwd.de/weather/weather_reports/synoptic/germany/>, a
directory of BUFR batches named
`Z__C_EDZW_<yyyymmddHHMMSS>_bda01,synop_bufr_GER_999999_999999__MW_<seq>.bin`,
about eight an hour, kept about 48 hours (a `…_latest_…MW_XXX.bin` copy of
the newest batch is not read). The sequence number cannot be predicted, so
each round reads the directory page and fetches every batch stamped after
the network's previous `latest` and not after the round — at most the
24-hour window. A batch holds nothing newer than its stamp, so everything
new is in these, and values for older times that arrive late arrive in
later batches. `files` is how many batches were read, `missing` how many
were gone between the listing and the request. The batches are decoded
with eccodes' `bufr_dump -j f`.

Arrival. A batch mixes observation times and templates: ten-minute
values come in half-hourly batches 20–35 minutes late; the batch on the
hour (~700 KB) adds an hourly SYNOP template for the WMO stations and an
hourly national template; the half-hour batch adds a thirty-minute
summary. One station at one time is therefore spread over several
subsets, in different batches and different rounds — the reason the
network is merged element by element. Within a round, the subsets of one
station and time are merged the same way; where two carry the same
element, the gust over the shorter period wins, then the ten-minute MOR
over the observer's visibility, then the later subset.

Reading. Values are picked by their BUFR key *and* the `timePeriod` or
sensor height the template states before them, never by position in a
template:

| BUFR key | condition | element | conversion |
|---|---|---|---|
| `airTemperature` | sensor ≥ 1 m above ground (the 5 cm one is the grass minimum) | `t` | K − 273.15 |
| `dewpointTemperature` | sensor ≥ 1 m | `td` | K − 273.15 |
| `relativeHumidity` | — | `rh` | rounded half up |
| `nonCoordinatePressure` | — | `p` | Pa / 100 |
| `pressureReducedToMeanSeaLevel` | — | `slp` | Pa / 100 |
| `windSpeed`, `windDirection` | `timePeriod` −10 min | `ws`, `wd` | direction 0 (calm) and any direction at `ws` 0 are `null` |
| `maximumWindGustSpeed` | the shortest `timePeriod` given, up to −60 min | `gust` | m/s |
| `totalPrecipitationOrTotalWaterEquivalent` | `timePeriod` −10 min | `pr` | kg m⁻² = mm |
| `totalPrecipitationOrTotalWaterEquivalent` | `timePeriod` −60 min (or −1 h) | `pr1h` | mm |
| `sunshineOverPeriodSpecified` | `timePeriod` −10 min | `sun` | minutes |
| `totalSnowDepth` | — | `snow` | m × 100; negative codes `null` |
| `meteorologicalOpticalRange` | — | `vis` | m (DWD's ten-minute MOR); the largest value in the batches examined is 75 000 |
| `horizontalVisibility` | when no MOR | `vis` | m (the hourly SYNOP template) |

The one-minute precipitation series, the 3, 6, 12 and 24-hour totals and
the hourly and daily sunshine are not elements. `pr1h` is the hourly
batch's own one-hour total: the hourly national template states it
(`timePeriod` −60 min; 921 stations in the 2026-10-31 14:00 batch), while
the ten-minute template carries none and the WMO SYNOP template's own
one-hour total was empty in the batches examined. Nothing is summed.

Quality. The reports carry no quality flags: a missing value is `null`,
and a value outside its §2 range is `null`. A subset that names no station
or no time — the NIL reports of stations that sent nothing — is counted
in `unplaced` and dropped.

Station identity: `dwd:<DWD's station identifier>`, the `Kennung` column
of DWD's station list
(<https://opendata.dwd.de/weather/weather_reports/stationlist_synoptic_germany.csv>).
The BUFR carries it as `shortStationName` for a national station (WIGOS
`0-276-<n>-<local>`, e.g. `dwd:M031`, Ellrich-Werna) and as the WMO block
and station number for a WMO station (WIGOS `0-20000-0-<wmo>`, e.g.
`dwd:10393`, Lindenberg) and for the few national stations DWD numbers in
the WMO style (`dwd:10271`, Neuruppin, WIGOS `0-276-2-96`); checked
against the station list on 2026-10-31. `wmo` is the block and station
number where the BUFR carries one, and `wigos` the WIGOS identifier its
3 01 150 sequence states (`wigosIdentifierSeries`,
`wigosIssuerOfIdentifier`, `wigosIssueNumber`,
`wigosLocalIdentifierCharacter`), as given. Position, elevation (ground) and
`name` (`stationOrSiteName`) come from the newest subset that places the
station; `names` is `null`. `rank` is 0 for a station with a WMO number,
2 for one whose reports in the round were precipitation and nothing else,
1 otherwise.

Terms. DWD's open data is licensed under CC BY 4.0
(<https://www.dwd.de/copyright>, the `licenseUrl`); the source is credited as
"Source: Deutscher Wetterdienst"
(<https://www.dwd.de/EN/service/legal_notice/templates_dwd_as_source.html>),
which is the `attribution` string.

### 9.3 `hko` — Hong Kong Observatory

The Hong Kong Observatory's regional weather — about fifty manned,
automatic, buoy and wind stations every ten minutes — with the hourly
rainfall of its automatic weather stations and the water level of the
tide gauges around Hong Kong. `cadence` is 600; `wsPeriod` is 600 (the
ten-minute mean wind); `prPeriod` is `null`: there is no `pr`, and
rainfall is `pr1h` alone. The network is `partial` (§6): each element
comes from its own file, and the files of one round need not name the
same minute — the rainfall is stamped on the quarter hour and the tide on
five minutes, so a station's rainfall and its regional readings usually
sit at different times of its line.

Source. Three sources, each its own entry of §7 so that one failing does
not take the others with it. `files` is how many of a source's files were
read. Only the latest reading of each is served, so there is no backfill:
a round that does not run loses that time for good.

- `hko-regional-weather` — five CSVs under
  <https://data.weather.gov.hk/weatherAPI/hko_data/regional-weather/>:
  `latest_1min_temperature.csv`, `latest_1min_humidity.csv`,
  `latest_1min_pressure.csv`, `latest_10min_wind.csv` and
  `latest_10min_visibility.csv`, each row a time (HKT, `YYYYMMDDHHMM`), a
  station name and the values.
- `hko-hourly-rainfall` —
  <https://data.weather.gov.hk/weatherAPI/opendata/hourlyRainfall.php?lang=en>,
  the open-data API's "rainfall in the past hour from automatic weather
  station" (renewed every fifteen minutes): one `obsTime` (ISO 8601 with
  `+08:00`) and per station its name, an identifier local to this dataset
  (`RF001`…, or a GEO raingauge number such as `N12`), the value and the
  unit (`mm`). The value is the total over the hour ending at `obsTime`,
  in whole millimetres, or `M` while the system is under maintenance. The
  Observatory calls the data provisional, with limited validation, and
  notes that its own station's figure here is not its official rainfall.
- `hko-tide` — <https://data.weather.gov.hk/weatherAPI/hko_data/tide/ALL_en.csv>,
  the latest five-minute water level of each tide gauge (UTF-8 with a byte
  order mark): `Tide Station,Date,Time,Height(m)`, date and time HKT
  (`2026-11-12,20:35`), the height in metres to a centimetre, or dashes
  when there is none. The gauges are the Observatory's (Quarry Bay, Shek
  Pik, Tsim Bei Tsui, Tai Miu Wan, Tai Po Kau, Waglan Island) and the
  Drainage Services Department's at Tai O; the Observatory states that the
  real-time tide data are not quality-controlled.

Reading. Missing values are `N/A` or empty in the regional CSVs, `M` in
the rainfall and dashes in the tide file. There are no quality flags.

| file | field | element | conversion |
|---|---|---|---|
| regional | Air Temperature (°C) | `t` | — |
| regional | Relative Humidity (%) | `rh` | — |
| regional | Mean Sea Level Pressure (hPa) | `slp` | — (no station pressure: `p` stays `null`) |
| regional | 10-Minute Mean Wind Direction | `wd` | eight compass points, North = 360, Northeast = 45, …; `Calm` and `Variable` have no direction |
| regional | 10-Minute Mean Speed (km/h) | `ws` | ÷ 3.6; `Calm` is 0 |
| regional | 10-Minute Maximum Gust (km/h) | `gust` | ÷ 3.6 |
| regional | 10 minute mean visibility | `vis` | `18 km` → 18000; whole kilometres, no ceiling stated (35 km is the largest in the fixture) |
| rainfall | `value` at `obsTime` | `pr1h` | mm, as given |
| tide | Height(m) | `tide` | m above Hong Kong Chart Datum, as given |

The tide file's heights are "in metre (m) above the Chart Datum, which is
0.146 m below the Hong Kong Principal Datum" (the file layout,
<https://data.weather.gov.hk/weatherAPI/hko_data/tide/File_layout_for_latest_tides_en.pdf>);
the Observatory's real-time tide page labels the same values "metres above
Chart Datum (mCD)". The Observatory gives the 1995–2014 average annual
mean sea level at Quarry Bay as 1.45 m above Hong Kong Chart Datum
(<https://www.hko.gov.hk/en/climate_change/proj_hk_msl_med_conf_info.htm>),
so a `tide` there of about 1.45 is mean sea level.

Stations. None of the files carries a station number or a position, so
positions come from `xuebuild/synop/hko_stations.json`, built (2026-10-31)
from the Observatory's station page <https://www.hko.gov.hk/en/cis/stn.htm>
and its Chinese twin `/tc/cis/stn.htm`: code, English and Chinese
(`names: {"zh-Hant": …}`) name, position and ground elevation of every
current manned and automatic weather station, buoy and wind station. The
page gives the wind stations' anemometer height rather than the ground's,
so their `elev` is `null`. To it were added (2026-11-12) the gauges the
rainfall and tide feeds name that are not among those stations — each with
`elev` `null`, since the page gives a raingauge's height above sea level
and nothing for a tide gauge, and `rank` 2:

| code | name | from |
|---|---|---|
| `R23` | Tai Po Wong Shiu Chi Secondary School (大埔王肇枝中學) | the station page's automatic rainfall section |
| `N12` | Shui Pin Wai (水邊圍) | GEO raingauge N12 |
| `K02` | Broadcast Drive (廣播道) | GEO raingauge K02 |
| `K09` | San Po Kong (新蒲崗) | GEO raingauge K09 |
| `H17` | Magazine Gap (馬己仙峽) | GEO raingauge H17 |
| `QUB` | Quarry Bay (鰂魚涌) | the station page's tide gauge section |
| `SPW` | Shek Pik (石壁) | the station page's tide gauge section |
| `TBT` | Tsim Bei Tsui (尖鼻咀) | the station page's tide gauge section |
| `TMW` | Tai Miu Wan (大廟灣) | the station page's tide gauge section |
| `TAO` | Tai O (tide gauge) (大澳(潮汐站)) | the Observatory's real-time tide page (<https://www.hko.gov.hk/en/tide/marine/realtide.htm>, its station list `TAO`) |

The GEO raingauges are the Civil Engineering and Development Department's
Geotechnical Engineering Office's, numbered as the rainfall feed numbers
them; their positions are those of the "GEO Raingauge Data" dataset on the
CSDI portal (`cedd_rcd_1636517963140_47030`, records of 2025–2026), their
names the feed's English and Chinese ones. The station page lists the two
remaining tide gauges, Tai Po Kau and Waglan Island, under the codes of
existing stations, `TPK` (the wind station, at the same position) and
`WGL` (the weather station, about 100 m away), so their water level joins
those stations' lines.

A file's station name resolves to the table's English name, or to an
alias the table lists, after a typographic apostrophe is read as `'` (the
rainfall feed spells `King’s Park`):

| name | file | station |
|---|---|---|
| Chek Lap Kok | all | `HKA` Hong Kong International Airport |
| HK Observatory | all | `HKO` Hong Kong Observatory |
| HK Park | all | `HKP` Hong Kong Park |
| Pak Tam Chung | all | `TYW` Pak Tam Chung (Tsak Yue Wu) |
| Star Ferry | all | `SF` Star Ferry (Kowloon) |
| Tai Po | all | `YCT` Tai Po (Yuen Chau Tsai Park) |
| Tsuen Wan Ho Koon | all | `TWN` Tsuen Wan |
| Tsing Yi | temperature, humidity, pressure | `TY1` New Tsing Yi Station |
| Tsing Yi | wind | `SHL` Shell Oil Depot |
| Tuen Mun | temperature, humidity, pressure, rainfall | `TU1` Tuen Mun Children and Juvenile Home |
| Tuen Mun | wind | `TUN` Tuen Mun Government Offices |
| Tap Mun | wind | `TME` Tap Mun East |
| Cheung Ching | rainfall | `CPH` Ching Pak House (Tsing Yi) |
| Tai Po Market | rainfall | `R23` Tai Po Wong Shiu Chi Secondary School |
| Tai O | tide | `TAO` Tai O (tide gauge) |

The aliases were resolved by hand: by the name and address in the
Observatory's "Network of weather stations in Hong Kong" dataset on the
CSDI portal (which spells `Tsing Yi`, `Tuen Mun`, `Pak Tam Chung` and
`Tsuen Wan Ho Koon` the CSV's way), by the station page's own remarks
(the Tai Po station moved to Yuen Chau Tsai Park in 2022; the Tap Mun
East wind station replaced the Tap Mun one in 2017), and, where one name
covers two stations, by which of them the station page says measures the
file's element (New Tsing Yi Station has no anemometer, the Shell Oil
Depot station is one; of the two Tuen Mun stations only the Children and
Juvenile Home has a raingauge). For the rainfall: Ching Pak House is a
block of Cheung Ching Estate and the station page's only raingauge there
(`CPH` measures rainfall alone); Tai Po Market is the town centre of Tai
Po, where the only raingauge the station page lists is the one at Wong
Shiu Chi Secondary School (`R23`) — the Yuen Chau Tsai Park station has
none. Tai O is the drainage department's gauge rather than the
Observatory's wind station `TO` of the same name, about a kilometre away.

Some rainfall names match an Observatory station whose station page row
has no raingauge: the feed's numbering says these are GEO raingauges near
it — `N15` Sai Kung (`SKG`, about 0.9 km away), `K03` Kwun Tong (`KTG`, at
the same position), `H15` Stanley (`STY`, about 0.3 km) and `H24` Wong
Chuk Hang (`HKS`, about 0.4 km) — and `RF013` Ngong Ping (`NGP`) is most
likely the Ngong Ping Fresh Water Reservoir raingauge 0.4 km away. Their
rainfall is placed at the station of the same name. Three regional names
have no station in either source — `Hong Kong Sea School` (wind),
`Central` and `Sai Wan Ho` (visibility) — and are dropped, counted in
`unplaced`.

Station identity: `hko:<the Observatory's station code>` (`hko:HKO`,
`hko:HKA`, `hko:WGL`), or the GEO raingauge number for a GEO gauge
(`hko:K02`); `name` is the station page's English name, or the feed's for
a GEO gauge. `rank` is 0 for the two manned stations (`HKO`, `HKA`), 2 for
a gauge that measures rainfall or the tide alone (`CPH` and the stations
added above), and 1 for every other. The station page gives no WMO
numbers, so `wmo` is `null`.

Terms. DATA.GOV.HK's terms of use
(<https://data.gov.hk/en/terms-and-conditions>, the `licenseUrl`) allow reuse, commercial
included, on condition that the source is identified and the Government's
and the Observatory's ownership acknowledged, with proper attribution to
them and to DATA.GOV.HK. The `attribution` string, "Hong Kong Observatory
(DATA.GOV.HK)", is what a display shows.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).

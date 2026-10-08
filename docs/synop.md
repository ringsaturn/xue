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
normative description of that product, schema v1.

## 1. Delivery

Everything is a static file under the data root, beside the run pointers
and the other point products' pointers.

- `latest-synop.json` is the one mutable object. Fetch it with caching
  disabled. It names the newest round's directory and the index inside it:

  ```json
  {"schemaVersion": 1, "product": "synop", "issued": "2026-10-05T00:10:00Z",
   "path": "synop.202610050010/index.json", "byteLength": 186330, "crc32": "1c0ffee5"}
  ```

  `path` is relative to the pointer and always
  `synop.<round>/index.json`; `byteLength` and `crc32` (zlib CRC32 of the
  file's bytes, eight lowercase hex digits) are the index's, so it can be
  requested as `<path>?v=<crc32>` through an immutable cache.
- `synop.<YYYYMMDDHHMM>/` is one immutable directory per round: the UTC
  minute the product was aggregated at, always a multiple of ten. It holds
  `index.json` (§4), one `<network>.jsonl` per network (§5) and `item.json`,
  the round's STAC Item (`docs/stac.md` §"Point products"). Nothing in it
  changes once the pointer names it. The rounds sit flat under the root:
  each carries the whole window and lives three hours
  (`make prune-r2-synop`, `SYNOP_KEEP=18`), so there is no archive to file
  by day.
- Paths inside `index.json` are file names beside it; request each as
  `<name>?v=<crc32>` with the CRC the index carries.

Two ways to read a network:

- **One station.** Take the station's row in the index, which ends in the
  byte `offset` and `length` of its line in its network's file, and issue
  `GET synop.<round>/<network>.jsonl?v=<crc32>` with
  `Range: bytes=<offset>-<offset + length - 1>`. The bytes are that
  station's JSON object exactly, without the newline, so they parse on
  their own.
- **A whole network.** Fetch `<network>.jsonl` and read it line by line:
  one station per line, sorted by id.

The publisher builds every ten minutes (`publish-synop.yml`). Each network
is asked for what it holds after its newest time in the previous round's
index, so a network that was unreachable catches up, on its next answer,
as far back as its own retention reaches. A network that fails is recorded
(§6) and keeps its history; the round publishes with the rest. The pointer
is withheld only when *no* network's observations arrived, in which case
the previous round stays live. A round the pointer already names is
finished; the workflow's `force` input is the manual rebuild.

Deployment is one-sided: the product is new files under a new pointer, so
a shell that does not know it never asks for it. A new network is new
lines in the index and a new file, which a v1 reader admits (§7); a
`schemaVersion` bump follows the runs' rule (ship the shell first).

## 2. Elements and units

Every network is converted to one table of elements, SI throughout, at
the adapter. The index lists them in this order (`elements`):

| key | quantity | unit | type |
|---|---|---|---|
| `t` | air temperature | °C, to a tenth | number, −100 … 70 |
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
| `vis` | visibility | metres | integer, ≥ 0 |

- A network publishes the elements it measures and leaves the others
  `null`; nothing is derived (no dew point from humidity, no sea-level
  reduction). Observations only.
- `wd: 0` never occurs; north is `360`. With `ws: 0`, `wd` is `null`.
- `pr` and `sun` are totals over the period the network reports at, which
  is the network's `prPeriod` in seconds (§4); `pr1h` is the network's own
  hourly total, published where the network states one, because adding up
  shorter periods across a gap would understate it.
- A value the network marks as doubtful, missing or suspended, or one
  outside the range above, is `null`. Each network's section (§8) says
  which of its quality flags are kept.
- Times in the network files are **epoch seconds, UTC**; times in the
  index are ISO 8601 UTC with a `Z` suffix.
- Positions are decimal degrees to four places; `elev` is metres above
  sea level from the network's station table.

The files are written compactly with ASCII escaping, and a file's identity
is its bytes: the CRC32 in the index, and the pointer's, is of the file
exactly as served. Given the same inputs a round is byte-identical; only
the index's `generated` and the sources' `fetched` depend on when the
build ran.

## 3. Identity

A station is `<network>:<the network's own station number>`
(`amedas:50066` is Mt. Fuji). The network id is lowercase,
`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`; the station part is the network's own
identifier, unchanged. The id is the key everywhere: the index's rows are
sorted by it, each network file's lines are sorted by it, and every line
repeats it as `id` so a slice read by range identifies itself. A WMO
station number, where the network's table gives one, is carried as `wmo`
but is not the key — most national-network stations have none.

## 4. `index.json`

```json
{
  "schemaVersion": 1,
  "issued": "2026-10-05T00:10:00Z",
  "generated": "2026-10-05T00:15:30Z",
  "networks": [
    {"id": "amedas", "name": "JMA AMeDAS", "cadence": 600, "prPeriod": 600,
     "attribution": "出典：気象庁ホームページ (Japan Meteorological Agency)",
     "license": "PDL-1.0", "url": "https://www.jma.go.jp/bosai/amedas/",
     "latest": "2026-10-05T00:10:00Z",
     "file": {"path": "amedas.jsonl", "byteLength": 6779928, "crc32": "3b0b746f"}}
  ],
  "elements": ["t", "rh", "p", "slp", "wd", "ws", "gust", "pr", "pr1h", "sun", "snow", "vis"],
  "stations": [
    ["amedas:50066", 0, 35.36, 138.7267, 3775.0, 0, "Mt. Fuji", "2026-10-05T00:10:00Z",
     5.8, 98, 646.7, null, null, null, null, null, null, null, null, null, 3563, 283]
  ],
  "sources": [ … ]
}
```

- `issued` is the round, `generated` when the build ran.
- `networks[]` is one entry per network that has a station in the window,
  in the publisher's order: its id and display `name`; `cadence`, the
  seconds between a station's reports; `prPeriod`, the seconds `pr` and
  `sun` accumulate over (or `null`); the `attribution` a display must show,
  the `license` the network's data is reused under and the network's
  `url`; `latest`, the newest observation time in the file; and `file`, the
  network file's name (always `<id>.jsonl`), length and CRC32.
- `elements` is the element order of the rows. A reader indexes a row by
  position: `8 + elements.indexOf(key)`.
- `stations[]` is one compact row per station, sorted by id and unique:

  | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 … | last two |
  |---|---|---|---|---|---|---|---|---|---|
  | `id` | `network` | `lat` | `lon` | `elev` | `rank` | `name` | `obsTime` | one value per `elements` | `offset`, `length` |

  `network` is the index into `networks[]`, and the id's prefix is that
  network's id. `rank` is how prominent the station is, which a map thins
  by: `0` a principal station (a staffed office, a summit, a remote
  island), `1` a multi-element automatic station, `2` a station that
  measures precipitation alone. `name` is the station's name in Latin
  script, or `null`. `obsTime` is the station's newest time. The element
  values are the station's **current** values: for each element, its
  newest non-null value no more than an hour before `obsTime` — some
  elements arrive less often than the station reports (AMeDAS sends Mt.
  Fuji's humidity and pressure on the hour only) — except that `pr`,
  `pr1h` and `sun`, which are totals over a period ending at their time,
  are only ever taken at `obsTime`. `offset` and `length` are the byte span
  of the station's line in its network's file, the JSON object alone; the
  spans of one network's stations are in row order, do not overlap, and
  lie inside that file's `byteLength`.
- `sources[]` is §6.

The index is what a map layer reads: every station's position and current
values, about 190 KB for AMeDAS's 1 300 stations, and no history.

## 5. `<network>.jsonl`

One line per station of that network, sorted by id, each the compact JSON
object below followed by `\n`:

```
{"id": "amedas:50066", "name": "Mt. Fuji" | null,
 "names": {"ja": "富士山", "ja-Kana": "フジサン"} | null,
 "lat": 35.36, "lon": 138.7267, "elev": 3775.0 | null, "wmo": null, "rank": 0,
 "time": [1791158400, 1791159000, …],     — epoch seconds UTC, strictly increasing
 "obs": {"t": [5.5, 5.8, …], "rh": [98, null, …], "p": [646.7, null, …]}}
```

- `names` maps BCP 47 language tags to the network's own spellings;
  a display picks the viewer's language and falls back to `name`.
- `time` is the station's window, oldest first: every time in the last
  24 hours before the round at which the station reported anything.
- `obs` holds one array per element the station reported at least once in
  the window, each as long as `time`, `null` where that element was not
  reported at that time. An element the station never reported in the
  window is absent, and reads as all `null`.

A station's window is a merge, not a snapshot. Each round lays the
network's new observations over the previous round's file keyed by
(station, time), the new one replacing the old whole — a network that
revises a value is believed — and drops what is older than 24 hours
before the round. A station that did not report keeps its history; one
with nothing left in the window leaves the file and the index until it
reports again. Station metadata comes from the network's current table
each round. A network with no station left is not listed.

## 6. `sources[]`

One entry per source, every network's in turn, in build order:

```json
{"id": "jma-amedas-map", "ok": true, "fetched": "2026-10-05T00:13:34Z",
 "url": "https://www.jma.go.jp/bosai/amedas/data/map", "snapshots": 1, "missing": 0, "reports": 1284}
{"id": "jma-amedas-table", "ok": true, "fetched": "2026-10-05T00:04:00Z",
 "url": "https://www.jma.go.jp/bosai/amedas/const/amedastable.json", "cached": true, "reports": 1286}
```

`ok: false` always carries `error`. `reports` is what the source
contributed — observations read, stations in a table. A network's own
fields are described in its section.

## 7. Validation

`xuebuild/synop/schema.py` validates every file as it is written, and
`tests/test_synop.py` holds two consecutive rounds built from the fetched
fixture (`tests/fixtures/synop/synop.202610050000/` and `…0010/`) to the
committed golden (`tests/fixtures/synop/expected/`), regenerated by
`tests/prepare_synop_golden.py`; every index row's span is checked against
the network file it addresses. Admission is structural: a network, a
station or an element key a reader has never seen is not an error — the
row width follows the index's own `elements` — and values of the elements
in §2 are held to their ranges. A reader validates the same shapes on read
and rejects a `schemaVersion` above the one it implements.

## 8. Networks

### 8.1 `amedas` — JMA AMeDAS

The Japan Meteorological Agency's Automated Meteorological Data
Acquisition System: about 1 300 stations over Japan and its islands, every
ten minutes. `cadence` and `prPeriod` are 600.

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
(missing) become `null`. The elements:

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

`gust` is not in the snapshots and stays `null`. The hourly snapshot (on
the hour) carries more than the others: several stations, Mt. Fuji among
them, report humidity and pressure only then, and snow depth and the
present-weather code appear only then (the weather code is not published
in v1).

Station identity: `amedas:<the five-digit AMeDAS number>`; `name` is the
table's English name, `names` its kanji (`ja`) and kana (`ja-Kana`).
`rank` is 0 for the table's types A, B, D, E, F and G (staffed offices,
special stations, and single sites such as Mt. Fuji and Minamitorishima),
2 for a type-C station whose element mask has neither temperature nor
wind (a precipitation gauge), and 1 otherwise. A snapshot entry for a
station the table does not list is dropped.

Terms. The agency's website content is reusable, commercially included,
under the Public Data License v1.0 with the source credited —
「出典：気象庁ホームページ」 — and, for edited content, a statement that it
was edited; it must not be presented as the agency's own publication
(<https://www.jma.go.jp/jma/kishou/info/coment.html>). The `attribution`
string carries the credit, and a display shows it with the data.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).

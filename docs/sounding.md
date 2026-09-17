# The radiosonde sounding product

The `.xue` container is a raster container. A radiosonde ascent — a few
hundred levels of pressure, height, temperature, dew point and wind at one
point — is not a raster, so soundings are another, smaller product
published beside the runs: plain JSON, a few megabytes an hour, aggregated
by `xue sounding-build` (`xuebuild/sounding/`) from the TEMP bulletins the
world's weather services exchange, and taken live by the same pointer →
immutable directory contract the runs and the tropical cyclone product
use. This document is the normative description of that product, schema
v1. The design it implements is in the project's private notes; what is
here is the contract a reader can hold the publisher to.

## 1. Delivery

Everything is a static file under the data root, the same root as the run
pointers (`latest.json`, `latest-<model>.json`), `latest-tc.json` and
`showcase.json`.

- `latest-sounding.json` is the one mutable object. Fetch it with caching
  disabled. It names the newest issue directory and the index inside it:

  ```json
  {"schemaVersion": 1, "product": "sounding", "issued": "2026-09-14T02:00:00Z",
   "path": "sounding.2026091402/index.json", "byteLength": 6569, "crc32": "c07d6fe2"}
  ```

  `path` is relative to the pointer and always
  `sounding.<issued hour>/index.json`; `byteLength` and `crc32` (zlib
  CRC32 of the file's bytes, eight lowercase hex digits) are the index's,
  so it can be requested as `<path>?v=<crc32>` through an immutable cache.
- `sounding.<YYYYMMDDHH>/` is one immutable directory per issue: the UTC
  hour the product was aggregated at, not any sounding's nominal time. It
  holds the product's two files, `index.json` and `soundings.jsonl`, and
  beside them `item.json`, the issue's STAC Item — a pure function of the
  index, read by a catalog client and by nothing in the product
  (`docs/stac.md` §"Point products"). Once the
  pointer names it nothing in it changes; a manual rebuild of an hour
  (`sounding-build --issue … --force`) writes the directory again, and
  both files are addressed by their CRC, so a cache never serves one
  hour's bytes under another's key. Directories are pruned after two days
  (`make prune-r2-sounding`).
- `soundings.jsonl` is every station's soundings, one JSON object per
  line, sorted by station id. `index.json` spans it: the `soundings`
  descriptor gives the whole file's `byteLength` and `crc32`, and each
  `stations[]` row gives the `offset` and `length` of that station's
  object. So there are two ways to read the product and both are cheap:

  - **one station**, the way the viewer reads it — fetch
    `soundings.jsonl?v=<crc32>` with
    `Range: bytes=<offset>-<offset+length-1>`. The span excludes the
    trailing newline, so what comes back parses as JSON on its own.
  - **all of it**, the way an analyst reads it — fetch the one file and
    stream it line by line. No directory traversal, no thousand small
    objects.

  One file per issue rather than one per station is deliberate: a bucket
  full of small objects is slow and expensive to traverse for anyone doing
  statistics over the archive, and it bills per object.

The publisher builds every hour at a quarter past (`publish-sounding.yml`).
A station with no new ascent this hour is carried forward from the
previous issue's `soundings.jsonl`, so its content is unchanged even
though the file around it is rewritten. A gateway that is late or
unreachable is recorded (see `sources`) and the hour publishes without it.
The pointer is withheld only when neither gateway contributed *and* there was nothing to
carry forward, in which case the previous hour stays live. An hour the
pointer already names is finished: a run that resolves to it builds
nothing, and the workflow's `force` input is the manual rebuild.

Deployment is one-sided: the product is new files under a new pointer, so
a shell that does not know it never asks for it. A `schemaVersion` bump
follows the runs' rule (ship the shell first); an added optional field
does not bump the version.

## 2. Units and fixed point

The scalar fields are SI, converted at the parser. The level arrays are
fixed-point integers — a sounding is a few hundred levels and JSON floats
would double the file for no precision — with `-32768` for a value the
bulletin does not report:

| array | quantity | unit |
|---|---|---|
| `p` | pressure | Pa |
| `z` | geopotential height | gpm |
| `t` | temperature | K × 100 |
| `td` | dew-point temperature | K × 100 |
| `wd` | wind direction, the direction the wind comes from | degrees |
| `ws` | wind speed | m/s × 10 |
| `sig` | `extendedVerticalSoundingSignificance` | the BUFR flag word, verbatim |

The seven arrays are parallel and all of length `n`. `-32768` is the
missing value in every one of them, including `sig`; it is a JSON number,
not a 16-bit field, so a height of 35 000 gpm is written out in full.

Scaling is half-up on the magnitude (`-0.5` and `0.5` both round away from
zero), so a second implementation does not have to know one language's
rounding rule.

`p` is the vertical axis: it is present on every level, strictly
descending, and the levels are ordered by it. A level the bulletin
reports without a pressure has no place on that axis and is not published;
two levels reported at one pressure (a significant temperature level and a
significant wind level at the same height, as a few centres file them) are
folded into one, the first keeping its values, taking what it is missing
from the second, and the two `sig` words or-ed together.

`sig` is BUFR flag table 0 08 042, an 18-bit field numbered left to right,
so bit 1 is `1 << 17`. The bits a reader is likely to want:

| bit | value | meaning |
|---|---|---|
| 1 | 131072 | surface |
| 2 | 65536 | standard level |
| 3 | 32768 | tropopause level |
| 4 | 16384 | maximum wind level |
| 5 | 8192 | significant temperature level |
| 6 | 4096 | significant humidity level |
| 7 | 2048 | significant wind level |

**Thinning.** The product publishes the classical TEMP set, not every
sample. A modern sonde reports every second — seven thousand levels of an
ascent whose mandatory-and-significant set is forty to a hundred and
fifty — which is detail no skew-T or model-profile comparison draws, at
two orders of magnitude of bytes. After the levels are ordered and levels
at one pressure folded, a level is published when:

- its `sig` has any of bits 1–7 set (surface, standard, tropopause,
  maximum wind, significant temperature, significant humidity,
  significant wind); or
- it is the lowest or the highest level of the ascent; or
- its pressure is at least **3 %** below the last published level's
  pressure — `p_last_published / p >= 1.03`, walking upward, the last
  published level being whichever was kept, flagged or not.

A `sig` of `-32768` counts as unflagged. A properly flagged ascent passes
through untouched; an unflagged run is capped at about 150 levels between
1000 and 10 hPa. `n` is the published level count and `reported` beside it
is what the bulletin held before thinning, so a reader can always see what
was left out — and the three sample bulletins in the fixture lose one
level between them.

Times are ISO 8601, UTC, `Z` suffix. Numbers outside the level arrays are
JSON numbers, and a value not reported is `null`. The files are written
compactly with ASCII escaping, and a file's identity is its bytes: the
CRC32 in the index is of the file exactly as served.

## 3. Identity

A station has one id for its whole life in the product, and it is a WIGOS
identifier: `<series>-<issuer>-<issue number>-<local identifier>`, written
out, which is digits and hyphens and needs no escaping in a URL. It is
what `soundings.jsonl` is sorted by and what the index's rows key on.

- Two soundings are the same station when they carry the same five-digit
  WMO number, `block × 1000 + station`. That number is the identity key,
  because a station that has migrated to a native WIGOS identifier is
  still filed under its WMO number by the centres that have not.
- The published `id` is the station's *native* WIGOS identifier when any
  of the hour's bulletins carries one (China's stations do:
  `0-20001-0-51463`), and otherwise the legacy form of the WMO number,
  `0-20000-0-<wmo>`. A station seen under both forms in one hour keeps the
  native identifier.
- `wmo` carries the five-digit number whenever it is known, whichever form
  the id took.
- A subset with neither a WMO number nor a usable WIGOS identifier — a
  ship, a mobile station, a WIGOS local identifier that is not
  URL-safe — is dropped and counted in its gateway's `dropped`.

A sounding's *nominal time* is the synoptic hour it is filed under: the
`<ddhhmm>` of the bulletin's WMO file name, dated by the month the
gateway's arrival stamp puts it nearest. It is not the launch time. The
launch time is the subset's own clock (`timeSignificance` 18) and is
published as `launched` when it lies between 90 minutes before the nominal
hour and an hour after it — the window that covers both the Chinese
stations releasing at 23:15 for 00Z and the Japanese ones at 00:16.
Anything outside is a clock the product does not trust, and `launched` is
`null`.

**Deduplication.** The same station-hour arrives more than once: the two
gateways carry overlapping sets, a station may be filed in more than one
bulletin, and corrections (`CCA`, `RRA`…) repeat a bulletin outright. One
sounding survives per (station, nominal time): **the one with the most
levels**, and on a tie **the one that arrived last**, which is the
correction. A station reformatted into four part-bulletins therefore keeps
the longest part and loses the others; merging the parts is not in v1.

## 4. `index.json`

```json
{
  "schemaVersion": 1,
  "issued": "2026-09-14T02:00:00Z",
  "generated": "2026-09-14T02:05:00Z",
  "watermark": {"jp-jma-gts-to-wis2": "2026-09-14T02:34:41Z", "de-dwd-gts-to-wis2": null},
  "soundings": {"path": "soundings.jsonl", "byteLength": 105657, "crc32": "…"},
  "stations": [
    {
      "id": "0-20000-0-47401", "wmo": "47401", "name": null,
      "lat": 45.415, "lon": 141.679, "elev": 2.9,
      "offset": 0, "length": 1531,
      "latest": "2026-09-14T00:00:00Z",
      "times": ["2026-09-14T00:00:00Z", "2026-09-13T12:00:00Z"],
      "headline": {"t500": -13.7, "td500": -40.7, "freezingLevel": 3292, "pw": 26.3, "levels": 15}
    }
  ],
  "sources": [ … ]
}
```

- `issued` is the aggregation hour, `generated` when the build ran.
- `watermark` is per source: the newest object modification time that
  source's listing held when the issue was built, which is where the next
  hour's fetch starts. `null` means the source has never been listed
  successfully. It is published because it is the product's own account of
  how current it is; a reader may ignore it.
- `soundings` describes the file beside the index: `path` is always
  `soundings.jsonl`, `byteLength` its length and `crc32` the zlib CRC32 of
  its bytes, which is its `?v=`.
- `stations[]` is sorted by `id`, and so is `soundings.jsonl`. `offset`
  and `length` are the byte span of that station's object in the file,
  excluding the newline after it. The spans are strictly increasing, do
  not overlap, and tile the file exactly: `Σ(length + 1) == byteLength`.
- `lat`, `lon` and `elev` come from the newest sounding. `elev` is metres
  above mean sea level, the first of three heights the subset may carry,
  in this order: the ground under the station
  (`heightOfStationGroundAboveMeanSeaLevel`), the BUFR height of station
  (`height`), the barometer's height; `null` when the bulletin gives none.
  `name` is `null` in v1: the bulletins carry no station names.
- `latest` is the newest nominal time and equals `times[0]`; `times` lists
  the nominal times the station's line carries, newest first, at most four.
- `headline` summarises the newest sounding for a marker layer that has
  not fetched the soundings file: `t500` and `td500` are the 500 hPa
  temperature and dew point **in °C** to a tenth (`null` when that level is
  absent), `freezingLevel` and `pw` repeat the derived values (§7), and
  `levels` is that sounding's `n`.
- `sources[]` is the build's account of its two gateways (§6).

A station whose newest sounding is more than 48 hours older than the issue
hour is not carried forward any further: it has stopped reporting, or its
identifier has changed.

## 5. `soundings.jsonl`

One line per station, sorted by station id, each line the compact JSON
object below followed by `\n`. It carries no `schemaVersion` and no
`sources`: the index it hangs off has both, and repeating them once per
station would cost more than the stations do.

```
{
  id, wmo, name, lat, lon, elev,
  soundings: [                        — the newest four nominal times, newest first
    {
      time:      the nominal (synoptic) time,
      launched:  the launch time | null,
      sondeType: BUFR code table 0 02 011 | null,
      bulletin:  "IUSC01 RJTD 140000" (plus " CCA" on a correction),
      gateway:   the source id the bytes came from,
      arrived:   when the gateway published the bulletin,
      n:        the published level count,
      reported: the levels the bulletin held before thinning (§2),
      p, z, t, td, wd, ws, sig:  n integers each (§2),
      derived: {freezingLevel, pw, lapse850_500, tropopause}   — §7
    }
  ]
}
```

`soundings` is in strictly decreasing `time`, newest first, and holds at
most four nominal times — two days of the twice-daily cycle. Each hour's
build merges what arrived into what the previous issue published for the
station, under the same rule §3 gives: for one nominal time, the most
levels wins, then the latest arrival. A station with nothing new this hour
is carried forward from the previous issue's file, its line re-encoded
unchanged; the file around it is rewritten every hour, which at this size
costs nothing worth saving.

`bulletin` is the WMO abbreviated heading of the bulletin the sounding was
decoded from, verbatim, and `arrived` the gateway's own timestamp for it.
Together they say exactly which message a value came from.

**Size.** Measured over one issue of the live feed, 491 stations: the
newest ascent held a median of 504 reported levels and a maximum of 7 553,
of which the thinning rule (§2) publishes a median of 169 and a maximum of
383 — 9.7 % of the samples. The median station line is 23 KB and the
largest 46 KB; the issue is 10.6 MB behind a 160 KB index, in two objects.
A client that wants a marker layer reads the index alone; opening one
station costs one range request of its `length`.

## 6. `sources[]`

One entry per source the build was asked for, in build order:

```json
{"id": "jp-jma-gts-to-wis2", "ok": true, "fetched": "2026-09-14T02:04:00Z",
 "url": "https://wis2globalcache.s3.us-east-1.amazonaws.com/data/jp-jma-gts-to-wis2/data/core/I/U/S/",
 "watermark": "2026-09-14T02:34:41Z", "listed": 1808, "objects": 47, "reused": 0,
 "bytes": 2310221, "bulletins": 47, "unreadable": 0, "subsets": 214, "dropped": 0, "soundings": 214}
{"id": "de-dwd-gts-to-wis2", "ok": false, "fetched": "2026-09-14T02:04:00Z",
 "watermark": null, "error": "request failed for …: HTTP Error 503"}
```

`ok: false` always carries `error`. `listed` is how many objects the
source's listing held, `objects` how many were downloaded (the ones newer
than the previous watermark), `reused` how many were carried over from the
previous issue's download instead, `failed` how many the cache would not
serve, `bulletins` how many decoded, `subsets` how many station subsets
they held and `dropped` how many of those carried no usable station
identity. The list lives in the index alone: a station's line does not
repeat it, so it always describes the issue the reader is holding, even
for a station whose soundings were carried forward from an earlier one.

A source's `watermark` only advances when its bulletins were read. A
listing that failed, and a download not one bulletin of which decoded,
both leave the previous hour's watermark in place, so the next hour sees
those objects again rather than stepping past them for good.

Source ids in v1 are the two GTS→WIS2 gateway directories in the WMO WIS2
Global Cache, a public bucket that lists without credentials and keeps an
object for 24 hours: `jp-jma-gts-to-wis2` (run by the Japan Meteorological
Agency) and `de-dwd-gts-to-wis2` (run by the Deutscher Wetterdienst). Each
republishes *every* country's GTS bulletins under the WMO abbreviated
heading tree, and the two carry different sets, which is why both are
read. The id shape `wis2:<centre>` is reserved for the national WIS2
nodes' own topic directories, which will replace the gateways as the
migration finishes; it is not implemented in v1.

The data is WMO core data under the WMO Unified Data Policy: free and
unrestricted, with attribution of the original source requested. The
originating national meteorological and hydrological services are the
source of every sounding; the WIS2 Global Cache and its two gateways are
the distribution.

## 7. Derived quantities

`derived` holds four numbers computed from the published levels — not from
the BUFR floats — in a fixed operation order, so a second implementation
reading `<station>.json` reproduces them exactly. Heights are rounded to
whole geopotential metres, the other two to a tenth. Any of them is `null`
when the sounding does not support it.

A derived height is a published `z`, interpolated or picked out, so it
carries `z`'s range and may be negative: stations below sea level report
negative geopotential heights, and a bulletin that flags a tropopause at
one is reporting a bad height rather than breaking the contract. A reader
sanity-checks derived values; the product does not silently drop them.

**`freezingLevel`** (gpm). Walking up from the surface, the first pair of
consecutive levels that both carry a temperature and a height and that
straddle 273.15 K with the lower one at or above it. The height is linear
in temperature across that pair:
`z = z_lower + (273.15 − t_lower) / (t_upper − t_lower) × (z_upper − z_lower)`.
`null` when the lowest level with a temperature is already below freezing
(there is no warm layer to leave) or when the profile never crosses.

**`pw`** (mm). The depth of the water-vapour column. Only levels carrying
both a pressure and a dew point take part, in descending pressure, up to
the last one at or above 300 hPa; the integral is not extrapolated to
exactly 300 hPa, so a sounding that stops early reports what it measured.
Per level, with `p` in hPa and the dew point in °C:

```
e = 6.112 × exp(17.67 × td / (td + 243.5))      Bolton (1980) eq. 10, hPa
w = 0.622 × e / (p − e)                          mixing ratio
q = w / (1 + w)                                  specific humidity
```

and the column is the trapezoidal integral of `q` over pressure in
pascals, summed from the surface upwards, divided by standard gravity
9.80665 m/s². `null` when fewer than two levels qualify. (Worked example:
two levels at 1000 and 900 hPa with a 10 °C dew point give `e` =
12.271696 hPa, `q` = 0.00766857 and 0.00852504, and `pw` = 8.3 mm.)

**`lapse850_500`** (K/km, positive when the temperature falls with
height). `(t850 − t500) / ((z500 − z850) / 1000)`, where the 850 and
500 hPa levels are levels the sounding actually reports, within one
hectopascal of their nominal pressure, each carrying a temperature and a
height. The product never interpolates a standard level that was not
reported, so `null` here means the ascent did not report one of them — a
high station whose surface is above 850 hPa, or a bulletin that carries
only significant levels.

**`tropopause`** (gpm). The height of the first level, in descending
pressure, whose `sig` has bit 3 (`32768`) set and which carries a height.
A sounding may flag a secondary tropopause above the first; the lowest is
published. `null` when no level is flagged, or when the flagged level
reports no height — which several centres' bulletins do.

## 8. Validation

`xuebuild/sounding/schema.py` validates every file as it is written, and
`tests/test_sounding.py` holds a build from the fetched fixture
(`tests/fixtures/sounding/sounding.2026091402/`) to the committed golden
(`tests/fixtures/sounding/expected/`), regenerated by
`tests/prepare_sounding_golden.py`. A reader validates the same shapes on
read and rejects a `schemaVersion` above the one it implements.

Admission is structural, in the posture the manifests take: a station
identifier the reader has never seen, a radiosonde type code it does not
know and a source id that is not one of today's two are all admitted. What
is checked is shape — the id pattern, coordinate ranges, arrays of one
length, pressure strictly descending, timestamps in UTC, CRC32s, the
`soundings` descriptor, and the stations' byte spans: in order, not
overlapping, and tiling `soundings.jsonl` exactly. Every line is validated
as it is written, so a span the index advertises always slices out
something that parses.

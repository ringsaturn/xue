# The alerts product

A weather warning is a short record — who issued it, what hazard, how
severe, where, until when — and a national service has a few dozen to a
few thousand in force at once. That is not a raster, so warnings are a
point product published beside the runs, the airports (`docs/airport.md`)
and the stations (`docs/synop.md`). Each national warning service enters
through an adapter that reads it into one shape — a closed list of
categories, one five-level scale, the service's own codes kept beside
them, and every language the service publishes — and the product carries
every service in that shape. The snapshot is taken every five minutes by
`xue alerts-build` (`xuebuild/alerts/`) and taken live by the same
pointer → immutable directory contract the other point products use. This
document is the normative description of that product, schema v1.

## 1. Delivery

Everything is a static file under the data root, beside the run pointers
and the other point products' pointers.

- `latest-alerts.json` is the one mutable object. Request it with
  `Cache-Control: no-cache` (in a browser, `fetch(url, {cache: "no-cache"})`):
  without it a browser cache may answer with a pointer up to four hours
  old. It names the newest round's directory and the index inside it:

  ```json
  {"schemaVersion": 1, "product": "alerts", "issued": "2026-11-01T00:05:00Z",
   "path": "alerts.202611010005/index.json", "byteLength": 89205, "crc32": "1c0ffee5"}
  ```

  `path` is relative to the pointer and always
  `alerts.<round>/index.json`; `byteLength` and `crc32` (zlib CRC32 of the
  file's bytes, eight lowercase hex digits) are the index's, so it can be
  requested as `<path>?v=<crc32>` through an immutable cache.
- `alerts.<YYYYMMDDHHMM>/` is one immutable directory per round: the UTC
  minute the snapshot was taken at, always a multiple of five. It holds
  `index.json` (§5), one `<network>.jsonl` per network (§6) and
  `item.json`, the round's STAC Item (`docs/stac.md` §"Point products").
  Nothing in it changes once the pointer names it. The rounds sit flat
  under the root: a round is a snapshot, not a history, and lives three
  hours (`make prune-r2-alerts`, `ALERTS_KEEP=36`).
- `alerts-areas/<set>.<version>.pmtiles` are the boundary sets (§9): one
  file per upstream release, immutable under its versioned name and kept
  for as long as any round may name it, each with its codes' boxes beside
  it in `<set>.<version>.bbox.json`. They are not part of a round and
  are never pruned with one.
- Paths inside `index.json` are relative to it; request each as
  `<path>?v=<crc32>` with the CRC the index carries.

A round is a **snapshot of what is in force at `issued`**: what each
network lists as current, less what has expired by the round's minute. It
carries no history; `ended[]` (§7) names what left since the previous
round, for that round only.

Two ways to read an alert's full record:

- **One alert.** Take its row in the index, which ends in the byte
  `offset` and `length` of its line in its network's file, and issue
  `GET alerts.<round>/<network>.jsonl?v=<crc32>` with
  `Range: bytes=<offset>-<offset + length - 1>`. The bytes are that
  alert's JSON object exactly, without the newline, so they parse on their
  own.
- **A whole network.** Fetch `<network>.jsonl` and read it line by line:
  one alert per line, sorted by id.

The publisher builds every five minutes (`publish-alerts.yml`). A network
that fails is recorded (§8) and carries its previous round's alerts over
(§7); the round publishes with the rest. The pointer is withheld only when
*no* network's state arrived, in which case the previous round stays live.
A round the pointer already names is finished; the workflow's `force`
input is the manual rebuild.

Deployment is one-sided: the product is new files under a new pointer, so
a shell that does not know it never asks for it. A new network is new rows
and a new file, which a v1 reader admits (§11); a `schemaVersion` bump
follows the runs' rule (ship the shell first).

## 2. Categories and levels

Every alert is filed under one **category**, from a closed list the index
repeats in this order (`categories`):

| key | hazard |
|---|---|
| `rain` | heavy rain |
| `flood` | river, flash and snowmelt flooding |
| `landslide` | landslides and debris flows |
| `wind` | wind on land, tropical cyclones |
| `thunderstorm` | thunderstorms, tornadoes, lightning |
| `snow-ice` | snow, blizzard, freezing rain, icing, avalanche |
| `heat` | heat |
| `cold` | cold, frost, freeze, wind chill |
| `fog` | fog |
| `dust` | dust and sand |
| `fire` | fire weather and dry air |
| `air` | air quality, smoke, ashfall |
| `coastal` | storm surge, coastal and lakeshore flooding, surf, rip currents |
| `marine` | conditions at sea: gales, seas, freezing spray, waves |
| `tsunami` | tsunami |
| `other` | anything else, including non-weather civil alerts |

and given one **level**, an integer 1 … 5 on a scale every network maps
onto:

| level | meaning | NWS | JMA | ECCC |
|---|---|---|---|---|
| 1 | statement, information | Statement, Outlook, Message, Forecast | — | statement (no colour) |
| 2 | advisory, be aware | Advisory, Watch | 注意報 (レベル２) | yellow; advisory or watch (no colour) |
| 3 | warning | Warning | 警報 (レベル３) | orange; warning (no colour) |
| 4 | danger | Warning with CAP severity Extreme | 危険警報 (レベル４) | red |
| 5 | exceptional | — | 特別警報 (レベル５) | — |

Level 5 is JMA's 特別警報 alone: no other network has a tier above its
danger level, and folding it into 4 would lose the distinction the agency
draws. The level and category are this product's; the network's own
event name, code and level are kept beside them in the line's `native`
(§6), verbatim.

## 3. Mapping per network

The mappings are data, in `xuebuild/alerts/tables.json`, and this section
is that file in prose. An event name, subject or code a table does not
list is not an error: it is filed under `other` and levelled by its last
word (NWS) or its colour and type (ECCC); a JMA code the table does not
know is skipped and counted (§8).

### 3.1 NWS

Level by event name, else by its last word:

| last word | level |
|---|---|
| Statement, Outlook, Message, Forecast | 1 |
| Advisory, Watch | 2 |
| Warning | 3 |
| anything else | 1 |

Named exceptions: `911 Telephone Outage` and `911 Telephone Outage
Emergency` 1, `Blue Alert` 1, `Air Quality Alert` 2, `Child Abduction
Emergency` 3, `Civil Emergency Message` 3, `Extreme Fire Danger` 3,
`Local Area Emergency` 3, `Evacuation Immediate` 4. Then any event ending
in `Warning` whose CAP `severity` is `Extreme` (a tornado emergency, a
particularly dangerous situation, a tsunami or extreme wind warning) is
raised to 4. Watches stay 2 at any severity: a watch says conditions are
possible, not occurring.

Category by event name (every event `api.weather.gov/alerts/types`
listed on 2026-10-31, and the names it retired that may still appear):

| category | NWS events |
|---|---|
| `flood` | Arroyo And Small Stream Flood Advisory, Flash Flood Statement, Flash Flood Warning, Flash Flood Watch, Flood Advisory, Flood Statement, Flood Warning, Flood Watch, Hydrologic Advisory, Hydrologic Outlook, Small Stream Flood Advisory, Urban And Small Stream Flood Advisory |
| `wind` | Extreme Wind Warning, High Wind Warning, High Wind Watch, Hurricane Local Statement, Hurricane Warning, Hurricane Watch, Lake Wind Advisory, Tropical Cyclone Local Statement, Tropical Depression Local Statement, Tropical Storm Local Statement, Tropical Storm Warning, Tropical Storm Watch, Typhoon Local Statement, Typhoon Warning, Typhoon Watch, Wind Advisory |
| `thunderstorm` | Severe Thunderstorm Warning, Severe Thunderstorm Watch, Severe Weather Statement, Tornado Warning, Tornado Watch |
| `snow-ice` | Avalanche Advisory, Avalanche Warning, Avalanche Watch, Blizzard Warning, Blizzard Watch, Freezing Rain Advisory, Ice Storm Warning, Lake Effect Snow Advisory, Lake Effect Snow Warning, Lake Effect Snow Watch, Snow Squall Warning, Winter Storm Warning, Winter Storm Watch, Winter Weather Advisory |
| `heat` | Excessive Heat Warning, Excessive Heat Watch, Extreme Heat Warning, Extreme Heat Watch, Heat Advisory |
| `cold` | Cold Weather Advisory, Extreme Cold Warning, Extreme Cold Watch, Freeze Warning, Freeze Watch, Frost Advisory, Hard Freeze Warning, Hard Freeze Watch, Wind Chill Advisory, Wind Chill Warning, Wind Chill Watch |
| `fog` | Dense Fog Advisory, Freezing Fog Advisory |
| `dust` | Blowing Dust Advisory, Blowing Dust Warning, Dust Advisory, Dust Storm Warning |
| `fire` | Extreme Fire Danger, Fire Warning, Fire Weather Watch, Red Flag Warning |
| `air` | Air Quality Alert, Air Stagnation Advisory, Ashfall Advisory, Ashfall Warning, Dense Smoke Advisory |
| `coastal` | Beach Hazards Statement, Coastal Flood Advisory, Coastal Flood Statement, Coastal Flood Warning, Coastal Flood Watch, High Surf Advisory, High Surf Warning, Lakeshore Flood Advisory, Lakeshore Flood Statement, Lakeshore Flood Warning, Lakeshore Flood Watch, Rip Current Statement, Storm Surge Warning, Storm Surge Watch |
| `marine` | Brisk Wind Advisory, Freezing Spray Advisory, Gale Warning, Gale Watch, Hazardous Seas Warning, Hazardous Seas Watch, Heavy Freezing Spray Warning, Heavy Freezing Spray Watch, Hurricane Force Wind Warning, Hurricane Force Wind Watch, Low Water Advisory, Marine Weather Statement, Small Craft Advisory, Small Craft Advisory For Hazardous Seas, Small Craft Advisory For Rough Bar, Small Craft Advisory For Winds, Special Marine Warning, Storm Warning, Storm Watch |
| `tsunami` | Tsunami Advisory, Tsunami Warning, Tsunami Watch |
| `other` | 911 Telephone Outage, 911 Telephone Outage Emergency, Administrative Message, Blue Alert, Child Abduction Emergency, Civil Danger Warning, Civil Emergency Message, Earthquake Warning, Evacuation Immediate, Hazardous Materials Warning, Hazardous Weather Outlook, Law Enforcement Warning, Local Area Emergency, Nuclear Power Plant Warning, Radiological Hazard Warning, Shelter In Place Warning, Short Term Forecast, Special Weather Statement, Volcano Warning |

The NWS issues no rain-only or landslide product; heavy rain reaches the
public as flood products. `Storm Warning` and `Storm Watch` are the
marine storm-force products, hence `marine`.

### 3.2 JMA

By kind code of the r8 warning map (the code table of the agency's own
warning page). Level is the agency's own レベル; the English names are the
page's translation table, published here for display, not in the files.

| code | 種別 | English | element | level | category |
|---|---|---|---|---|---|
| `10` | レベル２大雨注意報 | Heavy Rain Advisory | `rain` | 2 | `rain` |
| `03` | レベル３大雨警報 | Heavy Rain Warning | `rain` | 3 | `rain` |
| `43` | レベル４大雨危険警報 | Heavy Rain Urgent Warning | `rain` | 4 | `rain` |
| `33` | レベル５大雨特別警報 | Heavy Rain Emergency Warning | `rain` | 5 | `rain` |
| `29` | レベル２土砂災害注意報 | Landslide Advisory | `landslide` | 2 | `landslide` |
| `09` | レベル３土砂災害警報 | Landslide Warning | `landslide` | 3 | `landslide` |
| `49` | レベル４土砂災害危険警報 | Landslide Urgent Warning | `landslide` | 4 | `landslide` |
| `39` | レベル５土砂災害特別警報 | Landslide Emergency Warning | `landslide` | 5 | `landslide` |
| `19` | レベル２高潮注意報 | Storm Surge Advisory | `tide` | 2 | `coastal` |
| `08` | レベル３高潮警報 | Storm Surge Warning | `tide` | 3 | `coastal` |
| `48` | レベル４高潮危険警報 | Storm Surge Urgent Warning | `tide` | 4 | `coastal` |
| `38` | レベル５高潮特別警報 | Storm Surge Emergency Warning | `tide` | 5 | `coastal` |
| `15` | 強風注意報 | Gale Advisory | `wind` | 2 | `wind` |
| `05` | 暴風警報 | Storm Warning | `wind` | 3 | `wind` |
| `35` | 暴風特別警報 | Storm Emergency Warning | `wind` | 5 | `wind` |
| `13` | 風雪注意報 | Gale and Snow Advisory | `wind_snow` | 2 | `snow-ice` |
| `02` | 暴風雪警報 | Snowstorm Warning | `wind_snow` | 3 | `snow-ice` |
| `32` | 暴風雪特別警報 | Snowstorm Emergency Warning | `wind_snow` | 5 | `snow-ice` |
| `12` | 大雪注意報 | Heavy Snow Advisory | `snow` | 2 | `snow-ice` |
| `06` | 大雪警報 | Heavy Snow Warning | `snow` | 3 | `snow-ice` |
| `36` | 大雪特別警報 | Heavy Snow Emergency Warning | `snow` | 5 | `snow-ice` |
| `16` | 波浪注意報 | High Wave Advisory | `wave` | 2 | `marine` |
| `07` | 波浪警報 | High Wave Warning | `wave` | 3 | `marine` |
| `37` | 波浪特別警報 | High Wave Emergency Warning | `wave` | 5 | `marine` |
| `14` | 雷注意報 | Thunder Storm Advisory | `thunder` | 2 | `thunderstorm` |
| `17` | 融雪注意報 | Snow Melting Advisory | `snow_melting` | 2 | `flood` |
| `20` | 濃霧注意報 | Dense Fog Advisory | `fog` | 2 | `fog` |
| `21` | 乾燥注意報 | Dry Air Advisory | `dry` | 2 | `fire` |
| `22` | なだれ注意報 | Avalanche Advisory | `avalanche` | 2 | `snow-ice` |
| `23` | 低温注意報 | Low Temperature Advisory | `cold` | 2 | `cold` |
| `24` | 霜注意報 | Frost Advisory | `frost` | 2 | `cold` |
| `25` | 着氷注意報 | Ice Accretion Advisory | `ice_accretion` | 2 | `snow-ice` |
| `26` | 着雪注意報 | Snow Accretion Advisory | `snow_accretion` | 2 | `snow-ice` |

Wind, snow and wave have no 危険警報; the 土砂災害危険警報 (code 49) is
what was the 土砂災害警戒情報 before May 2026. The designated-river flood
warnings (氾濫注意報 … 氾濫特別警報, codes 20–22, 30–31, 40–41, 51, 53 in
their own numbering) are published in a separate document and are not in
v1.

### 3.3 ECCC

Level by the MSC risk colour where there is one, else by the alert type:

| colour | level | | type, no colour | level |
|---|---|---|---|---|
| yellow | 2 | | statement | 1 |
| orange | 3 | | advisory, watch | 2 |
| red | 4 | | warning | 3 |

ECCC's own colour is the level, so a yellow warning is level 2: MSC uses
yellow for its moderate-impact warnings, and the colour is the risk it
states. Category by the subject of the alert's short English name
(`alert_short_name_en` less a trailing parenthesis, lower case:
`Fog (advisory)` → `fog`):

| category | ECCC subjects |
|---|---|
| `rain` | rainfall |
| `wind` | hurricane, les suetes wind, les suêtes wind, tropical storm, wind, wreckhouse wind |
| `thunderstorm` | severe thunderstorm, tornado |
| `snow-ice` | blizzard, blowing snow, freezing drizzle, freezing rain, snow squall, snowfall, winter storm |
| `heat` | heat |
| `cold` | arctic outflow, extreme cold, flash freeze, frost |
| `fog` | fog |
| `dust` | dust storm |
| `air` | air quality, smoke |
| `coastal` | coastal flooding, storm surge |
| `tsunami` | tsunami |
| `other` | weather |

## 4. Identity

An alert is `<network>:<the network's own identifier>`: the CAP
identifier for NWS (`nws:urn:oid:2.49.0.1.840.0.…`), the feature id for
ECCC (`eccc:<CAP identifier>_<region>`), and `jma:<office>-<kind>` for
JMA, whose map has no per-alert identifier (`jma:130000-10` is Tokyo's
レベル２大雨注意報). The network id is lowercase,
`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`; the rest is the upstream's identifier
unchanged and may itself contain `:`. The id is the key everywhere: the
index's rows are sorted by it, each network file's lines are sorted by
it, and every line repeats it so a slice read by range identifies itself.

An upstream that replaces an alert under a new identifier (an NWS
`Update`; a JMA kind raised from 注意報 to 警報, or a 危険警報 lowered to
注意報, each a new kind code) produces a new id; the old one leaves the
snapshot and is listed in `ended[]`. Where the network says which alert
the new one replaces, the new line names it in `replaces` (§6) and the
old one ends as `replaced`, naming its successor (§7), so a reader can
chain an event's messages across rounds.

## 5. `index.json`

```json
{
  "schemaVersion": 1,
  "issued": "2026-11-01T00:05:00Z",
  "generated": "2026-11-01T00:06:10Z",
  "networks": [
    {"id": "nws", "name": "US National Weather Service",
     "attribution": "US National Weather Service", "license": "public-domain",
     "licenseUrl": "https://www.weather.gov/disclaimer",
     "url": "https://www.weather.gov/", "latest": "2026-11-01T00:06:00Z",
     "file": {"path": "nws.jsonl", "byteLength": 578054, "crc32": "0a1b2c3d"},
     "areas": {"set": "nws-zones", "version": "20260416",
               "path": "../alerts-areas/nws-zones.20260416.pmtiles",
               "byteLength": 8907673, "crc32": "3f5d3096",
               "layers": ["zone", "county", "marine", "offshore", "fire"],
               "bbox": {"path": "../alerts-areas/nws-zones.20260416.bbox.json",
                        "byteLength": 524181, "crc32": "e6ac20a5"}}},
    {"id": "jma", …, "areas": {"set": "jma-class20", "version": "20260226", …}},
    {"id": "eccc", …, "areas": null}
  ],
  "categories": ["rain", "flood", "landslide", "wind", "thunderstorm", "snow-ice", "heat", "cold",
                 "fog", "dust", "fire", "air", "coastal", "marine", "tsunami", "other"],
  "mapping": {"tables": "ebaf5daa"},
  "alerts": [
    ["nws:urn:oid:2.49.0.1.840.0.f65e…002.1", 0, "marine", 2, "Small Craft Advisory",
     "2026-10-31T23:40:00Z", "2026-11-01T00:00:00Z", "2026-11-01T12:00:00Z",
     null, null, null, null, 2671, 1662]
  ],
  "ended": [["nws:urn:oid:2.49.0.1.840.0.cad4…001.1", 0, "gone"]],
  "sources": [ … ],
  "builder": {"name": "xuebuild", "revision": "3dae963…"}
}
```

- `issued` is the round, `generated` when the build ran.
- `networks[]` is one entry per network that has a file this round
  (§7), in the publisher's order (nws, jma, eccc): its id and display
  `name`; the `attribution` a display must show, the `license` the
  network's data is reused under, `licenseUrl`, the page stating those
  terms (optional), and the network's `url`; `latest`, the
  upstream's own time of the state the file holds (NWS `updated`, JMA
  `latestControlDatetime`, ECCC `timeStamp`; on a carry-over, the previous
  round's); `file`, the network file's name (always `<id>.jsonl`), length
  and CRC32 — an empty file when nothing is in force; and `areas`, the
  boundary set the network's area codes resolve against (§9), or `null`.
  `areas.path` is relative to the index and always
  `../alerts-areas/<set>.<version>.pmtiles`; `layers` are the layer names
  in that file; `bbox` (optional) is the set's box file (§9),
  `../alerts-areas/<set>.<version>.bbox.json`, with its length and CRC32.
- `categories` is §2's list, in that order.
- `mapping` (optional) is `{"tables": <crc32>}`, the CRC32 of the bytes of
  `xuebuild/alerts/tables.json`, the mapping (§3) that filed this round's
  categories and levels: two rounds with the same value were mapped
  alike, and a reader that wants the table itself reads it at the commit
  `builder` names.
- `alerts[]` is one compact row per alert in force, sorted by id and
  unique:

  | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8–11 | 12, 13 |
  |---|---|---|---|---|---|---|---|---|---|
  | `id` | `network` | `category` | `level` | `event` | `sent` | `onset` | `expires` | `west`, `south`, `east`, `north` | `offset`, `length` |

  `network` is the index into `networks[]`, and the id's prefix is that
  network's id. `event` is the network's event name in its primary
  language (English for NWS and ECCC, Japanese for JMA) — the line's
  `native.event`. `sent` is when the network issued the alert's current
  message; `onset` when the hazard begins and `expires` when the alert
  stops being in force, ISO 8601 UTC, or `null` where the network states
  none (JMA states neither: a Japanese warning is in force until lifted).
  The four bounds are the bounding box of the line's `geometry` to four
  decimals (plain minimum and maximum of the coordinates); for an alert
  with no polygon of its own, the union of its `areas` codes' boxes from
  the network's boundary set (§9); four nulls when it has neither — no
  polygon and no boundary set, or a code the set does not hold (a box
  that left part of the area out would hide the alert from a reader
  asking about that part). Like any plain minimum and maximum, the box of
  an area that crosses the antimeridian spans every longitude. `offset` and `length` are the
  byte span of the alert's line in its network's file, the JSON object
  alone; the spans of one network's alerts are in row order, do not
  overlap, and lie inside that file's `byteLength`.
- `ended[]` is §7.
- `sources[]` is §8.
- `builder` (optional) is `{"name": "xuebuild", "revision": <commit>}`,
  the commit the round was built from (`XUE_BUILD_REVISION`, which the
  publisher sets), or `null` for a build that was not told (an offline
  build or the golden, which stays reproducible that way).

The index is what a map layer reads, and what a point or box query
filters on: every alert's category, level, times and box, about 90 KB (11 KB gzipped) for the 431 alerts in force on
2026-10-31 15:20 UTC, and no text.

## 6. `<network>.jsonl`

One line per alert of that network in force, sorted by id, each the
compact JSON object below followed by `\n`:

```
{"id": "nws:urn:oid:…", "network": "nws", "category": "marine", "level": 2,
 "native": {"event": "Small Craft Advisory", "code": "SCY", "level": "Minor", …},
 "sent": "2026-10-31T23:40:00Z", "onset": "2026-11-01T00:00:00Z" | null,
 "expires": "2026-11-01T12:00:00Z" | null, "status": "actual",
 "texts": {"en": {"headline": "…", "description": "…", "instruction": "…", "area": "…"}},
 "areas": ["ANZ639"] | [],
 "geometry": {"type": "MultiPolygon", "coordinates": …} | null,
 "url": "https://api.weather.gov/alerts/urn:oid:…" | null,
 "sender": "NWS Wakefield VA" | null,
 "replaces": ["nws:urn:oid:…"]}
```

- `network` is the network's id (the row carries its position instead).
- `native` is the upstream's own classification, verbatim: `event` (the
  event name, always), `code` (the network's event code, or null) and
  `level` (the network's own severity or level, or null), and the
  network's further fields listed in its section (§10). A reader that
  wants the network's own words reads these; the product's `category` and
  `level` are derived from them by §3.
- `status` is always `actual`: exercises, tests and drafts are not
  published.
- `texts` maps BCP 47 language tags to that language's texts, every
  language the network publishes, in full — never truncated. Each holds
  the fields the network has of `headline`, `description`,
  `instruction` and `area` (the network's own description of the area,
  e.g. NWS `areaDesc`); a field the network leaves empty is absent.
- `areas` lists the area codes the alert is in force in, in the
  network's code space, which the network's boundary set (§9) draws;
  empty for a network whose alerts carry their own polygons.
- `geometry` is the alert's own polygon where the network draws one
  (NWS storm-based warnings, every ECCC alert), simplified (§9), else
  `null`. Where both are given, the polygon is the warned area and
  `areas` the zones it touches.
- `url` is the alert's own page or document at the network, `sender` the
  issuing office, each `null` where the network has none.
- `replaces` (optional, after `sender`) lists, sorted, the ids of the
  same network's earlier alerts this one supersedes, where the network
  says so: NWS CAP `references` on any message but a `Cancel`, JMA the
  office's 危険警報 a 注意報 was lowered from. Absent when there are none
  (and for ECCC, whose collection carries no references). A replaced
  alert may already have left the snapshot rounds ago; only one that
  leaves in this round is listed in `ended[]` as `replaced`.

## 7. `ended[]` and carry-over

`ended[]` lists the alerts that were in the previous round's index and are
not in this one, sorted by id: `[id, network, reason]`, or
`[id, network, "replaced", by]`, `network` being the position in *this*
round's `networks[]`. `reason` is the first of these that holds:

- `expired` — the alert's `expires` is at or before this round's
  `issued` (whether the network still listed it or not);
- `replaced` — an alert in force this round names it in `replaces` (§6);
  `by` is that alert's id (of several, the first by id), always one of
  this round's rows;
- `cancelled` — the network withdrew it in so many words this round: an
  NWS `Cancel` message referencing it, a JMA kind whose status is 解除, an
  ECCC feature whose status is `ended` or `cancelled`;
- `gone` — anything else: it is simply no longer listed (a JMA kind
  raised to another code, the network dropped it without a word).

The four-element form is the only one with a fourth element; a reader
that predates `replaced` reads the first three.

`ended[]` is kept for one round. A reader that wants to animate a
warning's end compares nothing; a reader that missed a round has missed
those ends, and reads the snapshot.

A network that does not deliver a new state — its fetch failed, its
document did not parse, JMA's `map_time.json` is stale (§10.2), or JMA
says nothing changed — **carries over**: its file is the previous round's
lines, byte for byte, less those that have expired by this round, and its
`latest` stays the previous round's. Its `sources[]` entry says why (the
"nothing changed" case stays `ok: true`, with `unchanged: true`). A
carry-over has no end but its expiries, so a network that stays down shows
its last known warnings with an old `latest` and a failed source, rather
than none. A network with nothing to carry over (no previous round, or a
previous file that cannot be read) is left out of the round.

## 8. `sources[]`

One entry per source, every network's in turn, in build order:

```json
{"id": "nws-alerts", "ok": true, "fetched": "2026-11-01T00:06:00Z",
 "url": "https://api.weather.gov/alerts/active?status=actual", "reports": 380, "skipped": 0}
{"id": "jma-warning", "ok": false, "fetched": "2026-11-01T00:06:01Z",
 "url": "https://www.jma.go.jp/bosai/warning/data/r8/map.json",
 "error": "map_time.json has not advanced since 2026-10-31T23:30:00Z", "mapTime": "2026-10-31T23:30:00Z"}
{"id": "eccc-alerts", "ok": true, "fetched": "2026-11-01T00:06:03Z",
 "url": "https://api.weather.gc.ca/collections/weather-alerts/items?f=json&limit=1000",
 "pages": 1, "reports": 5, "skipped": 0}
```

`ok: false` always carries `error`. An `ok` source carries `sha256`
(optional), the SHA-256 of the raw bytes the network was read from, in
the order they were fetched: NWS `active.json`; JMA `map_time.json`, then
`map.json` when it was fetched; ECCC every page in turn. A reader holding
the upstream answer can tell whether a round was built from it.
`reports` is how many upstream
records the source held (features, JMA office reports); `skipped` how
many of them were not published — a test or a cancel message, an ended
ECCC feature, a JMA report from an office or a kind code the tables do
not know. A network's own fields are described in its section.

## 9. Geometry and boundary sets

**Inline polygons.** An upstream polygon (GeoJSON Polygon or
MultiPolygon) becomes a MultiPolygon: Douglas–Peucker at 0.001° (about
100 m, under a pixel at the zooms a warning is read at), coordinates
rounded to four decimals, positions the rounding made repeat dropped,
rings closed. A sliver that simplification would collapse keeps its
rounded, unsimplified ring; a hole that collapses is dropped; a polygon
with no area left is dropped, and an alert with none left has `geometry:
null`. Pure NumPy (`xuebuild/alerts/geometry.py`), so the build has no
GEOS dependency.

**Boundary sets.** A network that names areas by code rather than drawing
them has a boundary set: one PMTiles file of vector tiles per upstream
release, each feature carrying one property, `id`, the code a line's
`areas` lists. Several features may share an id (an island county).
A display joins a round's codes to the set — e.g. MapLibre with
`promoteId: "id"` and feature state, or a filter on `["get", "id"]` —
reading only the tiles in view by HTTP range. A set is named
`<set>.<version>` by the release's effective date; a new release is a new
file and a new pin in the network table, and older rounds keep naming the
file they were built against, so a round and its boundaries never
disagree.

Beside each set, `<set>.<version>.bbox.json` holds every code's box:

```json
{"set": "nws-zones", "version": "20260416",
 "layers": {"zone": {"AKZ317": [-143.8934, 58.8091, -137.4999, 60.5219], …}, "county": {…}, …}}
```

one object per layer in the set's order, codes sorted, each box
`[west, south, east, north]` over all of the code's features as released
(reprojected, not simplified), rounded outward to four decimals so it
holds the area. It is pinned by length and CRC32 in the network table
like the tiles. The build reads it from the raw directory's
`alerts-areas/`, fetching it once from the public data origin under
`?v=<crc32>` when it is missing there; a build that cannot have it (an
offline build without it, the origin down) leaves the boxes of the rows
that need it null and publishes anyway. How a network's codes resolve
against the layers is in its section (§9.1, §9.2).

Sets are built by hand, not by the five-minute build:

```sh
xue alerts-areas --set nws-zones --source <the downloaded release> --version 20260416 \
  --output web/public/data/alerts-areas
make upload-r2-alerts-areas SET=nws-zones VERSION=20260416
```

The tool runs `ogr2ogr` (reproject to EPSG:4326, simplify by 0.0005°,
keep the code attributes, five-decimal GeoJSONSeq) and `tippecanoe`
(`--simplification=10 --low-detail=11 --detect-shared-borders`, no tile
size or feature limit, tiny polygons kept so every code can be hit), with
a Python pass between them that builds each feature's `id` and holds the
county and fire-zone layers back to z6. tippecanoe runs on relative names
inside a scratch directory, so the same release builds the same bytes and
the pinned CRC32 can be reproduced. It writes the box file beside the tiles
and prints both files' lengths and CRC32s for the network table
(`xuebuild/alerts/networks.py`); the upload sends both and refuses to
replace an existing object of a different length.

Low-zoom thinning is tuned to two view budgets, measured with
`pmtiles extract <set> <out> --bbox=… --minzoom=z --maxzoom=z` (the size
of the extract, header and directories included):

| view | budget | measured |
|---|---|---|
| CONUS z4, `-125,24,-66,50` | ≤ 300 KB | 202 501 bytes |
| all Japan z5, `122,24,146,46` | ≤ 200 KB | 117 335 bytes |

Untuned (`--detect-shared-borders` alone) the same views were 481 435 and
217 526 bytes and the files 20.6 MB and 5.1 MB; `--low-detail=10` brought
CONUS z4 to 160 607 bytes at visibly coarser coastlines.

### 9.1 `nws-zones`

The AWIPS shapefiles of `https://www.weather.gov/gis/`, which post the
current and the next release side by side under their effective date:

| layer | release | id | zooms | features | codes |
|---|---|---|---|---|---|
| `zone` | public forecast zones `z_16ap26` | `STATE` + `Z` + `ZONE` (`OKZ025`) | 0–9 | 4 157 | 4 080 |
| `county` | counties `c_16ap26` | `STATE` + `C` + last three digits of `FIPS` (`OKC109`) | 6–9 | 3 352 | 3 269 |
| `marine` | coastal marine zones `mz16ap26` | `ID` (`ANZ639`) | 0–9 | 569 | 569 |
| `offshore` | offshore marine zones `oz16ap26` | `ID` (`PZZ900`) | 0–9 | 130 | 130 |
| `fire` | fire weather zones `fz16ap26` | `STATE` + `Z` + `ZONE` | 6–9 | 3 683 | 3 603 |

`nws-zones.20260416.pmtiles`: 8 907 673 bytes, CRC32 `3f5d3096`, built
in 21 s; `nws-zones.20260416.bbox.json`: 524 181 bytes, CRC32
`e6ac20a5`. Other views: CONUS z3 169 278 bytes, the southern Plains at
z6 (`-104,33,-94,40`) 71 571, a 1° box around Oklahoma City at z9 5 609.

Fire weather zones share the public zones' `Z` code space but not their
shapes: a code resolves against the `fire` layer for the fire-weather
events (`Red Flag Warning`, `Fire Weather Watch`, `Extreme Fire Danger`,
`Fire Warning`) and against `zone`, `county`, `marine` and `offshore`
otherwise (the four do not share codes). The high seas areas (`hz…`)
are not UGC-coded and not in the set. Boxes resolve the same way: a
fire-weather event's codes against `fire`, any other's against `zone`,
`county`, `marine` and `offshore` in that order. Three marine zones and an
Aleutian county cross the antimeridian, so their boxes span every
longitude.

### 9.2 `jma-class20`

The agency's 予報区等GISデータ (`https://www.data.jma.go.jp/developer/gis.html`),
市町村等（気象警報等）, release `20260226_AreaInformationCity_weather_GIS`:
one layer, `class20`, id `regioncode` (seven digits, the class20 codes of
the warning map), zooms 0–10, 1 805 features and codes, read as JGD2011
(EPSG:6668; the release ships without a `.prj`).
`jma-class20.20260226.pmtiles`: 2 795 153 bytes, CRC32 `43b3b6eb`, built
in 15 s; `jma-class20.20260226.bbox.json`: 82 289 bytes, CRC32
`c361aa9d`. Other views: all Japan z4 83 439 bytes, Kantō at z8
(`138.5,35,140.5,36.5`) 52 003, central Tokyo at z10 32 221. A line's
codes resolve against `class20`.

Terms. The release is reused under the agency's website terms (the
Public Data License v1.0,
<https://www.jma.go.jp/jma/kishou/info/coment.html>). Wherever the set or
a map drawn from it is shown or redistributed it carries:

- 出典：気象庁「予報区等GISデータ」(<https://www.data.jma.go.jp/developer/gis.html>),
  公共データ利用規約 第1.0版 (Public Data License 1.0,
  <https://www.digital.go.jp/resources/open_data/public_data_license_v1.0>);
- 加工：簡略化・PMTiles 化 by xue — the set is simplified and converted to
  vector tiles by xue, and is not published by the Japan Meteorological
  Agency;
- the agency's notice of the Geospatial Information Authority of Japan's
  approval, as its GIS page gives it: この地図の作成に当たっては、国土地理院長の承認を得て、同院発行の数値地図25000（空間データ基盤）、数値地図（国土基本情報）、電子国土基本図（地図情報）を使用した。（承認番号　平30情使 第401号・令２情使 第125号・令３情使 第90号・令３情使 第123号・令３情使 第648号・令４情使 第437号・令５情使 第71号・令６情使 第382号・令７情使 第395号）

## 10. Networks

### 10.1 `nws` — US National Weather Service

Every alert the US National Weather Service has in force, from
`nws-alerts` — `https://api.weather.gov/alerts/active?status=actual`, one
GeoJSON FeatureCollection of CAP 1.2 alerts nationwide (about 1.7 MB,
115 KB gzipped, on 2026-10-31). The service requires a User-Agent that
says who is asking and how to reach them; every request carries
`xue/<version> (+https://github.com/ringsaturn/xue)`. No rate limit is
published; one request every five minutes is far inside what it expects.

Reading:

- A feature whose `status` is not `Actual`, or whose `messageType` is
  `Cancel`, is not in force. The alerts a `Cancel` references end as
  `cancelled`; those any other message references (an `Update`'s
  predecessor) are its `replaces` and end as `replaced`.
- `expires` is CAP `ends` (when the event ends) where the alert states
  one, else CAP `expires`. The API keeps listing an alert until its event
  ends, long after its last message expired — a third of the alerts on
  2026-10-31 were marine and flood alerts whose message had expired days
  earlier — so filtering on the message's expiry would drop alerts the
  service itself shows as in force. The message's own expiry is in
  `native.expires`.
- `onset` is CAP `onset`, else `effective`.
- `native`: `event`; `code`, the NWS event code (`eventCode.NationalWeatherService`,
  e.g. `SCY`, `TOW`); `level`, CAP `severity`; `urgency`; `certainty`;
  `vtec`, the first P-VTEC string; `expires`, the message's expiry.
- `texts.en`: `headline`, `description`, `instruction`, and `area` (CAP
  `areaDesc`).
- `areas`: `geocode.UGC`, in the service's order — public zones, counties
  and marine zones, drawn by `nws-zones` (§9.1).
- `geometry`: the storm-based polygon where there is one.
- `url`: the alert's API document (`@id`); `sender`: `senderName`.

`latest` is the collection's `updated`.

Terms. Works of the US federal government; the API's documentation
describes its data as open data, free to use for any purpose. `license`
`public-domain`, `licenseUrl` `https://www.weather.gov/disclaimer`,
attribution `US National Weather Service`, url `https://www.weather.gov/`.

### 10.2 `jma` — Japan Meteorological Agency

The agency's warnings and advisories (気象警報・注意報) as its own warning
map reads them; there is no published API and no service commitment.
`jma-warning`:

- `https://www.jma.go.jp/bosai/warning/data/r8/map_time.json` —
  `{"latestControlDatetime": …}`, the time of the newest report. Recorded
  as `mapTime`.
- `https://www.jma.go.jp/bosai/warning/data/r8/map.json` — every forecast
  office's latest report per element group, nationwide (about 900 KB,
  40 KB gzipped). Not fetched when `mapTime` equals the network's `latest`
  in the previous round: the source is then `ok` with `unchanged: true`
  and the previous lines carry over (§7).

The `r8` documents follow the warning system in force since late May 2026
(レベル２注意報 … レベル５特別警報, with the new レベル４危険警報); the
earlier `bosai/warning/data/warning/` documents stopped updating on
2026-05-28.

Health. If `mapTime` is more than six hours before the fetch, the source
is `ok: false` with the error `map_time.json has not advanced since …` and
the network carries over: the map has moved once already, and a frozen
map must not pass for a calm country. The map only moves when a report
changes, and quiet nights leave it still for well over half an hour, so
the threshold is hours, not minutes.

Reading. A report lists, for each municipality-level area (class20,
seven digits) of its office, the kinds in it with a status: 発表
(issued), 継続 (continued), 解除 (lifted), 危険警報から注意報 (a 危険警報
lowered to 注意報) or 発表警報・注意報はなし (none). A kind is in force in
an area when it has a code and its status is anything but 解除; a kind
whose status is 危険警報から注意報 is the 注意報 now in force, and its line
`replaces` the office's 危険警報 for the element (unless that is still in
force elsewhere in the office). One alert
is one (office, kind) in force anywhere, with every area it is in force
in:

- `id` `jma:<office>-<kind>`, the office being the forecast office's area
  code (`130000` Tokyo), matched by the report's `publishingOffice` name
  in `xuebuild/alerts/jma_offices.json` — the `offices` level of the
  agency's `bosai/common/const/area.json`, 58 entries, committed. A report
  from an office the table does not know is skipped and counted.
- `event` and `native.event`: the agency's own name with its レベル
  prefix where it has one (`レベル２大雨注意報`, `雷注意報`); `native.code`
  the kind code; `native.level` the page's level number (`20`, `30`,
  `40`, `50`); `native.element` the page's element name (`rain`,
  `landslide`, …).
- `sent`: the report's `reportDatetime`; `onset` and `expires`: `null`.
- `texts.ja.headline`: the report's `headlineText`, the only text the
  map carries (it covers the office's whole report, not this kind alone).
- `areas`: the class20 codes, sorted; drawn by `jma-class20` (§9.2).
- `url`: the office's page on the warning map; `sender`: the
  `publishingOffice`.

`latest` is `mapTime`.

Terms. The agency's website content is reusable, commercially included,
under the Public Data License v1.0 with the source credited —
「出典：気象庁ホームページ」 — and, for edited content, a statement that it
was edited; it must not be presented as the agency's own publication
(<https://www.jma.go.jp/jma/kishou/info/coment.html>). The lines are
edited content (the agency's map reorganised by office and kind). The
boundary set the network names carries its own credit and the survey
approval notice (§9.2).
`license` `PDL-1.0`, `licenseUrl`
`https://www.jma.go.jp/jma/kishou/info/coment.html`, attribution
`出典：気象庁ホームページ (Japan Meteorological Agency)`, url
`https://www.jma.go.jp/bosai/warning/`.

### 10.3 `eccc` — Environment and Climate Change Canada

Environment and Climate Change Canada's public weather alerts, from
`eccc-alerts` — the MSC GeoMet OGC API collection
`https://api.weather.gc.ca/collections/weather-alerts/items?f=json&limit=1000`,
following `next` links if there are any (`pages`). The collection is every
alert in force nationwide, one feature per alert and forecast region,
each with the region's polygon and English and French texts.

Reading:

- One feature is one alert, `id` `eccc:<feature id>` (the CAP identifier
  and the region), so the texts and the polygon stay together. A feature
  whose `status_en` is `ended` or `cancelled` is not in force and ends as
  `cancelled`.
- `event` and `native.event`: `alert_name_en` (`fog advisory`).
- `native`: `code` `alert_code` (`FGA`), `level` `risk_colour_en`, `type`
  `alert_type`, `impact` `impact_en`, `confidence` `confidence_en`,
  `status` `status_en`, `province`, `ends` `event_end_datetime`.
- `sent` `publication_datetime`, `onset` `validity_datetime`, `expires`
  `expiration_datetime`.
- `texts.en` and `texts.fr`: `headline` (`alert_name_*`), `description`
  (`alert_text_*`), `area` (`feature_name_*`).
- `areas` is empty and `geometry` the region's polygon: every alert draws
  its own area, so the network has no boundary set (`areas: null`).
- `url` and `sender`: `null`; no `replaces`, since the collection's
  features carry no references to earlier alerts.

`latest` is the collection's `timeStamp`.

Terms. Open Government Licence – Canada
(<https://open.canada.ca/en/open-government-licence-canada>). `license`
`OGL-Canada-2.0`, `licenseUrl` the same page, attribution
`Contains information licensed under the Open Government Licence – Canada (Environment and Climate Change Canada)`,
url `https://weather.gc.ca/`.

## 11. Validation

`xuebuild/alerts/schema.py` validates every file as it is written, and
`tests/test_alerts.py` holds two consecutive rounds built from the fetched
fixture (`tests/fixtures/alerts/alerts.202611010000/` and `…0005/`, with
the boundary sets' box files in `alerts-areas/`) to the
committed golden (`tests/fixtures/alerts/expected/`), regenerated by
`tests/prepare_alerts_golden.py`. The second round exercises every
`ended[]` reason (an NWS `Update` for `replaced`), an NWS cancel, a stale JMA map carried over and a failed
ECCC fetch carried over with an expiry; every index row's span is checked
against the network file it addresses. Admission is structural: a
network, an event or a native field a reader has never seen is not an
error; the categories are the index's own list, the levels 1 … 5, the
times ISO 8601 UTC, the geometry closed four-decimal rings. A reader
validates the same shapes on read and rejects a `schemaVersion` above the
one it implements.

Sizes on a calm day (2026-10-31 15:20 UTC): `nws.jsonl` 380 alerts,
578 KB (61 KB gzipped); `jma.jsonl` 46, 33 KB (3 KB); `eccc.jsonl` 5,
31 KB (10 KB); `index.json` 89 KB (11 KB). A stormy day is five to ten
times that, mostly NWS text and polygons.

## 12. The archive

A round is a snapshot that lives three hours. The archive is the record:
every alert that was in force in some round of a UTC month, once, as the
last version of it a round of that month published, with when it ended
and why. It is a separate tree under the data root, written by the
publisher (`xue alerts-archive`) and never pruned.

### 12.1 Layout

```
alerts/archive/catalog.json               the one mutable name: every month
alerts/archive/<YYYY>/<MM>/alerts.jsonl   the month's alerts, one per line, sorted by id
alerts/archive/<YYYY>/<MM>/index.json     every alert's row and byte span in that file
```

- `catalog.json` is requested with `Cache-Control: no-cache`, as the
  pointer is (§1). Each entry names a month's index by `path` (relative to
  the catalog), `byteLength` and `crc32`; the index names its `.jsonl` the
  same way. Every file but the catalog is requested as
  `<path>?v=<crc32>`.
- A month is the UTC month of a round. An alert in force across the turn
  of a month is in both months' files, each holding the version that
  month last published.
- The month in progress is rewritten in place at most once an hour (the
  first round of each hour folds in the rounds before it), and its CRC32
  changes with it; a month is final once the first hour of the next has
  been folded in. Nothing in the files records when they were built: the
  same records give the same bytes.

### 12.2 Records

A line of `alerts.jsonl`:

```json
{"id": "nws:urn:oid:…", "network": "nws", "first": "2026-11-01T00:05:00Z",
 "ended": {"time": "2026-11-01T03:10:00Z", "reason": "replaced", "by": "nws:urn:oid:…"},
 "alert": { … }}
```

- `id` and `network` are the alert's (§4), repeated so a slice read by
  range identifies itself.
- `alert` is the alert's line (§6) as the month's last round that listed
  it published it, byte for byte the same object: a later version under
  the same id replaces an earlier one.
- `first` is the first round of the month whose snapshot listed the alert
  (its `issued`): for an alert carried in from the month before, the
  month's first round.
- `ended` is `null` while the alert was still in force at the month's
  last round (or, for the month in progress, at the last round folded in);
  else `time`, the round whose `ended[]` (§7) listed it, `reason`, and
  `by` for `replaced`, as that entry gave them. An alert listed again
  after it ended (a network carried over again) is in force once more and
  its `ended` is `null`. An end found in the first round of a month is
  recorded in the month the alert was last in force.

### 12.3 Indexes

A month's `index.json`:

```json
{"archiveSchemaVersion": 1, "product": "alerts", "month": "2026-11",
 "alerts": {"path": "alerts.jsonl", "byteLength": 41234567, "crc32": "…"},
 "networks": ["eccc", "jma", "nws"],
 "rows": [["nws:urn:oid:…", 2, "marine", 2, "Small Craft Advisory",
           "2026-11-01T00:05:00Z", "2026-11-01T03:10:00Z", "replaced", 0, 1450]]}
```

`networks` are the ids of the networks the month holds, sorted. A row is
`[id, network, category, level, event, first, ended, reason, offset,
length]`: `network` the position in `networks`, `category`, `level` and
`event` the alert's (§5), `first` the record's, `ended` and `reason` its
`ended.time` and `ended.reason` or two nulls, and `offset` and `length`
the byte span of the record's JSON object in `alerts.jsonl`, without the
newline. Rows are sorted by id and unique and their spans tile the file
in order.

`catalog.json`:

```json
{"archiveSchemaVersion": 1, "product": "alerts",
 "months": [{"month": "2026-11", "path": "2026/11/index.json", "byteLength": 1234567, "crc32": "…", "alerts": 23456}]}
```

`months` are sorted and unique. A reader rejects an `archiveSchemaVersion`
above the one it implements.

### 12.4 The producer

The rounds on the bucket last three hours, so the archive is fed by the
publisher as it goes, from the state it keeps on its own disk:

- After each round is published, `xue alerts-archive --round <round>`
  compares the round with the one before it (the live round the build
  measured `ended[]` against): every line that is new or whose bytes
  changed, and every `ended[]` entry, is appended to a journal in the
  publisher's state directory. A round with no round before it in the
  same month journals every line.
- When the journal holds a round of an earlier hour than the current one
  (or with `--compact`), it is folded into the month files under the
  state directory, in journal order: a line replaces the record's `alert`
  and clears its `ended`, `first` stays the earliest, an end sets
  `ended`. The month file is merged as a stream, so the journal, not the
  month, bounds the memory it takes. The files that differ from what the
  published catalog names are listed in the report, each before the file
  naming it, the catalog last, and uploaded in that order
  (`make upload-r2-alerts-archive`); a failed upload is listed again by
  the next fold.
- A month the state directory lacks but the published catalog names is
  pulled from the data origin first (CRC32 checked), so a new host or a
  lost disk continues the published record rather than restarting it. A
  catalog the origin cannot serve (other than not existing) stops the
  fold, and the journal waits for the next one.
- Months before the previous one that the published catalog already
  holds as they are on disk are removed from the state directory.

What a reader cannot learn from the archive: the versions of an alert
between the first and the last of a month (an NWS `Update` is a new id
and so a new record; a change under the same id keeps only the last
version), and anything that happened in a round the publisher missed.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).

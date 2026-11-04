# Point products

Seven products sit beside the gridded runs: tropical cyclone tracks
(`xuebuild/tc/`), airport METAR/TAF (`xuebuild/airport/`), radiosonde
soundings (`xuebuild/sounding/`), surface stations (`xuebuild/synop/`),
air-quality stations (`xuebuild/aq/`), weather warnings
(`xuebuild/alerts/`) and active fires (`xuebuild/fire/`). Schemas are
normative in [../tc.md](../tc.md), [../airport.md](../airport.md),
[../sounding.md](../sounding.md), [../synop.md](../synop.md),
[../aq.md](../aq.md), [../alerts.md](../alerts.md) and
[../fire.md](../fire.md); STAC in [../stac.md](../stac.md).

## Shared contract

- **Pointer → index → payload.** `latest-<product>.json` (read no-cache) is
  the only mutable object; it names an immutable
  `<product>.<issue>/index.json`. Airport and sounding issues are two
  objects: the index and one JSONL (one line per station, sorted by id)
  whose `offset` / `length` the index rows carry, so a browser reads one
  station with one Range request and an analyst streams one file. The tc
  index names one `<storm id>.json` per system. A synop round has one
  JSONL and one stations file per network, so a network's files stay the
  size of a network and a map loads only the networks it shows (aq has
  the same shape); an alerts round one JSONL per warning service, one
  warning per line, with area codes resolved against boundary sets
  published once per upstream release under `alerts-areas/` (PMTiles for
  drawing, a `.bbox.json` of every code's box that gives an area-coded
  warning its index box) and a monthly archive under `alerts/archive/`
  that the publisher feeds from a journal in its state directory, since
  the rounds last three hours; a
  fire round one JSONL per sensor with one line per UTC hour, so the index
  addresses hours rather than stations.
- **One object per issue, never per station**: many small files hurt bucket
  traversal and analysis.
- **The previous issue is input.** `make live-<product>-index` runs before
  each build: tc carries identities forward, airport, synop and fire merge history,
  sounding carries forward stations without a new ascent (after checking
  the JSONL's CRC32).
- **Sources fail alone** into `sources[]`; the pointer is withheld only when
  nothing contributed, so the last good issue stays live.
- **One skeleton, in `xuebuild/pointproduct.py`.** A minute-round product
  (airport, synop, aq, alerts, fire, nexrad) binds
  `RoundProduct(PRODUCT, <its error class>, cadence_minutes=…, archived=…)`
  in its `schema.py` and re-exports its methods under the module-level
  names (`parse_round`, `floor_round`, `round_directory`, `raw_directory`,
  `pointer_directories`, `local_round_directory`, `build_pointer`,
  `validate_pointer`, and the `_time` / `_number` / `_integer` /
  `_optional_*` / `_crc_file` primitives). Fire and nexrad are unarchived:
  flat round directories, a whole-hour cadence for fire. The hourly-issue
  twin (tc, sounding) binds `IssueProduct`. A product's `schema.py` keeps
  only its line validators and constants. `published_source` is the one
  way a `SourceStatus` enters an index, and `xuebuild/http.py` the one
  paced GET (custom User-Agent, 404 and, with `modified_since`, 304 as
  answers; `compressed=True` for a gzip-on-the-wire service). Network
  tables (`Network`) and station metadata (`StationMeta`) stay per
  product: their fields differ.
- **Shapes are JSON Schemas.** Each file a product publishes has one
  draft 2020-12 schema under `docs/schemas/` (`pointer.schema.json` is
  every product's pointer), referenced from its spec as the normative
  shape. The three implementations meet it differently:
  `tests/test_point_schemas.py` holds every golden to it with the
  reference `jsonschema` (a `dev` group dependency; the publishing image
  syncs `--no-dev`), so the writers' `schema.py` validators keep only
  what a schema cannot say — order, byte spans, sums, file names, the
  version gate — and refuse a file without the schema's shape through
  `pointproduct.shaped` (fire, aq and alerts; tc, sounding, airport and
  synop still check the shapes in `schema.py` too); the shell's checks are generated plain TypeScript
  (`make schemas`, `web/tooling/schemagen.ts` → `web/src/schema/generated/`,
  held to a fresh run by `tests/web/schemagen.test.ts`), and a product's
  `schema.ts` runs its version gate, the generated check, then the same
  invariants; the API worker reads the same goldens in its tests, and
  each of its readers ties the fields and columns it takes by name to the
  committed schema (`include_str!`). Positional rows name their columns in
  a `columns` array (airport and alerts indexes, aq and synop stations
  files), which the shell and the worker read by name with a positional
  fallback for files from before it. The
  generator implements a closed subset of the vocabulary (no `if`, no
  closed objects: a reader admits a field a later revision adds) and
  refuses a schema that leaves it.
- **Parsers import no other parser** and convert to SI themselves; `build.py`
  is the only place sources meet; `schema.py` validates on write.
- **Goldens.** `tests/fixtures/<product>/` is a fetched issue (two rounds
  for airport), `expected/` the output including STAC Items, regenerated by
  `tests/prepare_<product>_golden.py`. tc and sounding need `bufr_dump`.
- **Upload** payload → index → pointer (`make upload-r2-<product>`);
  `prune-r2-<product>` trims old issues.
- **Layout.** tc and sounding file an issue under its UTC day,
  `<product>/YYYY/MM/DD/<product>.<hour>/` (`pointproduct.archive_directory`,
  `point_dir` in `mk/products.mk`), so retention can grow to years without
  a flat root of thousands of prefixes; airport, synop, aq and alerts do
  the same with their minute rounds, `<product>/YYYY/MM/DD/<product>.<round>/`
  (`pointproduct.archive_round_directory`). Every validator, the API, the shell's pointer schemas, the
  STAC Item and the prune accept both layouts, nested and the flat
  `<product>.<id>/` issues were published under before. Raw fetch
  directories stay flat under `data/raw/`.

## Tropical cyclones

Parsers: `atcf.py`, `tcw.py`, `bufrtracks.py`, `ibtracs.py` → `track.py`.
`identity.py` keys on the ATCF id; invests and model-found systems get
`x-<basin>-<hour>-<n>` ids carried forward from the previous index by alias
and proximity. `web/src/tc/agencies.ts` reads `registry.py`'s agency
table out of the shell's registry (`web/src/registry.ts`).

## Airports

A round is a UTC minute floored to ten. The fetch reads the Aviation
Weather Center's three cache files and must send the custom User-Agent and
stay under a hundred requests a minute; the station table is refetched at
most daily with `If-Modified-Since`. A value outside the contract's ranges
is written null, not raised. `history.jsonl` holds each station's last 24 h
of METARs and current TAF; each round merges new reports by (station, time),
newest winning, drops what is older than 24 h, and removes stations whose
window empties. The pointer is withheld only when both METARs and TAFs
failed. `AIRPORT_KEEP` rounds are kept.

## Surface stations

- One product, many networks. `networks.py` is a fixed tuple; each network
  is an adapter module with `fetch(raw_root, round, since, …)` and
  `read(raw_root, statuses)` that converts its own format into the
  elements of `docs/synop.md` §2. Adding a network touches that tuple, a
  new module, a `docs/synop.md` §9 subsection, a fixture and the STAC prose
  — not the schema and not the shell.
- `since` is the network's `latest` in the previous index, so a round asks
  only for what is new and a gap refills as far as the network keeps data.
- The stations files' element values are each element's newest non-null
  value within the hour (accumulations only at the newest time): some
  elements arrive less often than the station reports.
- A network whose report for one time arrives in pieces (DWD's templates
  and batches, HKO's one file per element) sets `Network.partial`, and the
  merge lays new values over old element by element instead of replacing
  the time whole.
- AMeDAS reads the agency's map-page JSON (no API, no SLA); the quality
  rule and the 16-point wind code come from that page's own script. Its
  codes ride with the values under `station.QC_KEY` (not an element key)
  through the merge when `Network.quality_codes` is set; a line writes a
  `qc` column only where the codes say more than the values, and the next
  round reconstructs the omitted ones from the values.
- Each adapter's `read` stores `sha256` of the bytes it read in its
  source's status; the index adds `elementTable` and `builder`
  (`XUE_BUILD_REVISION`, set by the workflow, null offline).
- DWD is BUFR through `bufr_dump -j f`; values are picked by key plus the
  `timePeriod` / sensor height in force, never by template position, since
  one batch mixes four templates. HKO's CSVs name stations only in
  English, so `hko_stations.json` (built once from the Observatory's
  station page, aliases by hand) places them.

## Active fires

- FIRMS publishes no API worth the name for a whole-world feed; the
  last-24-hour CSVs (`<product>/csv/*_Global_24h.csv`, no key) are the
  input. The 7-day ones are 35–60 MB each and timed out once in testing,
  so the window is ours: each round merges the 24 h files onto the
  previous round's sensor files, keyed by (time, lat, lon) in the integer
  encoding, and drops what is older than seven days.
- `sensors.py` is two fixed tuples: the FIRMS files (sources) and the
  sensors they feed. MODIS is one file for Terra and Aqua, split by the
  `satellite` column; a row whose satellite is not the file's is rejected.
- `firms.py` reads numbers as `Decimal` so a 5-place coordinate lands on
  the same 1e-4 integer everywhere. MODIS confidence is classed with
  FIRMS's own thresholds (30, 80).
- The previous sensor file is used only when its CRC32 matches the
  previous index; a mismatch restarts that sensor from the 24 h file.
- Measured 2026-10-12: about 180 000 detections a day (VIIRS 55–58 k per
  satellite, MODIS 9.5 k), so the seven-day files are about 30 MB in
  total (9.5 MB gzipped) and the index a few tens of kilobytes.

## Single-site radar

- `xuebuild/nexrad/`: `level3.py` reads N0B / N0G (bzip2 packet 16) itself —
  no MetPy — and refuses any codebook but the spec's; `store.py` writes the
  polar store (`docs/zarr-profile.md`, "Polar store"); `build.py` builds a
  round from the sweeps newer than the window's newest per site and product,
  and `catch_up` fills a late or missed schedule with ordinary five-minute
  rounds, so a site's inner chunk never holds more than one round's sweeps.
- The index is the window manifest. It is rewritten every round from the
  previous one; the stores never are.
- Cases (`showcase/nexrad-cases/*.json`, `xue nexrad-case`) take the same
  rounds over a past interval but write one window store per product
  (`build_case`): a closed interval needs no per-round directories, and a
  store per round per product was ~35 objects a round. `replay` still
  builds the live layout over a past interval, for the golden.
- Level 3 N0B / N0G exist from 2022-02 on; earlier events have N0Q / N0U only.
- The live view publishes nothing (`docs/nexrad.md` §7): the shell lists
  and reads the source bucket itself, and `rust/xue/src/level3.rs` ports
  `level3.py` for it. A change to the reader changes both, and
  `tests/prepare_nexrad_golden.py` re-pins `expected/level3.json`.
  `xue nexrad-sites` rewrites the shell's site table.

## Soundings

The fetch lists the two GTS→WIS2 gateway directories of the public
`wis2globalcache` bucket and downloads only objects newer than the previous
index's per-gateway `watermark`. `bufr.py` decodes with `bufr_dump` (one
`Sounding` per subset, `-32768` missing, nominal time from `<ddhhmm>`) and
thins each ascent to the classical TEMP set (`THINNING_RATIO`, keeping the
original count as `reported`); `derive.py` runs in a fixed operation order.
Identity is the WMO number; one sounding survives per station-hour, the
longest then the latest (a correction). The workflow needs neither GDAL nor
the wheel.

## In the shell

Marks (`web/src/tc/`, `web/src/stations/`) take no session, fetch nothing
per frame and never gate the playhead; a showcase case hides them, and
`main.ts` polls their pointers with the runs.

- `stations/fetch.ts` resolves a 404 to null (a data root without the
  product costs nothing) and reads one station with
  `Range: bytes=<offset>-<offset+length-1>`; a 206 must answer exactly that
  span, a 200 is sliced at the same offsets.
- The playhead only sets a paint expression fading stations observed more
  than 3 h (airports), 2 h (surface stations) or 15 h (soundings) away:
  paint properties, no feature rebuild.
- Below zoom 6 only airports with a TAF are drawn; soundings are hollow when
  the ascent never reached 500 hPa.
- Surface stations thin by `rank` (principal always, automatic from zoom
  5, gauges from 7) and are filled by their current temperature.
- Active fires (`web/src/fire/`) are not stations but ride the same
  switch and poll. They show the 24 h ending at the playhead, read per
  sensor as one range over consecutive hour lines with a margin, so a
  playing run does not refetch; age colour and the after-playhead filter
  are expressions, and a clustered GeoJSON source keeps the world view
  cheap.
- Air-quality stations (`web/src/aq/`) are station marks on the same
  layers (`stations/layers.ts`), filled on fixed PM2.5 steps in µg/m³ —
  never an index, which AirNow's terms forbid deriving — and hollow
  without PM2.5; they fade 3 h from the playhead. Every network's stations
  file loads with the index; the card reads the station's window by one
  range request when it opens, for the day's range and the network's own
  spelling of the name, and says "preliminary" for such networks.
- Warnings (`web/src/alerts/`) are areas, not marks, and a snapshot of now:
  the playhead never touches them. The index is polled with the rest; the
  network files (one GET each) are read only while the tile is pressed,
  once per round. A polygon warning is a feature of one GeoJSON source; an
  area-coded one is drawn from its boundary set through the `pmtiles://`
  protocol (the `pmtiles` package's, registered in `alerts/pmtiles.ts`),
  one vector source per set and a fill and an outline
  layer per set layer, filtered to the codes in force and coloured by the
  highest level on each code. NWS fire weather events go to the `fire`
  layer, the rest to the others. A MeteoAlarm warning can have both a
  polygon and codes, for different parts of its area, and both are drawn.
  A click inside an area still pins the probe and opens the card of every
  warning there, each with its issue time; JMA's card carries the
  boundary set's 出典 and 加工 lines and folds the survey notice, and
  MeteoAlarm's links its credit to meteoalarm.org and carries the
  disclaimer its terms require.
- URL state: `?tc=<id>|off`, `?tcagency=`, `?tcmodel=`, `?tcmembers=`,
  `?stations=snd,apt,syn,fire,aq,alerts|off` (link only, never stored).

### In the probe panel

A pin reads the station products whether or not their tiles are pressed:
`stations/nearest.ts` takes the nearest ascent within 150 km, airport
within 40 km, up to six surface stations within 20 km, and the nearest
air-quality station with PM2.5 within 20 km, whose value is a chip beside
the airport's. The warnings are read off the rendered areas, so the
highest one under the pin is a chip only while the warnings are drawn.

- **Skew-T** (`sounding/`): behind a disclosure, open above phone width.
  Opening it fetches the station's line and opens probe sessions for the
  run's isobaric `tmp` / `rh` / wind bundles (`sounding/model.ts`,
  `probeBundleIds`). The model column is read at the frame nearest the
  ascent's nominal time, within 3 h, else the legend says there is none.
  `SkewTInk` is read from `--skewt-*` at every draw so a theme switch
  repaints in place.
- **Airport** (`stations/observations.ts`): each METAR is a mark at its own
  lead seconds (`axisPosition`), the TAF a `taf` row of bands hatched for
  `TEMPO` / `PROB`, and readouts gain the nearest report within 90 minutes.
  None of it moves the playhead.
- **Surface station** (`stations/synopobs.ts`): the nearest candidate within
  300 m of the DEM height under the pin (the nearest outright until the DEM
  answers) replaces the airport on the temperature, wind and pressure rows;
  its temperature goes on the terrain row when there is one. Its window is
  one range request, made the first time a render picks the station.

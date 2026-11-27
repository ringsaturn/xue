# Publishing

The live site is a static shell on Cloudflare Pages; the data lives on a
public R2 bucket served at `dataset.ringsaturn.me/xue/` (bundles exceed the
Pages 25 MB per-file limit). The bucket is managed over R2's S3 API with
the AWS CLI.

Credentials: an R2 API token's key pair in `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY` plus `CLOUDFLARE_ACCOUNT_ID`; on Actions the
`R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `CLOUDFLARE_ACCOUNT_ID`
secrets. `mk/r2.mk` points the CLI at `scripts/aws-config`, which raises
the multipart threshold above every artifact, so each object costs one
Class A operation instead of one per part.

Rules that hold everywhere:

- The pointer goes last, and nothing is overwritten under an unchanged
  `?v=` (which is why rolling windows build each round into its own
  directory): [`delivery-and-deploy.md`](../../.claude/rules/delivery-and-deploy.md#the-data-contract).
- The Pages shell deploys before widened or new-source data
  ([releasing](releasing.md#deploy-order)).

## Forecast runs

```sh
make upload-r2 MODEL=gfs RUN=2026081600   # run assets, warm the CDN, then the pointer
make prune-r2  MODEL=gfs                  # delete the runs it superseded
```

`prune-r2` considers only `<model>.<run>/` directories, so showcase cases
are never removed. Between the assets and the pointer, `upload-r2` runs
`make warm-r2` (`scripts/warm_edge_cache.sh`): one GET of every artifact
through the public hostname with the site's `Origin`, so the edge and the
tiered cache hold the run before its first viewer (a cold fill from R2 is
about 1 MB/s per object). A failed warm-up
is reported and the pointer goes live anyway; `WARM=false` skips it.

One workflow per source (`publish-gfs.yml`, `publish-ecmwf.yml`, …) calls
the reusable [`publish.yml`](../../.github/workflows/publish.yml) on a
schedule and takes a manual dispatch with a dry-run switch. They pin the
native encoder ([`encoder.md`](../encoder.md#in-the-build-pipeline)).

`publish.yml` builds a run in pieces: `xuebuild bundle-groups` packs the
bundles into at most `max_jobs` groups of roughly equal cost, each group
builds in its own job (`build-bin --bundles …`, fetching only its inputs),
writes its bundles' arrays into the run's shared stores (`bundles.zarr`,
`bundles.half.zarr`; [`zarr-profile.md`](../zarr-profile.md#manifest)),
uploads them (`make upload-r2-bundles`, which never uploads a part or a
group document) and hands on `manifest.part.<group>.json` with each store's
`zarr.part.<first bundle>.json`; a final job merges the parts into the
manifest and each store's group document (`xuebuild assemble-run`), uploads
the group documents, then the manifest, warms both with the shared stores'
arrays (whose `?v=` exists only now; a job warms its posters and series
stores), then the pointer (`make upload-r2-manifest`); a separate job prunes
once finalize has succeeded, after waiting six minutes: open pages poll the
pointer every five, so one poll has happened by then. A page that missed it
(a background tab, a dropped connection) keeps asking for the old run until its
next poll and meets a 404 if that is after the prune. A source in
`sources.COLUMN_STORE_SOURCES` gets a `columns` job between build and finalize
([Column store](#column-store)). By hand:

```sh
.venv/bin/python -m xuebuild build-bin --model gfs --run 2026081600 --profile balanced --bundles tmp2m prate
make upload-r2-bundles MODEL=gfs RUN=2026081600
# with every manifest.part.*.json and bundles*.zarr/zarr.part.*.json gathered into web/public/data/gfs.2026081600/
.venv/bin/python -m xuebuild assemble-run --model gfs --run 2026081600
make upload-r2-manifest MODEL=gfs RUN=2026081600
```

**Top-up.** When the live cycle is still the newest but lacks bundles the
source now publishes, only those are built and merged onto the live
manifest and the live group documents (`make live-store-groups`); the
group documents are rewritten under a new `?v=`, which every entry naming
them then carries, and the live arrays are not touched. `force` rebuilds
everything:

```sh
make live-manifest MODEL=gfs > live-manifest.json
make live-store-groups MODEL=gfs RUN=2026081600 LIVE_MANIFEST=live-manifest.json OUT=live-stores
.venv/bin/python -m xuebuild bundle-groups --model gfs --base-manifest live-manifest.json
.venv/bin/python -m xuebuild build-bin --model gfs --run 2026081600 --bundles htsgw perpw
make upload-r2-bundles MODEL=gfs RUN=2026081600
.venv/bin/python -m xuebuild assemble-run --model gfs --run 2026081600 \
  --base-manifest live-manifest.json --base-stores live-stores
make upload-r2-manifest MODEL=gfs RUN=2026081600
```

A live run published before shared stores names none, so `live-stores`
stays empty and the top-up's bundles start the run's shared store beside
the live one-bundle stores.

### Column store

The `columns` job of `publish.yml` cuts `<model>.<run>/columns.zarr`
([`zarr-profile.md`](../zarr-profile.md), "Column store") before the run goes
live, so a run takes the store with it in one manifest and one pointer write:
resolve, build, **columns**, finalize, prune. A source with a store therefore
goes live when its store is ready, later by the cut; it goes live without the
store if the cut fails or exceeds 30 minutes (the frontend then reads the
maps' stores as before), and the next scheduled publish attaches it. The
attach path below is the exception.

- **Whether it runs** is decided in `resolve`, cheaply: the source is in
  `sources.COLUMN_STORE_SOURCES` (so its `SourceSpec.column_arrays` is set),
  the publish is not a dry run, and either
  - the run is not the one R2 serves (a new cycle, or a source's first
    publish): cut mode; or
  - R2 serves this very run and the publish found nothing to build (a skip):
    attach mode, when `xuebuild columns-missing` says the live manifest lacks
    the store, or the manifest names `columns` under a pointer that carries
    another CRC-32 (an attach died between its two writes).

  A top-up or a forced rebuild of the live run does neither: nothing is cut
  before the flip, so no cut ever changes what a live run's `?v=` names.
  Finalize's merge rule applies (it keeps the base's store when no member was
  rebuilt, and goes live without one otherwise) and the next scheduled publish
  attaches it. A source outside the list (every one until it is enabled) never
  starts the job.
- **Cut mode** (`make cut-columns`, `mk/r2.mk`; build succeeded). The job
  downloads the parts and, for a top-up, the base artifact exactly as finalize
  does and runs `assemble-run` locally with the same arguments, so the
  manifest, the shared stores' group documents and the pointer on disk are the
  ones finalize will write. If that manifest already names the store (a
  top-up that keeps its base's) it stops with no descriptor. Otherwise it
  fetches only the shards `xuebuild column-inputs` lists (never a group
  document: the local ones are authoritative, R2's may be absent or, on a
  top-up, the old run's), runs `xuebuild build-columns` (which checks each
  group document against the manifest's CRC-32 and the local pointer against
  the run, cuts into the hidden `.columns.zarr` and renames it), uploads the
  store with three attempts (immutable; shards `application/octet-stream`,
  documents JSON; only `columns.zarr` is ever uploaded), and writes
  `columns.json` last: `{run, stores, columns}`, the run, the CRC-32 of every
  shared store's group document it was cut against, and the store's own entry.
  The job uploads it as the `columns-<model>-<run>` artifact right after the
  cut, whatever follows; a separate step then warms the store, bounded to five
  minutes and never failing the job, so a slow warm-up cannot lose a complete
  store. No manifest, Item or pointer is uploaded. Some 4 GB in and 5 GB out
  for GFS on a ~14 GB runner. Inner timeout 30 minutes (the cut, not the warm-up).
  Before anything, `make check-live-run` refuses (below) when R2 has moved on.
- **Finalize** downloads that artifact by name pattern when resolve expected
  one (no match is the ordinary outcome of a failed or overrun cut and not an
  error; a download that really fails still fails the job) and runs
  `assemble-run --columns columns.json`. The descriptor is bound: its run must
  be this run and its group CRC-32s the ones just assembled, and its store must
  validate; then it becomes the manifest's `columns`. On any mismatch, or an
  unreadable or invalid descriptor, or a source without a store, it warns and
  the run goes live without one (never fails for it). Without a descriptor the
  run goes live exactly as before.
- **Re-runs.** A re-run of a red `columns` job re-runs finalize and prune with
  the outputs of the first attempt. Before any write, `cut-columns` and
  finalize run `make check-live-run`: R2's live pointer has to be the one
  resolve saw, or (finalize only) already the one about to be written, else the
  job exits 1. Publishing an older run by hand, which is how a bad one is rolled
  back, passes, since resolve saw the newer pointer. A pointer that cannot be
  read is an error, an absent one (a first publish) is fine, and a dry run checks
  nothing. Prune only runs after a successful finalize, so a refused one deletes
  nothing.
- **Attach mode** (resolve said skip: R2 serves this run without its store).
  `make publish-columns` cuts from R2's own objects (each map store's group
  document and each member's shard) and writes, in this order: the store and
  its edge warm-up (its `?v=` does not depend on the manifest), the manifest and
  the Item, the manifest's warm-up, the pointer last. The live pointer must
  still name this run, manifest path and manifest CRC-32 the job started from
  after the cut, after the store, before the manifest and again before the
  pointer (the warm-ups take minutes). If `prune-r2` ran or a newer run went
  live it prints a notice and exits 0, the pointer untouched: the store is
  unreferenced and goes with its run. A pointer that cannot be read is an
  error (exit 1), never taken for one that moved. Inner timeout 52 minutes.
- **Finishing a flip.** An attach that died after the manifest went up leaves
  a manifest naming `columns` under a pointer with the CRC-32 of the manifest
  before. The next attach sees that the manifest without its `columns`,
  serialized as the writer does, has the pointer's CRC-32, and only re-uploads
  the Item and flips the pointer. A manifest that differs from its pointer's in
  anything else fails the job.
- **Open tabs.** The attach's second flip changes the live pointer's
  `manifestCrc32` while the run stays the same; the shell adopts it in place
  (the manifest's `columns`) instead of reloading the page.
- **Overwrites.** The store's group document carries every shard's CRC-32, so
  its CRC-32, the `columns` entry's `crc32` and the objects' `?v=` change with
  any change to the cut. Cut mode only writes into the directory of a run R2
  does not serve, where nothing exists under any `?v=` a viewer holds, so it
  rewrites nothing a page can have cached. The attach path is the one narrow
  exposure: it rewrites the live manifest at its own key before the pointer
  flips (as a top-up does), so a page still on the older manifest that
  refetches `manifest.json?v=<old crc>` after a cache miss may be served the
  new bytes; the pointer's new `manifestCrc32` is what a polling page adopts.
- **Failure isolation.** The job is `continue-on-error`, so a failure marks
  the job red and warns but leaves the workflow green. A red workflow would
  read as a failed publish in the run metrics even though the run is live;
  the metrics read only `finalize <model> <run>` and its pointer step, which
  the job does not touch. A job timeout would be a cancellation, which sets
  the workflow's conclusion to cancelled, so each mode runs under an inner
  `timeout` that fails the step instead (30 and 52 minutes of the job's 60);
  the report and warning steps run on failure and cancellation alike.
  Finalize's and prune's `if` are written out (`!cancelled()` plus their own
  needs' results; a status-less `if` would keep an implicit success() over the
  whole chain and skip them whenever `columns` is skipped) so the columns job's
  result, whatever it is, never decides whether a run goes live or is pruned.
  `tests/test_metrics.py` guards both strings.
- **Concurrency.** The job runs inside the caller's workflow run, so it holds
  the caller's concurrency group (`publish-<model>`) while it works, up to half
  an hour in cut mode, and delays the run's landing by the same. A source that
  publishes every hour should not be enabled without thought: the next
  scheduled publish queues behind it.
- **Two writers, one store.** The Python reference (`xuebuild/columns.py`)
  and the native writer in the xuepy wheel (`xue.write_columns`) emit the same
  bytes. The job runs under `XUE_ENCODER=native`, so a locked wheel without
  the function fails the job loudly (the run goes live without the store)
  rather than falling back to Python; `XUE_ENCODER=python` cuts a store by
  hand with the reference.
- The edge is warmed whole (`scripts/warm_edge_cache.sh --columns`), because
  the edge fills a whole object on the first range request and a shard is up
  to ~140 MB.

**Enabling a source.** Add the source's id to
`sources.COLUMN_STORE_SOURCES`: `sources.py` then sets its
`SourceSpec.column_arrays` with `columnarrays.column_arrays`, which builds the
arrays from the source's published bundle ids by registry identity. The wheel
that writes it must be released and locked first (`release` skill), and the shell
that reads `columns` must be deployed first ([`releasing.md`](releasing.md#deploy-order)),
and enabling one adds a store about the size of its map stores' arrays to every
run (GFS some 5 GB), so ask before turning one on.

## Rolling windows

`mrms`, `jma`, `cma`, `himawari`, `himawarijp`, `goeseast`, `goeswest`,
`meteosat` and `aurora` each run one round of
[`scripts/window_rounds.sh`](../../scripts/window_rounds.sh) per job
(`ONCE=true`) on a five-minute cron (ten for the 10-minute imagers). A
`loop` dispatch runs rounds until twenty past the next hour and yields to
the next scheduled job (`scripts/successor_queued.sh`).

A round: compare the newest upstream frame with the live `window.json`
(`make live-window`); if there is something new, rebuild the window
(`build-bin --run latest --hours N --round HHMM`, reusing frames on disk),
upload into `<model>.<run>/<HHMM>/` with `make upload-r2 … ROUND=HHMM`
(`WARM=false`), then prune: `make prune-r2-rounds` keeps the live run's
newest two rounds (clock order; it lists only the run directory the
pointer names), and on a run change `make prune-r2 KEEP=2` drops older
runs. One log line per round records the newest frame's age and each
step's seconds.

With `SEGMENTS=true` a round's stores are cut into clock-aligned segments
([`zarr-profile.md`](../zarr-profile.md#segmented-stores)) before upload:
the build reads the live pointer and manifest (`make live-pointer`,
`make live-manifest`), names again every segment the live round already
published, and writes only the rest under `<model>.segments/` — one shared
segment per tier and interval, `bundles[.<tier>].zarr`, holding every
bundle with frames there.
`upload-r2` copies those before the round's directory, so a round uploads
the hour in progress and whatever hour just closed, not the window.
`make prune-r2-segments` then reads every round left on the bucket and
deletes the segment directories none of them names; it finds them with
one recursive listing of `<model>.segments/`, not a listing per segment
start. `prune-r2` never counts `<model>.segments/` as a run.

Every window workflow except MRMS resolves its encoder at job time: it
takes `native` when the installed wheel's `native.knows_source` has the
model, else the reference pipeline with a system GDAL and a `::warning::`
on the run (a wheel on PyPI may predate a source, and a cron run takes no
input). A dispatch can insist on either. MRMS and MRMS 3D pass
`encoder: native`, so a fallback fails the round.

`publish-mrms3d.yml` is disabled in the repository's Actions settings (the
last scheduled round ran 2026-10-05): the live window is paused and the
volume is reached through the showcase cases. `gh workflow enable
publish-mrms3d.yml` resumes the cron; nothing in the tree changes.

### How the workflows are built

`publish-<model>.yml` for `himawari`, `himawarijp`, `goeseast`, `goeswest`,
`jma`, `aurora`, `mrms` and `mrms3d` is a wrapper, like the forecast ones:
it owns the cron, the dispatch inputs, the fork guard, the `permissions`
(`actions: read`, which the called workflow can only narrow) and the
concurrency group, then calls
[`rounds.yml`](../../.github/workflows/rounds.yml) with the feed's values.
`rounds.yml` owns the job: validate inputs, check the R2 credentials, set
up uv and sync, resolve the encoder, run `scripts/window_rounds.sh`, verify
the live pointer, upload the `publish-report-<model>` artifact. A feed
differs from the others only in the inputs it passes:

| Input | Meaning |
|---|---|
| `model`, `hours`, `round_minutes` | the feed and its window and loop cadence |
| `encoder` | `auto` (dispatchable) or `native` (pinned) |
| `uv_group` | the dependency group to sync (`satellite`, `aurora`) |
| `pip_install` | one extra requirement after the sync (`jma`: the jma-radar tool) |
| `gdal` | `always` (satellite fetch stage runs `gdalwarp`), `fallback` (only when the encoder resolved to the reference pipeline), `none` |
| `frame_cache`, `frames_keep_hours`, `ancillary`, `segments` | the `window_rounds.sh` switches of the same names |

`cma` (a private archive with its own credentials and listing check) and
`meteosat` (every step skipped when the EUMETSAT secrets are absent, and a
runner GDAL probe) stay standalone workflows. `cma` stays published and
is reached through its path (`/cma`) and the showcase cases, but the shell's
model sheet and `docs/sources.md` leave it out on purpose.

The steps several workflows share are composite actions under
`.github/actions/`: `check-r2-credentials` (empty-secret check and a test
read of the dataset bucket; the secrets are inputs of that one step, so no
other step sees them) and `resolve-encoder` (the `native.knows_source`
probe and its `::warning::` fallback; run it after `uv sync`). Both are
used by `rounds.yml`, `publish-meteosat.yml` and, for the encoder,
`publish-cma.yml`.

Per-source switches:

| Source | `HOURS` | Extra |
|---|---|---|
| `mrms` | 4 | none |
| `jma` | 3 | `FRAME_CACHE=true` (jma-radar's decoded frames, pulled before and pushed after, pruned to seven days) |
| `cma` | 3 | `XUE_CMA_ARCHIVE` secret, `uv sync --group cma`; `CMA_ARCHIVE_ACCESS_KEY_ID` / `CMA_ARCHIVE_SECRET_ACCESS_KEY` when the dataset token cannot read the archive |
| `himawari`, `goeseast`, `goeswest` | 6 | `FRAME_CACHE=true` (warped GeoTIFFs, kept `FRAMES_KEEP_HOURS`=8), `ANCILLARY=true` (CAMEL months for the dust confidence), `--group satellite`, system `gdal-bin` |
| `himawarijp` | 3 | `FRAME_CACHE=true` under `himawarijp-frames` (kept `FRAMES_KEEP_HOURS`=5), no `ANCILLARY`, `--group satellite`, system `gdal-bin` |
| `meteosat` | 24 | as the disks plus the `EUMETSAT_*` secrets and `hdf5plugin` |
| `aurora` | 12 | `FRAME_CACHE=true` (the feed has only its newest grid; `force` never clears the cache), `--group aurora` |

```sh
ONCE=true scripts/window_rounds.sh                         # one MRMS round
MODEL=jma HOURS=3 FRAME_CACHE=true ONCE=true scripts/window_rounds.sh
MODEL=cma HOURS=3 ONCE=true scripts/window_rounds.sh       # needs XUE_CMA_ARCHIVE + R2_*
MODEL=himawari HOURS=6 FRAME_CACHE=true ONCE=true scripts/window_rounds.sh
MODEL=meteosat HOURS=24 FRAME_CACHE=true ONCE=true scripts/window_rounds.sh
make upload-r2 MODEL=mrms RUN=2026091321 ROUND=1405
make prune-r2-rounds MODEL=mrms && make prune-r2 MODEL=mrms KEEP=2
```

### Ancillary staging

The CAMEL emissivity months the satellite rounds pull (`ANCILLARY=true`,
`make pull-r2-ancillary`) are staged by
[`stage-ancillary.yml`](../../.github/workflows/stage-ancillary.yml): at
03:00 UTC on the 25th it stages the month in progress and the next for
every region in `xuebuild/satellite/staging.py` (the 1st is the retry) and
pushes them with `make push-r2-ancillary`. It is the only job that touches
Earthdata: the repository secrets `EARTHDATA_USERNAME` /
`EARTHDATA_PASSWORD`, written to the runner's `~/.netrc` for the job and
removed after; R2 is the usual three secrets. A dispatch takes one
`region`, one `month` (`YYYY-MM`) and `dry_run`; it needs no GDAL.

## Point products

Each runs on its own schedule, independent of the raster publishes, and
withholds its pointer only when nothing contributed.

| Product | Workflow | Cadence | Spec |
|---|---|---|---|
| Tropical cyclones | `publish-tc.yml` | twenty past every hour | [`docs/tc.md`](../tc.md) |
| Tropical cyclone archive | `archive-tc.yml` | 00:50 UTC daily; backfill by dispatch | [`docs/tc.md`](../tc.md) §8 |
| Airports | `publish-airport.yml` | every ten minutes | [`docs/airport.md`](../airport.md) |
| Soundings | `publish-sounding.yml` | a quarter past every hour | [`docs/sounding.md`](../sounding.md) |
| Surface stations | `publish-synop.yml` | three past every ten minutes | [`docs/synop.md`](../synop.md) |
| Active fires | `publish-fire.yml` | twenty-three past every hour | [`docs/fire.md`](../fire.md) |
| Air quality | `publish-aq.yml` | a quarter past and a quarter to every hour | [`docs/aq.md`](../aq.md) |
| Weather warnings | a self-hosted timer (below); `publish-alerts.yml` by dispatch only | every five minutes | [`docs/alerts.md`](../alerts.md) |

```sh
make live-tc-index && make tc-build                  # previous index first, so ids carry over
make upload-r2-tc ISSUE=2026091301 && make prune-r2-tc            # keeps TC_KEEP issues
.venv/bin/python -m xuebuild tc-archive > tc-archive-report.json   # last 48 issues + IBTrACS → tc/archive/
make upload-r2-tc-archive                            # the files it wrote, the catalog last
.venv/bin/python -m xuebuild tc-images > tc-images-report.json   # needs --group tcimages; redraws changed tracks
make upload-r2-tc-images                             # the pictures it drew, the index last
make prune-r2-tc-images                              # pictures the index dropped, once 30 days old
make live-airport-index && make airport-build        # merges onto the live 24 h history
make upload-r2-airport ROUND=202609161440 && make prune-r2-airport  # history → index → pointer; keeps three hours
make live-sounding-index && make sounding-build      # watermark + carry-forward from the live issue
make upload-r2-sounding ISSUE=2026091402 && make prune-r2-sounding  # keeps seven days
make live-synop-index && make synop-build            # each network resumes after its live `latest`
make upload-r2-synop ROUND=202610050010 && make prune-r2-synop  # network files → index → pointer; keeps three hours
make live-fire-index && make fire-build              # merges FIRMS's last 24 h onto the live seven days
make upload-r2-fire ROUND=202610120500 && make prune-r2-fire    # sensor files → index → pointer; keeps six hours
.venv/bin/python -m xuebuild tc-build --issue 2026091206 --offline --raw-dir tests/fixtures/tc
```

The airport, sounding, synop and fire builds also take `--offline --raw-dir
tests/fixtures/<product>`.

### Weather warnings on a host

MeteoAlarm's terms ask for warnings within five minutes on average and ten
at most, which a scheduled workflow (often late by several minutes, at
times skipped) cannot promise. So `alerts` runs on a host:
`scripts/alerts-host/` holds `xue-alerts-run.sh` (installed as
`/usr/local/sbin/xue-alerts-run`), `xue-alerts.service` and
`xue-alerts.timer` (`*:0/5:30`).

- Each run updates a sparse checkout of `main` (`xuebuild`, `mk`,
  `scripts`), `uv sync --frozen`s it, exports `XUE_BUILD_REVISION` (the
  checkout's commit, the index's `builder`) and takes the same steps as
  the workflow: live index, build, upload, prune. The installed script
  only bootstraps: after the checkout it runs `main`'s copy of itself, so
  the host follows `main`, this script included, with no deploy of its own.
- Then it journals the round into the monthly archive
  (`xue alerts-archive`, `docs/alerts.md` §12) under
  `$STATE/alerts-archive/`, which must survive restarts: the journal and
  the month in progress live only there until the hourly fold uploads
  them (`make upload-r2-alerts-archive`). A lost directory is seeded
  again from the published month; what it loses is the rounds journaled
  since the last fold. A failed archive step is logged and does not fail
  the round. The workflow fallback keeps no state and does not archive.
- The boundary sets' box files are cached in the checkout's
  `data/raw/alerts-areas/`, fetched once from the data origin.
- `/etc/xue-alerts/r2.env` (mode 600) holds an R2 token scoped to the
  dataset bucket, as `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and
  `CLOUDFLARE_ACCOUNT_ID`; the AWS CLI is awscli from PyPI through `uvx`.
- `/etc/xue-alerts/env`, when present, adds `DRY_RUN=--dryrun` or
  `FORCE=--force` for a test round.
- The unit runs as a dynamic user under `MemoryMax=400M`, `CPUQuota=50%`;
  a round takes about 40 s and 20 s of CPU, most of it the sync.
- One writer per pointer: while the timer runs, `publish-alerts.yml` has
  no schedule. To fall back, stop the timer
  (`systemctl disable --now xue-alerts.timer`) and dispatch the workflow,
  or restore its cron.

## Indicators

`publish-indicators.yml` (seventeen past every hour) reduces the published
GFS, ECMWF IFS, AIFS and IFS HRES stores over the soybean regions and
appends the result under `indicators/` at the data root (spec:
[`docs/indicators.md`](../indicators.md)). It reads the public stores over
HTTPS and never touches a run's own publish, so it runs on its own schedule
with base dependencies only (no GDAL, no wheel).

```sh
make pull-r2-indicators-weights                 # area weights, one Zarr store per grid
make live-indicators-index                      # live index + this month's files, CRC32-checked
make indicators-build                           # SOURCES via INDICATORS_SOURCES, ROUND defaults to now
make upload-r2-indicators                       # month files → season → context → index
```

Upload order: every month file, then the season totals, the climatology and
the context table, and only after R2 reports each object's size the
`index.json` that names them. The month files grow by appending but are
served under a new `?v=<crc32>` each time, so no URL ever changes under its
old bytes.

The weights are built by hand, rarely: when the crop mask or the region table
changes. Download the SPAM 2020 v2r2 soybean physical-area GeoTIFF (it sits
behind a Harvard Dataverse guestbook form, so it cannot be fetched by a
script) and the Natural Earth 10 m admin-1 archive, then:

```sh
uv sync --group indicators
.venv/bin/python scripts/indicators_weights.py --spam SOYB_A.tif --admin1 ne_10m_admin_1_states_provinces.zip \
    --weights-version 2          # bump it: rows record the weights version they used
make upload-r2-indicators-weights                 # manual; only when asked
```

Existing rows keep the weights version they were computed with; they are not
recomputed.

## Run metrics

`metrics.yml` (every six hours) reads the forecast publish workflows back
from the Actions API and rewrites `metrics/latency.json` (spec:
[`docs/metrics.md`](../metrics.md)). The cycle comes from the finalize job's
name, `finalize <model> <run>`, and the landing time from its pointer step,
so renaming either breaks the metrics (`tests/test_metrics.py` pins both).
The previous file lists the workflow runs already read; a build fetches job
records only for new ones, at most `METRICS_BUDGET` calls (the token allows
1,000 an hour), and the rest waits for the next build.

```sh
make live-metrics                               # the live file, the build's state
GITHUB_TOKEN=$(gh auth token) make metrics-latency
make upload-r2-metrics                          # one object, no-cache
```

## STAC catalog

The catalog ([`docs/stac.md`](../stac.md)) is derived, never edited: every
document is a function of the manifest (or a point product's index), the
catalog row and `xuebuild/sources.py`, so a split build and a whole one
write the same bytes. Writers: `build-bin` for a whole run,
`assemble-run`, `showcase build` / `refresh` / `catalog`, each point
product's build, and `xue stac --model <m> --run <r> [--round HHMM]` or
`xue stac --product <p> --issue <i>` to rewrite one. Uploads: a run's Item
with its manifest; the Collection, the live Item, the root catalog and its
`index.html` (`xuebuild/stacindex.py`) with the pointer
(`upload-r2-pointer`, `upload-r2-stac-collection`); the showcase's with
`upload-r2-showcase`. The root catalog and `index.html` change only with the
registry, yet every publish of every source reaches them, so they are put only
when R2's ETag differs from the local MD5 (`put_if_changed` in `mk/r2.mk`).

## Showcase cases

```sh
make showcase-check                    # validate every definition
make showcase CASE=zhengzhou-2021      # fetch, crop, encode, index
make upload-r2-showcase                # cases, then the catalog
```

On a runner, [`showcase.yml`](../../.github/workflows/showcase.yml) takes
case ids by dispatch, pulls the live catalog rows first
(`make live-showcase-catalog`) so the rewritten `showcase.json` still
lists every case, builds and uploads. The archives sit in AWS us-east-1,
where a runner is fastest. A case whose inputs exist only on one machine
(a fire round cut from downloaded granules, a local frame cache) is built
there and uploaded by hand: its own directory, then a `showcase.json`
collected after `make live-showcase-catalog`, never a sync of the whole
local tree. Authoring: [`showcase/README.md`](../../showcase/README.md).

## The shell

`deploy-pages.yml` deploys the shell on every push to `main` that touches
the frontend, Rust or the documents `llms-full.txt` is built from;
`make deploy` does it by hand. It never touches the bucket.

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

- The pointer goes last. Everything a pointer names is immutable and
  addressed with `?v=<crc32>`; uploading the pointer is what takes a run
  live.
- Never rewrite an object under an unchanged `?v=`: a viewer's range
  requests against it would decode the wrong bytes. This is why rolling
  windows build each round into its own directory.
- The Pages shell deploys before data in a widened schema or from a new
  source id (see [releasing](releasing.md#deploy-order)).

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
about 1 MB/s per object). H.264 companions are left cold. A failed warm-up
is reported and the pointer goes live anyway; `WARM=false` skips it.

One workflow per source (`publish-gfs.yml`, `publish-ecmwf.yml`, …) calls
the reusable [`publish.yml`](../../.github/workflows/publish.yml) on a
schedule and takes a manual dispatch with a dry-run switch. These pin
`XUE_ENCODER=native`, so a fallback to the slow path fails rather than
passing unnoticed.

`publish.yml` builds a run in pieces: `xuebuild bundle-groups` packs the
bundles into at most `max_jobs` groups of roughly equal cost, each group
builds in its own job (`build-bin --bundles …`, fetching only its inputs),
uploads its bundles (`make upload-r2-bundles`) and hands on
`manifest.part.<group>.json`; a final job merges the parts
(`xuebuild assemble-run`), uploads the manifest and the pointer
(`make upload-r2-manifest`) and prunes. By hand:

```sh
.venv/bin/python -m xuebuild build-bin --model gfs --run 2026081600 --profile balanced --bundles tmp2m prate
make upload-r2-bundles MODEL=gfs RUN=2026081600
# with every manifest.part.*.json gathered into web/public/data/gfs.2026081600/
.venv/bin/python -m xuebuild assemble-run --model gfs --run 2026081600
make upload-r2-manifest MODEL=gfs RUN=2026081600
```

**Top-up.** When the live cycle is still the newest but lacks bundles the
source now publishes, only those are built and merged onto the live
manifest; `force` rebuilds everything:

```sh
make live-manifest MODEL=gfs > live-manifest.json
.venv/bin/python -m xuebuild bundle-groups --model gfs --base-manifest live-manifest.json
.venv/bin/python -m xuebuild build-bin --model gfs --run 2026081600 --bundles htsgw perpw
make upload-r2-bundles MODEL=gfs RUN=2026081600
.venv/bin/python -m xuebuild assemble-run --model gfs --run 2026081600 --base-manifest live-manifest.json
make upload-r2-manifest MODEL=gfs RUN=2026081600
```

## Rolling windows

`mrms`, `jma`, `cma`, `himawari`, `goeseast`, `goeswest`, `meteosat` and
`aurora` each run one round of
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

Every window workflow except MRMS resolves its encoder at job time: it
takes `native` when the installed wheel's `native.knows_source` has the
model, else the reference pipeline with a system GDAL and a `::warning::`
on the run (a wheel on PyPI may predate a source, and a cron run takes no
input). A dispatch can insist on either.

Per-source switches:

| Source | `HOURS` | Extra |
|---|---|---|
| `mrms` | 4 | none |
| `jma` | 3 | `FRAME_CACHE=true` (jma-radar's decoded frames, pulled before and pushed after, pruned to seven days) |
| `cma` | 3 | `XUE_CMA_ARCHIVE` secret, `uv sync --group cma`; `CMA_ARCHIVE_ACCESS_KEY_ID` / `CMA_ARCHIVE_SECRET_ACCESS_KEY` when the dataset token cannot read the archive |
| `himawari`, `goeseast`, `goeswest` | 6 | `FRAME_CACHE=true` (warped GeoTIFFs, kept `FRAMES_KEEP_HOURS`=8), `ANCILLARY=true` (CAMEL months for DEBRA), `--group satellite`, system `gdal-bin` |
| `meteosat` | 24 | as above plus the `EUMETSAT_*` secrets and `hdf5plugin` |
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

## Point products

Each runs on its own schedule, independent of the raster publishes, and
withholds its pointer only when nothing contributed.

| Product | Workflow | Cadence | Spec |
|---|---|---|---|
| Tropical cyclones | `publish-tc.yml` | twenty past every hour | [`docs/tc.md`](../tc.md) |
| Airports | `publish-airport.yml` | every ten minutes | [`docs/airport.md`](../airport.md) |
| Soundings | `publish-sounding.yml` | a quarter past every hour | [`docs/sounding.md`](../sounding.md) |

```sh
make live-tc-index && make tc-build                  # previous index first, so ids carry over
make upload-r2-tc ISSUE=2026091301 && make prune-r2-tc            # keeps two days
make live-airport-index && make airport-build        # merges onto the live 24 h history
make upload-r2-airport ROUND=202609161440 && make prune-r2-airport  # history → index → pointer; keeps three hours
make live-sounding-index && make sounding-build      # watermark + carry-forward from the live issue
make upload-r2-sounding ISSUE=2026091402 && make prune-r2-sounding  # keeps two days
.venv/bin/python -m xuebuild tc-build --issue 2026091206 --offline --raw-dir tests/fixtures/tc
```

The airport and sounding builds also take `--offline --raw-dir
tests/fixtures/<product>`.

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
`upload-r2-showcase`.

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
where a runner is fastest. Authoring: [`showcase/README.md`](../../showcase/README.md).

## The shell

`deploy-pages.yml` deploys the shell on every push to `main` that touches
the frontend, Rust or the documents `llms-full.txt` is built from;
`make deploy` does it by hand. It never touches the bucket.

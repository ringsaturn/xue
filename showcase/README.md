# Historical showcase cases

A **case** is one past weather event — a forecast run, or a series of
observations — cropped to the region and hours the event is about, and
published as a permanent dataset alongside the live feed. Cases are what
`showcase.html` lists and what `/?case=<id>` plays back.

Everything about a case comes out of the ordinary pipeline: same encoder, same
`.xue` container, same schema v5 manifest. Only two knobs differ from a live
run — a bounding box (`--bbox`, `crop_grid`) and a subset of variables — and
both keep the output small enough that a case can stay published forever.
`make prune-r2` only ever deletes `<model>.<run>/` directories, so nothing
retires a case.

## Adding a case

1. Write `showcase/cases/<id>.json` (the filename must match the `id`):

   ```json
   {
     "id": "zhengzhou-2021",
     "title": { "zh": "郑州「7·20」特大暴雨", "en": "Zhengzhou 7·20 rainstorm" },
     "summary": { "zh": "…", "en": "…" },
     "model": "gfs",
     "run": "2021071800",
     "hours": 120,
     "bbox": [105.0, 28.0, 122.0, 42.0],
     "variables": ["prate", "wind10m"],
     "defaultVariable": "prate",
     "eventTime": "2021-07-20T08:00:00Z",
     "tags": ["rainstorm", "asia"],
     "credit": "NOAA GFS"
   }
   ```

   - `title` and `summary` must carry every UI locale (`zh` and `en`).
   - `bbox` is `[west, south, east, north]` in degrees. `west > east` crosses
     the antimeridian. The crop rounds outward to whole grid cells, so the
     published data always covers the box outright.
   - `hours` must land on the model's published axis (GFS and sflux: hourly to
     f120, then 3-hourly to f240; ECMWF: 3-hourly to 144, then 6-hourly). On
     an observation source it is where to stop, and must be a whole hour the
     dataset carries a frame at — the frames in between come at whatever
     cadence the file has (six minutes for the radar mosaic).
   - `variables` is any subset of what the model publishes, in any order —
     `tmp2m`, `prate`, `wind10m`, `dswrf` on sflux only, and `cref` on radar
     only. The manifest ships just these, and the viewer hides the buttons for
     the rest.
   - Optional: `defaultVariable` (defaults to the first), `eventTime`, `tags`,
     `credit`, `profile`.

   An **observation case** is the same file with one substitution: instead of
   a `run` to fetch it names the local `dataset` it is built from, and `hours`
   counts from the first frame of that file rather than from an analysis.

   ```json
   {
     "id": "shadel-2026",
     "model": "radar",
     "dataset": "typhoon_shadel/shadel_track_hourly_20260826-0903_z4.nc",
     "hours": 212,
     "variables": ["cref"]
   }
   ```

   A relative `dataset` resolves against `$XUE_OBSERVATION_ROOT` (default
   `../radar-l3-mst/data`, the sibling checkout that produces these files).
   Nothing publishes them, so an observation case is only rebuildable by
   someone who has the dataset — the built output is an ordinary case like any
   other.

   Watch the size: an observation case keeps the source cadence, so it is
   frames × grid, not hours × grid. The radar mosaic at six minutes is ten
   times an hourly case, which is what the `bbox` is for.

2. Validate without downloading anything:

   ```sh
   make showcase-check
   ```

3. Build it. One case at a time — the fetch pulls every frame of an archived
   run, which is minutes of downloading and gigabytes on sflux:

   ```sh
   make showcase CASE=zhengzhou-2021
   ```

   This writes `web/public/data/showcase/<id>/` (bundles, posters, manifest,
   and a `case.json` sidecar) and rewrites `web/public/data/showcase.json`,
   the mutable catalog collected from every sidecar on disk.

   Scratch cases for local development go in `showcase/cases-local/`
   (gitignored) and build with `make showcase CASES_DIR=showcase/cases-local`,
   so experiments never reach the curated set.

4. Check it locally with `npm run dev` at `/showcase.html`, then publish:

   ```sh
   make upload-r2-showcase
   ```

   Cases upload immutable and `?v=<crc32>`-addressed; `showcase.json` is copied
   last with `no-cache`, and that is what makes a new case visible.

## Choosing a run

The public archives do not go back forever:

| Source | Reaches back to | Notes |
|---|---|---|
| `gfs`, `sflux` | about 2021-01 | NOAA moved the files under `atmos/` on 2021-03-23; `xue.fetch` picks the layout by run id |
| `ecmwf` | about 2024-02 | Only the 00z and 12z oper cycles reach far enough for a long case |
| `radar` | — | Not an archive to reach back into: whatever event someone has already decoded into a local NetCDF |

Pick a cycle a day or two before the event peaks, so the case is a *forecast*
of the event rather than an analysis of it, and give it enough `hours` to run
past the peak.

Two constraints worth knowing:

- On sflux there is no `PRATE` record at f000, so a prate-only sflux case has
  no variable present in every file to key its frames by. Add another variable
  (`wind10m` is the natural companion) — the case validator says so outright.
- Cases skip the half-resolution ladder and the H.264 companion. At a few
  megabytes there is nothing left for either to save.

## Copy

The `summary` is read by people who were not there. Keep it factual: what
happened, where, when, and which run this is. Do not editorialize, and do not
state figures you have not checked.

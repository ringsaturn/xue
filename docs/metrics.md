# Run metrics

`metrics/latency.json` at the data root records when each forecast run
went live, for the last 30 days, measured rather than promised. It is the
data behind the metrics page; Xue makes no availability commitment.

It is built by `xuebuild metrics-latency` (`.github/workflows/metrics.yml`,
every six hours) from the GitHub Actions records of the forecast publish
workflows. Nothing in a manifest or a pointer carries a publication time,
and nothing there changes for this file.

## What is measured

Every forecast source publishes through `publish.yml`. Its `finalize` job
is named `finalize <model> <run>`, and its step *Upload the manifest and
take the run live* writes the live pointer.

- **Landed**: that step succeeded and the publish was not a dry run (a dry
  run skips *Verify the live pointer*). `landedAt` is the step's completion
  time, the moment the pointer was written.
- **Latency**: `landedAt` minus the run's cycle time, in minutes. It
  includes the upstream model's own production time: a run cannot be
  published before its last input file exists.
- A cycle written live more than once (a top-up, a forced rebuild) arrives
  at its first write.
- **Cadence**: the commonest gap, in hours, between consecutive cycles that
  arrived in the window. It is measured, not declared, so a source that
  publishes two of its upstream's four daily cycles is not counted as
  missing the other two.
- **Missing**: cycles on that cadence, strictly between the window's first
  and last arrival, that never arrived.
- **Deadlines**: for each whole local day in the window, the newest cycle
  live at a fixed local clock time, and its age then in hours. The one
  deadline listed is the ERCOT day-ahead market close, 10:00 US Central
  (`America/Chicago`, so 15:00 UTC in summer and 16:00 UTC in winter).

Workflow runs whose finalize job is named without the run (before the
naming existed) take the run from their `publish-report-<model>` artifact
while it is retained, otherwise `run` is `null` and the arrival is not
counted.

## The file

One JSON object, `schemaVersion` 1, served `no-cache`:

| Field | Meaning |
|---|---|
| `schemaVersion` | `1` |
| `generatedAt` | When the file was built, ISO 8601 UTC |
| `windowDays` | Days the summaries cover (30) |
| `repository` | The repository whose workflow runs were read |
| `sources` | One object per forecast source id, below |

Per source:

| Field | Meaning |
|---|---|
| `workflow` | The caller of `publish.yml`, e.g. `publish-gfs.yml` |
| `cadenceHours` | Integer, or `null` with fewer than two arrivals |
| `landed` | Arrivals in the window |
| `missing` | The missing cycles, `YYYYMMDDHH`, oldest first |
| `outcomes` | Workflow runs started in the window, counted by `outcome` |
| `latencyMinutes` | `{median, p90, max}` over the window's arrivals, or `null` |
| `latest` | The newest arrival, as in `arrivals`, or `null` |
| `deadlines` | `[{id, label, timeZone, localTime, days: [{date, cutoff, run, ageHours}]}]`; `run` is `null` (and `ageHours` absent) when nothing had arrived by `cutoff` |
| `arrivals` | `[{run, landedAt, latencyMinutes}]`, oldest first |
| `backlog` | Workflow runs not yet read because the build's API budget ran out; the next build reads them |
| `workflowRuns` | Every workflow run read, `[{id, event, conclusion, startedAt, outcome, run, landedAt}]`, oldest first; `outcome` is `landed`, `skipped` (the bucket already served the newest cycle), `dry-run` or `failed` |

`workflowRuns` keeps one day beyond the window, so the first day's deadline
sees the run live before it. It is also the next build's state: a build
fetches job records only for workflow runs it does not list.

Observation windows (radar, satellite) and point products are not covered
yet.

---

Part of the Xue specification, licensed under [CC BY 4.0](../LICENSE-CC-BY).

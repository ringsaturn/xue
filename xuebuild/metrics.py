"""``metrics/latency.json``: when each forecast run went live, measured
from the publish workflows' own records (``docs/metrics.md``).

Nothing in the data contract carries a publication time, and nothing
needs to: every forecast source publishes through ``publish.yml``, whose
``finalize`` job is named after the run it takes live and whose step
``Upload the manifest and take the run live`` writes the pointer. That
step's completion is the moment the run went live; the run's cycle time
subtracted from it is the latency. The GitHub Actions API keeps both for
every workflow run, so this module reads them back once a day and writes
one JSON file, with no schema change anywhere else.

The file is its own state: the workflow runs it has seen are listed in
it, so a build fetches the job records of new runs only and the API
budget of one token stays bounded however long the window is. Runs older
than the job naming the cycle are resolved through the run's
``publish-report-<model>`` artifact, while it is retained.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .common import iso_z, write_bytes_atomic
from .errors import XueError

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1
LATENCY_PATH = "metrics/latency.json"
API_ROOT = "https://api.github.com"
USER_AGENT = "xue-metrics (+https://github.com/ringsaturn/xue)"
TIMEOUT_SECONDS = 60
WINDOW_DAYS = 30

# The job and step publish.yml writes the pointer from. The job carries the
# run in its name; reused through `uses:`, the API prefixes it with the
# caller's job ("publish / finalize gfs 2026101300").
_FINALIZE = re.compile(r"(?:^|/ )finalize (?P<model>[a-z0-9]+)(?: (?P<run>\d{10}))?$")
TAKE_LIVE_STEP = "Upload the manifest and take the run live"
# Skipped exactly when the publish is a dry run, whose upload step writes
# nothing.
VERIFY_STEP = "Verify the live pointer"

# The deadlines whose newest live run the file reports. ERCOT's day-ahead
# market closes at 10:00 Central, the daylight-saving shift included.
DEADLINES: tuple[dict[str, str], ...] = (
    {
        "id": "ercot-day-ahead",
        "label": "ERCOT day-ahead market close, 10:00 US Central",
        "timeZone": "America/Chicago",
        "localTime": "10:00",
    },
)

_WORKFLOW_MODEL = re.compile(r"^\s+model:\s*([a-z0-9]+)\s*$", re.MULTILINE)


class MetricsError(XueError):
    """The Actions API or the previous file could not be read."""


def forecast_workflows(workflows_dir: Path) -> dict[str, str]:
    """``{model: workflow file}`` for every caller of ``publish.yml``: the
    sources whose runs go live through the finalize job."""
    found: dict[str, str] = {}
    for path in sorted(workflows_dir.glob("publish-*.yml")):
        text = path.read_text(encoding="utf-8")
        if "uses: ./.github/workflows/publish.yml" not in text:
            continue
        match = _WORKFLOW_MODEL.search(text)
        if match is None:
            raise MetricsError(f"{path.name} calls publish.yml without a model")
        found[match.group(1)] = path.name
    return found


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def cycle_time(run: str) -> datetime:
    return datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=UTC)


class Api(Protocol):
    def json(self, path: str, params: Mapping[str, str] | None = None) -> Any: ...

    def download(self, path: str) -> bytes: ...


class GitHubApi:
    """The REST calls this module makes, counted against a budget so one
    build never spends the token's hourly allowance."""

    def __init__(self, token: str | None, *, budget: int) -> None:
        self.token = token
        self.budget = budget
        self.calls = 0

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.budget

    def _open(self, url: str) -> bytes:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.calls += 1
        for attempt in range(3):
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                if error.code < 500 or attempt == 2:
                    raise MetricsError(f"GitHub API {error.code} for {url}") from error
            except urllib.error.URLError as error:
                if attempt == 2:
                    raise MetricsError(f"GitHub API unreachable for {url}: {error.reason}") from error
            time.sleep(2 ** (attempt + 1))
        raise AssertionError("unreachable")

    def json(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        return json.loads(self._open(f"{API_ROOT}{path}{query}"))

    def download(self, path: str) -> bytes:
        # The archive endpoint answers with a redirect to a signed blob
        # storage URL, which refuses a request that still carries the
        # token; urllib would forward it, so the redirect is followed here.
        url = f"{API_ROOT}{path}"
        headers = {"User-Agent": USER_AGENT, "Authorization": f"Bearer {self.token}"} if self.token else {}
        self.calls += 1
        try:
            _NO_REDIRECT.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as error:
            location = error.headers.get("Location") if error.code in (301, 302, 307) else None
            if location is None:
                raise MetricsError(f"GitHub API {error.code} for {url}") from error
        else:
            raise MetricsError(f"GitHub API did not redirect {url} to the archive")
        request = urllib.request.Request(location, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.read()
        except urllib.error.URLError as error:
            raise MetricsError(f"artifact archive unreachable for {url}: {error}") from error


class _KeepRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_NO_REDIRECT = urllib.request.build_opener(_KeepRedirect)


def list_workflow_runs(api: Api, repo: str, workflow: str, since: datetime) -> list[dict[str, Any]]:
    """Every completed run of ``workflow`` created since ``since``, newest
    first. A run still in progress is left for the next build."""
    runs: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = api.json(
            f"/repos/{repo}/actions/workflows/{workflow}/runs",
            {"created": f">={since.date().isoformat()}", "per_page": "100", "page": str(page)},
        )
        batch = payload.get("workflow_runs", [])
        runs.extend(run for run in batch if run.get("status") == "completed")
        if len(batch) < 100:
            return runs
        page += 1


def record_from_jobs(workflow_run: Mapping[str, Any], jobs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """One workflow run as the file lists it.

    ``outcome`` is ``landed`` (the pointer was written), ``skipped`` (R2
    already served the newest cycle), ``dry-run``, or ``failed`` (no
    pointer was written and the run did not succeed). ``run`` is the cycle
    the finalize job names, or None when its name predates that and the
    artifact must say.
    """
    record: dict[str, Any] = {
        "id": int(workflow_run["id"]),
        "event": workflow_run.get("event"),
        "conclusion": workflow_run.get("conclusion"),
        "startedAt": workflow_run.get("run_started_at") or workflow_run.get("created_at"),
        "outcome": "failed",
        "run": None,
        "landedAt": None,
    }
    finalize = None
    for job in jobs:
        match = _FINALIZE.search(job.get("name", ""))
        if match is not None:
            finalize = (job, match)
    if finalize is None:
        if workflow_run.get("conclusion") == "success":
            record["outcome"] = "skipped"
        return record
    job, match = finalize
    record["run"] = match.group("run")
    steps = {step.get("name"): step for step in job.get("steps", [])}
    take_live = steps.get(TAKE_LIVE_STEP)
    verify = steps.get(VERIFY_STEP)
    if job.get("conclusion") == "skipped":
        record["outcome"] = "skipped" if workflow_run.get("conclusion") == "success" else "failed"
    elif take_live is not None and take_live.get("conclusion") == "success":
        if verify is not None and verify.get("conclusion") == "skipped":
            record["outcome"] = "dry-run"
        else:
            record["outcome"] = "landed"
            record["landedAt"] = take_live.get("completed_at")
    return record


def run_from_artifact(api: Api, repo: str, workflow_run_id: int, model: str) -> str | None:
    """The run a finalize job took live, from the assemble report in its
    ``publish-report-<model>`` artifact; None once the artifact expired."""
    listing = api.json(f"/repos/{repo}/actions/runs/{workflow_run_id}/artifacts", {"per_page": "100"})
    for artifact in listing.get("artifacts", []):
        if artifact.get("name") != f"publish-report-{model}" or artifact.get("expired"):
            continue
        archive = api.download(f"/repos/{repo}/actions/artifacts/{artifact['id']}/zip")
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            if "assemble-report.json" not in bundle.namelist():
                return None
            run = json.loads(bundle.read("assemble-report.json")).get("run")
        return run if isinstance(run, str) and re.fullmatch(r"\d{10}", run) else None
    return None


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def landings(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The first time each cycle went live, oldest first. A top-up or a
    forced rebuild writes the pointer again; only the first write is the
    run's arrival."""
    first: dict[str, datetime] = {}
    for record in records:
        if record.get("outcome") != "landed" or not record.get("run"):
            continue
        landed = parse_time(record.get("landedAt"))
        if landed is None:
            continue
        run = record["run"]
        if run not in first or landed < first[run]:
            first[run] = landed
    out = []
    for run, landed in sorted(first.items(), key=lambda item: item[1]):
        minutes = (landed - cycle_time(run)).total_seconds() / 60
        out.append({"run": run, "landedAt": iso_z(landed), "latencyMinutes": round(minutes, 1)})
    return out


def cadence_hours(runs: Sequence[str]) -> int | None:
    """The usual spacing of the cycles that went live: the commonest gap
    between consecutive ones. Measured, not declared, so a source that
    publishes two of its four cycles is not counted as missing the others."""
    times = sorted({cycle_time(run) for run in runs})
    gaps = [round((later - earlier).total_seconds() / 3600) for earlier, later in zip(times, times[1:])]
    gaps = [gap for gap in gaps if gap > 0]
    if not gaps:
        return None
    counts = Counter(gaps)
    return min(gap for gap, count in counts.items() if count == max(counts.values()))


def missing_cycles(runs: Sequence[str], cadence: int | None) -> list[str]:
    """Cycles on the cadence between the first and last live run that never
    went live."""
    if not cadence:
        return []
    times = sorted({cycle_time(run) for run in runs})
    present = set(times)
    missing = []
    step = timedelta(hours=cadence)
    moment = times[0]
    while moment < times[-1]:
        moment += step
        if moment not in present and moment < times[-1]:
            missing.append(moment.strftime("%Y%m%d%H"))
    return missing


def deadline_days(
    arrivals: Sequence[Mapping[str, Any]], deadline: Mapping[str, str], start: date, end: date
) -> list[dict[str, Any]]:
    """For each local day from ``start`` to ``end``, the newest cycle that
    was live at the deadline, and how old it was then."""
    zone = ZoneInfo(deadline["timeZone"])
    hour, minute = (int(part) for part in deadline["localTime"].split(":"))
    ordered = sorted(arrivals, key=lambda item: item["landedAt"])
    days = []
    day = start
    while day <= end:
        cutoff = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone).astimezone(UTC)
        live = [item["run"] for item in ordered if parse_time(item["landedAt"]) <= cutoff]
        newest = max(live) if live else None
        entry: dict[str, Any] = {"date": day.isoformat(), "cutoff": iso_z(cutoff), "run": newest}
        if newest is not None:
            entry["ageHours"] = round((cutoff - cycle_time(newest)).total_seconds() / 3600, 2)
        days.append(entry)
        day += timedelta(days=1)
    return days


def summarize_source(
    records: Sequence[Mapping[str, Any]], *, now: datetime, window_days: int
) -> dict[str, Any]:
    window_start = now - timedelta(days=window_days)
    arrivals = [item for item in landings(records) if parse_time(item["landedAt"]) >= window_start]
    latencies = [item["latencyMinutes"] for item in arrivals]
    runs = [item["run"] for item in arrivals]
    cadence = cadence_hours(runs)
    missing = missing_cycles(runs, cadence)
    in_window = [record for record in records if (parse_time(record.get("startedAt")) or now) >= window_start]
    outcomes = Counter(record.get("outcome") for record in in_window)
    summary: dict[str, Any] = {
        "cadenceHours": cadence,
        "landed": len(arrivals),
        "missing": missing,
        "outcomes": dict(sorted(outcomes.items())),
        "latencyMinutes": None,
        "latest": arrivals[-1] if arrivals else None,
    }
    if latencies:
        summary["latencyMinutes"] = {
            "median": round(statistics.median(latencies), 1),
            "p90": round(_percentile(latencies, 0.9), 1),
            "max": round(max(latencies), 1),
        }
    # Whole local days inside the window only: the first day may have
    # started before the oldest arrival the window holds.
    all_arrivals = landings(records)
    deadlines = []
    for deadline in DEADLINES:
        zone = ZoneInfo(deadline["timeZone"])
        start = (window_start.astimezone(zone) + timedelta(days=1)).date()
        end = now.astimezone(zone).date()
        days = deadline_days(all_arrivals, deadline, start, end)
        days = [day for day in days if parse_time(day["cutoff"]) <= now]
        deadlines.append({**deadline, "days": days})
    summary["deadlines"] = deadlines
    summary["arrivals"] = arrivals
    return summary


def load_previous(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != SCHEMA_VERSION:
        raise MetricsError(f"{path} is schema {payload.get('schemaVersion')}, this build writes {SCHEMA_VERSION}")
    return payload


def build_latency(
    api: GitHubApi | Api,
    *,
    repo: str,
    workflows: Mapping[str, str],
    previous: Mapping[str, Any] | None,
    now: datetime,
    window_days: int = WINDOW_DAYS,
) -> dict[str, Any]:
    """The new file: the previous one's workflow runs kept while inside the
    window (plus a day, for the first deadline), the new ones fetched
    newest first until the API budget runs out, and every summary
    recomputed from them."""
    keep_from = now - timedelta(days=window_days + 1)
    sources: dict[str, Any] = {}
    for model, workflow in sorted(workflows.items()):
        known = {
            int(record["id"]): dict(record)
            for record in ((previous or {}).get("sources", {}).get(model, {}).get("workflowRuns") or [])
            if (parse_time(record.get("startedAt")) or now) >= keep_from
        }
        backlog = 0
        for workflow_run in list_workflow_runs(api, repo, workflow, keep_from):
            identifier = int(workflow_run["id"])
            started = parse_time(workflow_run.get("run_started_at") or workflow_run.get("created_at"))
            if identifier in known or (started is not None and started < keep_from):
                continue
            if getattr(api, "exhausted", False):
                backlog += 1
                continue
            jobs = api.json(f"/repos/{repo}/actions/runs/{identifier}/jobs", {"per_page": "100"}).get("jobs", [])
            record = record_from_jobs(workflow_run, jobs)
            if record["outcome"] == "landed" and record["run"] is None:
                record["run"] = run_from_artifact(api, repo, identifier, model)
            known[identifier] = record
        records = sorted(known.values(), key=lambda record: record.get("startedAt") or "")
        summary = summarize_source(records, now=now, window_days=window_days)
        sources[model] = {"workflow": workflow, "backlog": backlog, **summary, "workflowRuns": records}
        if backlog:
            LOG.warning("%s: %d workflow runs left for the next build (API budget spent)", model, backlog)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": iso_z(now),
        "windowDays": window_days,
        "repository": repo,
        "sources": sources,
    }


def write_latency(output_dir: Path, payload: Mapping[str, Any]) -> Path:
    path = output_dir / LATENCY_PATH
    write_bytes_atomic(path, (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    return path


def report(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The step summary's view: one line per source."""
    out = {}
    for model, source in payload["sources"].items():
        out[model] = {
            "landed": source["landed"],
            "missing": len(source["missing"]),
            "latencyMinutes": source["latencyMinutes"],
            "outcomes": source["outcomes"],
            "backlog": source["backlog"],
        }
    return out


def run_metrics_latency(
    *,
    output_dir: Path,
    workflows_dir: Path,
    repo: str,
    budget: int,
    window_days: int = WINDOW_DAYS,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    api = GitHubApi(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"), budget=budget)
    path = output_dir / LATENCY_PATH
    payload = build_latency(
        api,
        repo=repo,
        workflows=forecast_workflows(workflows_dir),
        previous=load_previous(path),
        now=now or datetime.now(UTC),
        window_days=window_days,
    )
    if not dry_run:
        write_latency(output_dir, payload)
    return {"path": str(path), "apiCalls": api.calls, "dryRun": dry_run, "sources": report(payload)}

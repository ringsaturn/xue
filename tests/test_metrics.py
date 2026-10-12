import io
import json
import unittest
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

from xuebuild import metrics

ROOT = Path(__file__).resolve().parents[1]


def step(name, conclusion, completed="2026-10-12T05:45:59Z"):
    return {"name": name, "conclusion": conclusion, "completed_at": completed}


def finalize_job(name="publish / finalize gfs 2026101200", conclusion="success", verify="success", landed="2026-10-12T05:45:59Z"):
    return {
        "name": name,
        "conclusion": conclusion,
        "steps": [
            step("Assemble the manifest", "success"),
            step(metrics.TAKE_LIVE_STEP, "success", landed),
            step("Delete superseded runs", "success"),
            step(metrics.VERIFY_STEP, verify),
        ],
    }


def workflow_run(identifier=1, conclusion="success", started="2026-10-12T05:40:32Z"):
    return {"id": identifier, "event": "schedule", "conclusion": conclusion, "run_started_at": started}


class FakeApi:
    def __init__(self, runs, jobs, artifacts=None, budget=1000):
        self.runs = runs
        self.jobs = jobs
        self.artifacts = artifacts or {}
        self.budget = budget
        self.calls = 0

    @property
    def exhausted(self):
        return self.calls >= self.budget

    def json(self, path, params=None):
        self.calls += 1
        if path.endswith("/runs") and "/workflows/" in path:
            workflow = path.split("/workflows/")[1].split("/")[0]
            return {"workflow_runs": self.runs.get(workflow, [])}
        if path.endswith("/jobs"):
            return {"jobs": self.jobs[int(path.split("/")[-2])]}
        if path.endswith("/artifacts"):
            identifier = int(path.split("/")[-2])
            return {"artifacts": [{"id": identifier, "name": name, "expired": False} for name in self.artifacts.get(identifier, {})]}
        raise AssertionError(path)

    def download(self, path):
        self.calls += 1
        identifier = int(path.split("/")[-2])
        (report,) = self.artifacts[identifier].values()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("assemble-report.json", json.dumps(report))
        return buffer.getvalue()


class ForecastWorkflowsTest(unittest.TestCase):
    def test_every_caller_of_publish_yml_is_found(self):
        found = metrics.forecast_workflows(ROOT / ".github" / "workflows")
        self.assertEqual(found["gfs"], "publish-gfs.yml")
        self.assertEqual(found["hrrr"], "publish-hrrr.yml")
        self.assertNotIn("mrms", found)
        self.assertNotIn("indicators", found)

    def test_finalize_job_names_its_run(self):
        # The metrics read the cycle out of this job name.
        text = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
        self.assertIn("name: finalize ${{ inputs.model }} ${{ needs.resolve.outputs.run }}", text)
        self.assertIn(f"- name: {metrics.TAKE_LIVE_STEP}", text)
        self.assertIn(f"- name: {metrics.VERIFY_STEP}", text)


class RecordTest(unittest.TestCase):
    def test_landed(self):
        record = metrics.record_from_jobs(workflow_run(), [{"name": "publish / resolve gfs"}, finalize_job()])
        self.assertEqual(record["outcome"], "landed")
        self.assertEqual(record["run"], "2026101200")
        self.assertEqual(record["landedAt"], "2026-10-12T05:45:59Z")

    def test_skipped_when_finalize_skipped(self):
        job = finalize_job(conclusion="skipped")
        job["steps"] = []
        record = metrics.record_from_jobs(workflow_run(), [job])
        self.assertEqual(record["outcome"], "skipped")
        self.assertIsNone(record["landedAt"])

    def test_dry_run_is_not_a_landing(self):
        record = metrics.record_from_jobs(workflow_run(), [finalize_job(verify="skipped")])
        self.assertEqual(record["outcome"], "dry-run")

    def test_failure_before_the_pointer(self):
        job = finalize_job(conclusion="failure")
        job["steps"][1] = step(metrics.TAKE_LIVE_STEP, "failure")
        record = metrics.record_from_jobs(workflow_run(conclusion="failure"), [job])
        self.assertEqual(record["outcome"], "failed")

    def test_failure_after_the_pointer_still_landed(self):
        job = finalize_job(conclusion="failure")
        job["steps"][2] = step("Delete superseded runs", "failure")
        record = metrics.record_from_jobs(workflow_run(conclusion="failure"), [job])
        self.assertEqual(record["outcome"], "landed")

    def test_old_job_name_has_no_run(self):
        record = metrics.record_from_jobs(workflow_run(), [finalize_job(name="publish / finalize gfs")])
        self.assertEqual(record["outcome"], "landed")
        self.assertIsNone(record["run"])


class SummaryTest(unittest.TestCase):
    def test_first_write_of_a_cycle_is_its_arrival(self):
        records = [
            {"outcome": "landed", "run": "2026101200", "landedAt": "2026-10-12T06:00:00Z"},
            {"outcome": "landed", "run": "2026101200", "landedAt": "2026-10-12T05:00:00Z"},
        ]
        (arrival,) = metrics.landings(records)
        self.assertEqual(arrival["landedAt"], "2026-10-12T05:00:00Z")
        self.assertEqual(arrival["latencyMinutes"], 300.0)

    def test_cadence_and_missing(self):
        runs = ["2026101200", "2026101206", "2026101218", "2026101300", "2026101306"]
        self.assertEqual(metrics.cadence_hours(runs), 6)
        self.assertEqual(metrics.missing_cycles(runs, 6), ["2026101212"])

    def test_two_of_four_cycles_is_not_missing(self):
        runs = ["2026101100", "2026101112", "2026101200", "2026101212"]
        self.assertEqual(metrics.cadence_hours(runs), 12)
        self.assertEqual(metrics.missing_cycles(runs, 12), [])

    def test_deadline_follows_daylight_saving(self):
        deadline = metrics.DEADLINES[0]
        arrivals = [{"run": "2026110112", "landedAt": "2026-11-01T14:00:00Z"}]
        (summer,) = metrics.deadline_days(arrivals, deadline, date(2026, 10, 30), date(2026, 10, 30))
        (winter,) = metrics.deadline_days(arrivals, deadline, date(2026, 11, 2), date(2026, 11, 2))
        self.assertEqual(summer["cutoff"], "2026-10-30T15:00:00Z")
        self.assertIsNone(summer["run"])
        self.assertEqual(winter["cutoff"], "2026-11-02T16:00:00Z")
        self.assertEqual(winter["run"], "2026110112")
        self.assertEqual(winter["ageHours"], 28.0)


class BuildTest(unittest.TestCase):
    NOW = datetime(2026, 10, 13, 0, 0, tzinfo=UTC)

    def test_reads_new_runs_only_and_resolves_old_names_from_the_artifact(self):
        runs = {"publish-gfs.yml": [workflow_run(2, started="2026-10-12T11:30:00Z") | {"status": "completed"},
                                     workflow_run(1) | {"status": "completed"},
                                     {"id": 3, "status": "in_progress"}]}
        jobs = {2: [finalize_job(name="publish / finalize gfs", landed="2026-10-12T11:40:00Z")]}
        artifacts = {2: {"publish-report-gfs": {"run": "2026101206"}}}
        previous = {"schemaVersion": 1, "sources": {"gfs": {"workflowRuns": [
            metrics.record_from_jobs(workflow_run(1), [finalize_job()])]}}}
        api = FakeApi(runs, jobs, artifacts)
        payload = metrics.build_latency(api, repo="o/r", workflows={"gfs": "publish-gfs.yml"}, previous=previous, now=self.NOW)
        gfs = payload["sources"]["gfs"]
        self.assertEqual([item["run"] for item in gfs["arrivals"]], ["2026101200", "2026101206"])
        self.assertEqual(gfs["cadenceHours"], 6)
        self.assertEqual(gfs["backlog"], 0)
        self.assertEqual(len(gfs["workflowRuns"]), 2)

    def test_budget_leaves_a_backlog(self):
        runs = {"publish-gfs.yml": [workflow_run(identifier) | {"status": "completed"} for identifier in (1, 2, 3)]}
        jobs = {identifier: [finalize_job()] for identifier in (1, 2, 3)}
        api = FakeApi(runs, jobs, budget=2)
        payload = metrics.build_latency(api, repo="o/r", workflows={"gfs": "publish-gfs.yml"}, previous=None, now=self.NOW)
        self.assertEqual(payload["sources"]["gfs"]["backlog"], 2)


if __name__ == "__main__":
    unittest.main()

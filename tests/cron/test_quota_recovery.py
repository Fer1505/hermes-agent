"""Quota failure continuity through real local job/execution/incident stores.

Only model execution is synthetic; no provider or messaging transport is used.
"""
from datetime import datetime, timedelta, timezone

import pytest

from cron import executions, incidents, jobs, scheduler


@pytest.fixture
def clock(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr(jobs, "JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", tmp_path / "cron" / "output")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    monkeypatch.setattr(scheduler, "_get_hermes_home", lambda: tmp_path)
    current = [datetime(2026, 9, 15, 12, tzinfo=timezone.utc)]
    for module in (jobs, executions, incidents, scheduler):
        monkeypatch.setattr(module, "_hermes_now", lambda: current[0])
    return current


def test_quota_failure_preserves_schedule_and_history_through_later_success(clock, monkeypatch):
    job = jobs.create_job("Synthetic scheduled review", "every 6h", deliver="local")
    quota_error = "Synthetic provider usage limit; no reset time supplied"
    outcomes = iter([(False, "Synthetic interruption report", "", quota_error),
                     (True, "Synthetic completed review", "Review completed", None)])
    calls = []
    def run(job, **kwargs):
        calls.append(job["id"])
        return next(outcomes)
    monkeypatch.setattr(scheduler, "run_job", run)
    assert scheduler.run_one_job(job)
    interrupted = jobs.get_job(job["id"])
    assert interrupted["enabled"] is True
    assert interrupted["last_status"] == "error"
    assert interrupted["last_error"] == quota_error
    assert interrupted["failure_streak"] == 1
    assert interrupted["prompt"] == job["prompt"]
    assert interrupted["created_at"] == job["created_at"]
    assert datetime.fromisoformat(interrupted["next_run_at"]) == clock[0] + timedelta(hours=6)
    assert jobs.get_due_jobs() == []
    failed = executions.latest_execution(job["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == quota_error
    incident = incidents.list_incidents()[0]
    assert incident["job_id"] == job["id"]
    assert incident["failure_type"] == "rate_limit"

    # A later scheduled review is a distinct execution, not a replay of a
    # partly executed agent turn. Its success must not rewrite the old failure.
    clock[0] += timedelta(hours=6, seconds=1)
    due = jobs.get_due_jobs()
    assert [item["id"] for item in due] == [job["id"]]
    assert scheduler.run_one_job(due[0])
    recovered = jobs.get_job(job["id"])
    assert recovered["id"] == job["id"]
    assert recovered["created_at"] == job["created_at"]
    assert recovered["last_status"] == "ok"
    assert recovered["failure_streak"] == 0
    history = executions.list_executions(job_id=job["id"])
    assert [row["status"] for row in history] == ["completed", "failed"]
    assert history[1]["id"] == failed["id"]
    assert history[1]["error"] == quota_error
    assert calls == [job["id"], job["id"]]


def test_failed_finite_obligation_remains_inspectable_without_blind_replay(clock, monkeypatch):
    job = jobs.create_job("Synthetic one-time work", (clock[0] + timedelta(minutes=1)).isoformat(), deliver="local")
    quota_error = "Synthetic provider quota exceeded"
    monkeypatch.setattr(scheduler, "run_job", lambda job, **kwargs: (False, "Interrupted", "", quota_error))
    assert scheduler.run_one_job(job)
    retained = jobs.get_job(job["id"])
    assert retained is not None
    assert retained["last_status"] == "error"
    assert retained["last_error"] == quota_error
    assert retained["enabled"] is False
    assert retained["next_run_at"] is None
    assert executions.latest_execution(job["id"])["status"] == "failed"
    assert jobs.get_due_jobs() == []

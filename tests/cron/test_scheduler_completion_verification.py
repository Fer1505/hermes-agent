"""Fail-closed completion booking for cron runs (#93820).

The scheduler booked every finished run as ``cron_complete`` based on the run
lifecycle alone: a job whose agent turn died after a tool call, mid-API-wait,
or without any assistant text still surfaced as a healthy run (one audited
day held 10 such silently-failed sessions). The fix classifies the session's
LAST message row through the existing ``session_lifecycle_statuses`` helper
before ``end_session``: only a real assistant reply — a plain answer or the
``[SILENT]`` sentinel, both assistant-text rows — books as ``cron_complete``;
anything else books as ``cron_incomplete_no_output``. Classification is
best-effort: a probe failure keeps the historical reason rather than
mislabeling a healthy run.
"""

import os
import sqlite3

import pytest

import cron.scheduler as cron_scheduler
from gateway.session_context import reset_session_vars


class _FakeCronAgent:
    def __init__(self, *args, **kwargs):
        pass

    def run_conversation(self, prompt, **kwargs):
        return {
            "completed": True,
            "failed": False,
            "final_response": "done",
            "turn_exit_reason": "",
        }

    def close(self):
        pass


class _RecordingSessionDB:
    """SessionDB double with a configurable lifecycle classification."""

    def __init__(self, *args, **kwargs):
        self.ended: list[tuple[str, str]] = []
        self.lifecycle = type(self).next_lifecycle

    next_lifecycle = "complete"

    def set_session_title(self, *args, **kwargs):
        return True

    def get_compression_tip(self, session_id):
        return None

    def session_lifecycle_statuses(self, session_ids):
        if isinstance(type(self).next_lifecycle, Exception):
            raise type(self).next_lifecycle
        return {sid: type(self).next_lifecycle for sid in session_ids}

    def end_session(self, session_id, reason):
        self.ended.append((session_id, reason))

    def close(self):
        pass


def _run_booked_job(monkeypatch, tmp_path, *, db_type=_RecordingSessionDB, expected_error=None):
    import hermes_state
    import run_agent

    instances: list[_RecordingSessionDB] = []
    real_init = db_type.__init__

    def _capture_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        instances.append(self)

    monkeypatch.setattr(db_type, "__init__", _capture_init)
    monkeypatch.setattr(hermes_state, "SessionDB", db_type)
    monkeypatch.setattr(run_agent, "AIAgent", _FakeCronAgent)
    monkeypatch.setattr(
        "hermes_constants.resolve_reasoning_config", lambda *_a, **_k: None
    )
    # The runtime key is read from the environment (never a literal here);
    # AIAgent and SessionDB are fakes above, so the value is never used.
    monkeypatch.setenv("HERMES_TEST_RUNTIME_KEY", "unused-placeholder")

    def _fake_runtime(**_kwargs):
        return {
            "api_key": os.environ.get("HERMES_TEST_RUNTIME_KEY", ""),
            "base_url": None,
            "provider": "test-provider",
            "api_mode": None,
            "command": None,
            "args": None,
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", _fake_runtime
    )
    monkeypatch.setattr("tools.mcp_tool.discover_mcp_tools", lambda: [])
    monkeypatch.setattr(cron_scheduler, "_get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(cron_scheduler, "get_fallback_chain", lambda _cfg: [])
    monkeypatch.setattr(
        cron_scheduler, "_guard_job_credential_exfil", lambda _job: None
    )
    outcome = cron_scheduler.run_job(
        {
            "id": "verify-complete",
            "name": "Verification",
            "prompt": "Do the thing",
            "schedule_display": "manual",
        }
    )
    assert outcome[0] is (expected_error is None), outcome[3]
    if expected_error:
        assert expected_error in outcome[3]
    return instances


@pytest.fixture(autouse=True)
def _clean_state():
    reset_session_vars()
    _RecordingSessionDB.next_lifecycle = "complete"
    yield
    reset_session_vars()


def test_run_without_final_assistant_message_books_incomplete(monkeypatch, tmp_path):
    """Last row a tool result / pending call (lifecycle 'interrupted') must
    not surface as a healthy complete run."""
    _RecordingSessionDB.next_lifecycle = "interrupted"

    instances = _run_booked_job(monkeypatch, tmp_path)

    assert instances, "SessionDB was never constructed"
    reasons = [reason for _sid, reason in instances[0].ended]
    assert reasons == ["cron_incomplete_no_output"]


def test_run_with_final_assistant_reply_books_complete(monkeypatch, tmp_path):
    """A real assistant reply (plain answer or [SILENT] — both assistant
    text rows) keeps the healthy booking."""
    instances = _run_booked_job(monkeypatch, tmp_path)

    reasons = [reason for _sid, reason in instances[0].ended]
    assert reasons == ["cron_complete"]


def test_classification_probe_failure_keeps_historical_reason(monkeypatch, tmp_path):
    """Best-effort metadata: a failing classifier must not mislabel a run."""
    _RecordingSessionDB.next_lifecycle = RuntimeError("db busy")

    instances = _run_booked_job(monkeypatch, tmp_path)

    reasons = [reason for _sid, reason in instances[0].ended]
    assert reasons == ["cron_complete"]


@pytest.mark.parametrize("lifecycle", ["complete", "interrupted", RuntimeError("metadata probe unavailable")])
@pytest.mark.parametrize("failed,completed", [(True, False), (False, False)])
def test_reported_failure_never_books_healthy_session(monkeypatch, tmp_path, lifecycle, failed, completed):
    _RecordingSessionDB.next_lifecycle = lifecycle
    monkeypatch.setattr(_FakeCronAgent, "run_conversation", lambda self, prompt, **kwargs: {
        "failed": failed, "completed": completed,
        "final_response": "Synthetic provider usage limit; review was interrupted.",
        "turn_exit_reason": "provider_error",
    })
    instances = _run_booked_job(monkeypatch, tmp_path, expected_error="Synthetic provider usage limit")
    assert [reason for _, reason in instances[0].ended] == ["cron_failed"]


def test_quota_error_reply_persists_failed_session_in_real_sqlite(monkeypatch, tmp_path):
    from hermes_state import SessionDB
    path = tmp_path / "sessions.db"

    class DiskDB(SessionDB):
        def __init__(self, *args, **kwargs):
            super().__init__(path)

    def initialize(self, *args, session_db, session_id, **kwargs):
        self.db, self.session_id = session_db, session_id
        self.db.create_session(session_id, "cron")

    def interrupted(self, prompt, **kwargs):
        self.db.append_message(self.session_id, "user", "Synthetic scheduled review")
        self.db.append_message(self.session_id, "assistant", "Synthetic provider usage limit")
        return {"failed": True, "completed": False, "final_response": "Synthetic provider usage limit"}

    monkeypatch.setattr(_FakeCronAgent, "__init__", initialize)
    monkeypatch.setattr(_FakeCronAgent, "run_conversation", interrupted)
    _run_booked_job(monkeypatch, tmp_path, db_type=DiskDB, expected_error="Synthetic provider usage limit")
    with sqlite3.connect(path) as observer:
        assert observer.execute("SELECT source,end_reason FROM sessions").fetchall() == [("cron", "cron_failed")]
        assert observer.execute("SELECT role FROM messages ORDER BY id").fetchall() == [("user",), ("assistant",)]

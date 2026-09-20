"""Durable model-facing result reads: ownership, restart and no delivery effects."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tools import async_delegation as ad
from tools import delegate_tool as dt
from tools.process_registry import process_registry
from tools.registry import registry


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


def seed(owner="owner", state="completed", delivery="pending"):
    ad._persist_dispatch({"delegation_id": "deleg-test", "parent_session_id": owner,
                          "origin_session_id": "transport-wake-id",
                          "session_key": "routing-only", "dispatched_at": time.time()})
    result = {"summary": "saved synthetic result", "exit_reason": "iteration_limit",
              "truncated": True, "structured_output": {"count": 2}}
    ad._persist_completion({"delegation_id": "deleg-test", "status": state}, result)
    with ad._transaction() as conn:
        conn.execute("UPDATE async_delegations SET delivery_state=?", (delivery,))
    return result


def lookup(owner="owner", delegation_id="deleg-test"):
    return json.loads(registry.get_entry("delegate_task").handler(
        {"action": "result", "delegation_id": delegation_id},
        parent_agent=SimpleNamespace(session_id=owner)))


@pytest.mark.parametrize("state,delivery", [
    ("completed", "pending"), ("completed", "delivered"),
    ("error", "dropped"), ("unknown", "pending"), ("timeout", "pending")])
def test_preserves_result_and_execution_delivery_states_without_writes(state, delivery):
    result = seed(state=state, delivery=delivery)
    before = ad._db_path().read_bytes()
    receipt = lookup()
    assert receipt["state"] == state
    assert receipt["delivery_state"] == delivery
    assert receipt["result"] == result
    assert ad._db_path().read_bytes() == before


@pytest.mark.parametrize("record_owner,caller", [("foreign", "owner"), ("", "owner"), ("owner", "")])
def test_foreign_or_unbound_owner_never_discloses_result(record_owner, caller):
    seed(owner=record_owner)
    response = lookup(owner=caller)
    assert "error" in response
    assert "saved synthetic result" not in json.dumps(response)
    assert response == lookup(owner=caller, delegation_id="absent")


def test_missing_database_read_does_not_create_home():
    path = ad._db_path()
    assert not path.parent.exists()
    assert "error" in lookup()
    assert not path.parent.exists()


def test_existing_nonledger_database_is_not_migrated():
    path = ad._db_path()
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE unrelated(value TEXT)")
    before = path.read_bytes()
    assert "error" in lookup()
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", [None, "", " ", 7, ["deleg-test"]])
def test_bad_id_fails_without_creating_database(value):
    assert "error" in lookup(delegation_id=value)
    assert not ad._db_path().exists()


def test_fresh_process_recovers_without_live_registry():
    expected = seed()
    code = """
import json
from types import SimpleNamespace
from tools.delegate_tool import delegate_task
print(delegate_task(action='result', delegation_id='deleg-test',
                    parent_agent=SimpleNamespace(session_id='owner')))
"""
    proc = subprocess.run([sys.executable, "-c", code], env=dict(os.environ),
                          text=True, capture_output=True, timeout=20, check=True)
    assert json.loads(proc.stdout)["result"] == expected


def test_compression_continuation_can_read_but_foreign_parent_cannot():
    from hermes_state import SessionDB
    ad._db_path().parent.mkdir()
    db = SessionDB(ad._db_path())
    try:
        db.create_session("owner", "cli")
        db.end_session("owner", "compression")
        db.create_session("continued", "cli", parent_session_id="owner")
        db.append_message("continued", "user", "continue synthetic task")
        expected = seed()
        for caller, allowed in [("continued", True), ("foreign", False)]:
            response = json.loads(dt.delegate_task(
                action="result", delegation_id="deleg-test",
                parent_agent=SimpleNamespace(session_id=caller, _session_db=db)))
            if allowed:
                assert response["result"] == expected
            else:
                assert "error" in response
                assert "saved synthetic result" not in json.dumps(response)
    finally:
        db.close()


def test_read_sees_committed_wal_result_without_checkpoint():
    seed()
    with sqlite3.connect(ad._db_path()) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE async_delegations SET result_json=?",
                       (json.dumps({"summary": "latest WAL receipt"}),))
        writer.commit()
        assert ad._db_path().with_name("state.db-wal").stat().st_size > 0
        assert lookup()["result"]["summary"] == "latest WAL receipt"


def test_actual_async_runner_read_does_not_ack_or_repeat():
    release = threading.Event()
    calls = []
    def runner():
        calls.append("called")
        assert release.wait(5)
        return {"status": "completed", "summary": "finished once"}
    dispatch = ad.dispatch_async_delegation(
        goal="synthetic", context=None, toolsets=None, role="leaf", model=None,
        session_key="route", parent_session_id="owner", runner=runner)
    did = dispatch["delegation_id"]
    try:
        pending = lookup(delegation_id=did)
        assert pending["state"] == "running"
        assert pending["result"] is None
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while ad.active_count() and time.monotonic() < deadline:
        time.sleep(.01)
    assert ad.active_count() == 0
    before = ad.get_durable_delegation(did)
    queued = process_registry.completion_queue.qsize()
    for _ in range(2):
        assert lookup(delegation_id=did)["result"]["summary"] == "finished once"
    assert ad.get_durable_delegation(did) == before
    assert process_registry.completion_queue.qsize() == queued
    assert calls == ["called"]

from __future__ import annotations

import os
import time
from pathlib import Path

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from agent.memory_write_outbox import MemoryWriteOutbox


def _enqueue(outbox: MemoryWriteOutbox, event_id: str, *, content: str = "fact", now=100.0):
    return outbox.enqueue(
        event_id=event_id,
        provider="external",
        action="add",
        target="memory",
        content=content,
        metadata={"tool_call_id": event_id},
        now=now,
    )


def test_enqueue_claim_complete_and_receipt_deduplicate(tmp_path):
    outbox = MemoryWriteOutbox(tmp_path / "outbox.sqlite3")
    assert _enqueue(outbox, "evt-1") == "enqueued"
    assert _enqueue(outbox, "evt-1") == "duplicate"

    events = outbox.claim_due("external", lease_owner="worker-1", now=100.0)
    assert [event.event_id for event in events] == ["evt-1"]
    assert events[0].metadata == {"tool_call_id": "evt-1"}
    assert outbox.complete("evt-1", lease_owner="worker-1", now=101.0) is True
    assert outbox.stats() == {"pending": 0, "payload_bytes": 0}
    assert _enqueue(outbox, "evt-1", now=102.0) == "duplicate"


def test_failed_delivery_releases_lease_and_retries(tmp_path):
    outbox = MemoryWriteOutbox(
        tmp_path / "outbox.sqlite3",
        retry_base_seconds=5.0,
    )
    _enqueue(outbox, "evt-1")
    assert outbox.claim_due("external", lease_owner="worker-1", now=100.0)
    assert outbox.fail(
        "evt-1",
        lease_owner="worker-1",
        error="offline",
        now=100.0,
    )
    assert outbox.claim_due("external", lease_owner="worker-2", now=104.9) == []
    retried = outbox.claim_due("external", lease_owner="worker-2", now=105.0)
    assert retried[0].attempts == 1


def test_active_lease_prevents_concurrent_claim(tmp_path):
    outbox = MemoryWriteOutbox(tmp_path / "outbox.sqlite3", lease_seconds=10.0)
    _enqueue(outbox, "evt-1")
    assert outbox.claim_due("external", lease_owner="worker-1", now=100.0)
    assert outbox.claim_due("external", lease_owner="worker-2", now=109.9) == []
    claimed = outbox.claim_due("external", lease_owner="worker-2", now=110.0)
    assert [event.event_id for event in claimed] == ["evt-1"]


def test_claims_preserve_enqueue_order(tmp_path):
    outbox = MemoryWriteOutbox(tmp_path / "outbox.sqlite3")
    _enqueue(outbox, "evt-later", now=101.0)
    _enqueue(outbox, "evt-first", now=100.0)
    first = outbox.claim_due("external", lease_owner="worker", now=102.0, limit=1)
    assert [event.event_id for event in first] == ["evt-first"]
    outbox.complete("evt-first", lease_owner="worker", now=102.0)
    second = outbox.claim_due("external", lease_owner="worker", now=102.0, limit=1)
    assert [event.event_id for event in second] == ["evt-later"]


def test_count_bound_preserves_existing_work(tmp_path):
    outbox = MemoryWriteOutbox(tmp_path / "outbox.sqlite3", max_entries=1)
    assert _enqueue(outbox, "evt-1") == "enqueued"
    assert _enqueue(outbox, "evt-2") == "full"
    assert outbox.stats()["pending"] == 1


def test_byte_bound_rejects_oversize_record(tmp_path):
    outbox = MemoryWriteOutbox(tmp_path / "outbox.sqlite3", max_payload_bytes=1024)
    assert _enqueue(outbox, "evt-large", content="x" * 2048) == "oversize"
    assert outbox.stats()["pending"] == 0


def test_expired_pending_and_receipts_are_purged(tmp_path):
    outbox = MemoryWriteOutbox(
        tmp_path / "outbox.sqlite3",
        max_age_seconds=60.0,
    )
    _enqueue(outbox, "pending-old", now=100.0)
    _enqueue(outbox, "delivered-old", now=100.0)
    outbox.claim_due("external", lease_owner="worker", now=100.0, limit=10)
    outbox.complete("delivered-old", lease_owner="worker", now=101.0)

    assert outbox.purge_expired(now=161.1) == 1
    assert _enqueue(outbox, "delivered-old", now=162.0) == "enqueued"


def test_database_permissions_are_owner_only(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    MemoryWriteOutbox(path)
    assert os.stat(path).st_mode & 0o777 == 0o600


class _WriteProvider(MemoryProvider):
    def __init__(self, *, fail: bool = False, legacy: bool = False):
        self.fail = fail
        self.legacy = legacy
        self.calls = []

    @property
    def name(self):
        return "external"

    def initialize(self, session_id: str = "", **kwargs):
        return None

    def is_available(self):
        return True

    def system_prompt_block(self):
        return ""

    def prefetch(self, query, *, session_id: str = ""):
        return ""

    def sync_turn(self, user_content, assistant_content, *, session_id: str = ""):
        return None

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        return ""

    def shutdown(self):
        return None

    def on_memory_write(self, action, target, content, metadata=None):
        if self.fail:
            raise RuntimeError("provider offline")
        self.calls.append((action, target, content, dict(metadata or {})))


class _LegacyWriteProvider(_WriteProvider):
    def on_memory_write(self, action, target, content):
        if self.fail:
            raise RuntimeError("provider offline")
        self.calls.append((action, target, content))


class _TransientWriteProvider(_WriteProvider):
    def __init__(self):
        super().__init__()
        self.attempts = 0

    def on_memory_write(self, action, target, content, metadata=None):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("temporary outage")
        super().on_memory_write(action, target, content, metadata=metadata)


def _initialized_manager(home: Path, provider: MemoryProvider):
    manager = MemoryManager(
        write_outbox_retry_base_seconds=0.0,
        write_outbox_retry_max_seconds=0.0,
    )
    manager.add_provider(provider)
    assert manager.initialize_all("session-1", hermes_home=str(home)) == 1
    assert manager.flush_pending(timeout=5.0)
    return manager


def test_manager_enqueues_then_delivers_with_event_metadata(tmp_path):
    provider = _WriteProvider()
    manager = _initialized_manager(tmp_path, provider)
    manager.on_memory_write(
        "add",
        "memory",
        "durable fact",
        metadata={"tool_call_id": "call-1", "_outbox_operation_index": 0},
    )
    assert manager.flush_pending(timeout=5.0)

    assert len(provider.calls) == 1
    metadata = provider.calls[0][3]
    assert metadata["outbox_event_id"].startswith("mw_")
    assert metadata["delivery_semantics"] == "at-least-once"
    assert metadata["delivery_attempt"] == 1
    assert "_outbox_operation_index" not in metadata
    assert manager.provider_health()["external"]["write_outbox_pending"] == 0


def test_manager_replays_failed_write_after_restart(tmp_path):
    failing = _WriteProvider(fail=True)
    first = _initialized_manager(tmp_path, failing)
    first.on_memory_write(
        "replace",
        "user",
        "updated preference",
        metadata={"tool_call_id": "call-replay"},
    )
    assert first.flush_pending(timeout=5.0)
    assert first.provider_health()["external"]["write_outbox_pending"] == 1
    first.shutdown_all()

    recovered = _WriteProvider()
    second = _initialized_manager(tmp_path, recovered)
    assert recovered.calls[0][:3] == (
        "replace",
        "user",
        "updated preference",
    )
    assert recovered.calls[0][3]["delivery_attempt"] == 2
    assert second.provider_health()["external"]["write_outbox_pending"] == 0


def test_manager_retries_transient_failure_without_new_write(tmp_path):
    provider = _TransientWriteProvider()
    manager = MemoryManager(
        write_outbox_retry_base_seconds=0.05,
        write_outbox_retry_max_seconds=0.05,
    )
    manager.add_provider(provider)
    assert manager.initialize_all("session-1", hermes_home=str(tmp_path)) == 1
    manager.on_memory_write(
        "add",
        "memory",
        "retry me",
        metadata={"tool_call_id": "transient-call"},
    )
    time.sleep(0.15)
    assert manager.flush_pending(timeout=5.0)
    assert provider.attempts == 2
    assert len(provider.calls) == 1
    assert manager.provider_health()["external"]["write_outbox_pending"] == 0


def test_manager_receipt_deduplicates_same_tool_operation(tmp_path):
    provider = _WriteProvider()
    manager = _initialized_manager(tmp_path, provider)
    metadata = {"tool_call_id": "same-call", "_outbox_operation_index": 2}
    manager.on_memory_write("add", "memory", "same fact", metadata=metadata)
    assert manager.flush_pending(timeout=5.0)
    manager.on_memory_write("add", "memory", "same fact", metadata=metadata)
    assert manager.flush_pending(timeout=5.0)
    assert len(provider.calls) == 1


def test_manager_keeps_legacy_provider_compatible(tmp_path):
    provider = _LegacyWriteProvider()
    manager = _initialized_manager(tmp_path, provider)
    manager.on_memory_write(
        "remove",
        "memory",
        "",
        metadata={"tool_call_id": "legacy-call", "old_text": "obsolete"},
    )
    assert manager.flush_pending(timeout=5.0)
    assert provider.calls == [("remove", "memory", "")]


def test_manager_falls_back_cleanly_when_outbox_is_corrupt(tmp_path):
    runtime_dir = tmp_path / "memory"
    runtime_dir.mkdir()
    (runtime_dir / "external-memory-write-outbox.sqlite3").write_text("not sqlite")
    provider = _WriteProvider()
    manager = MemoryManager()
    manager.add_provider(provider)
    assert manager.initialize_all("session-1", hermes_home=str(tmp_path)) == 1

    manager.on_memory_write("add", "memory", "direct fallback")
    assert provider.calls[0][:3] == ("add", "memory", "direct fallback")
    assert manager._write_outbox is None


def test_manager_overflow_is_visible_and_preserves_older_event(tmp_path):
    provider = _WriteProvider(fail=True)
    manager = MemoryManager(
        write_outbox_max_entries=1,
        write_outbox_retry_base_seconds=0.0,
        write_outbox_retry_max_seconds=0.0,
    )
    manager.add_provider(provider)
    assert manager.initialize_all("session-1", hermes_home=str(tmp_path)) == 1
    manager.on_memory_write(
        "add",
        "memory",
        "older durable event",
        metadata={"tool_call_id": "older"},
    )
    assert manager.flush_pending(timeout=5.0)
    provider.fail = False
    manager.on_memory_write(
        "add",
        "memory",
        "new direct fallback",
        metadata={"tool_call_id": "newer"},
    )

    health = manager.provider_health()["external"]
    assert health["write_outbox_pending"] == 1
    assert health["write_outbox_rejections"] == 1
    assert provider.calls[0][:3] == (
        "add",
        "memory",
        "new direct fallback",
    )

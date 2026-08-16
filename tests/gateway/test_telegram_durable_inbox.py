from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_INBOX_PATH = (
    Path(__file__).parents[2] / "plugins" / "platforms" / "telegram" / "inbox.py"
)
_SPEC = importlib.util.spec_from_file_location("telegram_inbox_under_test", _INBOX_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_INBOX_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _INBOX_MODULE
_SPEC.loader.exec_module(_INBOX_MODULE)
TelegramInbox = _INBOX_MODULE.TelegramInbox


def _adapter_types():
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import (
        TelegramAdapter,
        _DurableTelegramUpdateQueue,
    )

    return PlatformConfig, TelegramAdapter, _DurableTelegramUpdateQueue


def _update(update_id: int, text: str = "hello") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1,
            "chat": {"id": 7, "type": "private"},
            "from": {"id": 9, "is_bot": False, "first_name": "A"},
            "text": text,
        },
    }


def _store(tmp_path, *, profile="default", token="token", clock=None):
    kwargs = {"clock": clock} if clock is not None else {}
    return TelegramInbox(
        tmp_path / "telegram.sqlite3",
        profile_id=profile,
        bot_token=token,
        **kwargs,
    )


def test_duplicate_provider_retry_is_idempotent_and_payload_change_fails(tmp_path):
    inbox = _store(tmp_path)
    assert inbox.persist_update(
        _update(10), transport="polling_tls", authenticated=True
    )
    assert not inbox.persist_update(
        _update(10), transport="polling_tls", authenticated=True
    )
    assert inbox.counts()["inbox"] == 1

    with pytest.raises(ValueError, match="payload changed"):
        inbox.persist_update(
            _update(10, "forged replacement"),
            transport="polling_tls",
            authenticated=True,
        )


def test_out_of_order_gap_closes_only_after_missing_update_is_durable(tmp_path):
    inbox = _store(tmp_path)
    inbox.persist_updates(
        [_update(100), _update(102)],
        transport="polling_tls",
        authenticated=True,
    )
    assert inbox.checkpoint()["highest_seen_update_id"] == 102
    assert inbox.checkpoint()["highest_contiguous_update_id"] == 100
    assert inbox.checkpoint()["gap_after_update_id"] == 100

    inbox.persist_update(
        _update(101), transport="polling_tls", authenticated=True
    )
    assert inbox.checkpoint()["highest_contiguous_update_id"] == 102
    assert inbox.checkpoint()["gap_after_update_id"] is None


def test_replay_record_retains_authenticated_routing_provenance(tmp_path):
    inbox = _store(tmp_path)
    payload = _update(103)
    payload["message"]["message_thread_id"] = 44
    inbox.persist_update(
        payload, transport="webhook_secret", authenticated=True
    )

    record = inbox.record(103)
    assert record["payload"] == payload
    assert record["provider_sender_id"] == "9"
    assert record["provider_chat_id"] == "7"
    assert record["provider_thread_id"] == "44"
    assert record["transport"] == "webhook_secret"
    assert len(record["archive_id"]) == 64


def test_invalid_or_unauthenticated_webhook_payload_never_enters_inbox(tmp_path):
    inbox = _store(tmp_path)
    with pytest.raises(PermissionError):
        inbox.persist_update(
            _update(1), transport="webhook_secret", authenticated=False
        )
    with pytest.raises(ValueError, match="update_id"):
        inbox.persist_update(
            {"message": {}}, transport="webhook_secret", authenticated=True
        )
    assert inbox.counts()["inbox"] == 0


def test_profile_and_bot_scopes_do_not_cross_deduplicate(tmp_path):
    first = _store(tmp_path, profile="one", token="token-a")
    second = _store(tmp_path, profile="two", token="token-a")
    third = _store(tmp_path, profile="one", token="token-b")
    for inbox in (first, second, third):
        assert inbox.persist_update(
            _update(5), transport="polling_tls", authenticated=True
        )
        assert inbox.counts()["inbox"] == 1

    secret = "super-secret-bot-token"
    secret_store = _store(tmp_path, profile="secret", token=secret)
    secret_store.persist_update(
        _update(6), transport="polling_tls", authenticated=True
    )
    assert secret.encode("utf-8") not in secret_store.db_path.read_bytes()


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode contract")
def test_inbox_directory_and_database_are_owner_private(tmp_path):
    parent = tmp_path / "gateway"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)

    inbox = TelegramInbox(
        parent / "telegram.sqlite3",
        profile_id="default",
        bot_token="token",
    )

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(inbox.db_path.stat().st_mode) == 0o600


def test_symlinked_inbox_file_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"not-a-database")
    link = tmp_path / "telegram.sqlite3"
    link.symlink_to(target)

    with pytest.raises(RuntimeError, match="securely open Telegram inbox"):
        _store(tmp_path)

    assert target.read_bytes() == b"not-a-database"


def test_schema_attests_inbox_checkpoint_archive_dead_letter_and_effects(tmp_path):
    inbox = _store(tmp_path)
    with inbox._connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "telegram_inbox",
            "telegram_checkpoints",
            "telegram_archive",
            "telegram_dead_letters",
            "telegram_effects",
        } <= tables
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(telegram_inbox)")
        }
        assert {
            "update_id",
            "archive_id",
            "provider_sender_id",
            "provider_chat_id",
            "provider_thread_id",
            "effect_key",
            "lease_owner",
            "lease_expires_at",
        } <= columns


def test_crash_after_persist_recovers_and_completed_effect_survives_pruning(tmp_path):
    now = [100.0]
    inbox = _store(tmp_path, clock=lambda: now[0])
    inbox.persist_update(
        _update(20), transport="polling_tls", authenticated=True
    )
    assert inbox.recoverable() == [_update(20)]

    claim = inbox.claim(20, lease_owner="worker")
    assert claim is not None
    assert inbox.complete(claim)
    now[0] = 1000.0
    assert inbox.prune_archive(retention_seconds=1) == 1

    # Provider retry after archive retention is still suppressed by the
    # permanent committed-effect tombstone.
    assert not inbox.persist_update(
        _update(20), transport="polling_tls", authenticated=True
    )
    assert inbox.counts()["inbox"] == 0
    assert inbox.counts()["effects"] == 1


def test_expired_claim_retries_then_dead_letters(tmp_path):
    now = [10.0]
    inbox = _store(tmp_path, clock=lambda: now[0])
    inbox.persist_update(
        _update(30), transport="polling_tls", authenticated=True
    )
    first = inbox.claim(30, lease_seconds=2, lease_owner="crashed")
    assert first is not None
    assert inbox.claim(30, lease_owner="other") is None

    now[0] = 13.0
    second = inbox.claim(30, lease_owner="recovery")
    assert second is not None and second.attempt == 2
    assert inbox.fail(second, RuntimeError("temporary"), retry_delay=5) == "retry"
    assert inbox.recoverable() == []
    now[0] = 19.0
    third = inbox.claim(30, lease_owner="final")
    assert third is not None and third.attempt == 3
    assert inbox.fail(third, RuntimeError("permanent"), max_attempts=3) == "dead_letter"
    assert inbox.counts()["dead_letter"] == 1
    assert inbox.counts()["inbox"] == 0
    assert inbox.replay_dead_letter(30)
    assert inbox.counts()["dead_letter"] == 0
    assert inbox.recoverable() == [_update(30)]


def test_cold_start_reclaims_prior_process_inflight_lease(tmp_path):
    inbox = _store(tmp_path)
    inbox.persist_update(
        _update(31), transport="polling_tls", authenticated=True
    )
    assert inbox.claim(31, lease_seconds=3600, lease_owner="dead-process")

    assert inbox.requeue_inflight() == 1
    assert inbox.recoverable() == [_update(31)]
    assert inbox.claim(31, lease_owner="new-process").attempt == 2


def test_failure_after_effect_fence_is_quarantined_without_retry(tmp_path):
    inbox = _store(tmp_path)
    inbox.persist_update(_update(32), transport="polling_tls", authenticated=True)
    claim = inbox.claim(32, lease_owner="worker")
    assert claim is not None
    assert inbox.begin_effects([claim])
    assert not inbox.begin_effects([claim])

    assert inbox.fail(claim, "provider outcome unknown") == "dead_letter"
    assert inbox.recoverable() == []
    assert inbox.record(32)["state"] == "dead_letter"


def test_provider_retry_cannot_steal_expired_effect_claim(tmp_path):
    now = [10.0]
    inbox = _store(tmp_path, clock=lambda: now[0])
    inbox.persist_update(_update(35), transport="polling_tls", authenticated=True)
    claim = inbox.claim(35, lease_seconds=2, lease_owner="original")
    assert claim is not None
    assert inbox.begin_effects([claim])

    now[0] = 100.0
    assert inbox.claim(35, lease_owner="provider-retry") is None
    assert inbox.complete(claim)


def test_cold_start_quarantines_crash_after_effect_fence(tmp_path):
    inbox = _store(tmp_path)
    inbox.persist_update(_update(33), transport="polling_tls", authenticated=True)
    claim = inbox.claim(33, lease_owner="crashed-worker")
    assert claim is not None
    assert inbox.begin_effects([claim])

    restarted = _store(tmp_path)
    assert restarted.requeue_inflight() == 1
    assert restarted.recoverable() == []
    assert restarted.record(33)["state"] == "dead_letter"
    # A provider retry cannot recreate the quarantined row while the durable
    # claimed receipt exists.
    assert not restarted.persist_update(
        _update(33), transport="polling_tls", authenticated=True
    )


def test_operator_replay_explicitly_clears_ambiguous_effect_fence(tmp_path):
    inbox = _store(tmp_path)
    inbox.persist_update(_update(34), transport="polling_tls", authenticated=True)
    claim = inbox.claim(34, lease_owner="worker")
    assert claim is not None
    assert inbox.begin_effects([claim])
    assert inbox.fail(claim, "ambiguous") == "dead_letter"

    assert inbox.replay_dead_letter(34)
    replay = inbox.claim(34, lease_owner="operator-replay")
    assert replay is not None
    assert inbox.begin_effects([replay])
    assert inbox.complete(replay)
    assert inbox.record(34)["state"] == "archive"


def test_polling_response_is_persisted_before_progress_is_recorded():
    PlatformConfig, TelegramAdapter, _ = _adapter_types()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    calls = []
    store = MagicMock()
    store.persist_updates.side_effect = lambda *a, **k: calls.append("persist")
    adapter._telegram_inbox = store
    adapter._record_polling_progress = lambda generation: calls.append("progress")
    request = SimpleNamespace(
        parse_json_payload=lambda payload: json.loads(payload.decode("utf-8"))
    )
    payload = json.dumps({"ok": True, "result": [_update(40)]}).encode()

    adapter._observe_polling_request_result(request, 1, (200, payload))

    assert calls == ["persist", "progress"]
    store.persist_updates.assert_called_once_with(
        [_update(40)], transport="polling_tls", authenticated=True
    )


def test_polling_db_failure_prevents_progress_and_escapes_to_ptb_retry():
    PlatformConfig, TelegramAdapter, _ = _adapter_types()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    store = MagicMock()
    store.persist_updates.side_effect = OSError("disk unavailable")
    adapter._telegram_inbox = store
    adapter._record_polling_progress = MagicMock()
    request = SimpleNamespace(
        parse_json_payload=lambda payload: json.loads(payload.decode("utf-8"))
    )
    payload = json.dumps({"ok": True, "result": [_update(41)]}).encode()

    with pytest.raises(OSError, match="disk unavailable"):
        adapter._observe_polling_request_result(request, 1, (200, payload))
    adapter._record_polling_progress.assert_not_called()


@pytest.mark.asyncio
async def test_authenticated_queue_persists_before_handler_visibility():
    PlatformConfig, TelegramAdapter, _DurableTelegramUpdateQueue = _adapter_types()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._inbound_transport = "webhook_secret"
    calls = []
    adapter._persist_inbound_payload = lambda payload, transport: calls.append(
        (payload["update_id"], transport)
    )
    queue = _DurableTelegramUpdateQueue(adapter)
    item = SimpleNamespace(to_dict=lambda: _update(50))

    queue.put_nowait(item)

    assert calls == [(50, "webhook_secret")]
    assert queue.get_nowait() is item


def test_webhook_db_failure_keeps_update_out_of_handler_queue():
    PlatformConfig, TelegramAdapter, _DurableTelegramUpdateQueue = _adapter_types()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._inbound_transport = "webhook_secret"
    adapter._persist_inbound_payload = MagicMock(
        side_effect=OSError("database unavailable")
    )
    queue = _DurableTelegramUpdateQueue(adapter)
    item = SimpleNamespace(to_dict=lambda: _update(51))

    with pytest.raises(OSError, match="database unavailable"):
        queue.put_nowait(item)
    assert queue.empty()


@pytest.mark.asyncio
async def test_durable_handler_claims_once_across_provider_retry(tmp_path):
    PlatformConfig, TelegramAdapter, _ = _adapter_types()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._telegram_inbox = _store(tmp_path)
    adapter._inbound_transport = "polling_tls"
    adapter._telegram_inbox.persist_update(
        _update(60), transport="polling_tls", authenticated=True
    )
    effects = []

    async def effect(update, context):
        effects.append(update.to_dict()["update_id"])

    wrapped = adapter._durable_handler(effect)
    update = SimpleNamespace(to_dict=lambda: _update(60))
    await wrapped(update, None)
    await wrapped(update, None)

    assert effects == [60]
    assert adapter._telegram_inbox.counts()["archive"] == 1


@pytest.mark.asyncio
async def test_batched_effect_is_not_archived_before_delayed_dispatch(tmp_path):
    PlatformConfig, TelegramAdapter, _ = _adapter_types()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._telegram_inbox = _store(tmp_path)
    adapter._telegram_inbox.persist_update(
        _update(61), transport="polling_tls", authenticated=True
    )
    event = SimpleNamespace(platform_update_id=61, metadata={})

    async def enqueue(update, context):
        adapter._mark_event_inbox_deferred(event)

    wrapped = adapter._durable_handler(enqueue)
    update = SimpleNamespace(update_id=61, to_dict=lambda: _update(61))
    await wrapped(update, None)
    assert adapter._telegram_inbox.counts()["inbox"] == 1
    assert adapter._telegram_inbox.counts()["archive"] == 0

    assert await adapter._begin_inbound_effect(event)
    await adapter._finish_inbound_effect(event, success=True)
    assert adapter._telegram_inbox.counts()["inbox"] == 0
    assert adapter._telegram_inbox.counts()["archive"] == 1

from __future__ import annotations

import importlib.util
import asyncio
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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


def test_duplicate_replay_batch_is_capacity_neutral(tmp_path, monkeypatch):
    monkeypatch.setattr(_INBOX_MODULE, "MAX_ACTIVE_OR_QUARANTINED_ROWS", 2)
    inbox = _store(tmp_path)
    assert inbox.persist_updates(
        [_update(10), _update(11)],
        transport="polling_tls",
        authenticated=True,
    ) == 2
    for update_id in (10, 11):
        claim = inbox.claim(update_id, lease_owner=f"worker-{update_id}")
        assert claim and inbox.complete(claim)

    assert inbox.persist_updates(
        [_update(10), _update(11)],
        transport="polling_tls",
        authenticated=True,
    ) == 0
    with pytest.raises(RuntimeError, match="capacity reached"):
        inbox.persist_update(
            _update(12), transport="polling_tls", authenticated=True
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
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
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
            "telegram_epoch_history",
            "telegram_inbox_operator_audit",
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


def test_crash_after_persist_recovers_and_effect_prunes_only_after_reset_window(tmp_path):
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
    assert inbox.prune_archive(retention_seconds=1) == 0

    # Provider retries inside the seven-day ambiguity window are suppressed.
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
    assert inbox.replay_dead_letter(
        30, reason="reviewed transient failure", confirmation="REPLAY:30"
    )
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

    with pytest.raises(PermissionError):
        inbox.replay_dead_letter(34, reason="reviewed", confirmation="yes")
    assert inbox.replay_dead_letter(
        34, reason="owner accepted duplicate risk", confirmation="REPLAY:34"
    )
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


def test_authenticated_queue_persists_before_handler_visibility():
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


def test_durable_handler_claims_once_across_provider_retry(tmp_path):
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
    asyncio.run(wrapped(update, None))
    asyncio.run(wrapped(update, None))

    assert effects == [60]
    assert adapter._telegram_inbox.counts()["archive"] == 1


def test_batched_effect_is_not_archived_before_delayed_dispatch(tmp_path):
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
    asyncio.run(wrapped(update, None))
    assert adapter._telegram_inbox.counts()["inbox"] == 1
    assert adapter._telegram_inbox.counts()["archive"] == 0

    assert asyncio.run(adapter._begin_inbound_effect(event))
    asyncio.run(adapter._finish_inbound_effect(event, success=True))
    assert adapter._telegram_inbox.counts()["inbox"] == 0
    assert adapter._telegram_inbox.counts()["archive"] == 1


def _durable_busy_fixture(tmp_path, update_id: int, *, text: str = "follow up"):
    from gateway.platforms.base import (
        MessageEvent,
        MessageType,
        Platform,
        SendResult,
        SessionSource,
        build_session_key,
    )
    from gateway.run import GatewayRunner

    PlatformConfig, TelegramAdapter, _ = _adapter_types()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._telegram_inbox = _store(tmp_path)
    adapter._telegram_inbox.persist_update(
        _update(update_id, text), transport="polling_tls", authenticated=True
    )
    adapter._send_with_retry = AsyncMock(
        return_value=SendResult(success=True, message_id="provider-message-1")
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="7",
        chat_type="dm",
        user_id="9",
    )
    event = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id=str(update_id),
        platform_update_id=update_id,
    )
    session_key = build_session_key(source)

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._running_agents = {session_key: MagicMock()}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._restart_requested = False
    runner._busy_input_mode = "queue"
    runner._busy_text_mode = "interrupt"
    runner.session_store = None
    runner.config = MagicMock()
    runner._is_user_authorized = lambda _source: True
    return runner, adapter, event, session_key


async def _dispatch_durable_busy(adapter, runner, event, session_key):
    async def route(_update_obj, _context):
        adapter._mark_event_inbox_deferred(event)
        return await runner._handle_active_session_busy_message(event, session_key)

    wrapped = adapter._durable_handler(route)
    update_obj = SimpleNamespace(
        update_id=event.platform_update_id,
        to_dict=lambda: _update(event.platform_update_id, event.text),
    )
    return await wrapped(update_obj, None)


def _effect_state(adapter, update_id: int) -> str | None:
    with adapter._telegram_inbox._connect() as conn:
        row = conn.execute(
            """SELECT state FROM telegram_effects
               WHERE profile_id=? AND bot_id=? AND update_id=?""",
            (
                adapter._telegram_inbox.profile_id,
                adapter._telegram_inbox.bot_id,
                update_id,
            ),
        ).fetchone()
    return str(row[0]) if row is not None else None


@pytest.mark.asyncio
async def test_durable_busy_unauthorized_is_archived_and_cannot_replay(tmp_path):
    from gateway.platforms.base import BusyMessageDisposition

    runner, adapter, event, session_key = _durable_busy_fixture(tmp_path, 62)
    runner._is_user_authorized = lambda _source: False

    disposition = await _dispatch_durable_busy(
        adapter, runner, event, session_key
    )

    assert disposition is BusyMessageDisposition.CONSUMED
    assert adapter._telegram_inbox.counts()["archive"] == 1
    assert adapter._telegram_inbox.recoverable() == []
    adapter._send_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_busy_plain_approval_commits_only_after_receipt(tmp_path):
    from gateway.platforms.base import BusyMessageDisposition

    runner, adapter, event, session_key = _durable_busy_fixture(
        tmp_path, 63, text="yes"
    )
    runner._handle_approve_command = AsyncMock(return_value="Approved")

    with patch("tools.approval.has_blocking_approval", return_value=True):
        disposition = await _dispatch_durable_busy(
            adapter, runner, event, session_key
        )

    assert disposition is BusyMessageDisposition.CONSUMED
    runner._handle_approve_command.assert_awaited_once()
    adapter._send_with_retry.assert_awaited_once()
    assert adapter._telegram_inbox.counts()["archive"] == 1
    assert _effect_state(adapter, 63) == "committed"


@pytest.mark.asyncio
async def test_durable_busy_draining_rejection_is_fenced_and_archived(tmp_path):
    from gateway.platforms.base import BusyMessageDisposition

    runner, adapter, event, session_key = _durable_busy_fixture(tmp_path, 64)
    runner._draining = True
    runner._queue_during_drain_enabled = lambda _mode=None: False

    disposition = await _dispatch_durable_busy(
        adapter, runner, event, session_key
    )

    assert disposition is BusyMessageDisposition.CONSUMED
    assert "not accepting" in adapter._send_with_retry.await_args.kwargs["content"]
    assert adapter._telegram_inbox.counts()["archive"] == 1
    assert _effect_state(adapter, 64) == "committed"


@pytest.mark.asyncio
async def test_durable_busy_steer_is_queued_until_terminal_next_turn(tmp_path):
    from gateway.platforms.base import BusyMessageDisposition

    runner, adapter, event, session_key = _durable_busy_fixture(tmp_path, 65)
    runner._busy_input_mode = "steer"
    running_agent = runner._running_agents[session_key]
    running_agent.steer = MagicMock(return_value=True)

    disposition = await _dispatch_durable_busy(
        adapter, runner, event, session_key
    )

    assert disposition is BusyMessageDisposition.QUEUED
    running_agent.steer.assert_not_called()
    adapter._send_with_retry.assert_not_awaited()
    assert adapter._pending_messages[session_key] is event
    assert adapter._telegram_inbox.counts()["inbox"] == 1
    assert adapter._telegram_inbox.counts()["archive"] == 0

    # A process crash before the queued turn starts reclaims the persisted
    # update; no premature archive can lose the accepted steer text.
    adapter._telegram_inbox.requeue_inflight()
    assert [item["update_id"] for item in adapter._telegram_inbox.recoverable()] == [65]


@pytest.mark.asyncio
async def test_durable_busy_queue_suppresses_ack_and_retains_claim(tmp_path, monkeypatch):
    from gateway.platforms.base import BusyMessageDisposition

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    runner, adapter, event, session_key = _durable_busy_fixture(tmp_path, 66)
    runner._busy_input_mode = "interrupt"
    running_agent = runner._running_agents[session_key]

    disposition = await _dispatch_durable_busy(
        adapter, runner, event, session_key
    )

    assert disposition is BusyMessageDisposition.QUEUED
    running_agent.interrupt.assert_not_called()
    adapter._send_with_retry.assert_not_awaited()
    assert adapter._telegram_inbox.counts()["inbox"] == 1
    assert adapter._telegram_inbox.counts()["archive"] == 0


@pytest.mark.asyncio
async def test_durable_busy_full_queue_emits_fenced_rejection(tmp_path, monkeypatch):
    from gateway.platforms.base import BusyMessageDisposition

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    runner, adapter, event, session_key = _durable_busy_fixture(tmp_path, 67)
    adapter._pending_messages[session_key] = SimpleNamespace(
        message_type=event.message_type,
        media_urls=[],
    )
    runner._queued_events[session_key] = [
        MagicMock() for _ in range(runner._BUSY_QUEUE_MAX_PENDING - 1)
    ]

    disposition = await _dispatch_durable_busy(
        adapter, runner, event, session_key
    )

    assert disposition is BusyMessageDisposition.CONSUMED
    assert "queue is full" in adapter._send_with_retry.await_args.kwargs["content"]
    assert adapter._telegram_inbox.counts()["archive"] == 1
    assert _effect_state(adapter, 67) == "committed"


def _callback_payload(update_id: int) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": 9, "is_bot": False, "first_name": "Owner"},
            "message": {
                "message_id": update_id,
                "date": 1,
                "chat": {"id": 7, "type": "private"},
                "text": "Approve?",
            },
            "data": "ea:once:1",
        },
    }


@pytest.mark.asyncio
async def test_callback_crash_after_answer_is_quarantined_not_replayed(
    tmp_path, monkeypatch
):
    PlatformConfig, TelegramAdapter, _ = _adapter_types()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._telegram_inbox = _store(tmp_path)
    payload = _callback_payload(68)
    adapter._telegram_inbox.persist_update(
        payload, transport="polling_tls", authenticated=True
    )
    adapter._approval_state[1] = "session-key"
    adapter._is_callback_user_authorized = lambda *_args, **_kwargs: True
    adapter.resume_typing_for_chat = MagicMock(
        side_effect=RuntimeError("crash after callback effect")
    )
    query = SimpleNamespace(
        data="ea:once:1",
        message=SimpleNamespace(
            chat_id=7,
            chat=SimpleNamespace(type="private"),
            message_thread_id=None,
        ),
        from_user=SimpleNamespace(id=9, first_name="Owner"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update_obj = SimpleNamespace(
        update_id=68,
        callback_query=query,
        to_dict=lambda: payload,
    )
    monkeypatch.setattr(
        "tools.approval.resolve_gateway_approval", lambda *_args: 1
    )
    wrapped = adapter._durable_handler(adapter._handle_callback_query)

    with pytest.raises(RuntimeError, match="crash after callback effect"):
        await wrapped(update_obj, None)

    assert query.answer.await_count == 1
    assert adapter._telegram_inbox.counts()["dead_letter"] == 1
    assert adapter._telegram_inbox.counts()["archive"] == 0
    assert _effect_state(adapter, 68) == "claimed"

    # A provider retry cannot claim or repeat an ambiguous callback effect.
    assert await wrapped(update_obj, None) is None
    assert query.answer.await_count == 1


@pytest.mark.asyncio
async def test_callback_fence_storage_failure_retries_before_any_effect(tmp_path):
    PlatformConfig, TelegramAdapter, _ = _adapter_types()
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
    adapter._telegram_inbox = _store(tmp_path)
    payload = _callback_payload(69)
    adapter._telegram_inbox.persist_update(
        payload, transport="polling_tls", authenticated=True
    )
    query = SimpleNamespace(
        data="ea:once:1",
        message=SimpleNamespace(chat_id=7, chat=SimpleNamespace(type="private")),
        from_user=SimpleNamespace(id=9, first_name="Owner"),
        answer=AsyncMock(),
    )
    update_obj = SimpleNamespace(
        update_id=69,
        callback_query=query,
        to_dict=lambda: payload,
    )
    adapter._telegram_inbox.begin_effects = MagicMock(
        side_effect=OSError("effect database unavailable")
    )
    adapter._schedule_inbox_retry = MagicMock()
    wrapped = adapter._durable_handler(adapter._handle_callback_query)

    with pytest.raises(OSError, match="effect database unavailable"):
        await wrapped(update_obj, None)

    assert query.answer.await_count == 0
    assert adapter._telegram_inbox.counts()["dead_letter"] == 0
    with adapter._telegram_inbox._connect() as conn:
        retry_state = conn.execute(
            """SELECT state FROM telegram_inbox
               WHERE profile_id=? AND bot_id=? AND update_id=69""",
            (adapter._telegram_inbox.profile_id, adapter._telegram_inbox.bot_id),
        ).fetchone()[0]
    assert retry_state == "retry"
    adapter._schedule_inbox_retry.assert_called_once_with(1.0)


def test_sequence_epoch_resets_only_after_seven_days_idle(tmp_path):
    now = [10.0]
    inbox = _store(tmp_path, clock=lambda: now[0])
    assert inbox.persist_update(_update(900), transport="polling_tls", authenticated=True)
    claim = inbox.claim(900, lease_owner="worker")
    assert claim and inbox.complete(claim)

    now[0] += 1
    assert not inbox.persist_update(_update(900), transport="polling_tls", authenticated=True)
    assert inbox.checkpoint()["sequence_epoch"] == 0

    now[0] += _INBOX_MODULE.UPDATE_ID_RESET_IDLE_SECONDS + 1
    assert inbox.persist_update(_update(3), transport="polling_tls", authenticated=True)
    assert inbox.checkpoint()["sequence_epoch"] == 1
    assert inbox.record(3)["effect_key"].endswith(":1:3")
    assert inbox.counts()["epoch_history"] == 1
    with inbox._connect() as conn:
        historical = conn.execute(
            """SELECT sequence_epoch,update_id,payload_json,effect_state
               FROM telegram_epoch_history
               WHERE profile_id=? AND bot_id=?""",
            (inbox.profile_id, inbox.bot_id),
        ).fetchone()
    assert tuple(historical) == (
        0,
        900,
        json.dumps(_update(900), sort_keys=True, separators=(",", ":")),
        "committed",
    )


def test_exact_duplicate_after_idle_never_rotates_or_refreshes_epoch_clock(tmp_path):
    now = [10.0]
    inbox = _store(tmp_path, clock=lambda: now[0])
    assert inbox.persist_update(_update(900), transport="polling_tls", authenticated=True)
    claim = inbox.claim(900, lease_owner="worker")
    assert claim and inbox.complete(claim)
    checkpoint_updated_at = inbox.checkpoint()["updated_at"]

    now[0] += _INBOX_MODULE.UPDATE_ID_RESET_IDLE_SECONDS + 1
    assert not inbox.persist_update(
        _update(900), transport="polling_tls", authenticated=True
    )
    checkpoint = inbox.checkpoint()
    assert checkpoint["sequence_epoch"] == 0
    assert checkpoint["updated_at"] == checkpoint_updated_at
    assert inbox.counts()["archive"] == 1

    assert inbox.persist_update(_update(3), transport="polling_tls", authenticated=True)
    assert inbox.checkpoint()["sequence_epoch"] == 1


def test_sequence_epoch_reset_never_discards_unresolved_or_quarantined_work(tmp_path):
    now = [10.0]
    inbox = _store(tmp_path, clock=lambda: now[0])
    assert inbox.persist_update(_update(900), transport="polling_tls", authenticated=True)

    now[0] += _INBOX_MODULE.UPDATE_ID_RESET_IDLE_SECONDS + 1
    with pytest.raises(RuntimeError, match="owner review required"):
        inbox.persist_update(_update(3), transport="polling_tls", authenticated=True)

    assert inbox.record(900)["state"] == "inbox"
    assert inbox.record(3) is None
    assert inbox.checkpoint()["sequence_epoch"] == 0


def test_operator_audit_is_bounded_by_retention_and_row_cap(tmp_path, monkeypatch):
    now = [100.0]
    inbox = _store(tmp_path, clock=lambda: now[0])
    monkeypatch.setattr(_INBOX_MODULE, "DEFAULT_MAX_HISTORY_ROWS", 2)
    for index in range(3):
        inbox.audit_operator_action(f"inspect-{index}", reason="review")

    inbox.prune_archive(retention_seconds=30 * 24 * 60 * 60)

    assert inbox.counts()["operator_audit"] == 2


def test_pre_effect_failures_use_bounded_backoff_before_quarantine(tmp_path):
    now = [0.0]
    inbox = _store(tmp_path, clock=lambda: now[0])
    inbox.persist_update(_update(77), transport="polling_tls", authenticated=True)
    for attempt in range(1, 4):
        claim = inbox.claim(77, lease_owner=f"worker-{attempt}")
        assert claim and claim.attempt == attempt
        assert inbox.fail(
            claim,
            "transient before fence",
            max_attempts=4,
            retry_delay=2 ** (attempt - 1),
        ) == "retry"
        now[0] += 2 ** (attempt - 1)
    final = inbox.claim(77, lease_owner="worker-4")
    assert final and inbox.fail(final, "exhausted", max_attempts=4) == "dead_letter"


def test_adapter_retry_timer_honors_persisted_backoff_delay():
    PlatformConfig, TelegramAdapter, _ = _adapter_types()

    async def scenario():
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
        adapter._app = SimpleNamespace(bot=object())
        adapter._durable_update_queue = MagicMock()
        adapter._telegram_inbox = MagicMock()
        adapter._telegram_inbox.recoverable.return_value = []
        with patch(
            "plugins.platforms.telegram.adapter.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep:
            adapter._schedule_inbox_retry(8.0)
            await asyncio.gather(*list(adapter._background_tasks))
        sleep.assert_awaited_once_with(8.0)

    asyncio.run(scenario())

"""Tests for the gateway delivery-obligation ledger (gateway/delivery_ledger.py).

State machine, dead-owner claiming, attempts cap, stale cutoff, retention,
id stability, and the startup redelivery sweep's contract:
- pending rows redeliver plainly (send never started, no dup risk)
- attempting rows from dead owners are quarantined without provider resend;
  definitively failed rows carry the recovered-reply marker after their due time
- live-owner task leases protect active rows; expired pending work is reclaimed
  and expired attempting work is quarantined
- poison rows abandon at the attempts cap / stale cutoff
"""

import asyncio
import time
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import delivery_ledger as dl


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    """Isolated state.db per test (autouse HERMES_HOME isolation already
    redirects get_hermes_home; make the redirect explicit and per-test)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    yield


def _record(oid="ob-1", session_key="agent:main:slack:channel:C1", **kw):
    return dl.record_obligation(
        obligation_id=oid,
        session_key=session_key,
        platform=kw.get("platform", "slack"),
        chat_id=kw.get("chat_id", "C1"),
        thread_id=kw.get("thread_id", "171.001"),
        content=kw.get("content", "the final answer"),
    )


def _row(oid):
    with dl._connect() as conn:
        r = conn.execute(
            """SELECT state, attempts, owner_pid, content
               FROM delivery_obligations WHERE obligation_id=?""",
            (oid,),
        ).fetchone()
    return None if r is None else {
        "state": r[0], "attempts": r[1], "owner_pid": r[2], "content": r[3],
    }


def _blocking_probe():
    """Return a blocking ledger call and an event-loop progress witness."""
    ledger_started = threading.Event()
    event_loop_progressed = threading.Event()
    blocked_event_loop = []

    def _slow_ledger_call(*args, **kwargs):
        ledger_started.set()
        # Generous timeout: a genuinely blocked loop can never set the event
        # (the witness coroutine cannot run), so a longer wait only guards
        # against loaded-CI scheduling flake, not against missing the bug.
        if not event_loop_progressed.wait(timeout=5.0):
            blocked_event_loop.append(True)

    async def _event_loop_witness():
        import asyncio

        deadline = asyncio.get_running_loop().time() + 10
        while not ledger_started.is_set():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("ledger call never started")
            await asyncio.sleep(0)
        event_loop_progressed.set()

    return _slow_ledger_call, _event_loop_witness, blocked_event_loop


def _orphan(oid):
    """Make the row look like it belongs to a dead process."""
    with dl._connect() as conn:
        conn.execute(
            "UPDATE delivery_obligations SET owner_pid=999999999, "
            "owner_started_at=1 WHERE obligation_id=?",
            (oid,),
        )


class TestStateMachine:
    def test_record_starts_pending(self):
        _record()
        assert _row("ob-1")["state"] == "pending"

    def test_full_happy_path(self):
        token = _record()
        assert dl.mark_attempting("ob-1", token)
        assert _row("ob-1")["state"] == "attempting"
        assert dl.mark_delivered("ob-1", token)
        assert _row("ob-1")["state"] == "delivered"

    def test_failed_records_error(self):
        token = _record()
        assert dl.mark_attempting("ob-1", token)
        assert dl.mark_failed("ob-1", token, "chat_not_found")
        assert _row("ob-1")["state"] == "failed"

    def test_rerecord_same_id_preserves_active_generation(self):
        token = _record()
        assert dl.mark_attempting("ob-1", token)
        assert _record() == token
        assert _row("ob-1")["state"] == "attempting"


class TestObligationId:
    def test_stable_and_distinct(self):
        a = dl.compute_obligation_id("sk1", "msg1", "hello")
        assert a == dl.compute_obligation_id("sk1", "msg1", "hello")
        # Different thread (baked into session_key) → different id. This is
        # the cron-topic collision class from the earlier outbox attempt.
        assert a != dl.compute_obligation_id("sk1:threadB", "msg1", "hello")
        assert a != dl.compute_obligation_id("sk1", "msg2", "hello")
        assert a != dl.compute_obligation_id("sk1", "msg1", "other")
        assert len(a) == 24


class TestSweep:
    def test_fresh_live_owner_rows_not_claimed(self):
        _record()  # owner = this (live) process
        assert dl.sweep_recoverable() == []

    def test_live_owner_failed_row_retries_after_backoff(self):
        token = _record()
        assert dl.mark_attempting("ob-1", token)
        dl.mark_failed("ob-1", token, "temporary rejection")
        future = time.time() + dl.RETRY_BASE_SECONDS + 1

        claimed = dl.sweep_recoverable(now=future)

        assert len(claimed) == 1
        assert claimed[0]["needs_marker"] is True
        assert claimed[0]["attempts"] == 1

    def test_live_owner_pending_task_loss_is_reclaimed_after_lease(self):
        _record()
        future = time.time() + dl.CLAIM_LEASE_SECONDS + 1

        claimed = dl.sweep_recoverable(now=future)

        assert len(claimed) == 1
        assert claimed[0]["needs_marker"] is False

    def test_live_owner_attempting_task_loss_is_quarantined_after_lease(self):
        token = _record()
        dl.mark_attempting("ob-1", token)
        assert dl.sweep_recoverable(
            now=time.time() + dl.CLAIM_LEASE_SECONDS - 1
        ) == []

        assert dl.sweep_recoverable(
            now=time.time() + dl.CLAIM_LEASE_SECONDS + 1
        ) == []
        assert _row("ob-1")["state"] == "ambiguous"

    def test_dead_owner_pending_claimed_without_marker(self):
        _record()
        _orphan("ob-1")
        claimed = dl.sweep_recoverable()
        assert len(claimed) == 1
        assert claimed[0]["needs_marker"] is False
        assert claimed[0]["attempts"] == 1
        # Claim re-stamps ownership: a second sweep in the same (live)
        # process must not double-claim.
        assert dl.sweep_recoverable() == []

    def test_stale_claim_cannot_overwrite_new_generation(self):
        stale = _record()
        _orphan("ob-1")
        claimed = dl.sweep_recoverable()
        current = claimed[0]["claim_token"]

        assert current != stale
        assert dl.mark_delivered("ob-1", stale) is False
        assert dl.mark_attempting("ob-1", current) is True
        assert dl.mark_delivered("ob-1", current) is True

    def test_renewal_is_token_cas_and_extends_due_time(self):
        token = _record()
        with dl._connect() as conn:
            before = conn.execute(
                "SELECT lease_due_at FROM delivery_obligations WHERE obligation_id='ob-1'"
            ).fetchone()[0]
        assert dl.renew_claim("ob-1", "stale") is False
        assert dl.renew_claim("ob-1", token, lease_seconds=600) is True
        with dl._connect() as conn:
            after = conn.execute(
                "SELECT lease_due_at FROM delivery_obligations WHERE obligation_id='ob-1'"
            ).fetchone()[0]
        assert after > before

    def test_same_generation_can_cross_send_boundary_only_once(self):
        token = _record()
        assert dl.mark_attempting("ob-1", token) is True
        assert dl.mark_attempting("ob-1", token) is False

    def test_terminal_state_cannot_be_rewritten_by_same_token(self):
        token = _record()
        assert dl.mark_attempting("ob-1", token)
        assert dl.mark_delivered("ob-1", token)

        assert dl.mark_failed("ob-1", token, "late stale callback") is False
        assert dl.mark_delivered("ob-1", token) is False
        assert _row("ob-1")["state"] == "delivered"

    def test_dead_owner_attempting_is_quarantined_without_resend(self):
        token = _record()
        assert dl.mark_attempting("ob-1", token)
        _orphan("ob-1")

        assert dl.sweep_recoverable() == []
        assert _row("ob-1")["state"] == "ambiguous"

    def test_ambiguous_failure_is_not_retryable(self):
        token = _record()
        assert dl.mark_attempting("ob-1", token)
        assert dl.mark_failed(
            "ob-1", token, "timeout after send", ambiguous=True
        )

        assert dl.sweep_recoverable(now=time.time() + dl.RETRY_MAX_SECONDS) == []
        assert _row("ob-1")["state"] == "ambiguous"


class TestPrune:
    def test_old_delivered_rows_pruned(self):
        token = _record()
        assert dl.mark_attempting("ob-1", token)
        dl.mark_delivered("ob-1", token)
        with dl._connect() as conn:
            conn.execute(
                "UPDATE delivery_obligations SET updated_at=? WHERE obligation_id=?",
                (time.time() - dl._RETENTION_SECONDS - 60, "ob-1"),
            )
        dl._prune()
        assert _row("ob-1") is None

    def test_row_cap_never_evicts_undelivered_work(self, monkeypatch):
        _record()
        monkeypatch.setattr(dl, "_MAX_ROWS", 0)

        dl._prune()

        assert _row("ob-1") is not None


class TestLedgerEnabled:
    def test_default_on(self):
        assert dl.ledger_enabled({}) is True
        assert dl.ledger_enabled({"gateway": {}}) is True


class TestGatewayRedeliverySweep:
    """Drive the real GatewayRunner._redeliver_pending_obligations."""

    @staticmethod
    def _runner(adapter=None):
        from gateway.config import Platform
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.adapters = {Platform.SLACK: adapter} if adapter else {}
        _store = MagicMock()
        _store.clear_resume_pending = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @staticmethod
    def _adapter(success=True):
        adapter = MagicMock()
        adapter.send = AsyncMock(
            return_value=MagicMock(success=success, error="" if success else "nope")
        )
        return adapter

    def test_pending_redelivers_plain_and_clears_resume(self):
        _record()  # pending
        _orphan("ob-1")
        adapter = self._adapter()
        runner = self._runner(adapter)

        n = asyncio.run(runner._redeliver_pending_obligations())

        assert n == 1
        sent = adapter.send.call_args.kwargs
        assert sent["content"] == "the final answer"  # no marker
        assert sent["metadata"] == {"thread_id": "171.001"}
        assert _row("ob-1")["state"] == "delivered"
        runner._async_session_store.clear_resume_pending.assert_awaited_once_with(
            "agent:main:slack:channel:C1"
        )

    def test_attempting_is_quarantined_without_provider_resend(self):
        token = _record()
        assert dl.mark_attempting("ob-1", token)
        _orphan("ob-1")
        adapter = self._adapter()
        runner = self._runner(adapter)

        n = asyncio.run(runner._redeliver_pending_obligations())

        assert n == 0
        adapter.send.assert_not_awaited()
        assert _row("ob-1")["state"] == "ambiguous"

    def test_periodic_watcher_recovers_without_gateway_restart(self):
        runner = self._runner()
        runner._running = True
        calls = 0

        async def sweep_once():
            nonlocal calls
            calls += 1
            runner._running = False
            return 0

        runner._redeliver_pending_obligations = sweep_once
        asyncio.run(
            runner._delivery_obligation_watcher(interval=0.01, initial_delay=0)
        )
        assert calls == 1

    @pytest.mark.parametrize(
        ("send_success", "ledger_method"),
        [(True, "mark_delivered"), (False, "mark_failed")],
    )
    @pytest.mark.asyncio
    async def test_slow_state_update_does_not_block_event_loop(
        self, send_success, ledger_method
    ):
        import asyncio

        _record()
        _orphan("ob-1")
        runner = self._runner(self._adapter(success=send_success))
        slow_update, event_loop_witness, blocked_event_loop = _blocking_probe()

        with patch.object(dl, ledger_method, side_effect=slow_update):
            await asyncio.gather(
                runner._redeliver_pending_obligations(), event_loop_witness()
            )

        assert blocked_event_loop == []


class TestAttemptsOnlySpentOnRealSends:
    """``attempts`` is the redelivery budget — it must buy a send.

    ``self.adapters`` only holds a platform after its ``connect()`` succeeded,
    and the sweep claimed every dead-owner row regardless. A platform that
    failed to connect this boot therefore burned one attempt per boot while
    the caller's ``adapter is None`` branch skipped it without sending — so
    after MAX_ATTEMPTS boots the row abandoned having never been sent once,
    losing exactly the response the ledger exists to guarantee. That failure
    correlates with the crash that created the obligation: the network
    trouble that killed the send tends to still be there on the next boot.
    """

    def test_absent_platform_does_not_burn_attempts(self):
        token = _record(platform="telegram")
        dl.mark_attempting("ob-1", token)

        for _ in range(dl.MAX_ATTEMPTS + 2):
            _orphan("ob-1")
            assert dl.sweep_recoverable(deliverable_platforms={"discord"}) == []

        row = dl.debug_rows()
        assert "abandoned" not in row
        with dl._connect() as conn:
            state, attempts = conn.execute(
                "SELECT state, attempts FROM delivery_obligations "
                "WHERE obligation_id=?", ("ob-1",),
            ).fetchone()
        assert attempts == 0, "an unsendable boot must not spend the budget"
        assert state == "ambiguous"

    def test_row_still_delivers_once_its_platform_returns(self):
        _record(platform="telegram")
        for _ in range(dl.MAX_ATTEMPTS + 2):
            _orphan("ob-1")
            dl.sweep_recoverable(deliverable_platforms={"discord"})

        _orphan("ob-1")
        claimed = dl.sweep_recoverable(deliverable_platforms={"telegram"})
        assert len(claimed) == 1
        assert claimed[0]["attempts"] == 1


class TestUnconnectedPlatformKeepsItsBudget:
    """End-to-end through the real runner: boots where the platform failed to
    connect must not consume the row's redelivery budget."""

    @staticmethod
    def _runner_without_slack():
        from gateway.run import GatewayRunner

        runner = object.__new__(GatewayRunner)
        runner.adapters = {}  # slack failed to connect this boot
        _store = MagicMock()
        _store.clear_resume_pending = AsyncMock()
        _store._store = None
        runner.session_store = None
        runner._async_session_store = _store
        return runner

    @pytest.mark.asyncio
    async def test_row_survives_boots_where_its_platform_is_down(self):
        token = _record(platform="slack")
        dl.mark_attempting("ob-1", token)

        for _ in range(dl.MAX_ATTEMPTS + 1):
            _orphan("ob-1")
            runner = self._runner_without_slack()
            assert await runner._redeliver_pending_obligations() == 0

        assert _row("ob-1")["state"] != "abandoned", (
            "the obligation was abandoned without a single send being attempted"
        )
        assert _row("ob-1")["attempts"] == 0

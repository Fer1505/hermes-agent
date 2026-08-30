"""Durable delivery-obligation ledger for gateway final responses.

A final agent response that was generated but not yet confirmed-delivered
to the messaging platform is the one artifact the gateway can lose without
a trace: the turn already burned its tokens, the text exists only in a
Python local, and a crash / planned restart between finalize and platform
ACK drops it silently (#58818, #41696, #63695).

This module records a small durable row per outbound final response in the
shared ``state.db`` (same file and conventions as
``tools.async_delegation`` — WAL, owner pid + process-start-time liveness,
bounded retention). The gateway writes three checkpoints around the send:

    record_obligation()   state='pending'     before any send attempt
    mark_attempting()     state='attempting'  immediately before the await
    mark_delivered() /    state='delivered'   only on SendResult.success
    mark_failed()         state='failed'      on a definitive rejection

On startup and at a bounded periodic cadence, ``sweep_recoverable()`` claims
pending work from dead owners and definitively failed work after its persisted
due time. Current renewable leases are never stolen; expired pending work is
safe to reclaim, while expired attempting work is quarantined. After a platform
adapter reconnects without a process restart, ``sweep_failed_for_runtime()`` may
claim only the same live process's explicitly allowlisted transient failures,
handing them back as fresh claim generations. Crash semantics are explicit about
ambiguity (the contract review of the earlier delivery-outbox attempt, #61790,
closed it for silently resending ambiguous sends):

- ``pending``     — the send never started: redeliver plainly, no dup risk.
- ``attempting``  — a live process crossed the provider boundary; its renewable
  token remains exclusive. If the owner dies, the row becomes ``ambiguous``
  and is quarantined without an automatic provider resend.
- ``failed``      — definitively rejected once; the restart is a natural
  retry boundary after ``next_attempt_at``. Carries the marker.
- ``ambiguous``   — timeout, unverified 2xx, or crash after the send boundary;
  operator review is required and the sweeper never resends it.
- ``delivered``   — nothing to do; retention prunes.

Poison rows cannot spin: attempts are capped, stale rows expire, and both
transition to ``abandoned`` (kept briefly for inspection, then pruned).

Ledger availability remains best-effort: callers send normally if recording
itself fails. Once a durable claim exists, however, token/CAS failures suppress
duplicate generations rather than weakening the idempotency contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()

# Redelivery policy knobs (module constants; deliberately not config — the
# ledger itself is gated by ``gateway.delivery_ledger`` and these bounds
# only matter in the rare recovery path).
MAX_ATTEMPTS = 3
STALE_AFTER_SECONDS = 24 * 60 * 60
CLAIM_LEASE_SECONDS = 2 * 60
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 15 * 60
_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_ROWS = 500

# Visible prefix for retries after a definitive provider rejection.
RECOVERED_MARKER = (
    "♻️ Recovered reply — an earlier delivery was rejected and is being retried:\n\n"
)
# Runtime recovery uses a distinct marker because no gateway restart occurred.
# Keep the ambiguity explicit: a network rejection normally means the platform
# did not accept the message, but an acknowledgement can be lost independently.
RECONNECTED_MARKER = (
    "♻️ Recovered reply — the messaging platform reconnected after the original "
    "delivery failed, so this may be a duplicate:\n\n"
)
# Runtime replay is deliberately fail-closed. Only errors whose send contract
# proves they are transient reconnect failures belong here; permanent rejects
# (blocked bot, bad auth, missing chat) must not be retried merely because an
# adapter reconnected.
_RUNTIME_RETRYABLE_ERRORS = frozenset({"send_path_degraded"})


def is_runtime_retryable_error(error: Any) -> bool:
    """True for a definitive transient rejection the runtime sweep may replay.

    Such an error means the adapter's send path was degraded before the
    provider accepted anything, so it is a plain ``failed`` outcome — never
    ``ambiguous`` — unless the adapter reports partial acceptance separately.
    """
    return str(error or "").strip().lower() in _RUNTIME_RETRYABLE_ERRORS


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (delivery_ledger)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_obligations (
            obligation_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT,
            content TEXT NOT NULL,
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            claim_generation INTEGER NOT NULL DEFAULT 0,
            claim_token TEXT,
            lease_due_at REAL,
            next_attempt_at REAL,
            last_error TEXT,
            adapter_profile TEXT
        )"""
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(delivery_obligations)")
    }
    for name, declaration in (
        ("claim_generation", "INTEGER NOT NULL DEFAULT 0"),
        ("claim_token", "TEXT"),
        ("lease_due_at", "REAL"),
        ("next_attempt_at", "REAL"),
        ("adapter_profile", "TEXT"),
    ):
        if name not in columns:
            try:
                conn.execute(
                    f"ALTER TABLE delivery_obligations ADD COLUMN {name} {declaration}"
                )
            except sqlite3.OperationalError as exc:
                # Concurrent first-use connections can both observe the old schema.
                if "duplicate column" not in str(exc).lower():
                    raise
    conn.execute(
        """UPDATE delivery_obligations
           SET lease_due_at=updated_at+?
           WHERE state IN ('pending','attempting') AND lease_due_at IS NULL""",
        (CLAIM_LEASE_SECONDS,),
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it.

    ``sqlite3.Connection.__enter__``/``__exit__`` only commit or roll back the
    transaction; they do not close the connection. Using ``with _connect()``
    alone therefore leaks a connection — and its WAL/SHM file descriptors — on
    every call, deferring the close to the garbage collector. On a long-running
    gateway that exhausts ``RLIMIT_NOFILE`` (the cron-ledger sibling of this
    bug was #69567 / PR #69594). ``record_obligation`` runs on every outbound
    final response, so this ledger is the highest-frequency leaker.
    """
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _owner_stamp() -> tuple[int, Optional[int]]:
    pid = os.getpid()
    try:
        from gateway.status import get_process_start_time

        return pid, get_process_start_time(pid)
    except Exception:
        return pid, None


def _owner_alive(pid: Any, started_at: Any) -> bool:
    """True when the recorded owning process still exists (pid + start time)."""
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        from gateway.status import get_process_start_time

        current_start = get_process_start_time(pid)
    except Exception:
        current_start = None
    if current_start is None:
        # No such process (or unreadable) — treat unreadable-but-extant
        # processes as alive only if the pid exists. Route through the
        # cross-platform probe: ``os.kill(pid, 0)`` on Windows is NOT a
        # no-op (bpo-14484 — CPython maps sig=0 to
        # ``GenerateConsoleCtrlEvent(0, pid)``), so a raw probe here could
        # Ctrl+C the gateway's own console group whenever psutil failed to
        # read the start time of a live pid. ``_pid_exists`` keeps the
        # EPERM-means-alive semantics (exists but owned by another user).
        try:
            from gateway.status import _pid_exists
        except Exception:
            if os.name == "nt":
                # Never fall back to a raw sig-0 probe on Windows.
                return False
            try:
                os.kill(pid, 0)  # windows-footgun: ok — POSIX-only fallback branch
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except OSError:
                return False
            return True
        try:
            return bool(_pid_exists(pid))
        except Exception:
            return False
    if started_at is None:
        return True
    try:
        return int(current_start) == int(started_at)
    except (TypeError, ValueError):
        return True


def compute_obligation_id(session_key: str, message_ref: str, content: str) -> str:
    """Stable id: same turn + same content re-records idempotently, while
    distinct threads/topics on the same chat can never collide (the
    session_key carries platform, chat and thread; ``message_ref`` is the
    triggering inbound message id, distinguishing turns in one session)."""
    payload = f"{session_key}|{message_ref}|{content}"
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:24]


def record_obligation(
    *,
    obligation_id: str,
    session_key: str,
    platform: str,
    chat_id: str,
    thread_id: Optional[str],
    content: str,
    adapter_profile: Optional[str] = None,
) -> Optional[str]:
    """Record a final response as owed to the platform (state='pending').

    ``adapter_profile`` persists the transport owner (the bot identity whose
    credentials must send this row) independently of the routed session
    namespace; ``None`` means the primary/default adapter.
    """
    now = time.time()
    pid, started = _owner_stamp()
    claim_token = uuid.uuid4().hex
    stored_profile = str(adapter_profile).strip() if adapter_profile else "default"
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, thread_id,
                content, state, attempts, created_at, updated_at,
                owner_pid, owner_started_at, claim_generation, claim_token,
                lease_due_at, next_attempt_at, adapter_profile)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, 1, ?, ?, NULL, ?)
               ON CONFLICT(obligation_id) DO NOTHING""",
            (obligation_id, session_key, platform, str(chat_id),
             str(thread_id) if thread_id else None, content, now, now,
             pid, started, claim_token, now + CLAIM_LEASE_SECONDS, stored_profile),
        )
        if not cursor.rowcount:
            row = conn.execute(
                """SELECT claim_token, owner_pid, owner_started_at, state
                   FROM delivery_obligations WHERE obligation_id=?""",
                (obligation_id,),
            ).fetchone()
            if row is None:
                return None
            claim_token = row[0]
    _prune()
    return claim_token


def mark_attempting(obligation_id: str, claim_token: str) -> bool:
    """CAS a claimed pending/failed generation into the provider-send state."""
    pid, started = _owner_stamp()
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='attempting', updated_at=?, lease_due_at=?
               WHERE obligation_id=? AND claim_token=?
                 AND owner_pid=? AND (owner_started_at IS ? OR owner_started_at=?)
                 AND (
                   state='pending'
                   OR (state='failed' AND (next_attempt_at IS NULL OR next_attempt_at<=?))
                 )""",
            (
                time.time(),
                time.time() + CLAIM_LEASE_SECONDS,
                obligation_id,
                claim_token,
                pid,
                started,
                started,
                time.time(),
            ),
        )
    return bool(cursor.rowcount)


def mark_delivered(obligation_id: str, claim_token: str) -> bool:
    return _update_state(obligation_id, "delivered", claim_token=claim_token)


def mark_failed(
    obligation_id: str,
    claim_token: str,
    error: str = "",
    *,
    retry_after: Optional[float] = None,
    ambiguous: bool = False,
) -> bool:
    if ambiguous:
        return _update_state(
            obligation_id,
            "ambiguous",
            error=error or "provider outcome unknown; operator review required",
            claim_token=claim_token,
        )
    delay = retry_after
    if delay is None:
        delay = RETRY_BASE_SECONDS
    delay = max(0.0, min(RETRY_MAX_SECONDS, float(delay)))
    return _update_state(
        obligation_id,
        "failed",
        error=error,
        claim_token=claim_token,
        next_attempt_at=time.time() + delay,
    )


def renew_claim(
    obligation_id: str,
    claim_token: str,
    *,
    lease_seconds: float = CLAIM_LEASE_SECONDS,
) -> bool:
    """Extend only the caller's current claim; stale generations cannot renew."""
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations SET lease_due_at=?, updated_at=?
               WHERE obligation_id=? AND claim_token=?
                 AND state IN ('pending','attempting')""",
            (
                time.time() + max(1.0, float(lease_seconds)),
                time.time(),
                obligation_id,
                claim_token,
            ),
        )
    return bool(cursor.rowcount)


def release_runtime_claim(
    obligation_id: str,
    error: str = "",
    *,
    claim_token: Optional[str] = None,
) -> bool:
    """Return an unsent runtime claim to ``failed`` without spending an attempt.

    Runtime recovery claims before clearing ``resume_pending`` so that two
    reconnect paths cannot send the same row. If the session flag cannot be
    cleared (or no adapter can send), no platform send was attempted and the
    claim must not consume the bounded redelivery budget. Release is
    fail-closed to the exact current process instance and to the ``pending``
    state a runtime claim holds before ``mark_attempting`` crosses the send
    boundary; ``claim_token`` additionally pins the generation when known.
    """
    pid, started = _owner_stamp()
    if started is None:
        return False
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state='failed', attempts=CASE
                       WHEN attempts > 0 THEN attempts - 1 ELSE 0 END,
                   updated_at=?, last_error=?, lease_due_at=NULL,
                   next_attempt_at=NULL
               WHERE obligation_id=? AND state='pending'
                 AND owner_pid IS ? AND owner_started_at IS ?
                 AND (? IS NULL OR claim_token=?)""",
            (time.time(), error[:500] if error else None,
             obligation_id, pid, started, claim_token, claim_token),
        )
    return bool(cursor.rowcount)


def _update_state(
    obligation_id: str,
    state: str,
    *,
    error: str = "",
    claim_token: str,
    next_attempt_at: Optional[float] = None,
) -> bool:
    with _DB_LOCK, _transaction() as conn:
        cursor = conn.execute(
            """UPDATE delivery_obligations
               SET state=?, updated_at=?, last_error=?, next_attempt_at=?,
                   lease_due_at=NULL
               WHERE obligation_id=? AND claim_token=? AND state='attempting'""",
            (
                state,
                time.time(),
                error[:500] if error else None,
                next_attempt_at,
                obligation_id,
                claim_token,
            ),
        )
    return bool(cursor.rowcount)


def sweep_recoverable(
    now: Optional[float] = None,
    *,
    deliverable_platforms: Optional[set] = None,
    deliverable_targets: Optional[set] = None,
) -> List[Dict[str, Any]]:
    """Claim undelivered rows with dead or expired-live owners for redelivery.

    Claiming atomically re-stamps the owner to THIS process and increments
    ``attempts``, so a second gateway racing the same sweep cannot
    double-claim (the UPDATE is guarded on the previous owner stamp).
    Rows over the attempts cap or older than the stale cutoff transition to
    'abandoned' instead of being returned.

    ``deliverable_platforms`` (platform value strings) restricts claiming to
    platforms the caller can actually send on this boot.  ``attempts`` is the
    redelivery budget, so it must only be spent on a real send: a platform
    that failed to connect would otherwise burn one attempt per boot and hit
    the cap having never been sent once.  Rows for absent platforms are left
    untouched for a later boot; the stale cutoff still bounds them.

    ``deliverable_targets`` further scopes multiplexed gateways by exact
    ``(platform, adapter_profile)`` identity, preventing one connected bot from
    spending another disconnected bot's retry budget.
    """
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    claimed: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, state, attempts, created_at, updated_at,
                      owner_pid, owner_started_at, claim_generation,
                      claim_token, lease_due_at, next_attempt_at, adapter_profile
               FROM delivery_obligations
               WHERE state IN ('pending', 'attempting', 'failed')"""
        ).fetchall()
        for (oid, session_key, platform, chat_id, thread_id, content, state,
             attempts, created_at, updated_at, owner_pid, owner_started_at,
             generation, previous_token, lease_due_at, next_attempt_at,
             adapter_profile) in rows:
            owner_alive = _owner_alive(owner_pid, owner_started_at)
            lease_expired = lease_due_at is not None and now >= lease_due_at
            if state == "attempting" and (not owner_alive or lease_expired):
                cursor = conn.execute(
                    """UPDATE delivery_obligations
                       SET state='ambiguous', updated_at=?, lease_due_at=NULL,
                           next_attempt_at=NULL,
                           last_error='provider outcome unknown after lease expiry or owner exit; operator review required'
                       WHERE obligation_id=? AND state='attempting'
                         AND claim_generation=? AND (claim_token IS ? OR claim_token=?)""",
                    (now, oid, generation, previous_token, previous_token),
                )
                if cursor.rowcount:
                    logger.error(
                        "Delivery obligation %s quarantined: provider outcome unknown after lease expiry or owner exit",
                        oid,
                    )
                continue
            if state == "attempting":
                # Current lease + live owner: the provider call is still in
                # flight and its heartbeat retains exclusive ownership.
                continue
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
                cursor = conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=?,
                           last_error=COALESCE(last_error, ?)
                       WHERE obligation_id=? AND state=? AND updated_at=?""",
                    (now, "delivery retry budget or age limit exhausted", oid,
                     state, updated_at),
                )
                if cursor.rowcount:
                    logger.error(
                        "Delivery obligation %s requires operator attention: "
                        "retry budget or age limit exhausted (state=%s, attempts=%d)",
                        oid, state, attempts,
                    )
                continue
            # A live process retains pending work while its task-level lease is
            # current. Expiry means that task disappeared; pending is safe to
            # reclaim because it never crossed the provider boundary. An
            # expired attempting row was quarantined above, never resent.
            if owner_alive and state == "pending" and not lease_expired:
                continue
            if state == "failed" and next_attempt_at is not None and now < next_attempt_at:
                continue
            if (
                deliverable_platforms is not None
                and platform not in deliverable_platforms
            ):
                # No adapter for this platform this boot — the caller cannot
                # send, so claiming would spend an attempt on a no-op.
                continue
            if (
                deliverable_targets is not None
                and (platform, adapter_profile) not in deliverable_targets
            ):
                # The exact transport owner (bot identity) is not connected;
                # another bot on the same platform must not spend its budget.
                continue
            new_token = uuid.uuid4().hex
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET state='pending', owner_pid=?, owner_started_at=?, attempts=attempts+1,
                       updated_at=?, claim_generation=claim_generation+1,
                       claim_token=?, lease_due_at=?, next_attempt_at=NULL
                   WHERE obligation_id=? AND state=? AND attempts=?
                     AND updated_at=? AND claim_generation=?
                     AND (claim_token IS ? OR claim_token=?)""",
                (
                    pid, started, now, new_token, now + CLAIM_LEASE_SECONDS,
                    oid, state, attempts, updated_at, generation,
                    previous_token, previous_token,
                ),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    # pending = send never started, redeliver plainly;
                    # Failed means definitively rejected, so carry a visible
                    # recovery marker. Attempting rows never reach this path.
                    "needs_marker": state != "pending",
                    "profile": adapter_profile,
                    "attempts": attempts + 1,
                    "claim_generation": generation + 1,
                    "claim_token": new_token,
                })
    return claimed


def sweep_failed_for_runtime(
    platform: str,
    now: Optional[float] = None,
    *,
    profile: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Claim this process's reconnect-retryable failed rows for one adapter.

    ``profile`` scopes multiplexed gateways to the bot identity that actually
    owned the failed send; ``None`` means the primary/default adapter. The
    persisted adapter owner is independent of the routed session namespace.

    Startup recovery intentionally ignores rows owned by a live gateway. That
    protects concurrent processes, but it also means a final response rejected
    with ``send_path_degraded`` remains stranded when only the platform adapter
    reconnects. This runtime sweep closes that gap without weakening ownership:

    - only rows stamped to this exact process instance are eligible;
    - only explicitly allowlisted transient errors are eligible;
    - attempts/staleness bounds match startup recovery;
    - every update is guarded by the prior owner stamp, the ``failed`` state
      and the row's claim generation.

    A claimed row becomes a fresh ``pending`` generation with a new claim
    token (exactly like a startup claim); the caller crosses the send boundary
    with ``mark_attempting`` and can hand an unsent claim back with
    ``release_runtime_claim``. Unowned rows and rows owned by another process
    are left untouched for the normal startup/dead-owner sweep. Claimed rows
    always carry the reconnect marker because the failed send's
    acknowledgement is not safe to infer.
    """
    now = now if now is not None else time.time()
    pid, started = _owner_stamp()
    if started is None:
        # PID equality alone cannot distinguish this process from a stale row
        # left by an earlier process incarnation after PID reuse. Runtime replay
        # is optional recovery, so fail closed when the process fingerprint is
        # unavailable; startup recovery remains the durable fallback.
        return []
    expected_profile = (
        "default" if not profile or profile == "default" else str(profile)
    )
    claimed: List[Dict[str, Any]] = []
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, platform, chat_id, thread_id,
                      content, attempts, created_at, owner_pid,
                      owner_started_at, last_error, adapter_profile,
                      claim_generation, claim_token
               FROM delivery_obligations
               WHERE state='failed' AND platform=?""",
            (platform,),
        ).fetchall()
        for (
            oid,
            session_key,
            row_platform,
            chat_id,
            thread_id,
            content,
            attempts,
            created_at,
            owner_pid,
            owner_started_at,
            last_error,
            adapter_profile,
            generation,
            previous_token,
        ) in rows:
            if adapter_profile != expected_profile:
                continue
            # Runtime reconnect recovery may act only on its own rows. Exact
            # process-start matching prevents PID reuse from stealing work.
            if owner_pid != pid or owner_started_at != started:
                continue
            if not is_runtime_retryable_error(last_error):
                continue
            owner_guard = (oid, owner_pid, owner_started_at)
            if attempts >= MAX_ATTEMPTS or (now - created_at) > STALE_AFTER_SECONDS:
                cursor = conn.execute(
                    """UPDATE delivery_obligations
                       SET state='abandoned', updated_at=?, lease_due_at=NULL,
                           next_attempt_at=NULL,
                           last_error=COALESCE(last_error, ?)
                       WHERE obligation_id=? AND state='failed'
                         AND owner_pid IS ? AND owner_started_at IS ?""",
                    (now, "delivery retry budget or age limit exhausted",
                     *owner_guard),
                )
                if cursor.rowcount:
                    logger.error(
                        "Delivery obligation %s requires operator attention: "
                        "retry budget or age limit exhausted (state=failed, attempts=%d)",
                        oid, attempts,
                    )
                continue
            new_token = uuid.uuid4().hex
            cursor = conn.execute(
                """UPDATE delivery_obligations
                   SET state='pending', attempts=attempts+1, updated_at=?,
                       claim_generation=claim_generation+1, claim_token=?,
                       lease_due_at=?, next_attempt_at=NULL
                   WHERE obligation_id=? AND state='failed'
                     AND owner_pid IS ? AND owner_started_at IS ?
                     AND claim_generation=?
                     AND (claim_token IS ? OR claim_token=?)""",
                (now, new_token, now + CLAIM_LEASE_SECONDS, *owner_guard,
                 generation, previous_token, previous_token),
            )
            if cursor.rowcount:
                claimed.append({
                    "obligation_id": oid,
                    "session_key": session_key,
                    "platform": row_platform,
                    "chat_id": chat_id,
                    "thread_id": thread_id,
                    "content": content,
                    "needs_marker": True,
                    "marker": RECONNECTED_MARKER,
                    "profile": adapter_profile,
                    "runtime_recovery": True,
                    "attempts": attempts + 1,
                    "claim_generation": generation + 1,
                    "claim_token": new_token,
                })
    return claimed


def _prune(now: Optional[float] = None) -> None:
    now = now if now is not None else time.time()
    cutoff = now - _RETENTION_SECONDS
    try:
        with _transaction() as conn:
            conn.execute(
                """DELETE FROM delivery_obligations
                   WHERE state IN ('delivered', 'abandoned') AND updated_at < ?""",
                (cutoff,),
            )
            total = conn.execute(
                "SELECT COUNT(*) FROM delivery_obligations"
            ).fetchone()[0]
            excess = max(0, total - _MAX_ROWS)
            if excess:
                conn.execute(
                    """DELETE FROM delivery_obligations WHERE obligation_id IN (
                         SELECT obligation_id FROM delivery_obligations
                         WHERE state IN ('delivered', 'abandoned')
                         ORDER BY updated_at ASC
                         LIMIT ?)""",
                    (excess,),
                )
    except Exception:
        logger.debug("delivery ledger prune failed", exc_info=True)


def ledger_enabled(config: Optional[Dict[str, Any]] = None) -> bool:
    """Read the ``gateway.delivery_ledger`` config gate (default on)."""
    try:
        if config is None:
            from hermes_cli.config import load_config

            config = load_config()
        gw = config.get("gateway") or {}
        value = gw.get("delivery_ledger", True)
        if isinstance(value, str):
            return value.strip().lower() not in {"false", "0", "no", "off"}
        return bool(value)
    except Exception:
        return True


def debug_rows(limit: int = 20) -> str:
    """Human-readable dump for ad-hoc inspection (sqlite3-free path)."""
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            """SELECT obligation_id, session_key, state, attempts,
                      created_at, updated_at, last_error
               FROM delivery_obligations
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return json.dumps(
        [
            {
                "id": r[0], "session": r[1], "state": r[2], "attempts": r[3],
                "created_at": r[4], "updated_at": r[5], "last_error": r[6],
            }
            for r in rows
        ],
        indent=2,
    )

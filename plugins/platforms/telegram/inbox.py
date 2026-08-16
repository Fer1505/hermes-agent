"""Durable, profile-scoped ingress journal for Telegram updates.

The Bot API considers a polling update acknowledged when the client advances
its offset, and considers a webhook update accepted when the HTTP handler
returns 2xx.  This store is deliberately synchronous: callers persist the raw
provider payload before allowing either acknowledgement boundary to proceed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 3
UPDATE_ID_RESET_IDLE_SECONDS = 7 * 24 * 60 * 60
DEFAULT_RETENTION_SECONDS = 30 * 24 * 60 * 60
DEFAULT_MAX_HISTORY_ROWS = 10_000
MAX_ACTIVE_OR_QUARANTINED_ROWS = 10_000
_VALID_TRANSPORTS = frozenset({"polling_tls", "webhook_secret"})


@dataclass(frozen=True)
class InboxClaim:
    update_id: int
    payload: dict[str, Any]
    effect_key: str
    attempt: int
    lease_owner: str


class TelegramInbox:
    """SQLite inbox isolated by Hermes home and a one-way bot-token digest."""

    def __init__(
        self,
        db_path: Path,
        *,
        profile_id: str,
        bot_token: str,
        clock=time.time,
    ) -> None:
        if not bot_token:
            raise ValueError("bot_token is required")
        self.db_path = Path(db_path)
        self.profile_id = str(profile_id or "default")
        self.bot_id = hashlib.sha256(bot_token.encode("utf-8")).hexdigest()
        self._clock = clock
        self._secure_storage_paths()
        self._initialize()

    def _secure_storage_paths(self) -> None:
        """Create owner-private journal storage without following leaf links."""
        parent = self.db_path.parent
        if parent.is_symlink():
            raise RuntimeError(f"Refusing symlinked Telegram inbox directory: {parent}")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise RuntimeError(f"Telegram inbox parent is not a real directory: {parent}")

        if os.name != "posix":
            if self.db_path.is_symlink():
                raise RuntimeError(f"Refusing symlinked Telegram inbox: {self.db_path}")
            return

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            directory_fd = os.open(parent, directory_flags)
        except OSError as exc:
            raise RuntimeError(
                f"Could not securely open Telegram inbox directory: {parent}"
            ) from exc
        try:
            metadata = os.fstat(directory_fd)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RuntimeError(
                    f"Telegram inbox directory is not owned by the current user: {parent}"
                )
            os.fchmod(directory_fd, 0o700)
            if stat.S_IMODE(os.fstat(directory_fd).st_mode) != 0o700:
                raise RuntimeError(
                    f"Telegram inbox directory is not owner-private: {parent}"
                )
        finally:
            os.close(directory_fd)

        file_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(self.db_path, file_flags, 0o600)
        except OSError as exc:
            raise RuntimeError(
                f"Could not securely open Telegram inbox: {self.db_path}"
            ) from exc
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise RuntimeError(
                    f"Telegram inbox is not an owner-controlled regular file: {self.db_path}"
                )
            os.fchmod(file_fd, 0o600)
            if stat.S_IMODE(os.fstat(file_fd).st_mode) != 0o600:
                raise RuntimeError(f"Telegram inbox is not owner-private: {self.db_path}")
        finally:
            os.close(file_fd)

    @classmethod
    def for_profile_home(
        cls, profile_home: Path, *, profile_id: str, bot_token: str
    ) -> "TelegramInbox":
        return cls(
            Path(profile_home) / "gateway" / "telegram-inbox.sqlite3",
            profile_id=profile_id,
            bot_token=bot_token,
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _initialize(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS telegram_inbox (
                    profile_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    update_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    archive_id TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    authenticated INTEGER NOT NULL CHECK (authenticated = 1),
                    provider_sender_id TEXT,
                    provider_chat_id TEXT,
                    provider_thread_id TEXT,
                    received_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending','processing','retry')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL,
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    last_error TEXT,
                    effect_key TEXT NOT NULL,
                    PRIMARY KEY (profile_id, bot_id, update_id),
                    UNIQUE (profile_id, bot_id, effect_key),
                    UNIQUE (profile_id, bot_id, archive_id)
                );

                CREATE TABLE IF NOT EXISTS telegram_checkpoints (
                    profile_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    highest_seen_update_id INTEGER NOT NULL,
                    highest_contiguous_update_id INTEGER NOT NULL,
                    gap_after_update_id INTEGER,
                    sequence_epoch INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (profile_id, bot_id)
                );

                CREATE TABLE IF NOT EXISTS telegram_effects (
                    profile_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    update_id INTEGER NOT NULL,
                    effect_key TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('claimed','committed')),
                    claimed_at REAL NOT NULL,
                    committed_at REAL,
                    PRIMARY KEY (profile_id, bot_id, effect_key),
                    UNIQUE (profile_id, bot_id, update_id)
                );

                CREATE TABLE IF NOT EXISTS telegram_archive (
                    profile_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    update_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    archive_id TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    provider_sender_id TEXT,
                    provider_chat_id TEXT,
                    provider_thread_id TEXT,
                    received_at REAL NOT NULL,
                    completed_at REAL NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    effect_key TEXT NOT NULL,
                    PRIMARY KEY (profile_id, bot_id, update_id),
                    UNIQUE (profile_id, bot_id, archive_id)
                );

                CREATE TABLE IF NOT EXISTS telegram_dead_letters (
                    profile_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    update_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    archive_id TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    provider_sender_id TEXT,
                    provider_chat_id TEXT,
                    provider_thread_id TEXT,
                    received_at REAL NOT NULL,
                    failed_at REAL NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    effect_key TEXT NOT NULL,
                    last_error TEXT NOT NULL,
                    PRIMARY KEY (profile_id, bot_id, update_id),
                    UNIQUE (profile_id, bot_id, archive_id)
                );

                CREATE INDEX IF NOT EXISTS telegram_inbox_ready_idx
                ON telegram_inbox(profile_id, bot_id, state, next_attempt_at, update_id);

                CREATE TABLE IF NOT EXISTS telegram_inbox_operator_audit (
                    audit_id TEXT PRIMARY KEY,
                    profile_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    update_id INTEGER,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    occurred_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS telegram_epoch_history (
                    profile_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    sequence_epoch INTEGER NOT NULL,
                    update_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    archive_id TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    provider_sender_id TEXT,
                    provider_chat_id TEXT,
                    provider_thread_id TEXT,
                    received_at REAL NOT NULL,
                    completed_at REAL NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    effect_key TEXT NOT NULL,
                    effect_state TEXT NOT NULL,
                    effect_claimed_at REAL,
                    effect_committed_at REAL,
                    PRIMARY KEY (profile_id, bot_id, sequence_epoch, update_id),
                    UNIQUE (profile_id, bot_id, archive_id)
                );

                PRAGMA user_version = 3;
                """
            )
            checkpoint_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(telegram_checkpoints)")
            }
            if "sequence_epoch" not in checkpoint_columns:
                conn.execute(
                    "ALTER TABLE telegram_checkpoints ADD COLUMN "
                    "sequence_epoch INTEGER NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _canonical_payload(payload: dict[str, Any]) -> tuple[str, str]:
        serialized = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        return serialized, hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _cap_operator_audit(self, conn: sqlite3.Connection) -> None:
        count = conn.execute(
            """SELECT COUNT(*) FROM telegram_inbox_operator_audit
               WHERE profile_id=? AND bot_id=?""",
            (self.profile_id, self.bot_id),
        ).fetchone()[0]
        excess = max(0, int(count) - DEFAULT_MAX_HISTORY_ROWS)
        if excess:
            conn.execute(
                """DELETE FROM telegram_inbox_operator_audit
                   WHERE audit_id IN (
                     SELECT audit_id FROM telegram_inbox_operator_audit
                     WHERE profile_id=? AND bot_id=?
                     ORDER BY occurred_at ASC LIMIT ?
                   )""",
                (self.profile_id, self.bot_id, excess),
            )

    @staticmethod
    def _routing_provenance(
        payload: dict[str, Any]
    ) -> tuple[str | None, str | None, str | None]:
        callback = payload.get("callback_query")
        callback = callback if isinstance(callback, dict) else {}
        message = callback.get("message")
        if not isinstance(message, dict):
            for key in (
                "message",
                "edited_message",
                "channel_post",
                "edited_channel_post",
                "business_message",
                "edited_business_message",
            ):
                candidate = payload.get(key)
                if isinstance(candidate, dict):
                    message = candidate
                    break
        message = message if isinstance(message, dict) else {}
        sender = callback.get("from") or message.get("from") or message.get("sender_chat")
        sender = sender if isinstance(sender, dict) else {}
        chat = message.get("chat")
        chat = chat if isinstance(chat, dict) else {}
        thread_id = message.get("message_thread_id")
        if thread_id is None:
            thread_id = message.get("direct_messages_topic_id")
        return (
            str(sender["id"]) if sender.get("id") is not None else None,
            str(chat["id"]) if chat.get("id") is not None else None,
            str(thread_id) if thread_id is not None else None,
        )

    def persist_updates(
        self,
        updates: Iterable[dict[str, Any]],
        *,
        transport: str,
        authenticated: bool,
    ) -> int:
        """Persist a provider batch atomically; return newly inserted rows.

        Authentication is part of the route contract, not metadata supplied by
        the request body.  A caller may use ``webhook_secret`` only after its
        HTTP framework validated Telegram's configured secret header.
        """
        if transport not in _VALID_TRANSPORTS:
            raise ValueError(f"unsupported Telegram transport: {transport}")
        if not authenticated:
            raise PermissionError("unauthenticated Telegram update rejected")

        normalized: list[
            tuple[int, str, str, str | None, str | None, str | None]
        ] = []
        for payload in updates:
            if not isinstance(payload, dict):
                raise ValueError("Telegram update must be an object")
            raw_id = payload.get("update_id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, int) or raw_id < 0:
                raise ValueError("Telegram update_id must be a non-negative integer")
            serialized, digest = self._canonical_payload(payload)
            sender_id, chat_id, thread_id = self._routing_provenance(payload)
            normalized.append(
                (
                    raw_id,
                    serialized,
                    digest,
                    sender_id,
                    chat_id,
                    thread_id,
                )
            )

        if not normalized:
            return 0
        if len({item[0] for item in normalized}) != len(normalized):
            raise ValueError("duplicate update_id within Telegram provider batch")

        now = float(self._clock())
        inserted = 0
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                epoch = self._prepare_sequence_epoch(
                    conn, [item[0] for item in normalized], now
                )
                existing_digests: dict[int, str] = {}
                for update_id, _payload_json, digest, *_provenance in normalized:
                    existing = conn.execute(
                        """SELECT payload_sha256 FROM telegram_inbox
                           WHERE profile_id=? AND bot_id=? AND update_id=?
                           UNION ALL
                           SELECT payload_sha256 FROM telegram_archive
                           WHERE profile_id=? AND bot_id=? AND update_id=?
                           UNION ALL
                           SELECT payload_sha256 FROM telegram_dead_letters
                           WHERE profile_id=? AND bot_id=? AND update_id=?
                           UNION ALL
                           SELECT payload_sha256 FROM telegram_effects
                           WHERE profile_id=? AND bot_id=? AND update_id=?
                           LIMIT 1""",
                        (
                            self.profile_id,
                            self.bot_id,
                            update_id,
                            self.profile_id,
                            self.bot_id,
                            update_id,
                            self.profile_id,
                            self.bot_id,
                            update_id,
                            self.profile_id,
                            self.bot_id,
                            update_id,
                        ),
                    ).fetchone()
                    if existing is not None:
                        existing_digest = str(existing["payload_sha256"])
                        if existing_digest != digest:
                            raise ValueError(
                                f"Telegram update_id {update_id} payload changed on replay"
                            )
                        existing_digests[update_id] = existing_digest

                active_count = conn.execute(
                    """SELECT
                         (SELECT COUNT(*) FROM telegram_inbox
                          WHERE profile_id=? AND bot_id=?) +
                         (SELECT COUNT(*) FROM telegram_dead_letters
                          WHERE profile_id=? AND bot_id=?) +
                         (SELECT COUNT(*) FROM telegram_archive
                          WHERE profile_id=? AND bot_id=?) +
                         (SELECT COUNT(*) FROM telegram_epoch_history
                          WHERE profile_id=? AND bot_id=?)""",
                    (
                        self.profile_id, self.bot_id,
                        self.profile_id, self.bot_id,
                        self.profile_id, self.bot_id,
                        self.profile_id, self.bot_id,
                    ),
                ).fetchone()[0]
                novel_count = len(normalized) - len(existing_digests)
                if int(active_count) + novel_count > MAX_ACTIVE_OR_QUARANTINED_ROWS:
                    raise RuntimeError(
                        "Telegram durable inbox capacity reached; owner review required"
                    )
                for (
                    update_id,
                    payload_json,
                    digest,
                    sender_id,
                    chat_id,
                    thread_id,
                ) in sorted(normalized):
                    effect_key = (
                        f"telegram:{self.profile_id}:{self.bot_id}:{epoch}:{update_id}"
                    )
                    archive_id = hashlib.sha256(
                        f"{effect_key}:{digest}".encode("utf-8")
                    ).hexdigest()
                    if update_id in existing_digests:
                        continue
                    conn.execute(
                        """INSERT INTO telegram_inbox
                           (profile_id,bot_id,update_id,payload_json,payload_sha256,
                            archive_id,transport,authenticated,provider_sender_id,
                            provider_chat_id,provider_thread_id,received_at,effect_key)
                           VALUES (?,?,?,?,?,?,?,1,?,?,?,?,?)""",
                        (
                            self.profile_id,
                            self.bot_id,
                            update_id,
                            payload_json,
                            digest,
                            archive_id,
                            transport,
                            sender_id,
                            chat_id,
                            thread_id,
                            now,
                            effect_key,
                        ),
                    )
                    inserted += 1
                if inserted:
                    self._update_checkpoint(
                        conn, [item[0] for item in normalized], now, epoch=epoch
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return inserted

    def _prepare_sequence_epoch(
        self,
        conn: sqlite3.Connection,
        update_ids: list[int],
        now: float,
    ) -> int:
        """Rotate Telegram's update-id namespace after documented long idle.

        Telegram may choose a random update_id after at least one week without
        updates. A lower id is therefore a new epoch, not necessarily a replay.
        Rotation removes the old bounded history only after its inactivity
        window and records an operator-audit event before accepting the new id.
        """
        row = conn.execute(
            """SELECT highest_seen_update_id, updated_at, sequence_epoch
               FROM telegram_checkpoints WHERE profile_id=? AND bot_id=?""",
            (self.profile_id, self.bot_id),
        ).fetchone()
        if row is None:
            return 0
        epoch = int(row["sequence_epoch"] or 0)
        reset = (
            now - float(row["updated_at"]) >= UPDATE_ID_RESET_IDLE_SECONDS
            and min(update_ids) < int(row["highest_seen_update_id"])
        )
        if not reset:
            return epoch
        unresolved = conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM telegram_inbox
                  WHERE profile_id=? AND bot_id=?) +
                 (SELECT COUNT(*) FROM telegram_dead_letters
                  WHERE profile_id=? AND bot_id=?)""",
            (
                self.profile_id,
                self.bot_id,
                self.profile_id,
                self.bot_id,
            ),
        ).fetchone()[0]
        if int(unresolved):
            raise RuntimeError(
                "Telegram update_id epoch reset blocked by unresolved inbox or "
                "quarantine records; owner review required"
            )
        audit_id = uuid.uuid4().hex
        conn.execute(
            """INSERT INTO telegram_inbox_operator_audit
               (audit_id,profile_id,bot_id,update_id,action,reason,occurred_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                audit_id,
                self.profile_id,
                self.bot_id,
                min(update_ids),
                "sequence_epoch_reset",
                "provider update_id reset after at least seven days idle",
                now,
            ),
        )
        self._cap_operator_audit(conn)
        # The active v1 tables cannot share update_id primary keys across
        # epochs. Preserve every committed payload and receipt in immutable
        # epoch history before clearing only the active namespace.
        conn.execute(
            """INSERT INTO telegram_epoch_history
               (profile_id,bot_id,sequence_epoch,update_id,payload_json,
                payload_sha256,archive_id,transport,provider_sender_id,
                provider_chat_id,provider_thread_id,received_at,completed_at,
                attempt_count,effect_key,effect_state,effect_claimed_at,
                effect_committed_at)
               SELECT a.profile_id,a.bot_id,?,a.update_id,a.payload_json,
                      a.payload_sha256,a.archive_id,a.transport,
                      a.provider_sender_id,a.provider_chat_id,
                      a.provider_thread_id,a.received_at,a.completed_at,
                      a.attempt_count,a.effect_key,
                      COALESCE(e.state,'committed'),e.claimed_at,e.committed_at
               FROM telegram_archive a
               LEFT JOIN telegram_effects e
                 ON e.profile_id=a.profile_id AND e.bot_id=a.bot_id
                AND e.effect_key=a.effect_key
               WHERE a.profile_id=? AND a.bot_id=?""",
            (epoch, self.profile_id, self.bot_id),
        )
        for table in ("telegram_effects", "telegram_archive"):
            conn.execute(
                f"DELETE FROM {table} WHERE profile_id=? AND bot_id=?",
                (self.profile_id, self.bot_id),
            )
        conn.execute(
            "DELETE FROM telegram_checkpoints WHERE profile_id=? AND bot_id=?",
            (self.profile_id, self.bot_id),
        )
        return epoch + 1

    def persist_update(
        self, payload: dict[str, Any], *, transport: str, authenticated: bool
    ) -> bool:
        return bool(
            self.persist_updates(
                [payload], transport=transport, authenticated=authenticated
            )
        )

    def _update_checkpoint(
        self,
        conn: sqlite3.Connection,
        update_ids: list[int],
        now: float,
        *,
        epoch: int,
    ) -> None:
        row = conn.execute(
            """SELECT highest_seen_update_id, highest_contiguous_update_id
               FROM telegram_checkpoints WHERE profile_id=? AND bot_id=?""",
            (self.profile_id, self.bot_id),
        ).fetchone()
        ordered = sorted(set(update_ids))
        if row is None:
            contiguous = ordered[0] - 1
            highest_seen = ordered[-1]
        else:
            contiguous = int(row["highest_contiguous_update_id"])
            highest_seen = max(int(row["highest_seen_update_id"]), ordered[-1])

        durable_ids = {
            int(r[0])
            for r in conn.execute(
                """SELECT update_id FROM telegram_inbox
                   WHERE profile_id=? AND bot_id=? AND update_id>?
                   UNION SELECT update_id FROM telegram_archive
                   WHERE profile_id=? AND bot_id=? AND update_id>?
                   UNION SELECT update_id FROM telegram_dead_letters
                   WHERE profile_id=? AND bot_id=? AND update_id>?""",
                (
                    self.profile_id,
                    self.bot_id,
                    contiguous,
                    self.profile_id,
                    self.bot_id,
                    contiguous,
                    self.profile_id,
                    self.bot_id,
                    contiguous,
                ),
            )
        }
        while contiguous + 1 in durable_ids:
            contiguous += 1
        gap_after = contiguous if highest_seen > contiguous else None
        conn.execute(
            """INSERT INTO telegram_checkpoints
               (profile_id,bot_id,highest_seen_update_id,highest_contiguous_update_id,
                gap_after_update_id,updated_at,sequence_epoch) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(profile_id,bot_id) DO UPDATE SET
                 highest_seen_update_id=excluded.highest_seen_update_id,
                 highest_contiguous_update_id=excluded.highest_contiguous_update_id,
                 gap_after_update_id=excluded.gap_after_update_id,
                 updated_at=excluded.updated_at,
                 sequence_epoch=excluded.sequence_epoch""",
            (
                self.profile_id,
                self.bot_id,
                highest_seen,
                contiguous,
                gap_after,
                now,
                epoch,
            ),
        )

    def claim(
        self,
        update_id: int,
        *,
        lease_seconds: float = 300.0,
        lease_owner: str | None = None,
    ) -> InboxClaim | None:
        now = float(self._clock())
        owner = lease_owner or uuid.uuid4().hex
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """SELECT * FROM telegram_inbox
                       WHERE profile_id=? AND bot_id=? AND update_id=?""",
                    (self.profile_id, self.bot_id, int(update_id)),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                eligible = row["state"] in {"pending", "retry"} and (
                    row["next_attempt_at"] is None or row["next_attempt_at"] <= now
                )
                if (
                    row["state"] == "processing"
                    and row["lease_expires_at"] is not None
                    and row["lease_expires_at"] <= now
                ):
                    effect = conn.execute(
                        """SELECT 1 FROM telegram_effects
                           WHERE profile_id=? AND bot_id=? AND effect_key=?
                             AND state='claimed'""",
                        (self.profile_id, self.bot_id, row["effect_key"]),
                    ).fetchone()
                    eligible = eligible or effect is None
                if not eligible:
                    conn.rollback()
                    return None
                attempt = int(row["attempt_count"]) + 1
                conn.execute(
                    """UPDATE telegram_inbox SET state='processing', attempt_count=?,
                       lease_owner=?, lease_expires_at=?, next_attempt_at=NULL
                       WHERE profile_id=? AND bot_id=? AND update_id=?""",
                    (
                        attempt,
                        owner,
                        now + max(1.0, float(lease_seconds)),
                        self.profile_id,
                        self.bot_id,
                        int(update_id),
                    ),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return InboxClaim(
            update_id=int(update_id),
            payload=json.loads(row["payload_json"]),
            effect_key=row["effect_key"],
            attempt=attempt,
            lease_owner=owner,
        )

    def begin_effects(self, claims: Iterable[InboxClaim]) -> bool:
        """Durably fence downstream work before any non-idempotent effect."""
        claim_list = list(claims)
        if not claim_list:
            return True
        now = float(self._clock())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for claim in claim_list:
                    row = conn.execute(
                        """SELECT payload_sha256 FROM telegram_inbox
                           WHERE profile_id=? AND bot_id=? AND update_id=?
                             AND state='processing' AND lease_owner=?""",
                        (
                            self.profile_id,
                            self.bot_id,
                            claim.update_id,
                            claim.lease_owner,
                        ),
                    ).fetchone()
                    if row is None:
                        conn.rollback()
                        return False
                    existing = conn.execute(
                        """SELECT state FROM telegram_effects
                           WHERE profile_id=? AND bot_id=? AND effect_key=?""",
                        (self.profile_id, self.bot_id, claim.effect_key),
                    ).fetchone()
                    if existing is not None:
                        conn.rollback()
                        return False
                    conn.execute(
                        """INSERT INTO telegram_effects
                           (profile_id,bot_id,update_id,effect_key,payload_sha256,
                            state,claimed_at)
                           VALUES (?,?,?,?,?,'claimed',?)""",
                        (
                            self.profile_id,
                            self.bot_id,
                            claim.update_id,
                            claim.effect_key,
                            row["payload_sha256"],
                            now,
                        ),
                    )
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise

    def complete(self, claim: InboxClaim) -> bool:
        """Atomically commit the effect marker and move the payload to archive."""
        now = float(self._clock())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """SELECT * FROM telegram_inbox WHERE profile_id=? AND bot_id=?
                       AND update_id=? AND state='processing' AND lease_owner=?""",
                    (
                        self.profile_id,
                        self.bot_id,
                        claim.update_id,
                        claim.lease_owner,
                    ),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                conn.execute(
                    """INSERT INTO telegram_archive
                       (profile_id,bot_id,update_id,payload_json,payload_sha256,
                        archive_id,transport,provider_sender_id,provider_chat_id,
                        provider_thread_id,received_at,completed_at,attempt_count,effect_key)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        self.profile_id,
                        self.bot_id,
                        claim.update_id,
                        row["payload_json"],
                        row["payload_sha256"],
                        row["archive_id"],
                        row["transport"],
                        row["provider_sender_id"],
                        row["provider_chat_id"],
                        row["provider_thread_id"],
                        row["received_at"],
                        now,
                        row["attempt_count"],
                        row["effect_key"],
                    ),
                )
                conn.execute(
                    """INSERT INTO telegram_effects
                       (profile_id,bot_id,update_id,effect_key,payload_sha256,
                        state,claimed_at,committed_at)
                       VALUES (?,?,?,?,?,'committed',?,?)
                       ON CONFLICT(profile_id,bot_id,effect_key) DO UPDATE SET
                         state='committed', committed_at=excluded.committed_at""",
                    (
                        self.profile_id,
                        self.bot_id,
                        claim.update_id,
                        claim.effect_key,
                        row["payload_sha256"],
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """DELETE FROM telegram_inbox
                       WHERE profile_id=? AND bot_id=? AND update_id=?""",
                    (self.profile_id, self.bot_id, claim.update_id),
                )
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise

    def fail(
        self,
        claim: InboxClaim,
        error: object,
        *,
        max_attempts: int = 5,
        retry_delay: float = 1.0,
    ) -> str:
        now = float(self._clock())
        message = str(error).replace("\n", " ")[:1000] or "unknown error"
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """SELECT * FROM telegram_inbox WHERE profile_id=? AND bot_id=?
                       AND update_id=? AND state='processing' AND lease_owner=?""",
                    (
                        self.profile_id,
                        self.bot_id,
                        claim.update_id,
                        claim.lease_owner,
                    ),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return "lost"
                effect = conn.execute(
                    """SELECT state FROM telegram_effects
                       WHERE profile_id=? AND bot_id=? AND effect_key=?""",
                    (self.profile_id, self.bot_id, claim.effect_key),
                ).fetchone()
                effect_ambiguous = effect is not None and effect["state"] == "claimed"
                if effect_ambiguous:
                    message = f"ambiguous after downstream effect claim: {message}"
                if effect_ambiguous or int(row["attempt_count"]) >= max(1, int(max_attempts)):
                    conn.execute(
                        """INSERT INTO telegram_dead_letters
                           (profile_id,bot_id,update_id,payload_json,payload_sha256,
                            archive_id,transport,provider_sender_id,provider_chat_id,
                            provider_thread_id,received_at,failed_at,attempt_count,
                            effect_key,last_error)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            self.profile_id,
                            self.bot_id,
                            claim.update_id,
                            row["payload_json"],
                            row["payload_sha256"],
                            row["archive_id"],
                            row["transport"],
                            row["provider_sender_id"],
                            row["provider_chat_id"],
                            row["provider_thread_id"],
                            row["received_at"],
                            now,
                            row["attempt_count"],
                            row["effect_key"],
                            message,
                        ),
                    )
                    conn.execute(
                        """DELETE FROM telegram_inbox
                           WHERE profile_id=? AND bot_id=? AND update_id=?""",
                        (self.profile_id, self.bot_id, claim.update_id),
                    )
                    result = "dead_letter"
                else:
                    conn.execute(
                        """UPDATE telegram_inbox SET state='retry', next_attempt_at=?,
                           lease_owner=NULL, lease_expires_at=NULL, last_error=?
                           WHERE profile_id=? AND bot_id=? AND update_id=?""",
                        (
                            now + max(0.0, float(retry_delay)),
                            message,
                            self.profile_id,
                            self.bot_id,
                            claim.update_id,
                        ),
                    )
                    result = "retry"
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

    def recoverable(self, *, limit: int = 1000) -> list[dict[str, Any]]:
        now = float(self._clock())
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT payload_json FROM telegram_inbox
                   WHERE profile_id=? AND bot_id=? AND
                     ((state IN ('pending','retry') AND
                       (next_attempt_at IS NULL OR next_attempt_at<=?)) OR
                      (state='processing' AND lease_expires_at<=? AND NOT EXISTS (
                         SELECT 1 FROM telegram_effects e
                         WHERE e.profile_id=telegram_inbox.profile_id
                           AND e.bot_id=telegram_inbox.bot_id
                           AND e.effect_key=telegram_inbox.effect_key
                           AND e.state='claimed'
                      )))
                   ORDER BY update_id LIMIT ?""",
                (self.profile_id, self.bot_id, now, now, max(1, int(limit))),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def requeue_inflight(self) -> int:
        """Reclaim safe leases and quarantine ambiguous downstream effects.

        A ``claimed`` effect may already have reached the agent or provider.
        Replaying it automatically could duplicate a tool call or reply, so a
        cold start moves that row to the dead-letter queue. Rows that crashed
        before the effect fence are safe to retry.
        """
        now = float(self._clock())
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ambiguous = conn.execute(
                    """SELECT i.* FROM telegram_inbox i
                       JOIN telegram_effects e
                         ON e.profile_id=i.profile_id AND e.bot_id=i.bot_id
                        AND e.effect_key=i.effect_key AND e.state='claimed'
                       WHERE i.profile_id=? AND i.bot_id=?
                         AND i.state='processing'""",
                    (self.profile_id, self.bot_id),
                ).fetchall()
                for row in ambiguous:
                    conn.execute(
                        """INSERT INTO telegram_dead_letters
                           (profile_id,bot_id,update_id,payload_json,payload_sha256,
                            archive_id,transport,provider_sender_id,provider_chat_id,
                            provider_thread_id,received_at,failed_at,attempt_count,
                            effect_key,last_error)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            self.profile_id,
                            self.bot_id,
                            row["update_id"],
                            row["payload_json"],
                            row["payload_sha256"],
                            row["archive_id"],
                            row["transport"],
                            row["provider_sender_id"],
                            row["provider_chat_id"],
                            row["provider_thread_id"],
                            row["received_at"],
                            now,
                            row["attempt_count"],
                            row["effect_key"],
                            "ambiguous after downstream effect claim: process ended during dispatch",
                        ),
                    )
                    conn.execute(
                        """DELETE FROM telegram_inbox
                           WHERE profile_id=? AND bot_id=? AND update_id=?""",
                        (self.profile_id, self.bot_id, row["update_id"]),
                    )
                cursor = conn.execute(
                    """UPDATE telegram_inbox SET state='retry', next_attempt_at=NULL,
                       lease_owner=NULL, lease_expires_at=NULL,
                       last_error=COALESCE(last_error, 'process ended during dispatch')
                       WHERE profile_id=? AND bot_id=? AND state='processing'""",
                    (self.profile_id, self.bot_id),
                )
                conn.commit()
                return len(ambiguous) + max(0, int(cursor.rowcount))
            except BaseException:
                conn.rollback()
                raise

    def replay_dead_letter(
        self,
        update_id: int,
        *,
        reason: str,
        confirmation: str,
    ) -> bool:
        """Owner-confirm and audit one ambiguity-accepting replay."""
        reason = str(reason).strip()
        expected = f"REPLAY:{int(update_id)}"
        if not reason:
            raise ValueError("operator replay reason is required")
        if confirmation != expected:
            raise PermissionError(f"confirmation must exactly equal {expected}")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    """SELECT * FROM telegram_dead_letters
                       WHERE profile_id=? AND bot_id=? AND update_id=?""",
                    (self.profile_id, self.bot_id, int(update_id)),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                effect = conn.execute(
                    """SELECT state FROM telegram_effects
                       WHERE profile_id=? AND bot_id=? AND effect_key=?""",
                    (self.profile_id, self.bot_id, row["effect_key"]),
                ).fetchone()
                # A committed receipt is permanent proof that replay would
                # duplicate a completed effect. A claimed receipt is an
                # explicit operator-risk boundary: selecting replay clears it.
                if effect is not None and effect["state"] == "committed":
                    conn.rollback()
                    return False
                if effect is not None:
                    conn.execute(
                        """DELETE FROM telegram_effects
                           WHERE profile_id=? AND bot_id=? AND effect_key=?
                             AND state='claimed'""",
                        (self.profile_id, self.bot_id, row["effect_key"]),
                    )
                conn.execute(
                    """INSERT INTO telegram_inbox
                       (profile_id,bot_id,update_id,payload_json,payload_sha256,
                        archive_id,transport,authenticated,provider_sender_id,
                        provider_chat_id,provider_thread_id,received_at,state,
                        attempt_count,effect_key,last_error)
                       VALUES (?,?,?,?,?,?,?,1,?,?,?,?,'retry',0,?,?)""",
                    (
                        self.profile_id,
                        self.bot_id,
                        int(update_id),
                        row["payload_json"],
                        row["payload_sha256"],
                        row["archive_id"],
                        row["transport"],
                        row["provider_sender_id"],
                        row["provider_chat_id"],
                        row["provider_thread_id"],
                        row["received_at"],
                        row["effect_key"],
                        f"operator replay after: {row['last_error']}",
                    ),
                )
                conn.execute(
                    """DELETE FROM telegram_dead_letters
                       WHERE profile_id=? AND bot_id=? AND update_id=?""",
                    (self.profile_id, self.bot_id, int(update_id)),
                )
                conn.execute(
                    """INSERT INTO telegram_inbox_operator_audit
                       (audit_id,profile_id,bot_id,update_id,action,reason,occurred_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex,
                        self.profile_id,
                        self.bot_id,
                        int(update_id),
                        "owner_confirmed_replay",
                        reason[:500],
                        float(self._clock()),
                    ),
                )
                self._cap_operator_audit(conn)
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise

    def prune_archive(self, *, retention_seconds: float, limit: int = 1000) -> int:
        cutoff = float(self._clock()) - max(
            UPDATE_ID_RESET_IDLE_SECONDS,
            max(0.0, float(retention_seconds)),
        )
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    """DELETE FROM telegram_archive WHERE rowid IN
                       (SELECT rowid FROM telegram_archive
                        WHERE profile_id=? AND bot_id=? AND completed_at<?
                        ORDER BY completed_at LIMIT ?)""",
                    (self.profile_id, self.bot_id, cutoff, max(1, int(limit))),
                )
                conn.execute(
                    """DELETE FROM telegram_effects
                       WHERE profile_id=? AND bot_id=? AND state='committed'
                         AND committed_at<?
                         AND NOT EXISTS (
                           SELECT 1 FROM telegram_archive a
                           WHERE a.profile_id=telegram_effects.profile_id
                             AND a.bot_id=telegram_effects.bot_id
                             AND a.effect_key=telegram_effects.effect_key
                         )""",
                    (self.profile_id, self.bot_id, cutoff),
                )
                conn.execute(
                    """DELETE FROM telegram_epoch_history
                       WHERE profile_id=? AND bot_id=? AND completed_at<?""",
                    (self.profile_id, self.bot_id, cutoff),
                )
                conn.execute(
                    """DELETE FROM telegram_inbox_operator_audit
                       WHERE profile_id=? AND bot_id=? AND occurred_at<?""",
                    (self.profile_id, self.bot_id, cutoff),
                )
                self._cap_operator_audit(conn)
                conn.commit()
                return max(0, int(cursor.rowcount))
            except BaseException:
                conn.rollback()
                raise

    def checkpoint(self) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """SELECT highest_seen_update_id,highest_contiguous_update_id,
                          gap_after_update_id,updated_at,sequence_epoch
                   FROM telegram_checkpoints WHERE profile_id=? AND bot_id=?""",
                (self.profile_id, self.bot_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def quarantine_count(self) -> int:
        with closing(self._connect()) as conn:
            return int(
                conn.execute(
                    """SELECT COUNT(*) FROM telegram_dead_letters
                       WHERE profile_id=? AND bot_id=?""",
                    (self.profile_id, self.bot_id),
                ).fetchone()[0]
            )

    def list_dead_letters(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """SELECT update_id,failed_at,attempt_count,last_error,
                          payload_sha256,provider_chat_id,provider_thread_id
                   FROM telegram_dead_letters
                   WHERE profile_id=? AND bot_id=?
                   ORDER BY failed_at DESC LIMIT ?""",
                (self.profile_id, self.bot_id, max(1, min(1000, int(limit)))),
            ).fetchall()
        return [dict(row) for row in rows]

    def audit_operator_action(
        self,
        action: str,
        *,
        reason: str,
        update_id: int | None = None,
    ) -> None:
        reason = str(reason).strip()
        if not reason:
            raise ValueError("operator audit reason is required")
        with closing(self._connect()) as conn:
            conn.execute(
                """INSERT INTO telegram_inbox_operator_audit
                   (audit_id,profile_id,bot_id,update_id,action,reason,occurred_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    self.profile_id,
                    self.bot_id,
                    update_id,
                    str(action)[:100],
                    reason[:500],
                    float(self._clock()),
                ),
            )
            self._cap_operator_audit(conn)

    def record(self, update_id: int) -> dict[str, Any] | None:
        """Return one queryable inbox/archive/dead-letter record for audit/replay."""
        with closing(self._connect()) as conn:
            for state, table, extra_columns in (
                ("inbox", "telegram_inbox", ",attempt_count,last_error"),
                ("archive", "telegram_archive", ""),
                (
                    "dead_letter",
                    "telegram_dead_letters",
                    ",attempt_count,last_error",
                ),
            ):
                row = conn.execute(
                    f"""SELECT update_id,payload_json,payload_sha256,archive_id,
                               transport,provider_sender_id,provider_chat_id,
                               provider_thread_id,received_at,effect_key
                               {extra_columns}
                        FROM {table}
                        WHERE profile_id=? AND bot_id=? AND update_id=?""",
                    (self.profile_id, self.bot_id, int(update_id)),
                ).fetchone()
                if row is not None:
                    result = dict(row)
                    result["payload"] = json.loads(result.pop("payload_json"))
                    result["state"] = state
                    return result
        return None

    def counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        with closing(self._connect()) as conn:
            for name, table in (
                ("inbox", "telegram_inbox"),
                ("archive", "telegram_archive"),
                ("dead_letter", "telegram_dead_letters"),
                ("effects", "telegram_effects"),
                ("epoch_history", "telegram_epoch_history"),
                ("operator_audit", "telegram_inbox_operator_audit"),
            ):
                result[name] = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE profile_id=? AND bot_id=?",
                        (self.profile_id, self.bot_id),
                    ).fetchone()[0]
                )
        return result

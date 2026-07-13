"""Bounded durable outbox for external memory-provider write mirrors.

The canonical Markdown write happens before an external provider is notified.
This outbox makes that secondary notification crash-replayable without allowing
an unavailable provider to grow an unbounded in-memory queue.

Delivery is at-least-once.  ``event_id`` is forwarded to metadata so providers
that support idempotency can collapse the small ambiguity window between a
successful remote write and the local completion transaction.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional


EnqueueResult = Literal["enqueued", "duplicate", "full", "oversize"]


@dataclass(frozen=True)
class MemoryWriteEvent:
    event_id: str
    provider: str
    action: str
    target: str
    content: str
    metadata: Dict[str, Any]
    created_at: float
    attempts: int


class MemoryWriteOutbox:
    """SQLite-backed, profile-scoped provider-write queue."""

    def __init__(
        self,
        path: Path,
        *,
        max_entries: int = 1000,
        max_payload_bytes: int = 8 * 1024 * 1024,
        max_age_seconds: float = 7 * 24 * 60 * 60,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 300.0,
        lease_seconds: float = 600.0,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self.max_payload_bytes = max(1024, int(max_payload_bytes))
        self.max_age_seconds = max(60.0, float(max_age_seconds))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.retry_max_seconds = max(
            self.retry_base_seconds,
            float(retry_max_seconds),
        )
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pending_writes (
                    event_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    payload_bytes INTEGER NOT NULL CHECK (payload_bytes >= 0),
                    created_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    next_attempt_at REAL NOT NULL,
                    last_error TEXT NOT NULL DEFAULT '',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_until REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS pending_writes_delivery_idx
                    ON pending_writes(provider, next_attempt_at, created_at, event_id);
                CREATE TABLE IF NOT EXISTS delivery_receipts (
                    event_id TEXT PRIMARY KEY,
                    delivered_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS delivery_receipts_age_idx
                    ON delivery_receipts(delivered_at);
                """
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self.purge_expired()

    @staticmethod
    def _metadata_json(metadata: Optional[Dict[str, Any]]) -> str:
        return json.dumps(
            dict(metadata or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _payload_size(
        provider: str,
        action: str,
        target: str,
        content: str,
        metadata_json: str,
    ) -> int:
        return len(
            "\0".join((provider, action, target, content, metadata_json)).encode("utf-8")
        )

    def enqueue(
        self,
        *,
        event_id: str,
        provider: str,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        now: Optional[float] = None,
    ) -> EnqueueResult:
        timestamp = time.time() if now is None else float(now)
        metadata_json = self._metadata_json(metadata)
        payload_bytes = self._payload_size(
            provider,
            action,
            target,
            content,
            metadata_json,
        )
        if payload_bytes > self.max_payload_bytes:
            return "oversize"

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._purge_expired(connection, timestamp)
            duplicate = connection.execute(
                """
                SELECT 1 FROM pending_writes WHERE event_id = ?
                UNION ALL
                SELECT 1 FROM delivery_receipts WHERE event_id = ?
                LIMIT 1
                """,
                (event_id, event_id),
            ).fetchone()
            if duplicate:
                return "duplicate"
            totals = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(payload_bytes), 0) AS bytes "
                "FROM pending_writes"
            ).fetchone()
            if (
                int(totals["count"]) >= self.max_entries
                or int(totals["bytes"]) + payload_bytes > self.max_payload_bytes
            ):
                return "full"
            connection.execute(
                """
                INSERT INTO pending_writes (
                    event_id, provider, action, target, content, metadata_json,
                    payload_bytes, created_at, next_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    provider,
                    action,
                    target,
                    content,
                    metadata_json,
                    payload_bytes,
                    timestamp,
                    timestamp,
                ),
            )
        return "enqueued"

    def claim_due(
        self,
        provider: str,
        *,
        lease_owner: str,
        limit: int = 100,
        now: Optional[float] = None,
    ) -> List[MemoryWriteEvent]:
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._purge_expired(connection, timestamp)
            rows = connection.execute(
                """
                SELECT * FROM pending_writes
                WHERE provider = ? AND next_attempt_at <= ? AND lease_until <= ?
                ORDER BY created_at, event_id
                LIMIT ?
                """,
                (provider, timestamp, timestamp, max(1, int(limit))),
            ).fetchall()
            if rows:
                connection.executemany(
                    """
                    UPDATE pending_writes
                    SET lease_owner = ?, lease_until = ?
                    WHERE event_id = ? AND lease_until <= ?
                    """,
                    [
                        (
                            lease_owner,
                            timestamp + self.lease_seconds,
                            row["event_id"],
                            timestamp,
                        )
                        for row in rows
                    ],
                )
            claimed_ids = [row["event_id"] for row in rows]
            if not claimed_ids:
                return []
            placeholders = ",".join("?" for _ in claimed_ids)
            claimed = connection.execute(
                f"""
                SELECT * FROM pending_writes
                WHERE lease_owner = ? AND event_id IN ({placeholders})
                ORDER BY created_at, event_id
                """,
                (lease_owner, *claimed_ids),
            ).fetchall()
        return [self._row_to_event(row) for row in claimed]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> MemoryWriteEvent:
        try:
            metadata = json.loads(row["metadata_json"])
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return MemoryWriteEvent(
            event_id=row["event_id"],
            provider=row["provider"],
            action=row["action"],
            target=row["target"],
            content=row["content"],
            metadata=metadata,
            created_at=float(row["created_at"]),
            attempts=int(row["attempts"]),
        )

    def complete(
        self,
        event_id: str,
        *,
        lease_owner: str,
        now: Optional[float] = None,
    ) -> bool:
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM pending_writes WHERE event_id = ? AND lease_owner = ?",
                (event_id, lease_owner),
            ).rowcount
            if not deleted:
                return False
            connection.execute(
                """
                INSERT INTO delivery_receipts(event_id, delivered_at) VALUES (?, ?)
                ON CONFLICT(event_id) DO UPDATE SET delivered_at = excluded.delivered_at
                """,
                (event_id, timestamp),
            )
            self._purge_expired(connection, timestamp)
        return True

    def fail(
        self,
        event_id: str,
        *,
        lease_owner: str,
        error: str,
        now: Optional[float] = None,
    ) -> bool:
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM pending_writes "
                "WHERE event_id = ? AND lease_owner = ?",
                (event_id, lease_owner),
            ).fetchone()
            if row is None:
                return False
            attempts = int(row["attempts"]) + 1
            delay = min(
                self.retry_max_seconds,
                self.retry_base_seconds * (2 ** min(attempts - 1, 16)),
            )
            connection.execute(
                """
                UPDATE pending_writes
                SET attempts = ?, next_attempt_at = ?, last_error = ?,
                    lease_owner = '', lease_until = 0
                WHERE event_id = ? AND lease_owner = ?
                """,
                (
                    attempts,
                    timestamp + delay,
                    str(error)[:500],
                    event_id,
                    lease_owner,
                ),
            )
        return True

    def stats(self, provider: Optional[str] = None) -> Dict[str, int]:
        where = " WHERE provider = ?" if provider else ""
        params = (provider,) if provider else ()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count, COALESCE(SUM(payload_bytes), 0) AS bytes "
                f"FROM pending_writes{where}",
                params,
            ).fetchone()
        return {"pending": int(row["count"]), "payload_bytes": int(row["bytes"])}

    def purge_expired(self, *, now: Optional[float] = None) -> int:
        timestamp = time.time() if now is None else float(now)
        with self._connect() as connection:
            return self._purge_expired(connection, timestamp)

    def _purge_expired(self, connection: sqlite3.Connection, now: float) -> int:
        cutoff = now - self.max_age_seconds
        pending = connection.execute(
            "DELETE FROM pending_writes WHERE created_at < ?",
            (cutoff,),
        ).rowcount
        connection.execute(
            "DELETE FROM delivery_receipts WHERE delivered_at < ?",
            (cutoff,),
        )
        return int(pending)

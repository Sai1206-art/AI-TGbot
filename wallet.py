from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class WalletStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path, timeout=30, factory=_ClosingConnection
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS wallets (
                    user_id INTEGER PRIMARY KEY,
                    free_credit_used INTEGER NOT NULL DEFAULT 0,
                    balance INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payments (
                    telegram_payment_charge_id TEXT PRIMARY KEY,
                    provider_payment_charge_id TEXT,
                    user_id INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    credits INTEGER NOT NULL,
                    stars INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wallet_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    amount INTEGER NOT NULL DEFAULT 0,
                    balance_after INTEGER NOT NULL,
                    free_credit_used_after INTEGER NOT NULL,
                    reference_id TEXT,
                    metadata TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    cost INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    mode TEXT,
                    prompt TEXT,
                    request_key TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generation_jobs (
                    job_id TEXT PRIMARY KEY,
                    request_key TEXT NOT NULL UNIQUE,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    reservation_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    cost INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    prompt TEXT,
                    caption_provided INTEGER NOT NULL DEFAULT 0,
                    file_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at TEXT NOT NULL,
                    claimed_at TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS generation_jobs_ready_idx
                    ON generation_jobs(status, available_at, created_at);
                CREATE TABLE IF NOT EXISTS request_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS request_events_user_time_idx
                    ON request_events(user_id, created_at);
                CREATE TABLE IF NOT EXISTS generation_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    reservation_id TEXT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    prompt TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(generation_reservations)")}
            if "cost" not in columns:
                connection.execute("ALTER TABLE generation_reservations ADD COLUMN cost INTEGER NOT NULL DEFAULT 1")
            if "request_key" not in columns:
                connection.execute("ALTER TABLE generation_reservations ADD COLUMN request_key TEXT")
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS generation_reservations_request_key_idx "
                    "ON generation_reservations(request_key) WHERE request_key IS NOT NULL"
                )
            job_columns = {row[1] for row in connection.execute("PRAGMA table_info(generation_jobs)")}
            if "caption_provided" not in job_columns:
                connection.execute(
                    "ALTER TABLE generation_jobs ADD COLUMN caption_provided INTEGER NOT NULL DEFAULT 0"
                )

    def _ensure_user_in(self, connection: sqlite3.Connection, user_id: int, timestamp: str) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO wallets
                (user_id, free_credit_used, balance, created_at, updated_at)
            VALUES (?, 0, 0, ?, ?)
            """,
            (user_id, timestamp, timestamp),
        )

    def ensure_user(self, user_id: int) -> None:
        timestamp = self._timestamp()
        with self._connect() as connection:
            self._ensure_user_in(connection, user_id, timestamp)

    def _record_event(
        self,
        connection: sqlite3.Connection,
        *,
        user_id: int,
        event_type: str,
        amount: int,
        balance_after: int,
        free_credit_used_after: int,
        timestamp: str,
        reference_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO wallet_events
                (user_id, event_type, amount, balance_after, free_credit_used_after,
                 reference_id, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                event_type,
                amount,
                balance_after,
                free_credit_used_after,
                reference_id,
                json.dumps(metadata or {}, sort_keys=True),
                timestamp,
            ),
        )

    def get_balance(self, user_id: int) -> dict[str, Any]:
        self.ensure_user(user_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT free_credit_used, balance, created_at, updated_at FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            history_count = connection.execute(
                "SELECT COUNT(*) FROM generation_history WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        result = dict(row)
        result["free_available"] = not bool(result["free_credit_used"])
        result["generation_count"] = history_count
        return result

    def get_generation_history(self, user_id: int, limit: int = 5) -> list[dict[str, Any]]:
        self.ensure_user(user_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT mode, status, prompt, error, created_at, completed_at
                FROM generation_history WHERE user_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, max(1, min(limit, 20))),
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve_generation(
        self,
        user_id: int,
        *,
        mode: str | None = None,
        prompt: str | None = None,
        cost: int = 1,
        allow_free: bool = True,
        request_key: str | None = None,
    ) -> dict[str, Any] | None:
        if cost < 1:
            raise ValueError("Generation cost must be at least 1 token.")
        timestamp = self._timestamp()
        reservation_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user_in(connection, user_id, timestamp)
            if request_key:
                existing = connection.execute(
                    "SELECT reservation_id, kind, cost, status FROM generation_reservations WHERE request_key = ?",
                    (request_key,),
                ).fetchone()
                if existing:
                    wallet = connection.execute(
                        "SELECT balance, free_credit_used FROM wallets WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                    return {
                        "reservation_id": existing["reservation_id"],
                        "kind": existing["kind"],
                        "cost": existing["cost"],
                        "balance": wallet["balance"],
                        "free_credit_used": wallet["free_credit_used"],
                        "status": existing["status"],
                    }
            row = connection.execute(
                "SELECT free_credit_used, balance FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if allow_free and row["free_credit_used"] == 0:
                kind = "free"
                new_balance = row["balance"]
                free_used = 1
                connection.execute(
                    "UPDATE wallets SET free_credit_used = 1, updated_at = ? WHERE user_id = ?",
                    (timestamp, user_id),
                )
            elif row["balance"] >= cost:
                kind = "paid"
                new_balance = row["balance"] - cost
                free_used = row["free_credit_used"]
                connection.execute(
                    "UPDATE wallets SET balance = ?, updated_at = ? WHERE user_id = ?",
                    (new_balance, timestamp, user_id),
                )
            else:
                return None
            connection.execute(
                """
                INSERT INTO generation_reservations
                    (reservation_id, user_id, kind, cost, status, mode, prompt, request_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?)
                """,
                (reservation_id, user_id, kind, cost, mode, prompt, request_key, timestamp, timestamp),
            )
            self._record_event(
                connection,
                user_id=user_id,
                event_type="generation_reserved",
                amount=-cost if kind == "paid" else 0,
                balance_after=new_balance,
                free_credit_used_after=free_used,
                timestamp=timestamp,
                reference_id=reservation_id,
                metadata={"kind": kind, "mode": mode},
            )
        return {
            "reservation_id": reservation_id,
            "kind": kind,
            "cost": cost if kind == "paid" else 0,
            "balance": new_balance,
            "free_credit_used": free_used,
            "status": "reserved",
        }

    def enqueue_generation_job(
        self,
        *,
        request_key: str,
        user_id: int,
        chat_id: int,
        message_id: int,
        reservation_id: str,
        kind: str,
        cost: int,
        mode: str,
        prompt: str,
        file_id: str,
        caption_provided: bool = False,
    ) -> dict[str, Any]:
        timestamp = self._timestamp()
        job_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO generation_jobs
                    (job_id, request_key, user_id, chat_id, message_id, reservation_id,
                    kind, cost, mode, prompt, caption_provided, file_id, status,
                    available_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (job_id, request_key, user_id, chat_id, message_id, reservation_id, kind, cost,
                 mode, prompt, int(caption_provided), file_id, timestamp, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM generation_jobs WHERE request_key = ?", (request_key,)
            ).fetchone()
        return dict(row)

    def allow_request(self, user_id: int, *, max_requests: int, window_seconds: int) -> bool:
        """Atomically enforce a durable per-user request rate limit."""
        timestamp = self._timestamp()
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=window_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM request_events WHERE user_id = ? AND created_at < ?",
                (user_id, cutoff.isoformat()),
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM request_events WHERE user_id = ?", (user_id,)
            ).fetchone()[0]
            if count >= max_requests:
                return False
            connection.execute(
                "INSERT INTO request_events(user_id, created_at) VALUES (?, ?)",
                (user_id, timestamp),
            )
        return True

    def claim_next_generation_job(self, *, max_attempts: int = 3) -> dict[str, Any] | None:
        timestamp = self._timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT job.* FROM generation_jobs AS job
                WHERE job.status = 'queued' AND job.attempts < ? AND job.available_at <= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM generation_jobs AS active
                      WHERE active.user_id = job.user_id AND active.status = 'running'
                  )
                ORDER BY job.created_at LIMIT 1
                """,
                (max_attempts, timestamp),
            ).fetchone()
            if not row:
                return None
            connection.execute(
                """UPDATE generation_jobs
                   SET status = 'running', attempts = attempts + 1,
                       claimed_at = ?, updated_at = ? WHERE job_id = ?""",
                (timestamp, timestamp, row["job_id"]),
            )
            row = connection.execute(
                "SELECT * FROM generation_jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
        return dict(row)

    def recover_stale_jobs(self, *, max_age_seconds: int = 1800, max_attempts: int = 3) -> int:
        """Requeue jobs left running by a crashed worker."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        timestamp = self._timestamp()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id, attempts FROM generation_jobs
                WHERE status = 'running' AND claimed_at < ?
                """,
                (cutoff.isoformat(),),
            ).fetchall()
            for row in rows:
                status = "dead_letter" if row["attempts"] >= max_attempts else "queued"
                connection.execute(
                    """
                    UPDATE generation_jobs
                    SET status = ?, available_at = ?, claimed_at = NULL,
                        last_error = 'worker lease expired', updated_at = ?
                    WHERE job_id = ?
                    """,
                    (status, timestamp, timestamp, row["job_id"]),
                )
            return len(rows)

    def release_running_job(self, job_id: str, *, error: str = "worker cancelled") -> bool:
        timestamp = self._timestamp()
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE generation_jobs
                SET status = 'queued', available_at = ?, claimed_at = NULL,
                    last_error = ?, updated_at = ?
                WHERE job_id = ? AND status = 'running'
                """,
                (timestamp, error, timestamp, job_id),
            ).rowcount
        return bool(updated)

    def finish_generation_job(
        self,
        job_id: str,
        *,
        failed: bool = False,
        error: str | None = None,
        retry_delay_seconds: int = 0,
        max_attempts: int = 3,
    ) -> str:
        timestamp = self._timestamp()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM generation_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if not row:
                return "missing"
            if not failed:
                status = "completed"
                available_at = timestamp
            elif row["attempts"] >= max_attempts:
                status = "dead_letter"
                available_at = timestamp
            else:
                status = "queued"
                available_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds)
                ).isoformat()
            connection.execute(
                """UPDATE generation_jobs
                   SET status = ?, available_at = ?, last_error = ?, updated_at = ?
                   WHERE job_id = ?""",
                (status, available_at, error, timestamp, job_id),
            )
        return status

    def get_job_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM generation_jobs GROUP BY status"
            ).fetchall()
        return {row["status"]: row["count"] for row in rows}

    def get_queue_metrics(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._connect() as connection:
            counts = connection.execute(
                "SELECT status, COUNT(*) AS count FROM generation_jobs GROUP BY status"
            ).fetchall()
            oldest = connection.execute(
                "SELECT MIN(created_at) FROM generation_jobs WHERE status = 'queued'"
            ).fetchone()[0]
            running = connection.execute(
                "SELECT MIN(claimed_at) FROM generation_jobs WHERE status = 'running'"
            ).fetchone()[0]
            history = connection.execute(
                "SELECT status, COUNT(*) AS count FROM generation_history GROUP BY status"
            ).fetchall()
        age = 0.0
        if oldest:
            age = max(0.0, (now - datetime.fromisoformat(oldest)).total_seconds())
        running_age = 0.0
        if running:
            running_age = max(0.0, (now - datetime.fromisoformat(running)).total_seconds())
        history_counts = {row["status"]: row["count"] for row in history}
        return {
            "counts": {row["status"]: row["count"] for row in counts},
            "oldest_queued_age_seconds": round(age, 2),
            "oldest_running_age_seconds": round(running_age, 2),
            "success_count": history_counts.get("succeeded", 0),
            "failure_count": history_counts.get("failed", 0),
        }

    def complete_generation(
        self,
        user_id: int,
        reservation: dict[str, Any],
        *,
        mode: str,
        prompt: str | None = None,
    ) -> bool:
        timestamp = self._timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE generation_reservations
                SET status = 'completed', mode = ?, prompt = ?, updated_at = ?
                WHERE reservation_id = ? AND user_id = ? AND status = 'reserved'
                """,
                (mode, prompt, timestamp, reservation["reservation_id"], user_id),
            ).rowcount
            if not updated:
                return False
            connection.execute(
                """
                INSERT INTO generation_history
                    (user_id, reservation_id, mode, status, prompt, created_at, completed_at)
                VALUES (?, ?, ?, 'succeeded', ?, ?, ?)
                """,
                (user_id, reservation["reservation_id"], mode, prompt, timestamp, timestamp),
            )
        return True

    def refund_generation(
        self,
        user_id: int,
        reservation: dict[str, Any],
        *,
        mode: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        timestamp = self._timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE generation_reservations SET status = 'refunded', updated_at = ?
                WHERE reservation_id = ? AND user_id = ? AND status = 'reserved'
                """,
                (timestamp, reservation["reservation_id"], user_id),
            ).rowcount
            if updated:
                if reservation["kind"] == "free":
                    connection.execute(
                        "UPDATE wallets SET free_credit_used = 0, updated_at = ? WHERE user_id = ?",
                        (timestamp, user_id),
                    )
                else:
                    connection.execute(
                        "UPDATE wallets SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
                        (reservation.get("cost", 1), timestamp, user_id),
                    )
                row = connection.execute(
                    "SELECT free_credit_used, balance FROM wallets WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                self._record_event(
                    connection,
                    user_id=user_id,
                    event_type="generation_refunded",
                    amount=reservation.get("cost", 1) if reservation["kind"] == "paid" else 0,
                    balance_after=row["balance"],
                    free_credit_used_after=row["free_credit_used"],
                    timestamp=timestamp,
                    reference_id=reservation["reservation_id"],
                    metadata={"kind": reservation["kind"], "mode": mode, "error": error},
                )
                connection.execute(
                    """
                    INSERT INTO generation_history
                        (user_id, reservation_id, mode, status, error, created_at, completed_at)
                    VALUES (?, ?, ?, 'failed', ?, ?, ?)
                    """,
                    (user_id, reservation["reservation_id"], mode or "unknown", error, timestamp, timestamp),
                )
            else:
                row = connection.execute(
                    "SELECT free_credit_used, balance FROM wallets WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
        return dict(row)

    def release_stale_reservations(self, max_age_seconds: int = 1800) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT reservation_id, user_id, kind, cost FROM generation_reservations
                WHERE status = 'reserved' AND created_at < ?
                """,
                (cutoff.isoformat(),),
            ).fetchall()
        released = 0
        for row in rows:
            self.refund_generation(
                row["user_id"],
                {"reservation_id": row["reservation_id"], "kind": row["kind"], "cost": row["cost"]},
                error="stale reservation recovered",
            )
            released += 1
        return released

    def add_payment(
        self,
        *,
        user_id: int,
        telegram_payment_charge_id: str,
        provider_payment_charge_id: str | None,
        payload: str,
        credits: int,
        stars: int,
    ) -> tuple[bool, dict[str, Any]]:
        timestamp = self._timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user_in(connection, user_id, timestamp)
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO payments
                    (telegram_payment_charge_id, provider_payment_charge_id, user_id,
                     payload, credits, stars, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_payment_charge_id, provider_payment_charge_id, user_id, payload, credits, stars, timestamp),
            ).rowcount
            if inserted:
                connection.execute(
                    "UPDATE wallets SET balance = balance + ?, updated_at = ? WHERE user_id = ?",
                    (credits, timestamp, user_id),
                )
            row = connection.execute(
                "SELECT free_credit_used, balance FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if inserted:
                self._record_event(
                    connection,
                    user_id=user_id,
                    event_type="payment_credited",
                    amount=credits,
                    balance_after=row["balance"],
                    free_credit_used_after=row["free_credit_used"],
                    timestamp=timestamp,
                    reference_id=telegram_payment_charge_id,
                    metadata={"stars": stars, "payload": payload},
                )
        return bool(inserted), dict(row)

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class WalletStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
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
                    status TEXT NOT NULL,
                    mode TEXT,
                    prompt TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
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
        self, user_id: int, *, mode: str | None = None, prompt: str | None = None
    ) -> dict[str, Any] | None:
        timestamp = self._timestamp()
        reservation_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_user_in(connection, user_id, timestamp)
            row = connection.execute(
                "SELECT free_credit_used, balance FROM wallets WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row["free_credit_used"] == 0:
                kind = "free"
                new_balance = row["balance"]
                free_used = 1
                connection.execute(
                    "UPDATE wallets SET free_credit_used = 1, updated_at = ? WHERE user_id = ?",
                    (timestamp, user_id),
                )
            elif row["balance"] > 0:
                kind = "paid"
                new_balance = row["balance"] - 1
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
                    (reservation_id, user_id, kind, status, mode, prompt, created_at, updated_at)
                VALUES (?, ?, ?, 'reserved', ?, ?, ?, ?)
                """,
                (reservation_id, user_id, kind, mode, prompt, timestamp, timestamp),
            )
            self._record_event(
                connection,
                user_id=user_id,
                event_type="generation_reserved",
                amount=-1,
                balance_after=new_balance,
                free_credit_used_after=free_used,
                timestamp=timestamp,
                reference_id=reservation_id,
                metadata={"kind": kind, "mode": mode},
            )
        return {
            "reservation_id": reservation_id,
            "kind": kind,
            "balance": new_balance,
            "free_credit_used": free_used,
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
                        "UPDATE wallets SET balance = balance + 1, updated_at = ? WHERE user_id = ?",
                        (timestamp, user_id),
                    )
                row = connection.execute(
                    "SELECT free_credit_used, balance FROM wallets WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                self._record_event(
                    connection,
                    user_id=user_id,
                    event_type="generation_refunded",
                    amount=1,
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
                SELECT reservation_id, user_id, kind FROM generation_reservations
                WHERE status = 'reserved' AND created_at < ?
                """,
                (cutoff.isoformat(),),
            ).fetchall()
        released = 0
        for row in rows:
            self.refund_generation(
                row["user_id"],
                {"reservation_id": row["reservation_id"], "kind": row["kind"]},
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

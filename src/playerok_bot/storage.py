from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .errors import BotError, InventoryNotReadyError
from .models import Credential, DealRecord, RestockRecord, StoredAccount


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StorageError(BotError):
    """Ошибка локального состояния бота."""


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.RLock()
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except Exception:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def _migrate(self) -> None:
        from .inventory import SCHEMA
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    login TEXT NOT NULL,
                    password TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'listed', 'sold')),
                    listed_item_id TEXT,
                    sold_deal_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_listed_account_per_item
                ON accounts(listed_item_id)
                WHERE status = 'listed';

                CREATE TABLE IF NOT EXISTS deals (
                    deal_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    chat_id TEXT,
                    sold_account_id INTEGER REFERENCES accounts(id),
                    next_account_id INTEGER REFERENCES accounts(id),
                    inventory_committed_at TEXT,
                    buyer_notified_at TEXT,
                    deal_confirmed_at TEXT,
                    item_updated_at TEXT,
                    item_published_at TEXT,
                    completed_at TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS restocks (
                    source_deal_id TEXT PRIMARY KEY REFERENCES deals(deal_id),
                    item_id TEXT NOT NULL,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    item_updated_at TEXT,
                    item_published_at TEXT,
                    completed_at TEXT,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS listing_timers (
                    item_id TEXT PRIMARY KEY,
                    initialized_at REAL NOT NULL,
                    last_sale_at REAL,
                    last_published_at REAL,
                    last_bump_attempt_at REAL
                );

                CREATE TABLE IF NOT EXISTS bumps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id TEXT NOT NULL,
                    attempted_at REAL NOT NULL,
                    price_rub INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'success', 'uncertain', 'skipped')),
                    error_code TEXT
                );
                """
            )
            connection.executescript(SCHEMA)

    @staticmethod
    def _account(row: sqlite3.Row | None) -> StoredAccount | None:
        if row is None:
            return None
        return StoredAccount(
            id=row["id"],
            fingerprint=row["fingerprint"],
            login=row["login"],
            password=row["password"],
            status=row["status"],
            listed_item_id=row["listed_item_id"],
            sold_deal_id=row["sold_deal_id"],
        )

    @staticmethod
    def _deal(row: sqlite3.Row | None) -> DealRecord | None:
        if row is None:
            return None
        return DealRecord(
            deal_id=row["deal_id"],
            item_id=row["item_id"],
            chat_id=row["chat_id"],
            sold_account_id=row["sold_account_id"],
            next_account_id=row["next_account_id"],
            inventory_committed_at=row["inventory_committed_at"],
            buyer_notified_at=row["buyer_notified_at"],
            deal_confirmed_at=row["deal_confirmed_at"],
            item_updated_at=row["item_updated_at"],
            item_published_at=row["item_published_at"],
            completed_at=row["completed_at"],
            error_code=row["error_code"],
        )

    def import_accounts(self, credentials: list[Credential]) -> int:
        inserted = 0
        now = _utc_now()
        with self._transaction() as connection:
            for credential in credentials:
                conflict = connection.execute(
                    'SELECT 1 FROM accounts WHERE lower(login)=lower(?) AND fingerprint!=?',
                    (credential.login, credential.fingerprint),
                ).fetchone()
                if conflict:
                    raise StorageError('Этот логин уже есть в базе с другими данными. Проверьте пароль; повторный импорт остановлен')
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO accounts(
                        fingerprint, login, password, status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'queued', ?, ?)
                    """,
                    (credential.fingerprint, credential.login, credential.password, now, now),
                )
                inserted += cursor.rowcount
            from .inventory import replenish_playerok
            replenish_playerok(connection)
        return inserted

    def bootstrap_listing(self, item_id: str, credential: Credential) -> StoredAccount:
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM accounts WHERE status = 'listed' AND listed_item_id = ?",
                (item_id,),
            ).fetchone()
            if existing:
                account = self._account(existing)
                if account.fingerprint != credential.fingerprint:
                    raise StorageError(
                        "В базе другой аккаунт уже помечен как выставленный для этого лота"
                    )
                return account

            row = connection.execute(
                "SELECT * FROM accounts WHERE fingerprint = ?", (credential.fingerprint,)
            ).fetchone()
            if row is None:
                raise InventoryNotReadyError(
                    "Добавьте в accaunts.txt логин и пароль аккаунта, указанного в текущем лоте"
                )
            if row["status"] != "queued":
                if row["status"] == "listed":
                    raise StorageError("Аккаунт текущего лота уже выставлен в другом объявлении")
                raise StorageError("Аккаунт текущего лота уже помечен как проданный")

            allocation = connection.execute("SELECT platform FROM allocations WHERE account_id=?", (row['id'],)).fetchone()
            if allocation and allocation[0] == 'funpay':
                raise StorageError('Аккаунт уже зарезервирован для FunPay')

            now = _utc_now()
            connection.execute(
                """
                UPDATE accounts
                SET status = 'listed', listed_item_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (item_id, now, row["id"]),
            )
            return self._account(
                connection.execute("SELECT * FROM accounts WHERE id = ?", (row["id"],)).fetchone()
            )

    def prepare_deal(self, deal_id: str, item_id: str, chat_id: str | None) -> DealRecord:
        with self._transaction() as connection:
            now = _utc_now()
            connection.execute(
                """
                INSERT OR IGNORE INTO deals(deal_id, item_id, chat_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (deal_id, item_id, chat_id, now, now),
            )
            connection.execute(
                "UPDATE deals SET chat_id = COALESCE(chat_id, ?), updated_at = ? WHERE deal_id = ?",
                (chat_id, now, deal_id),
            )
            deal_row = connection.execute(
                "SELECT * FROM deals WHERE deal_id = ?", (deal_id,)
            ).fetchone()
            if deal_row["item_id"] != item_id:
                raise StorageError("ID лота в сохранённой сделке не совпадает")
            if deal_row["inventory_committed_at"]:
                return self._deal(deal_row)

            listed = connection.execute(
                "SELECT * FROM accounts WHERE status = 'listed' AND listed_item_id = ?",
                (item_id,),
            ).fetchone()
            if listed is None:
                raise StorageError("Для проданного лота не найден выставленный аккаунт")

            connection.execute(
                """
                UPDATE accounts
                SET status = 'sold', sold_deal_id = ?, listed_item_id = NULL, updated_at = ?
                WHERE id = ?
                """,
                (deal_id, now, listed["id"]),
            )

            from .inventory import next_playerok, replenish_playerok
            next_row = next_playerok(connection)
            next_id = None
            if next_row is not None:
                next_id = next_row["id"]
                connection.execute(
                    """
                    UPDATE accounts
                    SET status = 'listed', listed_item_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (item_id, now, next_id),
                )
            replenish_playerok(connection)

            connection.execute(
                """
                UPDATE deals
                SET sold_account_id = ?, next_account_id = ?, inventory_committed_at = ?,
                    error_code = NULL, updated_at = ?
                WHERE deal_id = ?
                """,
                (listed["id"], next_id, now, now, deal_id),
            )
            self._touch_timer(connection, item_id, "last_sale_at", datetime.fromisoformat(now).timestamp())
            return self._deal(
                connection.execute("SELECT * FROM deals WHERE deal_id = ?", (deal_id,)).fetchone()
            )

    def get_deal(self, deal_id: str) -> DealRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM deals WHERE deal_id = ?", (deal_id,)
            ).fetchone()
            return self._deal(row)

    @staticmethod
    def _restock(row: sqlite3.Row | None) -> RestockRecord | None:
        if row is None:
            return None
        return RestockRecord(
            source_deal_id=row["source_deal_id"],
            item_id=row["item_id"],
            account_id=row["account_id"],
            item_updated_at=row["item_updated_at"],
            item_published_at=row["item_published_at"],
            completed_at=row["completed_at"],
            error_code=row["error_code"],
        )

    def exhausted_deal_id(self, item_id: str) -> str | None:
        """Последняя завершённая продажа без следующего аккаунта."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM deals WHERE item_id = ? ORDER BY rowid DESC LIMIT 1",
                (item_id,),
            ).fetchone()
            if row is None or not row["completed_at"] or row["next_account_id"] is not None:
                return None
            restock = self._connection.execute(
                "SELECT 1 FROM restocks WHERE source_deal_id = ?",
                (row["deal_id"],),
            ).fetchone()
            return None if restock else row["deal_id"]

    def pending_restock(self, item_id: str) -> RestockRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM restocks WHERE item_id = ? AND completed_at IS NULL
                ORDER BY rowid LIMIT 1
                """,
                (item_id,),
            ).fetchone()
            return self._restock(row)

    def reserve_restock(self, item_id: str) -> RestockRecord | None:
        """Зарезервировать первый новый аккаунт для исчерпанного лота."""
        with self._transaction() as connection:
            pending = self.pending_restock(item_id)
            if pending is not None:
                return pending
            if self.get_listed_account(item_id) or self.unfinished_deals(item_id):
                return None
            deal_id = self.exhausted_deal_id(item_id)
            if deal_id is None:
                return None
            from .inventory import next_playerok, replenish_playerok
            account = next_playerok(connection)
            if account is None:
                return None
            now = _utc_now()
            connection.execute(
                """
                UPDATE accounts SET status = 'listed', listed_item_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (item_id, now, account["id"]),
            )
            replenish_playerok(connection)
            connection.execute(
                """
                INSERT INTO restocks(source_deal_id, item_id, account_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (deal_id, item_id, account["id"], now, now),
            )
            return self.pending_restock(item_id)

    def mark_restock(self, source_deal_id: str, column: str) -> RestockRecord:
        if column not in {"item_updated_at", "item_published_at", "completed_at"}:
            raise ValueError(f"Недопустимое поле восстановления: {column}")
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                f"UPDATE restocks SET {column} = ?, error_code = NULL, updated_at = ? "
                "WHERE source_deal_id = ?",
                (now, now, source_deal_id),
            )
            record = self._restock(connection.execute(
                "SELECT * FROM restocks WHERE source_deal_id = ?", (source_deal_id,)
            ).fetchone())
            if column == "item_published_at":
                self._touch_timer(connection, record.item_id, "last_published_at", datetime.fromisoformat(now).timestamp())
            return record

    def mark_restock_error(self, source_deal_id: str, error_code: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE restocks SET error_code = ?, updated_at = ? WHERE source_deal_id = ?",
                (error_code[:100], _utc_now(), source_deal_id),
            )

    def get_account(self, account_id: int | None) -> StoredAccount | None:
        if account_id is None:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            return self._account(row)

    def get_listed_account(self, item_id: str) -> StoredAccount | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM accounts WHERE status = 'listed' AND listed_item_id = ?",
                (item_id,),
            ).fetchone()
            return self._account(row)

    def unfinished_deals(self, item_id: str) -> list[DealRecord]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM deals
                WHERE item_id = ? AND completed_at IS NULL
                ORDER BY created_at
                """,
                (item_id,),
            ).fetchall()
            return [self._deal(row) for row in rows]

    def mark_deal(self, deal_id: str, column: str) -> DealRecord:
        allowed = {
            "buyer_notified_at",
            "deal_confirmed_at",
            "item_updated_at",
            "item_published_at",
            "completed_at",
        }
        if column not in allowed:
            raise ValueError(f"Недопустимое поле этапа: {column}")
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                f"UPDATE deals SET {column} = ?, error_code = NULL, updated_at = ? WHERE deal_id = ?",
                (now, now, deal_id),
            )
            record = self._deal(
                connection.execute("SELECT * FROM deals WHERE deal_id = ?", (deal_id,)).fetchone()
            )
            if column == "item_published_at":
                self._touch_timer(connection, record.item_id, "last_published_at", datetime.fromisoformat(now).timestamp())
            return record

    @staticmethod
    def _touch_timer(connection, item_id: str, column: str, timestamp: float) -> None:
        if column not in {"last_sale_at", "last_published_at", "last_bump_attempt_at"}:
            raise ValueError(f"Некорректный таймер: {column}")
        connection.execute(
            f"INSERT INTO listing_timers(item_id, initialized_at, {column}) VALUES (?, ?, ?) "
            f"ON CONFLICT(item_id) DO UPDATE SET {column} = excluded.{column}",
            (item_id, timestamp, timestamp),
        )

    def bump_baseline(self, item_id: str, now: float) -> float:
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO listing_timers(item_id, initialized_at) VALUES (?, ?)",
                (item_id, now),
            )
            row = connection.execute(
                "SELECT * FROM listing_timers WHERE item_id = ?", (item_id,)
            ).fetchone()
            return max(row[key] for key in (
                "initialized_at", "last_sale_at", "last_published_at", "last_bump_attempt_at"
            ) if row[key] is not None)

    def claim_bump(self, item_id: str, now: float, interval: int, price: int) -> int | None:
        """Зафиксировать попытку до платного запроса, включая защиту при перезапуске."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM listing_timers WHERE item_id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return None
            baseline = max(row[key] for key in (
                "initialized_at", "last_sale_at", "last_published_at", "last_bump_attempt_at"
            ) if row[key] is not None)
            if now - baseline < interval:
                return None
            if (
                not self.get_listed_account(item_id)
                or self.unfinished_deals(item_id)
                or self.pending_restock(item_id)
            ):
                return None
            self._touch_timer(connection, item_id, "last_bump_attempt_at", now)
            cursor = connection.execute(
                "INSERT INTO bumps(item_id, attempted_at, price_rub, status) VALUES (?, ?, ?, 'pending')",
                (item_id, now, price),
            )
            return cursor.lastrowid

    def finish_bump(
        self, operation_id: int, status: str, error_code: str | None = None,
        *, price: int | None = None,
    ) -> None:
        if status not in {"success", "uncertain", "skipped"}:
            raise ValueError(f"Некорректное состояние поднятия: {status}")
        with self._transaction() as connection:
            connection.execute(
                "UPDATE bumps SET status = ?, error_code = ?, price_rub = COALESCE(?, price_rub) WHERE id = ?",
                (status, error_code, price, operation_id),
            )

    def mark_error(self, deal_id: str, error_code: str) -> None:
        with self._transaction() as connection:
            now = _utc_now()
            connection.execute(
                "UPDATE deals SET error_code = ?, updated_at = ? WHERE deal_id = ?",
                (error_code[:100], now, deal_id),
            )

    def initialize_state_int(self, key: str, initial_value: int) -> int:
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO bot_state(key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                (key, str(initial_value), now),
            )
            row = connection.execute(
                "SELECT value FROM bot_state WHERE key = ?", (key,)
            ).fetchone()
            try:
                return int(row["value"])
            except (TypeError, ValueError) as exc:
                raise StorageError(f"Некорректное числовое состояние: {key}") from exc

    def set_state_int(self, key: str, value: int) -> None:
        now = _utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO bot_state(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (key, str(value), now),
            )

    def counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT status, COUNT(*) AS amount FROM accounts GROUP BY status"
            ).fetchall()
            result = {"queued": 0, "listed": 0, "sold": 0}
            result.update({row["status"]: row["amount"] for row in rows})
            result["deals"] = self._connection.execute(
                "SELECT COUNT(*) FROM deals"
            ).fetchone()[0]
            result["completed_deals"] = self._connection.execute(
                "SELECT COUNT(*) FROM deals WHERE completed_at IS NOT NULL"
            ).fetchone()[0]
            result["failed_deals"] = self._connection.execute(
                "SELECT COUNT(*) FROM deals WHERE error_code IS NOT NULL AND completed_at IS NULL"
            ).fetchone()[0]
            result["pending_restocks"] = self._connection.execute(
                "SELECT COUNT(*) FROM restocks WHERE completed_at IS NULL"
            ).fetchone()[0]
            result["failed_restocks"] = self._connection.execute(
                "SELECT COUNT(*) FROM restocks WHERE error_code IS NOT NULL AND completed_at IS NULL"
            ).fetchone()[0]
            result["successful_bumps"] = self._connection.execute(
                "SELECT COUNT(*) FROM bumps WHERE status = 'success'"
            ).fetchone()[0]
            result["uncertain_bumps"] = self._connection.execute(
                "SELECT COUNT(*) FROM bumps WHERE status IN ('pending', 'uncertain')"
            ).fetchone()[0]
            return result

    def listing_counts(self) -> list[dict[str, str | int]]:
        """Сводка по лотам без логинов и паролей."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT item_id,
                    (SELECT COUNT(*) FROM accounts a
                     WHERE a.status = 'listed' AND a.listed_item_id = items.item_id) AS listed,
                    (SELECT COUNT(*) FROM accounts a JOIN deals d ON a.sold_deal_id = d.deal_id
                     WHERE a.status = 'sold' AND d.item_id = items.item_id) AS sold,
                    (SELECT COUNT(*) FROM bumps b
                     WHERE b.item_id = items.item_id AND b.status = 'success') AS bumps
                FROM (
                    SELECT listed_item_id AS item_id FROM accounts
                    WHERE listed_item_id IS NOT NULL
                    UNION SELECT item_id FROM deals
                ) AS items
                ORDER BY item_id
                """
            ).fetchall()
            return [dict(row) for row in rows]

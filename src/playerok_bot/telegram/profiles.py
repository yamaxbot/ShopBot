from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet

from ..errors import ConfigurationError
from ..storage import Storage


@dataclass
class Profile:
    user_id: int
    playerok_id: str | None
    enabled: bool
    last_notified: int
    config: dict = field(repr=False)
    funpay_id: str | None = None


class Profiles:
    """Реестр настроек; товарные аккаунты хранятся отдельно для каждого Telegram ID."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        key_file = self.root / "secret.key"
        db_file = self.root / "profiles.sqlite3"
        if not key_file.exists():
            if db_file.exists():
                raise ConfigurationError("Отсутствует secret.key. Восстановите его из резервной копии")
            with key_file.open("xb") as file:
                file.write(Fernet.generate_key())
            key_file.chmod(0o600)
        self.cipher = Fernet(key_file.read_bytes())
        self.connection = sqlite3.connect(db_file)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                playerok_id TEXT UNIQUE,
                config BLOB NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                last_notified INTEGER NOT NULL DEFAULT 0
            )
        """)
        columns = {row[1] for row in self.connection.execute('PRAGMA table_info(profiles)')}
        if 'funpay_id' not in columns:
            self.connection.execute('ALTER TABLE profiles ADD COLUMN funpay_id TEXT')
        self.connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS unique_funpay_owner ON profiles(funpay_id)')
        self.connection.commit()

    def close(self):
        self.connection.close()

    @staticmethod
    def validate_user_id(user_id: int):
        if not isinstance(user_id, int) or user_id <= 0 or user_id >= 2**63:
            raise ConfigurationError("Некорректный Telegram ID")

    def user_dir(self, user_id: int) -> Path:
        self.validate_user_id(user_id)
        result = self.root / "users" / str(user_id)
        result.mkdir(parents=True, exist_ok=True)
        return result

    def storage(self, user_id: int) -> Storage:
        return Storage(self.user_dir(user_id) / "inventory.sqlite3")

    def _encode(self, config: dict) -> bytes:
        return self.cipher.encrypt(json.dumps(config, ensure_ascii=False).encode())

    def _profile(self, row) -> Profile:
        return Profile(row["user_id"], row["playerok_id"], bool(row["enabled"]), row["last_notified"],
                       {**self.defaults(), **json.loads(self.cipher.decrypt(row["config"]).decode())}, row['funpay_id'])

    @staticmethod
    def defaults():
        return {"cookies": "", "user_agent": "", "items": [], "price_limit": 50, "bump": True,
                "funpay_key": "", "funpay_agent": "", "funpay_items": []}

    def get(self, user_id: int) -> Profile:
        self.validate_user_id(user_id)
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO profiles(user_id, config) VALUES (?, ?)",
                (user_id, self._encode(self.defaults())),
            )
        return self._profile(self.connection.execute("SELECT * FROM profiles WHERE user_id = ?", (user_id,)).fetchone())

    def all(self) -> list[Profile]:
        return [self._profile(row) for row in self.connection.execute("SELECT * FROM profiles ORDER BY user_id")]

    def connect(self, user_id: int, playerok_id: str, cookies: str, user_agent: str):
        profile = self.get(user_id)
        if profile.playerok_id and profile.playerok_id != playerok_id:
            raise ConfigurationError("К вашему Telegram уже подключён другой профиль Playerok")
        config = {**profile.config, "cookies": cookies, "user_agent": user_agent}
        try:
            with self.connection:
                self.connection.execute(
                    "UPDATE profiles SET playerok_id = ?, config = ?, enabled = 0 WHERE user_id = ?",
                    (playerok_id, self._encode(config), user_id),
                )
        except sqlite3.IntegrityError as exc:
            raise ConfigurationError("Этот профиль Playerok уже подключён к другому пользователю бота") from exc

    def update(self, user_id: int, **changes):
        if set(changes) - {"items", "price_limit", "bump", "funpay_items"}:
            raise ConfigurationError("Неизвестный параметр профиля")
        profile = self.get(user_id)
        with self.connection:
            self.connection.execute("UPDATE profiles SET config = ?, enabled = 0 WHERE user_id = ?",
                                    (self._encode({**profile.config, **changes}), user_id))

    def connect_funpay(self, user_id: int, funpay_id: str, key: str, user_agent: str):
        profile = self.get(user_id)
        if profile.funpay_id and profile.funpay_id != funpay_id:
            raise ConfigurationError('К вашему Telegram уже подключён другой профиль FunPay')
        config = {**profile.config, 'funpay_key': key, 'funpay_agent': user_agent}
        try:
            with self.connection:
                self.connection.execute('UPDATE profiles SET funpay_id=?,config=?,enabled=0 WHERE user_id=?',
                                        (funpay_id, self._encode(config), user_id))
        except sqlite3.IntegrityError:
            raise ConfigurationError('Этот FunPay уже подключён к другому пользователю бота') from None

    def set_enabled(self, user_id: int, enabled: bool):
        self.get(user_id)
        with self.connection:
            self.connection.execute("UPDATE profiles SET enabled = ? WHERE user_id = ?", (int(enabled), user_id))

    def mark_notified(self, user_id: int, count: int):
        with self.connection:
            self.connection.execute("UPDATE profiles SET last_notified = ? WHERE user_id = ?", (count, user_id))

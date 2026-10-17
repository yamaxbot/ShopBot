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
    group_id: int = 1
    group_name: str = "Основная"


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
        if not self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='product_groups'").fetchone():
            backup = sqlite3.connect(self.root / 'profiles.before-groups.sqlite3')
            try:
                self.connection.backup(backup)
            finally:
                backup.close()
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS product_groups (
                user_id INTEGER NOT NULL REFERENCES profiles(user_id),
                group_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                config BLOB NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                last_notified INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, group_id), UNIQUE(user_id, name)
            );
            CREATE TABLE IF NOT EXISTS selected_groups (
                user_id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL DEFAULT 1
            );
        """)
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO product_groups SELECT user_id,1,'Основная',config,enabled,last_notified FROM profiles")
            self.connection.execute("INSERT OR IGNORE INTO selected_groups SELECT user_id,1 FROM profiles")
            # Исправление перепутанных исходных ставок; пользовательские пары сохраняем.
            for row in self.connection.execute('SELECT user_id,group_id,config FROM product_groups').fetchall():
                config = json.loads(self.cipher.decrypt(row['config']).decode())
                if config.get('withdrawal_defaults_revision', 0) < 2:
                    if (config.get('playerok_withdrawal'), config.get('funpay_withdrawal')) == (3, 6):
                        config.update(playerok_withdrawal=6, funpay_withdrawal=3)
                    config['withdrawal_defaults_revision'] = 2
                    self.connection.execute('UPDATE product_groups SET config=? WHERE user_id=? AND group_id=?',
                                            (self._encode(config), row['user_id'], row['group_id']))

    def close(self):
        self.connection.close()

    @staticmethod
    def validate_user_id(user_id: int):
        if not isinstance(user_id, int) or user_id <= 0 or user_id >= 2**63:
            raise ConfigurationError("Некорректный Telegram ID")

    def user_dir(self, user_id: int, group_id: int = 1) -> Path:
        self.validate_user_id(user_id)
        result = self.root / "users" / str(user_id)
        if group_id != 1:
            if not isinstance(group_id, int) or group_id < 1:
                raise ConfigurationError("Некорректная группа")
            result = result / 'groups' / str(group_id)
        result.mkdir(parents=True, exist_ok=True)
        return result

    def storage(self, user_id: int, group_id: int | None = None) -> Storage:
        profile = self.get(user_id, group_id)
        return Storage(self.user_dir(user_id, profile.group_id) / "inventory.sqlite3",
                       registry=self.user_dir(user_id) / 'ownership.sqlite3', group_id=profile.group_id)

    def _encode(self, config: dict) -> bytes:
        return self.cipher.encrypt(json.dumps(config, ensure_ascii=False).encode())

    @staticmethod
    def defaults():
        return {"cookies": "", "user_agent": "", "items": [], "price_limit": 50, "bump": True,
                "funpay_key": "", "funpay_agent": "", "funpay_items": [],
                "playerok_fee": 0, "funpay_fee": 0,
                "playerok_withdrawal": 6, "funpay_withdrawal": 3, "withdrawal_defaults_revision": 2}

    def get(self, user_id: int, group_id: int | None = None) -> Profile:
        self.validate_user_id(user_id)
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO profiles(user_id,config) VALUES (?,?)",
                                    (user_id, self._encode(self.defaults())))
            self.connection.execute("INSERT OR IGNORE INTO product_groups SELECT user_id,1,'Основная',config,enabled,last_notified FROM profiles WHERE user_id=?", (user_id,))
            self.connection.execute("INSERT OR IGNORE INTO selected_groups VALUES (?,1)", (user_id,))
        if group_id is None:
            group_id = self.connection.execute('SELECT group_id FROM selected_groups WHERE user_id=?', (user_id,)).fetchone()[0]
        row = self.connection.execute('SELECT * FROM profiles WHERE user_id=?', (user_id,)).fetchone()
        group = self.connection.execute('SELECT * FROM product_groups WHERE user_id=? AND group_id=?', (user_id, group_id)).fetchone()
        if group is None:
            raise ConfigurationError('Группа не найдена. Список — /groups')
        auth = json.loads(self.cipher.decrypt(row['config']).decode())
        config = {**self.defaults(), **json.loads(self.cipher.decrypt(group['config']).decode())}
        for key in ('cookies', 'user_agent', 'funpay_key', 'funpay_agent'):
            config[key] = auth.get(key, '')
        return Profile(user_id, row['playerok_id'], bool(group['enabled']), group['last_notified'],
                       config, row['funpay_id'], group_id, group['name'])

    def groups(self, user_id: int) -> list[Profile]:
        self.get(user_id)
        return [self.get(user_id, row[0]) for row in self.connection.execute(
            'SELECT group_id FROM product_groups WHERE user_id=? ORDER BY group_id', (user_id,)).fetchall()]

    def all(self) -> list[Profile]:
        return [self.get(row[0], row[1]) for row in self.connection.execute(
            'SELECT user_id,group_id FROM product_groups ORDER BY user_id,group_id').fetchall()]

    def create_group(self, user_id: int, name: str) -> Profile:
        self.get(user_id)
        for group in self.groups(user_id):
            storage = self.storage(user_id, group.group_id)
            storage.close()
        name = name.strip()
        if not 1 <= len(name) <= 60 or any(ord(c) < 32 for c in name):
            raise ConfigurationError('Название группы: от 1 до 60 символов')
        if len(self.groups(user_id)) >= 30:
            raise ConfigurationError('Можно создать не больше 30 групп')
        try:
            with self.connection:
                group_id = self.connection.execute('SELECT COALESCE(MAX(group_id),0)+1 FROM product_groups WHERE user_id=?', (user_id,)).fetchone()[0]
                self.connection.execute('INSERT INTO product_groups(user_id,group_id,name,config) VALUES (?,?,?,?)',
                                        (user_id, group_id, name, self._encode(self.defaults())))
                self.connection.execute('UPDATE selected_groups SET group_id=? WHERE user_id=?', (group_id, user_id))
        except sqlite3.IntegrityError:
            raise ConfigurationError('Группа с таким названием уже существует') from None
        return self.get(user_id)

    def select_group(self, user_id: int, group_id: int):
        self.get(user_id, group_id)
        with self.connection:
            self.connection.execute('UPDATE selected_groups SET group_id=? WHERE user_id=?', (group_id, user_id))

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
        if set(changes) - {"items", "price_limit", "bump", "funpay_items", "playerok_fee", "funpay_fee", "playerok_withdrawal", "funpay_withdrawal"}:
            raise ConfigurationError("Неизвестный параметр профиля")
        profile = self.get(user_id)
        from ..ownership import Ownership
        ownership = Ownership(self.user_dir(user_id) / 'ownership.sqlite3', profile.group_id)
        for key, platform in (('items', 'playerok'), ('funpay_items', 'funpay')):
            if key in changes:
                for other in self.groups(user_id):
                    if other.group_id != profile.group_id and set(changes[key]) & set(other.config[key]):
                        raise ConfigurationError(f'Объявление уже закреплено за группой «{other.group_name}»')
                ownership.claim_lots(platform, changes[key])
        with self.connection:
            self.connection.execute("UPDATE product_groups SET config = ?, enabled = 0 WHERE user_id = ? AND group_id=?",
                                    (self._encode({**profile.config, **changes}), user_id, profile.group_id))

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

    def set_enabled(self, user_id: int, enabled: bool, group_id: int | None = None):
        profile = self.get(user_id, group_id)
        with self.connection:
            self.connection.execute("UPDATE product_groups SET enabled = ? WHERE user_id = ? AND group_id=?", (int(enabled), user_id, profile.group_id))

    def mark_notified(self, user_id: int, count: int, group_id: int | None = None):
        profile = self.get(user_id, group_id)
        with self.connection:
            self.connection.execute("UPDATE product_groups SET last_notified = ? WHERE user_id = ? AND group_id=?", (count, user_id, profile.group_id))

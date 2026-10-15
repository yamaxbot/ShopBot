from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from ..config import Settings
from ..errors import ConfigurationError
from .config import TelegramSettings
from .profiles import Profile, Profiles
from .workers import Workers
from .lock import HostLock


def migrate_user(user_id: int):
    """Скопировать прежнюю рабочую базу в личный профиль без удаления исходника."""
    root = Path.cwd()
    host = TelegramSettings.load(root, require_token=False)
    with HostLock(host.data_dir):
        _migrate(user_id, root, host)


def _migrate(user_id: int, root: Path, host: TelegramSettings):
    legacy = Settings.load(root)
    if not legacy.database_path.is_file():
        raise ConfigurationError("Прежняя SQLite-база не найдена")
    profiles = Profiles(host.data_dir)
    try:
        profile = profiles.get(user_id)
        if profile.enabled:
            raise ConfigurationError("Сначала остановите пользовательский обработчик")
        destination = profiles.user_dir(user_id) / "inventory.sqlite3"
        if destination.exists():
            con = sqlite3.connect(destination)
            try:
                tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "accounts" in tables and con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]:
                    raise ConfigurationError("У пользователя уже есть аккаунты. Перенос отменён")
            finally:
                con.close()
        candidate = Profile(user_id, profile.playerok_id, False, profile.last_notified, {
            "cookies": legacy.cookies, "user_agent": legacy.user_agent,
            "items": list(legacy.item_references), "price_limit": legacy.premium_price_limit_rub,
            "bump": legacy.auto_bump_enabled,
        })
        identity = asyncio.run(Workers(profiles, host.max_workers).probe(candidate, "identity"))
        profiles.connect(user_id, identity["id"], legacy.cookies, legacy.user_agent)
        profiles.update(user_id, items=list(legacy.item_references),
                        price_limit=legacy.premium_price_limit_rub, bump=legacy.auto_bump_enabled)
        source = sqlite3.connect(legacy.database_path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        storage = profiles.storage(user_id)
        try:
            completed = storage.counts()["completed_deals"]
        finally:
            storage.close()
        profiles.mark_notified(user_id, completed - completed % 10)
        print("Настройки и база перенесены. В Telegram выполните /stats, /check, затем /run.")
    finally:
        profiles.close()

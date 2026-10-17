"""Неизменяемая привязка аккаунтов и объявлений к группе продавца."""
import sqlite3
from pathlib import Path

from .errors import ConfigurationError


class Ownership:
    def __init__(self, path: Path, group_id: int):
        self.path, self.group_id = path, group_id
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS owners (kind TEXT, key TEXT, group_id INTEGER, PRIMARY KEY(kind,key))')

    def claim(self, kind, keys):
        with sqlite3.connect(self.path, timeout=30) as db:
            db.execute('BEGIN IMMEDIATE')
            for key in keys:
                row = db.execute('SELECT group_id FROM owners WHERE kind=? AND key=?', (kind, key)).fetchone()
                if row and row[0] != self.group_id:
                    raise ConfigurationError(f'Аккаунт или объявление уже закреплены за группой №{row[0]}. Смешивание групп запрещено.')
                db.execute('INSERT OR IGNORE INTO owners VALUES (?,?,?)', (kind, key, self.group_id))

    def claim_accounts(self, credentials):
        self.claim('account', [c.fingerprint for c in credentials])

    def claim_lots(self, platform, ids):
        self.claim(platform, ids)

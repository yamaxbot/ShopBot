"""Общий склад одного пользователя и эксклюзивные резервы площадок."""
from __future__ import annotations

import json

from .models import Credential
from .storage import StorageError, _utc_now


SCHEMA = """
CREATE INDEX IF NOT EXISTS accounts_login_lookup ON accounts(lower(login));
CREATE TABLE IF NOT EXISTS allocations (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    platform TEXT NOT NULL CHECK(platform IN ('playerok', 'funpay')),
    lot_id TEXT,
    state TEXT NOT NULL DEFAULT 'reserved',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS funpay_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    active INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'prepared',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS funpay_lots (
    lot_id TEXT PRIMARY KEY,
    amount INTEGER NOT NULL,
    active INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS funpay_sales (
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id),
    order_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS funpay_orders (
    order_id TEXT PRIMARY KEY,
    terminal INTEGER NOT NULL DEFAULT 0
);
"""


def replenish_playerok(connection):
    flag = connection.execute("SELECT value FROM bot_state WHERE key='platform_reserve'").fetchone()
    if flag is None or flag[0] != '1':
        return
    count = connection.execute("""
        SELECT COUNT(*) FROM accounts a JOIN allocations r ON r.account_id=a.id
        WHERE a.status='queued' AND r.platform='playerok'
    """).fetchone()[0]
    rows = connection.execute("""
        SELECT a.id FROM accounts a LEFT JOIN allocations r ON r.account_id=a.id
        WHERE a.status='queued' AND r.account_id IS NULL ORDER BY a.id LIMIT ?
    """, (max(0, 5-count),)).fetchall()
    for row in rows:
        connection.execute("INSERT INTO allocations(account_id,platform,updated_at) VALUES (?,'playerok',?)",
                           (row[0], _utc_now()))


def next_playerok(connection):
    replenish_playerok(connection)
    managed = connection.execute("SELECT value FROM bot_state WHERE key='platform_reserve'").fetchone()
    if managed and managed[0] == '1':
        return connection.execute("""
            SELECT a.* FROM accounts a JOIN allocations r ON r.account_id=a.id
            WHERE a.status='queued' AND r.platform='playerok' ORDER BY a.id LIMIT 1
        """).fetchone()
    return connection.execute("""
        SELECT a.* FROM accounts a LEFT JOIN allocations r ON r.account_id=a.id
        WHERE a.status='queued' AND (r.account_id IS NULL OR r.platform='playerok')
        ORDER BY a.id LIMIT 1
    """).fetchone()


class Inventory:
    def __init__(self, storage):
        self.storage = storage

    def configure(self, playerok: bool):
        self.storage.set_state_int('platform_reserve', int(playerok))
        with self.storage._transaction() as db:
            if not playerok:
                db.execute("DELETE FROM allocations WHERE platform='playerok' AND account_id IN (SELECT id FROM accounts WHERE status='queued')")
            replenish_playerok(db)

    def reserve(self, lot_id: str, amount: int):
        with self.storage._transaction() as db:
            replenish_playerok(db)
            rows = db.execute("""
                SELECT a.* FROM accounts a LEFT JOIN allocations r ON r.account_id=a.id
                WHERE a.status='queued' AND r.account_id IS NULL ORDER BY a.id LIMIT ?
            """, (max(0, amount),)).fetchall()
            for row in rows:
                db.execute("INSERT INTO allocations VALUES (?,'funpay',?,'staged',?)",
                           (row['id'], lot_id, _utc_now()))
            return [self.storage._account(row) for row in rows]

    def snapshot(self, lot_id: str, credentials: list[Credential], active: bool):
        """Резервировать фактически присутствующий товар. Пропавшее — в карантин."""
        fingerprints = {c.fingerprint for c in credentials}
        if len(fingerprints) != len(credentials) or len({c.login.casefold() for c in credentials}) != len(credentials):
            raise StorageError('В FunPay-лоте повторяются аккаунты')
        with self.storage._transaction() as db:
            for credential in credentials:
                row = db.execute('SELECT * FROM accounts WHERE fingerprint=?', (credential.fingerprint,)).fetchone()
                if row is None:
                    if db.execute('SELECT 1 FROM accounts WHERE lower(login)=lower(?)', (credential.login,)).fetchone():
                        raise StorageError('В FunPay обнаружен логин, уже сохранённый с другим паролем')
                    db.execute("INSERT INTO accounts(fingerprint,login,password,status,created_at,updated_at) VALUES (?,?,?,'queued',?,?)",
                               (credential.fingerprint, credential.login, credential.password, _utc_now(), _utc_now()))
                    row = db.execute('SELECT * FROM accounts WHERE fingerprint=?', (credential.fingerprint,)).fetchone()
                reservation = db.execute('SELECT * FROM allocations WHERE account_id=?', (row['id'],)).fetchone()
                if row['status'] != 'queued':
                    raise StorageError('В FunPay найден проданный аккаунт или аккаунт активного Playerok-лота')
                if reservation and not (reservation['platform'] == 'playerok' or (
                    reservation['lot_id'] == lot_id and reservation['state'] in {'listed', 'staged'})):
                    raise StorageError('Аккаунт FunPay уже закреплён за другим лотом или находится в карантине')
                db.execute("INSERT INTO allocations VALUES (?,'funpay',?,'listed',?) ON CONFLICT(account_id) DO UPDATE SET platform='funpay',lot_id=excluded.lot_id,state='listed',updated_at=excluded.updated_at",
                           (row['id'], lot_id, _utc_now()))
            rows = db.execute("SELECT a.id,a.fingerprint FROM accounts a JOIN allocations r ON a.id=r.account_id WHERE r.platform='funpay' AND r.lot_id=? AND r.state='listed'", (lot_id,)).fetchall()
            for row in rows:
                if row['fingerprint'] not in fingerprints:
                    db.execute("UPDATE allocations SET state='missing',updated_at=? WHERE account_id=?", (_utc_now(), row['id']))
            db.execute("INSERT INTO funpay_lots VALUES (?,?,?,?) ON CONFLICT(lot_id) DO UPDATE SET amount=excluded.amount,active=excluded.active,updated_at=excluded.updated_at",
                       (lot_id, len(credentials), int(active), _utc_now()))

    def accounts(self, lot_id: str, state='staged'):
        with self.storage._lock:
            rows = self.storage._connection.execute("SELECT a.* FROM accounts a JOIN allocations r ON a.id=r.account_id WHERE r.platform='funpay' AND r.lot_id=? AND r.state=? AND a.status='queued' ORDER BY a.id", (lot_id, state)).fetchall()
            return [self.storage._account(row) for row in rows]

    def transfer(self, donor: str, recipient: str, fingerprints: list[str]):
        """Вызывать только после подтверждённого удаления из НЕАКТИВНОГО донора."""
        with self.storage._transaction() as db:
            for fingerprint in fingerprints:
                already = db.execute("SELECT 1 FROM accounts a JOIN allocations r ON a.id=r.account_id WHERE a.fingerprint=? AND r.platform='funpay' AND r.lot_id=? AND r.state IN ('staged','listed','sold')", (fingerprint, recipient)).fetchone()
                if already:
                    continue
                row = db.execute("SELECT a.id FROM accounts a JOIN allocations r ON a.id=r.account_id WHERE a.fingerprint=? AND a.status='queued' AND r.platform='funpay' AND r.lot_id=? AND r.state IN ('listed','missing')", (fingerprint, donor)).fetchone()
                if row is None:
                    raise StorageError('Не удалось подтвердить резерв переносимого аккаунта')
                db.execute("UPDATE allocations SET lot_id=?,state='staged',updated_at=? WHERE account_id=?", (recipient, _utc_now(), row[0]))

    def prepare_edit(self, lot_id, kind, before, after, active):
        with self.storage._transaction() as db:
            cursor = db.execute("INSERT INTO funpay_edits(lot_id,kind,before_json,after_json,active,created_at) VALUES (?,?,?,?,?,?)",
                                (lot_id, kind, json.dumps(before), json.dumps(after), int(active), _utc_now()))
            return cursor.lastrowid

    def mark_edit(self, edit_id: int, status: str):
        with self.storage._transaction() as db:
            db.execute('UPDATE funpay_edits SET status=? WHERE id=?', (status, edit_id))

    def pending_edits(self):
        with self.storage._lock:
            return [dict(row) for row in self.storage._connection.execute("SELECT * FROM funpay_edits WHERE status!='done' ORDER BY id")]

    def confirm_sale(self, order_id, credential):
        with self.storage._transaction() as db:
            row = db.execute("SELECT a.id FROM accounts a JOIN allocations r ON r.account_id=a.id WHERE a.fingerprint=? AND r.platform='funpay'", (credential.fingerprint,)).fetchone()
            if row:
                db.execute('INSERT OR IGNORE INTO funpay_sales VALUES (?,?,?)', (row[0], order_id, _utc_now()))
                db.execute("UPDATE accounts SET status='sold',updated_at=? WHERE id=?", (_utc_now(), row[0]))
                db.execute("UPDATE allocations SET state='sold',updated_at=? WHERE account_id=?", (_utc_now(), row[0]))

    def confirmed(self, fingerprints):
        with self.storage._lock:
            return all(self.storage._connection.execute(
                'SELECT 1 FROM accounts a JOIN funpay_sales s ON a.id=s.account_id WHERE a.fingerprint=?',
                (fingerprint,),
            ).fetchone() for fingerprint in fingerprints)

    def order_seen(self, order_id):
        with self.storage._lock:
            row = self.storage._connection.execute('SELECT terminal FROM funpay_orders WHERE order_id=?', (order_id,)).fetchone()
            return None if row is None else bool(row[0])

    def mark_order(self, order_id, terminal):
        with self.storage._transaction() as db:
            db.execute('INSERT INTO funpay_orders VALUES (?,?) ON CONFLICT(order_id) DO UPDATE SET terminal=excluded.terminal', (order_id, int(terminal)))

    def stats(self):
        with self.storage._lock:
            db = self.storage._connection
            result = {}
            for name, sql in {
                'free': "SELECT COUNT(*) FROM accounts a LEFT JOIN allocations r ON a.id=r.account_id WHERE a.status='queued' AND r.account_id IS NULL",
                'playerok_reserve': "SELECT COUNT(*) FROM accounts a JOIN allocations r ON a.id=r.account_id WHERE a.status='queued' AND r.platform='playerok'",
                'funpay_listed': "SELECT COUNT(*) FROM allocations WHERE platform='funpay' AND state='listed'",
                'funpay_staged': "SELECT COUNT(*) FROM allocations WHERE platform='funpay' AND state='staged'",
                'funpay_unknown': "SELECT COUNT(*) FROM allocations WHERE platform='funpay' AND state='missing'",
                'funpay_sold': 'SELECT COUNT(*) FROM funpay_sales',
                'funpay_errors': "SELECT COUNT(*) FROM funpay_edits WHERE status!='done'",
            }.items():
                result[name] = db.execute(sql).fetchone()[0]
            result['lots'] = [dict(row) for row in db.execute('SELECT * FROM funpay_lots ORDER BY lot_id')]
            return result

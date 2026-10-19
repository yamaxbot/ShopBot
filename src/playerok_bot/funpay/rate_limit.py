"""Shared request pacing and durable HTTP 429 cooldown for this bot host."""
import math
import sqlite3
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from ..errors import BotError


class FunPayError(BotError):
    pass


class FunPayRateLimited(FunPayError):
    def __init__(self, until):
        self.until = until
        seconds = max(1, math.ceil(until - time.time()))
        super().__init__(f'FunPay ограничил частоту запросов (HTTP 429). Пауза ещё {seconds} с; бот продолжит автоматически.')


def retry_seconds(value, now):
    try:
        value = str(value).strip()
        if value.isascii() and value.isdigit():
            return int(value)
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0, math.ceil(stamp.timestamp() - now))
    except (ValueError, TypeError, OverflowError):
        return 0


class RequestLimit:
    def __init__(self, path=None, interval=3):
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path else ':memory:', timeout=30)
        self.interval = interval
        self.db.execute('CREATE TABLE IF NOT EXISTS request_limit (id INTEGER PRIMARY KEY, next_at REAL, until REAL, strikes INTEGER, last_429 REAL)')
        self.db.execute('INSERT OR IGNORE INTO request_limit VALUES (1,0,0,0,0)')
        self.db.commit()

    def close(self):
        self.db.close()

    def check(self):
        until = self.db.execute('SELECT until FROM request_limit WHERE id=1').fetchone()[0]
        if until > time.time():
            raise FunPayRateLimited(until)

    def acquire(self):
        while True:
            now = time.time()
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                next_at, until = self.db.execute('SELECT next_at,until FROM request_limit WHERE id=1').fetchone()
                if until > now:
                    raise FunPayRateLimited(until)
                if next_at <= now:
                    self.db.execute('UPDATE request_limit SET next_at=? WHERE id=1', (now+self.interval,))
                    return
            time.sleep(min(1, next_at-now))

    def limited(self, retry_after):
        now = time.time()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            previous_until, strikes, last = self.db.execute('SELECT until,strikes,last_429 FROM request_limit WHERE id=1').fetchone()
            strikes = min(strikes+1,5) if now-last < 7200 else 1
            delay = max(60 * 2**strikes, retry_seconds(retry_after, now))
            until = max(previous_until, now+delay)
            self.db.execute('UPDATE request_limit SET until=?,strikes=?,last_429=? WHERE id=1', (until,strikes,now))
        return FunPayRateLimited(until)

import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from playerok_bot.errors import ConfigurationError
from playerok_bot.finance import Finance, money
from playerok_bot.inventory import Inventory
from playerok_bot.models import Credential
from playerok_bot.storage import Storage
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.workers import Workers
from playerok_bot.telegram.reports import report_page


def test_groups_keep_old_database_and_isolate_stock_and_lots(tmp_path):
    profiles = Profiles(tmp_path)
    try:
        profiles.connect(101, 'seller', 'secret', 'agent')
        profiles.update(101, items=['first-lot'])
        storage = profiles.storage(101)
        first_path = storage.path
        first = Credential('first@example.test', 'pass')
        storage.import_accounts([first])
        storage.close()
        group = profiles.create_group(101, 'Other game')
        assert group.group_id == 2
        assert group.playerok_id == 'seller' and group.config['items'] == []
        other = profiles.storage(101)
        try:
            assert other.path != first_path
            assert other.counts()['queued'] == 0
            with pytest.raises(ConfigurationError):
                other.import_accounts([first])
            with pytest.raises(ConfigurationError):
                Inventory(other).snapshot('222', [first], True)
            assert other.counts()['queued'] == 0
            second = Credential('second@example.test', 'pass')
            other.import_accounts([second])
            Inventory(other).configure(False)
            assert Inventory(other).reserve('222', 5)[0].credential == second
        finally:
            other.close()
        with pytest.raises(ConfigurationError):
            profiles.update(101, items=['first-lot'])
        profiles.update(101, funpay_items=['222'])
        profiles.select_group(101, 1)
        assert profiles.get(101).config['items'] == ['first-lot']
        old = profiles.storage(101)
        assert old.path == first_path
        assert old.get_account(1).credential == first
        old.close()
        # Different sellers do not share account or listing ownership.
        user2 = profiles.storage(202)
        user2.import_accounts([first])
        user2.close()
        with pytest.raises(ConfigurationError):
            profiles.select_group(202, 2)
    finally:
        profiles.close()


def test_switching_group_does_not_stop_other_worker_and_restart_restores_both(tmp_path, monkeypatch):
    async def scenario():
        profiles = Profiles(tmp_path)
        workers = Workers(profiles, 3)
        workers.check = AsyncMock(return_value={'id': 'seller'})
        async def spawn(*args, **kwargs):
            return SimpleNamespace(returncode=None, pid=123)
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        workers._terminate = AsyncMock()
        try:
            profiles.connect(101, 'seller', 'secret', 'agent')
            profiles.update(101, items=['one'])
            await workers.start(101)
            profiles.create_group(101, 'Other')
            profiles.update(101, items=['two'])
            await workers.start(101)
            assert workers.running(101, 1) and workers.running(101, 2)
            await workers.shutdown()
            await workers.restore()
            assert workers.running(101, 1) and workers.running(101, 2)
            await workers.stop(101)
            assert workers.running(101, 1) and not workers.running(101, 2)
            await workers.stop_all(101)
            assert not workers.running(101, 1)
        finally:
            profiles.close()
    asyncio.run(scenario())


def test_financial_snapshots_daily_boundary_refunds_and_multiple_accounts(tmp_path, monkeypatch):
    storage = Storage(tmp_path / 'db.sqlite3')
    finance = Finance(storage)
    monkeypatch.setenv('SHOP_PLAYEROK_FEE', '0')
    monkeypatch.setenv('SHOP_PLAYEROK_WITHDRAWAL', '3')
    monkeypatch.setenv('SHOP_FUNPAY_FEE', '0')
    monkeypatch.setenv('SHOP_FUNPAY_WITHDRAWAL', '6')
    try:
        finance.record('playerok', 'a', gross='100.00', occurred_at='2026-10-16T20:59:59Z')
        finance.record('funpay', 'b', 5, gross='200.00', currency='RUB', occurred_at='2026-10-16T21:00:00Z', status='closed')
        finance.record('funpay', 'b', 5, gross='200.00', currency='RUB', occurred_at='2026-10-16T21:00:00Z', status='closed')
        assert finance.report(date(2026, 10, 16))['payout'] == 9700
        daily = finance.report(date(2026, 10, 17))
        assert daily['accounts'] == 5 and daily['orders'] == 1
        assert daily['payout'] == 18800
        monkeypatch.setenv('SHOP_FUNPAY_WITHDRAWAL', '20')
        finance.record('funpay', 'b', 5, gross=999, status='closed')
        assert finance.report()['payout'] == 28500
        finance.record('funpay', 'b', 5, status='refunded')
        assert finance.report()['payout'] == 9700
        assert finance.report()['refunds'] == 1
        finance.record('playerok', 'a', status='partially_refunded')
        assert finance.report()['unknown'] == 1
        assert finance.report()['gross'] == 0
        finance.record('playerok', 'c', gross=100)
        finance.record('playerok', 'd', gross=200)
        report = finance.report()
        assert report['priced_orders'] == 2
        assert report['gross'] // report['priced_orders'] == 15000
    finally:
        storage.close()


def test_unknown_money_currency_and_reopening_do_not_invent_revenue(tmp_path):
    path = tmp_path / 'db.sqlite3'
    storage = Storage(path)
    Finance(storage).record('playerok', 'missing')
    Finance(storage).record('funpay', 'usd', gross=20, currency='USD')
    storage.close()
    storage = Storage(path)
    try:
        report = Finance(storage).report()
        assert report['orders'] == 2
        assert report['unknown'] == 2
        assert report['gross'] == 0
        assert report['observed'] == 2
        for bad in ('NaN', 'Infinity', '-1', 'bad'):
            assert money(bad) is None
    finally:
        storage.close()


def test_reports_sum_only_owners_groups_and_buttons_pin_group(tmp_path):
    profiles = Profiles(tmp_path)
    try:
        for uid in (101, 202):
            storage = profiles.storage(uid)
            Finance(storage).record('playerok', str(uid), gross=uid, occurred_at='2026-10-16T15:00:00Z')
            storage.close()
        group = profiles.create_group(101, 'Another')
        storage = profiles.storage(101)
        Finance(storage).record('playerok', 'another', gross=50, occurred_at='2026-10-16T15:00:00Z')
        storage.close()
        text, markup = report_page(profiles, 101, 0, date(2026, 10, 16))
        assert '151.00' in text and '353.00' not in text
        _, markup = report_page(profiles, 101, group.group_id, date(2026, 10, 16))
        assert all(b.callback_data.startswith(f'report:101:{group.group_id}:') for row in markup.inline_keyboard for b in row)
    finally:
        profiles.close()

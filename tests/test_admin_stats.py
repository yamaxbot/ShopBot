import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage
from aiogram.types import Message, Update

from playerok_bot.finance import MSK
from playerok_bot.models import Credential
from playerok_bot.telegram.admin_stats import admin_stats_text, collect
from playerok_bot.telegram.billing import Billing
from playerok_bot.telegram.bot import create_router
from playerok_bot.telegram.config import TelegramSettings
from playerok_bot.telegram.help_text import help_text
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.texts import commands_text
from playerok_bot.telegram.workers import Workers

DAY = 86400
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=MSK).timestamp()


def _shop(tmp_path):
    """Пользователи, оплаты и склад с заранее известными датами."""
    profiles = Profiles(tmp_path)
    billing = Billing(profiles, frozenset({1}))
    created = {
        1: NOW - 200 * DAY,                                               # администратор
        10: NOW - 1 * 3600,                                               # сегодня 14:00 МСК
        15: datetime(2026, 10, 26, 23, 30, tzinfo=MSK).timestamp(),       # вчера, но в пределах 7 дней
        11: NOW - 3 * DAY,
        12: NOW - 20 * DAY,
        13: NOW - 100 * DAY,
        14: None,                                                         # пришёл до учёта дат
    }
    for user_id, stamp in created.items():
        profiles.get(user_id)
        with profiles.connection:
            profiles.connection.execute('UPDATE profiles SET created_at=? WHERE user_id=?',
                                        (None if stamp is None else int(stamp), user_id))
    profiles.connect(10, 'playerok-10', 'token=a', 'agent')
    profiles.connect(11, 'playerok-11', 'token=b', 'agent')
    profiles.connect_funpay(11, 'funpay-11', 'golden-key-11', 'agent')
    profiles.connect_funpay(12, 'funpay-12', 'golden-key-12', 'agent')
    profiles.create_group(10, 'Second game')

    def pay(charge, user_id, ago, status='paid'):
        with profiles.connection:
            profiles.connection.execute(
                'INSERT INTO star_payments(charge_id,payload,user_id,stars,days,paid_at,status) VALUES (?,?,?,?,?,?,?)',
                (charge, 'shop:' + charge, user_id, 100, 30, int(NOW - ago), status))
    pay('a', 11, 25 * DAY)            # первая покупка 25 дней назад
    pay('b', 11, 2 * 3600)            # продление сегодня → активна ещё ~35 дней
    pay('c', 12, 29 * DAY)            # истекает через сутки
    pay('d', 13, 90 * DAY)            # истекла
    pay('e', 10, 3600, 'refunded')    # первая покупка сегодня, возвращена
    for number, ago in enumerate((600, 7200, 10 * DAY)):
        with profiles.connection:
            profiles.connection.execute('INSERT INTO star_invoices VALUES (?,?,?,?,?,0)',
                                        (f'invoice-{number}', 10, 100, 30, int(NOW - ago)))

    storage = profiles.storage(10, 1)
    try:
        storage.import_accounts([Credential('a@example.test', 'one'), Credential('b@example.test', 'two')])
        with storage._transaction() as db:
            db.execute("UPDATE accounts SET status='sold' WHERE id=1")
            db.execute("INSERT INTO funpay_sales VALUES (1, 'order-1', '2026-10-27T10:00:00+00:00')")
    finally:
        storage.close()
    return profiles, billing


def test_admin_stats_counts_users_subscriptions_payments_and_sales(tmp_path):
    profiles, billing = _shop(tmp_path)
    try:
        stats = collect(profiles, billing, running={(10, 1), (10, 2), (11, 1)}, now=NOW)
        assert stats['users'] == 7
        assert stats['new_users'] == [1, 3, 4, 7]
        assert stats['unknown_date'] == 1
        assert (stats['playerok'], stats['funpay'], stats['any_platform'], stats['admins']) == (2, 2, 3, 1)
        assert (stats['groups'], stats['running_groups'], stats['running_sellers']) == (8, 3, 2)
        assert (stats['active'], stats['expiring'], stats['lapsed']) == (2, 1, 1)
        # Пользователь 10 оплатил и получил возврат — он не покупатель.
        assert stats['payers'] == 3
        assert stats['payments'] == [1, 1, 3, 4]
        assert stats['stars'] == [100, 100, 300, 400]
        assert stats['new_payers'] == [0, 0, 2, 3]
        assert stats['renewals'] == [1, 1, 1, 1]
        assert [a + b for a, b in zip(stats['new_payers'], stats['renewals'])] == stats['payments']
        assert stats['invoices'] == [2, 2, 3, 3]
        assert (stats['refunds'], stats['refunded_stars']) == (1, 100)
        assert (stats['sold'], stats['in_stock'], stats['unread_groups']) == (1, 1, 0)

        text = admin_stats_text(profiles, billing, now=NOW)
        assert 'Новые: сегодня 1 · 7 дней 3 · 30 дней 4 · всего 7' in text
        assert 'Без даты регистрации: 1' in text
        assert 'Оплатили хотя бы раз: 3 из 7 (42.9%)' in text
        assert 'Stars: сегодня 100 · 7 дней 100 · 30 дней 300 · всего 400' in text
        assert '27.10.2026 15:00 МСК' in text
        assert len(text) < 4096
        # Пароли и логины аккаунтов в сводку не попадают.
        assert 'example.test' not in text and 'one' not in text.split()
    finally:
        profiles.close()


def test_new_user_gets_registration_time_and_old_database_is_migrated(tmp_path, monkeypatch):
    monkeypatch.setattr('playerok_bot.telegram.profiles.time.time', lambda: 1_800_000_000)
    profiles = Profiles(tmp_path)
    profiles.get(5)
    assert profiles.connection.execute('SELECT created_at FROM profiles WHERE user_id=5').fetchone()[0] == 1_800_000_000
    # База предыдущей версии: колонки created_at нет.
    profiles.connection.execute('ALTER TABLE profiles DROP COLUMN created_at')
    profiles.connection.commit()
    profiles.close()
    profiles = Profiles(tmp_path)
    try:
        assert profiles.connection.execute('SELECT created_at FROM profiles WHERE user_id=5').fetchone()[0] is None
        assert profiles.get(5).user_id == 5
        profiles.get(6)
        assert profiles.connection.execute('SELECT created_at FROM profiles WHERE user_id=6').fetchone()[0] == 1_800_000_000
    finally:
        profiles.close()


def test_admin_stats_command_is_for_admins_only(tmp_path):
    class Session(BaseSession):
        def __init__(self):
            super().__init__()
            self.texts = []

        async def close(self):
            pass

        async def stream_content(self, *args, **kwargs):
            yield b''

        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, SendMessage):
                self.texts.append((method.chat_id, method.text))
                return Message.model_validate({'message_id': 1, 'date': 1, 'text': method.text,
                                               'chat': {'id': method.chat_id, 'type': 'private'}})
            raise AssertionError(type(method))

    async def scenario():
        profiles = Profiles(tmp_path)
        billing = Billing(profiles, frozenset({1}))
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(profiles, Workers(profiles, 20, billing),
                                                TelegramSettings('test', tmp_path, admin_ids=frozenset({1})), billing))
        session = Session()
        bot = Bot('123456:abcdefghijklmnopqrstuvwxyzABCDEFG', session=session)

        async def send(uid, text):
            await dispatcher.feed_update(bot, Update.model_validate({'update_id': 1, 'message': {
                'message_id': 1, 'date': 1, 'text': text, 'chat': {'id': uid, 'type': 'private'},
                'from': {'id': uid, 'is_bot': False, 'first_name': 'User'}}}))
            return session.texts[-1]

        try:
            await send(1, '/start')
            await send(2, '/start')
            assert await send(2, '/admin_stats') == (2, 'Команда недоступна.')
            chat, text = await send(1, '/admin_stats')
            assert chat == 1 and 'Статистика бота' in text and 'Всего: 2' in text
            assert '/admin_stats' in (await send(1, '/admin'))[1]
        finally:
            await bot.session.close()
            profiles.close()

    asyncio.run(scenario())


def test_admin_stats_is_documented_only_for_admins():
    assert '/admin_stats' in commands_text(is_admin=True)
    assert '/admin_stats' in help_text('admin', is_admin=True)
    assert '/admin_stats' not in commands_text()
    assert '/admin_stats' not in help_text('admin')

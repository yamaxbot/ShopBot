import asyncio
import time

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import RefundStarPayment, SendMessage
from aiogram.types import Message, Update

from playerok_bot.errors import ConfigurationError
from playerok_bot.telegram.billing import Billing
from playerok_bot.telegram.bot import create_router
from playerok_bot.telegram.config import TelegramSettings
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.workers import Workers


class Session(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.refund_result = True

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b''

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, RefundStarPayment):
            if isinstance(self.refund_result, BaseException):
                raise self.refund_result
            if self.refund_result == 'bad-request':
                raise TelegramBadRequest(method=method, message='Bad Request: CHARGE_NOT_FOUND')
            return self.refund_result
        if isinstance(method, SendMessage):
            return Message.model_validate({'message_id': 1, 'date': 1, 'text': method.text,
                                           'chat': {'id': method.chat_id, 'type': 'private'}})
        raise AssertionError(type(method))


class Harness:
    def __init__(self, tmp_path):
        self.profiles = Profiles(tmp_path)
        self.billing = Billing(self.profiles, frozenset({1}))
        self.workers = Workers(self.profiles, 20, self.billing)
        self.calls = []

        async def connect(uid, cookies, agent):
            self.profiles.connect(uid, f'playerok-{uid}', cookies, agent)
            return f'seller{uid}'

        async def check_playerok(uid):
            self.calls.append(('check_playerok', uid))
            return {'id': f'playerok-{uid}', 'username': f'seller{uid}'}

        async def stop_all(uid):
            self.calls.append(('stop_all', uid))
        self.workers.connect, self.workers.check_playerok, self.workers.stop_all = connect, check_playerok, stop_all
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_router(self.profiles, self.workers,
                                                     TelegramSettings('test', tmp_path, admin_ids=frozenset({1})), self.billing))
        self.session = Session()
        self.bot = Bot('123456:abcdefghijklmnopqrstuvwxyzABCDEFG', session=self.session)
        self.counter = 0

    async def send(self, uid, text):
        self.counter += 1
        await self.dispatcher.feed_update(self.bot, Update.model_validate({'update_id': self.counter, 'message': {
            'message_id': self.counter, 'date': 1, 'text': text, 'chat': {'id': uid, 'type': 'private'},
            'from': {'id': uid, 'is_bot': False, 'first_name': 'User'}}}))
        return self.session.calls[-1].text

    def pay(self, charge, user_id, stars=35, days=7, ago=0, status='paid'):
        with self.profiles.connection:
            cursor = self.profiles.connection.execute(
                'INSERT INTO star_payments(charge_id,payload,user_id,stars,days,paid_at,status) VALUES (?,?,?,?,?,?,?)',
                (charge, 'shop:' + charge, user_id, stars, days, int(time.time() - ago), status))
        return cursor.lastrowid

    async def close(self):
        await self.bot.session.close()
        self.profiles.close()


def run(tmp_path, scenario):
    async def main():
        harness = Harness(tmp_path)
        try:
            await scenario(harness)
        finally:
            await harness.close()
    asyncio.run(main())


def test_playerok_commands_use_pl_prefix_and_old_names_still_work(tmp_path):
    async def scenario(h):
        h.billing.set_plan(1, 1, 1)  # не влияет: администратор пользуется ботом без подписки
        assert 'Подключение Playerok' in await h.send(1, '/pl_connect')
        assert 'Подключён ваш профиль @seller1' in await h.send(1, 'token-value')
        assert '/pl_items' in h.session.calls[-1].text
        assert 'Playerok: отправьте ссылки' in await h.send(1, '/pl_items')
        assert 'Playerok-лоты сохранены' in await h.send(1, 'https://playerok.com/products/lot-a')
        assert h.profiles.get(1).config['items'] == ['https://playerok.com/products/lot-a']
        assert 'Лимит Playerok' in await h.send(1, '/pl_limit')
        assert 'Лимит сохранён' in await h.send(1, '30')
        assert h.profiles.get(1).config['price_limit'] == 30
        assert '/pl_bump on' in await h.send(1, '/pl_bump')
        await h.send(1, '/pl_bump off')
        assert h.profiles.get(1).config['bump'] is False
        status = await h.send(1, '/pl_bump_status')
        assert 'Поднятия Playerok' in status and '/pl_limit' in status and '/pl_check' in status
        assert 'Playerok проверен: @seller1' in await h.send(1, '/pl_check')
        assert h.calls == [('check_playerok', 1)]
        settings = await h.send(1, '/settings')
        assert '/pl_connect · /pl_items · /pl_check' in settings and '/fp_connect · /fp_items · /fp_check' in settings
        assert 'Playerok-лоты убраны' in await h.send(1, '/pl_items clear')
        # Старые имена — скрытые синонимы.
        assert 'Подключение Playerok' in await h.send(1, '/connect')
        await h.send(1, '/cancel')
        assert 'Playerok: отправьте ссылки' in await h.send(1, '/items')
        await h.send(1, '/cancel')
        assert 'Лимит Playerok' in await h.send(1, '/limit')
        await h.send(1, '/cancel')
        await h.send(1, '/bump on')
        assert h.profiles.get(1).config['bump'] is True
        assert 'Поднятия Playerok' in await h.send(1, '/bump_status')
    run(tmp_path, scenario)


def test_playerok_only_check_requires_connection(tmp_path):
    async def scenario():
        profiles = Profiles(tmp_path)
        workers = Workers(profiles, 20)
        probes = []

        async def probe(profile, mode='check'):
            probes.append(mode)
            return {'id': 'playerok-5', 'username': 'seller'}
        workers.probe = probe
        try:
            with pytest.raises(ConfigurationError, match='/pl_connect'):
                await workers.check_playerok(5)
            profiles.connect(5, 'playerok-5', 'token=a', 'agent')
            with pytest.raises(ConfigurationError, match='/pl_items'):
                await workers.check_playerok(5)
            profiles.update(5, items=['https://playerok.com/products/x'])
            assert (await workers.check_playerok(5))['username'] == 'seller'
            assert (await workers.check(5))['username'] == 'seller'
            assert probes == ['check', 'check']
            with pytest.raises(ConfigurationError, match='/pl_connect и /pl_items, FunPay — /fp_connect'):
                await workers.check(6)
        finally:
            profiles.close()
    asyncio.run(scenario())


def test_admin_refunds_stars_by_operation_id(tmp_path):
    async def scenario(h):
        h.pay('stx-old', 2, stars=20, days=3, ago=10 * 86400)   # давно истёкший платёж
        payment_id = h.pay('stx-charge-1', 2)                    # действующие 7 дней
        assert h.billing.active(2)

        # Покупатель видит ID операции в /payments, администратор — в /admin_payments.
        assert 'ID операции: stx-charge-1' in await h.send(2, '/payments')
        listing = await h.send(1, '/admin_payments 2')
        assert 'ID операции: stx-charge-1' in listing and '/refund' in listing

        assert await h.send(2, '/refund stx-charge-1 confirm') == 'Команда недоступна.'
        assert 'Формат' in await h.send(1, '/refund')
        assert 'Формат' in await h.send(1, '/refund stx-charge-1 now')
        assert 'не найден' in await h.send(1, '/refund stx-missing')

        preview = await h.send(1, '/refund stx-charge-1')
        assert 'Платёж №' in preview and 'пользователь 2' in preview and '35 Stars · 7 дней' in preview
        assert 'Доступ пользователя сейчас: до' in preview and 'После возврата: не активен' in preview
        assert 'Подтвердить: /refund stx-charge-1 confirm' in preview
        assert not any(isinstance(call, RefundStarPayment) for call in h.session.calls)

        # Telegram отклонил возврат: платёж ждёт повтора, Stars не возвращены.
        h.session.refund_result = 'bad-request'
        assert 'Telegram не подтвердил возврат' in await h.send(1, '/refund stx-charge-1 confirm')
        assert h.billing.payment(payment_id)['status'] == 'refund_pending'
        # Ответ потерялся: тот же статус, повтор безопасен.
        h.session.refund_result = TimeoutError()
        assert 'Telegram не ответил' in await h.send(1, '/refund stx-charge-1 confirm')
        assert h.billing.payment(payment_id)['status'] == 'refund_pending'

        h.session.refund_result = True
        done = await h.send(1, '/refund stx-charge-1 confirm')
        assert 'Возврат выполнен: 35 Stars пользователю 2' in done and 'не активен' in done
        refunds = [call for call in h.session.calls if isinstance(call, RefundStarPayment)]
        assert len(refunds) == 3
        assert (refunds[-1].user_id, refunds[-1].telegram_payment_charge_id) == (2, 'stx-charge-1')
        assert h.billing.payment(payment_id)['status'] == 'refunded'
        assert not h.billing.active(2)
        assert ('stop_all', 2) in h.calls

        assert 'уже возвращён' in await h.send(1, '/refund stx-charge-1 confirm')
        assert len([call for call in h.session.calls if isinstance(call, RefundStarPayment)]) == 3
        audit = [row[0] for row in h.profiles.connection.execute('SELECT action FROM billing_audit ORDER BY id')]
        assert audit.count('refunded') == 1
    run(tmp_path, scenario)


def test_refund_keeps_access_from_other_payments(tmp_path):
    async def scenario(h):
        h.pay('stx-a', 3, stars=70, days=15, ago=86400)
        h.pay('stx-b', 3, stars=35, days=7)
        preview = await h.send(1, '/refund stx-b')
        assert 'После возврата: до' in preview
        assert 'Возврат выполнен' in await h.send(1, '/refund stx-b confirm')
        assert h.billing.active(3) and ('stop_all', 3) not in h.calls
    run(tmp_path, scenario)

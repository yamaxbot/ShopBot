import asyncio

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendInvoice, SendMessage
from aiogram.types import Message, Update

from playerok_bot.errors import ConfigurationError
from playerok_bot.telegram.admin_stats import admin_stats_text
from playerok_bot.telegram.billing import Billing, days_text
from playerok_bot.telegram.bot import create_router
from playerok_bot.telegram.config import TelegramSettings
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.workers import Workers

DEFAULTS = [{'days': 3, 'stars': 20}, {'days': 7, 'stars': 35}, {'days': 15, 'stars': 70}]


@pytest.mark.parametrize('days, text', [
    (1, '1 день'), (2, '2 дня'), (3, '3 дня'), (5, '5 дней'), (7, '7 дней'), (11, '11 дней'),
    (14, '14 дней'), (15, '15 дней'), (21, '21 день'), (22, '22 дня'), (30, '30 дней'), (111, '111 дней'),
])
def test_days_are_declined(days, text):
    assert days_text(days) == text


def test_default_plans_and_admin_changes(tmp_path):
    profiles = Profiles(tmp_path)
    billing = Billing(profiles, frozenset({1}))
    assert billing.plans() == DEFAULTS
    assert billing.plans_text() == '• 3 дня — 20 ⭐\n• 7 дней — 35 ⭐\n• 15 дней — 70 ⭐'
    with pytest.raises(ConfigurationError):
        billing.set_plan(2, 10, 3)
    with pytest.raises(ConfigurationError):
        billing.remove_plan(2, 3)
    for stars, days in ((0, 3), (10001, 3), (10, 0), (10, 366)):
        with pytest.raises(ConfigurationError):
            billing.set_plan(1, stars, days)
    billing.set_plan(1, 40, 3)
    billing.set_plan(1, 100, 30)
    billing.remove_plan(1, 15)
    assert billing.plans() == [{'days': 3, 'stars': 40}, {'days': 7, 'stars': 35}, {'days': 30, 'stars': 100}]
    with pytest.raises(ConfigurationError, match='нет'):
        billing.remove_plan(1, 15)
    with pytest.raises(ConfigurationError, match='Такого тарифа нет'):
        billing.invoice(2, 15)
    profiles.close()
    # После перезапуска удалённый тариф по умолчанию не возвращается.
    profiles = Profiles(tmp_path)
    billing = Billing(profiles, frozenset({1}))
    assert [plan['days'] for plan in billing.plans()] == [3, 7, 30]
    billing.remove_plan(1, 3)
    billing.remove_plan(1, 7)
    with pytest.raises(ConfigurationError, match='хотя бы один'):
        billing.remove_plan(1, 30)
    for days in range(31, 38):
        billing.set_plan(1, 50, days)
    with pytest.raises(ConfigurationError, match='не больше 8'):
        billing.set_plan(1, 50, 60)
    billing.set_plan(1, 55, 31)  # изменить цену существующего можно и при 8 тарифах
    profiles.close()


def test_payment_stats_show_plans_and_purchases_by_term(tmp_path):
    profiles = Profiles(tmp_path)
    billing = Billing(profiles, frozenset({1}))
    for charge, days, stars in (('a', 3, 20), ('b', 7, 35), ('c', 7, 35)):
        with profiles.connection:
            profiles.connection.execute(
                "INSERT INTO star_payments(charge_id,payload,user_id,stars,days,paid_at) VALUES (?,?,?,?,?,?)",
                (charge, 'shop:' + charge, 2, stars, days, 1))
    text = admin_stats_text(profiles, billing, now=1_800_000_000)
    assert 'По срокам за всё время: 3 дня — 1 (20 ⭐) · 7 дней — 2 (70 ⭐)' in text
    assert 'Тарифы сейчас: 3 дня — 20 ⭐ · 7 дней — 35 ⭐ · 15 дней — 70 ⭐' in text
    profiles.close()


class Session(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b''

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, SendInvoice)):
            return Message.model_validate({'message_id': 1, 'date': 1, 'text': getattr(method, 'text', 'invoice'),
                                           'chat': {'id': method.chat_id, 'type': 'private'}})
        raise AssertionError(type(method))


def test_users_buy_a_plan_with_buttons_and_admins_edit_plans(tmp_path):
    async def scenario():
        profiles = Profiles(tmp_path)
        billing = Billing(profiles, frozenset({1}))
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(profiles, Workers(profiles, 20, billing),
                                                TelegramSettings('test', tmp_path, admin_ids=frozenset({1})), billing))
        session = Session()
        bot = Bot('123456:abcdefghijklmnopqrstuvwxyzABCDEFG', session=session)
        counter = 0

        async def send(uid, text):
            nonlocal counter
            counter += 1
            await dispatcher.feed_update(bot, Update.model_validate({'update_id': counter, 'message': {
                'message_id': counter, 'date': 1, 'text': text, 'chat': {'id': uid, 'type': 'private'},
                'from': {'id': uid, 'is_bot': False, 'first_name': 'User'}}}))
            return session.calls[-1]

        async def press(uid, data, chat_type='private'):
            nonlocal counter
            counter += 1
            await dispatcher.feed_update(bot, Update.model_validate({'update_id': counter, 'callback_query': {
                'id': str(counter), 'chat_instance': 'chat', 'data': data,
                'from': {'id': uid, 'is_bot': False, 'first_name': 'User'},
                'message': {'message_id': 1, 'date': 1, 'text': 'Условия',
                            'chat': {'id': uid if chat_type == 'private' else -5, 'type': chat_type}}}}))
            return session.calls[-1]

        try:
            terms = await send(2, '/subscribe')
            assert '7 дней — 35 ⭐' in terms.text and '/subscribe agree 3' in terms.text
            buttons = [row[0] for row in terms.reply_markup.inline_keyboard]
            assert [(b.text, b.callback_data) for b in buttons] == [
                ('3 дня — 20 ⭐', 'subscribe:2:3'), ('7 дней — 35 ⭐', 'subscribe:2:7'), ('15 дней — 70 ⭐', 'subscribe:2:15')]
            assert 'Тарифы:\n• 3 дня — 20 ⭐' in (await send(2, '/subscription')).text

            invoice = await press(2, 'subscribe:2:7')
            assert isinstance(invoice, SendInvoice) and invoice.chat_id == 2
            assert invoice.prices[0].amount == 35 and invoice.currency == 'XTR'
            assert invoice.title == 'Подписка ShopBot · 7 дней' and len(invoice.title) <= 32
            stored = profiles.connection.execute('SELECT stars, days FROM star_invoices WHERE payload=?', (invoice.payload,)).fetchone()
            assert tuple(stored) == (35, 7)

            # Чужая или устаревшая кнопка и кнопка не в личном чате счёт не выставляют.
            for uid, data, chat in ((3, 'subscribe:2:7', 'private'), (2, 'subscribe:2:99', 'private'),
                                    (2, 'subscribe:2:x', 'private'), (2, 'subscribe:2:7', 'group')):
                result = await press(uid, data, chat)
                assert not isinstance(result, SendInvoice)
            assert 'Такого тарифа нет' in session.calls[-3].text

            invoice = await send(2, '/subscribe agree 15')
            assert isinstance(invoice, SendInvoice) and invoice.prices[0].amount == 70
            assert '15 дней' in invoice.description
            assert isinstance(await send(2, '/subscribe agree'), SendMessage)  # несколько тарифов: нужен выбор

            assert (await send(2, '/set_price 1 3')).text == 'Команда недоступна.'
            assert (await send(2, '/del_price 3')).text == 'Команда недоступна.'
            reply = await send(1, '/set_price 40 3')
            assert 'Тариф сохранён: 3 дня — 40 Stars.' in reply.text and '• 3 дня — 40 ⭐' in reply.text
            assert 'убран' in (await send(1, '/del_price 15')).text
            assert 'Формат' in (await send(1, '/set_price 40')).text
            assert billing.plans() == [{'days': 3, 'stars': 40}, {'days': 7, 'stars': 35}]
            assert '• 7 дней — 35 ⭐' in (await send(1, '/admin')).text
            assert (await press(2, 'subscribe:2:3')).prices[0].amount == 40
            # Новая цена не меняет уже выставленный счёт на 7 дней.
            assert tuple(profiles.connection.execute(
                'SELECT stars FROM star_invoices WHERE days=7').fetchone()) == (35,)
        finally:
            await bot.session.close()
            profiles.close()

    asyncio.run(scenario())

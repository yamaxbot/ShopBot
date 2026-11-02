import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import StarTransactions
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import RefundStarPayment

from playerok_bot.errors import ConfigurationError
from playerok_bot.telegram.billing import Billing
from playerok_bot.telegram.profiles import Profiles


def transactions(**changes):
    row = {'id': 'charge', 'amount': 35, 'date': 1,
           'receiver': {'type': 'user', 'transaction_type': 'invoice_payment',
                        'user': {'id': 2, 'is_bot': False, 'first_name': 'Buyer'}}}
    row.update(changes)
    return StarTransactions.model_validate({'transactions': [row]})


@pytest.fixture
def billing(tmp_path):
    profiles = Profiles(tmp_path)
    bill = Billing(profiles, frozenset({1}))
    payload, plan = bill.invoice(2, 7)
    bill.validate(2, payload, 'XTR', 35, checkout=True)
    bill.paid(2, SimpleNamespace(invoice_payload=payload, currency='XTR', total_amount=35, telegram_payment_charge_id='charge'))
    yield bill
    profiles.close()


@pytest.mark.parametrize('result', [False, None, 'True', 1])
def test_refund_rejected_response_never_claims_success(billing, result):
    bot = SimpleNamespace(refund_star_payment=AsyncMock(return_value=result), get_star_transactions=AsyncMock())
    with pytest.raises(ConfigurationError):
        asyncio.run(billing.refund(bot, 1, 1))
    assert billing.payment(1)['status'] == 'refund_pending'
    assert billing.payment(1)['refund_verified'] is None
    bot.get_star_transactions.assert_not_awaited()


@pytest.mark.parametrize('page', [
    transactions(id='another'), transactions(amount=34), transactions(nanostar_amount=1),
    transactions(receiver=None, source={'type': 'user', 'transaction_type': 'invoice_payment',
                                      'user': {'id': 2, 'is_bot': False, 'first_name': 'Buyer'}}),
    transactions(receiver={'type': 'user', 'transaction_type': 'invoice_payment',
                           'user': {'id': 3, 'is_bot': False, 'first_name': 'Other'}}),
    transactions(receiver={'type': 'user', 'transaction_type': 'invoice_payment', 'invoice_payload': 'wrong',
                           'user': {'id': 2, 'is_bot': False, 'first_name': 'Buyer'}}),
])
def test_only_matching_outgoing_transaction_confirms_refund(billing, page):
    bot = SimpleNamespace(refund_star_payment=AsyncMock(return_value=True), get_star_transactions=AsyncMock(return_value=page))
    with pytest.raises(ConfigurationError, match='журналом'):
        asyncio.run(billing.refund(bot, 1, 1))
    assert billing.payment(1)['status'] == 'refund_pending'
    bot.get_star_transactions.return_value = transactions()
    asyncio.run(billing.refund(bot, 1, 1))
    assert billing.payment(1)['refund_verified'] == 'transactions'
    assert bot.refund_star_payment.await_count == 1


def test_legacy_refunded_flag_is_not_remote_confirmation(billing):
    with billing.db:
        billing.db.execute("UPDATE star_payments SET status='refunded'")
    bot = SimpleNamespace(refund_star_payment=AsyncMock(return_value=True), get_star_transactions=AsyncMock(return_value=transactions()))
    asyncio.run(billing.refund(bot, 1, 1))
    bot.refund_star_payment.assert_awaited_once_with(user_id=2, telegram_payment_charge_id='charge')
    assert billing.payment(1)['refund_verified'] == 'transactions'


def test_lost_ledger_response_can_retry_without_another_refund(billing):
    bot = SimpleNamespace(refund_star_payment=AsyncMock(return_value=True), get_star_transactions=AsyncMock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        asyncio.run(billing.refund(bot, 1, 1))
    assert billing.payment(1)['status'] == 'refund_pending'
    bot.get_star_transactions = AsyncMock(return_value=transactions())
    asyncio.run(billing.refund(bot, 1, 1))
    assert bot.refund_star_payment.await_count == 1


def test_already_refunded_error_still_requires_ledger(billing):
    error = TelegramBadRequest(method=RefundStarPayment(user_id=2, telegram_payment_charge_id='charge'),
                               message='Bad Request: CHARGE_ALREADY_REFUNDED')
    bot = SimpleNamespace(refund_star_payment=AsyncMock(side_effect=error),
                          get_star_transactions=AsyncMock(return_value=transactions()))
    asyncio.run(billing.refund(bot, 1, 1))
    assert billing.payment(1)['status'] == 'refunded'


def test_refund_search_reads_more_than_one_page(billing):
    first = StarTransactions(transactions=[transactions(id=f'other-{i}').transactions[0] for i in range(100)])
    bot = SimpleNamespace(refund_star_payment=AsyncMock(return_value=True),
                          get_star_transactions=AsyncMock(side_effect=[first, transactions()]))
    asyncio.run(billing.refund(bot, 1, 1))
    assert [call.kwargs['offset'] for call in bot.get_star_transactions.await_args_list] == [0, 100]


def test_refunded_update_must_match_invoice(billing):
    with pytest.raises(ConfigurationError):
        billing.refunded(2, SimpleNamespace(telegram_payment_charge_id='charge', total_amount=35,
                                           currency='XTR', invoice_payload='wrong'))
    assert billing.payment(1)['status'] == 'paid'

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from playerok_bot.errors import ConfigurationError
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.billing import Billing
from playerok_bot.telegram.workers import Workers


def payment(payload, charge='charge', amount=100):
    return SimpleNamespace(invoice_payload=payload, currency='XTR', total_amount=amount, telegram_payment_charge_id=charge)


def test_billing_validation_idempotency_renewal_refund_and_restart(tmp_path, monkeypatch):
    clock = [1000000]
    monkeypatch.setattr('playerok_bot.telegram.billing.time.time', lambda: clock[0])
    profiles = Profiles(tmp_path)
    billing = Billing(profiles, frozenset({1}))
    assert billing.active(1) and not billing.active(2)
    payload, plan = billing.invoice(2)
    assert plan == {'stars': 100, 'days': 30}
    for uid, currency, amount in ((3,'XTR',100),(2,'USD',100),(2,'XTR',1)):
        with pytest.raises(ConfigurationError):
            billing.validate(uid,payload,currency,amount,True)
    with pytest.raises(ConfigurationError):
        billing.paid(2,payment(payload))
    billing.validate(2,payload,'XTR',100,True)
    first = billing.paid(2,payment(payload))
    assert billing.paid(2,payment(payload)) == first
    assert billing.until(2) == clock[0]+30*86400
    with pytest.raises(ConfigurationError):
        billing.validate(2,payload,'XTR',100,True)
    with pytest.raises(ConfigurationError):
        billing.set_plan(2,1,365)
    billing.set_plan(1,250,10)
    assert billing.until(2) == clock[0]+30*86400
    payload2,_ = billing.invoice(2)
    billing.validate(2,payload2,'XTR',250,True)
    second = billing.paid(2,payment(payload2,'second',250))
    assert billing.until(2) == clock[0]+40*86400
    profiles.close()
    profiles = Profiles(tmp_path)
    billing = Billing(profiles,frozenset({1}))
    bot = SimpleNamespace(refund_star_payment=AsyncMock(return_value=True))
    asyncio.run(billing.refund(bot,1,first))
    assert billing.until(2) == clock[0]+10*86400
    asyncio.run(billing.refund(bot,1,first))
    assert bot.refund_star_payment.await_count == 1
    billing.refunded(2,payment(payload2,'second',250))
    assert not billing.active(2)
    profiles.close()


def test_invoice_price_is_frozen_and_pending_refund_can_retry(tmp_path, monkeypatch):
    profiles = Profiles(tmp_path)
    billing = Billing(profiles,frozenset({1}))
    payload,_ = billing.invoice(2)
    billing.set_plan(1,200,20)
    billing.validate(2,payload,'XTR',100,True)
    pid = billing.paid(2,payment(payload))
    bot = SimpleNamespace(refund_star_payment=AsyncMock(side_effect=TimeoutError))
    with pytest.raises(TimeoutError):
        asyncio.run(billing.refund(bot,1,pid))
    assert billing.payment(pid)['status'] == 'refund_pending'
    assert not billing.active(2)
    bot.refund_star_payment = AsyncMock(return_value=True)
    asyncio.run(billing.refund(bot,1,pid))
    assert billing.payment(pid)['status'] == 'refunded'
    profiles.close()


def test_expired_access_cannot_start_or_restore_workers(tmp_path):
    async def scenario():
        profiles = Profiles(tmp_path)
        profiles.get(2)
        profiles.set_enabled(2, True)
        billing = Billing(profiles,frozenset({1}))
        workers = Workers(profiles,20,billing)
        workers._spawn = AsyncMock()
        with pytest.raises(ConfigurationError):
            await workers.start(2)
        await workers.restore()
        workers._spawn.assert_not_awaited()
        assert not profiles.get(2).enabled
        profiles.close()
    asyncio.run(scenario())

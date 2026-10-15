from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from playerok_bot.bump import BumpService, MOSCOW, bump_time_allowed
from playerok_bot.errors import PremiumPriceLimitError
from playerok_bot.gateway import PlayerokGateway
from playerok_bot.models import Credential, PaidDeal
from playerok_bot.service import SaleService
from playerok_bot.storage import Storage
from test_storage_and_service import FakeGateway, settings


def msk(day=2, hour=12, minute=0):
    return datetime(2030, 1, day, hour, minute, tzinfo=MOSCOW)


class BumpGateway(FakeGateway):
    def __init__(self, item_id="item-1"):
        super().__init__()
        self.item_id = item_id
        self.current = Credential(f"{item_id}@example.test", "password")
        self.active = True
        self.sold = False
        self.bump_calls = 0
        self.lose_bump_response = False

    def plan_premium_bump(self):
        if not self.active:
            return None
        if self.reject_premium:
            raise PremiumPriceLimitError("Дороже лимита")
        return "premium-id", 25

    def bump_premium(self, priority_id, *, allowed_until):
        assert priority_id == "premium-id"
        self.bump_calls += 1
        if self.lose_bump_response:
            raise ConnectionError("Потерян ответ")
        return 25


def make_service(tmp_path, start=None):
    gateway = BumpGateway()
    storage = Storage(tmp_path / "state.sqlite3")
    storage.import_accounts([gateway.current])
    storage.bootstrap_listing(gateway.item_id, gateway.current)
    if start is not None:
        storage.bump_baseline(gateway.item_id, start.timestamp())
    service = BumpService(settings(tmp_path), storage, gateway)
    return service, gateway, storage


def test_bump_waits_two_hours_and_then_starts_a_new_interval(tmp_path):
    service, gateway, storage = make_service(tmp_path, msk(hour=12))
    try:
        service.check_due(msk(hour=13, minute=59))
        assert gateway.bump_calls == 0
        service.check_due(msk(hour=14))
        service.check_due(msk(hour=14, minute=1))
        assert gateway.bump_calls == 1
        service.check_due(msk(hour=16))
        assert gateway.bump_calls == 2
        assert storage.counts()["successful_bumps"] == 2
        assert storage.counts()["sold"] == 0
    finally:
        storage.close()


@pytest.mark.parametrize("hour,minute,allowed", [(0, 0, False), (11, 59, False), (12, 0, True), (23, 59, True)])
def test_moscow_window_boundaries(hour, minute, allowed):
    assert bump_time_allowed(msk(hour=hour, minute=minute)) == allowed


def test_window_uses_moscow_time_even_with_another_server_timezone():
    other = timezone(timedelta(hours=5))
    assert not bump_time_allowed(datetime(2030, 1, 2, 13, tzinfo=other))
    assert bump_time_allowed(datetime(2030, 1, 2, 14, tzinfo=other))


def test_overdue_bump_waits_until_noon_without_catchup_burst(tmp_path):
    service, gateway, storage = make_service(tmp_path, msk(day=1, hour=21))
    try:
        service.check_due(msk(hour=0))
        service.check_due(msk(hour=11, minute=59))
        assert gateway.bump_calls == 0
        service.check_due(msk(hour=12))
        service.check_due(msk(hour=12))
        assert gateway.bump_calls == 1
    finally:
        storage.close()


def test_paid_attempt_survives_restart_and_lost_response(tmp_path):
    service, gateway, storage = make_service(tmp_path, msk(hour=12))
    gateway.lose_bump_response = True
    with pytest.raises(ConnectionError):
        service.check_due(msk(hour=14))
    assert storage.counts()["uncertain_bumps"] == 1
    storage.close()
    reopened = Storage(tmp_path / "state.sqlite3")
    try:
        resumed = BumpService(settings(tmp_path), reopened, gateway)
        resumed.check_due(msk(hour=14, minute=1))
        assert gateway.bump_calls == 1
        gateway.lose_bump_response = False
        resumed.check_due(msk(hour=16))
        assert gateway.bump_calls == 2
    finally:
        reopened.close()


def test_price_limit_does_not_dispatch_a_paid_request(tmp_path):
    service, gateway, storage = make_service(tmp_path, msk(hour=12))
    gateway.reject_premium = True
    try:
        with pytest.raises(PremiumPriceLimitError):
            service.check_due(msk(hour=14))
        assert gateway.bump_calls == 0
        assert storage.counts()["uncertain_bumps"] == 0
        gateway.reject_premium = False
        service.check_due(msk(hour=14, minute=1))
        assert gateway.bump_calls == 1
    finally:
        storage.close()


def test_sale_resets_only_its_own_listing_timer(tmp_path, monkeypatch):
    service, first, storage = make_service(tmp_path, msk(hour=10))
    second = BumpGateway("item-2")
    replacement = Credential("next@example.test", "password")
    storage.import_accounts([second.current, replacement])
    storage.bootstrap_listing(second.item_id, second.current)
    storage.bump_baseline(second.item_id, msk(hour=10).timestamp())
    monkeypatch.setattr("playerok_bot.storage._utc_now", lambda: msk(hour=12).astimezone(timezone.utc).isoformat())
    first.active = False
    first.sold = True
    try:
        SaleService(settings(tmp_path), storage, first).process(PaidDeal("sale-1", first.item_id, None))
        service.check_due(msk(hour=12, minute=30))
        BumpService(settings(tmp_path), storage, second).check_due(msk(hour=12, minute=30))
        assert first.bump_calls == 0
        assert second.bump_calls == 1
        assert first.current == replacement
    finally:
        storage.close()


def test_inactive_listing_is_not_bumped(tmp_path):
    service, gateway, storage = make_service(tmp_path, msk(hour=12))
    gateway.active = False
    try:
        service.check_due(msk(hour=14))
        assert gateway.bump_calls == 0
    finally:
        storage.close()


def test_disabled_feature_does_not_bump(tmp_path):
    service, gateway, storage = make_service(tmp_path, msk(hour=12))
    service.settings = replace(service.settings, auto_bump_enabled=False)
    try:
        service.check_due(msk(hour=14))
        assert gateway.bump_calls == 0
    finally:
        storage.close()


def test_first_start_initializes_timer_without_immediate_payment(tmp_path):
    service, gateway, storage = make_service(tmp_path)
    try:
        service.check_due(msk(hour=14))
        assert gateway.bump_calls == 0
        service.check_due(msk(hour=16))
        assert gateway.bump_calls == 1
    finally:
        storage.close()


def test_gateway_rechecks_sale_and_midnight_before_payment():
    from types import SimpleNamespace
    from unittest.mock import Mock
    from playerokapi.enums import ItemStatuses

    gateway = PlayerokGateway(None, None)
    gateway._resolved_item_id = "item-1"
    gateway.account = Mock()
    item = SimpleNamespace(id="item-1", price=90, status=ItemStatuses.SOLD)
    gateway.get_listing = lambda: item
    gateway._premium_status = lambda item: SimpleNamespace(id="premium-id", price=25)
    assert gateway.bump_premium("premium-id", allowed_until=msk(day=3)) is None
    gateway.account.increase_item_priority_status.assert_not_called()
    item.status = ItemStatuses.APPROVED
    assert gateway.bump_premium("premium-id", allowed_until=datetime(2020, 1, 1, tzinfo=timezone.utc)) is None
    gateway.account.increase_item_priority_status.assert_not_called()
    assert gateway.bump_premium("premium-id", allowed_until=msk(day=3)) == 25
    gateway.account.increase_item_priority_status.assert_called_once_with("item-1", "premium-id")

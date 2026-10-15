from pathlib import Path
from types import SimpleNamespace

import pytest

from playerok_bot.app import BotApplication
from playerok_bot.config import Settings
from playerok_bot.errors import PremiumPriceLimitError
from playerok_bot.models import Credential, PaidDeal
from playerok_bot.service import SaleService
from playerok_bot.storage import Storage


class FakeGateway:
    item_id = "item-1"

    def __init__(self) -> None:
        self.current: Credential | None = None
        self.active = False
        self.confirmed: list[str] = []
        self.publish_calls = 0
        self.fail_after_publish_once = False
        self.sold = True
        self.reject_premium = False

    def send_courtesy_message(self, chat_id: str) -> None:
        raise AssertionError("Отправка сообщения отключена")

    def confirm_deal_sent(self, deal_id: str) -> None:
        if deal_id not in self.confirmed:
            self.confirmed.append(deal_id)

    def delivery_matches(self, credential: Credential) -> bool:
        return self.current == credential

    def update_delivery(self, credential: Credential) -> None:
        self.current = credential

    def is_active(self) -> bool:
        return self.active

    def is_sold(self) -> bool:
        return self.sold

    def publish_premium(self) -> int:
        if self.reject_premium:
            raise PremiumPriceLimitError("Премиум дороже лимита")
        self.publish_calls += 1
        self.active = True
        self.sold = False
        if self.fail_after_publish_once:
            self.fail_after_publish_once = False
            raise ConnectionError("Ответ потерян после успешной публикации")
        return 25


def settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        cookies="token=test",
        user_agent="test",
        item_reference="item-1",
        accounts_file=tmp_path / "accounts.txt",
        database_path=tmp_path / "state.sqlite3",
        log_path=tmp_path / "bot.log",
        delivery_comment="приятного пользования",
        premium_price_limit_rub=50,
        auto_confirm_deal=True,
        send_courtesy_message=False,
        courtesy_message="приятного пользования",
        reconcile_seconds=15,
        login_field_label=None,
        password_field_label=None,
        comment_field_label=None,
        combined_field_label=None,
    )


def test_sale_is_idempotent_and_lists_next_account(tmp_path: Path) -> None:
    first = Credential("first@example.test", "first-pass")
    second = Credential("second@example.test", "second-pass")
    storage = Storage(tmp_path / "state.sqlite3")
    storage.import_accounts([first, second])
    storage.bootstrap_listing("item-1", first)
    gateway = FakeGateway()
    gateway.current = first
    service = SaleService(settings(tmp_path), storage, gateway)  # type: ignore[arg-type]
    deal = PaidDeal("deal-1", "item-1", "chat-1")

    service.process(deal)
    service.process(deal)

    assert storage.counts()["sold"] == 1
    assert storage.counts()["listed"] == 1
    assert gateway.current == second
    assert gateway.confirmed == ["deal-1"]
    assert gateway.publish_calls == 1
    assert storage.get_deal("deal-1").completed_at is not None
    storage.close()


def test_last_account_does_not_republish(tmp_path: Path) -> None:
    only = Credential("only@example.test", "pass")
    storage = Storage(tmp_path / "state.sqlite3")
    storage.import_accounts([only])
    storage.bootstrap_listing("item-1", only)
    gateway = FakeGateway()
    gateway.current = only
    service = SaleService(settings(tmp_path), storage, gateway)  # type: ignore[arg-type]

    service.process(PaidDeal("deal-last", "item-1", "chat-last"))

    assert storage.counts()["sold"] == 1
    assert storage.counts()["listed"] == 0
    assert gateway.publish_calls == 0
    storage.close()


def test_retry_does_not_pay_for_premium_twice(tmp_path: Path) -> None:
    first = Credential("first@example.test", "first-pass")
    second = Credential("second@example.test", "second-pass")
    storage = Storage(tmp_path / "state.sqlite3")
    storage.import_accounts([first, second])
    storage.bootstrap_listing("item-1", first)
    gateway = FakeGateway()
    gateway.current = first
    gateway.fail_after_publish_once = True
    service = SaleService(settings(tmp_path), storage, gateway)  # type: ignore[arg-type]
    deal = PaidDeal("deal-retry", "item-1", "chat-retry")

    try:
        service.process(deal)
    except ConnectionError:
        pass
    service.process(deal)

    assert gateway.publish_calls == 1
    assert storage.get_deal("deal-retry").completed_at is not None
    storage.close()


def test_integer_bot_state_is_persistent(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    storage = Storage(database)

    assert storage.initialize_state_int("state-test", 10) == 10
    storage.set_state_int("state-test", 20)
    storage.close()

    reopened = Storage(database)
    assert reopened.initialize_state_int("state-test", 0) == 20
    reopened.close()


def exhausted_inventory(tmp_path: Path):
    first = Credential("first@example.test", "first-pass")
    storage = Storage(tmp_path / "state.sqlite3")
    storage.import_accounts([first])
    storage.bootstrap_listing("item-1", first)
    gateway = FakeGateway()
    gateway.current = first
    service = SaleService(settings(tmp_path), storage, gateway)  # type: ignore[arg-type]
    service.process(PaidDeal("deal-last", "item-1", "chat-last"))
    return storage, gateway, service


def test_restock_republishes_once_without_counting_another_sale(tmp_path: Path) -> None:
    storage, gateway, service = exhausted_inventory(tmp_path)
    service.restore_stock()
    assert gateway.publish_calls == 0

    second = Credential("second@example.test", "second-pass")
    third = Credential("third@example.test", "third-pass")
    storage.import_accounts([second, third])
    service.restore_stock()
    service.restore_stock()

    assert gateway.current == second
    assert gateway.publish_calls == 1
    assert storage.counts()["queued"] == 1
    assert storage.counts()["sold"] == 1
    assert storage.counts()["completed_deals"] == 1
    assert storage.counts()["pending_restocks"] == 0
    storage.close()


def test_restock_survives_restart_and_lost_publish_response(tmp_path: Path) -> None:
    storage, gateway, service = exhausted_inventory(tmp_path)
    second = Credential("second@example.test", "second-pass")
    storage.import_accounts([second])
    gateway.fail_after_publish_once = True
    with pytest.raises(ConnectionError):
        service.restore_stock()
    assert storage.counts()["pending_restocks"] == 1
    assert storage.counts()["failed_restocks"] == 1
    storage.close()

    reopened = Storage(tmp_path / "state.sqlite3")
    resumed = SaleService(settings(tmp_path), reopened, gateway)  # type: ignore[arg-type]
    resumed.restore_stock()

    assert gateway.current == second
    assert gateway.publish_calls == 1
    assert reopened.counts()["pending_restocks"] == 0
    assert reopened.counts()["failed_restocks"] == 0
    assert reopened.counts()["completed_deals"] == 1
    reopened.close()


def test_restock_waits_when_premium_exceeds_limit(tmp_path: Path) -> None:
    storage, gateway, service = exhausted_inventory(tmp_path)
    second = Credential("second@example.test", "second-pass")
    storage.import_accounts([second])
    gateway.reject_premium = True

    with pytest.raises(PremiumPriceLimitError):
        service.restore_stock()
    assert gateway.publish_calls == 0
    assert storage.counts()["sold"] == 1
    assert storage.counts()["pending_restocks"] == 1

    gateway.reject_premium = False
    service.restore_stock()
    assert gateway.publish_calls == 1
    assert gateway.current == second
    storage.close()


def test_restock_does_not_republish_a_manually_hidden_listing(tmp_path: Path) -> None:
    storage, gateway, service = exhausted_inventory(tmp_path)
    storage.import_accounts([Credential("second@example.test", "second-pass")])
    gateway.sold = False

    service.restore_stock()

    assert gateway.publish_calls == 0
    assert storage.counts()["queued"] == 1
    assert storage.counts()["listed"] == 0
    storage.close()


def test_second_exhaustion_can_be_restocked_again(tmp_path: Path) -> None:
    storage, gateway, service = exhausted_inventory(tmp_path)
    second = Credential("second@example.test", "second-pass")
    storage.import_accounts([second])
    service.restore_stock()

    gateway.active = False
    gateway.sold = True
    service.process(PaidDeal("deal-second", "item-1", "chat-second"))
    third = Credential("third@example.test", "third-pass")
    storage.import_accounts([third])
    service.restore_stock()

    assert gateway.current == third
    assert gateway.publish_calls == 2
    assert storage.counts()["sold"] == 2
    assert storage.counts()["completed_deals"] == 2
    storage.close()


@pytest.mark.parametrize("reserved", [False, True])
def test_application_restarts_after_exhaustion(tmp_path: Path, reserved: bool) -> None:
    storage, gateway, _ = exhausted_inventory(tmp_path)
    if reserved:
        storage.import_accounts([Credential("second@example.test", "second-pass")])
        assert storage.reserve_restock("item-1") is not None
    storage.close()
    bot_settings = settings(tmp_path)
    bot_settings.accounts_file.write_text("", encoding="utf-8")
    app = BotApplication(bot_settings)
    gateway.connect = lambda: None
    gateway.resolve_listing = lambda: SimpleNamespace(id="item-1")
    gateway.validate_listing = lambda item: SimpleNamespace(id="item-1")
    gateway.current_credential = lambda item: gateway.current
    app.listings[0].gateway = gateway  # type: ignore[assignment]

    try:
        app.prepare()
        assert app.storage.counts()["sold"] == 1
        assert app.storage.counts()["listed"] == int(reserved)
    finally:
        app.close()


def test_file_replenishment_is_imported_and_republishes_once(tmp_path: Path) -> None:
    storage, gateway, _ = exhausted_inventory(tmp_path)
    storage.close()
    bot_settings = settings(tmp_path)
    bot_settings.accounts_file.write_text(
        "first@example.test:first-pass\n"
        "Логин second@example.test Пароль second-pass\n",
        encoding="utf-8",
    )
    app = BotApplication(bot_settings)
    app.listings[0].gateway = gateway  # type: ignore[assignment]
    app.listings[0].service = SaleService(bot_settings, app.storage, gateway)  # type: ignore[arg-type]
    gateway.recent_paid_deals = lambda: []

    try:
        app.reconcile_once()
        app.reconcile_once()
        assert gateway.publish_calls == 1
        assert gateway.current == Credential("second@example.test", "second-pass")
        assert app.storage.counts()["sold"] == 1
        assert app.storage.counts()["listed"] == 1
        assert app.storage.counts()["queued"] == 0
        assert app.storage.counts()["completed_deals"] == 1
    finally:
        app.close()


def test_invalid_file_does_not_stop_deal_polling(tmp_path: Path) -> None:
    bot_settings = settings(tmp_path)
    bot_settings.accounts_file.write_text("invalid\n", encoding="utf-8")
    app = BotApplication(bot_settings)
    gateway = FakeGateway()
    checks = []
    gateway.recent_paid_deals = lambda: checks.append(True) or []
    app.listings[0].gateway = gateway  # type: ignore[assignment]
    app.listings[0].service = SaleService(bot_settings, app.storage, gateway)  # type: ignore[arg-type]

    try:
        app.reconcile_once()
        assert checks == [True]
        assert app.storage.counts()["queued"] == 0
    finally:
        app.close()

from dataclasses import replace
from types import SimpleNamespace

import pytest

from playerok_bot.app import BotApplication
from playerok_bot.config import Settings
from playerok_bot.errors import ConfigurationError, InventoryNotReadyError, ListingValidationError
from playerok_bot.gateway import ListingInfo
from playerok_bot.models import Credential, PaidDeal
from playerok_bot.service import SaleService
from test_storage_and_service import FakeGateway, settings


class ListingGateway(FakeGateway):
    def __init__(self, item_id: str, credential: Credential):
        super().__init__()
        self.item_id = item_id
        self.current = credential
        self.active = True
        self.sold = False
        self.account = SimpleNamespace(username="test")
        self.deals: list[PaidDeal] = []

    def resolve_listing(self):
        return SimpleNamespace(id=self.item_id)

    def validate_listing(self, item) -> ListingInfo:
        return ListingInfo(
            id=self.item_id,
            name="Test listing",
            price=90,
            status="APPROVED" if self.active else "SOLD",
            priority="PREMIUM",
            game_name="Car Parking Multiplayer 2",
            keep_in_sale=False,
            premium_republish_price=25,
            field_labels=("Логин", "Пароль", "Комментарий"),
        )

    def current_credential(self, item=None):
        return self.current

    def recent_paid_deals(self):
        return self.deals

    def sell(self, number: int) -> None:
        self.active = False
        self.sold = True
        self.deals.append(PaidDeal(f"deal-{number}", self.item_id, f"chat-{number}"))


def make_app(tmp_path, inventory_size=6):
    credentials = [Credential(f"user-{n}@example.test", f"pass-{n}") for n in range(inventory_size)]
    configured = replace(settings(tmp_path), additional_item_references=("item-2", "item-3"))
    configured.accounts_file.write_text(
        "\n".join(f"{credential.login}:{credential.password}" for credential in credentials),
        encoding="utf-8",
    )
    app = BotApplication(configured)
    gateways = []
    for index, runtime in enumerate(app.listings):
        gateway = ListingGateway(f"item-{index + 1}", credentials[index])
        runtime.gateway = gateway
        runtime.service = SaleService(configured, app.storage, gateway)
        gateways.append(gateway)
    return app, gateways, credentials


def test_three_initial_accounts_are_reserved_before_any_sale(tmp_path):
    app, gateways, credentials = make_app(tmp_path)
    try:
        infos = app.prepare()
        assert len(infos) == 3
        assert app.storage.counts()["listed"] == 3
        assert app.storage.counts()["queued"] == 3
        for gateway, credential in zip(gateways, credentials):
            assert app.storage.get_listed_account(gateway.item_id).credential == credential
    finally:
        app.close()


def test_three_sales_receive_different_accounts_and_are_idempotent(tmp_path):
    app, gateways, credentials = make_app(tmp_path)
    try:
        app.prepare()
        for number, gateway in enumerate(gateways, 1):
            gateway.sell(number)
        app.reconcile_once()
        app.reconcile_once()

        assert [gateway.current for gateway in gateways] == credentials[3:]
        assert [gateway.publish_calls for gateway in gateways] == [1, 1, 1]
        assert app.storage.counts()["sold"] == 3
        assert app.storage.counts()["listed"] == 3
        assert app.storage.counts()["queued"] == 0
        assert app.storage.counts()["completed_deals"] == 3
        assert [row["sold"] for row in app.storage.listing_counts()] == [1, 1, 1]
    finally:
        app.close()


def test_limited_replenishment_is_shared_without_duplicate_reservations(tmp_path):
    app, gateways, credentials = make_app(tmp_path, inventory_size=3)
    try:
        app.prepare()
        for number, gateway in enumerate(gateways, 1):
            gateway.sell(number)
        app.reconcile_once()
        assert app.storage.counts()["listed"] == 0

        with app.settings.accounts_file.open("a", encoding="utf-8") as file:
            file.write("\nnew-1@example.test:pass-1\nnew-2@example.test:pass-2\n")
        app.reconcile_once()
        assert app.storage.counts()["listed"] == 2
        assert [gateway.publish_calls for gateway in gateways] == [1, 1, 0]
        assert gateways[0].current != gateways[1].current

        with app.settings.accounts_file.open("a", encoding="utf-8") as file:
            file.write("new-3@example.test:pass-3\n")
        app.reconcile_once()
        assert app.storage.counts()["listed"] == 3
        assert [gateway.publish_calls for gateway in gateways] == [1, 1, 1]
        assert app.storage.counts()["sold"] == 3
    finally:
        app.close()


def test_publication_error_in_one_listing_does_not_stop_other_sales(tmp_path):
    app, gateways, _ = make_app(tmp_path)
    try:
        app.prepare()
        gateways[0].reject_premium = True
        for number, gateway in enumerate(gateways, 1):
            gateway.sell(number)
        app.reconcile_once()

        assert [gateway.publish_calls for gateway in gateways] == [0, 1, 1]
        assert app.storage.counts()["completed_deals"] == 2
        assert app.storage.counts()["failed_deals"] == 1

        gateways[0].reject_premium = False
        app.reconcile_once()
        assert [gateway.publish_calls for gateway in gateways] == [1, 1, 1]
        assert app.storage.counts()["completed_deals"] == 3
        assert app.storage.counts()["failed_deals"] == 0
    finally:
        app.close()


@pytest.mark.parametrize("duplicate", ["item", "account"])
def test_duplicate_listings_or_accounts_are_rejected_before_bootstrap(tmp_path, duplicate):
    app, gateways, _ = make_app(tmp_path)
    if duplicate == "item":
        gateways[1].item_id = gateways[0].item_id
    else:
        gateways[1].current = gateways[0].current
    try:
        with pytest.raises(ListingValidationError):
            app.prepare()
        assert app.storage.counts()["listed"] == 0
        assert [gateway.publish_calls for gateway in gateways] == [0, 0, 0]
    finally:
        app.close()


def test_missing_initial_account_identifies_listing_and_reserves_other_accounts(tmp_path):
    app, gateways, credentials = make_app(tmp_path)
    gateways[1].current = Credential("not-imported@example.test", "password")
    try:
        with pytest.raises(InventoryNotReadyError, match="№2") as error:
            app.prepare()
        assert error.value.lot_numbers == (2,)
        assert app.storage.get_listed_account("item-1").credential == credentials[0]
        assert app.storage.get_listed_account("item-3").credential == credentials[2]
    finally:
        app.close()


def test_adding_two_listings_preserves_existing_sales_history(tmp_path):
    app, gateways, credentials = make_app(tmp_path)
    try:
        app.storage.import_accounts([
            credentials[0], credentials[3], credentials[1], credentials[2],
            *credentials[4:],
        ])
        app.storage.bootstrap_listing("item-1", credentials[0])
        # История старой версии: обслуживался только первый лот.
        gateways[0].sell(1)
        app.listings[0].service.process(gateways[0].deals[0])
        assert gateways[0].current == credentials[3]
        app.prepare()
        assert app.storage.counts()["sold"] == 1
        assert app.storage.counts()["completed_deals"] == 1
        assert app.storage.counts()["listed"] == 3
    finally:
        app.close()


def test_config_accepts_three_links_and_keeps_single_listing_compatible(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYEROK_TOKEN", "test")
    monkeypatch.setenv("PLAYEROK_USER_AGENT", "test")
    monkeypatch.setenv("PLAYEROK_ITEM", "item-1")
    monkeypatch.setenv("PLAYEROK_ITEM_2", "item-2")
    monkeypatch.setenv("PLAYEROK_ITEM_3", "item-3")
    assert Settings.load(tmp_path).item_references == ("item-1", "item-2", "item-3")
    monkeypatch.setenv("PLAYEROK_ITEM_2", "")
    monkeypatch.setenv("PLAYEROK_ITEM_3", "")
    assert Settings.load(tmp_path).item_references == ("item-1",)
    monkeypatch.setenv("PLAYEROK_ITEM_3", "item-1")
    with pytest.raises(ConfigurationError):
        Settings.load(tmp_path)

"""Ссылки на лоты не должны попадать на склад как аккаунты.

Регрессия: ссылку https://funpay.com/lots/offerEdit?... бот разделил по первому
двоеточию на логин «https» и пароль «//funpay.com/...» и выдал её покупателю.
"""
import asyncio
from pathlib import Path

import pytest

from playerok_bot.accounts import load_accounts, parse_accounts_text
from playerok_bot.errors import AccountsFormatError
from playerok_bot.models import Credential
from playerok_bot.telegram.bot import create_router, link_hint, marketplace_links
from playerok_bot.telegram.config import TelegramSettings
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.workers import Workers

INCIDENT_LINK = "https://funpay.com/lots/offerEdit?node=2603&offer=79402043"


@pytest.mark.parametrize("text", [
    INCIDENT_LINK,
    "https://playerok.com/products/abc-123",
    "HTTP://example.test/page",
    "tg://resolve?domain=shop",
    "first@example.test:one\n" + INCIDENT_LINK,
    "Лот: https://funpay.com/lots/offer?id=1",
    "mail@example.test https://example.test/page",
    "Логин https://example.test Пароль secret",
    "login=https://example.test password=secret",
    "Логин: https://example.test\nПароль: secret",
    "Логин: mail@example.test\nПароль: https://example.test",
])
def test_links_are_not_parsed_as_accounts(text):
    with pytest.raises(AccountsFormatError, match="ссылку"):
        parse_accounts_text(text)


@pytest.mark.parametrize("text, expected", [
    ("mail@example.test:pa://ss", Credential("mail@example.test", "pa://ss")),
    ("mail@example.test:http-pass", Credential("mail@example.test", "http-pass")),
    ("https-fan@example.test:secret", Credential("https-fan@example.test", "secret")),
    ("player:https", Credential("player", "https")),
])
def test_accounts_resembling_links_still_parse(text, expected):
    assert parse_accounts_text(text) == [expected]


def test_accounts_file_with_link_is_rejected_with_line_number(tmp_path: Path):
    path = tmp_path / "accaunts.txt"
    path.write_text("first@example.test:one\n" + INCIDENT_LINK + "\n", encoding="utf-8")
    with pytest.raises(AccountsFormatError, match="Строка 2"):
        load_accounts(path)


@pytest.mark.parametrize("text, expected", [
    (INCIDENT_LINK, {"FunPay"}),
    ("https://funpay.com/lots/offer?id=1", {"FunPay"}),
    ("www.funpay.com/lots/offer?id=1", {"FunPay"}),
    ("funpay.com/lots/offer?id=1", {"FunPay"}),
    ("https://playerok.com/products/slug", {"Playerok"}),
    ("https://m.playerok.com/products/slug", {"Playerok"}),
    ("Лоты:\nhttps://funpay.com/lots/offer?id=1\nhttps://playerok.com/products/a", {"FunPay", "Playerok"}),
    ("https://example.test/page", set()),
    ("https://funpay.com.evil.test/lots/offer?id=1", set()),
    ("user@funpay.com:secret", set()),
    ("mail@example.test:funpay.com", set()),
    ("mail@example.test:secret", set()),
    ("https://[broken", set()),
])
def test_marketplace_links_are_detected(text, expected):
    assert marketplace_links(text) == expected


def test_link_hint_names_the_right_command():
    funpay = link_hint(INCIDENT_LINK)
    assert "/fp_items" in funpay and "склад не изменён" in funpay
    assert "/items" not in funpay.replace("/fp_items", "")
    playerok = link_hint("https://playerok.com/products/slug")
    assert "/items" in playerok and "/fp_items" not in playerok
    assert link_hint("mail@example.test:secret") is None


def test_router_rejects_marketplace_links_outside_setup(tmp_path):
    from aiogram import Bot, Dispatcher
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import DeleteMessage, SendMessage
    from aiogram.types import Message, Update

    class Session(BaseSession):
        def __init__(self):
            super().__init__()
            self.responses = []
            self.deleted = []

        async def close(self):
            pass

        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, DeleteMessage):
                self.deleted.append(method.message_id)
                return True
            if isinstance(method, SendMessage):
                self.responses.append(method.text)
                return Message.model_validate({
                    "message_id": 1000, "date": 1,
                    "chat": {"id": method.chat_id, "type": "private"}, "text": method.text,
                }, context={"bot": bot})
            raise AssertionError(type(method))

        async def stream_content(self, *args, **kwargs):
            yield b""

    async def scenario():
        profiles = Profiles(tmp_path)
        session = Session()
        bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFG", session=session)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(profiles, Workers(profiles, 20), TelegramSettings("test", tmp_path)))
        message_id = 0

        async def send(text):
            nonlocal message_id
            message_id += 1
            await dispatcher.feed_update(bot, Update.model_validate({"update_id": message_id, "message": {
                "message_id": message_id, "date": 1,
                "chat": {"id": 101, "type": "private"},
                "from": {"id": 101, "is_bot": False, "first_name": "User"}, "text": text,
            }}))
            return session.responses[-1]

        def stored():
            storage = profiles.storage(101)
            try:
                counts = storage.counts()
                return counts["queued"] + counts["listed"] + counts["sold"]
            finally:
                storage.close()

        try:
            # Ссылка со скриншота без /fp_items: подсказка, склад пуст, сообщение не удалено.
            assert "/fp_items" in await send(INCIDENT_LINK)
            assert "/items" in await send("https://playerok.com/products/own-item")
            assert "/fp_items" in await send("/add " + INCIDENT_LINK)
            assert "похожа на ссылку" in await send("https://example.test/page")
            assert stored() == 0
            assert session.deleted == []

            # Вторая ссылка отдельным сообщением после /fp_items тоже не попадает на склад.
            await send("/fp_items")
            assert "FunPay-лоты сохранены" in await send(INCIDENT_LINK)
            assert "/fp_items" in await send("https://funpay.com/lots/offerEdit?node=2603&offer=79402044")
            assert profiles.get(101).config["funpay_items"] == ["79402043"]
            assert stored() == 0

            # Обычный аккаунт по-прежнему импортируется, сообщение с паролем удаляется.
            assert "Склад пополнен" in await send("mail@example.test:secret")
            assert stored() == 1
            assert len(session.deleted) == 1
        finally:
            await bot.session.close()
            await dispatcher.storage.close()
            profiles.close()

    asyncio.run(scenario())

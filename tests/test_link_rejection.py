"""Ссылки и случайный текст не должны попадать на склад как аккаунты.

Регрессия: ссылку https://funpay.com/lots/offerEdit?... бот разделил по первому
двоеточию на логин «https» и пароль «//funpay.com/...» и выдал её покупателю.
"""
import asyncio
from pathlib import Path

import pytest

from playerok_bot.accounts import load_accounts, parse_accounts_text
from playerok_bot.errors import AccountsFormatError
from playerok_bot.models import Credential
from playerok_bot.telegram.billing import Billing
from playerok_bot.telegram.bot import create_router, is_link, link_hint, marketplace_links
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
    # Символ перед ссылкой раньше обходил проверку и давал логин «(https».
    "(https://example.com/page)",
    '"https://example.com"',
    "<https://example.com>",
    "•https://example.com",
    "(https://funpay.com)",
    "mail@example.test:pass https://example.com",
])
def test_links_are_not_parsed_as_accounts(text):
    with pytest.raises(AccountsFormatError, match="ссылку"):
        parse_accounts_text(text)


@pytest.mark.parametrize("text, expected", [
    ("mail@example.test:pa://ss", Credential("mail@example.test", "pa://ss")),
    ("mail@example.test:http-pass", Credential("mail@example.test", "http-pass")),
    ("https-fan@example.test:secret", Credential("https-fan@example.test", "secret")),
    ("player:https", Credential("player", "https")),
    # Пароль, начинающийся с «//», после обычного логина — не ссылка.
    ("player1://Secret12", Credential("player1", "//Secret12")),
])
def test_accounts_resembling_links_still_parse(text, expected):
    assert parse_accounts_text(text) == [expected]


def test_accounts_file_with_link_is_rejected_with_line_number(tmp_path: Path):
    path = tmp_path / "accaunts.txt"
    path.write_text("first@example.test:one\n(" + INCIDENT_LINK + ")\n", encoding="utf-8")
    with pytest.raises(AccountsFormatError, match="Строка 2"):
        load_accounts(path)


@pytest.mark.parametrize("text, expected", [
    (INCIDENT_LINK, {"FunPay"}),
    ("https://funpay.com/lots/offer?id=1", {"FunPay"}),
    ("www.funpay.com/lots/offer?id=1", {"FunPay"}),
    ("funpay.com/lots/offer?id=1", {"FunPay"}),
    ("HTTPS://FUNPAY.COM/LOTS/OFFER?ID=1", {"FunPay"}),
    ("(https://funpay.com)", {"FunPay"}),
    ("https://funpay.com.", {"FunPay"}),
    ("https://user:pw@funpay.com/lots", {"FunPay"}),
    ("https://playerok.com/products/slug", {"Playerok"}),
    ("https://m.playerok.com/products/slug", {"Playerok"}),
    ("Лоты:\nhttps://funpay.com/lots/offer?id=1\nhttps://playerok.com/products/a", {"FunPay", "Playerok"}),
    ("https://example.test/page", set()),
    ("https://funpay.com.evil.test/lots/offer?id=1", set()),
    ("https://notfunpay.com/lots", set()),
    ("evilfunpay.com/lots", set()),
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
    # Ссылка спрятана под текстом «мой лот».
    assert "/fp_items" in link_hint("мой лот", ["https://funpay.com/lots/offer?id=1"])
    assert "гиперссылка" in link_hint("мой лот", ["https://example.test/page"])


@pytest.mark.parametrize("value, expected", [
    (INCIDENT_LINK, True),
    ("(https://playerok.com/products/x)", True),
    ("funpay.com/lots/offer?id=1", True),
    ("www.playerok.com/products/x", True),
    ('"https://example.test"', True),
    ("token=abc.def; __ddg1_=value", False),
    ("eyJhbGciOiJIUzI1NiJ9.payload.signature", False),
    ("golden-key-value-1234567890", False),
])
def test_cookie_values_are_told_apart_from_links(value, expected):
    assert is_link(value) is expected


class _Harness:
    """Настоящий роутер aiogram с подменённой сетью Telegram."""

    def __init__(self, tmp_path, billing=False):
        from aiogram import Bot, Dispatcher
        from aiogram.client.session.base import BaseSession
        from aiogram.methods import DeleteMessage, SendMessage
        from aiogram.types import Message

        harness = self

        class Session(BaseSession):
            async def close(self):
                pass

            async def make_request(self, bot, method, timeout=None):
                if isinstance(method, DeleteMessage):
                    harness.deleted.append(method.message_id)
                    return True
                if isinstance(method, SendMessage):
                    harness.responses.append(method.text)
                    return Message.model_validate({
                        "message_id": 1000, "date": 1,
                        "chat": {"id": method.chat_id, "type": "private"}, "text": method.text,
                    }, context={"bot": bot})
                raise AssertionError(type(method))

            async def stream_content(self, *args, **kwargs):
                yield b""

        self.responses, self.deleted, self.files = [], [], {}
        self.connect_calls = []
        self.profiles = Profiles(tmp_path)
        self.billing = Billing(self.profiles, frozenset()) if billing else None
        workers = Workers(self.profiles, 20, self.billing)

        async def connect(uid, value, agent):
            self.connect_calls.append(("playerok", value))
            return "user"

        async def connect_funpay(uid, value, agent):
            self.connect_calls.append(("funpay", value))
            return "user"
        workers.connect, workers.connect_funpay = connect, connect_funpay
        self.bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDEFG", session=Session())

        async def download(document, destination):
            destination.write(self.files[document.file_id])
        self.bot.download = download
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(create_router(self.profiles, workers, TelegramSettings("test", tmp_path), self.billing))
        self.counter = 0

    async def send(self, text=None, *, uid=101, link=None, document=None):
        from aiogram.types import Update
        self.counter += 1
        message = {"message_id": self.counter, "date": 1, "chat": {"id": uid, "type": "private"},
                   "from": {"id": uid, "is_bot": False, "first_name": "User"}}
        if document is not None:
            self.files[f"file{self.counter}"] = document
            message["document"] = {"file_id": f"file{self.counter}", "file_unique_id": f"u{self.counter}",
                                   "file_name": "accounts.txt", "file_size": len(document)}
        else:
            message["text"] = text
        if link:
            # Гиперссылка на весь текст: в message.text адреса не видно.
            message["entities"] = [{"type": "text_link", "offset": 0, "length": len(text.encode("utf-16-le")) // 2, "url": link}]
        await self.dispatcher.feed_update(self.bot, Update.model_validate({"update_id": self.counter, "message": message}))
        return self.responses[-1]

    def stock(self, uid=101):
        storage = self.profiles.storage(uid)
        try:
            counts = storage.counts()
            return counts["queued"] + counts["listed"] + counts["sold"]
        finally:
            storage.close()

    async def close(self):
        await self.bot.session.close()
        await self.dispatcher.storage.close()
        self.profiles.close()


def run(tmp_path, scenario, **options):
    async def main():
        harness = _Harness(tmp_path, **options)
        try:
            await scenario(harness)
        finally:
            await harness.close()
    asyncio.run(main())


def test_plain_text_never_reaches_stock(tmp_path):
    async def scenario(bot):
        # Ссылка со скриншота без /fp_items: подсказка, склад пуст, сообщение не удалено.
        assert "/fp_items" in await bot.send(INCIDENT_LINK)
        assert "/items" in await bot.send("https://playerok.com/products/own-item")
        assert "/fp_items" in await bot.send("(https://funpay.com)")
        # Случайные фразы и даже аккаунт без /add на склад не попадают.
        for text in ("Спасибо большое", "Вопрос: как добавить лот", "mail@example.test:secret",
                     "https://example.test/page"):
            assert "не добавлено на склад" in await bot.send(text)
        # Гиперссылка, спрятанная под обычным текстом.
        assert "/fp_items" in await bot.send("мой лот", link=INCIDENT_LINK)
        assert bot.stock() == 0
        assert bot.deleted == []
    run(tmp_path, scenario)


def test_add_mode_imports_one_message_and_rejects_links(tmp_path):
    async def scenario(bot):
        assert "следующим сообщением" in await bot.send("/add")
        # В режиме /add ссылки отклоняются, режим остаётся для исправленного сообщения.
        assert "содержит ссылку" in await bot.send("(https://example.com/page)")
        assert "/fp_items" in await bot.send("мой лот", link=INCIDENT_LINK)
        assert "/fp_items" in await bot.send("/add " + INCIDENT_LINK)
        assert bot.stock() == 0
        await bot.send("/add")
        assert "Склад пополнен" in await bot.send("mail@example.test:secret")
        assert bot.stock() == 1 and len(bot.deleted) == 1
        # Режим одноразовый: следующий текст уже не импортируется.
        assert "не добавлено на склад" in await bot.send("Спасибо большое")
        assert "Склад пополнен" in await bot.send("/add second@example.test:secret")
        assert bot.stock() == 2
    run(tmp_path, scenario)


def test_txt_file_is_checked_for_links(tmp_path):
    async def scenario(bot):
        reply = await bot.send(document=f"a@example.test:one\n{INCIDENT_LINK}\n".encode())
        assert "/fp_items" in reply and bot.stock() == 0
        reply = await bot.send(document=b"a@example.test:one\n(https://example.com)\n")
        assert "содержит ссылку" in reply and bot.stock() == 0
        assert "Добавлено: 2" in await bot.send(document=b"a@example.test:one\nb@example.test:two\n")
        await bot.send("/add")
        assert "Добавлено: 1" in await bot.send(document=b"c@example.test:three\n")
        assert "не добавлено на склад" in await bot.send("d@example.test:four")
        assert bot.stock() == 3
    run(tmp_path, scenario)


def test_second_link_after_fp_items_gets_hint(tmp_path):
    async def scenario(bot):
        await bot.send("/fp_items")
        assert "FunPay-лоты сохранены" in await bot.send(INCIDENT_LINK)
        assert "/fp_items" in await bot.send("https://funpay.com/lots/offerEdit?node=2603&offer=79402044")
        assert bot.profiles.get(101).config["funpay_items"] == ["79402043"]
        assert bot.stock() == 0
    run(tmp_path, scenario)


def test_connect_steps_do_not_send_links_to_marketplaces(tmp_path):
    async def scenario(bot):
        await bot.send("/fp_connect")
        assert "а не golden_key" in await bot.send(INCIDENT_LINK)
        assert "а не golden_key" in await bot.send("значение ключа", link="https://example.test")
        await bot.send("/connect")
        assert "а не token" in await bot.send("(https://playerok.com/products/own-item)")
        assert "а не token" in await bot.send("www.playerok.com/products/own-item")
        assert bot.connect_calls == [] and bot.deleted == []
        # После подсказки ввод продолжается: настоящий token принимается.
        assert "Подключён ваш профиль" in await bot.send("real-token-value")
        assert bot.connect_calls == [("playerok", "token=real-token-value")]
    run(tmp_path, scenario)


def test_unpaid_user_link_is_blocked_before_import(tmp_path):
    async def scenario(bot):
        assert "подписка" in await bot.send(INCIDENT_LINK, uid=202)
        assert "подписка" in await bot.send("/add " + INCIDENT_LINK, uid=202)
        assert bot.stock(202) == 0
    run(tmp_path, scenario, billing=True)

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from playerok_bot.accounts import parse_accounts_text
from playerok_bot.errors import AccountsFormatError, ConfigurationError
from playerok_bot.models import Credential
from playerok_bot.telegram.bot import parse_items, create_router
from playerok_bot.telegram.config import DEFAULT_BROWSER_USER_AGENT, TelegramSettings
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.workers import worker_environment, Workers
from playerok_bot.telegram.lock import HostLock


@pytest.mark.parametrize("text", [
    "mail@example.test:pass:^&",
    "Логин mail@example.test пароль pass:^&",
    "login=mail@example.test password=pass:^&",
    "mail@example.test|pass:^&",
    "mail@example.test;pass:^&",
    "mail@example.test\tpass:^&",
    "Логин: mail@example.test\nПароль: pass:^&",
])
def test_multiple_account_formats_preserve_password(text):
    assert parse_accounts_text(text) == [Credential("mail@example.test", "pass:^&")]


def test_mixed_message_is_validated_in_full():
    parsed = parse_accounts_text("first@example.test:one\nЛогин: second@example.test\nПароль: two")
    assert len(parsed) == 2
    with pytest.raises(AccountsFormatError):
        parse_accounts_text("first@example.test:one\nЛогин: second@example.test")


def test_profiles_have_separate_inventory_and_encrypted_config(tmp_path):
    profiles = Profiles(tmp_path)
    try:
        profiles.connect(101, "playerok-a", "token=private-a", "agent-a")
        profiles.connect(202, "playerok-b", "token=private-b", "agent-b")
        first = profiles.storage(101)
        second = profiles.storage(202)
        try:
            first.import_accounts([Credential("first@example.test", "secret-a")])
            second.import_accounts([Credential("second@example.test", "secret-b")])
            assert first.counts()["queued"] == second.counts()["queued"] == 1
            assert first.get_account(1).login == "first@example.test"
            assert second.get_account(1).login == "second@example.test"
        finally:
            first.close()
            second.close()
        assert profiles.get(101).config["cookies"] == "token=private-a"
        assert profiles.get(202).config["cookies"] == "token=private-b"
        encrypted = profiles.connection.execute("SELECT config FROM profiles WHERE user_id=101").fetchone()[0]
        assert b"private-a" not in encrypted
        assert "private-a" not in repr(profiles.get(101))
    finally:
        profiles.close()
    reopened = Profiles(tmp_path)
    try:
        assert reopened.get(101).config["cookies"] == "token=private-a"
    finally:
        reopened.close()


def test_same_playerok_cannot_be_attached_to_two_telegram_users(tmp_path):
    profiles = Profiles(tmp_path)
    try:
        profiles.connect(101, "shared", "token=first", "agent")
        with pytest.raises(ConfigurationError):
            profiles.connect(202, "shared", "token=second", "agent")
        assert profiles.get(202).playerok_id is None
        assert profiles.get(101).config["cookies"] == "token=first"
    finally:
        profiles.close()


def test_user_directory_cannot_be_selected_by_arbitrary_path(tmp_path):
    profiles = Profiles(tmp_path)
    try:
        for invalid in ("../another-user", 0, -1):
            with pytest.raises(ConfigurationError):
                profiles.user_dir(invalid)
    finally:
        profiles.close()


def test_worker_environment_does_not_inherit_another_accounts_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYEROK_TOKEN", "host-secret")
    monkeypatch.setenv("PLAYEROK_ITEM_3", "foreign-lot")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "telegram-secret")
    monkeypatch.setenv("DATABASE_PATH", "/foreign/database")
    profiles = Profiles(tmp_path)
    try:
        profiles.connect(101, "own-playerok", "token=own-secret", "own-agent")
        profiles.update(101, items=["own-item"])
        env = worker_environment(profiles.get(101), profiles.user_dir(101))
        assert env["PLAYEROK_TOKEN"] == ""
        assert env["PLAYEROK_COOKIES"] == "token=own-secret"
        assert env["PLAYEROK_ITEM_3"] == ""
        assert "TELEGRAM_BOT_TOKEN" not in env
        assert Path(env["DATABASE_PATH"]).parent == profiles.user_dir(101)
        assert "host-secret" not in env.values()
    finally:
        profiles.close()


def test_link_parser_limits_to_three_playerok_items():
    assert len(parse_items("https://playerok.com/products/one\nhttps://playerok.com/products/two")) == 2
    for text in ("https://evil.test/products/a", "https://playerok.com/products/a https://playerok.com/products/a/",
                 "\n".join(f"https://playerok.com/products/{n}" for n in range(4))):
        with pytest.raises(ConfigurationError):
            parse_items(text)


def test_router_imports_are_bound_to_sender_and_reject_groups(tmp_path):
    from aiogram import Bot, Dispatcher
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import SendMessage, DeleteMessage
    from aiogram.types import Message, ReplyKeyboardRemove, InlineKeyboardMarkup, Update

    class Session(BaseSession):
        def __init__(self):
            super().__init__()
            self.responses = []

        async def close(self):
            pass

        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, DeleteMessage):
                return True
            if isinstance(method, SendMessage):
                assert isinstance(method.reply_markup, (ReplyKeyboardRemove, InlineKeyboardMarkup))
                self.responses.append((method.chat_id, method.text))
                return Message.model_validate({
                    "message_id": 100, "date": 1,
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
        workers = Workers(profiles, 20)
        async def connect(uid, cookies, agent):
            profiles.connect(uid, f"playerok-{uid}", cookies, agent)
            return f"user-{uid}"
        workers.connect = connect
        async def connect_funpay(uid, key, agent):
            profiles.connect_funpay(uid, f'funpay-{uid}', key, agent)
            return f'funpay-user-{uid}'
        workers.connect_funpay = connect_funpay
        dispatcher.include_router(create_router(profiles, workers, TelegramSettings("test", tmp_path)))

        async def send(uid, text, group=False):
            update = Update.model_validate({"update_id": uid, "message": {
                "message_id": uid, "date": 1,
                "chat": {"id": -123 if group else uid, "type": "group" if group else "private"},
                "from": {"id": uid, "is_bot": False, "first_name": "User"}, "text": text,
            }})
            await dispatcher.feed_update(bot, update)

        try:
            await send(101, '/start')
            assert '/commands' in session.responses[-1][1]
            assert '/stats' in session.responses[-1][1]
            assert len(session.responses[-1][1]) < 1000
            await send(101, '/commands')
            assert '/fp_connect' in session.responses[-1][1]
            assert '/pl_bump on' in session.responses[-1][1]
            await send(101, 'Команды')
            assert 'Команды бота' in session.responses[-1][1]
            await send(101, "/add first@example.test:secret-one")
            await send(202, "/add")
            await send(202, "Логин second@example.test Пароль secret-two")
            await send(303, "group@example.test:secret-three", group=True)
            # Два пользователя одновременно проходят разные шаги настройки.
            await send(101, "/pl_connect")
            await send(202, "/connect")
            await send(101, '/help')
            assert 'Как работает бот' in session.responses[-1][1]
            assert 'Карантин' in session.responses[-1][1]
            await send(101, "testtoken-a")
            await send(202, "testtoken-b")
            assert 'Подключён ваш профиль' in session.responses[-1][1]
            await send(101, "/pl_items")
            await send(101, "https://playerok.com/products/own-item-a")
            await send(202, "/items")
            await send(202, "https://playerok.com/products/own-item-b")
            assert profiles.get(101).config["cookies"] == "token=testtoken-a"
            assert profiles.get(202).config["cookies"] == "token=testtoken-b"
            assert profiles.get(101).config["user_agent"] == DEFAULT_BROWSER_USER_AGENT
            assert profiles.get(202).config["user_agent"] == DEFAULT_BROWSER_USER_AGENT
            assert profiles.get(101).config["items"] == ["https://playerok.com/products/own-item-a"]
            assert profiles.get(202).config["items"] == ["https://playerok.com/products/own-item-b"]
            await send(101, '/fp_connect')
            await send(202, '/fp_connect')
            await send(101, 'golden_key=private-key-user-a-123')
            await send(202, 'private-key-user-b-123')
            assert 'FunPay подключён' in session.responses[-1][1]
            await send(101, '/fp_items')
            await send(101, 'https://funpay.com/lots/offer?id=111\n112')
            await send(202, '/fp_items')
            await send(202, '222')
            assert profiles.get(101).config['funpay_key'] == 'private-key-user-a-123'
            assert profiles.get(202).config['funpay_key'] == 'private-key-user-b-123'
            assert profiles.get(101).config['funpay_agent'] == DEFAULT_BROWSER_USER_AGENT
            assert profiles.get(202).config['funpay_agent'] == DEFAULT_BROWSER_USER_AGENT
            assert profiles.get(101).config['funpay_items'] == ['111', '112']
            assert profiles.get(202).config['funpay_items'] == ['222']
            await send(101, "/stats")
            assert 'Ваша статистика' in session.responses[-1][1]
            assert 'Всего продано/выдано: 0' in session.responses[-1][1]
            assert 'Осталось по базе без карантина: 1' in session.responses[-1][1]
            for uid, login in ((101, "first@example.test"), (202, "second@example.test")):
                storage = profiles.storage(uid)
                try:
                    assert storage.get_account(1).login == login
                    assert storage.counts()["queued"] == 1
                finally:
                    storage.close()
            assert not (tmp_path / "users" / "303").exists()
            assert all("secret-" not in text for _, text in session.responses)
            assert all("testtoken-" not in text for _, text in session.responses)
            assert all('private-key-' not in text for _, text in session.responses)
            assert session.responses[-1][0] == 101
            await send(101, '/pl_items clear')
            assert profiles.get(101).config['items'] == []
            assert profiles.get(101).config['funpay_items'] == ['111', '112']
            assert profiles.get(202).config['items'] == ['https://playerok.com/products/own-item-b']
            assert 'Playerok-лоты убраны' in session.responses[-1][1]
            await send(101, '/group_new Second game')
            assert profiles.get(101).group_id == 2
            await send(101, '/add second-game@example.test:pass')
            await send(101, '/group 1')
            storage = profiles.storage(101)
            assert storage.counts()['queued'] == 1
            storage.close()
            await send(101, '/daily 2026-10-01')
            assert '01.10.2026' in session.responses[-1][1]
            await send(101, '/allstats')
            assert 'Все группы' in session.responses[-1][1]
            await send(101, '/fees funpay 0 3')
            assert profiles.get(101).config['funpay_withdrawal'] == 3
        finally:
            await bot.session.close()
            await dispatcher.storage.close()
            profiles.close()

    asyncio.run(scenario())


def test_only_one_telegram_host_can_use_a_data_directory(tmp_path):
    with HostLock(tmp_path):
        with pytest.raises(ConfigurationError):
            with HostLock(tmp_path):
                pass
    with HostLock(tmp_path):
        pass


def test_workers_spawn_in_different_processes_and_private_directories(tmp_path, monkeypatch):
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=None, pid=100 + len(calls))

    async def scenario():
        profiles = Profiles(tmp_path)
        workers = Workers(profiles, 20)
        for uid in (101, 202):
            profiles.connect(uid, f"playerok-{uid}", f"token=secret-{uid}", "agent")
            profiles.update(uid, items=[f"item-{uid}"])
        async def check(uid):
            return {"id": f"playerok-{uid}"}
        workers.check = check
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        try:
            await workers.start(101)
            await workers.start(202)
            assert len(calls) == 2
            assert calls[0][0][-2:] == ("playerok_bot", "run")
            assert calls[0][1]["cwd"] != calls[1][1]["cwd"]
            assert calls[0][1]["env"]["PLAYEROK_COOKIES"] == "token=secret-101"
            assert calls[1][1]["env"]["PLAYEROK_COOKIES"] == "token=secret-202"
            assert profiles.get(101).enabled and profiles.get(202).enabled
        finally:
            profiles.close()
    asyncio.run(scenario())


def test_legacy_migration_preserves_sold_accounts_and_original_database(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from playerok_bot.storage import Storage
    from playerok_bot.telegram.migrate import migrate_user
    from test_storage_and_service import settings, FakeGateway
    from playerok_bot.service import SaleService
    from playerok_bot.models import PaidDeal
    from playerok_bot.config import Settings

    legacy = settings(tmp_path)
    first, second = Credential("first@example.test", "first-pass"), Credential("second@example.test", "second-pass")
    storage = Storage(legacy.database_path)
    storage.import_accounts([first, second])
    storage.bootstrap_listing("item-1", first)
    gateway = FakeGateway()
    gateway.current = first
    SaleService(legacy, storage, gateway).process(PaidDeal("old-sale", "item-1", None))
    storage.close()
    monkeypatch.setattr(Settings, "load", lambda root: legacy)
    monkeypatch.setenv("TELEGRAM_DATA_DIR", str(tmp_path / "telegram"))
    monkeypatch.setattr(Workers, "probe", AsyncMock(return_value={"ok": True, "id": "own-playerok", "username": "own"}))
    migrate_user(101)
    profiles = Profiles(tmp_path / "telegram")
    copied = profiles.storage(101)
    original = Storage(legacy.database_path)
    try:
        assert copied.counts()["sold"] == original.counts()["sold"] == 1
        assert copied.counts()["listed"] == original.counts()["listed"] == 1
        assert copied.get_deal("old-sale").completed_at is not None
        assert profiles.get(101).config["items"] == ["item-1"]
        assert not profiles.get(101).enabled
        with pytest.raises(ConfigurationError):
            migrate_user(101)
    finally:
        copied.close()
        original.close()
        profiles.close()

import asyncio
from datetime import datetime

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText
from aiogram.types import Update, Message

from playerok_bot.finance import MSK
from playerok_bot.telegram.bot import create_router, commands_text, start_text
from playerok_bot.telegram.config import TelegramSettings
from playerok_bot.telegram.help_text import help_text
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.workers import Workers


def test_report_callbacks_reject_other_owner_and_keep_original_group(tmp_path):
    class Session(BaseSession):
        def __init__(self):
            super().__init__()
            self.edits = []

        async def close(self):
            pass

        async def make_request(self, bot, method, timeout=None):
            if isinstance(method, AnswerCallbackQuery):
                return True
            if isinstance(method, EditMessageText):
                self.edits.append(method.text)
                return Message.model_validate({'message_id': 1, 'date': 1, 'chat': {'id': method.chat_id, 'type': 'private'}, 'text': method.text})
            raise AssertionError(type(method))

        async def stream_content(self, *args, **kwargs):
            yield b''

    async def scenario():
        profiles = Profiles(tmp_path)
        profiles.get(101)
        profiles.create_group(101, 'Second group')
        session = Session()
        bot = Bot('123456:abcdefghijklmnopqrstuvwxyzABCDEFG', session=session)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(profiles, Workers(profiles, 20), TelegramSettings('test', tmp_path)))
        async def send(data):
            update = Update.model_validate({'update_id': 1, 'callback_query': {
                'id': 'query', 'chat_instance': 'private', 'data': data,
                'from': {'id': 101, 'is_bot': False, 'first_name': 'User'},
                'message': {'message_id': 1, 'date': 1, 'chat': {'id': 101, 'type': 'private'}, 'text': 'old'},
            }})
            await dispatcher.feed_update(bot, update)
        try:
            await send('report:202:1:all')
            assert not session.edits
            await send('report:101:999:all')
            assert not session.edits
            await send('report:101:1:' + datetime.now(MSK).date().isoformat())
            assert 'Основная' in session.edits[-1]
            assert profiles.get(101).group_id == 2
        finally:
            await bot.session.close()
            await dispatcher.storage.close()
            profiles.close()
    asyncio.run(scenario())


def test_all_commands_are_documented_and_texts_fit_telegram():
    for text in (start_text(), commands_text(), help_text()):
        assert len(text) <= 4096
    for command in ('start', 'commands', 'help', 'settings', 'add', 'check', 'run',
                    'stop', 'stats', 'status', 'cancel', 'connect', 'items', 'limit',
                    'bump', 'fp_connect', 'fp_items', 'fp_check', 'fp_errors',
                    'groups', 'group', 'group_new', 'group_rename', 'fees',
                    'daily', 'allstats', 'stop_all', 'bump_status'):
        assert '/' + command in commands_text()
    assert help_text() != commands_text()

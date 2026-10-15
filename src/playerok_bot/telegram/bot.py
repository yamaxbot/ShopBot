from __future__ import annotations

import asyncio
import io
import logging
import re
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, Message, ReplyKeyboardRemove

from ..accounts import parse_accounts_text
from ..errors import BotError, ConfigurationError
from ..logging_setup import configure_logging
from ..inventory import Inventory
from ..funpay.gateway import parse_lots
from .config import DEFAULT_BROWSER_USER_AGENT, TelegramSettings
from .profiles import Profiles
from .workers import Workers
from .lock import HostLock

logger = logging.getLogger(__name__)


class Setup(StatesGroup):
    token = State()
    items = State()
    limit = State()
    funpay_key = State()
    funpay_items = State()


def start_text():
    return (
        "Бот пополняет ваши лоты Playerok и FunPay. У каждого пользователя свой склад.\n\n"
        "1. Создайте объявления на площадке.\n"
        "2. Подключите Playerok: /connect → /items, или FunPay: /fp_connect → /fp_items. Можно обе площадки.\n"
        "3. Отправьте аккаунты в формате логин:пароль, каждый с новой строки, или файлом .txt. Для Playerok добавьте и данные текущих лотов.\n"
        "4. Выполните /check, затем /run.\n\n"
        "/stats — ваша статистика.\n/commands — все команды с пояснениями.\n"
        "Playerok: премиум-публикации и поднятия платные. FunPay: /stop не отключает встроенную выдачу уже загруженного товара."
    )


def commands_text():
    return (
        "Команды бота\n\n"
        "Основные\n"
        "/start — короткая инструкция по запуску.\n"
        "/commands или /help — список команд с пояснениями.\n"
        "/settings — ваши подключения и настройки.\n"
        "/add — как добавить аккаунты; можно сразу /add логин:пароль. Сообщения и .txt также принимаются без команды.\n"
        "/check — проверить подключённые площадки и лоты.\n"
        "/run — запустить обслуживание ваших лотов.\n"
        "/stop — остановить бота; очередь и история сохраняются.\n"
        "/stats или /status — продажи, остатки, резервы, лоты и ошибки только вашего профиля.\n"
        "/cancel — отменить текущий ввод.\n\n"
        "Playerok\n"
        "/connect — подключить свой профиль Playerok по cookie token.\n"
        "/items — указать 1–3 премиум-лота Playerok; /items clear — убрать их из настроек.\n"
        "/limit — установить лимит цены публикации или поднятия (по умолчанию 50 ₽).\n"
        "/bump on или /bump off — включить или выключить платные поднятия после 2 часов без покупок, с 12:00 до 00:00 МСК.\n\n"
        "FunPay\n"
        "/fp_connect — подключить свой профиль FunPay по cookie golden_key.\n"
        "/fp_items — указать 1–8 лотов с встроенной автовыдачей одинаковых аккаунтов.\n"
        "/fp_check — проверить FunPay, лоты и возможность автоподнятия.\n"
        "/fp_errors — показать изменения, результат которых требует сверки.\n\n"
        "Резерв Playerok: 5 запасных аккаунтов сверх выставленных. FunPay: каждый лот пополняется до 5 при наличии товара и поднимается, когда FunPay разрешит.\n"
        "После изменения настроек выполните /run. /stop останавливает бота, но FunPay продолжает выдавать уже загруженный товар; для остановки продаж отключите лоты на сайте."
    )


def account_help():
    return (
        "Отправьте аккаунты сообщением или файлом .txt в UTF-8. Примеры:\n\n"
        "mail@example.com:password\n"
        "Логин mail@example.com Пароль password\n"
        "login=mail@example.com password=password\n"
        "mail@example.com|password\n\n"
        "Можно блоком:\nЛогин: mail@example.com\nПароль: password\n\n"
        "Несколько аккаунтов — каждый с новой строки или отдельным блоком. "
        "Добавляйте только непроданные аккаунты, включая данные текущих лотов."
    )


def parse_items(text: str) -> list[str]:
    items = [part.strip('"\'') for part in re.split(r"[\s,]+", text.strip()) if part.strip()]
    if not 1 <= len(items) <= 3:
        raise ConfigurationError("Отправьте от одной до трёх ссылок, каждую с новой строки")
    normalized = []
    for item in items:
        parsed = urlsplit(item)
        if parsed.scheme:
            if parsed.scheme != "https" or parsed.hostname != "playerok.com" or not parsed.path.startswith("/products/"):
                raise ConfigurationError("Нужны ссылки https://playerok.com/products/... или UUID лотов")
            normalized.append("https://playerok.com" + parsed.path.rstrip("/"))
        else:
            try:
                normalized.append(str(UUID(item)))
            except ValueError as exc:
                raise ConfigurationError("Некорректная ссылка или UUID лота") from exc
    if len(set(normalized)) != len(normalized):
        raise ConfigurationError("Каждый лот укажите один раз")
    return normalized


class PrivateAccess(BaseMiddleware):
    def __init__(self, allowed_ids):
        self.allowed_ids = allowed_ids

    async def __call__(self, handler, event: Message, data):
        if event.chat.type != ChatType.PRIVATE or not event.from_user or event.from_user.is_bot:
            return
        if self.allowed_ids and event.from_user.id not in self.allowed_ids:
            await event.answer("Доступ ограничен владельцем бота.", reply_markup=ReplyKeyboardRemove())
            return
        return await handler(event, data)


def create_router(profiles: Profiles, workers: Workers, settings: TelegramSettings):
    router = Router(name="personal_playerok")
    router.message.outer_middleware(PrivateAccess(settings.allowed_user_ids))

    async def reply(message: Message, text: str, **kwargs):
        kwargs.setdefault('reply_markup', ReplyKeyboardRemove())
        return await message.answer(text, **kwargs)

    async def erase(message):
        with suppress(Exception):
            await message.delete()

    async def import_text(message: Message, text: str):
        try:
            accounts = parse_accounts_text(text)
            if len(accounts) > 10000:
                raise ConfigurationError("За один раз можно добавить не больше 10000 аккаунтов")
            profiles.get(message.from_user.id)
            storage = profiles.storage(message.from_user.id)
            try:
                inserted = storage.import_accounts(accounts)
                Inventory(storage).configure(bool(profiles.get(message.from_user.id).config['items']))
                counts = storage.counts()
            finally:
                storage.close()
        except BotError as exc:
            await reply(message, str(exc))
            return
        await erase(message)
        await reply(
            message,
            f"Добавлено: {inserted}\nУже были в базе: {len(accounts) - inserted}\n"
            f"Осталось: {counts['queued'] + counts['listed']}",
        )

    @router.message(Command("start"))
    async def start(message: Message, state: FSMContext):
        await state.clear()
        profiles.get(message.from_user.id)
        await reply(message, start_text())

    @router.message(Command("commands", "help"))
    @router.message(F.text == "Команды")
    async def commands(message: Message):
        await reply(message, commands_text())

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await reply(message, "Ввод отменён.")

    @router.message(Command("settings"))
    @router.message(F.text == "Настройки")
    async def show_settings(message: Message, state: FSMContext):
        await state.clear()
        profile = profiles.get(message.from_user.id)
        await reply(
            message,
            f"Playerok подключён: {'да' if profile.playerok_id else 'нет'}\n"
            f"Лотов: {len(profile.config['items'])}\n"
            f"Лимит операции: {profile.config['price_limit']} ₽\n"
            f"Плановые поднятия: {'включены' if profile.config['bump'] else 'выключены'}\n"
            f"Автовыдача: {'работает' if workers.running(profile.user_id) else 'остановлена'}\n\n"
            f"FunPay подключён: {'да' if profile.funpay_id else 'нет'}\n"
            f"FunPay-лотов: {len(profile.config['funpay_items'])}\n"
            "FunPay: до 5 аккаунтов в каждом лоте. Playerok: 5 в резерве сверх активных лотов.\n\n"
            "Изменить: /connect, /items, /limit, /bump on, /bump off, /fp_connect, /fp_items."
        )

    @router.message(Command('fp_connect'))
    async def fp_connect(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Setup.funpay_key)
        await reply(message, 'Отправьте значение cookie golden_key своего FunPay (F12 → Application → Cookies → funpay.com). Отмена — /cancel.')

    @router.message(Command('fp_items'))
    async def fp_items(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Setup.funpay_items)
        await reply(message, 'FunPay: отправьте от 1 до 8 ID или ссылок https://funpay.com/lots/offer?id=123, каждую с новой строки. Неактивный лот с нулевым остатком тоже подходит, если в нём включена автовыдача. Все лоты должны продавать одинаковые аккаунты; цены могут различаться.')

    @router.message(Command('fp_check'))
    async def fp_check(message: Message, state: FSMContext):
        await state.clear()
        await reply(message, 'Проверяю FunPay и автовыдачу…')
        try:
            result = await workers.check_funpay(message.from_user.id)
            await reply(message, f"FunPay проверен: {result['username']}. Запуск — /run.")
        except BotError as exc:
            await reply(message, str(exc))

    @router.message(Command('fp_errors'))
    async def fp_errors(message: Message, state: FSMContext):
        await state.clear()
        storage = profiles.storage(message.from_user.id)
        try:
            operations = Inventory(storage).pending_edits()
        finally:
            storage.close()
        lines = [f"№{op['id']}: лот {op['lot_id']}, {op['status']}" for op in operations]
        await reply(message, ('Несверенные изменения:\n' + '\n'.join(lines[:30]) + '\nБот сверит их автоматически. Не загружайте те же аккаунты вручную. Порядок ручной сверки описан в README.') if lines else 'Несверенных изменений FunPay нет.')

    @router.message(Command("connect"))
    async def connect(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Setup.token)
        await reply(
            message,
            "Отправьте значение cookie token из вашего Playerok. "
            "Можно отправить полную строку cookies. Вход должен быть выполнен в ваш профиль. "
            "Отмена — /cancel."
        )

    @router.message(Command("items"))
    @router.message(F.text == "Лоты")
    async def items(message: Message, state: FSMContext):
        parts = (message.text or '').split(maxsplit=1)
        if len(parts) > 1:
            if parts[1].strip().lower() != 'clear':
                await reply(message, 'Используйте /items для добавления Playerok-лотов или /items clear для удаления их из настроек.')
                return
            await state.clear()
            await workers.stop(message.from_user.id)
            profiles.update(message.from_user.id, items=[])
            await reply(message, 'Playerok-лоты убраны из настроек бота. Объявления на сайте остались без изменений. Если у вас настроен FunPay, выполните /run для его запуска.')
            return
        await state.clear()
        await state.set_state(Setup.items)
        await reply(message, "Playerok: отправьте ссылки на 1–3 своих премиум-лота, каждую с новой строки. В лотах должны быть разные аккаунты. «Оставлять в продаже» выключите. Для FunPay используйте /fp_items.")

    @router.message(Command("limit"))
    async def limit(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Setup.limit)
        await reply(message, "Отправьте максимальную цену одной премиум-публикации или поднятия в рублях (0–10000).")

    @router.message(Command("bump"))
    async def bump(message: Message, state: FSMContext, command: CommandObject):
        await state.clear()
        arg = (command.args or "").strip().lower()
        if arg not in {"on", "off"}:
            await reply(message, "Используйте /bump on или /bump off.")
            return
        await workers.stop(message.from_user.id)
        profiles.update(message.from_user.id, bump=arg == "on")
        await reply(message, "Настройка сохранена. Выполните /run для запуска с новыми параметрами.")

    @router.message(Command("check"))
    async def check(message: Message, state: FSMContext):
        await state.clear()
        await reply(message, "Проверяю ваш профиль и лоты…")
        try:
            result = await workers.check(message.from_user.id)
            await reply(message, f"Проверка пройдена: @{result['username']}. Можно выполнить /run.")
        except BotError as exc:
            await reply(message, str(exc))

    @router.message(Command("run"))
    @router.message(F.text == "Запустить")
    async def run(message: Message, state: FSMContext):
        await state.clear()
        await reply(message, "Проверяю настройку перед запуском…")
        try:
            await workers.start(message.from_user.id)
            await reply(message, "Автовыдача запущена. Статистика — /stats, остановка — /stop.")
        except BotError as exc:
            await reply(message, str(exc))

    @router.message(Command("stop"))
    @router.message(F.text == "Остановить")
    async def stop(message: Message, state: FSMContext):
        await state.clear()
        await workers.stop(message.from_user.id)
        await reply(message, "Автовыдача остановлена. Очередь и история сохранены.")

    @router.message(Command("stats", "status"))
    @router.message(F.text == "Статистика")
    async def stats(message: Message, state: FSMContext):
        await state.clear()
        profiles.get(message.from_user.id)
        storage = profiles.storage(message.from_user.id)
        try:
            counts = storage.counts()
            stock = Inventory(storage).stats()
        finally:
            storage.close()
        await reply(
            message,
            "Ваша статистика\n\n"
            f"Всего продано/выдано: {counts['completed_deals'] + stock['funpay_sold']}\n"
            f"Продано Playerok: {counts['completed_deals']}\nВыдано FunPay: {stock['funpay_sold']}\n"
            f"Выставлено Playerok: {counts['listed']}\nРезерв Playerok: {stock['playerok_reserve']} / 5\n"
            f"Свободно на складе: {stock['free']}\nFunPay в лотах: {stock['funpay_listed']}\n"
            f"FunPay ожидают загрузки: {stock['funpay_staged']}\nКарантин FunPay: {stock['funpay_unknown']}\n"
            f"Несверенных изменений FunPay: {stock['funpay_errors']}\n"
            f"Осталось по базе без карантина: {counts['queued'] + counts['listed'] - stock['funpay_unknown']}\n"
            f"Обработано сделок: {counts['completed_deals']}\nОшибок сделок: {counts['failed_deals']}\n"
            f"Поднятий: {counts['successful_bumps']}\n"
            f"Восстановлений с ошибкой: {counts['failed_restocks']}\n"
            f"Поднятий с неизвестным результатом: {counts['uncertain_bumps']}\n\n"
            + '\n'.join(f"FunPay {lot['lot_id']}: {lot['amount']}/5, {'активен' if lot['active'] else 'неактивен'}" for lot in stock['lots'])
        )

    @router.message(Command("add"))
    async def add(message: Message, state: FSMContext, command: CommandObject):
        await state.clear()
        if command.args:
            await import_text(message, command.args)
        else:
            await reply(message, account_help())

    @router.message(F.text == "Аккаунты")
    async def accounts_help(message: Message, state: FSMContext):
        await state.clear()
        await reply(message, account_help())

    @router.message(Setup.token, F.text)
    async def receive_token(message: Message, state: FSMContext):
        raw = message.text.strip().strip('"\'')
        if raw.startswith("PLAYEROK_TOKEN="):
            raw = raw.split("=", 1)[1].strip().strip('"\'')
        if not raw or "\n" in raw or len(raw) > 8192:
            await reply(message, "Отправьте token или cookies одной строкой.")
            return
        cookies = raw if ";" in raw or raw.startswith(("token=", "__ddg")) else "token=" + raw
        await erase(message)
        await state.clear()
        await reply(message, "Проверяю авторизацию Playerok…")
        try:
            username = await workers.connect(message.from_user.id, cookies, DEFAULT_BROWSER_USER_AGENT)
        except BotError as exc:
            await reply(message, f"{exc}\nПовторить подключение: /connect.")
            return
        await reply(message, f"Подключён ваш профиль @{username}. Укажите лоты через /items.")

    @router.message(Setup.funpay_key, F.text)
    async def receive_fp_key(message: Message, state: FSMContext):
        key = message.text.strip().strip('"\'')
        if key.startswith(('golden_key=', 'FUNPAY_GOLDEN_KEY=')):
            key = key.split('=', 1)[1].strip().strip('"\'')
        await erase(message)
        if not re.fullmatch(r'[^\s;,]{16,512}', key):
            await reply(message, 'Отправьте только значение golden_key, без других cookies.')
            return
        await state.clear()
        await reply(message, 'Проверяю авторизацию FunPay…')
        try:
            name = await workers.connect_funpay(message.from_user.id, key, DEFAULT_BROWSER_USER_AGENT)
        except BotError as exc:
            await reply(message, f'{exc}\nПовторить: /fp_connect.')
            return
        await reply(message, f'FunPay подключён: {name}. Укажите объявления через /fp_items.')

    @router.message(Setup.funpay_items, F.text)
    async def receive_fp_items(message: Message, state: FSMContext):
        try:
            lots = parse_lots(message.text)
        except BotError as exc:
            await reply(message, str(exc))
            return
        await workers.stop(message.from_user.id)
        profiles.update(message.from_user.id, funpay_items=lots)
        await state.clear()
        await reply(message, 'FunPay-лоты сохранены. Добавьте запас аккаунтов, затем /fp_check и /run. Все площадки используют ваш личный склад с отдельными резервами.')

    @router.message(Setup.items, F.text)
    async def receive_items(message: Message, state: FSMContext):
        try:
            references = parse_items(message.text)
        except BotError as exc:
            await reply(message, str(exc))
            return
        await workers.stop(message.from_user.id)
        profiles.update(message.from_user.id, items=references)
        await state.clear()
        await reply(message, "Playerok-лоты сохранены. Отправьте боту точные логины и пароли аккаунтов, уже указанных в этих лотах, и запас. Затем /check и /run.")

    @router.message(Setup.limit, F.text)
    async def receive_limit(message: Message, state: FSMContext):
        try:
            amount = int(message.text.strip())
            if not 0 <= amount <= 10000:
                raise ValueError
        except ValueError:
            await reply(message, "Нужно целое число от 0 до 10000.")
            return
        await workers.stop(message.from_user.id)
        profiles.update(message.from_user.id, price_limit=amount)
        await state.clear()
        await reply(message, "Лимит сохранён. Запуск — /run.")

    @router.message(F.document)
    async def document(message: Message, state: FSMContext):
        if await state.get_state() is not None:
            await reply(message, "Завершите настройку или выполните /cancel перед загрузкой файла.")
            return
        doc = message.document
        if not (doc.file_name or "").lower().endswith(".txt") or (doc.file_size or 0) > 524288:
            await reply(message, "Нужен файл .txt размером до 512 КБ.")
            return
        buffer = io.BytesIO()
        await message.bot.download(doc, destination=buffer)
        try:
            text = buffer.getvalue().decode("utf-8-sig")
        except UnicodeDecodeError:
            await reply(message, "Сохраните файл в UTF-8 и отправьте ещё раз.")
            return
        await import_text(message, text)

    @router.message(F.text.startswith("/"))
    async def unknown(message: Message):
        await reply(message, "Неизвестная команда. Список команд — /commands.")

    @router.message(F.text)
    async def text(message: Message):
        await import_text(message, message.text)

    @router.errors()
    async def error(event):
        # В журнал не попадает текст обновления с cookie или паролями.
        logger.error("Ошибка Telegram-обработчика: %s", type(event.exception).__name__)
        return True

    return router


async def run(settings: TelegramSettings):
    profiles = Profiles(settings.data_dir)
    workers = Workers(profiles, settings.max_workers)
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.include_router(create_router(profiles, workers, settings))
    session = AiohttpSession(proxy=settings.proxy) if settings.proxy else None
    async with Bot(token=settings.token, session=session) as bot:
        monitor = None
        try:
            await bot.set_my_commands([BotCommand(command=name, description=description) for name, description in (
                ("start", "Как начать работу"), ("commands", "Все команды с пояснениями"),
                ("settings", "Мои настройки"), ("connect", "Подключить Playerok"),
                ("items", "Мои объявления"), ("add", "Добавить аккаунты"),
                ("stats", "Моя статистика"), ("check", "Проверить лоты"),
                ("run", "Запустить продажи"), ("stop", "Остановить продажи"),
                ("help", "Помощь и список команд"),
                ("cancel", "Отменить ввод"), ("limit", "Лимит стоимости Playerok"),
                ("bump", "Включить или выключить поднятия"),
                ("fp_connect", "Подключить FunPay"), ("fp_items", "Мои лоты FunPay"),
                ("fp_check", "Проверить FunPay"), ("fp_errors", "Сверка изменений FunPay"),
            )])
            await workers.restore()
            monitor = asyncio.create_task(workers.monitor(bot))
            logger.info("Telegram-бот запущен; лимит активных профилей: %s", settings.max_workers)
            await dispatcher.start_polling(bot, close_bot_session=False,
                                           allowed_updates=dispatcher.resolve_used_update_types())
        finally:
            if monitor:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            await workers.shutdown()
            profiles.close()


def main():
    try:
        settings = TelegramSettings.load()
        configure_logging(settings.data_dir / "telegram.log")
        with HostLock(settings.data_dir):
            asyncio.run(run(settings))
    except BotError as exc:
        print(f"Ошибка: {exc}")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()

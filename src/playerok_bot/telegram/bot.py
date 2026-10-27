from __future__ import annotations

import asyncio
import io
import logging
import re
from contextlib import suppress
from pathlib import Path
from datetime import date, datetime
from urllib.parse import urlsplit
from uuid import UUID

from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, Message, ReplyKeyboardRemove, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ..accounts import parse_accounts_text
from ..errors import BotError, ConfigurationError
from ..logging_setup import configure_logging
from ..inventory import Inventory
from ..funpay.gateway import parse_lots
from .config import DEFAULT_BROWSER_USER_AGENT, TelegramSettings
from .profiles import Profiles
from .workers import Workers
from .lock import HostLock
from .reports import report_page
from .help_text import help_text, TOPICS
from ..finance import MSK, Finance, rate, render_report
from .texts import start_text, commands_text, display_time

logger = logging.getLogger(__name__)


class Setup(StatesGroup):
    token = State()
    items = State()
    limit = State()
    funpay_key = State()
    funpay_items = State()
    accounts = State()


def account_help():
    return (
        "📥 Добавить аккаунты\n\nАккаунты попадут в выбранную группу из /settings.\n"
        "Отправьте их следующим сообщением или файлом .txt в UTF-8. Примеры:\n\n"
        "mail@example.com:password\n"
        "Логин mail@example.com Пароль password\n"
        "login=mail@example.com password=password\n"
        "mail@example.com|password\n\n"
        "Можно блоком:\nЛогин: mail@example.com\nПароль: password\n\n"
        "Несколько аккаунтов — каждый с новой строки или отдельным блоком. Больше сотни удобнее отправить файлом. "
        "Добавляйте только непроданные аккаунты, включая данные текущих лотов.\n\n"
        "Можно и одной командой: /add mail@example.com:password\n"
        "Ссылки на объявления сюда не отправляйте: для них /items (Playerok) и /fp_items (FunPay).\n"
        "Отмена: /cancel"
    )


NOT_IMPORTED = (
    "Сообщение не добавлено на склад.\n\n"
    "Чтобы добавить аккаунты, отправьте /add, затем аккаунты следующим сообщением или файлом .txt. "
    "Можно и одной командой: /add логин:пароль\n\nВсе команды: /commands"
)

# Хост площадки после http(s):// (с поддоменами), либо без схемы, но с путём после домена.
# Хвост вроде «)» или «.» в конце не мешает, а funpay.com.evil.test не считается FunPay.
_MARKETPLACE_LINK = re.compile(
    r"https?://(?:[^\s/@]*@)?(?:[a-z0-9-]+\.)*(funpay|playerok)\.com(?![\w-]|\.[a-z0-9])"
    r"|(?<![\w.@/-])(?:[a-z0-9-]+\.)*(funpay|playerok)\.com/",
    re.I,
)
_PLATFORMS = {"funpay": "FunPay", "playerok": "Playerok"}
# Значение целиком — ссылка (возможно, в скобках или кавычках), а не cookie.
_LINK_VALUE = re.compile(
    r"[(<\[\"'«]*(?:[a-z][a-z0-9+.\-]*://|www\.|(?:[a-z0-9-]+\.)*(?:funpay|playerok)\.com/)", re.I
)


def marketplace_links(text: str) -> set[str]:
    """Площадки, на объявления которых ссылается текст."""
    return {_PLATFORMS[(match.group(1) or match.group(2)).lower()] for match in _MARKETPLACE_LINK.finditer(text)}


def hidden_links(message: Message) -> list[str]:
    """Адреса гиперссылок, спрятанных за текстом: в message.text их не видно."""
    return [entity.url for entity in message.entities or () if entity.type == "text_link" and entity.url]


def is_link(value: str) -> bool:
    return bool(_LINK_VALUE.match(value.strip()))


def link_hint(text: str, hidden: list[str] | tuple[str, ...] = ()) -> str | None:
    """Подсказка вместо импорта, если в сообщении ссылка на лот или скрытая гиперссылка."""
    platforms = marketplace_links("\n".join([text, *hidden]))
    if not platforms and not hidden:
        return None
    title = "🔗 В сообщении есть ссылка на объявление" if platforms else "🔗 В сообщении есть гиперссылка"
    lines = [title + "\n\nСсылки не принимаются как аккаунты, склад не изменён."]
    if "FunPay" in platforms:
        lines.append("FunPay: выполните /fp_items и отправьте все ссылки одним сообщением, каждую с новой строки. "
                     "Новый список заменяет прежний.")
    if "Playerok" in platforms:
        lines.append("Playerok: выполните /items и отправьте все ссылки одним сообщением, каждую с новой строки. "
                     "Новый список заменяет прежний.")
    lines.append("Аккаунты отправляйте простым текстом без ссылок: /add, затем аккаунты.")
    return "\n\n".join(lines)


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
        if self.allowed_ids and event.from_user.id not in self.allowed_ids and not (event.successful_payment or event.refunded_payment):
            await event.answer("Доступ ограничен владельцем бота.", reply_markup=ReplyKeyboardRemove())
            return
        return await handler(event, data)


def create_router(profiles: Profiles, workers: Workers, settings: TelegramSettings, billing=None):
    router = Router(name="personal_playerok")
    router.message.outer_middleware(PrivateAccess(settings.allowed_user_ids | settings.admin_ids if settings.allowed_user_ids else frozenset()))
    from .export_routes import ExportAccess, register_export
    router.message.outer_middleware(ExportAccess(profiles))
    if billing:
        from .billing import SubscriptionAccess
        from .payment_routes import register_payments
        router.message.outer_middleware(SubscriptionAccess(billing))
        register_payments(router, billing, workers, settings)
    register_export(router, profiles, workers)

    async def reply(message: Message, text: str, **kwargs):
        kwargs.setdefault('reply_markup', ReplyKeyboardRemove())
        return await message.answer(text, **kwargs)

    async def erase(message):
        with suppress(Exception):
            await message.delete()

    async def import_text(message: Message, text: str) -> bool:
        hint = link_hint(text, hidden_links(message))
        if hint:
            await reply(message, hint)
            return False
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
            return False
        await erase(message)
        await reply(
            message,
            f"✅ Склад пополнен\nГруппа «{profiles.get(message.from_user.id).group_name}»\n\nДобавлено: {inserted}\nУже были в базе: {len(accounts) - inserted}\n"
            f"Осталось: {counts['queued'] + counts['listed']}\n\nРаботающая группа подхватит запас автоматически.\nЕсли группа остановлена: /run · Остатки: /stats\nЕщё аккаунты: /add",
        )
        return True

    @router.message(Command("start"))
    async def start(message: Message, state: FSMContext):
        await state.clear()
        profiles.get(message.from_user.id)
        await reply(message, start_text())
        if billing:
            await reply(message, billing.summary(message.from_user.id))

    @router.message(Command("commands"))
    @router.message(F.text == "Команды")
    async def commands(message: Message):
        await reply(message, commands_text(is_admin=message.from_user.id in settings.admin_ids))

    @router.message(Command("help"))
    async def help_command(message: Message):
        await reply(message, help_text(), reply_markup=help_keyboard(message.from_user.id))

    def help_keyboard(user_id):
        buttons = [InlineKeyboardButton(text=title, callback_data=f'guide:{user_id}:{key}')
                   for key, (title, _) in TOPICS.items() if key != 'admin' or user_id in settings.admin_ids]
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
        rows.append([InlineKeyboardButton(text='📚 Все разделы', callback_data=f'guide:{user_id}:index')])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @router.callback_query(F.data.startswith('guide:'))
    async def help_callback(query: CallbackQuery):
        if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE or query.message.chat.id != query.from_user.id:
            await query.answer('Справка доступна в личном чате с ботом.')
            return
        if settings.allowed_user_ids and query.from_user.id not in settings.allowed_user_ids | settings.admin_ids:
            await query.answer('Доступ ограничен владельцем бота.')
            return
        parts = query.data.split(':')
        if len(parts) != 3 or parts[1] != str(query.from_user.id) or parts[2] not in {*TOPICS, 'index'}:
            await query.answer('Откройте справку заново: /help.')
            return
        if parts[2] == 'admin' and query.from_user.id not in settings.admin_ids:
            await query.answer('Раздел недоступен.')
            return
        text = help_text(parts[2], is_admin=query.from_user.id in settings.admin_ids)
        await query.answer()
        if query.message.text != text:
            await query.message.edit_text(text, reply_markup=help_keyboard(query.from_user.id))

    @router.message(Command("groups"))
    async def groups_command(message: Message, state: FSMContext):
        await state.clear()
        selected = profiles.get(message.from_user.id).group_id
        lines = [f"{'→' if g.group_id == selected else '•'} №{g.group_id} {g.group_name} — "
                 f"{'работает' if workers.running(g.user_id, g.group_id) else 'остановлена'}, "
                 f"Playerok: {len(g.config['items'])}, FunPay: {len(g.config['funpay_items'])}"
                 for g in profiles.groups(message.from_user.id)]
        await reply(message, 'Ваши группы\n' + '\n'.join(lines) + '\n\nВыбрать: /group ID. Создать: /group_new Название.')

    @router.message(Command("group", "group_new", "group_rename"))
    async def group_command(message: Message, state: FSMContext, command: CommandObject):
        await state.clear()
        try:
            arg = (command.args or '').strip()
            if command.command == 'group_new':
                group = profiles.create_group(message.from_user.id, arg)
            elif command.command == 'group_rename':
                if not 1 <= len(arg) <= 60 or any(ord(c) < 32 for c in arg):
                    raise ConfigurationError('Используйте /group_rename Название (до 60 символов)')
                group = profiles.get(message.from_user.id)
                import sqlite3
                try:
                    with profiles.connection:
                        profiles.connection.execute('UPDATE product_groups SET name=? WHERE user_id=? AND group_id=?',
                                                    (arg, group.user_id, group.group_id))
                except sqlite3.IntegrityError:
                    raise ConfigurationError('Такое название группы уже есть') from None
                group = profiles.get(message.from_user.id)
            else:
                if not arg.isascii() or not arg.isdigit():
                    raise ConfigurationError('Используйте /group ID. Список: /groups')
                profiles.select_group(message.from_user.id, int(arg))
                group = profiles.get(message.from_user.id)
            await reply(message, f'Выбрана группа №{group.group_id}: {group.group_name}.\n'
                        'Объявления (/items, /fp_items), отправленные аккаунты и команды /run, /stop относятся к ней. '
                        'Другие запущенные группы продолжают работать.')
        except BotError as exc:
            await reply(message, str(exc))

    @router.message(Command('stop_all'))
    async def stop_all(message: Message, state: FSMContext):
        await state.clear()
        await workers.stop_all(message.from_user.id)
        await reply(message, 'Все ваши группы остановлены. Загруженная автовыдача FunPay продолжает работать на сайте.')

    @router.message(Command('fees'))
    async def fees(message: Message, state: FSMContext, command: CommandObject):
        await state.clear()
        group = profiles.get(message.from_user.id)
        parts = (command.args or '').split()
        if not parts:
            await reply(message, f'Группа «{group.group_name}»\n' + '\n'.join(
                f"{platform}: продажа {group.config[platform + '_fee']}%, вывод {group.config[platform + '_withdrawal']}%"
                for platform in ('playerok', 'funpay')) + '\nНастроить: /fees playerok 0 6 или /fees funpay 0 3. Ставки сохраняются для новых продаж; старые записи не пересчитываются.')
            return
        try:
            if len(parts) != 3 or parts[0] not in {'playerok', 'funpay'}:
                raise ValueError()
            sale, withdrawal = rate(parts[1]), rate(parts[2])
            await workers.stop(message.from_user.id)
            profiles.update(message.from_user.id, **{parts[0] + '_fee': sale / 100, parts[0] + '_withdrawal': withdrawal / 100})
            await reply(message, 'Ставки сохранены для новых продаж выбранной группы. Выполните /run.')
        except ValueError:
            await reply(message, 'Формат: /fees playerok 0 6 или /fees funpay 0 3. Проценты от 0 до 100.')

    @router.message(Command('allstats', 'daily'))
    async def financial_report(message: Message, state: FSMContext, command: CommandObject):
        await state.clear()
        group = profiles.get(message.from_user.id)
        args = (command.args or '').split()
        group_id = 0 if command.command == 'allstats' else group.group_id
        if args and args[0] == 'all':
            group_id = 0
            args = args[1:]
        try:
            day = None if command.command == 'allstats' else date.fromisoformat(args[0]) if args else datetime.now(MSK).date()
            if len(args) > 1 or (day and not date(2000, 1, 1) <= day <= datetime.now(MSK).date()):
                raise ValueError()
        except ValueError:
            await reply(message, 'Формат: /daily ГГГГ-ММ-ДД или /daily all ГГГГ-ММ-ДД; дата не должна быть в будущем.')
            return
        text, keyboard = report_page(profiles, message.from_user.id, group_id, day)
        await reply(message, text, reply_markup=keyboard)

    @router.callback_query(F.data.startswith('report:'))
    async def report_callback(query: CallbackQuery):
        if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE or query.message.chat.id != query.from_user.id:
            await query.answer('Откройте личный чат с ботом')
            return
        if settings.allowed_user_ids and query.from_user.id not in settings.allowed_user_ids | settings.admin_ids:
            await query.answer('Доступ ограничен')
            return
        try:
            _, owner, group, value = query.data.split(':')
            if int(owner) != query.from_user.id:
                raise ValueError()
            day = None if value == 'all' else date.fromisoformat(value)
            if day and not date(2000, 1, 1) <= day <= datetime.now(MSK).date():
                raise ValueError()
            text, keyboard = report_page(profiles, query.from_user.id, int(group), day)
        except (ValueError, BotError):
            await query.answer('Отчёт недоступен. Откройте /daily заново.')
            return
        await query.answer()
        if query.message.text != text or query.message.reply_markup != keyboard:
            await query.message.edit_text(text, reply_markup=keyboard)

    @router.message(Command('bump_status'))
    async def bump_status(message: Message):
        profile = profiles.get(message.from_user.id)
        storage = profiles.storage(profile.user_id)
        try:
            with storage._lock:
                rows = storage._connection.execute("SELECT * FROM bot_state WHERE key LIKE 'bump_status:%' OR key LIKE 'bump_error:%' ORDER BY key").fetchall()
                attempts = storage._connection.execute('SELECT item_id,status,error_code FROM bumps ORDER BY id DESC LIMIT 3').fetchall()
            lines = [f"Лот {row['key'].split(':', 1)[1]} · {display_time(row['updated_at'])}\n{row['value']}" for row in rows if row['value']]
            statuses = {'success': 'поднят', 'pending': 'ожидает сверки', 'uncertain': 'результат неизвестен', 'skipped': 'пропущено без оплаты'}
            lines += [f"Попытка {row['item_id']}: {statuses.get(row['status'], row['status'])} {row['error_code'] or ''}" for row in attempts]
        finally:
            storage.close()
        await reply(message, f'🟣 Поднятия Playerok\nГруппа «{profile.group_name}»\n\nПоднятия: {"включены" if profile.config["bump"] else "выключены"}\n'
                    f'Обслуживание: {"работает" if workers.running(profile.user_id) else "остановлено"}\n\n'
                    + ('\n\n'.join(lines) or 'Проверок поднятия ещё не было. Выполните /check и /run.')
                    + '\n\nИнтервал: 2 часа без покупок отдельно для каждого лота.\nВремя: 12:00–00:00 МСК. Поднятия платные.\nПредел цены: /limit · Подробнее: /help → «Playerok»')

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
            f"⚙️ Настройки · {profile.group_name}\n"
            f"Группа №{profile.group_id}\n"
            f"Обслуживание: {'🟢 работает' if workers.running(profile.user_id) else '⏸ остановлено'}\n\n"
            "🟣 Playerok\n"
            f"Playerok подключён: {'да' if profile.playerok_id else 'нет'}\n"
            f"Лотов: {len(profile.config['items'])}\n"
            f"Лимит операции: {profile.config['price_limit']} ₽\n"
            f"Плановые поднятия: {'включены' if profile.config['bump'] else 'выключены'}\n"
            "\n🔵 FunPay\n"
            f"FunPay подключён: {'да' if profile.funpay_id else 'нет'}\n"
            f"FunPay-лотов: {len(profile.config['funpay_items'])}\n"
            "FunPay: до 5 аккаунтов в каждом лоте. Playerok: 5 в резерве сверх активных лотов.\n\n"
            "Запуск: /run · Остановка: /stop\nСменить группу: /groups\n"
            "Настройка площадок: кнопки Playerok и FunPay в /help\nПодписка: /subscription"
        )

    @router.message(Command('fp_connect'))
    async def fp_connect(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Setup.funpay_key)
        await reply(message, '🔵 Подключение FunPay\n\nОтправьте значение cookie golden_key своего профиля.\nГде найти: браузер → F12 → Application → Cookies → funpay.com → golden_key.\n\nПодключение будет общим для всех ваших групп.\nОтмена: /cancel')

    @router.message(Command('fp_items'))
    async def fp_items(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Setup.funpay_items)
        await reply(message, '🔵 Объявления FunPay\n\nОтправьте от 1 до 8 ID или ссылок, каждую с новой строки.\nПример: https://funpay.com/lots/offer?id=123\n\n• На сайте должна быть включена автовыдача.\n• Пустой неактивный лот тоже подходит.\n• Игра и тип аккаунтов должны совпадать; цены могут различаться.\n\nЛоты привяжутся к выбранной группе. Отмена: /cancel')

    @router.message(Command('fp_check'))
    async def fp_check(message: Message, state: FSMContext):
        await state.clear()
        await reply(message, 'Проверяю FunPay и автовыдачу…')
        try:
            result = await workers.check_funpay(message.from_user.id)
            await reply(message, f"✅ FunPay проверен: {result['username']}.\n\nПроверка не запускает пополнение.\nСледующий шаг: /run\nЕсли запас не загружается: /fp_errors")
        except BotError as exc:
            await reply(message, str(exc))

    @router.message(Command('fp_errors'))
    async def fp_errors(message: Message, state: FSMContext):
        await state.clear()
        storage = profiles.storage(message.from_user.id)
        try:
            operations = Inventory(storage).pending_edits()
            with storage._lock:
                errors = storage._connection.execute("SELECT key,value,updated_at FROM bot_state WHERE key LIKE 'fp_refill_error:%' AND value!='' ORDER BY key").fetchall()
        finally:
            storage.close()
        statuses = {'prepared': 'подготовлено, запрос ещё не отправлен', 'sent': 'ожидается проверка результата', 'uncertain': 'результат запроса неизвестен'}
        lines = [f"№{op['id']} · лот {op['lot_id']}\n{statuses.get(op['status'], 'требуется сверка')}" for op in operations]
        text = '🔎 Диагностика FunPay\n\n' + (('Несверенные изменения:\n' + '\n'.join(lines[:30]) + '\nБот сверит их автоматически. Не загружайте те же аккаунты вручную.') if lines else 'Несверенных изменений FunPay нет.')
        if errors:
            text += '\n\nПоследние ошибки пополнения:\n' + '\n'.join(
                f"{'Общая пауза FunPay' if row['key'].endswith(':pause') else 'Лот ' + row['key'].split(':', 1)[1]} · {display_time(row['updated_at'])}\n{row['value']}" for row in errors[:8])
        await reply(message, text[:3850] + '\n\nHTTP 429 — дождитесь паузы.\nЧто делать при ошибках: /help → «Если что-то не работает»')

    @router.message(Command("connect"))
    async def connect(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(Setup.token)
        await reply(
            message,
            "🟣 Подключение Playerok\n\nОтправьте значение cookie token своего профиля или полную строку cookies.\n"
            "Где найти: браузер → F12 → Application → Cookies → playerok.com → token.\n"
            "В браузере должен быть открыт ваш профиль.\n\nПодключение общее для всех ваших групп.\nОтмена: /cancel"
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
            await reply(message, "✅ Автовыдача выбранной группы запущена.\n\nБот проверяет лоты и пополняет их из свободного запаса. Первые изменения могут появиться не сразу, особенно во время паузы FunPay.\n\nСклад и продажи: /stats\nОшибки FunPay: /fp_errors\nПоднятия Playerok: /bump_status\nОстановка: /stop")
        except BotError as exc:
            await reply(message, str(exc))

    @router.message(Command("stop"))
    @router.message(F.text == "Остановить")
    async def stop(message: Message, state: FSMContext):
        await state.clear()
        await workers.stop(message.from_user.id)
        await reply(message, "⏸ Автовыдача выбранной группы остановлена.\nОчередь и история сохранены.\n\nОбъявления и встроенная выдача на сайтах остаются. Для полной остановки продаж снимите лоты вручную.\nВозобновить обслуживание: /run")

    @router.message(Command("stats", "status"))
    @router.message(F.text == "Статистика")
    async def stats(message: Message, state: FSMContext):
        await state.clear()
        profiles.get(message.from_user.id)
        storage = profiles.storage(message.from_user.id)
        try:
            counts = storage.counts()
            stock = Inventory(storage).stats()
            financial = Finance(storage).report()
        finally:
            storage.close()
        await reply(
            message,
            f"📊 Ваша статистика · {profiles.get(message.from_user.id).group_name}\n"
            f"Обслуживание: {'🟢 работает' if workers.running(message.from_user.id) else '⏸ остановлено'}\n\n"
            "Продажи за всё время\n"
            f"Всего продано/выдано: {counts['completed_deals'] + stock['funpay_sold']}\n"
            f"Продано Playerok: {counts['completed_deals']}\nВыдано FunPay: {stock['funpay_sold']}\n"
            f"\n📦 Склад сейчас\nВыставлено Playerok: {counts['listed']}\nРезерв Playerok: {stock['playerok_reserve']} / 5\n"
            f"Свободно на складе: {stock['free']}\nFunPay в лотах: {stock['funpay_listed']}\n"
            f"FunPay ожидают загрузки: {stock['funpay_staged']}\nКарантин FunPay: {stock['funpay_unknown']}\n"
            f"Несверенных изменений FunPay: {stock['funpay_errors']}\n"
            f"Осталось по базе без карантина: {counts['queued'] + counts['listed'] - stock['funpay_unknown']}\n"
            f"\n🔎 Состояние операций\nОбработано сделок: {counts['completed_deals']}\nОшибок сделок: {counts['failed_deals']}\n"
            f"Поднятий: {counts['successful_bumps']}\n"
            f"Восстановлений с ошибкой: {counts['failed_restocks']}\n"
            f"Поднятий с неизвестным результатом: {counts['uncertain_bumps']}\n\n"
            + '🔵 Лоты FunPay\n' + ('\n'.join(f"№{lot['lot_id']}: {lot['amount']}/5 · {'активен' if lot['active'] else 'неактивен'}" for lot in stock['lots']) or 'Лоты пока не учтены.') + '\n\n💰 Деньги за всё время\n' + render_report(financial)
            + '\n\nПо дням: /daily · Все группы: /allstats\nЧто означают статусы: /help → «Склад и статусы»'
        )

    @router.message(Command("add"))
    @router.message(F.text == "Аккаунты")
    async def add(message: Message, state: FSMContext, command: CommandObject | None = None):
        await state.clear()
        if command and command.args:
            await import_text(message, command.args)
        else:
            # Только после /add обычный текст считается аккаунтами: случайная фраза не попадёт на склад.
            await state.set_state(Setup.accounts)
            await reply(message, account_help())

    @router.message(Setup.accounts, F.text, ~F.text.startswith("/"))
    async def receive_accounts(message: Message, state: FSMContext):
        # Режим одноразовый; при ошибке остаётся, чтобы можно было исправить и отправить снова.
        if await import_text(message, message.text):
            await state.clear()

    @router.message(Setup.token, F.text)
    async def receive_token(message: Message, state: FSMContext):
        if is_link(message.text) or hidden_links(message):
            await reply(message, "🔗 Это ссылка, а не token\n\nОтправьте значение cookie token своего профиля Playerok: "
                                 "браузер → F12 → Application → Cookies → playerok.com → token.\n"
                                 "Объявления Playerok добавляются отдельно: /items.\nОтмена: /cancel")
            return
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
        if is_link(message.text) or hidden_links(message):
            await reply(message, "🔗 Это ссылка, а не golden_key\n\nОтправьте значение cookie golden_key: "
                                 "браузер → F12 → Application → Cookies → funpay.com → golden_key.\n"
                                 "Объявления FunPay добавляются отдельно: /fp_items.\nОтмена: /cancel")
            return
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
        try:
            profiles.update(message.from_user.id, funpay_items=lots)
        except BotError as exc:
            await reply(message, str(exc))
            return
        await state.clear()
        await reply(message, 'FunPay-лоты сохранены. Добавьте запас аккаунтов через /add, затем /fp_check и /run. Объявления и новые аккаунты относятся к выбранной группе (/settings).')

    @router.message(Setup.items, F.text)
    async def receive_items(message: Message, state: FSMContext):
        try:
            references = parse_items(message.text)
        except BotError as exc:
            await reply(message, str(exc))
            return
        await workers.stop(message.from_user.id)
        try:
            profiles.update(message.from_user.id, items=references)
        except BotError as exc:
            await reply(message, str(exc))
            return
        await state.clear()
        await reply(message, "Playerok-лоты сохранены. Добавьте через /add точные логины и пароли аккаунтов, уже указанных в этих лотах, и запас. Затем /check и /run.")

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
        # Файл .txt — явное действие, поэтому принимается и без /add.
        current = await state.get_state()
        if current not in (None, Setup.accounts.state):
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
        if await import_text(message, text) and current is not None:
            await state.clear()

    @router.message(F.text.startswith("/"))
    async def unknown(message: Message):
        await reply(message, "Неизвестная команда. Список команд — /commands.")

    @router.message(F.text)
    async def text(message: Message):
        # Обычный текст без /add на склад не попадает: иначе фраза из двух слов стала бы «логином и паролем».
        await reply(message, link_hint(message.text, hidden_links(message)) or NOT_IMPORTED)

    @router.errors()
    async def error(event):
        # В журнал не попадает текст обновления с cookie или паролями.
        logger.error("Ошибка Telegram-обработчика: %s", type(event.exception).__name__)
        return True

    return router


async def run(settings: TelegramSettings):
    profiles = Profiles(settings.data_dir)
    from .billing import Billing
    billing = Billing(profiles, settings.admin_ids)
    workers = Workers(profiles, settings.max_workers, billing)
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.include_router(create_router(profiles, workers, settings, billing))
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
                ("help", "Как работает бот и что означают статусы"),
                ("subscription", "Моя подписка и тарифы"), ("subscribe", "Купить или продлить подписку"),
                ("payments", "Мои платежи"), ("terms", "Условия подписки"),
                ("paysupport", "Поддержка оплаты"), ("support", "Поддержка"),
                ("export", "Забрать непроданный склад группы"), ("export_confirm", "Подтвердить выгрузку"),
                ("export_cancel", "Отменить подготовку выгрузки"), ("export_file", "Последний файл выгрузки"),
                ("groups", "Группы товаров"), ("group", "Выбрать группу"),
                ("group_new", "Создать группу"), ("group_rename", "Переименовать группу"),
                ("daily", "Статистика по дням"), ("allstats", "Все группы за всё время"),
                ("fees", "Комиссии для расчёта"), ("bump_status", "Таймеры поднятия Playerok"),
                ("stop_all", "Остановить все группы"), ("status", "Статистика выбранной группы"),
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

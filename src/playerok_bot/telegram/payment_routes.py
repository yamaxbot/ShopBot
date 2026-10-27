import logging
import time
from datetime import datetime

from aiogram import F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Message, PreCheckoutQuery
from ..errors import BotError, ConfigurationError
from ..finance import MSK
from .billing import days_text

logger = logging.getLogger(__name__)
STATUSES = {'paid': 'оплачен', 'refund_pending': 'возврат ожидает подтверждения', 'refunded': 'возвращён'}


def _moment(stamp):
    return datetime.fromtimestamp(stamp, MSK).strftime('%d.%m.%Y %H:%M МСК')


def _access(end):
    return f'до {_moment(end)}' if end > time.time() else 'не активен'


def register_payments(router, billing, workers, settings):
    def plan_keyboard(user_id):
        # В кнопке есть Telegram ID: чужая пересланная кнопка счёт не выставит.
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=f"{days_text(plan['days'])} — {plan['stars']} ⭐", callback_data=f"subscribe:{user_id}:{plan['days']}")]
            for plan in billing.plans()])

    async def send_invoice(message: Message, user_id: int, days: int):
        if not settings.admin_ids:
            await message.answer('Оплата временно недоступна. Попробуйте позже.')
            return
        try:
            payload, plan = billing.invoice(user_id, days)
        except BotError as exc:
            await message.answer(str(exc))
            return
        period = days_text(plan['days'])
        await message.answer_invoice(title=f'Подписка ShopBot · {period}',
            description=f"Автоматизация ваших групп Playerok и FunPay на {period}. Без автопродления.",
            payload=payload, provider_token='', currency='XTR',
            prices=[LabeledPrice(label=f'Доступ на {period}', amount=plan['stars'])], start_parameter='subscribe')

    @router.message(Command('subscription'))
    async def subscription(message: Message):
        await message.answer(billing.summary(message.from_user.id))

    @router.message(Command('terms'))
    async def terms(message: Message):
        example = (billing.plans() or [{'days': 7}])[0]['days']
        await message.answer("⭐ Условия подписки\n\nДоступ ко всем вашим группам Playerok и FunPay.\n\n"
            f"Тарифы\n{billing.plans_text()}\n\n"
            'Оплата и продление\nОплата разовая, без автоматического списания. Новый срок добавляется к действующему. '
            'Расходы площадок оплачиваются отдельно.\n\nПосле окончания срока\nБот прекращает обслуживание; '
            'уже опубликованные лоты и встроенная выдача FunPay остаются на сайтах до ручного отключения. '
            'Выгрузка склада и статистика доступны без подписки.\n\nУсловия оплаты\nВозврат оплаты подписки по запросу пользователя не предусмотрен. '
            'Работа зависит от доступности площадок. Вопросы по оплате: /paysupport.\n\n'
            'Если согласны с условиями, выберите срок кнопкой ниже — бот пришлёт счёт в Stars.\n'
            f'Можно и командой: /subscribe agree {example}', reply_markup=plan_keyboard(message.from_user.id))

    @router.callback_query(F.data.startswith('subscribe:'))
    async def subscribe_button(query: CallbackQuery):
        if not isinstance(query.message, Message) or query.message.chat.type != ChatType.PRIVATE or query.message.chat.id != query.from_user.id:
            await query.answer('Оплата доступна в личном чате с ботом.')
            return
        if settings.allowed_user_ids and query.from_user.id not in settings.allowed_user_ids | settings.admin_ids:
            await query.answer('Доступ ограничен владельцем бота.')
            return
        parts = query.data.split(':')
        if len(parts) != 3 or parts[1] != str(query.from_user.id) or not parts[2].isdigit():
            await query.answer('Откройте тарифы заново: /subscribe')
            return
        await query.answer()
        await send_invoice(query.message, query.from_user.id, int(parts[2]))

    @router.message(Command('support','paysupport'))
    async def paysupport(message: Message, command: CommandObject):
        text = (command.args or '').strip()
        if not text:
            await message.answer('💬 Помощь с оплатой\n\nОтправьте /paysupport и описание проблемы в одном сообщении. Укажите ID операции из /payments — он есть и в чеке Telegram.\nПароли, cookie и токены отправлять не нужно.')
            return
        if len(text) > 3000:
            await message.answer('Сократите описание до 3000 символов.')
            return
        delivered = False
        for user_id in sorted(settings.admin_ids):
            try:
                await message.bot.send_message(user_id, f'Обращение по оплате от пользователя {message.from_user.id}:\n\n{text}\n\n'
                                               f'Платежи пользователя: /admin_payments {message.from_user.id}\nВозврат: /refund ID_ОПЕРАЦИИ',
                                               parse_mode=None)
                delivered = True
            except Exception:
                continue
        await message.answer('Обращение передано в поддержку.' if delivered else 'Не удалось передать обращение. Повторите позже.')

    @router.message(Command('subscribe'))
    async def subscribe(message: Message, command: CommandObject):
        args = (command.args or '').split()
        plans = billing.plans()
        if not args or args[0] != 'agree':
            await terms(message)
            return
        if len(args) == 1 and len(plans) == 1:
            days = plans[0]['days']
        elif len(args) == 2 and args[1].isascii() and args[1].isdigit():
            days = int(args[1])
        else:
            # Срок не выбран: показываем условия и кнопки тарифов.
            await terms(message)
            return
        await send_invoice(message, message.from_user.id, days)

    @router.pre_checkout_query()
    async def checkout(query: PreCheckoutQuery):
        try:
            if settings.allowed_user_ids and query.from_user.id not in settings.allowed_user_ids | settings.admin_ids:
                raise ConfigurationError('Доступ к боту ограничен владельцем.')
            billing.validate(query.from_user.id, query.invoice_payload, query.currency, query.total_amount, checkout=True)
        except BotError as exc:
            await query.answer(ok=False, error_message=str(exc))
        else:
            await query.answer(ok=True)

    @router.message(F.successful_payment)
    async def paid(message: Message):
        try:
            payment_id = billing.paid(message.from_user.id, message.successful_payment)
        except BotError as exc:
            await message.answer(str(exc) + '\nСохраните чек Telegram и обратитесь через /paysupport.')
            return
        await message.answer(f'Платёж №{payment_id} сохранён.\nID операции: {message.successful_payment.telegram_payment_charge_id}\n\n'
                             + billing.summary(message.from_user.id) + '\nЗапуск группы: /run.')

    @router.message(F.refunded_payment)
    async def refunded(message: Message):
        billing.refunded(message.from_user.id, message.refunded_payment)
        if not billing.active(message.from_user.id):
            await workers.stop_all(message.from_user.id)
        await message.answer('Возврат учтён. ' + billing.summary(message.from_user.id))

    @router.message(Command('payments','admin_payments'))
    async def payments(message: Message, command: CommandObject):
        try:
            user_id = message.from_user.id
            if command.command == 'admin_payments':
                billing.require_admin(user_id)
                user_id = int(command.args or '0')
                if user_id <= 0:
                    raise ValueError()
            rows = billing.db.execute('SELECT id,charge_id,stars,days,paid_at,status FROM star_payments WHERE user_id=? ORDER BY id DESC LIMIT 30', (user_id,)).fetchall()
            text = '\n\n'.join(f"№{r['id']} · {r['stars']} Stars · {days_text(r['days'])} · {_moment(r['paid_at'])}\n"
                               f"{STATUSES.get(r['status'], 'требует сверки')}\nID операции: {r['charge_id']}" for r in rows)
            footer = ('\n\nВозврат: /refund ID_ОПЕРАЦИИ' if command.command == 'admin_payments'
                      else '\n\nВопрос по платежу: /paysupport — укажите ID операции.')
            await message.answer(('🧾 Последние платежи\n\n' + (text or 'Платежей пока нет. Оплатить доступ: /subscribe'))[:3900] + footer)
        except (BotError,ValueError) as exc:
            await message.answer(str(exc) if isinstance(exc,BotError) else 'Формат: /admin_payments TELEGRAM_ID')

    @router.message(Command('admin','set_price','del_price'))
    async def admin(message: Message, command: CommandObject):
        try:
            billing.require_admin(message.from_user.id)
            parts = (command.args or '').split()
            if command.command == 'admin':
                await message.answer('🔐 Управление ботом\n\nСтатистика\n/admin_stats — пользователи, подписки, оплаты и продажи за сегодня, 7 и 30 дней и всё время.\n\n'
                    f'Тарифы сейчас\n{billing.plans_text()}\n\n'
                    '/set_price 40 3 — 3 дня за 40 Stars: добавить тариф или изменить цену.\n/del_price 3 — убрать тариф на 3 дня.\n'
                    'Цена: 1–10000 Stars; срок: 1–365 дней; до 8 тарифов. Изменения относятся к новым счетам, оплаченные сроки не меняются.\n\n'
                    'Платежи и возвраты\n/admin_payments TELEGRAM_ID — последние 30 платежей с ID операций.\n'
                    '/refund ID_ОПЕРАЦИИ — показать платёж и что станет с подпиской.\n'
                    '/refund ID_ОПЕРАЦИИ confirm — вернуть Stars. Срок этого платежа перестанет учитываться; отменить возврат нельзя.\n\n'
                    'Подробности: /help → «Администратор»')
                return
            if command.command == 'set_price':
                if len(parts) != 2:
                    raise ValueError()
                billing.set_plan(message.from_user.id, int(parts[0]), int(parts[1]))
                done = f'Тариф сохранён: {days_text(int(parts[1]))} — {int(parts[0])} Stars.'
            else:
                if len(parts) != 1:
                    raise ValueError()
                billing.remove_plan(message.from_user.id, int(parts[0]))
                done = f'Тариф на {days_text(int(parts[0]))} убран.'
            await message.answer(f'{done}\n\nТарифы сейчас\n{billing.plans_text()}\n\nУже выставленные счета действуют по прежней цене 30 минут.')
        except (BotError,ValueError) as exc:
            await message.answer(str(exc) if isinstance(exc,BotError) else 'Формат: /set_price ЗВЁЗДЫ ДНИ или /del_price ДНИ. Подсказка: /admin')

    @router.message(Command('refund'))
    async def refund(message: Message, command: CommandObject):
        """Вернуть Stars по ID операции: сначала показать платёж, затем вернуть после confirm."""
        try:
            billing.require_admin(message.from_user.id)
            parts = (command.args or '').split()
            if not 1 <= len(parts) <= 2 or (len(parts) == 2 and parts[1].lower() != 'confirm'):
                raise ValueError()
            row = billing.payment_by_charge(parts[0])
        except BotError as exc:
            await message.answer(str(exc))
            return
        except ValueError:
            await message.answer('Формат: /refund ID_ОПЕРАЦИИ — показать платёж; /refund ID_ОПЕРАЦИИ confirm — вернуть Stars.\n'
                                 'ID операции: /admin_payments TELEGRAM_ID или чек Telegram у покупателя.')
            return
        user_id, charge = row['user_id'], row['charge_id']
        details = (f"💸 Возврат Stars\n\nID операции: {charge}\nПлатёж №{row['id']} · пользователь {user_id}\n"
                   f"{row['stars']} Stars · {days_text(row['days'])} · {_moment(row['paid_at'])}\n"
                   f"Статус: {STATUSES.get(row['status'], 'требует сверки')}")
        if row['status'] == 'refunded':
            await message.answer(details + '\n\nЭтот платёж уже возвращён.')
            return
        if len(parts) == 1:
            await message.answer(details + f"\n\nДоступ пользователя сейчас: {_access(billing.until(user_id))}\n"
                                 f"После возврата: {_access(billing.until(user_id, exclude_id=row['id']))}\n\n"
                                 f"Stars вернутся покупателю, срок этого платежа перестанет учитываться. Отменить возврат нельзя.\n"
                                 f"Подтвердить: /refund {charge} confirm")
            return
        try:
            await billing.refund(message.bot, message.from_user.id, row['id'])
        except BotError as exc:
            await message.answer(f'{exc}\nПовторить: /refund {charge} confirm')
            return
        except Exception as exc:
            # Ответ мог потеряться: платёж остаётся «возврат ожидает подтверждения», повтор безопасен.
            logger.warning('Возврат Stars %s не подтверждён: %s', row['id'], type(exc).__name__)
            await message.answer(f'Telegram не ответил. Статус: возврат ожидает подтверждения.\nПовторить: /refund {charge} confirm')
            return
        if not billing.active(user_id):
            await workers.stop_all(user_id)
        await message.answer(f"✅ Возврат выполнен: {row['stars']} Stars пользователю {user_id}.\n"
                             f"Доступ пользователя: {_access(billing.until(user_id))}.\n"
                             'Telegram пришлёт пользователю уведомление о возврате.')

    @router.message(Command('admin_stats'))
    async def admin_stats(message: Message):
        try:
            billing.require_admin(message.from_user.id)
        except BotError as exc:
            await message.answer(str(exc))
            return
        from .admin_stats import admin_stats_text
        running = {key for key, process in workers.processes.items() if process.returncode is None}
        await message.answer(admin_stats_text(workers.profiles, billing, running))

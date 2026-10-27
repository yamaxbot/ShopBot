from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, PreCheckoutQuery, LabeledPrice
from ..errors import BotError, ConfigurationError


def register_payments(router, billing, workers, settings):
    @router.message(Command('subscription'))
    async def subscription(message: Message):
        await message.answer(billing.summary(message.from_user.id))

    @router.message(Command('terms'))
    async def terms(message: Message):
        plan = billing.plan()
        await message.answer(f"⭐ Условия подписки\n\nТариф: {plan['stars']} Stars за {plan['days']} дней.\nДоступ ко всем вашим группам Playerok и FunPay.\n\n"
            'Оплата и продление\nОплата разовая, без автоматического списания. Новый срок добавляется к действующему. '
            'Расходы площадок оплачиваются отдельно.\n\nПосле окончания срока\nБот прекращает обслуживание; '
            'уже опубликованные лоты и встроенная выдача FunPay остаются на сайтах до ручного отключения. '
            'Выгрузка склада и статистика доступны без подписки.\n\nУсловия оплаты\nВозврат оплаты подписки по запросу пользователя не предусмотрен. '
            'Работа зависит от доступности площадок. Вопросы по оплате: /paysupport.\n\n'
            'Если согласны с условиями, получите счёт:\n/subscribe agree')

    @router.message(Command('support','paysupport'))
    async def paysupport(message: Message, command: CommandObject):
        text = (command.args or '').strip()
        if not text:
            await message.answer('💬 Помощь с оплатой\n\nОтправьте /paysupport и описание проблемы в одном сообщении. Укажите номер платежа из /payments.\nПароли, cookie и токены отправлять не нужно.')
            return
        if len(text) > 3000:
            await message.answer('Сократите описание до 3000 символов.')
            return
        delivered = False
        for user_id in sorted(settings.admin_ids):
            try:
                await message.bot.send_message(user_id, f'Обращение по оплате от пользователя {message.from_user.id}:\n\n{text}', parse_mode=None)
                delivered = True
            except Exception:
                continue
        await message.answer('Обращение передано в поддержку.' if delivered else 'Не удалось передать обращение. Повторите позже.')

    @router.message(Command('subscribe'))
    async def subscribe(message: Message, command: CommandObject):
        if (command.args or '').strip() != 'agree':
            await terms(message)
            return
        if not settings.admin_ids:
            await message.answer('Оплата временно недоступна. Попробуйте позже.')
            return
        payload, plan = billing.invoice(message.from_user.id)
        await message.answer_invoice(title='Подписка ShopBot', description=f"Автоматизация ваших групп Playerok и FunPay на {plan['days']} дней. Без автопродления.",
            payload=payload, provider_token='', currency='XTR', prices=[LabeledPrice(label='Доступ', amount=plan['stars'])], start_parameter='subscribe')

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
        await message.answer(f'Платёж №{payment_id} сохранён.\n' + billing.summary(message.from_user.id) + '\nЗапуск группы: /run.')

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
            rows = billing.db.execute('SELECT id,stars,days,status FROM star_payments WHERE user_id=? ORDER BY id DESC LIMIT 30', (user_id,)).fetchall()
            statuses = {'paid': 'оплачен', 'refund_pending': 'возврат ожидает подтверждения', 'refunded': 'возвращён'}
            await message.answer('🧾 Последние платежи\n\n' + ('\n\n'.join(f"№{r['id']} · {r['stars']} Stars · {r['days']} дней\n{statuses.get(r['status'], 'требует сверки')}" for r in rows) or 'Платежей пока нет. Оплатить доступ: /subscribe') + '\n\nВопрос по платежу: /paysupport')
        except (BotError,ValueError) as exc:
            await message.answer(str(exc) if isinstance(exc,BotError) else 'Формат: /admin_payments TELEGRAM_ID')

    @router.message(Command('admin','set_price'))
    async def admin(message: Message, command: CommandObject):
        try:
            billing.require_admin(message.from_user.id)
            parts = (command.args or '').split()
            if command.command == 'admin':
                await message.answer('🔐 Управление ботом\n\nСтатистика\n/admin_stats — пользователи, подписки, оплаты и продажи за сегодня, 7 и 30 дней и всё время.\n\nТариф\n/set_price 100 30 — 100 Stars за 30 дней.\n/set_price 150 — изменить только цену.\nЦена: 1–10000 Stars; срок: 1–365 дней. Изменения относятся к новым счетам.\n\nПлатежи пользователя\n/admin_payments TELEGRAM_ID — последние 30 платежей.\n\nПодробности: /help → «Администратор»')
            elif command.command == 'set_price':
                if not 1 <= len(parts) <= 2:
                    raise ValueError()
                billing.set_plan(message.from_user.id, int(parts[0]), int(parts[1]) if len(parts)==2 else billing.plan()['days'])
                plan = billing.plan()
                await message.answer(f"Новый тариф: {plan['stars']} Stars за {plan['days']} дней.")
        except (BotError,ValueError) as exc:
            await message.answer(str(exc) if isinstance(exc,BotError) else 'Неверный формат. Подсказка: /admin')

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

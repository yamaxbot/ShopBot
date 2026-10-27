"""Durable Stars invoices, prepaid access and full refunds."""
import time
import secrets
from datetime import datetime
from ..finance import MSK

from aiogram import BaseMiddleware
from ..errors import ConfigurationError

# Тарифы первого запуска: (дней, Stars). Дальше их меняют администраторы.
DEFAULT_PLANS = ((3, 20), (7, 35), (15, 70))
MAX_PLANS = 8


def days_text(days: int) -> str:
    """«1 день», «3 дня», «15 дней», «21 день»."""
    if 11 <= days % 100 <= 14 or not 1 <= days % 10 <= 4:
        word = 'дней'
    else:
        word = 'день' if days % 10 == 1 else 'дня'
    return f'{days} {word}'


class Billing:
    def __init__(self, profiles, admin_ids=frozenset()):
        self.db = profiles.connection
        self.admin_ids = admin_ids
        fresh = not self.db.execute("SELECT 1 FROM sqlite_master WHERE name='billing_plans'").fetchone()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS billing_plans (days INTEGER PRIMARY KEY, stars INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS star_invoices (
                payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL, stars INTEGER NOT NULL,
                days INTEGER NOT NULL, created_at INTEGER NOT NULL, approved INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS star_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, charge_id TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL, user_id INTEGER NOT NULL, stars INTEGER NOT NULL,
                days INTEGER NOT NULL, paid_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'paid');
            CREATE TABLE IF NOT EXISTS billing_audit (
                id INTEGER PRIMARY KEY, admin_id INTEGER, action TEXT, detail TEXT, created_at INTEGER);
        """)
        # Таблица прежнего единственного тарифа (billing_plan) больше не используется.
        # Тарифы по умолчанию добавляются один раз: удалённые администратором не возвращаются.
        if fresh:
            with self.db:
                self.db.executemany('INSERT OR IGNORE INTO billing_plans VALUES (?,?)', DEFAULT_PLANS)

    def plans(self):
        """Тарифы по возрастанию срока: [{'days': 3, 'stars': 20}, …]."""
        return [dict(row) for row in self.db.execute('SELECT days,stars FROM billing_plans ORDER BY days')]

    def plans_text(self):
        return '\n'.join(f"• {days_text(plan['days'])} — {plan['stars']} ⭐" for plan in self.plans()) or 'Тарифы пока не настроены.'

    def plan_for(self, days):
        row = self.db.execute('SELECT days,stars FROM billing_plans WHERE days=?', (days,)).fetchone()
        if not row:
            raise ConfigurationError('Такого тарифа нет. Выберите срок кнопкой: /subscribe')
        return dict(row)

    def set_plan(self, admin_id, stars, days):
        """Добавить тариф или изменить цену тарифа с этим сроком."""
        self.require_admin(admin_id)
        if not 1 <= stars <= 10000 or not 1 <= days <= 365:
            raise ConfigurationError('Цена: 1–10000 Stars; срок: 1–365 дней.')
        with self.db:
            exists = self.db.execute('SELECT 1 FROM billing_plans WHERE days=?', (days,)).fetchone()
            if not exists and len(self.plans()) >= MAX_PLANS:
                raise ConfigurationError(f'Можно не больше {MAX_PLANS} тарифов. Уберите лишний: /del_price ДНИ')
            self.db.execute('INSERT INTO billing_plans VALUES (?,?) ON CONFLICT(days) DO UPDATE SET stars=excluded.stars',
                            (days, stars))
            self.audit(admin_id, 'plan', f'{stars}/{days}')

    def remove_plan(self, admin_id, days):
        self.require_admin(admin_id)
        with self.db:
            if not self.db.execute('SELECT 1 FROM billing_plans WHERE days=?', (days,)).fetchone():
                raise ConfigurationError(f'Тарифа на {days_text(days)} нет. Текущие тарифы: /admin')
            if len(self.plans()) == 1:
                raise ConfigurationError('Должен остаться хотя бы один тариф. Сначала добавьте другой: /set_price ЗВЁЗДЫ ДНИ')
            self.db.execute('DELETE FROM billing_plans WHERE days=?', (days,))
            self.audit(admin_id, 'plan_removed', days)

    def audit(self, admin_id, action, detail):
        self.db.execute('INSERT INTO billing_audit(admin_id,action,detail,created_at) VALUES (?,?,?,?)',
                        (admin_id, action, str(detail), int(time.time())))

    def require_admin(self, user_id):
        if user_id not in self.admin_ids:
            raise ConfigurationError('Команда недоступна.')

    def until(self, user_id):
        end = 0
        for row in self.db.execute("SELECT * FROM star_payments WHERE user_id=? AND status='paid' ORDER BY paid_at,id", (user_id,)):
            end = max(end, row['paid_at']) + row['days'] * 86400
        return end

    def active(self, user_id):
        return user_id in self.admin_ids or self.until(user_id) > time.time()

    def require_active(self, user_id):
        if not self.active(user_id):
            raise ConfigurationError('Для работы нужна подписка: /subscribe. Статистика, остановка и выгрузка остаются доступны.')

    def summary(self, user_id):
        end = self.until(user_id)
        state = 'Администратор: доступ без оплаты.' if user_id in self.admin_ids else (
            'Оплачено до ' + datetime.fromtimestamp(end, MSK).strftime('%d.%m.%Y %H:%M МСК') if end > time.time() else 'Подписка не активна.')
        return (f"⭐ Подписка ShopBot\n\n{state}\nДоступ действует на все ваши группы.\n\n"
                f"Тарифы:\n{self.plans_text()}\n\n"
                'Купить или продлить: /subscribe\nАвтоматических списаний нет.\nПлатежи: /payments · Условия: /terms')

    def invoice(self, user_id, days):
        plan = self.plan_for(days)
        payload = 'shop:' + secrets.token_urlsafe(24)
        with self.db:
            self.db.execute('INSERT INTO star_invoices VALUES (?,?,?,?,?,0)',
                            (payload, user_id, plan['stars'], plan['days'], int(time.time())))
        return payload, plan

    def validate(self, user_id, payload, currency, amount, checkout=False):
        row = self.db.execute('SELECT * FROM star_invoices WHERE payload=?', (payload,)).fetchone()
        if not row or row['user_id'] != user_id or currency != 'XTR' or amount != row['stars']:
            raise ConfigurationError('Счёт не соответствует платежу. Создайте новый через /subscribe.')
        if checkout:
            if time.time() - row['created_at'] > 1800 or self.db.execute('SELECT 1 FROM star_payments WHERE payload=?', (payload,)).fetchone():
                raise ConfigurationError('Счёт истёк или уже оплачен. Создайте новый через /subscribe.')
            with self.db:
                self.db.execute('UPDATE star_invoices SET approved=1 WHERE payload=?', (payload,))
        elif not row['approved']:
            raise ConfigurationError('Платёж требует сверки: /paysupport.')
        return row

    def paid(self, user_id, payment):
        row = self.validate(user_id, payment.invoice_payload, payment.currency, payment.total_amount)
        existing = self.db.execute('SELECT * FROM star_payments WHERE charge_id=?', (payment.telegram_payment_charge_id,)).fetchone()
        if existing:
            if existing['user_id'] != user_id or existing['payload'] != payment.invoice_payload:
                raise ConfigurationError('Платёж требует сверки: /paysupport.')
            return existing['id']
        with self.db:
            cursor = self.db.execute('INSERT INTO star_payments(charge_id,payload,user_id,stars,days,paid_at) VALUES (?,?,?,?,?,?)',
                (payment.telegram_payment_charge_id, payment.invoice_payload, user_id, row['stars'], row['days'], int(time.time())))
        return cursor.lastrowid

    def payment(self, payment_id):
        row = self.db.execute('SELECT * FROM star_payments WHERE id=?', (payment_id,)).fetchone()
        if not row:
            raise ConfigurationError('Платёж не найден.')
        return row

    async def refund(self, bot, admin_id, payment_id):
        self.require_admin(admin_id)
        row = self.payment(payment_id)
        if row['status'] == 'refunded':
            return row['user_id']
        with self.db:
            self.db.execute("UPDATE star_payments SET status='refund_pending' WHERE id=?", (payment_id,))
            self.audit(admin_id, 'refund_requested', payment_id)
        # Повтор после сетевой ошибки использует тот же charge_id, без нового платежа.
        from aiogram.exceptions import TelegramBadRequest
        try:
            result = await bot.refund_star_payment(user_id=row['user_id'], telegram_payment_charge_id=row['charge_id'])
            if result is not True:
                raise ConfigurationError('Возврат не подтверждён. Повторите /refund для этого платежа.')
        except TelegramBadRequest as exc:
            if 'CHARGE_ALREADY_REFUNDED' not in str(exc).upper():
                raise ConfigurationError('Telegram не подтвердил возврат. Платёж сохранён для повторной сверки.') from None
        with self.db:
            self.db.execute("UPDATE star_payments SET status='refunded' WHERE id=?", (payment_id,))
            self.audit(admin_id, 'refunded', payment_id)
        return row['user_id']

    def refunded(self, user_id, payment):
        row = self.db.execute('SELECT * FROM star_payments WHERE charge_id=?', (payment.telegram_payment_charge_id,)).fetchone()
        if not row or row['user_id'] != user_id or row['stars'] != payment.total_amount or payment.currency != 'XTR':
            raise ConfigurationError('Неизвестный возврат: обратитесь в /paysupport.')
        with self.db:
            self.db.execute("UPDATE star_payments SET status='refunded' WHERE id=?", (row['id'],))


class SubscriptionAccess(BaseMiddleware):
    FREE = {'start','help','commands','subscribe','subscription','payments','terms','support','paysupport',
            'stop','stop_all','stats','status','daily','allstats','settings','groups','group','cancel','bump_status',
            'export','export_confirm','export_cancel','export_file','admin','set_price','del_price','refund','admin_payments','admin_stats','fp_errors'}

    def __init__(self, billing):
        self.billing = billing

    async def __call__(self, handler, event, data):
        if event.successful_payment or event.refunded_payment:
            return await handler(event, data)
        words = (event.text or '').split(maxsplit=1)
        command = words[0].split('@')[0][1:] if words and words[0].startswith('/') else ''
        if command not in self.FREE and not self.billing.active(event.from_user.id):
            await event.answer('Для работы нужна подписка: /subscribe. Ваш склад сохранён; /export доступен без оплаты.')
            return
        return await handler(event, data)

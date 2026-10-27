"""Сводка для администраторов: пользователи, подписки, оплаты Stars и объём продаж."""
from __future__ import annotations

import logging
import time
from datetime import datetime

from ..finance import MSK
from ..inventory import Inventory

logger = logging.getLogger(__name__)
DAY = 86400
EXPIRING_DAYS = 3
PERIODS = ("сегодня", "7 дней", "30 дней", "всего")


def period_starts(now: float) -> tuple[int, int, int, int]:
    """Начало суток по МСК, 7 и 30 суток назад и «всё время»."""
    midnight = datetime.fromtimestamp(now, MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp()), int(now - 7 * DAY), int(now - 30 * DAY), 0


def collect(profiles, billing, running=frozenset(), now: float | None = None) -> dict:
    """running — множество (user_id, group_id) работающих сейчас процессов."""
    now = time.time() if now is None else now
    starts = period_starts(now)
    db = profiles.connection
    admins = billing.admin_ids

    users = db.execute('SELECT user_id, created_at, playerok_id, funpay_id FROM profiles').fetchall()
    known = [row['created_at'] for row in users if row['created_at'] is not None]
    groups = db.execute('SELECT user_id, group_id FROM product_groups').fetchall()

    payments = db.execute('SELECT id, user_id, stars, paid_at, status FROM star_payments ORDER BY paid_at, id').fetchall()
    # Возвращённые платежи не считаются покупками: первые покупки + продления = оплаты.
    paid = [row for row in payments if row['status'] == 'paid']
    refunds = [row for row in payments if row['status'] != 'paid']
    first_paid = {}
    for row in paid:
        first_paid.setdefault(row['user_id'], row['id'])
    invoices = [row[0] for row in db.execute('SELECT created_at FROM star_invoices')]

    # Срок доступа считает сам Billing, чтобы отчёт совпадал с реальным доступом.
    active = expiring = lapsed = 0
    for user_id in {row['user_id'] for row in paid} - set(admins):
        until = billing.until(user_id)
        if until > now:
            active += 1
            expiring += until <= now + EXPIRING_DAYS * DAY
        else:
            lapsed += 1

    def count(values):
        return [sum(value >= start for value in values) for start in starts]

    stats = {
        'now': now,
        'users': len(users),
        'unknown_date': len(users) - len(known),
        'new_users': count(known)[:3] + [len(users)],
        'playerok': sum(row['playerok_id'] is not None for row in users),
        'funpay': sum(row['funpay_id'] is not None for row in users),
        'any_platform': sum(row['playerok_id'] is not None or row['funpay_id'] is not None for row in users),
        'admins': sum(row['user_id'] in admins for row in users),
        'groups': len(groups),
        'running_groups': len(running),
        'running_sellers': len({user_id for user_id, _ in running}),
        'active': active,
        'expiring': expiring,
        'lapsed': lapsed,
        'payers': len(first_paid),
        'payments': count([row['paid_at'] for row in paid]),
        'stars': [sum(row['stars'] for row in paid if row['paid_at'] >= start) for start in starts],
        'new_payers': count([row['paid_at'] for row in paid if first_paid[row['user_id']] == row['id']]),
        'renewals': count([row['paid_at'] for row in paid if first_paid[row['user_id']] != row['id']]),
        'invoices': count(invoices),
        'refunds': len(refunds),
        'refunded_stars': sum(row['stars'] for row in refunds),
        'plan': billing.plan(),
        'sold': 0,
        'in_stock': 0,
        'unread_groups': 0,
    }
    for row in groups:
        try:
            storage = profiles.storage(row['user_id'], row['group_id'])
        except Exception as exc:
            logger.warning('Статистика группы %s/%s недоступна: %s', row['user_id'], row['group_id'], type(exc).__name__)
            stats['unread_groups'] += 1
            continue
        try:
            counts = storage.counts()
            stock = Inventory(storage).stats()
            stats['sold'] += counts['completed_deals'] + stock['funpay_sold']
            stats['in_stock'] += counts['queued'] + counts['listed'] - stock['funpay_unknown']
        finally:
            storage.close()
    return stats


def _row(label: str, values) -> str:
    return f"{label}: " + " · ".join(f"{name} {value}" for name, value in zip(PERIODS, values))


def render(stats: dict) -> str:
    users = stats['users']
    conversion = f" ({stats['payers'] / users:.1%})" if users else ""
    lines = [
        "📈 Статистика бота · для администраторов",
        datetime.fromtimestamp(stats['now'], MSK).strftime('%d.%m.%Y %H:%M МСК'),
        "",
        "👥 Пользователи",
        f"Всего: {users}",
        _row("Новые", stats['new_users']),
    ]
    if stats['unknown_date']:
        lines.append(f"Без даты регистрации: {stats['unknown_date']} (пришли до включения учёта, в периоды не входят)")
    lines += [
        f"Подключили Playerok: {stats['playerok']} · FunPay: {stats['funpay']} · хотя бы одну: {stats['any_platform']}",
        f"Администраторы: {stats['admins']}",
        "",
        "🗂 Группы",
        f"Всего групп: {stats['groups']}",
        f"Работают сейчас: {stats['running_groups']} у {stats['running_sellers']} продавцов",
        "",
        "⭐ Подписки",
        f"Активны сейчас: {stats['active']} (администраторы не считаются)",
        f"Истекают в ближайшие {EXPIRING_DAYS} дня: {stats['expiring']}",
        f"Истекли и не продлены: {stats['lapsed']}",
        f"Оплатили хотя бы раз: {stats['payers']} из {users}{conversion}",
        "",
        "💳 Оплаты Stars",
        _row("Оплат", stats['payments']),
        _row("Stars", stats['stars']),
        _row("Первые покупки", stats['new_payers']),
        _row("Продления", stats['renewals']),
        _row("Счетов выставлено", stats['invoices']),
        f"Возвраты за всё время: {stats['refunds']} ({stats['refunded_stars']} Stars)",
        f"Тариф сейчас: {stats['plan']['stars']} Stars за {stats['plan']['days']} дней",
        "",
        "📦 Продажи продавцов",
        f"Продано/выдано аккаунтов за всё время: {stats['sold']}",
        f"Аккаунтов на складах сейчас: {stats['in_stock']}",
    ]
    if stats['unread_groups']:
        lines.append(f"Не удалось прочитать групп: {stats['unread_groups']} (подробности в журнале)")
    lines += ["", "Сегодня — с 00:00 МСК; 7 и 30 дней — последние 7 и 30 суток до текущего момента.",
              "Оплаты, первые покупки, продления и Stars — без возвращённых платежей."]
    return "\n".join(lines)


def admin_stats_text(profiles, billing, running=frozenset(), now: float | None = None) -> str:
    return render(collect(profiles, billing, running, now))

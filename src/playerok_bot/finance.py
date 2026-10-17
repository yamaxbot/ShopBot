"""История денег в копейках. Неизвестные суммы никогда не считаются нулевыми продажами."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import os

MSK = timezone(timedelta(hours=3))
SCHEMA = """
CREATE TABLE IF NOT EXISTS revenue (
    platform TEXT NOT NULL, order_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
    observed_date INTEGER NOT NULL, quantity INTEGER NOT NULL,
    gross INTEGER, currency TEXT NOT NULL, fee_bps INTEGER, withdrawal_bps INTEGER,
    status TEXT NOT NULL, PRIMARY KEY(platform,order_id)
);
CREATE TABLE IF NOT EXISTS expenses (
    key TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, amount INTEGER NOT NULL
);
"""


def money(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(',', '.'))
        if not number.is_finite() or number < 0 or number > Decimal('90000000000000'):
            return None
        return int((number * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def rate(value):
    if value is None or value == '':
        return None
    parsed = money(value)
    if parsed is None or parsed > 10000:
        raise ValueError('Процент должен быть от 0 до 100')
    return parsed


def normalize_time(value):
    try:
        if isinstance(value, (int, float)):
            stamp = datetime.fromtimestamp(value, timezone.utc)
        else:
            stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('Timezone missing')
        return stamp.astimezone(timezone.utc).isoformat(timespec='seconds'), False
    except (ValueError, TypeError, OverflowError, OSError):
        return datetime.now(timezone.utc).isoformat(timespec='seconds'), True


class Finance:
    def __init__(self, storage):
        self.storage = storage

    def record(self, platform, order_id, quantity=1, *, gross=None, currency='RUB',
               occurred_at=None, status='paid'):
        stamp, observed = normalize_time(occurred_at)
        fee = rate(os.environ.get(f'SHOP_{platform.upper()}_FEE', '0'))
        withdrawal = rate(os.environ.get(f'SHOP_{platform.upper()}_WITHDRAWAL', '6' if platform == 'playerok' else '3'))
        with self.storage._transaction() as db:
            db.execute('''INSERT INTO revenue VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(platform,order_id) DO UPDATE SET
                gross=COALESCE(revenue.gross,excluded.gross),
                currency=CASE WHEN revenue.gross IS NULL AND excluded.gross IS NOT NULL THEN excluded.currency ELSE revenue.currency END,
                fee_bps=COALESCE(revenue.fee_bps,excluded.fee_bps),
                withdrawal_bps=COALESCE(revenue.withdrawal_bps,excluded.withdrawal_bps),
                quantity=MAX(revenue.quantity,excluded.quantity), status=excluded.status,
                occurred_at=CASE WHEN revenue.observed_date=1 AND excluded.observed_date=0 THEN excluded.occurred_at ELSE revenue.occurred_at END,
                observed_date=MIN(revenue.observed_date,excluded.observed_date)
            ''', (platform, str(order_id), stamp, int(observed), quantity, money(gross),
                  str(currency).upper(), fee, withdrawal, status))

    def expense(self, key, rub):
        amount = money(rub)
        if amount is not None:
            with self.storage._transaction() as db:
                db.execute('INSERT OR IGNORE INTO expenses VALUES (?,?,?)',
                           (key, normalize_time(None)[0], amount))

    def report(self, day: date | None = None):
        def included(stamp):
            return day is None or datetime.fromisoformat(stamp).astimezone(MSK).date() == day
        result = dict(orders=0, accounts=0, gross=0, net=0, payout=0, refunds=0,
                      unknown=0, fee_unknown=0, withdrawal_unknown=0, observed=0,
                      pending=0, confirmed=0, promotion=0, uncertain_bumps=0,
                      playerok=0, funpay=0, playerok_gross=0, funpay_gross=0,
                      status_unknown=0, priced_orders=0)
        with self.storage._lock:
            db = self.storage._connection
            for row in db.execute('SELECT * FROM revenue'):
                if not included(row['occurred_at']):
                    continue
                result['orders'] += 1
                result['accounts'] += row['quantity']
                result[row['platform']] += row['quantity']
                result['observed'] += row['observed_date']
                status = row['status'].lower()
                refunded = status in {'refunded', 'rolled_back', 'cancelled'}
                partial = status == 'partially_refunded'
                result['refunds'] += int(refunded or partial)
                status_key = ('confirmed' if status in {'closed', 'confirmed', 'confirmed_automatically'}
                              else 'pending' if status in {'paid', 'pending', 'sent'} else 'status_unknown')
                result[status_key] += int(not refunded and not partial)
                if refunded:
                    continue
                if row['gross'] is None or row['currency'] != 'RUB' or partial:
                    result['unknown'] += 1
                    continue
                gross = row['gross']
                result['priced_orders'] += 1
                result['gross'] += gross
                result[row['platform'] + '_gross'] += gross
                if row['fee_bps'] is None:
                    result['fee_unknown'] += 1
                    continue
                fee = int((Decimal(gross) * row['fee_bps'] / 10000).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
                net = gross - fee
                result['net'] += net
                if row['withdrawal_bps'] is None:
                    result['withdrawal_unknown'] += 1
                    continue
                result['payout'] += net - int((Decimal(net) * row['withdrawal_bps'] / 10000).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            for row in db.execute('SELECT * FROM expenses'):
                if included(row['occurred_at']):
                    result['promotion'] += row['amount']
            for row in db.execute('SELECT * FROM bumps'):
                if included(datetime.fromtimestamp(row['attempted_at'], timezone.utc).isoformat()):
                    if row['status'] == 'success':
                        result['promotion'] += row['price_rub'] * 100
                    elif row['status'] in {'pending', 'uncertain'}:
                        result['uncertain_bumps'] += 1
        return result


def render_report(report):
    rub = lambda n: f'{Decimal(n) / 100:.2f} ₽'
    complete = not (report['unknown'] or report['fee_unknown'])
    payout_complete = complete and not report['withdrawal_unknown']
    return (
        f"Заказов: {report['orders']} · аккаунтов: {report['accounts']}\n"
        f"Playerok: {report['playerok']} · FunPay: {report['funpay']}\n"
        f"Подтверждено: {report['confirmed']} · ожидают: {report['pending']}\n"
        f"Статус пока неизвестен: {report['status_unknown']}\n"
        f"Заказов с возвратом: {report['refunds']}\n\n"
        f"Сумма продаж по известным данным: {rub(report['gross'])}\n"
        f"Из них Playerok: {rub(report['playerok_gross'])}; FunPay: {rub(report['funpay_gross'])}\n"
        f"После комиссии продажи (оценка): {rub(report['net']) if complete else 'неполные данные'}\n"
        f"После вывода (оценка): {rub(report['payout']) if payout_complete else 'неполные данные'}\n"
        f"Комиссия вывода (оценка): {rub(report['net'] - report['payout']) if payout_complete else 'неполные данные'}\n"
        f"Учтённые публикации и поднятия: {rub(report['promotion'])}\n"
        f"После комиссии продажи и продвижения: {rub(report['net'] - report['promotion']) if complete else 'неполные данные'}\n"
        f"Средняя сумма заказа: {rub(report['gross'] // report['priced_orders']) if report['priced_orders'] else 'нет данных'}\n\n"
        f"Заказов без известной суммы RUB: {report['unknown']}\n"
        f"Без ставки продажи / вывода: {report['fee_unknown']} / {report['withdrawal_unknown']}\n"
        f"С датой обнаружения вместо даты покупки: {report['observed']}\n"
        f"Поднятий с неизвестным списанием: {report['uncertain_bumps']}\n"
        "Дни — по МСК. Это расчёт поступлений, без себестоимости аккаунтов. "
        "Частичные возвраты исключены из денежных итогов до сверки."
    )

from datetime import date, datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..finance import Finance, MSK, render_report
from ..inventory import Inventory


def report_page(profiles, user_id, group_id, day=None):
    groups = profiles.groups(user_id) if group_id == 0 else [profiles.get(user_id, group_id)]
    totals = None
    stock_totals = dict(free=0, playerok_reserve=0, funpay_listed=0, funpay_unknown=0)
    listed = 0
    for group in groups:
        storage = profiles.storage(user_id, group.group_id)
        try:
            part = Finance(storage).report(day)
            stock = Inventory(storage).stats()
            listed += storage.counts()['listed']
            for key in stock_totals:
                stock_totals[key] += stock[key]
        finally:
            storage.close()
        if totals is None:
            totals = part
        else:
            for key in totals:
                totals[key] += part[key]
    title = 'Все группы' if group_id == 0 else f'Группа «{groups[0].group_name}»'
    period = day.strftime('%d.%m.%Y') if day else 'За всё время'
    text = f'{title}\n{period}\n\n' + render_report(totals)
    text += (f"\n\nСклад сейчас: свободно {stock_totals['free']}, резерв Playerok {stock_totals['playerok_reserve']}, "
             f"выставлено Playerok {listed}, FunPay {stock_totals['funpay_listed']}, карантин {stock_totals['funpay_unknown']}.")
    today = datetime.now(MSK).date()
    def button(label, target):
        return InlineKeyboardButton(text=label, callback_data=f'report:{user_id}:{group_id}:{target}')
    rows = []
    if day:
        nav = []
        if day > date(2000, 1, 1):
            nav.append(button('← День', (day - timedelta(days=1)).isoformat()))
        nav.append(button('Сегодня', today.isoformat()))
        if day < today:
            nav.append(button('День →', (day + timedelta(days=1)).isoformat()))
        rows.append(nav)
        rows.append([button('За всё время', 'all')])
    else:
        rows.append([button('По дням', today.isoformat())])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)

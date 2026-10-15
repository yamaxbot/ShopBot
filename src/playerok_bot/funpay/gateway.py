"""Небольшой адаптер веб-API FunPay; цены и описания сохраняются из свежей формы.

Протокол сверён с FunPayCardinal/FunPayAPI и DKeken/funpay-mcp.
Запросы изменения НЕ повторяются автоматически при сетевой ошибке.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit, urljoin

import requests
from bs4 import BeautifulSoup

from ..accounts import parse_accounts_text
from ..errors import ConfigurationError, BotError
from ..models import Credential


class FunPayError(BotError):
    pass


class FunPayRejected(FunPayError):
    """Сервер явно отклонил форму; повтор разрешён только с новой формой."""


@dataclass(frozen=True)
class RaiseOutcome:
    raised: bool
    wait_seconds: int | None


def parse_lots(text: str) -> list[str]:
    result = []
    for value in re.split(r'[\s,]+', text.strip()):
        if not value:
            continue
        if value.isascii() and value.isdigit():
            lot = value
        else:
            url = urlsplit(value)
            if url.scheme != 'https' or url.netloc != 'funpay.com' or url.path not in {'/lots/offer', '/lots/offerEdit'}:
                raise ConfigurationError('Нужны ID или ссылки https://funpay.com/lots/offer?id=123')
            params = parse_qs(url.query)
            values = params.get('id', params.get('offer', []))
            if len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
                raise ConfigurationError('В ссылке FunPay отсутствует числовой ID лота')
            lot = values[0]
        number = int(lot)
        if number <= 0 or number >= 2**63:
            raise ConfigurationError('Некорректный ID FunPay-лота')
        result.append(str(number))
    if not 1 <= len(result) <= 8 or len(set(result)) != len(result):
        raise ConfigurationError('Укажите от 1 до 8 разных FunPay-лотов')
    return result


def format_secrets(credentials: list[Credential]) -> str:
    """Один аккаунт FunPay — одна строка встроенной автовыдачи."""
    return '\n'.join(f'Логин: {c.login} Пароль: {c.password}' for c in credentials)


@dataclass(repr=False)
class Lot:
    id: str
    credentials: list[Credential]
    active: bool
    fields: dict = field(repr=False)
    node_id: str = ''

    @property
    def fingerprints(self):
        return [credential.fingerprint for credential in self.credentials]

    @property
    def delivery_formatted(self):
        raw = self.fields.get('secrets')
        return raw is None or raw.strip() == format_secrets(self.credentials)


class FunPayGateway:
    def __init__(self, golden_key: str, user_agent: str):
        self.session = requests.Session()
        self.session.cookies.set('golden_key', golden_key, domain='funpay.com', path='/')
        self.session.cookies.set('cookie_prefs', '1', domain='funpay.com', path='/')
        self.session.headers.update({'User-Agent': user_agent, 'Accept-Language': 'ru'})
        self.id = None
        self.username = None
        self.csrf = None
        self._node_games: dict[str, str] = {}

    def close(self):
        self.session.close()

    def request(self, method, path, **kwargs):
        url = 'https://funpay.com/' + path
        for _ in range(4):
            try:
                response = self.session.request(method, url, timeout=(10, 30),
                                                allow_redirects=False, **kwargs)
            except requests.RequestException:
                raise FunPayError('FunPay не ответил. Результат изменения может быть неизвестен') from None
            if method != 'GET' or response.status_code not in {301, 302, 303, 307, 308}:
                break
            url = urljoin(url, response.headers.get('Location', ''))
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.netloc != 'funpay.com' or parsed.path.endswith('/account/login'):
                raise FunPayError('FunPay перенаправил запрос на вход; обновите golden_key')
            kwargs = {}
        if response.status_code != 200:
            raise FunPayError(f'FunPay вернул HTTP {response.status_code}; проверьте авторизацию')
        return response

    def soup(self, response):
        return BeautifulSoup(response.content, 'html.parser', from_encoding='utf-8')

    def identity_from(self, page):
        body = page.find('body')
        try:
            data = json.loads(body.get('data-app-data', '{}')) if body else {}
            uid = str(int(data.get('userId', 0)))
        except (ValueError, TypeError, json.JSONDecodeError):
            raise FunPayError('Не удалось прочитать профиль FunPay') from None
        if uid == '0' or not page.select_one('.user-link-name'):
            raise FunPayError('FunPay не авторизован; обновите golden_key через /fp_connect')
        if self.id and uid != self.id:
            raise FunPayError('Во время запроса изменился владелец FunPay')
        self.id = uid
        self.username = page.select_one('.user-link-name').get_text(strip=True)
        self.csrf = data.get('csrf-token') or self.csrf

    def connect(self):
        page = self.soup(self.request('GET', '?setlocale=ru'))
        self.identity_from(page)
        self._node_games.clear()
        for game in page.select('.game-title[data-id]'):
            game_id = game.get('data-id', '')
            if not re.fullmatch(r'[1-9][0-9]*', game_id):
                continue
            for group in game.parent.select('ul[data-id]'):
                if group.get('data-id') != game_id:
                    continue
                for link in group.select('a[href]'):
                    parsed = urlsplit(urljoin('https://funpay.com/', link['href']))
                    match = re.fullmatch(r'/lots/([1-9][0-9]*)/', parsed.path)
                    if parsed.scheme == 'https' and parsed.netloc == 'funpay.com' and match:
                        self._node_games[match[1]] = game_id
        if not self.csrf:
            raise FunPayError('FunPay не вернул CSRF токен')
        return self

    def game_id_for(self, node_id: str) -> str:
        game_id = self._node_games.get(str(node_id))
        if not game_id:
            raise FunPayError(f'Категория FunPay для подкатегории {node_id} не найдена; автоподнятие недоступно')
        return game_id

    def raise_lots(self, node_id: str) -> RaiseOutcome:
        """Один POST поднимает предложения продавца в указанной подкатегории."""
        game_id = self.game_id_for(node_id)
        response = self.request('POST', 'lots/raise', data={
            'game_id': game_id, 'node_id': node_id, 'node_ids[]': [node_id],
        }, headers={
            'Accept': '*/*', 'X-Requested-With': 'XMLHttpRequest',
            'X-CSRF-Token': self.csrf, 'Referer': f'https://funpay.com/lots/{node_id}/',
        })
        try:
            result = response.json()
        except ValueError:
            raise FunPayError('Неизвестный ответ FunPay на поднятие предложений') from None
        if not isinstance(result, dict):
            raise FunPayError('Неизвестный ответ FunPay на поднятие предложений')
        raw_wait = result.get('wait')
        if raw_wait is None or raw_wait == '':
            wait = None
        else:
            try:
                seconds = float(raw_wait)
                if isinstance(raw_wait, bool) or not math.isfinite(seconds) or seconds < 0:
                    raise ValueError
                wait = math.ceil(seconds)
            except (ValueError, TypeError, OverflowError):
                raise FunPayError('FunPay вернул некорректное время до следующего поднятия') from None
        if result.get('url') or result.get('html'):
            raise FunPayError('FunPay запросил дополнительное действие для поднятия; проверьте сайт')
        if result.get('error'):
            message = str(result.get('msg') or '')
            if wait is not None or any(word in message for word in ('Подождите', 'Please wait', 'Зачекайте')):
                return RaiseOutcome(False, wait)
            raise FunPayError('FunPay отклонил поднятие предложений; проверьте сайт')
        return RaiseOutcome(True, wait)

    def read(self, lot_id: str) -> Lot:
        page = self.soup(self.request('GET', 'lots/offerEdit', params={'offer': lot_id}))
        self.identity_from(page)
        form = page.select_one('form.form-offer-editor')
        if form is None:
            raise FunPayError(f'Лот FunPay {lot_id} недоступен для редактирования вашим профилем')
        fields = {}
        for element in form.select('input[name], textarea[name], select[name]'):
            if element.has_attr('disabled'):
                continue
            name = element['name']
            if element.name == 'input':
                kind = element.get('type', 'text')
                if kind in {'submit', 'button', 'file', 'reset'}:
                    continue
                if kind in {'checkbox', 'radio'} and not element.has_attr('checked'):
                    continue
                value = element.get('value', 'on' if kind == 'checkbox' else '')
            elif element.name == 'textarea':
                value = element.get_text()
            else:
                options = element.select('option[selected]') or element.select('option')[:1]
                value = [option.get('value', option.get_text()) for option in options]
                if not element.has_attr('multiple'):
                    value = value[0] if value else ''
            if name in fields:
                old = fields[name]
                fields[name] = (old if isinstance(old, list) else [old]) + (value if isinstance(value, list) else [value])
            else:
                fields[name] = value
        if str(fields.get('offer_id')) != lot_id:
            raise FunPayError('ID в форме FunPay не совпадает с запрошенным лотом')
        if not fields.get('auto_delivery') or 'secrets' not in fields:
            raise FunPayError(f'В лоте FunPay {lot_id} включите встроенную автовыдачу')
        if not fields.get('csrf_token') or not fields.get('form_created_at'):
            raise FunPayError('Форма FunPay изменилась: отсутствуют токены свежей формы. Изменения остановлены')
        raw = fields['secrets'].strip()
        credentials = parse_accounts_text(raw) if raw else []
        try:
            data = json.loads(form.get('data-offer', '{}'))
            amount = data['amount']
            if int(amount) != len(credentials):
                raise FunPayError('Число товаров FunPay отличается от доступного списка; изменения остановлены')
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            raise FunPayError('Не удалось проверить остаток автовыдачи FunPay') from None
        return Lot(lot_id, credentials, bool(fields.get('active')) and bool(credentials), fields,
                   str(fields.get('node_id', '')))

    def save(self, fresh: Lot, credentials: list[Credential], active: bool):
        fields = dict(fresh.fields)
        fields['secrets'] = format_secrets(credentials)
        fields['active'] = 'on' if active else ''
        fields['auto_delivery'] = 'on'
        # Для авто-выдачи количество определяется secrets, старое ручное amount не отправляем.
        fields.pop('amount', None)
        # Сохраняем csrf_token и form_created_at именно этой формы, а не прошлой.
        response = self.request('POST', 'lots/offerSave', data=fields, headers={
            'X-Requested-With': 'XMLHttpRequest', 'X-CSRF-Token': self.csrf,
            'Referer': f'https://funpay.com/lots/offerEdit?offer={fresh.id}',
        })
        try:
            result = response.json()
        except ValueError:
            raise FunPayError('Неизвестный ответ при сохранении FunPay-лота') from None
        if not isinstance(result, dict):
            raise FunPayError('Неизвестный ответ при сохранении FunPay-лота')
        if result.get('error') or result.get('errors'):
            # Ошибка сервера может содержать секреты из формы; не пересылать её.
            raise FunPayRejected('FunPay отклонил сохранение лота')

    def sales_page(self, cursor=None):
        response = self.request('POST' if cursor else 'GET', 'orders/trade',
                                **({'data': {'continue': cursor}} if cursor else {}))
        page = self.soup(response)
        if not cursor:
            self.identity_from(page)
        ids = []
        for element in page.select('a.tc-item .tc-order'):
            value = element.get_text(strip=True).lstrip('#')
            if re.fullmatch(r'[A-Za-z0-9]+', value):
                ids.append(value)
        more = page.select_one('input[name="continue"]')
        return ids, more.get('value') if more else None

    def orders(self, order_ids):
        if not 1 <= len(order_ids) <= 10:
            raise ValueError('FunPay принимает от 1 до 10 заказов в одном запросе')
        response = self.request('POST', 'api/orders/get', json={
            'order_uids': order_ids, 'include': ['details', 'users'],
        })
        try:
            data = response.json()
            orders = {order_id: data['data'][order_id] for order_id in order_ids}
            if data['status'] != 'SUCCESS' or any(str(order['seller']['user_id']) != self.id for order in orders.values()):
                raise ValueError
            return orders
        except (KeyError, TypeError, ValueError):
            raise FunPayError('Не удалось прочитать собственную продажу FunPay') from None

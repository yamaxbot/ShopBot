from __future__ import annotations

import json
import logging
import time

from ..accounts import parse_accounts_text
from ..errors import BotError
from ..inventory import Inventory
from ..storage import _utc_now
from .gateway import FunPayError, FunPayRejected

logger = logging.getLogger(__name__)


class FunPayService:
    TARGET = 5

    def __init__(self, storage, gateway, lots: list[str]):
        self.inventory = Inventory(storage)
        self.gateway = gateway
        self.lots = lots
        self.node_id = None

    def check(self):
        if self.gateway.id is None:
            self.gateway.connect()
        snapshots = [self.gateway.read(lot) for lot in self.lots]
        if self.inventory.storage.ownership:
            self.inventory.storage.ownership.claim_lots('funpay', self.lots)
            self.inventory.storage.ownership.claim_accounts([c for lot in snapshots for c in lot.credentials])
        if len({lot.node_id for lot in snapshots}) != 1:
            raise FunPayError('Для общего запаса FunPay укажите лоты одной категории с одинаковым товаром')
        self.node_id = snapshots[0].node_id
        # Отличающиеся тип, сервер и другие параметры товара смешивать нельзя.
        variants = []
        for lot in snapshots:
            variants.append({key: value for key, value in lot.fields.items() if key.startswith('fields[')
                             and not key.startswith(('fields[summary]', 'fields[desc]', 'fields[payment_msg]', 'fields[images]'))})
        if any(variant != variants[0] for variant in variants[1:]):
            raise FunPayError('FunPay-лоты должны продавать одинаковые аккаунты; параметры товара отличаются')
        seen = set()
        for lot in snapshots:
            logins = {c.login.casefold() for c in lot.credentials}
            if len(logins) != len(lot.credentials) or seen.intersection(logins):
                raise FunPayError('Один аккаунт одновременно находится в нескольких FunPay-лотах')
            seen.update(logins)
        return snapshots

    def raise_when_available(self) -> bool:
        """Поднимать лоты после пополнения, соблюдая таймер ответа FunPay."""
        if not self.node_id:
            raise FunPayError('Сначала проверьте FunPay-лоты')
        listed = self.inventory.stats()['lots']
        if not any(lot['lot_id'] in self.lots and lot['active'] and lot['amount'] > 0 for lot in listed):
            return False
        storage = self.inventory.storage
        key = f'fp_raise_due:{self.node_id}'
        now = int(time.time())
        if storage.initialize_state_int(key, 0) > now:
            return False
        # Запись до POST переживает потерянный ответ или остановку процесса.
        storage.set_state_int(key, now + 600)
        outcome = self.gateway.raise_lots(self.node_id)
        delay = max(30, outcome.wait_seconds or 60)
        storage.set_state_int(key, int(time.time()) + delay)
        if outcome.raised:
            logger.info('Подняты предложения FunPay в подкатегории %s; следующая попытка через %s с', self.node_id, delay)
        else:
            logger.info('FunPay пока не разрешает поднятие подкатегории %s; следующая попытка через %s с', self.node_id, delay)
        return outcome.raised

    def _observe(self, lot):
        self.inventory.snapshot(lot.id, lot.credentials, lot.active)
        return lot

    def _finish(self, operation, actual):
        after = json.loads(operation['after_json'])
        kind = operation['kind']
        if actual.fingerprints != after or actual.active != bool(operation['active']):
            # Во время проверки успешно активированного лота товар мог быть выдан.
            missing = set(after) - set(actual.fingerprints)
            if (kind not in {'activate', 'refill', 'freeze'} or not missing
                    or any(f not in after for f in actual.fingerprints)
                    or not self.inventory.confirmed(missing)
                    or (actual.credentials and actual.active != bool(operation['active']))):
                return False
        if kind == 'format' and not actual.delivery_formatted:
            return False
        if kind.startswith('move:'):
            removed = [f for f in json.loads(operation['before_json']) if f not in after]
            self.inventory.transfer(operation['lot_id'], kind.split(':', 1)[1], removed)
        self._observe(actual)
        if kind == 'withdraw':
            if actual.active or actual.credentials:
                return False
            with self.inventory.storage._transaction() as db:
                for fingerprint in json.loads(operation['before_json']):
                    db.execute("DELETE FROM allocations WHERE platform='funpay' AND lot_id=? AND account_id IN (SELECT id FROM accounts WHERE fingerprint=? AND status='queued')", (operation['lot_id'], fingerprint))
        self.inventory.mark_edit(operation['id'], 'done')
        return True

    def recover(self):
        blocked = set()
        for operation in self.inventory.pending_edits():
            # prepared значит: отметка sent ещё не сохранена, запрос не начинался.
            if operation['status'] == 'prepared':
                self.inventory.mark_edit(operation['id'], 'done')
                continue
            try:
                actual = self.gateway.read(operation['lot_id'])
                if self._finish(operation, actual):
                    continue
            except Exception as exc:
                logger.warning('Сверка изменения FunPay %s: %s', operation['id'], type(exc).__name__)
            blocked.add(operation['lot_id'])
            if operation['kind'].startswith('move:'):
                blocked.add(operation['kind'].split(':', 1)[1])
        return blocked

    def edit(self, current, desired, active, kind):
        active = bool(active and desired)
        after = [c.fingerprint for c in desired]
        if current.fingerprints == after and current.active == active and (kind != 'format' or current.delivery_formatted):
            return self._observe(current)
        edit_id = self.inventory.prepare_edit(current.id, kind, current.fingerprints, after, active)
        self.inventory.mark_edit(edit_id, 'sent')
        try:
            self.gateway.save(current, desired, active)
        except FunPayRejected:
            self.inventory.mark_edit(edit_id, 'done')
            raise
        except Exception as exc:
            logger.warning('Ответ сохранения FunPay %s неизвестен: %s', edit_id, type(exc).__name__)
        operation = next(op for op in self.inventory.pending_edits() if op['id'] == edit_id)
        try:
            actual = self.gateway.read(current.id)
            if self._finish(operation, actual):
                return actual
        except Exception as exc:
            logger.warning('Проверка сохранения FunPay %s: %s', edit_id, type(exc).__name__)
        self.inventory.mark_edit(edit_id, 'uncertain')
        raise FunPayError(f'Изменение FunPay №{edit_id} требует сверки. Повторная отправка аккаунтов заблокирована')

    def freeze(self, lot):
        lot = self.edit(lot, lot.credentials, False, 'freeze')
        # Новая форма после деактивации; старый список больше не используется.
        return self._observe(self.gateway.read(lot.id))

    def activate(self, lot):
        return self.edit(lot, lot.credentials, bool(lot.credentials), 'activate')

    def refill(self, lot_id):
        lot = self._observe(self.gateway.read(lot_id))
        staged = self.inventory.accounts(lot_id)
        if lot.credentials and not lot.delivery_formatted:
            # Уже загруженные аккаунты тоже переводим на одну строку с подписями.
            lot = self.freeze(lot)
            if not lot.delivery_formatted:
                lot = self.edit(lot, lot.credentials, False, 'format')
        if len(lot.credentials) >= self.TARGET and not staged:
            # Восстановление после падения между загрузкой и активацией.
            if not lot.active:
                self.activate(lot)
            return
        if not staged and not self.inventory.stats()['free']:
            # Склад пуст: не трогаем действующий остаток и ждём новых аккаунтов.
            if not lot.active and lot.credentials:
                self.activate(lot)
            return
        lot = self.freeze(lot)
        if lot.active:
            raise FunPayError('FunPay-лот остался активным; пополнение отменено')
        staged = self.inventory.accounts(lot_id)
        need = max(0, self.TARGET - len(lot.credentials) - len(staged))
        staged += self.inventory.reserve(lot_id, need)
        desired = lot.credentials + [a.credential for a in staged]
        lot = self.edit(lot, desired, False, 'refill')
        self.activate(lot)

    def sync_sales(self):
        # Сохраняем курсор: заказы, накопившиеся за время остановки, не теряются.
        storage = self.inventory.storage
        with storage._lock:
            state = storage._connection.execute("SELECT value FROM bot_state WHERE key='fp_sales_cursor'").fetchone()
        cursor = state[0] if state and state[0] else None
        ids, more = self.gateway.sales_page()
        known = self._sync_orders(ids)
        if cursor:
            ids, more = self.gateway.sales_page(cursor)
            known = self._sync_orders(ids)
        next_cursor = more if more and not known else ''
        with storage._transaction() as db:
            db.execute("INSERT INTO bot_state VALUES ('fp_sales_cursor',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                       (next_cursor, _utc_now()))
        # Закрытый заказ может получить возврат позднее и уже уйти с первой страницы.
        now = int(time.time())
        if storage.initialize_state_int('fp_finance_due', 0) <= now:
            storage.set_state_int('fp_finance_due', now + 300)
            offset = storage.initialize_state_int('fp_finance_offset', 0)
            with storage._lock:
                rows = storage._connection.execute("SELECT order_id FROM revenue WHERE platform='funpay' ORDER BY order_id LIMIT 5 OFFSET ?", (offset,)).fetchall()
            if rows:
                self._sync_orders([row[0] for row in rows])
            storage.set_state_int('fp_finance_offset', offset + len(rows) if len(rows) == 5 else 0)

    def _sync_orders(self, ids):
        known = all(self.inventory.order_seen(order_id) is True for order_id in ids)
        pending = ids
        for index in range(0, len(pending), 10):
            batch = pending[index:index + 10]
            orders = self.gateway.orders(batch)
            for order_id in batch:
                order = orders[order_id]
                secrets = order.get('type_data', {}).get('secrets', [])
                parsed_count = 0
                all_parsed = bool(secrets)
                for secret in secrets:
                    try:
                        credentials = parse_accounts_text(secret['value'])
                    except (KeyError, TypeError, BotError):
                        all_parsed = False
                        continue
                    parsed_count += len(credentials)
                    for credential in credentials:
                        self.inventory.confirm_sale(order_id, credential)
                from ..finance import Finance
                with self.inventory.storage._lock:
                    count = self.inventory.storage._connection.execute('SELECT COUNT(*) FROM funpay_sales WHERE order_id=?', (order_id,)).fetchone()[0]
                if count:
                    # Сумма заказа учитывается один раз, даже если выдано несколько аккаунтов.
                    Finance(self.inventory.storage).record('funpay', order_id, count,
                        gross=order.get('amount') if all_parsed and parsed_count == count else None,
                        currency=order.get('currency', 'UNKNOWN'),
                        occurred_at=order.get('created_at'), status=order.get('status', 'unknown'))
                # Выданный аккаунт никогда не возвращается в продажу, даже при возврате.
                terminal = order.get('status') in {'closed', 'refunded', 'partially_refunded'}
                self.inventory.mark_order(order_id, terminal)
        return known

    def reconcile_once(self):
        blocked = self.recover()
        try:
            self.sync_sales()
        except Exception as exc:
            logger.warning('Сверка продаж FunPay временно не выполнена: %s', type(exc).__name__)
        # Сначала наблюдать ВСЕ лоты: их существующий товар не может попасть в очередь.
        snapshots = self.check()
        for lot in snapshots:
            if lot.id not in blocked:
                self._observe(lot)
        order = [lot.id for lot in sorted(snapshots, key=lambda lot: (len(lot.credentials), lot.id))]
        for lot_id in order:
            if lot_id in blocked:
                continue
            try:
                self.refill(lot_id)
            except Exception as exc:
                logger.warning('Пополнение FunPay-лота %s: %s', lot_id, type(exc).__name__)
                blocked.update(self.recover())

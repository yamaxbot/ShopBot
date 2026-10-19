"""Withdraw a group's stock only after the seller manually disables its listings."""
import json
import os
import hashlib
from pathlib import Path

from .errors import ConfigurationError
from .inventory import Inventory
from .models import Credential


def state(storage):
    with storage._lock:
        row = storage._connection.execute("SELECT value FROM bot_state WHERE key='export_state'").fetchone()
        return row[0] if row else ''


def assert_idle(storage):
    if state(storage) in {'preparing','ready','delivered'}:
        raise ConfigurationError('Идёт выгрузка склада. Завершите её через /export_confirm.')


def check_local(storage):
    with storage._lock:
        db = storage._connection
        if db.execute('SELECT 1 FROM deals WHERE completed_at IS NULL').fetchone() or db.execute('SELECT 1 FROM restocks WHERE completed_at IS NULL').fetchone():
            raise ConfigurationError('Есть незавершённые продажи или пополнения. Сначала завершите сверку.')
        if Inventory(storage).pending_edits():
            raise ConfigurationError('Есть незавершённые изменения FunPay: /fp_errors. Сначала завершите сверку.')


def prepare_remote(storage, playerok_gateways, funpay_service=None):
    """No publication or delivery. Uncertain states fail closed; retries read durable journal."""
    if state(storage) == 'ready':
        return
    # Recover ONLY withdrawal journal operations, never a refill/activation.
    if funpay_service:
        for op in Inventory(storage).pending_edits():
            if op['kind'] != 'withdraw':
                raise ConfigurationError('Есть незавершённое изменение FunPay. Сначала выполните сверку.')
            if op['status'] == 'prepared':
                Inventory(storage).mark_edit(op['id'], 'done')
                continue
            actual = funpay_service.gateway.read(op['lot_id'])
            if not funpay_service._finish(op, actual):
                raise ConfigurationError('Снятие товара FunPay не подтверждено. Проверьте лот и повторите /export_confirm.')
    check_local(storage)
    from playerokapi.enums import ItemStatuses
    verified = set()
    for gateway in playerok_gateways:
        item = gateway.resolve_listing()
        if str(item.user.id) != str(gateway.account.id):
            raise ConfigurationError('Playerok: лот принадлежит другому продавцу.')
        listed = storage.get_listed_account(gateway.item_id)
        if item.status is ItemStatuses.SOLD and listed is None:
            credential = gateway.current_credential(item)
            with storage._lock:
                known_sale = credential and storage._connection.execute(
                    "SELECT 1 FROM accounts WHERE fingerprint=? AND status='sold'",
                    (credential.fingerprint,),
                ).fetchone()
            if known_sale:
                continue
            raise ConfigurationError('Продажа Playerok ещё не подтверждена в базе. Сначала выполните сверку; выгрузка остановлена.')
        if item.status not in {ItemStatuses.DRAFT, ItemStatuses.EXPIRED, ItemStatuses.DECLINED, ItemStatuses.BLOCKED}:
            raise ConfigurationError('Снимите объявления Playerok с продажи вручную. Проданные и ожидающие модерации лоты выгружать нельзя.')
        if getattr(item, 'keep_in_sale', False):
            raise ConfigurationError('Отключите «Оставлять в продаже» на Playerok.')
        if gateway.recent_paid_deals():
            raise ConfigurationError('В лоте Playerok есть незакрытые покупки. Сначала завершите сделки.')
        credential = gateway.current_credential(item)
        if listed and (not credential or listed.fingerprint != credential.fingerprint):
            raise ConfigurationError('Данные Playerok отличаются от базы. Сначала выполните сверку.')
        if credential:
            storage.import_accounts([credential])
            with storage._lock:
                sold = storage._connection.execute("SELECT 1 FROM accounts WHERE fingerprint=? AND status='sold'", (credential.fingerprint,)).fetchone()
            if sold:
                raise ConfigurationError('В неактивном лоте остался проданный аккаунт. Уберите такой лот из настроек.')
        verified.add(gateway.item_id)
    with storage._lock:
        listed_ids = {r[0] for r in storage._connection.execute("SELECT listed_item_id FROM accounts WHERE status='listed'")}
    if listed_ids - verified:
        raise ConfigurationError('В базе есть выставленный Playerok-лот, которого нет в настройках. Верните его ссылку и снимите с продажи.')
    if funpay_service:
        snapshots = funpay_service.check()
        if any(lot.active or lot.fields.get('active') for lot in snapshots):
            raise ConfigurationError('Снимите все лоты FunPay выбранной группы с продажи вручную.')
        for lot in snapshots:
            funpay_service._observe(lot)
        funpay_service.sync_sales()
        if Inventory(storage).stats()['funpay_unknown']:
            raise ConfigurationError('Есть аккаунты в карантине. Выгрузка остановлена до сверки покупок.')
        for lot in snapshots:
            # Fresh form: never submit stale credentials after a concurrent sale/edit.
            fresh = funpay_service.gateway.read(lot.id)
            if fresh.active or fresh.fields.get('active') or fresh.fingerprints != lot.fingerprints:
                raise ConfigurationError('FunPay-лот изменился во время выгрузки. Проверьте его и повторите.')
            funpay_service.edit(fresh, [], False, 'withdraw')
            with storage._transaction() as db:
                db.execute("DELETE FROM allocations WHERE platform='funpay' AND lot_id=? AND state='staged'", (lot.id,))
    with storage._lock:
        if storage._connection.execute("SELECT 1 FROM allocations WHERE platform='funpay' AND state!='sold'").fetchone():
            raise ConfigurationError('Не все резервы FunPay сняты. Проверьте настройки лотов и /fp_errors.')
    storage.set_state_text('export_state', 'ready')


def payload(storage):
    if state(storage) != 'ready':
        raise ConfigurationError('Склад ещё не подготовлен к выгрузке.')
    with storage._lock:
        rows = storage._connection.execute("SELECT login,password FROM accounts WHERE status!='sold' ORDER BY id").fetchall()
    return ('\n'.join(f"{r['login']}:{r['password']}" for r in rows) + ('\n' if rows else '')).encode('utf-8'), len(rows)


def verify_source_file(storage, path):
    row = storage._connection.execute("SELECT value FROM bot_state WHERE key='export_source_hash'").fetchone()
    if row and hashlib.sha256(path.read_bytes()).hexdigest() != row[0]:
        raise ConfigurationError('accaunts.txt изменился после подготовки. Сохраните новые строки отдельно и восстановите исходный файл перед повтором выгрузки.')


def finish(storage, file_id):
    # Called only after Telegram acknowledged the document. Preserve sales history.
    with storage._transaction() as db:
        ids = [r[0] for r in db.execute("SELECT id FROM accounts WHERE status!='sold'")]
        fingerprints = [r[0] for r in db.execute("SELECT fingerprint FROM accounts WHERE status!='sold'")]
        db.executemany('INSERT OR IGNORE INTO withdrawn_fingerprints VALUES (?)', ((f,) for f in fingerprints))
        for account_id in ids:
            if db.execute('SELECT 1 FROM deals WHERE sold_account_id=?', (account_id,)).fetchone():
                raise ConfigurationError('Обнаружена связанная продажа: требуется сверка.')
            db.execute('UPDATE deals SET next_account_id=NULL WHERE next_account_id=? AND completed_at IS NOT NULL', (account_id,))
            db.execute('DELETE FROM restocks WHERE account_id=? AND completed_at IS NOT NULL', (account_id,))
            db.execute('DELETE FROM allocations WHERE account_id=?', (account_id,))
            db.execute('DELETE FROM accounts WHERE id=?', (account_id,))
        if file_id:
            db.execute("INSERT OR REPLACE INTO bot_state VALUES ('export_file',?,datetime('now'))", (file_id,))
        db.execute("INSERT OR REPLACE INTO bot_state VALUES ('export_state','delivered',datetime('now'))")


def run_from_environment():
    from .storage import Storage
    from .funpay.gateway import FunPayGateway
    from .funpay.service import FunPayService
    from .app import BotApplication
    from .config import Settings
    storage = Storage(Path(os.environ['DATABASE_PATH']))
    app = fp = None
    try:
        from .accounts import load_accounts
        source = Path(os.environ['ACCOUNTS_FILE'])
        if state(storage) != 'ready':
            raw = source.read_bytes()
            storage.import_accounts(load_accounts(source, allow_empty=True))
            if source.read_bytes() != raw:
                raise ConfigurationError('Файл аккаунтов изменился во время чтения. Повторите выгрузку.')
            storage.set_state_text('export_source_hash', hashlib.sha256(raw).hexdigest())
        gateways = []
        if os.environ.get('PLAYEROK_ITEM'):
            app = BotApplication(Settings.load(Path.cwd()))
            for runtime in app.listings:
                runtime.gateway.connect()
                if str(runtime.gateway.account.id) != os.environ['PLAYEROK_OWNER_ID']:
                    raise ConfigurationError('Playerok: авторизация другого продавца.')
                gateways.append(runtime.gateway)
        lots = json.loads(os.environ.get('FUNPAY_LOTS', '[]'))
        service = None
        if lots:
            fp = FunPayGateway(os.environ['FUNPAY_GOLDEN_KEY'], os.environ['FUNPAY_USER_AGENT']).connect()
            if str(fp.id) != os.environ['FUNPAY_OWNER_ID']:
                raise ConfigurationError('FunPay: авторизация другого продавца.')
            service = FunPayService(storage, fp, lots)
        prepare_remote(storage, gateways, service)
    finally:
        storage.close()
        if app:
            app.close()
        if fp:
            fp.close()

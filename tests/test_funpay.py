import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from playerok_bot.models import Credential
from playerok_bot.storage import Storage, StorageError
from playerok_bot.inventory import Inventory
from playerok_bot.funpay.gateway import FunPayGateway, FunPayError, FunPayRejected, Lot, format_secrets, parse_lots
from playerok_bot.funpay.service import FunPayService
from playerok_bot.errors import ConfigurationError
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.workers import worker_environment


def accounts(n, prefix='stock'):
    return [Credential(f'{prefix}-{i}@example.test', f'p:^&{i}') for i in range(n)]


class Gateway:
    def __init__(self, lots):
        self.id = 'owner'
        self.username = 'seller'
        self.lots = {key: Lot(key, list(values), bool(values), {'price': key}, '1') for key, values in lots.items()}
        self.calls = []
        self.fail = None
        self.sales = {}
        self.on_save = None

    def read(self, lot):
        return deepcopy(self.lots[lot])

    def connect(self):
        return self

    def save(self, fresh, credentials, active):
        self.calls.append((fresh.id, list(credentials), active))
        if self.fail == 'before':
            raise TimeoutError
        if self.fail == 'rejected':
            raise FunPayRejected('Rejected')
        if self.on_save:
            self.on_save(fresh, credentials, active)
        fields = dict(fresh.fields)
        if 'secrets' in fields:
            fields['secrets'] = format_secrets(credentials)
        self.lots[fresh.id] = Lot(fresh.id, list(credentials), bool(active and credentials), fields, fresh.node_id)
        if self.fail == 'after':
            raise TimeoutError

    def sales_page(self, cursor=None):
        return list(self.sales), None

    def orders(self, order_ids):
        return {order_id: self.sales[order_id] for order_id in order_ids}


@pytest.fixture
def storage(tmp_path):
    storage = Storage(tmp_path / 'inventory.sqlite3')
    yield storage
    storage.close()


def test_funpay_refills_to_five_without_touching_playerok_reserve(storage):
    stock = accounts(20)
    storage.import_accounts(stock)
    storage.bootstrap_listing('playerok', stock[0])
    inventory = Inventory(storage)
    inventory.configure(True)
    gateway = Gateway({'1': []})
    service = FunPayService(storage, gateway, ['1'])
    service.reconcile_once()
    assert len(gateway.lots['1'].credentials) == 5
    assert gateway.lots['1'].active
    assert inventory.stats()['playerok_reserve'] == 5
    assert inventory.stats()['free'] == 9
    issued = set(c.fingerprint for c in gateway.lots['1'].credentials)
    deal = storage.prepare_deal('sale', 'playerok', None)
    assert storage.get_account(deal.next_account_id).fingerprint not in issued
    assert inventory.stats()['playerok_reserve'] == 5


def test_partial_funpay_lot_refills_to_five_after_each_sale(storage):
    existing = accounts(2, 'remote')
    storage.import_accounts(accounts(10))
    gateway = Gateway({'1': existing})
    service = FunPayService(storage, gateway, ['1'])
    service.reconcile_once()
    assert len(gateway.lots['1'].credentials) == 5
    gateway.lots['1'].credentials.pop()
    service.reconcile_once()
    assert len(gateway.lots['1'].credentials) == 5
    assert Inventory(storage).stats()['funpay_unknown'] == 1


def test_partial_funpay_lot_waits_without_stock_then_refills(storage):
    existing = accounts(3, 'remote')
    gateway = Gateway({'1': existing})
    service = FunPayService(storage, gateway, ['1'])

    service.reconcile_once()
    assert gateway.lots['1'].credentials == existing
    assert gateway.lots['1'].active
    assert not gateway.calls

    storage.import_accounts(accounts(2))
    service.reconcile_once()
    assert len(gateway.lots['1'].credentials) == 5
    assert gateway.lots['1'].active


def test_existing_funpay_stock_gets_one_line_labels(storage):
    stock = accounts(3)
    gateway = Gateway({'1': stock})
    gateway.lots['1'].fields['secrets'] = '\n'.join(f'{c.login}:{c.password}' for c in stock)
    service = FunPayService(storage, gateway, ['1'])

    service.reconcile_once()

    assert gateway.lots['1'].fields['secrets'] == format_secrets(stock)
    assert gateway.lots['1'].credentials == stock
    assert gateway.lots['1'].active
    assert [active for _, _, active in gateway.calls] == [False, True]
    service.reconcile_once()
    assert len(gateway.calls) == 2


def test_funpay_format_must_be_verified_before_reactivation(storage):
    stock = accounts(3)
    gateway = Gateway({'1': stock})
    gateway.lots['1'].fields['secrets'] = '\n'.join(f'{c.login}:{c.password}' for c in stock)
    original_save = gateway.save

    def strips_labels(fresh, credentials, active):
        original_save(fresh, credentials, active)
        gateway.lots[fresh.id].fields['secrets'] = '\n'.join(f'{c.login}:{c.password}' for c in credentials)

    gateway.save = strips_labels
    FunPayService(storage, gateway, ['1']).reconcile_once()

    assert not gateway.lots['1'].active
    assert Inventory(storage).stats()['funpay_errors'] == 1
    assert [active for _, _, active in gateway.calls] == [False, False]


def test_eight_lots_use_distinct_accounts(storage):
    storage.import_accounts(accounts(50))
    inventory = Inventory(storage)
    inventory.configure(True)
    gateway = Gateway({str(i): [] for i in range(1, 9)})
    service = FunPayService(storage, gateway, list(gateway.lots))
    service.reconcile_once()
    all_accounts = [c for lot in gateway.lots.values() for c in lot.credentials]
    assert len(all_accounts) == len({c.fingerprint for c in all_accounts}) == 40
    assert inventory.stats()['playerok_reserve'] == 5
    assert inventory.stats()['free'] == 5
    assert all(lot.active for lot in gateway.lots.values())


def test_empty_funpay_lot_waits_without_taking_another_lots_stock(storage):
    stock = accounts(5)
    gateway = Gateway({'1': stock, '2': []})
    service = FunPayService(storage, gateway, ['1', '2'])
    service.reconcile_once()
    assert gateway.lots['1'].credentials == stock
    assert gateway.lots['1'].active
    assert gateway.lots['2'].credentials == []
    assert not gateway.calls
    service.reconcile_once()
    assert not gateway.calls


def test_low_stock_lot_is_left_unchanged_when_warehouse_empty(storage):
    gateway = Gateway({'1': accounts(2), '2': []})
    FunPayService(storage, gateway, ['1', '2']).reconcile_once()
    assert len(gateway.lots['1'].credentials) == 2
    assert not gateway.lots['2'].credentials


def test_staged_upload_survives_restart(storage):
    storage.import_accounts(accounts(5))
    inventory = Inventory(storage)
    reserved = inventory.reserve('1', 5)
    gateway = Gateway({'1': []})
    FunPayService(storage, gateway, ['1']).reconcile_once()
    assert gateway.lots['1'].credentials == [a.credential for a in reserved]
    assert inventory.stats()['funpay_staged'] == 0


def test_lost_reply_after_commit_is_verified_without_duplicate_upload(storage):
    storage.import_accounts(accounts(5))
    gateway = Gateway({'1': []})
    gateway.fail = 'after'
    service = FunPayService(storage, gateway, ['1'])
    service.reconcile_once()
    assert len(gateway.lots['1'].credentials) == 5
    assert gateway.lots['1'].active
    assert not Inventory(storage).pending_edits()
    calls = len(gateway.calls)
    service.reconcile_once()
    assert len(gateway.calls) == calls


def test_lost_reply_before_commit_blocks_retries_and_keeps_reservations(storage):
    storage.import_accounts(accounts(5))
    gateway = Gateway({'1': []})
    gateway.fail = 'before'
    service = FunPayService(storage, gateway, ['1'])
    service.reconcile_once()
    calls = len(gateway.calls)
    gateway.fail = None
    restarted = FunPayService(storage, gateway, ['1'])
    restarted.reconcile_once()
    assert len(gateway.calls) == calls
    assert Inventory(storage).stats()['funpay_staged'] == 5
    assert Inventory(storage).stats()['funpay_errors'] == 1


def test_explicit_rejection_allows_new_fresh_request(storage):
    storage.import_accounts(accounts(5))
    gateway = Gateway({'1': []})
    gateway.fail = 'rejected'
    service = FunPayService(storage, gateway, ['1'])
    service.reconcile_once()
    assert not Inventory(storage).pending_edits()
    gateway.fail = None
    service.reconcile_once()
    assert len(gateway.lots['1'].credentials) == 5


def test_transfer_journal_recovers_crash_after_remote_removal(storage):
    stock = accounts(5)
    gateway = Gateway({'1': stock, '2': []})
    inventory = Inventory(storage)
    inventory.snapshot('1', stock, True)
    edit_id = inventory.prepare_edit('1', 'move:2', [c.fingerprint for c in stock], [c.fingerprint for c in stock[:2]], False)
    inventory.mark_edit(edit_id, 'sent')
    gateway.lots['1'].credentials = stock[:2]
    gateway.lots['1'].active = False
    service = FunPayService(storage, gateway, ['1', '2'])
    assert service.recover() == set()
    assert len(inventory.accounts('2')) == 3
    service.reconcile_once()
    assert len(gateway.lots['2'].credentials) == 3
    assert len(gateway.lots['1'].credentials) == 2


def test_transfer_recovery_is_idempotent_after_database_commit(storage):
    stock = accounts(5)
    inventory = Inventory(storage)
    inventory.snapshot('1', stock, False)
    moved = [c.fingerprint for c in stock[2:]]
    edit_id = inventory.prepare_edit('1', 'move:2', [c.fingerprint for c in stock], [c.fingerprint for c in stock[:2]], False)
    inventory.mark_edit(edit_id, 'sent')
    inventory.transfer('1', '2', moved)
    gateway = Gateway({'1': stock[:2], '2': []})
    gateway.lots['1'].active = False
    assert FunPayService(storage, gateway, ['1', '2']).recover() == set()
    assert len(inventory.accounts('2')) == 3


def test_disappeared_stock_not_resold_and_confirmed_sales_count_once(storage):
    stock = accounts(2)
    gateway = Gateway({'1': stock})
    service = FunPayService(storage, gateway, ['1'])
    service.reconcile_once()
    gateway.lots['1'].credentials = [stock[0]]
    service.reconcile_once()
    inventory = Inventory(storage)
    assert inventory.stats()['funpay_unknown'] == 1
    inventory.confirm_sale('order', stock[1])
    inventory.confirm_sale('order', stock[1])
    assert inventory.stats()['funpay_sold'] == 1
    assert inventory.stats()['funpay_unknown'] == 0
    assert storage.counts()['sold'] == 1
    assert not inventory.reserve('2', 5)


def test_orders_count_delivered_goods_even_if_later_refunded(storage):
    stock = accounts(2)
    gateway = Gateway({'1': stock})
    service = FunPayService(storage, gateway, ['1'])
    service.reconcile_once()
    gateway.lots['1'].credentials = []
    gateway.lots['1'].active = False
    gateway.sales['ABC'] = {'status': 'refunded', 'type_data': {'secrets': [{'value': format_secrets([c])} for c in stock]}}
    service.reconcile_once()
    service.reconcile_once()
    assert Inventory(storage).stats()['funpay_sold'] == 2
    assert not gateway.lots['1'].credentials


def test_late_refund_outside_first_page_updates_money(storage):
    from playerok_bot.finance import Finance
    stock = accounts(2)
    gateway = Gateway({'1': stock})
    service = FunPayService(storage, gateway, ['1'])
    service.reconcile_once()
    gateway.sales['OLD'] = {'status': 'closed', 'amount': '100.00', 'currency': 'RUB',
                            'type_data': {'secrets': [{'value': format_secrets([c])} for c in stock]}}
    service.sync_sales()
    assert Finance(storage).report()['gross'] == 10000
    assert Finance(storage).report()['orders'] == 1
    assert Finance(storage).report()['accounts'] == 2
    gateway.sales_page = lambda cursor=None: ([], None)
    gateway.sales['OLD']['status'] = 'refunded'
    storage.set_state_int('fp_finance_due', 0)
    service.sync_sales()
    assert Finance(storage).report()['gross'] == 0
    assert Finance(storage).report()['refunds'] == 1


def test_duplicate_login_across_platforms_and_changed_password_rejected(storage):
    stock = accounts(1)
    storage.import_accounts(stock)
    storage.bootstrap_listing('playerok', stock[0])
    with pytest.raises(StorageError):
        Inventory(storage).snapshot('funpay', stock, True)
    with pytest.raises(StorageError):
        storage.import_accounts([Credential(stock[0].login, 'different')])


def test_funpay_reservations_are_excluded_even_from_standalone_playerok(storage):
    stock = accounts(8)
    storage.import_accounts(stock)
    storage.bootstrap_listing('playerok', stock[0])
    inventory = Inventory(storage)
    reserved = inventory.reserve('funpay', 5)
    deal = storage.prepare_deal('sale', 'playerok', None)
    assert deal.next_account_id not in {a.id for a in reserved}


def test_mixed_categories_or_product_variants_rejected(storage):
    gateway = Gateway({'1': [], '2': []})
    gateway.lots['2'].node_id = 'another'
    service = FunPayService(storage, gateway, ['1', '2'])
    with pytest.raises(FunPayError):
        service.check()
    gateway.lots['2'].node_id = '1'
    gateway.lots['2'].fields['fields[type]'] = 'different'
    with pytest.raises(FunPayError):
        service.check()


def test_profiles_funpay_owner_unique_and_secrets_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv('FUNPAY_GOLDEN_KEY', 'foreign-secret')
    profiles = Profiles(tmp_path)
    try:
        profiles.connect_funpay(1, 'fp-own', 'own-key', 'own-agent')
        profiles.update(1, funpay_items=['123'])
        with pytest.raises(ConfigurationError):
            profiles.connect_funpay(2, 'fp-own', 'stolen-key', 'agent')
        profile = profiles.get(1)
        env = worker_environment(profile, profiles.user_dir(1))
        assert env['FUNPAY_GOLDEN_KEY'] == 'own-key'
        assert env['FUNPAY_OWNER_ID'] == 'fp-own'
        assert env['FUNPAY_LOTS'] == '["123"]'
        assert 'foreign-secret' not in env.values()
        assert b'own-key' not in profiles.connection.execute('SELECT config FROM profiles WHERE user_id=1').fetchone()[0]
        assert profiles.get(2).funpay_id is None
    finally:
        profiles.close()


@pytest.mark.parametrize('text', ['0', '-1', 'https://evil.test/lots/offer?id=1', '1 01', ' '.join(str(n) for n in range(1, 10)), 'https://funpay.com@evil.test/lots/offer?id=1'])
def test_bad_lot_references_rejected(text):
    with pytest.raises(ConfigurationError):
        parse_lots(text)


def test_funpay_urls_and_private_urls_are_normalized():
    assert parse_lots('https://funpay.com/lots/offer?id=12\nhttps://funpay.com/lots/offerEdit?offer=13\n14') == ['12', '13', '14']


def test_adapter_preserves_fresh_form_and_metadata(monkeypatch):
    gateway = FunPayGateway('fake-key', 'agent')
    html = b'''<body data-app-data='{"userId":123,"csrf-token":"header-csrf"}'>
    <div class="user-link-name">seller</div><form class="form-offer-editor" data-offer='{"amount":1}'>
    <input name="offer_id" value="12"><input name="node_id" value="77">
    <input name="form_created_at" value="fresh-time"><input name="csrf_token" value="form-csrf">
    <input type="checkbox" name="active" checked><input type="checkbox" name="auto_delivery" checked>
    <input type="checkbox" name="deactivate_after_sale"><input name="price" value="12.34">
    <textarea name="secrets">mail@example.test:p:^&lt;&amp;</textarea>
    <textarea name="fields[desc][ru]">my description</textarea><select name="fields[type]"><option value="x" selected>X</option></select>
    </form></body>'''
    calls = []
    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return SimpleNamespace(content=html, json=lambda: {'ok': True})
    monkeypatch.setattr(gateway, 'request', request)
    lot = gateway.read('12')
    assert lot.credentials == [Credential('mail@example.test', 'p:^<&')]
    assert 'deactivate_after_sale' not in lot.fields
    gateway.save(lot, lot.credentials + accounts(1), False)
    body = calls[-1][2]['data']
    assert body['form_created_at'] == 'fresh-time'
    assert body['csrf_token'] == 'form-csrf'
    assert calls[-1][2]['headers']['X-CSRF-Token'] == 'header-csrf'
    assert body['price'] == '12.34'
    assert body['fields[desc][ru]'] == 'my description'
    assert body['fields[type]'] == 'x'
    assert body['active'] == ''
    assert body['secrets'] == (
        'Логин: mail@example.test Пароль: p:^<&\n'
        'Логин: stock-0@example.test Пароль: p:^&0'
    )
    gateway.close()


def test_sale_between_form_read_and_freeze_does_not_restore_stale_stock(storage):
    stock = accounts(4)
    storage.import_accounts(accounts(2, 'free'))
    gateway = Gateway({'1': stock})
    service = FunPayService(storage, gateway, ['1'])
    def race(fresh, desired, active):
        if fresh.id == '1' and len(desired) == 4:
            gateway.lots['1'].credentials.pop()
            gateway.on_save = None
            # Сервер отклоняет старую форму; сервис не повторяет старый список.
            raise FunPayRejected('Stale form')
    gateway.on_save = race
    service.reconcile_once()
    service.reconcile_once()
    actual = [c for lot in gateway.lots.values() for c in lot.credentials]
    assert stock[-1] not in actual
    assert len(actual) == len(set(actual)) == 5
    assert Inventory(storage).stats()['funpay_unknown'] == 1


def test_sale_after_activation_before_verification_resolves_from_order(storage):
    storage.import_accounts(accounts(5))
    inventory = Inventory(storage)
    staged = inventory.reserve('1', 5)
    stock = [a.credential for a in staged]
    inventory.snapshot('1', stock, False)
    edit_id = inventory.prepare_edit('1', 'activate', [c.fingerprint for c in stock], [c.fingerprint for c in stock], True)
    inventory.mark_edit(edit_id, 'uncertain')
    gateway = Gateway({'1': stock[:-1]})
    gateway.sales['ORDER'] = {'status': 'closed', 'type_data': {'secrets': [{'value': f'{stock[-1].login}:{stock[-1].password}'}]}}
    service = FunPayService(storage, gateway, ['1'])
    assert service.recover() == {'1'}
    service.sync_sales()
    assert service.recover() == set()
    assert not inventory.pending_edits()
    assert inventory.stats()['funpay_sold'] == 1
    assert not gateway.calls


def test_new_accounts_restore_empty_funpay_without_restart(storage):
    gateway = Gateway({'1': []})
    service = FunPayService(storage, gateway, ['1'])
    service.reconcile_once()
    assert not gateway.lots['1'].active
    assert not gateway.calls
    storage.import_accounts(accounts(5))
    service.reconcile_once()
    assert gateway.lots['1'].active
    assert len(gateway.lots['1'].credentials) == 5


def test_playerok_priority_on_small_shared_stock_and_later_topup(storage):
    inventory = Inventory(storage)
    inventory.configure(True)
    storage.import_accounts(accounts(3))
    assert inventory.stats()['playerok_reserve'] == 3
    assert not inventory.reserve('1', 5)
    storage.import_accounts(accounts(7))
    assert inventory.stats()['playerok_reserve'] == 5
    assert len(inventory.reserve('1', 5)) == 2
    inventory.configure(False)
    assert inventory.stats()['playerok_reserve'] == 0
    assert inventory.stats()['free'] == 5
    assert inventory.stats()['funpay_staged'] == 2


def test_conflicting_password_rolls_back_entire_import(storage):
    stock = accounts(2)
    storage.import_accounts(stock[:1])
    with pytest.raises(StorageError):
        storage.import_accounts([stock[1], Credential(stock[0].login, 'wrong')])
    assert storage.counts()['queued'] == 1


def test_newest_orders_processed_while_historical_cursor_pending(storage):
    stock = accounts(2)
    gateway = Gateway({'1': stock})
    inventory = Inventory(storage)
    inventory.snapshot('1', stock, True)
    gateway.sales = {
        'NEW': {'status': 'closed', 'type_data': {'secrets': [{'value': f'{stock[0].login}:{stock[0].password}'}]}},
        'OLD': {'status': 'closed', 'type_data': {'secrets': [{'value': f'{stock[1].login}:{stock[1].password}'}]}},
    }
    storage._connection.execute("INSERT INTO bot_state VALUES ('fp_sales_cursor','old-page','now')")
    storage._connection.commit()
    calls = []
    def pages(cursor=None):
        calls.append(cursor)
        return (['OLD'], None) if cursor else (['NEW'], 'old-page')
    gateway.sales_page = pages
    FunPayService(storage, gateway, ['1']).sync_sales()
    assert calls == [None, 'old-page']
    assert inventory.stats()['funpay_sold'] == 2


def test_old_registry_profiles_gain_funpay_defaults_without_losing_config(tmp_path):
    profiles = Profiles(tmp_path)
    profiles.connect(101, 'playerok', 'token=original', 'agent')
    encrypted = profiles._encode({'cookies': 'token=original', 'user_agent': 'agent', 'items': ['old-item'], 'price_limit': 25, 'bump': False})
    profiles.connection.execute('UPDATE profiles SET config=? WHERE user_id=101', (encrypted,))
    # В старой версии таблиц групп ещё не было.
    profiles.connection.execute('DROP TABLE product_groups')
    profiles.connection.execute('DROP TABLE selected_groups')
    profiles.connection.commit()
    profiles.close()
    reopened = Profiles(tmp_path)
    try:
        profile = reopened.get(101)
        assert profile.config['funpay_items'] == []
        assert profile.config['items'] == ['old-item']
        assert profile.config['cookies'] == 'token=original'
        assert profile.config['price_limit'] == 25
    finally:
        reopened.close()


def test_adapter_never_sends_cookie_to_external_redirect(monkeypatch):
    gateway = FunPayGateway('secret-key', 'agent')
    calls = []
    def request(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(status_code=302, headers={'Location': 'https://evil.test/'})
    monkeypatch.setattr(gateway.session, 'request', request)
    with pytest.raises(FunPayError):
        gateway.request('GET', '')
    assert len(calls) == 1
    gateway.close()


def test_adapter_handles_same_site_locale_redirect(monkeypatch):
    gateway = FunPayGateway('secret-key', 'agent')
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=302, headers={'Location': '/'}) if len(calls) == 1 else SimpleNamespace(status_code=200)
    monkeypatch.setattr(gateway.session, 'request', request)
    gateway.request('GET', '?setlocale=ru')
    assert calls == ['https://funpay.com/?setlocale=ru', 'https://funpay.com/']
    gateway.close()

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from playerok_bot.accounts import parse_accounts_text, parse_account_line, load_accounts
from playerok_bot.errors import AccountsFormatError, FieldMappingError
from playerok_bot.fields import DeliveryFieldMapper, FieldOverrides
from playerok_bot.models import Credential
from playerok_bot.storage import Storage, StorageError
from playerok_bot.funpay.gateway import format_secrets
from playerok_bot.gateway import PlayerokGateway


@pytest.mark.parametrize('separator', [' ', '\t', '\n', '\r\n', '\u00a0', '\u2028'])
def test_bulk_accounts_are_individual_credentials(separator, tmp_path):
    text = separator.join(f'account{i}@example.test:p{i}:!%$' for i in range(20))
    expected = [Credential(f'account{i}@example.test', f'p{i}:!%$') for i in range(20)]
    assert parse_accounts_text(text) == expected
    path = tmp_path / 'accounts.txt'
    path.write_text(text, encoding='utf-8')
    assert load_accounts(path) == expected
    db = Storage(tmp_path / 'stock.db')
    try:
        assert db.import_accounts(expected) == 20
        assert db.import_accounts(expected) == 0
        assert db.counts()['queued'] == 20
    finally:
        db.close()


@pytest.mark.parametrize('text', [
    'a@example.test:first b@example.test:second',
    'a@example.test:first\nb@example.test:second',
    'a@example.test:first\\nb@example.test:second',
    'a@example.test:firstb@example.test:second',
    'Логин a@example.test Пароль first b@example.test:second',
])
def test_single_account_parser_never_swallows_list(text):
    with pytest.raises(AccountsFormatError):
        parse_account_line(text, 1)


@pytest.mark.parametrize('password', ['one two', 'one\ntwo', 'one\ttwo', 'one\x00two', 'one\\ntwo', 'xother@example.test:secret'])
def test_unsafe_credentials_block_storage_and_both_deliveries(tmp_path, password):
    credential = Credential('first@example.test', password)
    db = Storage(tmp_path / 'stock.db')
    try:
        with pytest.raises(AccountsFormatError):
            db.import_accounts([Credential('good', 'pass'), credential])
        assert db.counts()['queued'] == 0
    finally:
        db.close()
    fields = [SimpleNamespace(label=label, value='unchanged') for label in ('Логин', 'Пароль', 'Комментарий')]
    mapper = DeliveryFieldMapper(FieldOverrides(), 'thanks')
    with pytest.raises(AccountsFormatError):
        mapper.apply(fields, credential)
    assert all(f.value == 'unchanged' for f in fields)
    with pytest.raises(AccountsFormatError):
        format_secrets([credential])


def test_legacy_corruption_blocks_read_and_start_without_deleting(tmp_path):
    db = Storage(tmp_path / 'stock.db')
    try:
        db.import_accounts([Credential('a', 'safe')])
        with db._transaction() as connection:
            connection.execute("UPDATE accounts SET password='safe other:secret'")
        with pytest.raises(StorageError, match='№1'):
            db.validate_inventory()
        with pytest.raises(AccountsFormatError):
            db.get_account(1)
        assert db.counts()['queued'] == 1
    finally:
        db.close()


def test_multiple_combined_accounts_and_duplicate_fields_rejected():
    mapper = DeliveryFieldMapper(FieldOverrides(), 'thanks')
    fields = [SimpleNamespace(label='Данные аккаунта', value='Логин: a\nПароль: one\nЛогин: b\nПароль: two')]
    with pytest.raises(FieldMappingError):
        mapper.extract(fields)
    with pytest.raises(FieldMappingError):
        mapper.apply([SimpleNamespace(label='Пароль', value=None)] * 2, Credential('a', 'pass'))


def test_playerok_never_publishes_a_legacy_bulk_password():
    mapper = DeliveryFieldMapper(FieldOverrides(), 'thanks')
    gateway = PlayerokGateway(SimpleNamespace(), mapper)
    gateway.account = Mock()
    gateway.get_listing = Mock(return_value=SimpleNamespace(data_fields=[
        SimpleNamespace(label='Логин', value='a'),
        SimpleNamespace(label='Пароль', value='one b:two'),
        SimpleNamespace(label='Комментарий', value='thanks')]))
    with pytest.raises(AccountsFormatError):
        gateway.publish_premium()
    gateway.account.publish_item.assert_not_called()


def test_delivery_clears_stale_combined_field():
    mapper = DeliveryFieldMapper(FieldOverrides(), 'thanks')
    fields = [SimpleNamespace(label=label, value='old') for label in ('Логин', 'Пароль', 'Комментарий', 'Данные аккаунта')]
    with pytest.raises(FieldMappingError):
        mapper.extract(fields)
    mapper.apply(fields, Credential('a', 'one'))
    assert [f.value for f in fields] == ['a', 'one', 'thanks', '']


def test_playerok_update_requires_readback_confirmation():
    from playerok_bot.errors import ListingValidationError
    fields = lambda password: [SimpleNamespace(label=label, value=value) for label, value in
                              [('Логин', 'a'), ('Пароль', password), ('Комментарий', 'thanks')]]
    gateway = PlayerokGateway(SimpleNamespace(), DeliveryFieldMapper(FieldOverrides(), 'thanks'))
    gateway.account = Mock()
    gateway._resolved_item_id = 'lot'
    gateway.get_listing = Mock(side_effect=[SimpleNamespace(data_fields=fields('old')),
                                           SimpleNamespace(data_fields=fields('old'))])
    with pytest.raises(ListingValidationError, match='не подтвердил'):
        gateway.update_delivery(Credential('a', 'new'))
    gateway.account.publish_item.assert_not_called()
    gateway.get_listing = Mock(side_effect=[SimpleNamespace(data_fields=fields('old')),
                                           SimpleNamespace(data_fields=fields('new'))])
    gateway.update_delivery(Credential('a', 'new'))


def test_imported_batch_republishes_exactly_one_account_per_sale(tmp_path):
    from playerok_bot.service import SaleService
    from playerok_bot.models import PaidDeal
    credentials = parse_accounts_text('first@example.test:p1 second@example.test:p2 third@example.test:p3')
    storage = Storage(tmp_path / 'stock.db')
    mapper = DeliveryFieldMapper(FieldOverrides(), 'thanks')
    fields = [SimpleNamespace(label=label, value=None) for label in ('Логин', 'Пароль', 'Комментарий')]
    sent = []
    def update(credential):
        mapper.apply(fields, credential)
        sent.append((fields[0].value, fields[1].value))
    gateway = SimpleNamespace(item_id='lot', confirm_deal_sent=Mock(), delivery_matches=lambda _: False,
                              update_delivery=update, is_active=lambda: False, publish_premium=lambda: 25)
    service = SaleService(SimpleNamespace(send_courtesy_message=False, auto_confirm_deal=True), storage, gateway)
    try:
        storage.import_accounts(credentials)
        storage.bootstrap_listing('lot', credentials[0])
        for i in range(3):
            service.process(PaidDeal(f'deal{i}', 'lot', None))
        assert sent == [('second@example.test', 'p2'), ('third@example.test', 'p3')]
        assert storage.counts()['sold'] == 3
    finally:
        storage.close()

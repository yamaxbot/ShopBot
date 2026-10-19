from types import SimpleNamespace
import pytest
from playerokapi.enums import ItemStatuses
from playerok_bot import withdrawal
from playerok_bot.storage import Storage, StorageError
from playerok_bot.models import Credential
from playerok_bot.errors import ConfigurationError
from playerok_bot.inventory import Inventory
from playerok_bot.funpay.service import FunPayService
from test_funpay import Gateway


def test_free_stock_removed_only_after_delivery_and_cannot_reimport(tmp_path):
    db = Storage(tmp_path/'db')
    account = Credential('one','secret')
    db.import_accounts([account])
    db.set_state_text('export_state','preparing')
    withdrawal.prepare_remote(db,[])
    assert withdrawal.payload(db) == (b'one:secret\n',1)
    assert db.counts()['queued'] == 1
    with pytest.raises(ConfigurationError):
        withdrawal.assert_idle(db)
    withdrawal.finish(db,'telegram-file')
    assert db.counts()['queued'] == 0
    with pytest.raises(StorageError):
        db.import_accounts([account])
    db.close()
    db = Storage(tmp_path/'db')
    assert withdrawal.state(db) == 'delivered'
    assert db._connection.execute("SELECT value FROM bot_state WHERE key='export_file'").fetchone()[0] == 'telegram-file'
    db.close()


def test_playerok_active_or_changed_or_unclosed_sale_blocks(tmp_path):
    db = Storage(tmp_path/'db')
    credential = Credential('one','secret')
    db.import_accounts([credential])
    db.bootstrap_listing('lot',credential)
    item = SimpleNamespace(user=SimpleNamespace(id='owner'),status=ItemStatuses.APPROVED,keep_in_sale=False)
    gateway = SimpleNamespace(item_id='lot',account=SimpleNamespace(id='owner'),resolve_listing=lambda:item,
                              current_credential=lambda _:credential,recent_paid_deals=lambda:[])
    with pytest.raises(ConfigurationError):
        withdrawal.prepare_remote(db,[gateway])
    item.status = ItemStatuses.DRAFT
    gateway.recent_paid_deals = lambda:[object()]
    with pytest.raises(ConfigurationError):
        withdrawal.prepare_remote(db,[gateway])
    gateway.recent_paid_deals = lambda:[]
    withdrawal.prepare_remote(db,[gateway])
    assert withdrawal.payload(db)[1] == 1
    withdrawal.finish(db,'file')
    assert db.counts()['listed'] == 0
    db.close()


def test_funpay_manual_pause_then_clear_and_lost_reply_recovery(tmp_path):
    db = Storage(tmp_path/'db')
    credential = Credential('fp','secret')
    gateway = Gateway({'1':[credential]})
    service = FunPayService(db,gateway,['1'])
    with pytest.raises(ConfigurationError):
        withdrawal.prepare_remote(db,[],service)
    assert not gateway.calls
    gateway.lots['1'].active = False
    gateway.fail = 'after'
    withdrawal.prepare_remote(db,[],service)
    assert gateway.lots['1'].credentials == []
    assert withdrawal.payload(db) == (b'fp:secret\n',1)
    withdrawal.finish(db,'file')
    assert db.counts()['queued'] == 0
    db.close()


def test_unknown_funpay_removal_does_not_release_accounts(tmp_path):
    db = Storage(tmp_path/'db')
    credential = Credential('fp','secret')
    gateway = Gateway({'1':[credential]})
    gateway.lots['1'].active = False
    service = FunPayService(db,gateway,['1'])
    gateway.fail = 'before'
    with pytest.raises(Exception):
        withdrawal.prepare_remote(db,[],service)
    assert withdrawal.state(db) != 'ready'
    assert db.counts()['queued'] == 1
    assert Inventory(db).pending_edits()
    with pytest.raises(ConfigurationError):
        withdrawal.prepare_remote(db,[],service)
    # Server eventually confirms the original request; retry only reconciles it.
    gateway.lots['1'].credentials = []
    withdrawal.prepare_remote(db,[],service)
    assert len(gateway.calls) == 1
    assert withdrawal.payload(db)[1] == 1
    db.close()

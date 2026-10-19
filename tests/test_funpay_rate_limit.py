from types import SimpleNamespace
from datetime import datetime, timezone
from email.utils import format_datetime
import pytest
from playerok_bot.funpay.rate_limit import RequestLimit, FunPayRateLimited, retry_seconds
from playerok_bot.funpay.gateway import FunPayGateway
from playerok_bot.funpay.service import FunPayService
from playerok_bot.inventory import Inventory
from playerok_bot.storage import Storage
from test_funpay import Gateway, accounts


def test_shared_pacing_cooldown_restart_and_retry_after(tmp_path,monkeypatch):
    clock=[10000.0]
    monkeypatch.setattr('playerok_bot.funpay.rate_limit.time.time',lambda:clock[0])
    monkeypatch.setattr('playerok_bot.funpay.rate_limit.time.sleep',lambda delay:clock.__setitem__(0,clock[0]+delay))
    first=RequestLimit(tmp_path/'rate.db')
    second=RequestLimit(tmp_path/'rate.db')
    first.acquire(); second.acquire()
    assert clock[0]==10003
    error=first.limited('600')
    assert error.until==10603
    with pytest.raises(FunPayRateLimited): second.acquire()
    first.close(); first=RequestLimit(tmp_path/'rate.db')
    with pytest.raises(FunPayRateLimited): first.acquire()
    clock[0]=10603
    second.acquire()
    assert second.limited(None).until==10843
    assert retry_seconds(format_datetime(datetime.fromtimestamp(11000,timezone.utc)),10603)==397
    assert retry_seconds('invalid',10603)==0
    first.close(); second.close()


def test_http_429_does_not_retry_and_blocks_other_client(tmp_path,monkeypatch):
    monkeypatch.setenv('FUNPAY_RATE_DB',str(tmp_path/'rate.db'))
    first=FunPayGateway('fake','agent'); second=FunPayGateway('fake','agent')
    calls=[]
    def request(*args,**kwargs):
        calls.append(args)
        return SimpleNamespace(status_code=429,headers={'Retry-After':'600'})
    monkeypatch.setattr(first.session,'request',request)
    monkeypatch.setattr(second.session,'request',request)
    with pytest.raises(FunPayRateLimited): first.request('POST','lots/offerSave')
    with pytest.raises(FunPayRateLimited): second.request('GET','lots/offerEdit')
    assert len(calls)==1
    first.close(); second.close()


def test_429_during_upload_keeps_stock_and_retries_fresh_after_pause(tmp_path,monkeypatch):
    storage=Storage(tmp_path/'db')
    storage.import_accounts(accounts(5))
    gateway=Gateway({'1':[],'2':[]})
    service=FunPayService(storage,gateway,['1','2'])
    original=gateway.save
    calls=[]
    def limited(*args):
        calls.append(args)
        raise FunPayRateLimited(9999999999)
    monkeypatch.setattr(gateway,'save',limited)
    service.reconcile_once()
    assert len(calls)==1
    assert not Inventory(storage).pending_edits()
    assert storage.counts()['queued']==5
    monkeypatch.setattr(gateway,'save',original)
    service.reconcile_once()
    assert len(gateway.lots['1'].credentials)==5
    assert gateway.lots['1'].active
    storage.close()

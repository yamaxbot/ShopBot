from types import SimpleNamespace

import pytest

from playerok_bot.funpay.gateway import FunPayError, FunPayGateway, RaiseOutcome
from playerok_bot.funpay.service import FunPayService
from playerok_bot.models import Credential
from playerok_bot.storage import Storage

from test_funpay import Gateway


def test_gateway_discovers_game_and_posts_only_configured_subcategory(monkeypatch):
    gateway = FunPayGateway('fake-key', 'agent')
    homepage = b'''<body data-app-data='{"userId":123,"csrf-token":"csrf"}'>
        <div class="user-link-name">seller</div>
        <div class="promo-game-item"><div class="game-title" data-id="576">CPM2</div>
        <ul data-id="576"><li><a href="https://funpay.com/lots/2603/">Accounts</a></li>
        <li><a href="https://funpay.com/lots/2604/">Currency</a></li></ul></div>
        </body>'''
    requests = []

    def request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        if method == 'GET':
            return SimpleNamespace(content=homepage)
        return SimpleNamespace(json=lambda: {'error': False, 'wait': 347})

    monkeypatch.setattr(gateway, 'request', request)
    try:
        gateway.connect()
        assert gateway.game_id_for('2603') == '576'
        assert gateway.raise_lots('2603') == RaiseOutcome(True, 347)
        method, path, kwargs = requests[-1]
        assert (method, path) == ('POST', 'lots/raise')
        assert kwargs['data'] == {'game_id': '576', 'node_id': '2603', 'node_ids[]': ['2603']}
        assert kwargs['headers']['X-Requested-With'] == 'XMLHttpRequest'
        with pytest.raises(FunPayError):
            gateway.raise_lots('9999')
        assert len(requests) == 2
    finally:
        gateway.close()


@pytest.mark.parametrize(('payload', 'expected'), [
    ({'error': True, 'wait': '81', 'msg': 'Подождите'}, RaiseOutcome(False, 81)),
    ({'error': True, 'msg': 'Please wait'}, RaiseOutcome(False, None)),
    ({'error': False, 'wait': 1.2}, RaiseOutcome(True, 2)),
])
def test_gateway_uses_server_cooldown(monkeypatch, payload, expected):
    gateway = FunPayGateway('fake-key', 'agent')
    gateway._node_games['2603'] = '576'
    monkeypatch.setattr(gateway, 'request', lambda *args, **kwargs: SimpleNamespace(json=lambda: payload))
    try:
        assert gateway.raise_lots('2603') == expected
    finally:
        gateway.close()


@pytest.mark.parametrize('payload', [
    {'error': True, 'msg': 'Access denied'},
    {'error': False, 'url': '/captcha'},
    {'error': False, 'html': '<form>choose</form>'},
    {'error': False, 'wait': 'invalid'},
])
def test_gateway_does_not_report_unknown_response_as_success(monkeypatch, payload):
    gateway = FunPayGateway('fake-key', 'agent')
    gateway._node_games['2603'] = '576'
    monkeypatch.setattr(gateway, 'request', lambda *args, **kwargs: SimpleNamespace(json=lambda: payload))
    try:
        with pytest.raises(FunPayError):
            gateway.raise_lots('2603')
    finally:
        gateway.close()


def test_raise_schedule_is_persisted_and_shared_by_managed_lots(tmp_path, monkeypatch):
    storage = Storage(tmp_path / 'seller.sqlite3')
    gateway = Gateway({
        '1': [Credential('first@example.test', 'password-1')],
        '2': [Credential('second@example.test', 'password-2')],
    })
    calls = []
    gateway.raise_lots = lambda node: (calls.append(node), RaiseOutcome(True, 91))[1]
    monkeypatch.setattr('playerok_bot.funpay.service.time.time', lambda: 1000)
    try:
        service = FunPayService(storage, gateway, ['1', '2'])
        service.reconcile_once()
        assert service.raise_when_available()
        assert calls == ['1']
        assert not service.raise_when_available()
        restarted = FunPayService(storage, gateway, ['1', '2'])
        restarted.check()
        monkeypatch.setattr('playerok_bot.funpay.service.time.time', lambda: 1090)
        assert not restarted.raise_when_available()
        monkeypatch.setattr('playerok_bot.funpay.service.time.time', lambda: 1091)
        assert restarted.raise_when_available()
        assert calls == ['1', '1']
    finally:
        storage.close()


def test_raise_waits_for_active_stock_and_recovers_after_refill(tmp_path, monkeypatch):
    storage = Storage(tmp_path / 'seller.sqlite3')
    gateway = Gateway({'1': []})
    calls = []
    gateway.raise_lots = lambda node: (calls.append(node), RaiseOutcome(False, 120))[1]
    monkeypatch.setattr('playerok_bot.funpay.service.time.time', lambda: 1000)
    try:
        service = FunPayService(storage, gateway, ['1'])
        service.reconcile_once()
        assert not service.raise_when_available()
        assert calls == []
        storage.import_accounts([Credential('first@example.test', 'password-1')])
        service.reconcile_once()
        assert not service.raise_when_available()
        assert calls == ['1']
        assert not service.raise_when_available()
    finally:
        storage.close()


def test_unknown_raise_result_is_not_retried_immediately(tmp_path, monkeypatch):
    storage = Storage(tmp_path / 'seller.sqlite3')
    gateway = Gateway({'1': [Credential('first@example.test', 'password-1')]})
    calls = []

    def fail(node):
        calls.append(node)
        raise FunPayError('Unknown result')

    gateway.raise_lots = fail
    monkeypatch.setattr('playerok_bot.funpay.service.time.time', lambda: 1000)
    try:
        service = FunPayService(storage, gateway, ['1'])
        service.reconcile_once()
        with pytest.raises(FunPayError):
            service.raise_when_available()
        restarted = FunPayService(storage, gateway, ['1'])
        restarted.check()
        assert not restarted.raise_when_available()
        assert calls == ['1']
    finally:
        storage.close()

"""Один процесс на продавца, две площадки и одна личная SQLite-база."""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

from ..app import BotApplication
from ..config import Settings
from ..errors import ConfigurationError, InventoryNotReadyError
from ..funpay.gateway import FunPayError, FunPayGateway, FunPayRateLimited
from ..funpay.service import FunPayService
from ..inventory import Inventory
from ..logging_setup import configure_logging
from ..storage import Storage

logger = logging.getLogger(__name__)


def run():
    # Startup can also hit 429. Preserve enabled workers and retry after the shared pause.
    while True:
        try:
            return _run_once()
        except FunPayRateLimited as exc:
            logger.warning('Запуск FunPay отложен: %s', exc)
            while time.time() < exc.until:
                time.sleep(max(0, min(5, exc.until - time.time())))


def _run_once():
    directory = Path.cwd()
    configure_logging(directory / 'marketplaces.log')
    storage = Storage(Path(os.environ['DATABASE_PATH']))
    playerok = None
    funpay = None
    stop = threading.Event()
    previous = {}
    for name in ('SIGTERM', 'SIGINT'):
        sig = getattr(signal, name, None)
        if sig is not None:
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
    try:
        has_playerok = bool(os.environ.get('PLAYEROK_ITEM'))
        lot_ids = json.loads(os.environ.get('FUNPAY_LOTS', '[]'))
        if not has_playerok and not lot_ids:
            raise ConfigurationError('Не настроены объявления ни одной площадки')
        inventory = Inventory(storage)
        inventory.configure(has_playerok)
        ready = False
        if has_playerok:
            playerok = BotApplication(Settings.load(directory))
            try:
                playerok.prepare()
                ready = True
            except InventoryNotReadyError:
                logger.info('Playerok ожидает данные текущих лотов')
            if str(playerok.listings[0].gateway.account.id) != os.environ['PLAYEROK_OWNER_ID']:
                raise ConfigurationError('Playerok принадлежит другому продавцу')
        service = None
        if lot_ids:
            funpay = FunPayGateway(os.environ['FUNPAY_GOLDEN_KEY'], os.environ['FUNPAY_USER_AGENT']).connect()
            if funpay.id != os.environ['FUNPAY_OWNER_ID']:
                raise ConfigurationError('FunPay принадлежит другому продавцу')
            service = FunPayService(storage, funpay, lot_ids)
            # Существующая автовыдача учитывается прежде, чем Playerok выберет замену.
            blocked = service.recover()
            snapshots = service.check()
            funpay.game_id_for(service.node_id)
            if playerok:
                playerok_logins = set()
                for listing in playerok.listings:
                    current = listing.gateway.current_credential(listing.gateway.resolve_listing())
                    if current:
                        playerok_logins.add(current.login.casefold())
                if playerok_logins.intersection(c.login.casefold() for lot in snapshots for c in lot.credentials):
                    raise ConfigurationError('Один аккаунт уже выставлен одновременно на Playerok и FunPay. Уберите дубликат вручную')
            for lot in snapshots:
                if lot.id not in blocked:
                    inventory.snapshot(lot.id, lot.credentials, lot.active)
        while not stop.is_set():
            inventory.configure(has_playerok)
            if playerok:
                try:
                    if not ready:
                        playerok.prepare()
                        if str(playerok.listings[0].gateway.account.id) != os.environ['PLAYEROK_OWNER_ID']:
                            raise ConfigurationError('Playerok принадлежит другому продавцу')
                        ready = True
                    playerok.reconcile_once()
                except InventoryNotReadyError:
                    logger.info('Playerok ожидает данные текущих лотов; добавьте их через Telegram')
                except ConfigurationError:
                    raise
                except Exception as exc:
                    logger.warning('Проверка Playerok: %s', type(exc).__name__)
            if service:
                try:
                    service.reconcile_once()
                except FunPayRateLimited as exc:
                    storage.set_state_text('fp_refill_error:pause', str(exc))
                    logger.warning('Проверка FunPay: %s', exc)
                except Exception as exc:
                    logger.warning('Проверка FunPay: %s', type(exc).__name__)
                else:
                    storage.set_state_text('fp_refill_error:pause', '')
                    try:
                        service.raise_when_available()
                    except FunPayError as exc:
                        logger.warning('Автоподнятие FunPay: %s', exc)
                    except Exception as exc:
                        logger.warning('Автоподнятие FunPay: %s', type(exc).__name__)
            stop.wait(30)
    finally:
        if playerok:
            playerok.close()
        if funpay:
            funpay.close()
        storage.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)

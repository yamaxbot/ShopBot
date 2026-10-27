"""Проверки Playerok выполняются в отдельном интерпретаторе из-за singleton Account."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..errors import BotError, InventoryNotReadyError


def record_stage(stage: str) -> None:
    """Сохранять только наши названия этапов, без ответов API и секретов."""
    try:
        with Path("probe.log").open("a", encoding="utf-8") as output:
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            output.write(f"{timestamp} | pid={os.getpid()} | {stage}\n")
    except OSError:
        pass


def main():
    logging.disable(logging.CRITICAL)
    if len(sys.argv) > 1 and sys.argv[1] == 'withdraw':
        try:
            from ..withdrawal import run_from_environment
            run_from_environment()
            print(json.dumps({'ok': True}))
        except BotError as exc:
            print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({'ok': False, 'error': 'Выгрузка не подтверждена: ' + type(exc).__name__}))
        return
    logging.disable(logging.CRITICAL)
    app = None
    funpay = None
    try:
        if sys.argv[1].startswith('funpay_'):
            record_stage("FunPay: подключение к профилю")
            from ..funpay.gateway import FunPayGateway
            funpay = FunPayGateway(os.environ['FUNPAY_GOLDEN_KEY'], os.environ['FUNPAY_USER_AGENT']).connect()
            if sys.argv[1] == 'funpay_check':
                from ..storage import Storage
                from ..funpay.service import FunPayService
                storage = Storage(Path(os.environ['DATABASE_PATH']))
                try:
                    record_stage("FunPay: проверка лотов и автовыдачи")
                    snapshots = FunPayService(storage, funpay, json.loads(os.environ['FUNPAY_LOTS'])).check()
                    record_stage("FunPay: проверка раздела для поднятия")
                    funpay.game_id_for(snapshots[0].node_id)
                finally:
                    storage.close()
            account = funpay
        elif sys.argv[1] == "identity":
            record_stage("Playerok: подключение к профилю")
            from ..gateway import _ensure_playerok_certificate
            from playerokapi.account import Account
            _ensure_playerok_certificate()
            account = Account(cookies=os.environ["PLAYEROK_COOKIES"],
                              user_agent=os.environ["PLAYEROK_USER_AGENT"]).get()
        else:
            from ..app import BotApplication
            from ..config import Settings
            app = BotApplication(Settings.load(Path.cwd()))
            app.prepare(progress=record_stage)
            account = app.listings[0].gateway.account
        record_stage("Проверка завершена успешно")
        print(json.dumps({"ok": True, "id": str(account.id), "username": account.username}))
    except InventoryNotReadyError as exc:
        record_stage("Ошибка: InventoryNotReadyError")
        numbers = ', '.join(map(str, exc.lot_numbers))
        lots = f'№{numbers}' if numbers else 'из ваших настроек'
        message = (
            'Проверка Playerok не пройдена.\n\n'
            f'В вашей базе не найдены данные игровых аккаунтов из объявлений Playerok {lots}. '
            'Номера соответствуют порядку ссылок, отправленных через /items.\n\n'
            'Откройте эти объявления на Playerok и посмотрите логин и пароль в данных для покупателя. '
            'Отправьте сюда, в личный чат с ботом, команду /add, а затем эти же данные в формате:\n'
            'логин:пароль\n\n'
            'Для нескольких аккаунтов — каждый с новой строки. '
            'Логин и пароль должны точно совпадать с указанными в объявлениях.\n\n'
            'Дождитесь сообщения об импорте, затем выполните /check и /run.'
        )
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        raise SystemExit(2)
    except BotError as exc:
        record_stage("Ошибка: " + type(exc).__name__)
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)
    except Exception as exc:
        record_stage("Ошибка: " + type(exc).__name__)
        platform = 'FunPay' if sys.argv[1].startswith('funpay_') else 'Playerok'
        print(json.dumps({"ok": False, "error": platform + " не прошёл проверку: " + type(exc).__name__}, ensure_ascii=False))
        raise SystemExit(1)
    finally:
        if app:
            app.close()
        if funpay:
            funpay.close()


if __name__ == "__main__":
    main()

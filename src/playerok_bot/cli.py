from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .accounts import load_accounts
from .app import BotApplication
from .config import Settings
from .errors import BotError
from .logging_setup import configure_logging
from .storage import Storage

logger = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Автовыдача Car Parking Multiplayer 2")
    parser.add_argument(
        "command",
        choices=("check", "run", "status", "import-accounts", "telegram", "migrate-user"),
        help=(
            "check — проверить Playerok; run — запустить Playerok; "
            "status — остаток; import-accounts — импортировать файл; "
            "telegram — Telegram-хост; migrate-user — перенос старой базы"
        ),
    )
    parser.add_argument("--user-id", type=int, help="Telegram ID для migrate-user")
    return parser


def _print_status(storage: Storage) -> None:
    counts = storage.counts()
    print(f"Ожидают продажи: {counts['queued']}")
    print(f"Сейчас выставлено: {counts['listed']}")
    print(f"Продано: {counts['sold']}")
    print(f"Сделок обработано: {counts['completed_deals']}")
    print(f"Сделок с ошибкой: {counts['failed_deals']}")
    print(f"Восстановлений ожидают завершения: {counts['pending_restocks']}")
    print(f"Восстановлений с ошибкой: {counts['failed_restocks']}")
    print(f"Успешных плановых поднятий: {counts['successful_bumps']}")
    print(f"Поднятий с неизвестным результатом: {counts['uncertain_bumps']}")
    for row in storage.listing_counts():
        print(f"Лот {row['item_id']}: выставлено {row['listed']}, продано {row['sold']}, поднятий {row['bumps']}")


def main() -> None:
    args = _parser().parse_args()
    app: BotApplication | None = None
    storage: Storage | None = None
    try:
        if args.command == "telegram":
            from .telegram.bot import main as telegram_main
            telegram_main()
            return
        if args.command == "migrate-user":
            from .telegram.migrate import migrate_user
            if not args.user_id:
                parser = _parser()
                parser.error("Для migrate-user укажите --user-id")
            migrate_user(args.user_id)
            return
        if args.command == 'run' and os.environ.get('PLATFORM_INVENTORY') == 'true':
            from .telegram.engine import run
            run()
            return
        settings = Settings.load(Path.cwd())
        configure_logging(settings.log_path)

        if args.command == "status":
            storage = Storage(settings.database_path)
            _print_status(storage)
            return

        if args.command == "import-accounts":
            storage = Storage(settings.database_path)
            credentials = load_accounts(settings.accounts_file)
            inserted = storage.import_accounts(credentials)
            print(f"Добавлено новых аккаунтов: {inserted}")
            _print_status(storage)
            return

        app = BotApplication(settings)
        if args.command == "check":
            infos = app.prepare()
            print("Проверка пройдена.")
            print(f"Профиль Playerok: @{app.listings[0].gateway.account.username}")
            print(f"Лотов: {len(infos)}")
            for number, info in enumerate(infos, 1):
                print(f"Лот №{number}: {info.name}")
                print(f"Игра: {info.game_name}")
                print(f"ID лота: {info.id}")
                print(f"Цена товара: {info.price} ₽")
                print(f"Повторная премиум-публикация: {info.premium_republish_price} ₽")
                print("Поля выдачи: " + ", ".join(info.field_labels))
            _print_status(app.storage)
            return

        app.run()
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    except BotError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        logger.exception("Неожиданная ошибка приложения: %s", type(exc).__name__)
        print(
            f"Неожиданная ошибка: {type(exc).__name__}. Подробности записаны в журнал.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    finally:
        if app is not None:
            app.close()
        if storage is not None:
            storage.close()

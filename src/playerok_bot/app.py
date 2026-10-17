from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from .accounts import load_accounts
from .bump import BumpService
from .config import Settings
from .errors import AccountsFormatError, InventoryNotReadyError, ListingValidationError, PremiumPriceLimitError
from .fields import DeliveryFieldMapper, FieldOverrides
from .gateway import ListingInfo, PlayerokGateway
from .models import Credential
from .service import SaleService
from .storage import Storage

logger = logging.getLogger(__name__)


@dataclass
class ListingRuntime:
    gateway: PlayerokGateway
    service: SaleService


class BotApplication:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = Storage(settings.database_path)
        self.listings: list[ListingRuntime] = []
        for reference in settings.item_references:
            listing_settings = replace(settings, item_reference=reference)
            mapper = DeliveryFieldMapper(
                FieldOverrides(
                    login=settings.login_field_label,
                    password=settings.password_field_label,
                    comment=settings.comment_field_label,
                    combined=settings.combined_field_label,
                ),
                comment=settings.delivery_comment,
            )
            gateway = PlayerokGateway(listing_settings, mapper)
            self.listings.append(ListingRuntime(
                gateway=gateway,
                service=SaleService(listing_settings, self.storage, gateway),
            ))
        self.stop_event = threading.Event()

    def close(self) -> None:
        self.stop_event.set()
        self.storage.close()

    def import_accounts_from_file(self) -> int:
        credentials = load_accounts(self.settings.accounts_file, allow_empty=True)
        inserted = self.storage.import_accounts(credentials)
        if inserted:
            logger.info("Импорт аккаунтов: добавлено %s, всего в файле %s", inserted, len(credentials))
        return inserted

    def prepare(self, progress: Callable[[str], None] | None = None) -> list[ListingInfo]:
        report = progress or (lambda stage: None)
        report("Playerok: импорт локального запаса")
        self.import_accounts_from_file()
        primary_gateway = self.listings[0].gateway
        if getattr(primary_gateway, "account", None) is None:
            report("Playerok: подключение к профилю")
            primary_gateway.connect()

        # Сначала прочитать все лоты, затем привязать их аккаунты в базе.
        # Обработка продаж начинается только после подготовки всех лотов.
        snapshots: list[tuple[ListingInfo, Credential]] = []
        item_ids: set[str] = set()
        logins: set[str] = set()
        for number, runtime in enumerate(self.listings, 1):
            gateway = runtime.gateway
            if gateway is not primary_gateway:
                gateway.account = primary_gateway.account
            report(f"Playerok: загрузка лота №{number}")
            item = gateway.resolve_listing()
            if self.storage.ownership:
                self.storage.ownership.claim_lots('playerok', [str(item.id)])
            report(f"Playerok: проверка лота №{number} и стоимости премиума")
            info = gateway.validate_listing(item)
            if snapshots and snapshots[0][0].game_name != info.game_name:
                raise ListingValidationError('Объявления разных игр нужно добавить в разные группы через /group_new')
            if info.id in item_ids:
                raise ListingValidationError("Несколько ссылок ведут на один и тот же лот")
            item_ids.add(info.id)
            current = gateway.current_credential(item)
            if current is None:
                raise ListingValidationError(f"Не удалось прочитать данные выдачи лота №{number}")
            login = current.login.casefold()
            if login in logins:
                raise ListingValidationError("В нескольких лотах указан один аккаунт: задайте разные логины")
            logins.add(login)
            snapshots.append((info, current))

        missing: list[int] = []
        for number, (info, current) in enumerate(snapshots, 1):
            report(f"Playerok: сверка лота №{number} с базой")
            try:
                self._prepare_listing(info, current)
            except InventoryNotReadyError:
                missing.append(number)
        if missing:
            numbers = ", ".join(map(str, missing))
            raise InventoryNotReadyError(
                f"В базе не найдены игровые аккаунты из объявлений Playerok №{numbers}. "
                "Добавьте в accaunts.txt логины и пароли, уже указанные в данных для покупателя "
                "в этих объявлениях, в формате логин:пароль, каждый аккаунт с новой строки. "
                "Данные должны точно совпадать с объявлениями.",
                lot_numbers=missing,
            )
        started_at = datetime.now(timezone.utc).timestamp()
        for info, _ in snapshots:
            self.storage.bump_baseline(info.id, started_at)
        return [info for info, _ in snapshots]

    def _prepare_listing(self, info: ListingInfo, current: Credential) -> None:
        listed = self.storage.get_listed_account(info.id)
        if listed is None:
            if self.storage.exhausted_deal_id(info.id) or self.storage.unfinished_deals(info.id):
                logger.info("Запас лота %s исчерпан; бот ожидает пополнения очереди", info.id)
            else:
                self.storage.bootstrap_listing(info.id, current)
        elif listed.fingerprint != current.fingerprint:
            unfinished = self.storage.unfinished_deals(info.id)
            restock = self.storage.pending_restock(info.id)
            if (
                not any(deal.next_account_id == listed.id for deal in unfinished)
                and not (restock and restock.account_id == listed.id)
            ):
                raise ListingValidationError(
                    f"Данные лота {info.id} не совпадают с аккаунтом, отмеченным в базе"
                )
            logger.warning("Незавершённая замена данных лота %s будет продолжена", info.id)

    def resume_unfinished(self, runtime: ListingRuntime) -> None:
        from .models import PaidDeal

        for deal in self.storage.unfinished_deals(runtime.gateway.item_id):
            try:
                runtime.service.process(PaidDeal(deal.deal_id, deal.item_id, deal.chat_id))
            except Exception:
                continue

    def reconcile_once(self) -> None:
        try:
            self.import_accounts_from_file()
        except (AccountsFormatError, OSError) as exc:
            logger.warning("Импорт файла временно не выполнен: %s", exc)
        for runtime in self.listings:
            try:
                self.resume_unfinished(runtime)
                runtime.service.restore_stock()
                for deal in runtime.gateway.recent_paid_deals():
                    try:
                        runtime.service.process(deal)
                    except Exception:
                        continue
            except Exception as exc:
                logger.warning(
                    "Проверка лота %s временно не выполнена: %s. Повтор через %s с",
                    runtime.gateway.item_id,
                    type(exc).__name__,
                    self.settings.reconcile_seconds,
                )
            # Ошибка чтения сделок не должна отключать отдельный таймер поднятия.
            # Само поднятие повторно проверяет статус и данные лота перед оплатой.
            try:
                BumpService(self.settings, self.storage, runtime.gateway).check_due()
            except Exception as exc:
                reason = str(exc) if isinstance(exc, (ListingValidationError, PremiumPriceLimitError)) else type(exc).__name__
                code = getattr(exc, 'error_code', None)
                if isinstance(code, str) and code.isascii() and code.replace('_', '').isalnum() and len(code) < 80:
                    reason += ': ' + code
                self.storage.set_state_text('bump_error:' + runtime.gateway.item_id, reason)
                logger.warning('Поднятие лота %s: %s', runtime.gateway.item_id, type(exc).__name__)
        try:
            self.sync_finance()
        except Exception as exc:
            logger.warning('Сверка финансов Playerok: %s', type(exc).__name__)

    def sync_finance(self):
        """Перечитывать статусы известных продаж, включая поздние возвраты."""
        import time
        from .finance import Finance
        now = int(time.time())
        if self.storage.initialize_state_int('po_finance_due', 0) > now:
            return
        self.storage.set_state_int('po_finance_due', now + 300)
        offset = self.storage.initialize_state_int('po_finance_offset', 0)
        with self.storage._lock:
            rows = self.storage._connection.execute("SELECT order_id FROM revenue WHERE platform='playerok' ORDER BY order_id LIMIT 5 OFFSET ?", (offset,)).fetchall()
        for row in rows:
            deal = self.listings[0].gateway._require_account().get_deal(row[0])
            # Текущая цена переиспользуемого лота не восстанавливает цену старой продажи.
            Finance(self.storage).record('playerok', row[0],
                                         occurred_at=getattr(deal, 'created_at', None), status=deal.status.name.lower())
        self.storage.set_state_int('po_finance_offset', offset + len(rows) if len(rows) == 5 else 0)

    def _prepare_when_stock_available(self) -> list[ListingInfo] | None:
        last_wait_message: str | None = None
        while not self.stop_event.is_set():
            try:
                return self.prepare()
            except InventoryNotReadyError as exc:
                if str(exc) != last_wait_message:
                    logger.info("Ожидание первой загрузки аккаунтов: %s", exc)
                    last_wait_message = str(exc)
                self.stop_event.wait(self.settings.reconcile_seconds)
        return None

    def run(self) -> None:
        infos = self._prepare_when_stock_available()
        if infos is None:
            return
        for number, info in enumerate(infos, 1):
            logger.info(
                "Бот запущен для лота №%s: лот=%s, цена=%s ₽, премиум-публикация=%s ₽",
                number,
                info.id,
                info.price,
                info.premium_republish_price,
            )
        while not self.stop_event.is_set():
            self.reconcile_once()
            self.stop_event.wait(self.settings.reconcile_seconds)

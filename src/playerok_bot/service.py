from __future__ import annotations

import logging
import threading

from .config import Settings
from .gateway import PlayerokGateway
from .models import PaidDeal
from .storage import Storage

logger = logging.getLogger(__name__)


class SaleService:
    """Идемпотентная обработка продажи и восстановление премиум-лота."""

    def __init__(self, settings: Settings, storage: Storage, gateway: PlayerokGateway):
        self.settings = settings
        self.storage = storage
        self.gateway = gateway
        self._lock = threading.Lock()

    def process(self, paid_deal: PaidDeal) -> None:
        if paid_deal.item_id != self.gateway.item_id:
            return

        with self._lock:
            try:
                record = self.storage.prepare_deal(
                    paid_deal.id, paid_deal.item_id, paid_deal.chat_id
                )
                if record.completed_at:
                    return

                if self.settings.send_courtesy_message and not record.buyer_notified_at:
                    if not record.chat_id:
                        raise RuntimeError("Для сделки не найден чат покупателя")
                    self.gateway.send_courtesy_message(record.chat_id)
                    record = self.storage.mark_deal(record.deal_id, "buyer_notified_at")

                if self.settings.auto_confirm_deal and not record.deal_confirmed_at:
                    self.gateway.confirm_deal_sent(record.deal_id)
                    record = self.storage.mark_deal(record.deal_id, "deal_confirmed_at")

                next_account = self.storage.get_account(record.next_account_id)
                if next_account is None:
                    self.storage.mark_deal(record.deal_id, "completed_at")
                    logger.warning(
                        "Сделка %s обработана. Запас закончился; лот ожидает новых аккаунтов",
                        record.deal_id,
                    )
                    return

                if not record.item_updated_at:
                    if not self.gateway.delivery_matches(next_account.credential):
                        self.gateway.update_delivery(next_account.credential)
                    record = self.storage.mark_deal(record.deal_id, "item_updated_at")

                if not record.item_published_at:
                    if not self.gateway.is_active():
                        premium_price = self.gateway.publish_premium()
                        logger.info(
                            "Лот %s восстановлен за %s ₽ после сделки %s",
                            paid_deal.item_id,
                            premium_price,
                            paid_deal.id,
                        )
                    record = self.storage.mark_deal(record.deal_id, "item_published_at")

                self.storage.mark_deal(record.deal_id, "completed_at")
            except Exception as exc:
                self.storage.mark_error(paid_deal.id, type(exc).__name__)
                logger.exception(
                    "Обработка сделки %s остановлена на этапе %s",
                    paid_deal.id,
                    type(exc).__name__,
                )
                raise

    def restore_stock(self) -> None:
        """Восстановить проданный лот после пополнения очереди."""
        with self._lock:
            item_id = self.gateway.item_id
            record = self.storage.pending_restock(item_id)
            if record is None:
                if self.storage.get_listed_account(item_id):
                    return
                if not self.storage.exhausted_deal_id(item_id):
                    return
                if self.storage.counts()["queued"] == 0:
                    return
                if not self.gateway.is_sold():
                    return
                record = self.storage.reserve_restock(item_id)
                if record is None:
                    return

            try:
                account = self.storage.get_account(record.account_id)
                if account is None:
                    raise RuntimeError("Для восстановления не найден зарезервированный аккаунт")
                if not record.item_updated_at:
                    if not self.gateway.delivery_matches(account.credential):
                        self.gateway.update_delivery(account.credential)
                    record = self.storage.mark_restock(record.source_deal_id, "item_updated_at")

                if not record.item_published_at:
                    if not self.gateway.is_active():
                        premium_price = self.gateway.publish_premium()
                        logger.info(
                            "Лот %s восстановлен после пополнения за %s ₽",
                            item_id,
                            premium_price,
                        )
                    record = self.storage.mark_restock(record.source_deal_id, "item_published_at")

                self.storage.mark_restock(record.source_deal_id, "completed_at")
            except Exception as exc:
                self.storage.mark_restock_error(record.source_deal_id, type(exc).__name__)
                logger.exception("Автовосстановление лота %s временно не выполнено", item_id)
                raise

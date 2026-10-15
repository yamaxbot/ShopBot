from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .config import Settings
from .errors import ListingValidationError
from .gateway import PlayerokGateway
from .storage import Storage

logger = logging.getLogger(__name__)
MOSCOW = timezone(timedelta(hours=3), name="MSK")


def bump_time_allowed(now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("Для расписания нужно время с часовым поясом")
    return now.astimezone(MOSCOW).hour >= 12


class BumpService:
    """Плановые поднятия активного лота с отдельным сохранённым таймером."""

    def __init__(self, settings: Settings, storage: Storage, gateway: PlayerokGateway):
        self.settings = settings
        self.storage = storage
        self.gateway = gateway

    def check_due(self, now: datetime | None = None) -> None:
        if not self.settings.auto_bump_enabled:
            return
        clock = (lambda: now) if now is not None else (lambda: datetime.now(timezone.utc))
        current = clock()
        if not bump_time_allowed(current):
            return
        item_id = self.gateway.item_id
        baseline = self.storage.bump_baseline(item_id, current.timestamp())
        if current.timestamp() - baseline < self.settings.auto_bump_seconds:
            return
        listed = self.storage.get_listed_account(item_id)
        if (
            listed is None
            or self.storage.unfinished_deals(item_id)
            or self.storage.pending_restock(item_id)
        ):
            return
        if not self.gateway.delivery_matches(listed.credential):
            raise ListingValidationError(f"Данные лота {item_id} изменены; плановое поднятие отменено")
        plan = self.gateway.plan_premium_bump()
        if plan is None:
            return
        priority_id, price = plan
        # Запрос цены мог закончиться уже после полуночи.
        current = clock()
        if not bump_time_allowed(current):
            return
        operation_id = self.storage.claim_bump(
            item_id, current.timestamp(), self.settings.auto_bump_seconds, price
        )
        if operation_id is None:
            return
        local = current.astimezone(MOSCOW)
        allowed_until = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        try:
            actual_price = self.gateway.bump_premium(priority_id, allowed_until=allowed_until)
        except Exception as exc:
            # После потери ответа результат платного запроса неизвестен.
            # Повторная плановая попытка допускается только через полный интервал.
            self.storage.finish_bump(operation_id, "uncertain", type(exc).__name__)
            logger.exception("Ответ на поднятие лота %s не подтверждён; частые повторы исключены", item_id)
            raise
        self.storage.finish_bump(
            operation_id, "success" if actual_price is not None else "skipped", price=actual_price
        )
        if actual_price is not None:
            logger.info("Лот %s поднят после простоя за %s ₽; время МСК %s", item_id, actual_price, local.isoformat())


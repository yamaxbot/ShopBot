from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .errors import ListingValidationError, PremiumPriceLimitError
from .fields import DeliveryFieldMapper
from .models import Credential, PaidDeal

logger = logging.getLogger(__name__)


def _ensure_playerok_certificate() -> None:
    """Восстановить CA bundle, который не попадает в wheel PlayerokAPI."""
    import certifi
    import playerokapi

    package_file = getattr(playerokapi, "__file__", None)
    if not package_file:
        raise FileNotFoundError("Не удалось определить каталог пакета playerokapi")

    destination = Path(package_file).resolve().with_name("cacert.pem")
    if destination.is_file():
        return

    shutil.copyfile(certifi.where(), destination)
    logger.info("Восстановлен отсутствующий CA bundle PlayerokAPI")


@dataclass(frozen=True, slots=True)
class ListingInfo:
    id: str
    name: str
    price: int
    status: str
    priority: str
    game_name: str
    keep_in_sale: bool
    premium_republish_price: int
    field_labels: tuple[str, ...]


class PlayerokGateway:
    """Небольшой адаптер над неофициальным клиентом Playerok."""

    def __init__(self, settings: Settings, field_mapper: DeliveryFieldMapper):
        self.settings = settings
        self.field_mapper = field_mapper
        self.account = None
        self._resolved_item_id: str | None = None
        self._resolved_item_slug: str | None = None

    def connect(self) -> None:
        _ensure_playerok_certificate()
        from playerokapi.account import Account

        self.account = Account(
            cookies=self.settings.cookies,
            user_agent=self.settings.user_agent,
        ).get()
        logger.info("Соединение с Playerok установлено для профиля @%s", self.account.username)

    def _require_account(self):
        if self.account is None:
            raise RuntimeError("Сначала вызовите connect()")
        return self.account

    @staticmethod
    def _reference_parts(reference: str) -> tuple[str | None, str | None]:
        value = reference.strip()
        if value.startswith(("http://", "https://")):
            parsed = urlparse(value)
            slug = parsed.path.rstrip("/").split("/")[-1]
            return None, slug or None
        return value, value

    def resolve_listing(self):
        account = self._require_account()
        raw_id, slug = self._reference_parts(self.settings.item_reference)
        item = None
        if raw_id:
            try:
                item = account.get_item(id=raw_id)
            except Exception:
                item = None
        if item is None and slug:
            item = account.get_item(slug=slug)
        if item is None:
            raise ListingValidationError("Лот из PLAYEROK_ITEM не найден")
        self._resolved_item_id = str(item.id)
        self._resolved_item_slug = str(getattr(item, "slug", "") or "").strip() or None
        return item

    @property
    def item_id(self) -> str:
        if self._resolved_item_id is None:
            raise RuntimeError("Лот ещё не был загружен")
        return self._resolved_item_id

    def get_listing(self):
        return self._require_account().get_item(id=self.item_id)

    def _premium_status(self, item):
        from playerokapi.enums import PriorityTypes

        statuses = self._require_account().get_item_priority_statuses(item.id, item.price)
        premium = next(
            (status for status in statuses if status.type is PriorityTypes.PREMIUM),
            None,
        )
        if premium is None:
            raise ListingValidationError("Для этого лота Playerok не вернул премиум-статус")
        if premium.price > self.settings.premium_price_limit_rub:
            raise PremiumPriceLimitError(
                f"Премиум стоит {premium.price} ₽, лимит — "
                f"{self.settings.premium_price_limit_rub} ₽"
            )
        return premium

    def validate_listing(self, item=None) -> ListingInfo:
        from playerokapi.enums import PriorityTypes

        item = item or self.get_listing()
        if getattr(item.user, "id", None) != self._require_account().id:
            raise ListingValidationError("PLAYEROK_ITEM указывает на чужой лот")
        if getattr(item, "priority", None) is not PriorityTypes.PREMIUM:
            raise ListingValidationError("Лот должен быть опубликован как премиум")
        keep_in_sale = bool(getattr(item, "keep_in_sale", False))
        if keep_in_sale:
            raise ListingValidationError(
                "Отключите у лота опцию «Оставлять в продаже»: она может повторно "
                "выставить старые данные раньше бота"
            )
        fields = list(getattr(item, "data_fields", None) or [])
        self.field_mapper.validate(fields)
        premium = self._premium_status(item)
        game_name = getattr(getattr(item, "game", None), "name", "") or ""
        if "car parking multiplayer 2" not in game_name.casefold():
            raise ListingValidationError(
                f"Лот относится к игре «{game_name or 'не определена'}», "
                "ожидалась Car Parking Multiplayer 2"
            )
        return ListingInfo(
            id=str(item.id),
            name=item.name,
            price=int(item.price),
            status=getattr(getattr(item, "status", None), "name", "UNKNOWN"),
            priority=getattr(getattr(item, "priority", None), "name", "UNKNOWN"),
            game_name=game_name,
            keep_in_sale=keep_in_sale,
            premium_republish_price=int(premium.price),
            field_labels=tuple(field.label for field in fields),
        )

    def current_credential(self, item=None) -> Credential | None:
        item = item or self.get_listing()
        fields = list(getattr(item, "data_fields", None) or [])
        return self.field_mapper.extract(fields)

    def update_delivery(self, credential: Credential) -> None:
        item = self.get_listing()
        fields = list(getattr(item, "data_fields", None) or [])
        updated_fields = self.field_mapper.apply(fields, credential)
        self._require_account().update_item(id=self.item_id, data_fields=updated_fields)

    def delivery_matches(self, credential: Credential) -> bool:
        current = self.current_credential()
        return current is not None and current.fingerprint == credential.fingerprint

    def is_active(self) -> bool:
        from playerokapi.enums import ItemStatuses

        return getattr(self.get_listing(), "status", None) is ItemStatuses.APPROVED

    def is_sold(self) -> bool:
        from playerokapi.enums import ItemStatuses

        return getattr(self.get_listing(), "status", None) is ItemStatuses.SOLD

    def publish_premium(self) -> int:
        item = self.get_listing()
        premium = self._premium_status(item)
        self._require_account().publish_item(item.id, premium.id)
        return int(premium.price)

    def plan_premium_bump(self) -> tuple[str, int] | None:
        from playerokapi.enums import ItemStatuses

        item = self.get_listing()
        if getattr(item, "status", None) is not ItemStatuses.APPROVED:
            return None
        premium = self._premium_status(item)
        return str(premium.id), int(premium.price)

    def bump_premium(self, priority_id: str, *, allowed_until: datetime) -> int | None:
        from playerokapi.enums import ItemStatuses

        # Покупка могла произойти после получения цены поднятия.
        item = self.get_listing()
        if getattr(item, "status", None) is not ItemStatuses.APPROVED:
            return None
        premium = self._premium_status(item)
        if str(premium.id) != priority_id:
            return None
        if datetime.now(timezone.utc) >= allowed_until:
            return None
        self._require_account().increase_item_priority_status(self.item_id, priority_id)
        return int(premium.price)

    def confirm_deal_sent(self, deal_id: str) -> None:
        from playerokapi.enums import ItemDealStatuses

        account = self._require_account()
        deal = account.get_deal(deal_id)
        if deal.status in {
            ItemDealStatuses.SENT,
            ItemDealStatuses.CONFIRMED,
            ItemDealStatuses.CONFIRMED_AUTOMATICALLY,
        }:
            return
        if deal.status not in {ItemDealStatuses.PAID, ItemDealStatuses.PENDING}:
            raise ListingValidationError(
                f"Сделка {deal_id} имеет неподходящий статус {deal.status.name}"
            )
        account.update_deal(deal_id, ItemDealStatuses.SENT)

    def send_courtesy_message(self, chat_id: str) -> None:
        self._require_account().send_message(chat_id, text=self.settings.courtesy_message)

    def _is_target_item(self, item) -> bool:
        if item is None:
            return False
        if str(getattr(item, "id", "") or "") == self.item_id:
            return True
        deal_slug = str(getattr(item, "slug", "") or "").strip()
        return bool(
            deal_slug
            and self._resolved_item_slug
            and deal_slug.casefold() == self._resolved_item_slug.casefold()
        )

    def recent_paid_deals(self) -> list[PaidDeal]:
        from playerokapi.enums import ItemDealDirections, ItemDealStatuses

        page = self._require_account().get_deals(
            statuses=[ItemDealStatuses.PAID, ItemDealStatuses.PENDING],
            direction=ItemDealDirections.OUT,
            count=24,
        )
        result: list[PaidDeal] = []
        for deal in page.deals:
            if not self._is_target_item(getattr(deal, "item", None)):
                continue
            chat_id = getattr(getattr(deal, "chat", None), "id", None)
            result.append(PaidDeal(str(deal.id), self.item_id, chat_id))
        return result

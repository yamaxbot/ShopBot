from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256


@dataclass(frozen=True, slots=True)
class Credential:
    login: str
    password: str

    @property
    def fingerprint(self) -> str:
        payload = f"{self.login}\0{self.password}".encode("utf-8")
        return sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredAccount:
    id: int
    fingerprint: str
    login: str
    password: str
    status: str
    listed_item_id: str | None
    sold_deal_id: str | None

    @property
    def credential(self) -> Credential:
        return Credential(login=self.login, password=self.password)


@dataclass(frozen=True, slots=True)
class DealRecord:
    deal_id: str
    item_id: str
    chat_id: str | None
    sold_account_id: int | None
    next_account_id: int | None
    inventory_committed_at: str | None
    buyer_notified_at: str | None
    deal_confirmed_at: str | None
    item_updated_at: str | None
    item_published_at: str | None
    completed_at: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class PaidDeal:
    id: str
    item_id: str
    chat_id: str | None
    gross: int | float | None = None
    occurred_at: str | None = None
    status: str = 'paid'


@dataclass(frozen=True, slots=True)
class RestockRecord:
    source_deal_id: str
    item_id: str
    account_id: int
    item_updated_at: str | None
    item_published_at: str | None
    completed_at: str | None
    error_code: str | None

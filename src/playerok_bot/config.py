from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigurationError


def _as_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "да"}:
        return True
    if normalized in {"0", "false", "no", "off", "нет"}:
        return False
    raise ConfigurationError(f"{name} должен иметь значение true или false")


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    cookies: str
    user_agent: str
    item_reference: str
    accounts_file: Path
    database_path: Path
    log_path: Path
    delivery_comment: str
    premium_price_limit_rub: int
    auto_confirm_deal: bool
    send_courtesy_message: bool
    courtesy_message: str
    reconcile_seconds: int
    login_field_label: str | None
    password_field_label: str | None
    comment_field_label: str | None
    combined_field_label: str | None
    additional_item_references: tuple[str, ...] = ()
    auto_bump_enabled: bool = True
    auto_bump_seconds: int = 7200

    @property
    def item_references(self) -> tuple[str, ...]:
        return (self.item_reference, *self.additional_item_references)

    @classmethod
    def load(cls, project_root: Path | None = None) -> "Settings":
        root = (project_root or Path.cwd()).resolve()
        load_dotenv(root / ".env")

        cookies = os.getenv("PLAYEROK_COOKIES", "").strip()
        token = os.getenv("PLAYEROK_TOKEN", "").strip()
        if not cookies and token:
            cookies = f"token={token}"

        item_reference = os.getenv("PLAYEROK_ITEM", "").strip()
        additional_items = tuple(
            value for name in ("PLAYEROK_ITEM_2", "PLAYEROK_ITEM_3")
            if (value := os.getenv(name, "").strip())
        )
        normalized_items = [
            reference.rstrip("/") for reference in (item_reference, *additional_items)
            if reference
        ]
        if len(set(normalized_items)) != len(normalized_items):
            raise ConfigurationError("Ссылки PLAYEROK_ITEM, PLAYEROK_ITEM_2 и PLAYEROK_ITEM_3 должны различаться")
        user_agent = os.getenv("PLAYEROK_USER_AGENT", "").strip()

        missing: list[str] = []
        if not cookies:
            missing.append("PLAYEROK_COOKIES или PLAYEROK_TOKEN")
        if not user_agent:
            missing.append("PLAYEROK_USER_AGENT")
        if not item_reference:
            missing.append("PLAYEROK_ITEM")
        if missing:
            raise ConfigurationError("Не заполнены параметры: " + ", ".join(missing))

        def resolve_path(name: str, default: str) -> Path:
            value = Path(os.getenv(name, default).strip())
            return value if value.is_absolute() else root / value

        try:
            premium_limit = int(os.getenv("PREMIUM_PRICE_LIMIT_RUB", "50"))
            reconcile_seconds = int(os.getenv("RECONCILE_SECONDS", "15"))
            auto_bump_seconds = int(os.getenv("AUTO_BUMP_SECONDS", "7200"))
        except ValueError as exc:
            raise ConfigurationError(
                "PREMIUM_PRICE_LIMIT_RUB, RECONCILE_SECONDS и AUTO_BUMP_SECONDS должны быть целыми числами"
            ) from exc

        if premium_limit < 0:
            raise ConfigurationError("PREMIUM_PRICE_LIMIT_RUB не может быть отрицательным")
        if reconcile_seconds < 10:
            raise ConfigurationError("RECONCILE_SECONDS должен быть не меньше 10")
        if auto_bump_seconds < 7200:
            raise ConfigurationError("AUTO_BUMP_SECONDS должен быть не меньше 7200 (2 часа)")

        return cls(
            project_root=root,
            cookies=cookies,
            user_agent=user_agent,
            item_reference=item_reference,
            accounts_file=resolve_path("ACCOUNTS_FILE", "accaunts.txt"),
            database_path=resolve_path("DATABASE_PATH", "data/playerok_bot.sqlite3"),
            log_path=resolve_path("LOG_PATH", "logs/playerok_bot.log"),
            delivery_comment=os.getenv("DELIVERY_COMMENT", "приятного пользования").strip(),
            premium_price_limit_rub=premium_limit,
            auto_confirm_deal=_as_bool("AUTO_CONFIRM_DEAL", True),
            send_courtesy_message=_as_bool("SEND_COURTESY_MESSAGE", False),
            courtesy_message=os.getenv("COURTESY_MESSAGE", "приятного пользования").strip(),
            reconcile_seconds=reconcile_seconds,
            login_field_label=_optional("LOGIN_FIELD_LABEL"),
            password_field_label=_optional("PASSWORD_FIELD_LABEL"),
            comment_field_label=_optional("COMMENT_FIELD_LABEL"),
            combined_field_label=_optional("COMBINED_FIELD_LABEL"),
            additional_item_references=additional_items,
            auto_bump_enabled=_as_bool("AUTO_BUMP_ENABLED", True),
            auto_bump_seconds=auto_bump_seconds,
        )

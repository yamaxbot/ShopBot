from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Protocol

from .errors import FieldMappingError
from .models import Credential


class DataField(Protocol):
    label: str
    value: str | None


def _normalized(value: str) -> str:
    decomposed = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in decomposed if character.isalnum())


def _matches(label: str, explicit: str | None, keywords: Iterable[str]) -> bool:
    normalized = _normalized(label)
    if explicit:
        return normalized == _normalized(explicit)
    return any(keyword in normalized for keyword in keywords)


@dataclass(frozen=True, slots=True)
class FieldOverrides:
    login: str | None = None
    password: str | None = None
    comment: str | None = None
    combined: str | None = None


class DeliveryFieldMapper:
    LOGIN_KEYWORDS = ("логин", "login", "email", "почт", "username")
    PASSWORD_KEYWORDS = ("парол", "password")
    COMMENT_KEYWORDS = ("комментар", "comment")
    COMBINED_KEYWORDS = ("данныетовара", "данныеаккаунта", "данныедлявхода", "аккаунт")

    def __init__(self, overrides: FieldOverrides, comment: str):
        self.overrides = overrides
        self.comment = comment

    def _classify(self, fields: list[DataField]) -> dict[str, DataField | None]:
        result: dict[str, DataField | None] = {
            "login": None,
            "password": None,
            "comment": None,
            "combined": None,
        }
        for field in fields:
            if _matches(field.label, self.overrides.login, self.LOGIN_KEYWORDS):
                result["login"] = field
            elif _matches(field.label, self.overrides.password, self.PASSWORD_KEYWORDS):
                result["password"] = field
            elif _matches(field.label, self.overrides.comment, self.COMMENT_KEYWORDS):
                result["comment"] = field
            elif _matches(field.label, self.overrides.combined, self.COMBINED_KEYWORDS):
                result["combined"] = field
        return result

    def validate(self, fields: list[DataField]) -> None:
        mapping = self._classify(fields)
        has_separate = mapping["login"] is not None and mapping["password"] is not None
        if not has_separate and mapping["combined"] is None:
            labels = ", ".join(field.label for field in fields) or "нет полей"
            raise FieldMappingError(
                "Не найдены поля логина и пароля. Доступные подписи: " + labels
            )
        if mapping["comment"] is None and mapping["combined"] is None:
            labels = ", ".join(field.label for field in fields) or "нет полей"
            raise FieldMappingError(
                "Не найдено поле комментария. Укажите COMMENT_FIELD_LABEL в .env. "
                "Доступные подписи: " + labels
            )

    def apply(self, fields: list[DataField], credential: Credential) -> list[DataField]:
        self.validate(fields)
        mapping = self._classify(fields)
        if mapping["login"] is not None and mapping["password"] is not None:
            mapping["login"].value = credential.login
            mapping["password"].value = credential.password
            if mapping["comment"] is not None:
                mapping["comment"].value = self.comment
        else:
            mapping["combined"].value = (
                f"Логин: {credential.login}\n"
                f"Пароль: {credential.password}\n"
                f"Комментарий: {self.comment}"
            )
        return fields

    def extract(self, fields: list[DataField]) -> Credential | None:
        mapping = self._classify(fields)
        if mapping["login"] is not None and mapping["password"] is not None:
            login = (mapping["login"].value or "").strip()
            password = (mapping["password"].value or "").strip()
            return Credential(login, password) if login and password else None

        combined = mapping["combined"]
        if combined is None or not combined.value:
            return None
        login_match = re.search(r"(?im)^\s*(?:логин|login)\s*[:=]\s*(\S+)\s*$", combined.value)
        password_match = re.search(
            r"(?im)^\s*(?:пароль|password)\s*[:=]\s*(\S+)\s*$", combined.value
        )
        if not login_match or not password_match:
            return None
        return Credential(login_match.group(1), password_match.group(1))


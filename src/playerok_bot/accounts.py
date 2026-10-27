from __future__ import annotations

from pathlib import Path
import re

from .errors import AccountsFormatError
from .models import Credential


_LOGIN = r"(?:логин|login|почта|email|e-mail)"
_PASSWORD = r"(?:пароль|password|pass)"
_LABELED = re.compile(rf"^{_LOGIN}\s*[:=]?\s*(\S+)\s+{_PASSWORD}\s*[:=]?\s*(.+)$", re.I)
_LOGIN_ONLY = re.compile(rf"^{_LOGIN}\s*[:=]?\s*(\S+)\s*$", re.I)
_PASSWORD_ONLY = re.compile(rf"^{_PASSWORD}\s*[:=]?\s*(.+)$", re.I)
# Ссылка «https://…» иначе разделилась бы по первому двоеточию на логин «https» и пароль «//…».
# Ищем по всей строке: перед ссылкой может стоять скобка, кавычка или маркер списка.
# Схемы перечислены явно, чтобы пароль вида «//…» после обычного логина не считался ссылкой.
_LINK = re.compile(r"(?:https?|ftp|tg)://", re.I)


def _reject_link(text: str, line_number: int) -> None:
    if _LINK.search(text):
        raise AccountsFormatError(f"Строка {line_number} содержит ссылку. Ссылки не принимаются как логин и пароль")


def _credential(login: str, password: str, line_number: int) -> Credential:
    if not login or not password:
        raise AccountsFormatError(f"Строка {line_number}: логин или пароль пуст")
    return Credential(login=login, password=password)


def parse_account_line(line: str, line_number: int) -> Credential:
    text = line.strip()
    if not text or text.startswith("#"):
        raise AccountsFormatError(f"Строка {line_number} не содержит аккаунт")
    _reject_link(text, line_number)

    labeled = _LABELED.fullmatch(text)
    if labeled:
        login, password = labeled.groups()
    else:
        separated = re.match(r"^([^\s:|;]+)\s*[:|;\t]\s*(.+)$", text)
        if separated:
            login, password = separated.groups()
        elif len(text.split()) == 2:
            login, password = text.split()
        else:
            raise AccountsFormatError(
                f"Строка {line_number}: ожидается «Логин VALUE Пароль VALUE» "
                "или два значения, разделённые :, пробелом, |, ; либо табуляцией"
            )

    return _credential(login, password, line_number)


def parse_accounts_text(text: str) -> list[Credential]:
    """Разобрать сообщение целиком до импорта, включая двухстрочные блоки."""
    result: list[Credential] = []
    pending_login: str | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line in {"```", "```text", "```txt"}:
            continue
        _reject_link(line, number)
        if pending_login is not None:
            password = _PASSWORD_ONLY.fullmatch(line)
            if password is None:
                raise AccountsFormatError(f"Строка {number}: после логина ожидается «Пароль: VALUE»")
            result.append(_credential(pending_login, password.group(1).strip(), number))
            pending_login = None
            continue
        login_only = _LOGIN_ONLY.fullmatch(line)
        if login_only:
            pending_login = login_only.group(1)
        else:
            result.append(parse_account_line(line, number))
    if pending_login is not None:
        raise AccountsFormatError("Для последнего логина не указан пароль")
    if not result:
        raise AccountsFormatError("Аккаунты не найдены")
    return result


def load_accounts(path: Path, *, allow_empty: bool = False) -> list[Credential]:
    if not path.is_file():
        raise AccountsFormatError(f"Файл аккаунтов не найден: {path}")

    credentials: list[Credential] = []
    fingerprints: set[str] = set()
    for number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        credential = parse_account_line(raw_line, number)
        if credential.fingerprint in fingerprints:
            raise AccountsFormatError(f"Строка {number}: этот аккаунт уже указан выше")
        fingerprints.add(credential.fingerprint)
        credentials.append(credential)

    if not credentials and not allow_empty:
        raise AccountsFormatError("В файле аккаунтов нет ни одной записи")
    return credentials

from __future__ import annotations

from pathlib import Path
import re

from .errors import AccountsFormatError
from .models import Credential


def validate_credential(credential: Credential) -> None:
    """Validate at storage and delivery boundaries, including legacy database rows."""
    for value in (credential.login, credential.password):
        if not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
            raise AccountsFormatError('Ожидается один аккаунт: логин и пароль без пробелов и переносов. Список отправляйте отдельными строками.')
    if len(credential.login) > 320 or len(credential.password) > 512 or re.search(
        r'[^\s:;|@]+@[^\s:;|@]+[.:][^\s:;|@]*:', credential.password
    ) or re.search(r'\\[nr]', credential.password):
        raise AccountsFormatError('Данные похожи на склеенный список аккаунтов. Выдача заблокирована; проверьте исходный файл.')


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
    credential = Credential(login=login, password=password)
    validate_credential(credential)
    return credential


def parse_account_line(line: str, line_number: int) -> Credential:
    text = line.strip()
    if not text or text.startswith("#"):
        raise AccountsFormatError(f"Строка {line_number} не содержит аккаунт")
    _reject_link(text, line_number)

    labeled = _LABELED.fullmatch(text)
    if labeled:
        login, password = labeled.groups()
    else:
        separated = re.fullmatch(r"([^\s:|;]+)\s*[:|;\t]\s*(.+)", text)
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
            # A visual wrap in Telegram is not a newline. Only split a list when
            # every whitespace-delimited token is an explicit login:password pair.
            parts = line.split()
            if len(parts) > 1 and all(re.fullmatch(r'[^\s:|;]+:[^\s]+', part) for part in parts):
                result.extend(parse_account_line(part, number) for part in parts)
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
    text = path.read_text(encoding="utf-8-sig")
    if not any(line.strip() and not line.lstrip().startswith('#') for line in text.splitlines()):
        if allow_empty:
            return []
        raise AccountsFormatError("В файле аккаунтов нет ни одной записи")
    for number, credential in enumerate(parse_accounts_text(text), 1):
        if credential.fingerprint in fingerprints:
            raise AccountsFormatError(f"Строка {number}: этот аккаунт уже указан выше")
        fingerprints.add(credential.fingerprint)
        credentials.append(credential)

    if not credentials and not allow_empty:
        raise AccountsFormatError("В файле аккаунтов нет ни одной записи")
    return credentials

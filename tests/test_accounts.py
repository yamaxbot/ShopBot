from pathlib import Path

import pytest

from playerok_bot.accounts import load_accounts, parse_account_line
from playerok_bot.errors import AccountsFormatError


def test_parses_current_labeled_format() -> None:
    credential = parse_account_line("Логин user@example.test Пароль p@ss-word", 1)
    assert credential.login == "user@example.test"
    assert credential.password == "p@ss-word"


def test_parses_colon_format_with_special_characters() -> None:
    credential = parse_account_line("user@example.test:p@ss^word&(value)", 1)
    assert credential.login == "user@example.test"
    assert credential.password == "p@ss^word&(value)"


def test_parses_multiple_formats_from_file(tmp_path: Path) -> None:
    path = tmp_path / "accounts.txt"
    path.write_text(
        "first@example.test:first-pass\n"
        "Логин second@example.test пароль second-pass",
        encoding="utf-8",
    )
    credentials = load_accounts(path)
    assert [item.login for item in credentials] == [
        "first@example.test",
        "second@example.test",
    ]


def test_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "accounts.txt"
    path.write_text("one@example.test pass\none@example.test pass\n", encoding="utf-8")
    with pytest.raises(AccountsFormatError, match="уже указан"):
        load_accounts(path)


def test_empty_file_is_allowed_while_waiting_for_stock(tmp_path: Path) -> None:
    path = tmp_path / "accounts.txt"
    path.write_text("# Аккаунты пока не добавлены\n", encoding="utf-8")

    assert load_accounts(path, allow_empty=True) == []

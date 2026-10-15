from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

from ..errors import ConfigurationError


@dataclass(frozen=True)
class TelegramSettings:
    token: str
    data_dir: Path
    max_workers: int = 20
    allowed_user_ids: frozenset[int] = frozenset()
    proxy: str | None = None

    @classmethod
    def load(cls, root: Path | None = None, *, require_token=True):
        root = (root or Path.cwd()).resolve()
        load_dotenv(root / ".env")
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if require_token and not token:
            raise ConfigurationError("Заполните TELEGRAM_BOT_TOKEN")
        path = Path(os.getenv("TELEGRAM_DATA_DIR", "data/telegram"))
        if not path.is_absolute():
            path = root / path
        try:
            workers = int(os.getenv("TELEGRAM_MAX_WORKERS", "20"))
            ids = frozenset(int(part.strip()) for part in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if part.strip())
        except ValueError as exc:
            raise ConfigurationError("Telegram ID и TELEGRAM_MAX_WORKERS должны быть числами") from exc
        if workers < 1 or any(uid <= 0 for uid in ids):
            raise ConfigurationError("Некорректные Telegram ID или число процессов")
        return cls(token, path.resolve(), workers, ids, os.getenv('TELEGRAM_PROXY', '').strip() or None)

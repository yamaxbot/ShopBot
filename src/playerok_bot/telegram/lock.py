from pathlib import Path

from ..errors import ConfigurationError


class HostLock:
    """Разрешить только один Telegram-хост для каталога пользователей."""

    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "host.lock"
        self.file = None

    def __enter__(self):
        try:
            self.file = self.path.open("a+b")
            self.file.seek(0)
            if self.file.read(1) == b"":
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if self.file:
                self.file.close()
            self.file = None
            raise ConfigurationError("Telegram-хост уже запущен. Остановите его перед повторным запуском или переносом") from exc
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
            self.file = None

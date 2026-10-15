from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from aiogram.types import ReplyKeyboardRemove

from ..errors import ConfigurationError
from .profiles import Profile, Profiles

logger = logging.getLogger(__name__)
ENGINE_KEYS = {
    "ACCOUNTS_FILE", "DATABASE_PATH", "LOG_PATH", "DELIVERY_COMMENT",
    "PREMIUM_PRICE_LIMIT_RUB", "AUTO_CONFIRM_DEAL", "SEND_COURTESY_MESSAGE",
    "COURTESY_MESSAGE", "RECONCILE_SECONDS", "LOGIN_FIELD_LABEL",
    "PASSWORD_FIELD_LABEL", "COMMENT_FIELD_LABEL", "COMBINED_FIELD_LABEL",
    "AUTO_BUMP_ENABLED", "AUTO_BUMP_SECONDS",
}


def process_options():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}


def worker_environment(profile: Profile, directory: Path) -> dict[str, str]:
    # Исключить как глобальную авторизацию, так и любые настройки чужого профиля.
    environment = {
        key: value for key, value in os.environ.items()
        if key not in ENGINE_KEYS and not key.startswith(("PLAYEROK_", "TELEGRAM_", "FUNPAY_", "PLATFORM_"))
    }
    config = profile.config
    items = config["items"]
    environment.update({
        "PLAYEROK_COOKIES": config["cookies"], "PLAYEROK_TOKEN": "",
        "PLAYEROK_USER_AGENT": config["user_agent"],
        "PLAYEROK_ITEM": items[0] if items else "", "PLAYEROK_ITEM_2": "", "PLAYEROK_ITEM_3": "",
        "ACCOUNTS_FILE": str(directory / "accaunts.txt"),
        "DATABASE_PATH": str(directory / "inventory.sqlite3"),
        "LOG_PATH": str(directory / "playerok.log"),
        "PREMIUM_PRICE_LIMIT_RUB": str(config["price_limit"]),
        "AUTO_BUMP_ENABLED": str(config["bump"]).lower(), "AUTO_BUMP_SECONDS": "7200",
        "AUTO_CONFIRM_DEAL": "true", "SEND_COURTESY_MESSAGE": "false",
        "DELIVERY_COMMENT": "приятного пользования", "RECONCILE_SECONDS": "15",
        "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1",
        "FUNPAY_GOLDEN_KEY": config.get('funpay_key', ''),
        "FUNPAY_USER_AGENT": config.get('funpay_agent', ''),
        "FUNPAY_LOTS": json.dumps(config.get('funpay_items', [])),
        "FUNPAY_OWNER_ID": profile.funpay_id or '',
        "PLAYEROK_OWNER_ID": profile.playerok_id or '',
        "PLATFORM_INVENTORY": 'true',
    })
    for index, item in enumerate(items[1:], 2):
        environment[f"PLAYEROK_ITEM_{index}"] = item
    return environment


class Workers:
    def __init__(self, profiles: Profiles, max_workers: int):
        self.profiles = profiles
        self.max_workers = max_workers
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self.lock = asyncio.Lock()
        self.revisions: dict[int, int] = {}
        self.funpay_alerts: dict[int, set[int]] = {}

    def environment(self, profile: Profile):
        directory = self.profiles.user_dir(profile.user_id)
        accounts = directory / "accaunts.txt"
        if not accounts.exists():
            accounts.write_text("# Аккаунты загружаются в личную SQLite-базу через Telegram\n", encoding="utf-8")
        return directory, worker_environment(profile, directory)

    async def probe(self, profile: Profile, mode="check") -> dict:
        platform = "FunPay" if mode.startswith("funpay_") else "Playerok"
        directory, env = self.environment(profile)
        logger.info("Проверка %s: пользователь=%s, режим=%s", platform, profile.user_id, mode)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playerok_bot.telegram.probe", mode,
            cwd=directory, env=env, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, **process_options(),
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=120)
        except asyncio.TimeoutError:
            await self._terminate(process)
            logger.warning("Таймаут проверки %s: пользователь=%s, режим=%s. Этапы — в личном probe.log",
                           platform, profile.user_id, mode)
            raise ConfigurationError(
                f"Проверка {platform} не завершилась за 2 минуты. "
                "Этапы проверки сохранены в probe.log на сервере."
            ) from None
        for line in reversed(output.decode("utf-8", errors="replace").splitlines()):
            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict) and "ok" in result:
                if not result["ok"]:
                    raise ConfigurationError(result["error"])
                return result
        raise ConfigurationError(f"Не удалось получить результат проверки {platform}")

    async def connect(self, user_id: int, cookies: str, user_agent: str):
        profile = self.profiles.get(user_id)
        candidate = Profile(user_id, profile.playerok_id, False, profile.last_notified,
                            {**profile.config, "cookies": cookies, "user_agent": user_agent})
        result = await self.probe(candidate, "identity")
        await self.stop(user_id)
        self.profiles.connect(user_id, result["id"], cookies, user_agent)
        return result["username"]

    async def check(self, user_id: int):
        profile = self.profiles.get(user_id)
        if not profile.config['items'] and not profile.config['funpay_items']:
            raise ConfigurationError("Сначала подключите Playerok через /connect и укажите лоты через /items")
        result = None
        if profile.config['funpay_items']:
            result = await self.check_funpay(user_id)
        if profile.config['items']:
            if not profile.playerok_id:
                raise ConfigurationError('Подключите Playerok через /connect')
            result = await self.probe(profile)
            if result["id"] != profile.playerok_id:
                raise ConfigurationError("Авторизация относится к другому профилю Playerok")
        return result

    async def connect_funpay(self, user_id: int, key: str, agent: str):
        profile = self.profiles.get(user_id)
        candidate = Profile(user_id, profile.playerok_id, False, profile.last_notified,
                            {**profile.config, 'funpay_key': key, 'funpay_agent': agent}, profile.funpay_id)
        result = await self.probe(candidate, 'funpay_identity')
        await self.stop(user_id)
        self.profiles.connect_funpay(user_id, result['id'], key, agent)
        return result['username']

    async def check_funpay(self, user_id: int):
        profile = self.profiles.get(user_id)
        if not profile.funpay_id or not profile.config['funpay_items']:
            raise ConfigurationError('Подключите FunPay через /fp_connect и укажите лоты через /fp_items')
        result = await self.probe(profile, 'funpay_check')
        if result['id'] != profile.funpay_id:
            raise ConfigurationError('Авторизация относится к другому профилю FunPay')
        return result

    async def start(self, user_id: int):
        if self.running(user_id):
            return
        expected = self.profiles.get(user_id).config
        revision = self.revisions.get(user_id, 0)
        await self.check(user_id)
        async with self.lock:
            if self.revisions.get(user_id, 0) != revision or self.profiles.get(user_id).config != expected:
                raise ConfigurationError("Настройки изменены или запуск отменён. При необходимости выполните /run ещё раз")
            if self.running(user_id):
                return
            if sum(self.running(uid) for uid in self.processes) >= self.max_workers:
                raise ConfigurationError("На сервере достигнут лимит одновременно работающих профилей")
            await self._spawn(self.profiles.get(user_id))
            self.profiles.set_enabled(user_id, True)

    async def _spawn(self, profile: Profile):
        directory, env = self.environment(profile)
        with (directory / "console.log").open("ab") as output:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playerok_bot", "run", cwd=directory,
                env=env, stdout=output, stderr=output, **process_options(),
            )
        self.processes[profile.user_id] = process

    def running(self, user_id: int) -> bool:
        process = self.processes.get(user_id)
        return process is not None and process.returncode is None

    @staticmethod
    async def _terminate(process):
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill.exe", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                **process_options(),
            )
            await killer.wait()
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def stop(self, user_id: int):
        async with self.lock:
            self.revisions[user_id] = self.revisions.get(user_id, 0) + 1
            self.profiles.set_enabled(user_id, False)
            process = self.processes.pop(user_id, None)
            if process is not None:
                await self._terminate(process)

    async def shutdown(self):
        # enabled сохраняется: после штатного перезапуска хоста работники возобновятся.
        for process in list(self.processes.values()):
            await self._terminate(process)
        self.processes.clear()

    async def restore(self):
        for profile in self.profiles.all():
            if profile.enabled:
                if len(self.processes) >= self.max_workers:
                    self.profiles.set_enabled(profile.user_id, False)
                else:
                    await self._spawn(profile)

    async def monitor(self, bot):
        while True:
            await asyncio.sleep(10)
            for profile in self.profiles.all():
                try:
                    process = self.processes.get(profile.user_id)
                    if process is not None and process.returncode is not None:
                        self.processes.pop(profile.user_id)
                        self.profiles.set_enabled(profile.user_id, False)
                        await bot.send_message(profile.user_id, "Автовыдача остановилась с ошибкой. Выполните /check, затем /run. Очередь сохранена.", reply_markup=ReplyKeyboardRemove())
                    storage = self.profiles.storage(profile.user_id)
                    try:
                        counts = storage.counts()
                        from ..inventory import Inventory
                        stock = Inventory(storage).stats()
                        pending = Inventory(storage).pending_edits()
                    finally:
                        storage.close()
                    uncertain = {op['id'] for op in pending if op['status'] == 'uncertain'}
                    if uncertain - self.funpay_alerts.get(profile.user_id, set()):
                        await bot.send_message(profile.user_id, 'Изменение FunPay требует сверки. Затронутые лоты приостановлены, аккаунты повторно не загружаются. Подробности — /fp_errors и /stats.', reply_markup=ReplyKeyboardRemove())
                    self.funpay_alerts[profile.user_id] = uncertain
                    milestone = profile.last_notified + 10
                    while counts["completed_deals"] + stock['funpay_sold'] >= milestone:
                        await bot.send_message(
                            profile.user_id,
                            f"Продано/выдано аккаунтов: {milestone}\nОсталось по базе без карантина: {counts['queued'] + counts['listed'] - stock['funpay_unknown']}\n"
                            f"FunPay: {stock['funpay_listed']}, резерв Playerok: {stock['playerok_reserve']}",
                            reply_markup=ReplyKeyboardRemove(),
                        )
                        self.profiles.mark_notified(profile.user_id, milestone)
                        milestone += 10
                except Exception as exc:
                    logger.warning("Проверка Telegram-профиля %s временно не выполнена: %s", profile.user_id, type(exc).__name__)

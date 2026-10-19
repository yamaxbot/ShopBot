from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from dataclasses import replace

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
        if key not in ENGINE_KEYS and not key.startswith(("PLAYEROK_", "TELEGRAM_", "FUNPAY_", "PLATFORM_", "OWNERSHIP_", "SHOP_"))
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
        "SHOP_GROUP_ID": str(profile.group_id),
        "SHOP_PLAYEROK_FEE": str(config.get('playerok_fee', 0)),
        "SHOP_FUNPAY_FEE": str(config.get('funpay_fee', 0)),
        "SHOP_PLAYEROK_WITHDRAWAL": str(config.get('playerok_withdrawal', 6)),
        "SHOP_FUNPAY_WITHDRAWAL": str(config.get('funpay_withdrawal', 3)),
    })
    for index, item in enumerate(items[1:], 2):
        environment[f"PLAYEROK_ITEM_{index}"] = item
    return environment


class Workers:
    def __init__(self, profiles: Profiles, max_workers: int, billing=None):
        self.profiles = profiles
        self.max_workers = max_workers
        self.billing = billing
        self.processes: dict[tuple[int, int], asyncio.subprocess.Process] = {}
        self.lock = asyncio.Lock()
        self.revisions: dict[tuple[int, int], int] = {}
        self.funpay_alerts: dict[tuple[int, int], set[int]] = {}
        self.bump_alerts: dict[tuple[int, int], set[tuple[str, str]]] = {}

    def environment(self, profile: Profile):
        directory = self.profiles.user_dir(profile.user_id, profile.group_id)
        accounts = directory / "accaunts.txt"
        if not accounts.exists():
            accounts.write_text("# Аккаунты загружаются в личную SQLite-базу через Telegram\n", encoding="utf-8")
        env = worker_environment(profile, directory)
        env['OWNERSHIP_PATH'] = str(self.profiles.user_dir(profile.user_id) / 'ownership.sqlite3')
        env['FUNPAY_RATE_DB'] = str(self.profiles.root / 'funpay_rate.sqlite3')
        return directory, env

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
        candidate = replace(profile, config={**profile.config, "cookies": cookies, "user_agent": user_agent})
        result = await self.probe(candidate, "identity")
        await self.stop_all(user_id)
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
        candidate = replace(profile, config={**profile.config, 'funpay_key': key, 'funpay_agent': agent})
        result = await self.probe(candidate, 'funpay_identity')
        await self.stop_all(user_id)
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
        if self.billing:
            self.billing.require_active(user_id)
        if self.running(user_id):
            return
        profile = self.profiles.get(user_id)
        key = (user_id, profile.group_id)
        expected = profile.config
        revision = self.revisions.get(key, 0)
        await self.check(user_id)
        async with self.lock:
            if self.revisions.get(key, 0) != revision or self.profiles.get(user_id).group_id != profile.group_id or self.profiles.get(user_id, profile.group_id).config != expected:
                raise ConfigurationError("Настройки изменены или запуск отменён. При необходимости выполните /run ещё раз")
            if self.running(user_id):
                return
            if sum(p.returncode is None for p in self.processes.values()) >= self.max_workers:
                raise ConfigurationError("На сервере достигнут лимит одновременно работающих профилей")
            await self._spawn(self.profiles.get(user_id))
            self.profiles.set_enabled(user_id, True, profile.group_id)

    async def _spawn(self, profile: Profile):
        if self.billing:
            self.billing.require_active(profile.user_id)
        from ..withdrawal import assert_idle
        storage = self.profiles.storage(profile.user_id, profile.group_id)
        try:
            assert_idle(storage)
        finally:
            storage.close()
        directory, env = self.environment(profile)
        with (directory / "console.log").open("ab") as output:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playerok_bot", "run", cwd=directory,
                env=env, stdout=output, stderr=output, **process_options(),
            )
        self.processes[(profile.user_id, profile.group_id)] = process

    def running(self, user_id: int, group_id: int | None = None) -> bool:
        group_id = group_id or self.profiles.get(user_id).group_id
        process = self.processes.get((user_id, group_id))
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

    async def stop_all(self, user_id: int):
        for group in self.profiles.groups(user_id):
            await self.stop(user_id, group.group_id)

    async def stop(self, user_id: int, group_id: int | None = None):
        profile = self.profiles.get(user_id, group_id)
        key = (user_id, profile.group_id)
        async with self.lock:
            self.revisions[key] = self.revisions.get(key, 0) + 1
            self.profiles.set_enabled(user_id, False, profile.group_id)
            process = self.processes.pop(key, None)
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
                if self.billing and not self.billing.active(profile.user_id):
                    self.profiles.set_enabled(profile.user_id, False, profile.group_id)
                    continue
                if len(self.processes) >= self.max_workers:
                    self.profiles.set_enabled(profile.user_id, False, profile.group_id)
                else:
                    await self._spawn(profile)

    async def monitor(self, bot):
        while True:
            await asyncio.sleep(10)
            for profile in self.profiles.all():
                try:
                    key = (profile.user_id, profile.group_id)
                    if self.billing and profile.enabled and not self.billing.active(profile.user_id):
                        await self.stop(profile.user_id, profile.group_id)
                        await bot.send_message(profile.user_id, 'Подписка закончилась: обслуживание группы остановлено. Продлить: /subscribe. Объявления на сайтах остаются; снимите их вручную для остановки продаж.')
                        continue
                    process = self.processes.get(key)
                    if process is not None and process.returncode is not None:
                        self.processes.pop(key)
                        self.profiles.set_enabled(profile.user_id, False, profile.group_id)
                        await bot.send_message(profile.user_id, f"Группа «{profile.group_name}»: автовыдача остановилась с ошибкой. Выполните /check, затем /run. Очередь сохранена.", reply_markup=ReplyKeyboardRemove())
                    storage = self.profiles.storage(profile.user_id, profile.group_id)
                    try:
                        counts = storage.counts()
                        from ..inventory import Inventory
                        stock = Inventory(storage).stats()
                        pending = Inventory(storage).pending_edits()
                        with storage._lock:
                            bump_errors = {(row['key'], row['value']) for row in storage._connection.execute(
                                "SELECT key,value FROM bot_state WHERE key LIKE 'bump_error:%' AND value!=''")}
                    finally:
                        storage.close()
                    if bump_errors - self.bump_alerts.get(key, set()):
                        await bot.send_message(profile.user_id, f'Группа «{profile.group_name}»: поднятие Playerok не выполнено. '
                                               'Причина и таймер — /bump_status. Проверьте выбранную группу через /groups.',
                                               reply_markup=ReplyKeyboardRemove())
                    self.bump_alerts[key] = bump_errors
                    uncertain = {op['id'] for op in pending if op['status'] == 'uncertain'}
                    if uncertain - self.funpay_alerts.get(key, set()):
                        await bot.send_message(profile.user_id, f'Группа «{profile.group_name}»: изменение FunPay требует сверки. Затронутые лоты приостановлены, аккаунты повторно не загружаются. Подробности — /fp_errors и /stats.', reply_markup=ReplyKeyboardRemove())
                    self.funpay_alerts[key] = uncertain
                    milestone = profile.last_notified + 10
                    while counts["completed_deals"] + stock['funpay_sold'] >= milestone:
                        await bot.send_message(
                            profile.user_id,
                            f"Группа «{profile.group_name}»\nПродано/выдано аккаунтов: {milestone}\nОсталось по базе без карантина: {counts['queued'] + counts['listed'] - stock['funpay_unknown']}\n"
                            f"FunPay: {stock['funpay_listed']}, резерв Playerok: {stock['playerok_reserve']}",
                            reply_markup=ReplyKeyboardRemove(),
                        )
                        self.profiles.mark_notified(profile.user_id, milestone, profile.group_id)
                        milestone += 10
                except Exception as exc:
                    logger.warning("Проверка Telegram-профиля %s временно не выполнена: %s", profile.user_id, type(exc).__name__)

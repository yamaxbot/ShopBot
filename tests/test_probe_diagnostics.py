import asyncio
from unittest.mock import AsyncMock

import pytest

from playerok_bot.errors import ConfigurationError
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.workers import Workers


@pytest.mark.parametrize("mode,platform", [("check", "Playerok"), ("funpay_check", "FunPay")])
def test_probe_timeout_names_platform_and_terminates_process(tmp_path, monkeypatch, mode, platform):
    profiles = Profiles(tmp_path)
    try:
        workers = Workers(profiles, 1)
        process = AsyncMock()
        process.communicate.side_effect = asyncio.TimeoutError
        monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
        workers._terminate = AsyncMock()
        with pytest.raises(ConfigurationError, match=f"Проверка {platform}"):
            asyncio.run(workers.probe(profiles.get(101), mode))
        workers._terminate.assert_awaited_once_with(process)
    finally:
        profiles.close()


def test_probe_saves_failure_stage_without_exception_secrets(tmp_path, monkeypatch):
    import logging
    from playerok_bot import app
    from playerok_bot.telegram import probe

    class FailingApplication:
        def __init__(self, settings):
            pass

        def prepare(self, progress):
            progress("Playerok: загрузка лота №1")
            raise RuntimeError("token=private-secret")

        def close(self):
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(probe.sys, "argv", ["probe", "check"])
    monkeypatch.setattr(app, "BotApplication", FailingApplication)
    monkeypatch.setattr(app.Settings, "load", lambda directory: None)
    previous_disable = logging.root.manager.disable
    try:
        with pytest.raises(SystemExit):
            probe.main()
    finally:
        logging.disable(previous_disable)
    log = (tmp_path / "probe.log").read_text(encoding="utf-8")
    assert "Playerok: загрузка лота №1" in log
    assert "RuntimeError" in log
    assert "private-secret" not in log

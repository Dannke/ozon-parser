"""Ежедневный планировщик: время следующего запуска и шаги прогона."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys

from ozon_parser import scheduler
from ozon_parser.settings import parse_settings

MSK = scheduler.get_timezone("Europe/Moscow")
BASE = {"discovery": {"categories": [
    {"name": "phones", "url": "https://www.ozon.ru/category/smartfony-15502/",
     "panel_size": 5}]}}


def settings(**schedule):
    return parse_settings(dict(BASE, schedule=dict({"daily_at": "05:30"}, **schedule)))


def at_msk(hour, minute, day=29):
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=MSK)


def test_next_run_is_today_before_daily_at():
    now = at_msk(4, 0).astimezone(dt.timezone.utc)
    assert scheduler.next_run_at(now, dt.time(5, 30), MSK) == at_msk(5, 30)


def test_next_run_is_tomorrow_after_daily_at():
    assert scheduler.next_run_at(at_msk(6, 0), dt.time(5, 30), MSK) == at_msk(5, 30, day=30)
    # Ровно в момент запуска - следующий уже завтра, двойного прогона нет.
    assert scheduler.next_run_at(at_msk(5, 30), dt.time(5, 30), MSK) == at_msk(5, 30, day=30)


def test_unknown_timezone_falls_back_to_utc():
    assert scheduler.get_timezone("Mars/Olympus") is dt.timezone.utc


def test_job_steps():
    steps = scheduler.job_commands(settings(session_max_age_days=7))
    assert [name for name, _ in steps] == ["ensure_session", "parse"]
    assert steps[0][1][-2:] == ["--max-age-days", "7"]
    assert "--non-interactive" in steps[0][1]
    assert steps[1][1] == [sys.executable, "-m", "ozon_parser", "parse", "--kind", "daily"]

    steps = scheduler.job_commands(settings(ensure_session=False))
    assert [name for name, _ in steps] == ["parse"]


def test_failed_session_step_does_not_skip_parse(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        code = 1 if "get_cookies.py" in " ".join(command) else 0
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    assert scheduler.run_job(settings()) == 0
    assert len(calls) == 2


def test_timeout_is_a_failure(monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    assert scheduler.run_job(settings(ensure_session=False)) == 1

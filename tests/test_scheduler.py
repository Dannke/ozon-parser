"""Ежедневный планировщик: время следующего запуска и шаги прогона."""

from __future__ import annotations

import datetime as dt
import subprocess
import sys

from ozon_parser import scheduler
from ozon_parser.pipeline import EXIT_BLOCKED
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
    assert steps[1][1] == [sys.executable, "-m", "ozon_parser", "parse", "--kind", "daily",
                           "--missing-today"]

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


def test_day_start_is_midnight_in_schedule_timezone():
    # 23:30 UTC 28.09 - это уже 29.09 по Москве.
    now = dt.datetime(2026, 9, 28, 23, 30, tzinfo=dt.timezone.utc)
    assert scheduler.day_start(now, MSK) == at_msk(0, 0)


class FakeClock:
    """Часы, которые идут только во время «сна» и шагов прогона."""

    def __init__(self):
        self.now = 1_000_000.0
        self.slept = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds


def scripted_parse(monkeypatch, clock, codes, step_seconds=1800):
    """subprocess.run, где шаг parse по очереди возвращает codes."""
    codes = list(codes)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(kwargs["timeout"])
        clock.now += step_seconds
        return subprocess.CompletedProcess(command, codes.pop(0))

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    return calls


def test_blocked_run_is_retried_after_delay(monkeypatch):
    clock = FakeClock()
    calls = scripted_parse(monkeypatch, clock, [EXIT_BLOCKED, 0])
    code = scheduler.run_job(settings(ensure_session=False), clock=clock, sleep=clock.sleep)
    assert code == 0
    assert len(calls) == 2
    assert clock.slept == 3 * 3600
    # Повтор получает только остаток общего лимита прогона.
    assert calls[1] < calls[0]


def test_retries_are_limited(monkeypatch):
    clock = FakeClock()
    calls = scripted_parse(monkeypatch, clock, [EXIT_BLOCKED] * 3)
    code = scheduler.run_job(settings(ensure_session=False, block_retries=2,
                                      block_retry_delay_hours=1),
                             clock=clock, sleep=clock.sleep)
    assert code == EXIT_BLOCKED
    assert len(calls) == 3


def test_ordinary_failure_is_not_retried(monkeypatch):
    clock = FakeClock()
    calls = scripted_parse(monkeypatch, clock, [1])
    assert scheduler.run_job(settings(ensure_session=False), clock=clock,
                             sleep=clock.sleep) == 1
    assert len(calls) == 1 and clock.slept == 0


def test_no_retry_without_time_left(monkeypatch):
    """Повтор, который не успеет поработать хотя бы час, не начинается."""
    clock = FakeClock()
    calls = scripted_parse(monkeypatch, clock, [EXIT_BLOCKED], step_seconds=5 * 3600)
    code = scheduler.run_job(settings(ensure_session=False, parse_timeout_hours=8,
                                      block_retry_delay_hours=3),
                             clock=clock, sleep=clock.sleep)
    assert code == EXIT_BLOCKED
    assert len(calls) == 1 and clock.slept == 0


def test_retry_repeats_session_step(monkeypatch):
    clock = FakeClock()
    commands = []
    codes = iter([0, EXIT_BLOCKED, 0, 0])

    def fake_run(command, **kwargs):
        commands.append("get_cookies.py" in " ".join(command))
        return subprocess.CompletedProcess(command, next(codes))

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    assert scheduler.run_job(settings(), clock=clock, sleep=clock.sleep) == 0
    assert commands == [True, False, True, False]

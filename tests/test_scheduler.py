"""Ежедневный планировщик: время следующего запуска, шаги прогона, оповещения."""

from __future__ import annotations

import datetime as dt
import socket
import subprocess
import sys

import pytest

from price_panel import scheduler
from price_panel.db import DatabaseError
from price_panel.pipeline import EXIT_BLOCKED
from price_panel.settings import parse_settings

MSK = scheduler.get_timezone("Europe/Moscow")
BASE = {
    "discovery": {
        "categories": [
            {
                "name": "phones",
                "url": "https://www.ozon.ru/category/smartfony-15502/",
                "panel_size": 5,
            }
        ]
    }
}


@pytest.fixture(autouse=True)
def network(monkeypatch):
    """Сеть «есть» без настоящего DNS; network.down - сколько проверок подряд её нет."""

    class Network:
        down = 0
        checks = 0

    def resolve(host, port):
        Network.checks += 1
        if Network.checks <= Network.down:
            raise socket.gaierror(11001, "getaddrinfo failed")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("185.73.193.68", port))]

    monkeypatch.setattr(scheduler.socket, "getaddrinfo", resolve)
    return Network


def settings(backup=None, **schedule):
    data = dict(BASE, schedule=dict({"daily_at": "05:30"}, **schedule))
    if backup is not None:
        data["backup"] = backup
    return parse_settings(data)


def at_msk(hour, minute, day=29):
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=MSK)


def test_next_run_is_today_before_daily_at():
    now = at_msk(4, 0).astimezone(dt.UTC)
    assert scheduler.next_run_at(now, dt.time(5, 30), MSK) == at_msk(5, 30)


def test_next_run_is_tomorrow_after_daily_at():
    assert scheduler.next_run_at(at_msk(6, 0), dt.time(5, 30), MSK) == at_msk(5, 30, day=30)
    # Ровно в момент запуска - следующий уже завтра, двойного прогона нет.
    assert scheduler.next_run_at(at_msk(5, 30), dt.time(5, 30), MSK) == at_msk(5, 30, day=30)


def test_unknown_timezone_falls_back_to_utc():
    assert scheduler.get_timezone("Mars/Olympus") is dt.UTC


def test_job_steps():
    steps = scheduler.job_commands(settings(session_max_age_days=7))
    assert [name for name, _ in steps] == ["ensure_session", "parse"]
    assert steps[0][1][-2:] == ["--max-age-days", "7"]
    assert "--non-interactive" in steps[0][1]
    assert steps[1][1] == [
        sys.executable,
        "-m",
        "price_panel",
        "parse",
        "--kind",
        "daily",
        "--missing-today",
    ]

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
    now = dt.datetime(2026, 9, 28, 23, 30, tzinfo=dt.UTC)
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
    code = scheduler.run_job(
        settings(ensure_session=False, block_retries=2, block_retry_delay_hours=1),
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == EXIT_BLOCKED
    assert len(calls) == 3


def test_job_waits_for_network_before_start(monkeypatch, network):
    """10.10.2026: компьютер включился в 12:42, в 12:45 сети ещё не было."""
    clock = FakeClock()
    network.down = 2
    events = []
    monkeypatch.setattr(
        scheduler.notify, "report_start", lambda details=None: events.append(("start", clock.slept))
    )
    scripted_parse(monkeypatch, clock, [0])
    assert scheduler.run_job(settings(ensure_session=False), clock=clock, sleep=clock.sleep) == 0
    assert events == [("start", 2 * scheduler.NETWORK_POLL_SECONDS)]


def test_job_runs_anyway_when_network_never_comes(monkeypatch, network):
    """Сеть так и не появилась - прогон всё равно идёт, его неудача попадёт в итог."""
    clock = FakeClock()
    network.down = 10_000
    calls = scripted_parse(monkeypatch, clock, [1])
    assert scheduler.run_job(settings(ensure_session=False), clock=clock, sleep=clock.sleep) == 1
    assert len(calls) == 1
    assert clock.slept == scheduler.NETWORK_WAIT_SECONDS
    # Ожидание съело часть общего предела прогона, а не продлило его.
    assert calls[0] == 10 * 3600 - scheduler.NETWORK_WAIT_SECONDS


def test_ordinary_failure_is_not_retried(monkeypatch):
    clock = FakeClock()
    calls = scripted_parse(monkeypatch, clock, [1])
    assert scheduler.run_job(settings(ensure_session=False), clock=clock, sleep=clock.sleep) == 1
    assert len(calls) == 1 and clock.slept == 0


def test_no_retry_without_time_left(monkeypatch):
    """Повтор, который не успеет поработать хотя бы час, не начинается."""
    clock = FakeClock()
    calls = scripted_parse(monkeypatch, clock, [EXIT_BLOCKED], step_seconds=5 * 3600)
    code = scheduler.run_job(
        settings(ensure_session=False, parse_timeout_hours=8, block_retry_delay_hours=3),
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == EXIT_BLOCKED
    assert len(calls) == 1 and clock.slept == 0


def with_backup():
    return settings(backup={"enabled": True}, ensure_session=False)


def recorded_reports(monkeypatch) -> list:
    reports: list = []
    monkeypatch.setattr(
        scheduler.notify,
        "report_job",
        lambda code, backup_ok, summary=None: reports.append((code, backup_ok)),
    )
    return reports


def test_backup_and_report_run_once_after_retries(monkeypatch):
    """Копия базы и оповещение - один раз, после повтора, с итоговым кодом parse."""
    clock = FakeClock()
    steps = []
    codes = iter([EXIT_BLOCKED, 0, 0])  # parse, повтор parse, backup

    def fake_run(command, **kwargs):
        steps.append(command[3])
        return subprocess.CompletedProcess(command, next(codes))

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    reports = recorded_reports(monkeypatch)
    assert scheduler.run_job(with_backup(), clock=clock, sleep=clock.sleep) == 0
    assert steps == ["parse", "parse", "backup"]
    assert reports == [(0, True)]


def test_failed_backup_is_reported_but_keeps_parse_code(monkeypatch):
    codes = iter([0, 1])  # parse, backup
    monkeypatch.setattr(
        scheduler.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, next(codes)),
    )
    reports = recorded_reports(monkeypatch)
    assert scheduler.run_job(with_backup()) == 0
    assert reports == [(0, False)]


def test_report_without_backup(monkeypatch):
    """Копия выключена - в оповещение уходит None, а не «не создана»."""
    monkeypatch.setattr(
        scheduler.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1),
    )
    reports = recorded_reports(monkeypatch)
    assert scheduler.run_job(settings(ensure_session=False)) == 1
    assert reports == [(1, None)]


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


def test_job_reports_start_retry_and_result(monkeypatch):
    """Оповещения прогона: начало, повтор после блокировки, итог - по порядку."""
    clock = FakeClock()
    scripted_parse(monkeypatch, clock, [EXIT_BLOCKED, 0])
    events: list = []
    monkeypatch.setattr(scheduler.notify, "report_start", lambda details: events.append("start"))
    monkeypatch.setattr(
        scheduler.notify,
        "report_retry",
        lambda retry_at, attempt, attempts, details: events.append(
            ("retry", retry_at, attempt, attempts)
        ),
    )
    monkeypatch.setattr(
        scheduler.notify,
        "report_job",
        lambda code, backup_ok, summary: events.append(("result", code)),
    )

    assert scheduler.run_job(settings(ensure_session=False), clock=clock, sleep=clock.sleep) == 0
    # Повтор - через 3 ч после конца первого шага: старт 1_000_000 + 1800 с.
    retry_at = dt.datetime.fromtimestamp(1_000_000 + 1800 + 3 * 3600, MSK).strftime("%H:%M")
    assert events == ["start", ("retry", retry_at + " MSK", 1, 1), ("result", 0)]


def test_run_summary_text():
    row = (15, "daily", "partial", None, 1200, 1150, 50, 8100.0, 8.9, 6.7, 3.0)
    text = scheduler.format_run(row, [("blocked", 45), ("timeout", 5)])
    assert text == (
        "Прогон 15 (daily): partial, успешно 1150 из 1200, ошибок 50, "
        "2h 15m 00s, 8.9 SKU/мин\nОшибки: blocked 45, timeout 5"
    )
    # Прерванный прогон: длительности и скорости может не быть.
    row = (16, "daily", "interrupted", None, 1200, 10, 0, None, None, None, 3.0)
    assert scheduler.format_run(row, []) == (
        "Прогон 16 (daily): interrupted, успешно 10 из 1200, ошибок 0"
    )
    assert scheduler.format_progress(1200, 52) == "За сегодня собрано 1148 из 1200 SKU panel"


def test_summaries_are_empty_without_database(monkeypatch):
    """Оповещение уходит и без подробностей, если база не отвечает."""

    def broken():
        raise DatabaseError("Ошибка PostgreSQL: connection refused")

    monkeypatch.setattr(scheduler, "Warehouse", broken)
    assert scheduler.start_details(settings()) == ""
    assert scheduler.job_summary(settings()) == ""


def run_schedule(monkeypatch, *argv) -> list:
    """python -m price_panel schedule при недоступной базе; что ушло в report_failure."""
    from price_panel import __main__ as cli

    def broken():
        raise DatabaseError("Ошибка PostgreSQL: connection refused")

    failures: list = []
    monkeypatch.setattr(cli, "Warehouse", broken)
    monkeypatch.setattr(cli, "load_settings", lambda path: settings())
    monkeypatch.setattr(cli.notify, "report_failure", failures.append)
    assert cli.main(["schedule", *argv]) == 1
    return failures


def test_daily_task_reports_database_failure(monkeypatch):
    (problem,) = run_schedule(monkeypatch, "--once")
    assert "connection refused" in problem and "PG_DSN" in problem


def test_container_loop_does_not_report_on_restart(monkeypatch):
    assert run_schedule(monkeypatch) == []


class SummaryWarehouse:
    """Warehouse для сводки итога: panel из 3 SKU, прогоны новые - первыми."""

    def __init__(self, runs):
        self.runs = runs

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def active_panel_skus(self):
        return {"1", "2", "3"}

    def panel_skus(self, missing_since=None):
        return ["3"]

    def recent_runs(self, limit):
        return self.runs[:limit]

    def error_counts(self, run_id):
        return [("timeout", 1)] if run_id == 7 else []


def run_row(run_id, kind, started):
    return (run_id, kind, "partial", started, 3, 2, 1, 600.0, 9.0, 6.6, 3.0)


def test_summary_reports_todays_daily_run_not_a_later_manual_one(monkeypatch):
    now = dt.datetime.now(dt.UTC)
    runs = [run_row(8, "benchmark", now), run_row(7, "daily", now)]
    monkeypatch.setattr(scheduler, "Warehouse", lambda: SummaryWarehouse(runs))
    summary = scheduler.job_summary(settings())
    assert summary.splitlines()[0] == "За сегодня собрано 2 из 3 SKU panel"
    assert "Прогон 7 (daily)" in summary and "Ошибки: timeout 1" in summary
    assert "Прогон 8" not in summary


def test_summary_skips_yesterdays_run(monkeypatch):
    """Сегодня собирать было нечего - вчерашний прогон за итог не выдаётся."""
    yesterday = dt.datetime.now(dt.UTC) - dt.timedelta(days=2)
    monkeypatch.setattr(
        scheduler, "Warehouse", lambda: SummaryWarehouse([run_row(7, "daily", yesterday)])
    )
    assert scheduler.job_summary(settings()) == "За сегодня собрано 2 из 3 SKU panel"

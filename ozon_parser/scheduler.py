"""Ежедневный запуск без внешнего планировщика: python -m ozon_parser schedule.

Сделано для docker-compose: контейнер parser живёт постоянно и раз в сутки
(schedule.daily_at) выполняет те же шаги, что Airflow DAG:

    ensure_session  ->  parse (panel)

Каждый шаг - отдельный процесс. Браузер и драйвер Playwright умирают вместе
с процессом прогона, так что утечка памяти или зависший Chromium не
накапливаются от ночи к ночи, а сам планировщик остаётся лёгким. Прогоны идут
строго по очереди: следующий не начнётся, пока не закончился предыдущий.

Airflow DAG (dags/ozon_parser_dag.py) остаётся рабочим вариантом для тех, у
кого Airflow уже есть; оба способа вызывают одни и те же команды.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config
from .logger import get_logger
from .settings import Settings

log = get_logger("scheduler")

# Спим короткими отрезками: так планировщик быстро реагирует на остановку
# контейнера и не «проспит» запуск после перевода системных часов.
SLEEP_STEP_SECONDS = 60


def get_timezone(name: str) -> dt.tzinfo:
    """Часовой пояс по имени; если базы поясов нет - UTC с предупреждением."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Часовой пояс %r не найден (нет пакета tzdata?) - использую UTC", name)
        return dt.timezone.utc


def next_run_at(now: dt.datetime, daily_at: dt.time, tz: dt.tzinfo) -> dt.datetime:
    """Ближайший момент daily_at в поясе tz строго после now."""
    local_now = now.astimezone(tz)
    candidate = dt.datetime.combine(local_now.date(), daily_at, tzinfo=tz)
    if candidate <= local_now:
        candidate = dt.datetime.combine(local_now.date() + dt.timedelta(days=1), daily_at,
                                        tzinfo=tz)
    return candidate


def job_commands(settings: Settings) -> list:
    """Шаги ежедневного прогона: (название, команда)."""
    python = sys.executable
    steps = []
    if settings.schedule.ensure_session:
        steps.append(("ensure_session", [
            python, str(config.BASE_DIR / "get_cookies.py"), "--non-interactive",
            "--max-age-days", str(settings.schedule.session_max_age_days)]))
    steps.append(("parse", [python, "-m", "ozon_parser", "parse", "--kind", "daily"]))
    return steps


def run_job(settings: Settings) -> int:
    """Один ежедневный прогон. Возвращает код возврата шага parse."""
    timeout = settings.schedule.parse_timeout_hours * 3600
    code = 1
    for name, command in job_commands(settings):
        log.info("Шаг %s: %s", name, " ".join(command[1:]))
        started = time.monotonic()
        try:
            code = subprocess.run(command, cwd=str(config.BASE_DIR), timeout=timeout,
                                  check=False).returncode
        except subprocess.TimeoutExpired:
            log.error("Шаг %s не уложился в %.1f ч и остановлен", name, timeout / 3600)
            code = 1
        log.info("Шаг %s завершён с кодом %s за %.0f с", name, code, time.monotonic() - started)
        if name == "ensure_session" and code != 0:
            # Карточки ozon.ru открываются и без входа: прогон всё равно
            # запускаем, а проблема с сессией видна в логе и в parse_runs.
            log.warning("Сессию обновить не удалось - запускаю парсинг с имеющейся")
    return code


def serve(settings: Settings, once: bool = False, clock=time.time) -> int:
    """Цикл планировщика. once=True - выполнить прогон сразу и выйти."""
    if once:
        return run_job(settings)

    tz = get_timezone(settings.schedule.timezone)
    log.info("Планировщик запущен: ежедневно в %s (%s)",
             settings.schedule.daily_at.strftime("%H:%M"), settings.schedule.timezone)
    if settings.schedule.run_on_start:
        run_job(settings)

    while True:
        now = dt.datetime.fromtimestamp(clock(), dt.timezone.utc)
        target = next_run_at(now, settings.schedule.daily_at, tz)
        log.info("Следующий прогон: %s", target.isoformat(timespec="minutes"))
        while clock() < target.timestamp():
            time.sleep(min(SLEEP_STEP_SECONDS, max(target.timestamp() - clock(), 0)))
        run_job(settings)


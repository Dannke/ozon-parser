"""Ежедневный запуск без внешнего планировщика: python -m ozon_parser schedule.

Сделано для docker-compose: контейнер parser живёт постоянно и раз в сутки
(schedule.daily_at) выполняет те же шаги, что Airflow DAG:

    ensure_session  ->  parse (panel)

Если Ozon остановил прогон (parse вышел с кодом EXIT_BLOCKED), прогон
повторяется через schedule.block_retry_delay_hours, не больше
schedule.block_retries раз. Повтор берёт только SKU, у которых за текущий день
ещё нет наблюдения (parse --missing-today), а весь прогон с повторами
укладывается в schedule.parse_timeout_hours.

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
from .pipeline import EXIT_BLOCKED
from .settings import Settings

log = get_logger("scheduler")

# Спим короткими отрезками: так планировщик быстро реагирует на остановку
# контейнера и не «проспит» запуск после перевода системных часов.
SLEEP_STEP_SECONDS = 60

# Повтор не начинается, если до конца отведённого прогону времени остаётся
# меньше часа: столько нужно, чтобы успеть собрать хоть сколько-то SKU.
MIN_RETRY_WINDOW_SECONDS = 3600


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


def day_start(now: dt.datetime, tz: dt.tzinfo) -> dt.datetime:
    """Начало текущего дня в поясе tz - граница дня наблюдения."""
    return dt.datetime.combine(now.astimezone(tz).date(), dt.time(0), tzinfo=tz)


def job_commands(settings: Settings) -> list:
    """Шаги ежедневного прогона: (название, команда)."""
    python = sys.executable
    steps = []
    if settings.schedule.ensure_session:
        steps.append(("ensure_session", [
            python, str(config.BASE_DIR / "get_cookies.py"), "--non-interactive",
            "--max-age-days", str(settings.schedule.session_max_age_days)]))
    steps.append(("parse", [python, "-m", "ozon_parser", "parse", "--kind", "daily",
                            "--missing-today"]))
    return steps


def _clock_time(timestamp: float, settings: Settings) -> str:
    """Время для лога в поясе расписания (в контейнере системный пояс - UTC)."""
    tz = get_timezone(settings.schedule.timezone)
    return dt.datetime.fromtimestamp(timestamp, tz).strftime("%H:%M %Z")


def _sleep_until(target: float, clock, sleep) -> None:
    while clock() < target:
        sleep(min(SLEEP_STEP_SECONDS, max(target - clock(), 0)))


def _run_steps(settings: Settings, deadline: float, clock) -> int:
    """Шаги прогона по очереди. Возвращает код возврата шага parse."""
    code = 1
    for name, command in job_commands(settings):
        timeout = max(deadline - clock(), 1)
        log.info("Шаг %s: %s", name, " ".join(command[1:]))
        started = time.monotonic()
        try:
            code = subprocess.run(command, cwd=str(config.BASE_DIR), timeout=timeout,
                                  check=False).returncode
        except subprocess.TimeoutExpired:
            log.error("Шаг %s не уложился в отведённое время (до %s) и остановлен", name,
                      _clock_time(deadline, settings))
            code = 1
        log.info("Шаг %s завершён с кодом %s за %.0f с", name, code, time.monotonic() - started)
        if name == "ensure_session" and code != 0:
            # Карточки ozon.ru открываются и без входа: прогон всё равно
            # запускаем, а проблема с сессией видна в логе и в parse_runs.
            log.warning("Сессию обновить не удалось - запускаю парсинг с имеющейся")
    return code


def run_job(settings: Settings, clock=time.time, sleep=time.sleep) -> int:
    """Один ежедневный прогон с повторами после блокировки.

    Возвращает код возврата последнего шага parse.
    """
    schedule = settings.schedule
    deadline = clock() + schedule.parse_timeout_hours * 3600
    code = _run_steps(settings, deadline, clock)
    for attempt in range(1, schedule.block_retries + 1):
        if code != EXIT_BLOCKED:
            break
        retry_at = clock() + schedule.block_retry_delay_hours * 3600
        if deadline - retry_at < MIN_RETRY_WINDOW_SECONDS:
            log.warning("Прогон остановлен блокировкой Ozon; на повтор не хватает времени "
                        "(schedule.parse_timeout_hours=%s) - SKU доберёт следующий прогон",
                        schedule.parse_timeout_hours)
            break
        log.warning("Прогон остановлен блокировкой Ozon - повтор %s из %s в %s",
                    attempt, schedule.block_retries, _clock_time(retry_at, settings))
        _sleep_until(retry_at, clock, sleep)
        code = _run_steps(settings, deadline, clock)
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
        _sleep_until(target.timestamp(), clock, time.sleep)
        run_job(settings)


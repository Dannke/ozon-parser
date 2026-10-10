"""Ежедневный запуск без внешнего планировщика: python -m price_panel schedule.

Сделано для docker-compose: контейнер parser живёт постоянно и раз в сутки
(schedule.daily_at) выполняет те же шаги, что Airflow DAG:

    сеть (DNS)  ->  оповещение о начале  ->  ensure_session (если включён)  ->  parse
    (panel)  ->  backup  ->  оповещение об итоге

Если Ozon остановил прогон (parse вышел с кодом EXIT_BLOCKED), прогон
повторяется через schedule.block_retry_delay_hours, не больше
schedule.block_retries раз, и об этом уходит отдельное оповещение. Повтор
берёт только SKU, у которых за текущий день ещё нет наблюдения (parse
--missing-today), а весь прогон с повторами укладывается в
schedule.parse_timeout_hours. Резервная копия базы (если backup.enabled) и
оповещение об итоге (notify.py) - один раз, после всех повторов.

Каждый шаг - отдельный процесс. Браузер и драйвер Playwright умирают вместе
с процессом прогона, так что утечка памяти или зависший Chromium не
накапливаются от ночи к ночи, а сам планировщик остаётся лёгким. Прогоны идут
строго по очереди: следующий не начнётся, пока не закончился предыдущий.

Airflow DAG (dags/ozon_parser_dag.py) остаётся рабочим вариантом для тех, у
кого Airflow уже есть; оба способа вызывают одни и те же команды.
"""

from __future__ import annotations

import datetime as dt
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from price_panel.app.pipeline import format_duration
from price_panel.app.settings import Settings
from price_panel.core.models import EXIT_BLOCKED
from price_panel.infra import backup, config, notify
from price_panel.infra.logger import get_logger
from price_panel.infra.warehouse import Warehouse

log = get_logger("scheduler")

# Спим короткими отрезками: так планировщик быстро реагирует на остановку
# контейнера и не «проспит» запуск после перевода системных часов.
SLEEP_STEP_SECONDS = 60

# Повтор не начинается, если до конца отведённого прогону времени остаётся
# меньше часа: столько нужно, чтобы успеть собрать хоть сколько-то SKU.
MIN_RETRY_WINDOW_SECONDS = 3600

# Предел для шага backup: внутри него pg_dump и проверка pg_restore, у каждого
# свой предел backup.TIMEOUT_SECONDS. Копия в несколько мегабайт снимается за
# секунды - это страховка от зависшего Docker.
BACKUP_TIMEOUT_SECONDS = 2 * backup.TIMEOUT_SECONDS + 60

# Сколько последних прогонов просматривать в поисках сегодняшнего daily для
# итога: между ним и концом задачи бывают ручные parse и benchmark.
RECENT_RUNS_FOR_SUMMARY = 10

# Перед прогоном ждём сеть: 10.10.2026 компьютер включился за три минуты до
# запуска, Wi-Fi ещё не поднялся, и без DNS прогон за 86 с закончился ничем, а
# сообщения в Telegram не ушли. Сеть есть, когда разрешается адрес Ozon.
NETWORK_HOST = "www.ozon.ru"
NETWORK_WAIT_SECONDS = 30 * 60
NETWORK_POLL_SECONDS = 60


def get_timezone(name: str) -> dt.tzinfo:
    """Часовой пояс по имени; если базы поясов нет - UTC с предупреждением."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("Часовой пояс %r не найден (нет пакета tzdata?) - использую UTC", name)
        return dt.UTC


def next_run_at(now: dt.datetime, daily_at: dt.time, tz: dt.tzinfo) -> dt.datetime:
    """Ближайший момент daily_at в поясе tz строго после now."""
    local_now = now.astimezone(tz)
    candidate = dt.datetime.combine(local_now.date(), daily_at, tzinfo=tz)
    if candidate <= local_now:
        candidate = dt.datetime.combine(
            local_now.date() + dt.timedelta(days=1), daily_at, tzinfo=tz
        )
    return candidate


def day_start(now: dt.datetime, tz: dt.tzinfo) -> dt.datetime:
    """Начало текущего дня в поясе tz - граница дня наблюдения."""
    return dt.datetime.combine(now.astimezone(tz).date(), dt.time(0), tzinfo=tz)


def job_commands(settings: Settings) -> list:
    """Шаги ежедневного прогона: (название, команда)."""
    python = sys.executable
    steps = []
    if settings.schedule.ensure_session:
        steps.append(
            (
                "ensure_session",
                [
                    python,
                    str(config.BASE_DIR / "get_cookies.py"),
                    "--non-interactive",
                    "--max-age-days",
                    str(settings.schedule.session_max_age_days),
                ],
            )
        )
    steps.append(
        ("parse", [python, "-m", "price_panel", "parse", "--kind", "daily", "--missing-today"])
    )
    return steps


def _clock_time(timestamp: float, settings: Settings) -> str:
    """Время для лога в поясе расписания (в контейнере системный пояс - UTC)."""
    tz = get_timezone(settings.schedule.timezone)
    return dt.datetime.fromtimestamp(timestamp, tz).strftime("%H:%M %Z")


def _sleep_until(target: float, clock, sleep) -> None:
    while clock() < target:
        sleep(min(SLEEP_STEP_SECONDS, max(target - clock(), 0)))


def _run_step(name: str, command: list, timeout: float, limit: Callable[[], str]) -> int:
    """Шаг отдельным процессом. Возвращает код возврата (1 - не уложился в timeout).

    :param limit: описание предела для лога; вызывается, только если он превышен.
    """
    log.info("Шаг %s: %s", name, " ".join(command[1:]))
    started = time.monotonic()
    try:
        code = subprocess.run(
            command, cwd=str(config.BASE_DIR), timeout=timeout, check=False
        ).returncode
    except subprocess.TimeoutExpired:
        log.error("Шаг %s не уложился в %s и остановлен", name, limit())
        code = 1
    log.info("Шаг %s завершён с кодом %s за %.0f с", name, code, time.monotonic() - started)
    return code


def _run_steps(settings: Settings, deadline: float, clock) -> int:
    """Шаги прогона по очереди. Возвращает код возврата шага parse."""
    code = 1
    for name, command in job_commands(settings):
        code = _run_step(
            name,
            command,
            max(deadline - clock(), 1),
            lambda: "отведённое время (до {})".format(_clock_time(deadline, settings)),
        )
        if name == "ensure_session" and code != 0:
            # Карточки ozon.ru открываются и без входа: прогон всё равно
            # запускаем, а проблема с сессией видна в логе и в parse_runs.
            log.warning("Сессию обновить не удалось - запускаю парсинг с имеющейся")
    return code


def wait_for_network(clock=time.time, sleep=time.sleep) -> bool:
    """Ждёт, пока NETWORK_HOST разрешается в DNS, не дольше NETWORK_WAIT_SECONDS.

    False - сеть так и не появилась; прогон всё равно запускается, и его
    неудача попадёт в учёт и в оповещение.
    """
    give_up = clock() + NETWORK_WAIT_SECONDS
    while True:
        try:
            socket.getaddrinfo(NETWORK_HOST, 443)
            return True
        except OSError as exc:
            if clock() >= give_up:
                log.error(
                    "Сети нет %s мин: %s не разрешается (%s) - запускаю прогон как есть",
                    NETWORK_WAIT_SECONDS // 60,
                    NETWORK_HOST,
                    exc,
                )
                return False
            log.warning(
                "Сети нет: %s не разрешается (%s) - жду %s с",
                NETWORK_HOST,
                exc,
                NETWORK_POLL_SECONDS,
            )
            sleep(NETWORK_POLL_SECONDS)


def run_job(settings: Settings, clock=time.time, sleep=time.sleep) -> int:
    """Один ежедневный прогон с повторами после блокировки, копией базы и оповещениями.

    Возвращает код возврата последнего шага parse: ни копия, ни оповещения
    его не меняют.
    """
    schedule = settings.schedule
    # Ожидание сети входит в общий предел прогона: задача Windows его не превысит.
    deadline = clock() + schedule.parse_timeout_hours * 3600
    wait_for_network(clock, sleep)
    notify.report_start(details=lambda: start_details(settings))
    code = _run_steps(settings, deadline, clock)
    for attempt in range(1, schedule.block_retries + 1):
        if code != EXIT_BLOCKED:
            break
        retry_at = clock() + schedule.block_retry_delay_hours * 3600
        if deadline - retry_at < MIN_RETRY_WINDOW_SECONDS:
            log.warning(
                "Прогон остановлен блокировкой Ozon; на повтор не хватает времени "
                "(schedule.parse_timeout_hours=%s) - SKU доберёт следующий прогон",
                schedule.parse_timeout_hours,
            )
            break
        log.warning(
            "Прогон остановлен блокировкой Ozon - повтор %s из %s в %s",
            attempt,
            schedule.block_retries,
            _clock_time(retry_at, settings),
        )
        notify.report_retry(
            _clock_time(retry_at, settings),
            attempt,
            schedule.block_retries,
            details=lambda: day_progress(settings),
        )
        _sleep_until(retry_at, clock, sleep)
        code = _run_steps(settings, deadline, clock)

    # Копия снимается и после неудачного прогона: собранное за день уже в базе.
    backup_ok = _run_backup() if settings.backup.enabled else None
    notify.report_job(code, backup_ok, summary=lambda: job_summary(settings))
    return code


def _run_backup() -> bool:
    """Шаг backup отдельным процессом, как остальные шаги. True - копия снята."""
    return (
        _run_step(
            "backup",
            [sys.executable, "-m", "price_panel", "backup"],
            BACKUP_TIMEOUT_SECONDS,
            lambda: "{} с".format(BACKUP_TIMEOUT_SECONDS),
        )
        == 0
    )


# ------------------------------------------------------- тексты оповещений --
def format_progress(panel_total: int, missing: int) -> str:
    collected = panel_total - missing
    return "За сегодня собрано {} из {} SKU panel".format(collected, panel_total)


def format_run(row: tuple, error_counts: list) -> str:
    """Строка о прогоне из parse_runs (порядок полей - Warehouse.recent_runs)."""
    run_id, kind, status, _started, total, ok, errors, duration, speed = row[:9]
    text = "Прогон {} ({}): {}, успешно {} из {}, ошибок {}".format(
        run_id, kind, status, ok, total, errors
    )
    if duration is not None:
        text += ", {}".format(format_duration(float(duration)))
    if speed is not None:
        text += ", {} SKU/мин".format(speed)
    if error_counts:
        text += "\nОшибки: " + ", ".join(
            "{} {}".format(error_type, count) for error_type, count in error_counts
        )
    return text


def _read_db(settings: Settings, read: Callable[[Warehouse, dt.datetime], str]) -> str:
    """Текст для оповещения из базы; пусто, если база недоступна."""
    tz = get_timezone(settings.schedule.timezone)
    since = day_start(dt.datetime.now(dt.UTC), tz)
    try:
        with Warehouse() as wh:
            return read(wh, since)
    except Exception as exc:  # noqa: BLE001 - оповещение не должно падать из-за базы
        log.debug("Данные для оповещения не прочитаны: %s", exc)
        return ""


def _progress(wh: Warehouse, since: dt.datetime) -> str:
    total = len(wh.active_panel_skus())
    return format_progress(total, len(wh.panel_skus(missing_since=since))) if total else ""


def start_details(settings: Settings) -> str:
    """Для оповещения о начале: сколько SKU panel осталось собрать за день."""

    def read(wh: Warehouse, since: dt.datetime) -> str:
        total = len(wh.active_panel_skus())
        if not total:
            return "Panel пуста - нужен python -m price_panel discover"
        missing = len(wh.panel_skus(missing_since=since))
        if not missing:
            return "Все {} SKU panel за сегодня уже собраны".format(total)
        return "К сбору {} из {} SKU panel".format(missing, total)

    return _read_db(settings, read)


def day_progress(settings: Settings) -> str:
    """Для оповещения о повторе: сколько SKU уже собрано за день."""
    return _read_db(settings, _progress)


def job_summary(settings: Settings) -> str:
    """Для оповещения об итоге: собрано за день, последний ежедневный прогон, его ошибки.

    Берётся только сегодняшний прогон с kind=daily: ручной parse или benchmark
    после него - не итог задачи, а если parse сегодня собирать было нечего,
    строки о прогоне нет вовсе.
    """

    def read(wh: Warehouse, since: dt.datetime) -> str:
        lines = [_progress(wh, since)]
        daily = [
            row
            for row in wh.recent_runs(RECENT_RUNS_FOR_SUMMARY)
            if row[1] == "daily" and row[3] >= since
        ]
        if daily:
            lines.append(format_run(daily[0], wh.error_counts(daily[0][0])))
        return "\n".join(line for line in lines if line)

    return _read_db(settings, read)


def serve(settings: Settings, once: bool = False, clock=time.time) -> int:
    """Цикл планировщика. once=True - выполнить прогон сразу и выйти."""
    if once:
        return run_job(settings)

    tz = get_timezone(settings.schedule.timezone)
    log.info(
        "Планировщик запущен: ежедневно в %s (%s)",
        settings.schedule.daily_at.strftime("%H:%M"),
        settings.schedule.timezone,
    )
    if settings.schedule.run_on_start:
        run_job(settings)

    while True:
        now = dt.datetime.fromtimestamp(clock(), dt.UTC)
        target = next_run_at(now, settings.schedule.daily_at, tz)
        log.info("Следующий прогон: %s", target.isoformat(timespec="minutes"))
        _sleep_until(target.timestamp(), clock, time.sleep)
        run_job(settings)

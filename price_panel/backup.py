"""Резервная копия базы конвейера: pg_dump внутри контейнера PostgreSQL.

История цен копится только в томе Docker pgdata: docker compose down -v или
сброс Docker Desktop стёрли бы её целиком. Копия снимается после ежедневного
прогона (scheduler.run_job, если backup.enabled в config.yaml) и вручную:

    python -m price_panel backup

pg_dump запускается в самом контейнере (docker compose exec): на хосте
клиента PostgreSQL нет, а внутри контейнера вход по локальному сокету не
спрашивает пароль. Формат custom (-Fc) сжат и восстанавливается pg_restore:

    docker compose exec -T postgres pg_restore -U ozon -d <база> < backups/ozon-ГГГГ-ММ-ДД.dump

Копия пишется во временный файл, проверяется (pg_restore --list читает её
оглавление) и только потом встаёт на место: обрыв посреди копирования не
оставит битый файл под видом целого. Хранятся последние backup.keep копий,
повторная копия за тот же день заменяет утреннюю.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

from . import config
from .logger import get_logger

log = get_logger("backup")

PREFIX = "ozon-"
SUFFIX = ".dump"
# Первые байты архива pg_dump в формате custom.
DUMP_MAGIC = b"PGDMP"
# Строка оглавления, по которой видно, что в копию попала история цен.
EXPECTED_ENTRY = "TABLE DATA public price_history"
TIMEOUT_SECONDS = 600


class BackupError(RuntimeError):
    """Резервная копия не создана."""


def compose_exec(*args: str) -> list:
    """Команда внутри контейнера postgres из docker-compose.yml проекта."""
    return ["docker", "compose", "exec", "-T", "postgres", *args]


def _run(name: str, command: list, runner: Callable, **kwargs) -> subprocess.CompletedProcess:
    try:
        return runner(command, cwd=str(config.BASE_DIR), timeout=TIMEOUT_SECONDS, check=False,
                      **kwargs)
    except FileNotFoundError as exc:
        raise BackupError("не найден docker - нужен запущенный Docker Desktop с базой") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError("{} не уложился в {} с".format(name, TIMEOUT_SECONDS)) from exc


def _stderr(result: subprocess.CompletedProcess) -> str:
    """Хвост stderr для сообщения об ошибке (процессы запускаются в байтовом режиме)."""
    text = (result.stderr or b"").decode("utf-8", errors="replace")
    return " ".join(text.split())[-300:]


def create_backup(directory: Path, keep: int, today: Optional[dt.date] = None,
                  runner: Callable = subprocess.run) -> Path:
    """Снимает копию базы в directory/ozon-ГГГГ-ММ-ДД.dump и удаляет лишние старые.

    :raises BackupError: копия не создана; прежние копии при этом не трогаются.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "{}{}{}".format(PREFIX, (today or dt.date.today()).isoformat(), SUFFIX)
    tmp = target.with_name(target.name + ".tmp")
    try:
        with tmp.open("wb") as handle:
            result = _run("pg_dump", compose_exec("pg_dump", "-U", config.POSTGRES_USER,
                                                  "-d", config.POSTGRES_DB, "-Fc"),
                          runner, stdout=handle, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise BackupError("pg_dump завершился с кодом {}: {}".format(
                result.returncode, _stderr(result)))
        verify(tmp, runner)
        os.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)

    removed = prune(directory, keep)
    log.info("Резервная копия базы: %s (%.1f МБ), удалено старых копий: %s",
             target, target.stat().st_size / 2**20, len(removed))
    return target


def verify(path: Path, runner: Callable = subprocess.run) -> None:
    """Проверяет, что файл - читаемый архив pg_dump с историей цен.

    :raises BackupError: файл не архив или pg_restore не читает его оглавление.
    """
    with path.open("rb") as handle:
        if handle.read(len(DUMP_MAGIC)) != DUMP_MAGIC:
            raise BackupError("{} - не архив pg_dump".format(path.name))
    # Файл открывается заново, а не перематывается seek(0): дочерний процесс
    # читает дескриптор ОС, а после буферизованного read() он стоит дальше
    # начала файла, даже если tell() показывает 0.
    with path.open("rb") as handle:
        result = _run("pg_restore", compose_exec("pg_restore", "--list"), runner,
                      stdin=handle, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    listing = (result.stdout or b"").decode("utf-8", errors="replace")
    if result.returncode != 0 or EXPECTED_ENTRY not in listing:
        raise BackupError("pg_restore не читает копию {}: {}".format(
            path.name, _stderr(result) or "в оглавлении нет истории цен"))


def prune(directory: Path, keep: int) -> list:
    """Удаляет копии сверх keep самых свежих. Возвращает удалённые пути.

    Дата в имени файла (ГГГГ-ММ-ДД) сортируется как строка, так что порядок
    имён - это порядок дней.
    """
    copies = sorted(directory.glob("{}*{}".format(PREFIX, SUFFIX)))
    stale = copies[:-keep] if keep > 0 else []
    for path in stale:
        path.unlink()
    return stale

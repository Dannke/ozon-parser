"""Проверка, что срез за указанную дату действительно попал в хранилище.

Нужен Airflow-у (и любому другому планировщику) как шаг контроля после
парсинга: молчаливый пустой результат хуже явной ошибки. Работает в том же
окружении, что и парсер, поэтому не требует провайдеров Airflow.

Запуск:
    python check_snapshot.py                     # за сегодня, бэкенд из .env
    python check_snapshot.py --date 2026-09-22
    python check_snapshot.py --storage postgres --min-rows 2

Код возврата: 0 - данные на месте, 1 - данных нет либо проверка не удалась.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys

from . import cli, config, db
from .logger import get_logger

log = get_logger("check_snapshot")


class CheckError(RuntimeError):
    """Проверку выполнить не удалось (нет драйвера, недоступна база)."""


def count_csv(snapshot_date: dt.date) -> int:
    """Количество строк с данными в CSV последнего запуска.

    Колонки с датой среза в CSV нет (это снимок последнего запуска), поэтому
    дату берём из времени изменения файла - иначе файл прошлого запуска
    засчитался бы за сегодняшний результат.

    Следствие: проверить CSV за прошедшую дату нельзя - истории в нём нет.
    Для этого нужен бэкенд с таблицей (postgres/clickhouse).
    """
    path = config.OUTPUT_CSV
    if not path.exists():
        raise CheckError("Файл не создан: {}".format(path))

    modified = dt.date.fromtimestamp(path.stat().st_mtime)
    if modified != snapshot_date:
        raise CheckError(
            "Файл {} обновлялся {}, а ожидается срез за {} - парсер не записал "
            "результат за эту дату".format(path, modified, snapshot_date)
        )

    with open(path, encoding="utf-8-sig", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def count_postgres(snapshot_date: dt.date) -> int:
    """Количество строк за дату среза в PostgreSQL."""
    try:
        from psycopg2 import sql

        query = sql.SQL("SELECT count(*) FROM {table} WHERE parsed_date = %s").format(
            table=db.pg_identifier(config.PG_TABLE)
        )
        with db.postgres_cursor() as cursor:
            cursor.execute(query, (snapshot_date,))
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except ImportError as exc:
        raise CheckError('Не установлен драйвер: pip install ".[postgres]"') from exc
    except db.DatabaseError as exc:
        raise CheckError(str(exc)) from exc


def count_clickhouse(snapshot_date: dt.date) -> int:
    """Количество строк за дату среза в ClickHouse."""
    try:
        # FINAL - чтобы ReplacingMergeTree не посчитал ещё не схлопнутые дубли.
        query = "SELECT count() FROM {} FINAL WHERE parsed_date = %(d)s".format(
            db.ch_identifier(config.CH_TABLE)
        )
        with db.clickhouse_client() as client:
            return int(client.query(query, parameters={"d": snapshot_date}).result_rows[0][0])
    except db.DatabaseError as exc:
        raise CheckError(str(exc)) from exc


def run(snapshot_date: dt.date | None = None, backend: str = "",
        min_rows: int = 1) -> int:
    """Проверяет наличие данных. Возвращает код возврата процесса."""
    snapshot_date = snapshot_date or dt.date.today()
    backend = (backend or config.STORAGE).lower()

    try:
        if backend == "postgres":
            count = count_postgres(snapshot_date)
        elif backend == "clickhouse":
            count = count_clickhouse(snapshot_date)
        else:
            if backend != "csv":
                log.warning("Неизвестный STORAGE=%r, проверяю CSV", backend)
                backend = "csv"
            count = count_csv(snapshot_date)
    except CheckError as exc:
        log.error("Проверка не выполнена: %s", exc)
        return 1

    where = "{} за {}".format(backend, snapshot_date)
    if count < min_rows:
        log.error("%s: строк %s, ожидалось хотя бы %s - парсер отработал вхолостую",
                  where, count, min_rows)
        return 1

    log.info("%s: строк %s - данные на месте", where, count)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка среза данных за дату")
    cli.add_storage_arguments(parser, storage_help="где проверять (по умолчанию из .env)")
    parser.add_argument("--min-rows", type=int, default=1,
                        help="минимальное количество строк (по умолчанию 1)")
    args = parser.parse_args()

    try:
        snapshot_date = cli.parse_date(args.date)
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    return run(snapshot_date, args.storage or "", args.min_rows)


if __name__ == "__main__":
    sys.exit(main())

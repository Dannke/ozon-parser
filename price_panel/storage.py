"""Сохранение результатов парсинга: CSV, PostgreSQL или ClickHouse.

Бэкенд выбирается переменной окружения STORAGE. Подключения и проверка имён
таблиц вынесены в db.py.

Схема таблиц рассчитана на ежедневный запуск: ключ включает дату среза
(sku + parsed_date), поэтому каждый день добавляется новая строка и копится
история цен и отзывов - по ней DataLens строит динамику.

Повторный запуск за тот же день не плодит дубли, а обновляет строку: PostgreSQL
делает это через ON CONFLICT, ClickHouse - движком ReplacingMergeTree. Это важно
для Airflow, который может перезапустить упавшую задачу, и для парсинга
батчами, где save() вызывается несколько раз за прогон.

В таблицах есть колонка source ("api" или "html"): по ней видно, какие
карточки собраны из JSON, встроенного в HTML, - там art_set и
has_rich_content заведомо пусты. В CSV её нет - там ровно 12 полей задания.
"""

from __future__ import annotations

import contextlib
import csv
import datetime as dt
import os
from collections.abc import Sequence
from pathlib import Path

from . import config, db
from .extract import FIELDS
from .logger import get_logger

log = get_logger("storage")

# Колонки таблиц в БД: ключ, дата среза, источник данных и поля товара.
DB_FIELDS = ("sku", "parsed_date", "source") + tuple(f for f in FIELDS if f != "sku")


class StorageError(RuntimeError):
    """Не удалось сохранить результаты."""


def _db_rows(rows: Sequence[dict], snapshot_date: dt.date) -> list:
    """Готовит значения для вставки: поля товара в порядке DB_FIELDS."""
    prepared = []
    for row in rows:
        values = []
        for field in DB_FIELDS:
            if field == "parsed_date":
                values.append(snapshot_date)
            else:
                values.append(row.get(field))
        prepared.append(tuple(values))
    return prepared


# ---------------------------------------------------------------------- CSV --
def save_csv(rows: Sequence[dict], path: Path) -> None:
    """Записывает строки в CSV (UTF-8 с BOM, чтобы Excel не ломал кириллицу).

    В CSV выгружаются ровно те 12 полей, что перечислены в задании, без
    служебной даты среза и источника: этот файл - снимок последнего запуска.

    Пустой результат тоже записывается, одним заголовком: иначе на месте
    остался бы снимок прошлого запуска, и check_snapshot.py принял бы его
    за сегодняшний.

    Пишем во временный файл и подменяем через os.replace, чтобы обрыв посреди
    записи не оставил обрезанный, но внешне валидный CSV.
    """
    if not rows:
        log.warning("Список товаров пуст - пишу CSV с одним заголовком")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with tmp_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(FIELDS), extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field) for field in FIELDS})
        os.replace(tmp_path, path)
    except OSError as exc:
        # Сообщаем об ошибке записи CSV; неудача уборки временного файла - мелочь.
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)
        raise StorageError("Не удалось записать CSV {}: {}".format(path, exc)) from exc

    log.info("Сохранено строк в CSV: %s -> %s", len(rows), path)


# --------------------------------------------------------------- PostgreSQL --
PG_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    sku              TEXT NOT NULL,
    parsed_date      DATE NOT NULL,
    source           TEXT,
    title            TEXT,
    price            NUMERIC(12, 2),
    rating           NUMERIC(3, 2),
    reviews_total    INTEGER,
    cover_image      TEXT,
    photos_seller    INTEGER,
    videos_seller    INTEGER,
    color            TEXT,
    material         TEXT,
    art_set          TEXT,
    has_rich_content BOOLEAN,
    parsed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (sku, parsed_date)
)
"""

PG_UPSERT = """
INSERT INTO {table} (
    sku, parsed_date, source, title, price, rating, reviews_total, cover_image,
    photos_seller, videos_seller, color, material, art_set, has_rich_content
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (sku, parsed_date) DO UPDATE SET
    source = EXCLUDED.source,
    title = EXCLUDED.title,
    price = EXCLUDED.price,
    rating = EXCLUDED.rating,
    reviews_total = EXCLUDED.reviews_total,
    cover_image = EXCLUDED.cover_image,
    photos_seller = EXCLUDED.photos_seller,
    videos_seller = EXCLUDED.videos_seller,
    color = EXCLUDED.color,
    material = EXCLUDED.material,
    art_set = EXCLUDED.art_set,
    has_rich_content = EXCLUDED.has_rich_content,
    parsed_at = now()
"""


def save_postgres(rows: Sequence[dict], dsn: str, table: str,
                  snapshot_date: dt.date | None = None) -> None:
    """Создаёт таблицу при необходимости и заливает срез за указанную дату."""
    if not rows:
        log.warning("Нечего сохранять: список товаров пуст")
        return

    snapshot_date = snapshot_date or dt.date.today()
    values = _db_rows(rows, snapshot_date)

    try:
        from psycopg2 import sql  # локальный импорт: драйвер нужен не всем

        identifier = db.pg_identifier(table)
        with db.postgres_cursor(dsn) as cursor:
            cursor.execute(sql.SQL(PG_DDL).format(table=identifier))
            cursor.executemany(sql.SQL(PG_UPSERT).format(table=identifier), values)
    except ImportError as exc:
        raise StorageError('Не установлен драйвер: pip install ".[postgres]"') from exc
    except db.DatabaseError as exc:
        raise StorageError(str(exc)) from exc

    log.info("Записано строк в PostgreSQL (%s) за %s: %s", table, snapshot_date, len(rows))


# --------------------------------------------------------------- ClickHouse --
CH_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    sku              String,
    parsed_date      Date,
    source           Nullable(String),
    title            Nullable(String),
    price            Nullable(Float64),
    rating           Nullable(Float64),
    reviews_total    Nullable(Int64),
    cover_image      Nullable(String),
    photos_seller    Nullable(Int64),
    videos_seller    Nullable(Int64),
    color            Nullable(String),
    material         Nullable(String),
    art_set          Nullable(String),
    has_rich_content Nullable(UInt8),
    parsed_at        DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(parsed_at)
ORDER BY (sku, parsed_date)
"""


def save_clickhouse(rows: Sequence[dict], table: str,
                    snapshot_date: dt.date | None = None) -> None:
    """Создаёт таблицу ReplacingMergeTree и вставляет срез за указанную дату."""
    if not rows:
        log.warning("Нечего сохранять: список товаров пуст")
        return

    snapshot_date = snapshot_date or dt.date.today()

    # ClickHouse не понимает булевы None - приводим к 0/1/None явно.
    def to_uint8(value):
        return None if value is None else int(bool(value))

    data = [
        [to_uint8(value) if DB_FIELDS[index] == "has_rich_content" else value
         for index, value in enumerate(row)]
        for row in _db_rows(rows, snapshot_date)
    ]

    try:
        identifier = db.ch_identifier(table)
        with db.clickhouse_client() as client:
            client.command(CH_DDL.format(table=identifier))
            client.insert(identifier, data, column_names=list(DB_FIELDS))
    except db.DatabaseError as exc:
        raise StorageError(str(exc)) from exc

    log.info("Записано строк в ClickHouse (%s) за %s: %s", table, snapshot_date, len(rows))


# ------------------------------------------------------------------ фасад ---
def save(rows: Sequence[dict], backend: str = "", csv_path: Path | None = None,
         snapshot_date: dt.date | None = None) -> None:
    """Сохраняет результаты выбранным бэкендом.

    Порядок важен: сначала БД, потом CSV. База - источник истины для витрин,
    и сбой записи CSV (нет прав на каталог, кончилось место) не должен мешать
    данным дойти до неё.

    CSV при этом пишется всегда, даже при работе с БД: это дешёвая страховка
    на случай, если база недоступна, и удобный артефакт для глазной проверки.
    Ошибка базы поднимается наверх уже после записи CSV - прогон не должен
    выглядеть успешным. Ошибка CSV фатальна, только если другого хранилища нет.

    :param snapshot_date: дата среза для таблиц БД; по умолчанию сегодня.
        Airflow передаёт сюда дату запуска, чтобы перезапуск задачи за прошлый
        день не записал данные сегодняшним числом.
    """
    backend = (backend or config.STORAGE).lower()
    csv_path = csv_path or config.OUTPUT_CSV

    # Служебный бэкенд конвейера panel: результаты уже записаны в PostgreSQL
    # по одному SKU (pipeline.py), а экспорт в CSV выключен в config.yaml.
    if backend == "none":
        return

    db_error: StorageError | None = None
    try:
        if backend == "postgres":
            save_postgres(rows, config.PG_DSN, config.PG_TABLE, snapshot_date)
        elif backend == "clickhouse":
            save_clickhouse(rows, config.CH_TABLE, snapshot_date)
        elif backend != "csv":
            log.warning("Неизвестный STORAGE=%r, сохраняю только в CSV", backend)
    except StorageError as exc:
        db_error = exc
        log.error("В %s записать не удалось (%s) - сохраняю хотя бы CSV", backend, exc)

    try:
        save_csv(rows, csv_path)
    except StorageError as exc:
        if db_error is not None:
            raise StorageError("{}; CSV тоже не записан: {}".format(db_error, exc)) from exc
        if backend == "csv":
            raise
        log.error("Данные в %s записаны, но CSV сохранить не удалось: %s", backend, exc)
    if db_error is not None:
        raise db_error

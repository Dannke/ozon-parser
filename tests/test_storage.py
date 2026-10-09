"""Проверки слоя сохранения: согласованность колонок и запись CSV.

Подключение к БД не требуется - тесты сверяют SQL как текст. Это ловит самую
неприятную ошибку такого кода: рассогласование порядка колонок между
DB_FIELDS, DDL и INSERT, при котором цена молча записывается в рейтинг.

    python test_storage.py        (или: pytest test_storage.py)
"""

from __future__ import annotations

import csv
import datetime as dt
import tempfile
from pathlib import Path

from price_panel import storage
from price_panel.extract import FIELDS

SAMPLE = {
    "sku": "2359066702",
    "title": "Раскраска по номерам",
    "price": 1759.0,
    "rating": 4.9,
    "reviews_total": 1627,
    "cover_image": "https://ir.ozone.ru/s3/cover.jpg",
    "photos_seller": 19,
    "videos_seller": 2,
    "color": "Темно-розовый",
    "material": "Бумага",
    "art_set": "Раскраска",
    "has_rich_content": True,
}


def _columns_from_insert(sql: str) -> list:
    """Список колонок из INSERT INTO ... ( ... ) VALUES."""
    block = sql.split("(", 1)[1].split(")", 1)[0]
    return [name.strip() for name in block.split(",")]


def _columns_from_ddl(sql: str) -> list:
    """Имена колонок из CREATE TABLE (служебные строки пропускаем)."""
    block = sql.split("(", 1)[1]
    columns = []
    for line in block.splitlines():
        line = line.strip()
        service = line.upper().startswith(("PRIMARY", "ENGINE", "ORDER"))
        if not line or line.startswith(")") or service:
            continue
        name = line.split()[0]
        if name.isidentifier():
            columns.append(name)
    return columns


def test_postgres_columns_aligned():
    """Колонки INSERT совпадают с DB_FIELDS, а плейсхолдеров ровно столько же."""
    columns = _columns_from_insert(storage.PG_UPSERT)
    assert columns == list(storage.DB_FIELDS), columns

    placeholders = storage.PG_UPSERT.split("VALUES", 1)[1].count("%s")
    assert placeholders == len(storage.DB_FIELDS), placeholders


def test_postgres_ddl_covers_fields():
    """В таблице есть все поля товара плюс служебные."""
    columns = _columns_from_ddl(storage.PG_DDL)
    for field in storage.DB_FIELDS:
        assert field in columns, field
    assert "parsed_at" in columns


def test_clickhouse_ddl_matches_order():
    """Порядок колонок в ClickHouse совпадает с порядком вставки."""
    columns = _columns_from_ddl(storage.CH_DDL)
    assert columns[:len(storage.DB_FIELDS)] == list(storage.DB_FIELDS), columns


def test_upsert_is_idempotent_by_day():
    """Повторный запуск за тот же день обновляет строку, а не плодит дубли.

    Для Airflow это критично: задача может быть перезапущена.
    """
    assert "ON CONFLICT (sku, parsed_date) DO UPDATE" in storage.PG_UPSERT
    assert "PRIMARY KEY (sku, parsed_date)" in storage.PG_DDL
    assert "ReplacingMergeTree" in storage.CH_DDL
    assert "ORDER BY (sku, parsed_date)" in storage.CH_DDL


def test_db_rows_values_in_order():
    """Значения выстраиваются ровно в порядке DB_FIELDS, дата подставляется."""
    date = dt.date(2026, 9, 22)
    row = storage._db_rows([SAMPLE], date)[0]

    assert len(row) == len(storage.DB_FIELDS)
    values = dict(zip(storage.DB_FIELDS, row, strict=True))
    assert values["sku"] == "2359066702"
    assert values["parsed_date"] == date
    assert values["price"] == 1759.0
    assert values["rating"] == 4.9
    assert values["has_rich_content"] is True


def test_csv_keeps_task_fields():
    """В CSV ровно 12 полей из задания, без служебной даты среза."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "products.csv"
        storage.save_csv([SAMPLE], path)

        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames
            rows = list(reader)

    assert header == list(FIELDS)
    assert "parsed_date" not in (header or [])
    assert len(rows) == 1
    assert rows[0]["title"] == "Раскраска по номерам"


def test_empty_run_rewrites_csv():
    """Пустой прогон не оставляет на месте файл прошлого запуска.

    Регрессия: при пустом списке save_csv выходил, не тронув файл. Снимок
    вчерашнего запуска оставался, и check.py насчитывал по нему
    строки - сегодняшний холостой прогон выглядел успешным.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "products.csv"
        storage.save_csv([SAMPLE], path)
        storage.save_csv([], path)

        with open(path, encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames
            rows = list(reader)

    assert header == list(FIELDS), "заголовок должен остаться на месте"
    assert rows == [], "строки прошлого запуска должны быть стёрты"


def test_csv_write_leaves_no_temp_file():
    """Запись идёт через временный файл, но после себя его не оставляет."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        storage.save_csv([SAMPLE], directory / "products.csv")
        assert sorted(p.name for p in directory.iterdir()) == ["products.csv"]


def test_source_column_lives_in_db_only():
    """Источник данных пишется в БД, но не в CSV.

    В CSV должны остаться ровно те 12 полей, что перечислены в задании, а по
    таблице полезно видеть, какие карточки собраны из JSON в HTML страницы.
    """
    assert "source" in storage.DB_FIELDS
    assert "source" not in FIELDS

    values = storage._db_rows([dict(SAMPLE, source="html")], dt.date(2026, 9, 23))[0]
    row = dict(zip(storage.DB_FIELDS, values, strict=True))
    assert row["source"] == "html"

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "products.csv"
        storage.save_csv([dict(SAMPLE, source="html")], path)
        with open(path, encoding="utf-8-sig", newline="") as handle:
            assert csv.DictReader(handle).fieldnames == list(FIELDS)


def test_csv_failure_does_not_block_database():
    """Сбой записи CSV не мешает данным дойти до базы.

    Регрессия: CSV писался первым, и нехватка прав на каталог блокировала
    загрузку в живую, полностью доступную базу.
    """
    calls = []
    original = storage.save_postgres
    storage.save_postgres = lambda *args, **kwargs: calls.append(args)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # Каталог вместо файла: запись гарантированно не пройдёт.
            blocked = Path(tmp) / "products.csv"
            blocked.mkdir()
            storage.save([SAMPLE], backend="postgres", csv_path=blocked)
    finally:
        storage.save_postgres = original

    assert calls, "база должна быть записана до попытки сохранить CSV"


def test_database_failure_still_writes_csv(monkeypatch):
    """База недоступна - CSV всё равно пишется, а ошибка базы идёт наверх.

    Регрессия: исключение из save_postgres вылетало до записи CSV, хотя CSV и
    задуман страховкой на случай недоступной базы.
    """
    def database_down(*args, **kwargs):
        raise storage.StorageError("Ошибка PostgreSQL: connection refused")

    monkeypatch.setattr(storage, "save_postgres", database_down)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "products.csv"
        try:
            storage.save([SAMPLE], backend="postgres", csv_path=path)
        except storage.StorageError as exc:
            assert "connection refused" in str(exc)
        else:
            raise AssertionError("ошибка базы должна была подняться наверх")
        assert len(path.read_text(encoding="utf-8-sig").splitlines()) == 2


def test_csv_failure_is_fatal_when_csv_is_the_only_backend():
    """Если другого хранилища нет, ошибка CSV обязана быть фатальной."""
    with tempfile.TemporaryDirectory() as tmp:
        blocked = Path(tmp) / "products.csv"
        blocked.mkdir()
        try:
            storage.save([SAMPLE], backend="csv", csv_path=blocked)
        except storage.StorageError:
            return
    raise AssertionError("ошибка записи CSV должна была подняться наверх")


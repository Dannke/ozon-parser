"""Проверки шага контроля: действительно ли срез за дату попал в хранилище.

Подключение к БД не требуется - проверяется ветка CSV, самая коварная из
трёх. В CSV нет колонки с датой, поэтому свежесть файла приходится брать из
времени его изменения; если этого не делать, шаг контроля превращается в
украшение и рапортует об успехе по данным прошлого запуска.

    pytest test_check.py        (или: python test_check.py)
"""

from __future__ import annotations

import contextlib
import datetime as dt
import os
import tempfile
import time
from pathlib import Path

from price_panel.infra import config
from price_panel.legacy import check, storage

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

OK, FAILED = 0, 1


@contextlib.contextmanager
def csv_snapshot(rows, age_days: float = 0.0):
    """Готовит CSV нужного возраста и подставляет его в config.OUTPUT_CSV.

    Подмена атрибута модуля - следствие того, что настройки читаются один раз
    на импорте config. Пока это так, иначе к ним из теста не подобраться.
    """
    original = config.OUTPUT_CSV
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "products.csv"
        storage.save_csv(rows, path)
        if age_days:
            stale = time.time() - age_days * 86400
            os.utime(path, (stale, stale))
        config.OUTPUT_CSV = path
        try:
            yield path
        finally:
            config.OUTPUT_CSV = original


def test_fresh_csv_with_rows_passes():
    """Свежий файл с данными - проверка проходит."""
    with csv_snapshot([SAMPLE]):
        assert check.run(dt.date.today(), "csv", min_rows=1) == OK


def test_empty_csv_fails():
    """Файл есть, но строк в нём нет - парсер отработал вхолостую."""
    with csv_snapshot([]):
        assert check.run(dt.date.today(), "csv", min_rows=1) == FAILED


def test_stale_csv_is_not_counted_as_today():
    """Файл прошлого запуска не засчитывается за сегодняшний результат.

    Регрессия: count_csv считал строки в файле, не глядя на дату. Парсер мог
    неделями возвращать ноль строк (протухли cookies, сменился API, бан по
    IP), а шаг контроля каждый день оставался зелёным по вчерашним данным.
    """
    with csv_snapshot([SAMPLE], age_days=2):
        assert check.run(dt.date.today(), "csv", min_rows=1) == FAILED


def test_min_rows_is_respected():
    """Строк меньше ожидаемого минимума - это отказ."""
    with csv_snapshot([SAMPLE]):
        assert check.run(dt.date.today(), "csv", min_rows=2) == FAILED


def test_missing_csv_fails():
    """Файла нет вовсе - парсер до сохранения не дошёл."""
    original = config.OUTPUT_CSV
    with tempfile.TemporaryDirectory() as tmp:
        config.OUTPUT_CSV = Path(tmp) / "products.csv"
        try:
            assert check.run(dt.date.today(), "csv", min_rows=1) == FAILED
        finally:
            config.OUTPUT_CSV = original


def test_unknown_backend_falls_back_to_csv():
    """Опечатка в STORAGE не должна тихо превращаться в успешную проверку."""
    with csv_snapshot([]):
        assert check.run(dt.date.today(), "mysql", min_rows=1) == FAILED

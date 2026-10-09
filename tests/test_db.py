"""Проверки общего слоя БД: разбор имён таблиц.

Драйверы и живые базы не нужны - проверяется только то, что имя таблицы из
конфигурации не уезжает в SQL как попало.

    pytest test_db.py        (или: python test_db.py)
"""

from __future__ import annotations

from price_panel import db

VALID = ("ozon_products", "public.ozon_products", "_tmp", "T1", "schema_1.table_2")

INVALID = (
    "",
    "ozon products",                      # пробел
    "ozon-products",                      # дефис
    "1table",                             # начинается с цифры
    "a.b.c",                              # слишком много частей
    "ozon_products; DROP TABLE users",    # классика
    "ozon_products--",                    # хвост комментария
    'ozon"products',                      # кавычка
    "таблица",                            # не латиница
)


def _expect_rejected(name: str):
    try:
        db.split_identifier(name)
    except db.DatabaseError:
        return
    raise AssertionError("имя {!r} должно было быть отвергнуто".format(name))


def test_valid_table_names():
    assert db.split_identifier("ozon_products") == ["ozon_products"]
    assert db.split_identifier("public.ozon_products") == ["public", "ozon_products"]
    for name in VALID:
        assert db.split_identifier(name), name


def test_invalid_table_names_are_rejected():
    """Имя таблицы подставляется в SQL текстом - проверка обязана быть строгой."""
    for name in INVALID:
        _expect_rejected(name)


def test_clickhouse_identifier_round_trip():
    assert db.ch_identifier("ozon_products") == "ozon_products"
    assert db.ch_identifier("analytics.ozon_products") == "analytics.ozon_products"
    _expect_rejected("analytics.ozon products")


def test_missing_driver_gives_actionable_message():
    """Если драйвера нет, сообщение должно говорить, что именно ставить."""
    try:
        db._import_psycopg2()
    except db.DatabaseError as exc:
        assert "postgres" in str(exc), exc


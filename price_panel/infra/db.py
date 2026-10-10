"""Общий слой подключений к БД: PostgreSQL и ClickHouse.

Используется и при сохранении (storage.py), и при проверке среза (check.py).
Драйверы импортируются лениво: запуск с STORAGE=csv их не требует.

Про закрытие соединений. У psycopg2 ``with connect(...)`` управляет
ТРАНЗАКЦИЕЙ, а не соединением: на выходе делается commit или rollback, но
соединение остаётся открытым - это документированное поведение, на котором
легко потерять дескриптор. Поэтому менеджеров два: closing() закрывает
соединение, вложенный connection закрывает транзакцию.

Имена таблиц приходят из PG_TABLE / CH_TABLE и подставляются в SQL как
идентификаторы, а не параметры, поэтому они проверяются по белому списку
символов и экранируются.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Generator

from price_panel.infra import config

# Допустимое имя таблицы: идентификатор или схема.идентификатор.
IDENTIFIER_PART_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAX_IDENTIFIER_PARTS = 2


class DatabaseError(RuntimeError):
    """Не удалось подключиться к базе или выполнить запрос."""


# ------------------------------------------------------------ идентификаторы --
def split_identifier(name: str) -> list:
    """Разбирает имя таблицы на части, проверяя каждую."""
    parts = (name or "").split(".")
    if not 1 <= len(parts) <= MAX_IDENTIFIER_PARTS:
        raise DatabaseError("Недопустимое имя таблицы: {!r}".format(name))
    if not all(IDENTIFIER_PART_RE.fullmatch(part) for part in parts):
        raise DatabaseError(
            "Недопустимое имя таблицы: {!r}. Разрешены латиница, цифры и '_', "
            "необязательная схема через точку.".format(name)
        )
    return parts


def pg_identifier(name: str):
    """Имя таблицы как psycopg2.sql.Identifier (с учётом схемы)."""
    from psycopg2 import sql  # локальный импорт: драйвер нужен не всем

    return sql.Identifier(*split_identifier(name))


def ch_identifier(name: str) -> str:
    """Проверенное имя таблицы для ClickHouse.

    clickhouse-connect принимает имя строкой и в DDL, и в insert(), поэтому
    возвращаем его как есть - но только после проверки.
    """
    return ".".join(split_identifier(name))


# ------------------------------------------------------------------ драйверы --
def _import_psycopg2():
    try:
        import psycopg2
    except ImportError as exc:
        raise DatabaseError('Не установлен драйвер PostgreSQL: pip install ".[postgres]"') from exc
    return psycopg2


def _import_clickhouse():
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise DatabaseError(
            'Не установлен драйвер ClickHouse: pip install ".[clickhouse]"'
        ) from exc
    return clickhouse_connect


# --------------------------------------------------------------- соединения --
@contextlib.contextmanager
def postgres_cursor(dsn: str = "") -> Generator:
    """Курсор в транзакции; соединение закрывается на выходе в любом случае."""
    psycopg2 = _import_psycopg2()
    dsn = dsn or config.PG_DSN
    if not dsn:
        raise DatabaseError("Не задан PG_DSN")

    try:
        connection = psycopg2.connect(dsn)
    except psycopg2.Error as exc:
        raise DatabaseError("Ошибка PostgreSQL: {}".format(exc)) from exc

    try:
        with contextlib.closing(connection):
            with connection:  # commit при успехе, rollback при исключении
                with connection.cursor() as cursor:
                    yield cursor
    except psycopg2.Error as exc:
        raise DatabaseError("Ошибка PostgreSQL: {}".format(exc)) from exc


@contextlib.contextmanager
def clickhouse_client() -> Generator:
    """Клиент ClickHouse с параметрами из конфигурации; закрывается на выходе."""
    clickhouse_connect = _import_clickhouse()
    client = None
    try:
        client = clickhouse_connect.get_client(
            host=config.CH_HOST,
            port=config.CH_PORT,
            username=config.CH_USER,
            password=config.CH_PASSWORD,
            database=config.CH_DATABASE,
        )
        yield client
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001 - драйвер поднимает свои классы
        raise DatabaseError("Ошибка ClickHouse: {}".format(exc)) from exc
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()

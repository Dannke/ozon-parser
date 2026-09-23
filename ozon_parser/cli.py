"""Общие аргументы командной строки для parse_ozon.py и check_snapshot.py.

Оба скрипта принимают одну и ту же дату среза и один и тот же список
бэкендов - разбор живёт в одном месте, чтобы форматы не разошлись.
"""

from __future__ import annotations

import argparse
import datetime as dt
from typing import Optional

STORAGE_CHOICES = ("csv", "postgres", "clickhouse")

DATE_HELP = ("дата среза в формате ГГГГ-ММ-ДД "
             "(по умолчанию сегодня; Airflow передаёт дату запуска)")


def add_storage_arguments(parser: argparse.ArgumentParser,
                          storage_help: str = "куда сохранять результат (по умолчанию из .env)"
                          ) -> None:
    """Добавляет --storage и --date - общие для обоих скриптов."""
    parser.add_argument("--storage", choices=list(STORAGE_CHOICES), help=storage_help)
    parser.add_argument("--date", help=DATE_HELP)


def parse_date(value: Optional[str]) -> Optional[dt.date]:
    """Разбирает значение --date.

    :raises ValueError: с текстом, готовым к выводу пользователю.
    """
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(
            "Неверный формат --date: {!r}, ожидается ГГГГ-ММ-ДД".format(value)
        ) from None

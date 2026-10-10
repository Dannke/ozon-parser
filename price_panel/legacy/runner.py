"""Старый сценарий: список SKU -> карточки Ozon -> CSV / таблица ozon_products.

Точка входа parse_ozon.py. Карточки собирает тот же адаптер Ozon, что и
конвейер (marketplaces/ozon/parse.py), а цикл - то же ядро (core/collect.py).
Здесь только то, чем старый сценарий отличается: сохранение партиями через
storage.save и код возврата по доле успеха.

Запуск:
    python parse_ozon.py                               # SKU из config.DEFAULT_SKUS
    python parse_ozon.py 2359066702 2829800382         # SKU аргументами
    python parse_ozon.py --file skus.txt --storage csv # SKU из файла
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from price_panel.core.collect import RowBatches, collect
from price_panel.infra import config
from price_panel.infra.logger import get_logger
from price_panel.infra.skus import read_skus_file, select_range
from price_panel.legacy import cli, storage
from price_panel.marketplaces.ozon import parse, session

log = get_logger("parse_ozon")


def save_results(
    rows: list, backend: str, output: Path | None, snapshot_date: dt.date | None
) -> bool:
    """Сохраняет собранное. Возвращает False, если сохранить не удалось."""
    try:
        storage.save(rows, backend=backend, csv_path=output, snapshot_date=snapshot_date)
        return True
    except storage.StorageError as exc:
        log.error("Сохранение не удалось: %s", exc)
        return False


def run(
    skus,
    storage_backend: str = "",
    output: Path | None = None,
    snapshot_date: dt.date | None = None,
    batch_size: int | None = None,
    min_success_rate: float = 0.0,
) -> int:
    """Парсит список SKU и сохраняет результат. Возвращает код возврата процесса.

    :param snapshot_date: дата среза для таблиц БД (по умолчанию сегодня).
    :param batch_size: сбрасывать собранное в хранилище каждые N товаров
        (0 - только в конце). Промежуточное сохранение безопасно: в БД идёт
        upsert по (sku, parsed_date), CSV переписывается целиком и атомарно.
    :param min_success_rate: минимальная доля успешных SKU от длины входа.
    """
    if not skus:
        log.error("Список SKU пуст")
        return 1
    # Битый файл сессии - выходим, не трогая снимок прошлого запуска.
    try:
        session.load_session(config.COOKIES_FILE)
    except session.SessionError as exc:
        log.error("%s", exc)
        return 1

    batches = RowBatches(
        lambda rows: save_results(rows, storage_backend, output, snapshot_date),
        config.BATCH_SIZE if batch_size is None else batch_size,
    )
    result = collect(parse.OzonAdapter(), skus, batches, parse.breaker(), log)
    if not batches.flush():
        return 1
    if not result.success or result.blocked:
        return 1
    if min_success_rate > 0 and result.success_rate < min_success_rate:
        log.error(
            "Доля успеха %.0f%% ниже порога %.0f%% - считаю прогон неудачным",
            result.success_rate * 100,
            min_success_rate * 100,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Парсер карточек товаров ozon.ru")
    parser.add_argument("skus", nargs="*", help="список SKU через пробел")
    parser.add_argument("--file", type=Path, help="файл со списком SKU (по одному в строке)")
    cli.add_storage_arguments(parser)
    parser.add_argument("--output", type=Path, help="путь к CSV-файлу результата")
    parser.add_argument("--offset", type=int, default=0, help="пропустить первые N SKU списка")
    parser.add_argument(
        "--limit",
        type=int,
        help="обработать не больше N SKU (вместе с --offset делит длинный список на части)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="сбрасывать собранное в хранилище каждые N товаров "
        "(0 - только в конце; по умолчанию из .env)",
    )
    parser.add_argument(
        "--min-success-rate",
        type=float,
        default=0.0,
        help="минимальная доля успешных SKU от длины списка (0..1); "
        "ниже неё прогон считается неудачным",
    )
    args = parser.parse_args()

    try:
        snapshot_date = cli.parse_date(args.date)
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    try:
        skus = args.skus or (read_skus_file(args.file) if args.file else config.DEFAULT_SKUS)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1

    return run(
        select_range(skus, args.offset, args.limit),
        storage_backend=args.storage or "",
        output=args.output,
        snapshot_date=snapshot_date,
        batch_size=args.batch_size,
        min_success_rate=args.min_success_rate,
    )


if __name__ == "__main__":
    sys.exit(main())

"""Сверка price_history.list_price WB с разбором сырых ответов (raw_products).

Миграция 0006 заполнила list_price старых строк SQL-запросом, повторяющим
правило marketplaces/wb/extract.py. Здесь каждый сырой ответ проходит через
сам product_record - тот же код, что пишет новые строки, - и сравнивается с
базой:

- list_price расходится с разбором - строка заполнена не так, как её записал
  бы конвейер; apply=True исправляет (по (marketplace, run_id, sku), повторный
  запуск ничего не меняет);
- old_price есть, но не равен разбору - ошибка разбора: зачёркнутая выше цены
  обязана совпадать с basic.

Сырые ответы хранятся 90 дней - старше сверять не с чем.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from price_panel.infra.warehouse import Warehouse
from price_panel.marketplaces.wb.extract import product_record


def _same(stored, parsed) -> bool:
    if stored is None or parsed is None:
        return stored is parsed
    return Decimal(str(stored)) == parsed


def reconcile(wh: Warehouse, apply: bool = False) -> Counter:
    """Счётчики checked / differ / old_mismatch / unparsed / updated (см. модуль)."""
    stats = Counter(checked=0, differ=0, old_mismatch=0, unparsed=0, updated=0)
    fixes = []
    for run_id, sku, card, old_price, list_price in wh.raw_price_rows():
        try:
            parsed = product_record(card)["list_price"]
        except (AttributeError, KeyError, TypeError, ValueError, ArithmeticError):
            stats["unparsed"] += 1
            continue
        stats["checked"] += 1
        if not _same(list_price, parsed):
            stats["differ"] += 1
            fixes.append((run_id, sku, parsed))
        if old_price is not None and not _same(old_price, parsed):
            stats["old_mismatch"] += 1
    if apply and fixes:
        stats["updated"] = wh.set_list_prices(fixes)
    return stats

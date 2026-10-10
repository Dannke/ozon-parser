"""Понятия конвейера, общие для всех маркетплейсов и слоёв."""

from __future__ import annotations

from dataclasses import dataclass

# Код маркетплейса в таблице marketplaces. Пока конвейер собирает только Ozon.
OZON = "ozon"

# Код выхода parse, если прогон остановил предохранитель. Планировщик по нему
# отличает блокировку сайта (имеет смысл повторить через несколько часов) от
# прочих неудач (повтор не поможет).
EXIT_BLOCKED = 3


@dataclass
class SkuOutcome:
    """Итог обработки одного SKU: запись о товаре либо причина неудачи.

    error_type - код для parse_errors: not_found, antibot, fetch_error, ...
    seconds - время разбора этого SKU без паузы между товарами.
    """

    sku: str
    product: dict | None = None
    error_type: str = ""
    error_message: str = ""
    attempts: int = 0
    seconds: float = 0.0

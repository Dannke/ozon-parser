"""Списки SKU на входе: чтение файла и нарезка на части."""

from __future__ import annotations

from pathlib import Path

from price_panel.infra.logger import get_logger

log = get_logger("parse_ozon")


def read_skus_file(path: Path) -> list:
    """Читает список SKU из текстового файла (по одному в строке).

    Пустые строки и комментарии (#, в том числе с отступом) пропускаются,
    дубли убираются с сохранением порядка. Понимает и CSV с заголовком sku
    (так выгружает panel команда discover): берётся первый столбец, строка
    заголовка пропускается.
    """
    if not path.exists():
        raise FileNotFoundError("Файл со списком SKU не найден: {}".format(path))
    # utf-8-sig: CSV, сохранённый из Excel, начинается с BOM.
    lines = (
        line.split(",", 1)[0].strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
    )
    return list(
        dict.fromkeys(
            line for line in lines if line and not line.startswith("#") and line.lower() != "sku"
        )
    )


def select_range(skus: list, offset: int = 0, limit: int | None = None) -> list:
    """Часть списка SKU: с какого начать и сколько взять.

    Позволяет разложить длинный список на несколько задач планировщика: при
    ~10-15 с на товар сорокаминутная задача успевает около двухсот SKU.
    """
    selected = skus[max(offset, 0) :]
    if limit is not None and limit >= 0:
        selected = selected[:limit]
    if len(selected) != len(skus):
        log.info(
            "Из списка (%s) взято SKU: %s (offset=%s, limit=%s)",
            len(skus),
            len(selected),
            offset,
            limit,
        )
    return selected

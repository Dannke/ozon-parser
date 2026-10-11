"""Разбор ответа карточек Wildberries (u-card v4) в запись того же контракта, что у Ozon.

Правило цены сверено на живых карточках 11.10.2026 (docs/verification-log.md):

    sizes[].price.product  - цена на странице, копейки  -> price
    sizes[].price.basic    - зачёркнутая цена, копейки  -> old_price

Цена с WB Кошельком на странице на 2 % ниже price, но её считает сам сайт, в
ответе её нет, поэтому card_price у WB пуст. Количество на складе WB для
сторонних обрезает, поэтому хранится только наличие.
"""

from __future__ import annotations

from decimal import Decimal

from price_panel.core.quality import discount_pct

KOPECKS = Decimal(100)

# Служебные поля без смысла для истории: токены, маски флагов.
RAW_DROP = ("logs", "payload", "dtype", "ndtype", "dtypeMask", "viewFlags", "saleConditions")


def cards_by_sku(body: dict) -> dict:
    """{sku: карточка} из ответа u-card; SKU - строкой, как в panel."""
    products = body.get("products")
    if not isinstance(products, list):
        data = body.get("data")
        products = data.get("products") if isinstance(data, dict) else None
    return {str(card["id"]): card for card in products or [] if "id" in card}


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in RAW_DROP}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def product_record(card: dict) -> dict:
    """Запись о товаре из карточки WB.

    ponytail: у размеров бывает разная цена - берётся минимальная среди
    размеров с ценой (у всех сверенных 11.10.2026 товаров цена размеров была
    одна); если понадобится цена конкретного размера, нужна запись по размерам.
    """
    prices = [size["price"] for size in card.get("sizes") or [] if size.get("price")]
    best = min(prices, key=lambda price: price["product"]) if prices else None
    price = Decimal(best["product"]) / KOPECKS if best else None
    basic = Decimal(best["basic"]) / KOPECKS if best and best.get("basic") else None
    # Зачёркнутой нет, если она не выше цены: сайт её тогда не показывает.
    old_price = basic if basic is not None and price is not None and basic > price else None
    feedbacks = card.get("feedbacks")
    colors = card.get("colors") or []
    return {
        "sku": str(card["id"]),
        "title": card.get("name") or None,
        "cover_image": None,
        "color": colors[0].get("name") if colors else None,
        "material": None,
        "art_set": None,
        "has_rich_content": None,
        "photos_seller": card.get("pics"),
        "videos_seller": None,
        "price": price,
        "card_price": None,
        "old_price": old_price,
        "discount_pct": discount_pct(price, old_price),
        "is_available": price is not None and bool(card.get("totalQuantity")),
        # Рейтинг 0 у товара без отзывов - «нет оценки», а не ноль баллов.
        "rating": card.get("reviewRating") if feedbacks else None,
        "reviews_total": feedbacks,
        "source": "api",
        "details": True,
        "raw": _clean(card),
    }

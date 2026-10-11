"""Цена товара на границе доверия: скидка и проверки правдоподобия записи.

Цены приходят с чужого сайта. Неверно прочитанная цена (копейки вместо
рублей, не тот размер, сломанный формат ответа) тихо испортила бы историю и
выводы по ней, поэтому сомнительная запись становится ошибкой invalid_data, а
не наблюдением.
"""

from __future__ import annotations

from decimal import Decimal

# Выше - почти наверняка ошибка разбора (копейки, лишний разряд), а не цена.
MAX_PRICE = Decimal("10000000")


def discount_pct(price, old_price):
    """Скидка price относительно зачёркнутой цены, % (None - зачёркнутой нет)."""
    if not price or not old_price:
        return None
    if old_price <= price:
        return 0.0
    return round((old_price - price) / old_price * 100, 2)


def problems(product: dict) -> str:
    """Что неправдоподобно в записи о товаре; пустая строка - всё в порядке."""
    price, old_price = product.get("price"), product.get("old_price")
    rating, reviews = product.get("rating"), product.get("reviews_total")
    if price is None:
        return "товар в наличии, но без цены" if product.get("is_available") else ""
    if not 0 < price < MAX_PRICE:
        return "цена {} вне диапазона (0; {})".format(price, MAX_PRICE)
    if old_price is not None and old_price < price:
        return "зачёркнутая цена {} ниже цены {}".format(old_price, price)
    if rating is not None and not 0 <= rating <= 5:
        return "рейтинг {} вне 0..5".format(rating)
    if reviews is not None and reviews < 0:
        return "отрицательное число отзывов {}".format(reviews)
    return ""

"""Поля истории цен: цена с Ozon Картой, зачёркнутая цена, скидка, наличие.

Структура webPrice проверена на живых карточках 29.09.2026; фикстура та же,
что в test_extract.py.
"""

from __future__ import annotations

import json

from test_extract import FIXTURE, card_html

from price_panel.marketplaces.ozon.extract import (
    FIELDS,
    discount_pct,
    extract_offer,
    parse_html,
    parse_product,
)


def test_offer_fields_from_web_price():
    product = parse_product(FIXTURE, "1")
    assert product["price"] == 12490.0
    assert product["card_price"] == 11240.0
    assert product["old_price"] == 19990.0
    assert product["discount_pct"] == 37.52
    assert product["is_available"] is True


def test_out_of_stock_widget_means_not_available():
    page = {
        "widgetStates": {
            "webOutOfStock-1-default-1": json.dumps({"price": "990 ₽"}),
            "webPrice-2-default-1": json.dumps({"price": "990 ₽", "isAvailable": True}),
        }
    }
    assert extract_offer(page)["is_available"] is False


def test_hidden_original_price_is_not_old_price():
    page = {
        "widgetStates": {
            "webPrice-2-default-1": json.dumps(
                {"price": "990 ₽", "originalPrice": "990 ₽", "showOriginalPrice": False}
            )
        }
    }
    assert extract_offer(page)["old_price"] is None


def test_availability_from_web_sale_when_price_widget_is_silent():
    """webPriceDecreasedCompact - не webPrice: префикс не должен его подхватить."""
    page = {
        "widgetStates": {
            "webPriceDecreasedCompact-1-default-1": json.dumps({"isAvailable": True}),
            "webSale-2-default-1": json.dumps({"offer": {"isAvailable": False}}),
        }
    }
    assert extract_offer(page)["is_available"] is False


def test_discount_pct():
    assert discount_pct(1463.0, 7990.0) == 81.69
    assert discount_pct(1000.0, 1000.0) == 0.0
    assert discount_pct(1000.0, None) is None
    assert discount_pct(None, 1000.0) is None


def test_html_fallback_takes_availability_from_json_ld():
    html = card_html(
        {"webProductHeading-1-default-1": {"title": "Кресло"}},
        json_ld={
            "@type": "Product",
            "name": "Кресло",
            "offers": {"price": "5000", "availability": "https://schema.org/OutOfStock"},
        },
    )
    product = parse_html(html, "1")
    assert product["price"] == 5000.0
    assert product["is_available"] is False
    assert product["discount_pct"] is None


def test_new_fields_do_not_leak_into_csv_contract():
    """CSV и таблица старого сценария - по-прежнему ровно 12 полей FIELDS."""
    assert not {"card_price", "old_price", "discount_pct", "is_available"} & set(FIELDS)

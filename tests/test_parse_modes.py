"""Порядок источников при разборе карточки: HTML, API, вторая часть карточки.

Браузер не нужен: открытие страницы и запросы к API подменены, проверяется,
к чему парсер обращается в каждом режиме и что попадает в запись.
"""

from __future__ import annotations

import copy
from typing import cast

from playwright.sync_api import Page
from test_extract import FIXTURE, card_html

from price_panel.marketplaces.ozon import parse

PAGE = cast(Page, None)
FULL_HTML = card_html(
    {
        "webProductHeading-1-default-1": {"title": "Кресло из HTML"},
        "webPrice-2-default-1": {"price": "9 990 ₽"},
    }
)
NO_PRICE_HTML = card_html({"webProductHeading-1-default-1": {"title": "Кресло из HTML"}})

HTML_ONLY = parse.ParseOptions(price_source="html", details_for=frozenset())
HTML_WITH_DETAILS = parse.ParseOptions(price_source="html", details_for=frozenset({"1"}))
API_NO_DETAILS = parse.ParseOptions(price_source="api", details_for=frozenset())


class FakeSite:
    """Подменяет вкладку с карточкой и API и записывает обращения к ним."""

    def __init__(self, html: str):
        self.html = html
        self.calls: list = []

    def open_product_page(self, page, sku, settle=True):
        self.calls.append("open" if settle else "open-fast")
        return self.html

    def settle_page(self, page, sku):
        self.calls.append("settle")

    def fetch_page_json(self, page, sku, page_index=1):
        self.calls.append("api-{}".format(page_index))
        return copy.deepcopy(FIXTURE)


def use(monkeypatch, html: str) -> FakeSite:
    site = FakeSite(html)
    for name in ("open_product_page", "settle_page", "fetch_page_json"):
        monkeypatch.setattr(parse, name, getattr(site, name))
    return site


def test_html_mode_needs_nothing_but_the_page(monkeypatch):
    site = use(monkeypatch, FULL_HTML)
    product = parse.parse_sku(PAGE, "1", HTML_ONLY)
    assert site.calls == ["open-fast"]
    assert (product["source"], product["price"], product["details"]) == ("html", 9990.0, False)


def test_html_mode_fetches_only_the_second_part_when_due(monkeypatch):
    site = use(monkeypatch, FULL_HTML)
    product = parse.parse_sku(PAGE, "1", HTML_WITH_DETAILS)
    assert site.calls == ["open-fast", "settle", "api-2"]
    assert product["details"] is True and product["art_set"] == "CH-545-GREY"
    assert product["price"] == 9990.0  # цена - из HTML, а не из ответа API


def test_html_without_price_falls_back_to_api(monkeypatch):
    site = use(monkeypatch, NO_PRICE_HTML)
    product = parse.parse_sku(PAGE, "1", HTML_ONLY)
    assert site.calls == ["open-fast", "settle", "api-1"]
    assert (product["source"], product["price"]) == ("api", 12490.0)


def test_api_mode_skips_the_second_part_when_not_due(monkeypatch):
    site = use(monkeypatch, FULL_HTML)
    product = parse.parse_sku(PAGE, "1", API_NO_DETAILS)
    assert site.calls == ["open", "api-1"]
    assert product["source"] == "api" and product["details"] is False


def test_default_options_are_the_legacy_scenario(monkeypatch):
    """parse_ozon.py: API и обе части карточки у каждого SKU, как раньше."""
    site = use(monkeypatch, FULL_HTML)
    product = parse.parse_sku(PAGE, "1")
    assert site.calls == ["open", "api-1", "api-2"]
    assert product["details"] is True


def test_sold_out_card_in_html_is_enough():
    """«Нет в наличии» без цены - тоже карточка: идти в API незачем."""
    sold_out = parse.parse_html(
        card_html(
            {
                "webProductHeading-1-default-1": {"title": "Кресло"},
                "webOutOfStock-2-default-1": {"price": ""},
            }
        ),
        "1",
    )
    assert parse.html_is_enough(sold_out)
    assert not parse.html_is_enough(parse.parse_html(NO_PRICE_HTML, "1"))

"""Discovery без сети: разбор ответов источников.

Живой ozon.ru не нужен: ответы API повторяют структуру, снятую с сайта
29.09.2026. Обход страниц и реакция на отказ Ozon - в test_discovery_crawl.py.
"""

from __future__ import annotations

import json

from price_panel.app import discovery
from price_panel.core.sampling import listing_position

# Урезанный ответ /api/entrypoint-api.bx/page/json/v2?url=/category/...?page=3
LISTING_JSON = {
    "pageInfo": {"pageType": "category", "url": "/category/smartfony-15502/?page=3"},
    "widgetStates": {
        "tileGridDesktop-3669724-default-1": json.dumps(
            {
                "items": [
                    {"sku": 111111, "action": {"link": "/product/smartfon-a-111111/?at=token"}},
                    {"sku": 999, "action": {"link": "/category/aksessuary-7697/"}},  # не товар
                    {"sku": 222222, "action": {"link": "/product/smartfon-b-222222/"}},
                ]
            }
        ),
        "infiniteVirtualPaginator-3618992-default-1": json.dumps({"nextPage": "/x?page=4"}),
    },
}


def test_parse_listing_takes_sku_from_links():
    candidates = discovery.parse_listing(LISTING_JSON, page=3)
    assert [c.sku for c in candidates] == ["111111", "222222"]
    assert candidates[0].url == "https://www.ozon.ru/product/smartfon-a-111111/"
    # Место в выдаче считается по позиции плитки на странице, включая чужие.
    assert [c.position for c in candidates] == [
        listing_position(3, 0, 3),
        listing_position(3, 2, 3),
    ]
    assert all(c.page == 3 for c in candidates)


def test_parse_listing_without_tiles_is_empty():
    assert discovery.parse_listing({"widgetStates": {"header-1": "{}"}}, page=900) == []


def test_parse_data_ozon_prefers_link_over_field():
    data = {
        "totals": "1000",
        "items": [
            {"sku": "149222760", "link": "https://www.ozon.ru/product/149222760"},
            {"sku": "1", "link": "https://www.ozon.ru/product/379913038"},  # расхождение
            {"sku": "4238578733", "link": ""},  # нет ссылки
        ],
    }
    candidates = discovery.parse_data_ozon(data, offset=200)
    assert [c.sku for c in candidates] == ["149222760", "379913038", "4238578733"]
    assert [c.position for c in candidates] == [201, 202, 203]
    assert {c.page for c in candidates} == {3}


def test_challenge_body_is_recognized():
    assert discovery.is_challenge_body("<html><title>Доступ ограничен</title></html>")
    assert not discovery.is_challenge_body('{"widgetStates": {}}')

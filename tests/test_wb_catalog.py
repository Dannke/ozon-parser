"""Discovery Wildberries без сети: разбор выдачи, источник и обход категории.

Живой wildberries.ru не нужен: WbCatalogSource проверяется на поддельной
вкладке, которая на открытие страницы категории «делает» запрос выдачи
сайта и отвечает по сценарию, а общий обход (discover_listing) - на
поддельном источнике со страницами по 100 товаров.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest
from playwright.sync_api import Error as PlaywrightError

from price_panel.app import discovery
from price_panel.app.settings import SettingsError, parse_settings
from price_panel.core.sampling import Candidate, make_rng
from price_panel.infra import config
from price_panel.marketplaces.wb import catalog

PAGE_URL = "https://www.wildberries.ru/catalog/elektronika/tehnika-dlya-kuhni/x?xsubject=631%3B1188"
SITE_URL = (
    "https://www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search"
    "?appType=1&dest=-365341&query=menu_v3_9794+x&resultset=catalog&sort=popular&xsubject=631;1188"
)
SETTINGS_DATA = {
    "discovery": {
        "categories": [
            {
                "name": "phones",
                "panel_size": 5,
                "url": "https://www.ozon.ru/category/smartfony-15502/",
            }
        ]
    },
    "marketplaces": {
        "wb": {
            "dest": "-365341",
            "categories": [{"name": "coffee", "url": PAGE_URL, "panel_size": 300}],
        }
    },
}
CATEGORY = parse_settings(SETTINGS_DATA).category("coffee", "wb")


def listing(*ids) -> str:
    return json.dumps(
        {"metadata": {}, "total": 2740, "products": [{"id": i, "name": "x"} for i in ids]}
    )


# ----------------------------------------------------------------- разбор --
def test_parse_search_takes_ids_in_listing_order():
    candidates = catalog.parse_search({"products": [{"id": 11}, {"name": "без id"}, {"id": 12}]}, 3)
    assert [c.sku for c in candidates] == ["11", "12"]
    # Место в выдаче - по 100 на страницу, даже если страница неполная.
    assert [c.position for c in candidates] == [201, 202]
    assert candidates[0].page == 3
    assert candidates[0].url == "https://www.wildberries.ru/catalog/11/detail.aspx"
    assert catalog.parse_search({"products": None}, 1) == []


def test_page_addresses():
    """Адрес страницы категории получает page; номер страницы читается из запроса сайта."""
    assert catalog.category_page_url(PAGE_URL, 1) == PAGE_URL
    assert catalog.category_page_url(PAGE_URL, 7) == PAGE_URL + "&page=7"
    plain = "https://www.wildberries.ru/catalog/elektronika/smartfony-i-telefony/vse-smartfony"
    assert catalog.category_page_url(plain, 3) == plain + "?page=3"
    assert catalog.listing_page(SITE_URL) == 1
    assert catalog.listing_page(SITE_URL + "&page=12") == 12
    assert catalog.is_listing_request(SITE_URL)
    assert not catalog.is_listing_request(
        SITE_URL.replace("resultset=catalog", "resultset=filters")
    )


def test_wb_categories_from_settings():
    """Категории WB - со своим источником, адресом WB и хвостом по умолчанию."""
    assert CATEGORY.source == "wb_catalog"
    assert (CATEGORY.tail_max_page, CATEGORY.tail_items_per_page) == (99, 5)
    assert CATEGORY.top_size == 120
    data = json.loads(json.dumps(SETTINGS_DATA))
    data["marketplaces"]["wb"]["categories"][0]["url"] = "https://www.ozon.ru/category/x-1/"
    with pytest.raises(SettingsError, match="wildberries"):
        parse_settings(data)
    data["marketplaces"]["wb"]["categories"] = [{"name": "a", "url": PAGE_URL, "panel_size": 1}] * 2
    with pytest.raises(SettingsError, match="повторяются"):
        parse_settings(data)
    with pytest.raises(SettingsError, match="phones"):
        parse_settings(SETTINGS_DATA).category("phones", "wb")


# ------------------------------------------------------------ источник --
class FakeResponse:
    def __init__(self, url: str, status: int, body: str):
        self.url = url
        self.status = status
        self.body = body

    def text(self) -> str:
        return self.body


class FakeContext:
    def __init__(self, token: bool):
        self.token = token

    def cookies(self) -> list:
        return [{"name": "x_wbaas_token"}] if self.token else []


class FakePage:
    """Вкладка WB: на открытие страницы категории «сайт» запрашивает выдачу.

    answers - по одному на открытие страницы категории: (статус, тело) ответа
    сайта, None - сайт выдачу не запросил, исключение - goto упал.
    ignore_page - сайт не понимает page в адресе и всегда грузит первую страницу.
    """

    def __init__(self, answers=(), token=True, ignore_page=False):
        self.context = FakeContext(token)
        self.answers = list(answers)
        self.ignore_page = ignore_page
        self.handlers: list = []
        self.opened: list = []

    def on(self, event, handler):
        assert event == "response"
        self.handlers.append(handler)

    def goto(self, url, **kwargs):
        self.opened.append(url)
        if not url.startswith(PAGE_URL):
            return  # главная
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return
        number = 1 if self.ignore_page or "page=" not in url else int(url.split("page=")[1])
        site = SITE_URL + ("&page={}".format(number) if number > 1 else "")
        for handler in self.handlers:
            handler(FakeResponse(site, *answer))
            handler(FakeResponse(SITE_URL.replace("catalog", "filters"), 200, "{}"))

    def wait_for_timeout(self, ms):
        pass


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    sleeps: list = []
    monkeypatch.setattr(catalog.time, "sleep", sleeps.append)
    monkeypatch.setattr(catalog, "RESPONSE_WAIT_SECONDS", 0)
    monkeypatch.setattr(config, "MAX_RETRIES", 2)
    return sleeps


def source_for(page: FakePage) -> catalog.WbCatalogSource:
    source = catalog.WbCatalogSource(lambda: cast(Any, page), delay=0.0)
    source.open(CATEGORY)
    return source


def test_pages_are_opened_like_a_visitor_and_read_from_the_sites_answer():
    """Своих запросов выдачи нет: страница открывается по адресу, ответ - сайта."""
    page = FakePage(answers=[(200, listing(1, 2)), (200, listing(3, 4))])
    source = source_for(page)

    assert [c.sku for c in source.fetch(CATEGORY, 1) or []] == ["1", "2"]
    assert [c.sku for c in source.fetch(CATEGORY, 5) or []] == ["3", "4"]
    assert page.opened == ["https://www.wildberries.ru/", PAGE_URL, PAGE_URL + "&page=5"]
    assert source.requests == 2 and not source.blocked


def test_empty_page_is_the_end_of_listing():
    source = source_for(FakePage(answers=[(200, listing())]))
    assert source.fetch(CATEGORY, 100) == []
    assert not source.blocked


@pytest.mark.parametrize("status", [403, 429, 498])
def test_refusal_stops_the_source_without_retries(status):
    page = FakePage(answers=[(status, "")])
    source = source_for(page)
    assert source.fetch(CATEGORY, 2) is None
    assert source.blocked and len(page.opened) == 2
    assert source.fetch(CATEGORY, 3) is None and len(page.opened) == 2


def test_failed_browser_check_blocks_before_any_page():
    page = FakePage(token=False)
    source = source_for(page)
    assert source.blocked and source.fetch(CATEGORY, 1) is None
    assert page.opened == ["https://www.wildberries.ru/"]


def test_other_page_is_never_taken_for_the_requested_one():
    """WB проигнорировал page и показал первую: страница пропущена, а не подменена."""
    page = FakePage(answers=[(200, listing(1))] * 3, ignore_page=True)
    source = source_for(page)
    assert source.fetch(CATEGORY, 4) is None
    assert not source.blocked and len(page.opened) == 1 + 3


def test_ordinary_errors_are_retried_then_skipped(no_waits):
    page = FakePage(answers=[(502, ""), PlaywrightError("net::ERR_RESET"), None, (200, listing(5))])
    source = catalog.WbCatalogSource(lambda: cast(Any, page), delay=0.0)
    source.open(CATEGORY)
    config.MAX_RETRIES = 3
    assert [c.sku for c in source.fetch(CATEGORY, 2) or []] == ["5"]
    assert not source.blocked and len(no_waits) == 3

    broken = FakePage(answers=[(500, "")] * 4)
    source = source_for(broken)
    assert source.fetch(CATEGORY, 2) is None
    assert not source.blocked and len(broken.opened) == 1 + 4


# --------------------------------------------------------------- обход --
class FakeWbListing(catalog.WbCatalogSource):
    """Выдача WB из depth страниц по 100 товаров; глубже - пустые."""

    def __init__(self, depth: int):  # без браузера
        self.depth = depth
        self.requested: list = []
        self.requests = 0
        self.blocked = False

    def open(self, category):
        pass

    def fetch(self, category, page_number):
        self.requested.append(page_number)
        self.requests += 1
        if page_number > self.depth:
            return []
        return [
            Candidate(sku="{}-{}".format(page_number, i), position=i, page=page_number)
            for i in range(100)
        ]


def test_crawl_uses_wb_page_size_for_top_zone():
    """Top 120 - две страницы WB; хвост - глубже, по 5 со страницы, в пределах выдачи."""
    source = FakeWbListing(depth=60)
    result = discovery.discover_listing(source, CATEGORY, 120, 180, set(), make_rng("s", "coffee"))
    assert sorted({c.page for c in result.top}) == [1, 2]
    tail_pages = {c.page for c in result.tail}
    assert len(tail_pages) >= 36 and all(3 <= p <= 60 for p in tail_pages)

    # Донабор одного хвоста начинается за зоной top WB (3-я страница), а не Ozon (16-я).
    source = FakeWbListing(depth=60)
    result = discovery.discover_listing(source, CATEGORY, 0, 10, set(), make_rng("s", "coffee"))
    assert min(c.page for c in result.tail) >= 3 and 1 not in source.requested


class StubWarehouse:
    marketplace = "wb"


def test_wb_discovery_must_be_enabled_and_never_exports_csv():
    with pytest.raises(SettingsError, match="enabled"):
        discovery.run_discovery(cast(Any, StubWarehouse()), parse_settings(SETTINGS_DATA))
    data = json.loads(json.dumps(SETTINGS_DATA))
    data["marketplaces"]["wb"]["enabled"] = True
    data["discovery"]["export_csv"] = "data/panel_skus.csv"
    # CSV panel - вход parse_ozon.py: panel WB туда не выгружается.
    assert discovery.export_panel(cast(Any, StubWarehouse()), parse_settings(data)) is None

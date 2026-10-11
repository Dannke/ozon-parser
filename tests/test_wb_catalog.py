"""Discovery Wildberries без сети: разбор выдачи, источник и обход категории.

Живой wildberries.ru не нужен: WbCatalogSource проверяется на поддельной
вкладке, которая «делает» запрос выдачи сайта и отвечает на наши запросы по
сценарию, а общий обход (discover_listing) - на поддельном источнике со
страницами по 100 товаров.
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


def listing(*ids) -> dict:
    return {"metadata": {}, "total": 2740, "products": [{"id": i, "name": "x"} for i in ids]}


# ----------------------------------------------------------------- разбор --
def test_parse_search_takes_ids_in_listing_order():
    candidates = catalog.parse_search({"products": [{"id": 11}, {"name": "без id"}, {"id": 12}]}, 3)
    assert [c.sku for c in candidates] == ["11", "12"]
    # Место в выдаче - по 100 на страницу, даже если страница неполная.
    assert [c.position for c in candidates] == [201, 202]
    assert candidates[0].page == 3
    assert candidates[0].url == "https://www.wildberries.ru/catalog/11/detail.aspx"
    assert catalog.parse_search({"products": None}, 1) == []


def test_page_url_changes_only_the_page():
    assert catalog.page_url(SITE_URL, 1) == SITE_URL
    url = catalog.page_url(SITE_URL, 7)
    assert "page=7" in url and "xsubject=631;1188" in url and "sort=popular" in url
    assert "query=menu_v3_9794+x" in url
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
class FakeRequest:
    def __init__(self, status: int, body: str):
        self.url = SITE_URL
        self.status = status
        self.body = body

    def all_headers(self) -> dict:
        return {"cookie": "c", "user-agent": "ua", "deviceid": "site_1", "x-spa-version": "14"}

    def response(self):
        return self

    def text(self) -> str:
        return self.body


class FakeContext:
    def __init__(self, token: bool):
        self.token = token

    def cookies(self) -> list:
        return [{"name": "x_wbaas_token"}] if self.token else []


SITE_ANSWER = (200, json.dumps(listing(1, 2)))


class FakePage:
    """Вкладка WB: на открытие категории «сайт» делает запрос выдачи.

    answers - ответы на наши запросы (fetch) по порядку: (статус, тело) или исключение.
    """

    def __init__(self, site=SITE_ANSWER, answers=(), token=True):
        self.context = FakeContext(token)
        self.site = site
        self.answers = list(answers)
        self.handlers: list = []
        self.opened: list = []
        self.fetched: list = []

    def on(self, event, handler):
        self.handlers.append(handler)

    def goto(self, url, **kwargs):
        self.opened.append(url)
        if url == PAGE_URL and self.site:
            for handler in self.handlers:
                handler(FakeRequest(*self.site))

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, script, args):
        url, headers = args
        self.fetched.append((url, headers))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return {"status": status, "body": body}


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    sleeps: list = []
    monkeypatch.setattr(catalog.time, "sleep", sleeps.append)
    monkeypatch.setattr(config, "MAX_RETRIES", 2)
    return sleeps


def source_for(page: FakePage) -> catalog.WbCatalogSource:
    source = catalog.WbCatalogSource(lambda: cast(Any, page), delay=0.0)
    source.open(CATEGORY)
    return source


def test_first_page_comes_from_the_sites_own_request():
    page = FakePage(answers=[(200, json.dumps(listing(3, 4)))])
    source = source_for(page)

    assert page.opened == ["https://www.wildberries.ru/", PAGE_URL]
    assert [c.sku for c in source.fetch(CATEGORY, 1) or []] == ["1", "2"]
    assert page.fetched == []  # первую страницу повторно не запрашиваем
    assert [c.sku for c in source.fetch(CATEGORY, 2) or []] == ["3", "4"]
    url, headers = page.fetched[0]
    assert "page=2" in url and "xsubject=631;1188" in url
    # Заголовки - сайта, без тех, что браузер ставит сам.
    assert headers == {"deviceid": "site_1", "x-spa-version": "14"}
    assert source.requests == 2


def test_empty_page_is_the_end_of_listing():
    source = source_for(FakePage(answers=[(200, json.dumps(listing()))]))
    assert source.fetch(CATEGORY, 100) == []
    assert not source.blocked


@pytest.mark.parametrize("status", [403, 429, 498])
def test_refusal_stops_the_source_without_retries(status):
    page = FakePage(answers=[(status, "")])
    source = source_for(page)
    assert source.fetch(CATEGORY, 2) is None
    assert source.blocked and len(page.fetched) == 1
    assert source.fetch(CATEGORY, 3) is None and len(page.fetched) == 1


def test_refused_site_request_or_browser_check_blocks_before_any_fetch():
    source = source_for(FakePage(site=(498, "")))
    assert source.blocked and source.fetch(CATEGORY, 1) is None
    source = source_for(FakePage(token=False))
    assert source.blocked and source.site is None


def test_category_page_is_reopened_after_fetches_per_view():
    """Шестой подряд запрос с одного показа WB отверг - страница открывается заново."""
    answers = [(200, json.dumps(listing(n))) for n in range(10, 16)]
    page = FakePage(answers=answers)
    source = source_for(page)
    for number in range(2, 8):
        assert source.fetch(CATEGORY, number)
    # 4 запроса с первого показа, затем показ заново и ещё 2.
    assert page.opened == ["https://www.wildberries.ru/", PAGE_URL, PAGE_URL]
    assert len(page.fetched) == 6 and source.requests == 8


def test_refusal_on_reopen_stops_the_source():
    page = FakePage(answers=[(200, json.dumps(listing(n))) for n in range(4)])
    source = source_for(page)
    for number in range(2, 6):
        source.fetch(CATEGORY, number)
    page.site = (498, "")
    assert source.fetch(CATEGORY, 6) is None
    assert source.blocked and len(page.fetched) == 4


def test_page_without_listing_request_is_a_failure():
    with pytest.raises(catalog.SiteRefused, match="выдачу"):
        source_for(FakePage(site=None))


def test_ordinary_errors_are_retried_then_skipped(no_waits):
    page = FakePage(
        answers=[(502, ""), PlaywrightError("net::ERR_RESET"), (200, json.dumps(listing(5)))]
    )
    source = source_for(page)
    assert [c.sku for c in source.fetch(CATEGORY, 2) or []] == ["5"]
    assert not source.blocked and len(no_waits) == 2

    broken = FakePage(answers=[(500, "")] * 3)
    source = source_for(broken)
    assert source.fetch(CATEGORY, 2) is None
    assert not source.blocked and len(broken.fetched) == 3


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

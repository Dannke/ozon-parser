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


def test_listing_page_numbers():
    assert catalog.listing_page(SITE_URL) == 1
    assert catalog.listing_page(SITE_URL + "&page=12") == 12
    assert catalog.is_listing_request(SITE_URL)
    assert not catalog.is_listing_request(
        SITE_URL.replace("resultset=catalog", "resultset=filters")
    )


def test_wb_categories_from_settings():
    """Категории WB - со своим источником, адресом WB и хвостом по умолчанию."""
    assert CATEGORY.source == "wb_catalog"
    assert (CATEGORY.tail_max_page, CATEGORY.tail_items_per_page) == (40, 5)
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


class FakeKeyboard:
    def __init__(self, page: FakePage):
        self.page = page

    def press(self, key: str) -> None:
        assert key == "End"
        self.page.presses += 1
        self.page.answer()


class FakePage:
    """Вкладка WB с бесконечной выдачей: страницы сайт грузит по порядку.

    answers - что сайт отдаёт: первое - на открытие страницы категории,
    дальше - по одному на прокрутку. Ответ - (статус, тело) одной страницы,
    список таких - сайт загрузил сразу несколько, None - ничего не загрузил.
    """

    def __init__(self, answers=(), token=True):
        self.context = FakeContext(token)
        self.keyboard = FakeKeyboard(self)
        self.answers = list(answers)
        self.handlers: list = []
        self.opened: list = []
        self.presses = 0
        self.next_page = 1

    def on(self, event, handler):
        assert event == "response"
        self.handlers.append(handler)

    def answer(self) -> None:
        answer = self.answers.pop(0) if self.answers else None
        for status, body in [answer] if isinstance(answer, tuple) else answer or []:
            number = self.next_page
            self.next_page += 1
            site = SITE_URL + ("&page={}".format(number) if number > 1 else "")
            for handler in self.handlers:
                handler(FakeResponse(site, status, body))
                handler(FakeResponse(SITE_URL.replace("catalog", "filters"), 200, "{}"))

    def goto(self, url, **kwargs):
        self.opened.append(url)
        if url == PAGE_URL:
            self.answer()

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


def test_pages_come_from_the_site_by_scrolling():
    """Своих запросов нет: первую страницу сайт грузит сам, следующие - на прокрутку."""
    page = FakePage(answers=[(200, listing(1)), (200, listing(2)), (200, listing(3))])
    source = source_for(page)

    assert page.opened == ["https://www.wildberries.ru/", PAGE_URL]
    assert [c.sku for c in source.fetch(CATEGORY, 1) or []] == ["1"]
    assert page.presses == 0
    assert [c.sku for c in source.fetch(CATEGORY, 3) or []] == ["3"]
    assert page.presses == 2
    # Пройденная страница - из памяти, без новой прокрутки.
    assert [c.sku for c in source.fetch(CATEGORY, 2) or []] == ["2"]
    assert page.presses == 2 and source.requests == 3
    assert source.fetch(CATEGORY, 3)[0].position == 201  # type: ignore[index]


def test_several_pages_from_one_scroll():
    page = FakePage(answers=[(200, listing(1)), [(200, listing(2)), (200, listing(3))]])
    source = source_for(page)
    assert [c.sku for c in source.fetch(CATEGORY, 3) or []] == ["3"]
    assert page.presses == 1


def test_empty_page_is_the_end_of_listing():
    page = FakePage(answers=[(200, listing(1)), (200, listing())])
    source = source_for(page)
    assert source.fetch(CATEGORY, 2) == []
    assert source.fetch(CATEGORY, 9) == [] and page.presses == 1
    assert not source.blocked


@pytest.mark.parametrize("status", [403, 429, 498])
def test_refusal_stops_the_source(status):
    page = FakePage(answers=[(200, listing(1)), (status, "")])
    source = source_for(page)
    assert source.fetch(CATEGORY, 2) is None
    assert source.blocked and page.presses == 1
    assert source.fetch(CATEGORY, 3) is None and page.presses == 1
    # Уже загруженное остаётся.
    assert source.fetch(CATEGORY, 1)


def test_refused_first_page_or_browser_check_blocks():
    source = source_for(FakePage(answers=[(498, "")]))
    assert source.blocked and source.fetch(CATEGORY, 1) is None
    page = FakePage(token=False)
    source = source_for(page)
    assert source.blocked and page.opened == ["https://www.wildberries.ru/"]


def test_page_without_listing_request_is_a_failure():
    with pytest.raises(catalog.SiteRefused, match="выдачу"):
        source_for(FakePage(answers=[None]))


def test_stalled_scroll_ends_the_listing():
    """Прокрутка не грузит ничего - после повторов глубже не идём."""
    page = FakePage(answers=[(200, listing(1)), (200, listing(2))])
    source = source_for(page)
    assert source.fetch(CATEGORY, 5) == []
    assert page.presses == 1 + 3  # страница 2, затем 3 пустые попытки
    assert source.fetch(CATEGORY, 2) and source.fetch(CATEGORY, 7) == []
    assert page.presses == 4 and not source.blocked


def test_unparsed_answer_is_not_a_page(no_waits):
    page = FakePage(answers=[(200, listing(1)), (502, ""), (200, listing(3))])
    source = source_for(page)
    # Страница 2 не разобралась, сайт пошёл дальше - её нет, но 3 есть.
    assert [c.sku for c in source.fetch(CATEGORY, 3) or []] == ["3"]
    assert 2 not in source.pages


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

"""Discovery без сети: обход страниц листинга и реакция на отказ Ozon.

Живой ozon.ru не нужен: обход проверяется на поддельном источнике, который
помнит, какие страницы у него запрашивали, а ListingSource - на поддельной
вкладке, отдающей заранее заданные ответы API.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, cast

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from price_panel.app import discovery
from price_panel.app.settings import CategoryConfig
from price_panel.core.sampling import Candidate, listing_position, make_rng
from price_panel.infra import config

CATEGORY = CategoryConfig(
    name="phones",
    panel_size=20,
    top_ratio=0.4,
    url="https://www.ozon.ru/category/smartfony-15502/",
    tail_max_page=300,
    tail_items_per_page=2,
)


class FakeListing(discovery.ListingSource):
    """Листинг из depth страниц по 8 товаров; глубже - пустые страницы.

    broken - страницы, которые не отдаются (обычный сбой); block_after - после
    стольких запросов Ozon «ограничивает запросы» и больше ничего не отдаёт.
    """

    def __init__(self, depth: int, broken: Iterable = (), block_after: int = 0):  # без браузера
        self.depth = depth
        self.broken = set(broken)
        self.block_after = block_after
        self.requested: list = []
        self.requests = 0
        self.blocked = False

    def open(self, category):
        pass

    def fetch(self, category, page_number):
        if self.blocked:
            return None
        if self.block_after and self.requests >= self.block_after:
            self.blocked = True
            return None
        self.requested.append(page_number)
        self.requests += 1
        if page_number in self.broken:
            return None
        if page_number > self.depth:
            return []
        return [
            Candidate(
                sku="{}-{}".format(page_number, i),
                position=listing_position(page_number, i, 8),
                page=page_number,
            )
            for i in range(8)
        ]


def crawl(source, top_needed, tail_needed, exclude=()):
    return discovery.discover_listing(
        source, CATEGORY, top_needed, tail_needed, set(exclude), make_rng("s", "phones")
    )


# ------------------------------------------------------------ обход ------
def test_top_is_read_page_by_page_and_tail_is_deep_and_random():
    source = FakeListing(depth=500)
    result = crawl(source, 8, 12)
    top_pages = sorted({c.page for c in result.top})
    tail_pages = sorted({c.page for c in result.tail})

    assert top_pages == [1]  # 8 товаров top - одна страница
    assert len(tail_pages) == 6  # 12 товаров хвоста по 2 со страницы
    assert all(2 <= p <= CATEGORY.tail_max_page for p in tail_pages)
    assert len(source.requested) == len(set(source.requested)), "страница запрошена дважды"
    assert result.depth is None  # конец выдачи не встретился
    assert not result.blocked


def test_top_skips_skus_already_in_panel():
    source = FakeListing(depth=500)
    result = crawl(source, 8, 0, exclude={"1-{}".format(i) for i in range(8)})
    assert source.requested == [1, 2]
    assert {c.page for c in result.top} == {1, 2}


def test_short_listing_shrinks_tail_range():
    """Выдача кончилась раньше tail_max_page: пустые страницы сужают диапазон."""
    result = crawl(FakeListing(depth=40), 8, 12)
    assert len({c.page for c in result.tail}) == 6
    assert all(c.page <= 40 for c in result.tail)
    assert result.depth is not None and result.depth < CATEGORY.tail_max_page


def test_tail_only_top_up_starts_after_top_zone():
    """Донабор одного хвоста: top не запрашивается, хвост - за зоной top."""
    source = FakeListing(depth=500)
    result = crawl(source, 0, 4)
    assert result.top == []
    assert 1 not in source.requested
    assert all(c.page > 1 for c in result.tail)


def test_broken_tail_page_is_skipped_not_fatal():
    source = FakeListing(depth=500, broken=range(2, 301, 2))  # все чётные страницы сбоят
    result = crawl(source, 8, 6)
    assert len({c.page for c in result.tail}) == 3
    assert all(c.page % 2 == 1 for c in result.tail)
    assert result.skipped_pages > 0


def test_broken_top_page_keeps_what_was_found():
    """Сбой посреди top не роняет категорию: найденное остаётся."""
    source = FakeListing(depth=500, broken={3})
    result = crawl(source, 40, 0)
    assert {c.page for c in result.top} == {1, 2}
    assert result.skipped_pages == 1


def test_block_during_tail_returns_partial_result_and_stops_requests():
    """Ozon ограничил запросы: собранное сохраняется, новых запросов нет."""
    source = FakeListing(depth=500, block_after=4)  # top 1 страница + 3 страницы хвоста
    result = crawl(source, 8, 12)
    assert result.blocked
    assert {c.page for c in result.top} == {1}
    assert len({c.page for c in result.tail}) == 3
    assert source.requests == 4


def test_block_during_top_skips_tail():
    source = FakeListing(depth=500, block_after=2)
    result = crawl(source, 40, 12)
    assert result.blocked
    assert {c.page for c in result.top} == {1, 2}
    assert result.tail == []


# ------------------------------------------------ ListingSource.fetch ------
LISTING_OK = json.dumps(
    {
        "widgetStates": {
            "tileGridDesktop-1-default-1": json.dumps(
                {"items": [{"action": {"link": "/product/phone-123456/"}}]}
            )
        }
    }
)


class FakePage:
    """Вкладка, отдающая по очереди заданные ответы API (или исключения)."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls = 0
        self.sessions = 0

    def evaluate(self, script, url):
        self.calls += 1
        response: Any = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def waits(monkeypatch):
    """Подменяет time.sleep и повторное открытие категории; возвращает журнал."""
    journal: dict = {"sleeps": [], "reopens": 0}

    def fake_open(self, category):
        journal["reopens"] += 1

    monkeypatch.setattr(discovery.time, "sleep", journal["sleeps"].append)
    monkeypatch.setattr(discovery.ListingSource, "open", fake_open)
    monkeypatch.setattr(config, "MAX_RETRIES", 2)
    return journal


def source_for(responses, backoff=(10, 60, 180)):
    """Источник на поддельной вкладке; page.sessions - сколько раз брали новую сессию."""
    page = FakePage(responses)

    def new_page():
        page.sessions += 1
        return cast(Page, page)

    return discovery.ListingSource(new_page, delay=0.001, jitter=0, block_backoff=backoff), page


def ok():
    return {"status": 200, "body": LISTING_OK}


def test_403_waits_by_backoff_and_recovers(waits):
    source, page = source_for([{"status": 403, "body": ""}, {"status": 403, "body": ""}, ok()])
    items = source.fetch(CATEGORY, 5)
    assert items is not None and [c.sku for c in items] == ["123456"]
    assert [w for w in waits["sleeps"] if w >= 1] == [10, 60]
    assert waits["reopens"] == 2
    # Отказ прилипает к сессии: после каждого ожидания - новая сессия.
    assert page.sessions == 1 + 2
    assert not source.blocked


def test_challenge_page_and_429_count_as_block(waits):
    challenge = {"status": 200, "body": "<html><title>Доступ ограничен</title></html>"}
    source, _ = source_for([challenge, {"status": 429, "body": ""}, ok()])
    assert source.fetch(CATEGORY, 5)
    assert [w for w in waits["sleeps"] if w >= 1] == [10, 60]


def test_persistent_block_stops_the_source(waits):
    source, page = source_for([{"status": 403, "body": ""}] * 4, backoff=(10, 60, 180))
    assert source.fetch(CATEGORY, 5) is None
    assert source.blocked
    assert [w for w in waits["sleeps"] if w >= 1] == [10, 60, 180]
    calls = page.calls
    # Дальше источник ничего не запрашивает - блокировку не продлеваем.
    assert source.fetch(CATEGORY, 6) is None
    assert page.calls == calls


def test_ordinary_errors_use_short_retries_not_backoff(waits):
    source, page = source_for(
        [PlaywrightError("Execution context was destroyed"), {"status": 500, "body": ""}, ok()]
    )
    assert source.fetch(CATEGORY, 5)
    assert all(w < 1 for w in waits["sleeps"]), "обычный сбой не должен ждать минутами"
    assert page.sessions == 1, "обычный сбой повторяется в той же сессии"
    assert not source.blocked


def test_ordinary_errors_give_up_after_max_retries(waits):
    source, page = source_for([{"status": 500, "body": ""}] * 3)
    assert source.fetch(CATEGORY, 5) is None
    assert page.calls == 3  # 1 + MAX_RETRIES
    assert not source.blocked  # это не блокировка: идём дальше

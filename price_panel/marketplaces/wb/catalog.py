"""Discovery Wildberries: товары категории из выдачи самого сайта.

Выдачу категории сайт получает запросом

    /__internal/u-search/exactmatch/ru/common/v18/search?query=<запрос категории>&page=N

по 100 товаров на страницу. Наши собственные запросы выдачи WB терпит
недолго: с чужим query - сразу 498, с тем же query и другой page - около
шести за сессию, дальше 498, и повторный показ страницы этого не меняет
(discovery 11.10.2026, 18:39 и 19:25). Поэтому источник своих запросов
выдачи не делает вовсе - он листает сайт, как человек:

  1. проходит проверку браузера на главной (как адаптер карточек);
  2. открывает страницу категории из config.yaml (фильтры, xsubject, - в её
     адресе) с page=N и берёт ответ на запрос выдачи, который сделал сам
     сайт для этой страницы; между страницами - пауза request_delay.

Выдача кончается раньше 100-й страницы (сотая - уже пустая): пустая
страница - конец. Отказ WB (403 / 429 / 498) - стоп без повторов: капчу и
проверки источник не решает, найденное сохраняется.
"""

from __future__ import annotations

import json
import random
import time
import urllib.parse
from collections.abc import Callable
from typing import Protocol

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from price_panel.core.sampling import Candidate, listing_position
from price_panel.infra import config
from price_panel.infra.logger import get_logger
from price_panel.marketplaces.wb.adapter import (
    BLOCK_STATUSES,
    CARD_URL,
    SITE_REQUEST_WAIT_SECONDS,
    SiteRefused,
    pass_browser_check,
)

log = get_logger("discovery")

SEARCH_PATH = "/__internal/u-search/"
PAGE_SIZE = 100
RESPONSE_WAIT_SECONDS = SITE_REQUEST_WAIT_SECONDS


class Category(Protocol):
    """Что источнику нужно от категории config.yaml."""

    @property
    def name(self) -> str: ...

    @property
    def url(self) -> str: ...


def is_listing_request(url: str) -> bool:
    return SEARCH_PATH in url and "resultset=catalog" in url


def listing_page(url: str) -> int:
    """Номер страницы в запросе выдачи сайта: без page - первая."""
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    try:
        return int(query.get("page") or 1)
    except ValueError:
        return 0


def category_page_url(url: str, page: int) -> str:
    """Адрес страницы категории с нужной страницей выдачи; первая - как в config.yaml."""
    if page == 1:
        return url
    return "{}{}page={}".format(url, "&" if "?" in url else "?", page)


def parse_search(body: dict, page: int) -> list:
    """Кандидаты из ответа выдачи: SKU - id товара, позиция - место в выдаче."""
    products = [p for p in body.get("products") or [] if isinstance(p, dict) and p.get("id")]
    return [
        Candidate(
            sku=str(product["id"]),
            # Размер страницы - всегда 100: последняя бывает неполной.
            position=listing_position(page, index, PAGE_SIZE),
            page=page,
            url=CARD_URL.format(sku=product["id"]),
        )
        for index, product in enumerate(products)
    ]


class WbCatalogSource:
    """Выдача категории WB для общего обхода discovery (discover_listing).

    :param new_page: вкладка в новой чистой сессии браузера.
    """

    page_size = PAGE_SIZE

    def __init__(self, new_page: Callable[[], Page], delay: float, jitter: float = 0.0):
        self.new_page = new_page
        self.delay = delay
        self.jitter = jitter
        self.requests = 0
        self.blocked = False
        self.page: Page | None = None
        self._responses: list = []
        self._last_done = 0.0

    def _pause(self) -> None:
        """Пауза от конца предыдущей страницы, со случайным разбросом."""
        target = self.delay * random.uniform(1 - self.jitter, 1 + self.jitter)
        wait = target - (time.monotonic() - self._last_done)
        if wait > 0:
            time.sleep(wait)

    def _refused(self, category: Category, reason: str) -> None:
        self.blocked = True
        log.error(
            "%s: WB отказал (%s) - discovery остановлен, найденное сохраняется",
            category.name,
            reason,
        )

    def open(self, category: Category) -> None:
        """Новая сессия: проверка браузера WB и слежение за ответами выдачи сайта."""
        page = self.page = self.new_page()
        page.on(
            "response",
            lambda response: (
                self._responses.append(response) if is_listing_request(response.url) else None
            ),
        )
        try:
            pass_browser_check(page)
        except SiteRefused as exc:
            self._refused(category, str(exc))

    def _site_response(self, page: Page, number: int):
        """Ответ сайта на запрос выдачи страницы number, если он был."""
        deadline = time.monotonic() + RESPONSE_WAIT_SECONDS
        while True:
            for response in self._responses:
                if listing_page(response.url) == number:
                    return response
            if time.monotonic() >= deadline:
                return None
            page.wait_for_timeout(500)

    def _view(self, page: Page, category: Category, number: int) -> tuple:
        """Открывает страницу выдачи: (HTTP-статус ответа сайта, тело) или (0, причина)."""
        self._responses.clear()
        self._pause()
        log.info("%s: открываю страницу выдачи %s", category.name, number)
        try:
            page.goto(
                category_page_url(category.url, number),
                wait_until="domcontentloaded",
                timeout=config.PAGE_TIMEOUT,
            )
            response = self._site_response(page, number)
            if response is None:
                # Не та страница (WB проигнорировал page) за нужную не выдаётся.
                return 0, "сайт не запросил выдачу страницы {}".format(number)
            return response.status, response.text() if response.status == 200 else ""
        except PlaywrightError as exc:
            return 0, str(exc).splitlines()[0]
        finally:
            self._last_done = time.monotonic()

    def fetch(self, category: Category, page_number: int) -> list | None:
        """Товары одной страницы выдачи; [] - выдача кончилась; None - не получили."""
        if self.blocked or self.page is None:
            return None
        errors = 0
        while True:
            self.requests += 1
            status, body = self._view(self.page, category, page_number)
            if status in BLOCK_STATUSES:
                self._refused(category, "HTTP {}, страница {}".format(status, page_number))
                return None
            try:
                data = json.loads(body) if status == 200 else None
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                return parse_search(data, page_number)
            errors += 1
            reason = "HTTP {}".format(status) if status else body
            if errors > config.MAX_RETRIES:
                log.warning(
                    "%s: страница %s пропущена после %s попыток (%s)",
                    category.name,
                    page_number,
                    errors,
                    reason,
                )
                return None
            log.warning(
                "%s: страница %s не получена (%s), повтор %s из %s",
                category.name,
                page_number,
                reason,
                errors,
                config.MAX_RETRIES,
            )
            time.sleep(self.delay * errors)

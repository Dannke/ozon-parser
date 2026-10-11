"""Discovery Wildberries: товары категории из выдачи самого сайта.

Выдачу категории WB отдаёт запросом

    /__internal/u-search/exactmatch/ru/common/v18/search?query=<запрос категории>&page=N

по 100 товаров на страницу. Запрос с чужим query WB отвергает (498,
разведка 11.10.2026), поэтому источник:

  1. проходит проверку браузера на главной (как адаптер карточек);
  2. открывает страницу категории из config.yaml - фильтры (xsubject) прямо
     в её адресе - и запоминает запрос выдачи, который сделал сам сайт, и
     его ответ: это первая страница, повторно её не запрашиваем;
  3. дальше меняет в этом запросе только page, с паузой между запросами;
  4. после FETCHES_PER_VIEW своих запросов открывает страницу категории
     заново: шестой подряд запрос с одного показа страницы WB отверг (498,
     discovery 11.10.2026, 18:40).

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
    FETCH_JS,
    SiteRefused,
    SiteRequest,
    own_headers,
    pass_browser_check,
    wait_site_request,
)

log = get_logger("discovery")

SEARCH_PATH = "/__internal/u-search/"
PAGE_SIZE = 100
# ponytail: порог - по одному прогону (5 запросов прошли, 6-й - 498); если 498
# вернётся, уменьшить или поднять marketplaces.wb.request_delay.
FETCHES_PER_VIEW = 4


class Category(Protocol):
    """Что источнику нужно от категории config.yaml."""

    @property
    def name(self) -> str: ...

    @property
    def url(self) -> str: ...


def is_listing_request(url: str) -> bool:
    return SEARCH_PATH in url and "resultset=catalog" in url


def page_url(site_url: str, page: int) -> str:
    """Запрос выдачи сайта с другой страницей; первая - как у сайта, без page."""
    if page == 1:
        return site_url
    parts = urllib.parse.urlsplit(site_url)
    query = dict(urllib.parse.parse_qsl(parts.query), page=str(page))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, safe=";")))


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

    def __init__(
        self,
        new_page: Callable[[], Page],
        delay: float,
        jitter: float = 0.0,
        fetches_per_view: int = FETCHES_PER_VIEW,
    ):
        self.new_page = new_page
        self.delay = delay
        self.jitter = jitter
        self.fetches_per_view = fetches_per_view
        self.requests = 0
        self.blocked = False
        self.page: Page | None = None
        self.site: SiteRequest | None = None
        self.first: list | None = None
        self._seen: list = []
        self._view_fetches = 0
        self._last_done = 0.0

    def _pause(self) -> None:
        """Пауза от конца предыдущего запроса, со случайным разбросом."""
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
        """Открывает категорию и берёт запрос выдачи, который сделал сам сайт.

        :raises SiteRefused: сайт не запросил выдачу (не та страница, сменилась вёрстка).
        """
        page = self.page = self.new_page()
        page.on(
            "request",
            lambda request: self._seen.append(request) if is_listing_request(request.url) else None,
        )
        try:
            pass_browser_check(page)
        except SiteRefused as exc:
            self._refused(category, str(exc))
            return
        self.first = self._view(page, category)

    def _view(self, page: Page, category: Category) -> list | None:
        """Показ страницы категории: запрос выдачи сайта и его ответ (первая страница).

        :raises SiteRefused: сайт не запросил выдачу.
        """
        self._seen.clear()
        self._pause()
        log.info("%s: открываю %s", category.name, category.url)
        try:
            page.goto(category.url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT)
            wait_site_request(page, self._seen)
        finally:
            self._last_done = time.monotonic()
        if not self._seen:
            raise SiteRefused("{}: страница не запросила выдачу".format(category.name))
        request = self._seen[0]
        self.site = SiteRequest(url=request.url, headers=own_headers(request.all_headers()))
        self.requests += 1
        self._view_fetches = 0
        response = request.response()
        if response is None:
            return None
        if response.status in BLOCK_STATUSES:
            self._refused(category, "HTTP {} на запрос самого сайта".format(response.status))
            return None
        try:
            body = json.loads(response.text()) if response.status == 200 else None
        except (json.JSONDecodeError, PlaywrightError):
            body = None
        return parse_search(body, 1) if isinstance(body, dict) else None

    def fetch(self, category: Category, page_number: int) -> list | None:
        """Товары одной страницы выдачи; [] - выдача кончилась; None - не получили."""
        if self.blocked or self.site is None or self.page is None:
            return None
        if page_number == 1 and self.first is not None:
            return self.first
        errors = 0
        while True:
            if self._view_fetches >= self.fetches_per_view:
                try:
                    self._view(self.page, category)
                except (SiteRefused, PlaywrightError) as exc:
                    # Не открылась заново - дальше не дёргаем: сбой похож на отказ.
                    self._refused(category, str(exc).splitlines()[0])
                if self.blocked:
                    return None
            self._pause()
            self.requests += 1
            self._view_fetches += 1
            try:
                result = self.page.evaluate(
                    FETCH_JS, [page_url(self.site.url, page_number), self.site.headers]
                )
                status, body = result["status"], result["body"]
            except PlaywrightError as exc:
                status, body = 0, str(exc).splitlines()[0]
            finally:
                self._last_done = time.monotonic()
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

"""Discovery Wildberries: товары категории из выдачи самого сайта.

Выдачу категории сайт получает запросом

    /__internal/u-search/exactmatch/ru/common/v18/search?query=<запрос категории>&page=N

по 100 товаров на страницу. Свои запросы выдачи WB терпит недолго: с чужим
query - сразу 498, с тем же query и другой page - около шести за сессию,
дальше 498; page в адресе самой страницы сайт не понимает и грузит первую
(discovery 11.10.2026, 18:39, 19:25, 20:14). Следующие страницы сайт
запрашивает сам, когда выдачу прокручивают до конца. Поэтому источник
листает выдачу, как человек:

  1. проходит проверку браузера на главной (как адаптер карточек);
  2. открывает страницу категории из config.yaml (фильтры, xsubject, - в её
     адресе): первую страницу выдачи сайт запрашивает сам;
  3. чтобы получить страницу N, прокручивает выдачу (клавиша End) с паузой
     request_delay, пока сайт не загрузит её; пройденные страницы
     запоминает - хвост выбирается из них.

Пустая страница - конец выдачи; если прокрутка несколько раз подряд не
загрузила ничего, глубже источник тоже не идёт. Отказ WB (403 / 429 / 498)
на запрос сайта - стоп без повторов: капчу и проверки источник не решает,
найденное сохраняется.
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

    Страницы приходят только по порядку (прокруткой), поэтому fetch(N)
    догружает всё до N и запоминает: обход просит страницы хвоста по
    возрастанию, и каждая загружается один раз.

    :param new_page: вкладка в новой чистой сессии браузера.
    """

    page_size = PAGE_SIZE

    def __init__(self, new_page: Callable[[], Page], delay: float, jitter: float = 0.0):
        self.new_page = new_page
        self.delay = delay
        self.jitter = jitter
        # Запросов выдачи, которые сделал сайт (наших запросов нет).
        self.requests = 0
        self.blocked = False
        self.page: Page | None = None
        self.pages: dict = {}
        # Первая страница, на которой выдача кончилась (или дальше не прокрутить).
        self.end: int | None = None
        self._responses: list = []
        self._last_done = 0.0

    def _pause(self) -> None:
        """Пауза от предыдущего действия на странице, со случайным разбросом."""
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

    def _wait_responses(self, page: Page) -> None:
        """Ждёт ответ на запрос выдачи, который сделает сайт."""
        deadline = time.monotonic() + RESPONSE_WAIT_SECONDS
        while not self._responses and time.monotonic() < deadline:
            page.wait_for_timeout(500)

    def _collect(self, category: Category) -> None:
        """Раскладывает пришедшие сайту ответы выдачи по страницам."""
        responses, self._responses = self._responses, []
        for response in responses:
            self.requests += 1
            number = listing_page(response.url)
            if response.status in BLOCK_STATUSES:
                self._refused(category, "HTTP {}, страница {}".format(response.status, number))
                return
            try:
                body = json.loads(response.text()) if response.status == 200 else None
            except (json.JSONDecodeError, PlaywrightError):
                body = None
            if not isinstance(body, dict):
                log.warning(
                    "%s: страница %s: ответ сайта не разобран (HTTP %s)",
                    category.name,
                    number,
                    response.status,
                )
                continue
            self.pages[number] = parse_search(body, number)
            log.info("%s: сайт загрузил страницу выдачи %s", category.name, number)
            if not self.pages[number]:
                self.end = number if self.end is None else min(self.end, number)

    def open(self, category: Category) -> None:
        """Новая сессия: проверка браузера WB и страница категории (первая страница выдачи).

        :raises SiteRefused: страница не запросила выдачу (не та страница, сменилась вёрстка).
        """
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
            return
        self._pause()
        log.info("%s: открываю %s", category.name, category.url)
        try:
            page.goto(category.url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT)
            self._wait_responses(page)
        finally:
            self._last_done = time.monotonic()
        self._collect(category)
        if not self.blocked and 1 not in self.pages:
            raise SiteRefused("{}: страница не запросила выдачу".format(category.name))

    def _scroll(self, page: Page) -> None:
        """Прокрутка выдачи до конца: следующую страницу сайт запросит сам."""
        self._pause()
        try:
            page.keyboard.press("End")
            self._wait_responses(page)
        finally:
            self._last_done = time.monotonic()

    def fetch(self, category: Category, page_number: int) -> list | None:
        """Товары одной страницы выдачи; [] - выдача кончилась; None - не получили."""
        if self.page is None:
            return None
        stalls = 0
        while page_number not in self.pages:
            if self.blocked:
                return None
            if self.end is not None and page_number >= self.end:
                return []
            if stalls > config.MAX_RETRIES:
                log.warning(
                    "%s: прокрутка не загрузила страницу %s после %s попыток - глубже "
                    "выдачу не беру",
                    category.name,
                    page_number,
                    stalls,
                )
                self.end = max(self.pages, default=0) + 1
                return []
            loaded = len(self.pages)
            try:
                self._scroll(self.page)
            except PlaywrightError as exc:
                log.warning("%s: прокрутка не удалась: %s", category.name, str(exc).splitlines()[0])
            self._collect(category)
            stalls = stalls + 1 if len(self.pages) == loaded else 0
        return self.pages[page_number]

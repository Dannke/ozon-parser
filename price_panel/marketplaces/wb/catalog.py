"""Discovery Wildberries: товары категории из выдачи самого сайта.

Выдачу категории сайт получает запросом

    /__internal/u-search/exactmatch/ru/common/v18/search?query=<запрос категории>&page=N

по 100 товаров на страницу. page в адресе самой страницы сайт не понимает
(грузит первую), следующие страницы он запрашивает сам, когда выдачу плавно
прокручивают вниз (разведка 11.10.2026, 20:14-20:28). Поэтому источник своих
запросов выдачи не делает - он листает выдачу, как человек:

  1. проходит проверку браузера на главной (как адаптер карточек);
  2. открывает страницу категории из config.yaml (фильтры, xsubject, - в её
     адресе): первую страницу выдачи сайт запрашивает сам;
  3. чтобы получить страницу N, плавно крутит колесо мыши, пока сайт не
     загрузит её, с паузой request_delay между страницами; пройденные
     страницы запоминает - хвост выбирается из них.

498 на запрос выдачи - плановая смена токена: JS самого сайта получает новый
(create-token) и через секунду повторяет запрос. Источник на это время
перестаёт прокручивать (новая прокрутка - новый запрос со старым токеном, ещё
один 498: 12.10.2026, 16:04) и ждёт ответа сайта до TOKEN_RECOVERY_SECONDS.
Не дождался или сайт ответил 403 / 429 - стоп без повторов: капчу и проверки
источник не решает, найденное сохраняется.
Пустая страница - конец выдачи; если прокрутка несколько раз подряд не
загрузила ничего, глубже источник тоже не идёт.
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
    TOKEN_REFRESH,
    SiteRefused,
    pass_browser_check,
)

log = get_logger("discovery")

SEARCH_PATH = "/__internal/u-search/"
PAGE_SIZE = 100
RESPONSE_WAIT_SECONDS = SITE_REQUEST_WAIT_SECONDS
# Плавная прокрутка: шаг колеса и пауза - как у человека; одна попытка догрузки
# крутит не дольше SCROLL_SECONDS (страница выдачи - ~25 шагов, 20:27).
SCROLL_STEP_PX = 400
SCROLL_PAUSE_MS = 400
SCROLL_SECONDS = 30
# Сколько ждать, пока сайт после 498 сам повторит запрос с новым токеном
# (вживую - около секунды, 11.10.2026, 20:27).
TOKEN_RECOVERY_SECONDS = 30


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
        # Страница, на которую сайт получил 498 и ещё не получил ответа с новым токеном.
        self._refreshing: int | None = None
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
            if response.status == TOKEN_REFRESH and number not in self.pages:
                # Сайт сам обновит токен и повторит запрос - ждём его ответа.
                if self._refreshing != number:
                    log.info("%s: страница %s: WB обновляет токен (498)", category.name, number)
                self._refreshing = number
                continue
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
            if self._refreshing == number:
                self._refreshing = None
            if not self.pages[number]:
                self.end = number if self.end is None else min(self.end, number)

    def _wait_recovery(self, page: Page, category: Category) -> None:
        """После 498 ничего не трогает и ждёт, пока сайт повторит запрос с новым токеном."""
        number = self._refreshing
        deadline = time.monotonic() + TOKEN_RECOVERY_SECONDS
        while self._refreshing is not None and not self.blocked:
            page.wait_for_timeout(500)
            self._collect(category)
            if self._refreshing is not None and time.monotonic() >= deadline:
                self._refused(
                    category,
                    "498, сайт не обновил токен за {} с, страница {}".format(
                        TOKEN_RECOVERY_SECONDS, number
                    ),
                )

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
            self._collect(category)
            if self._refreshing is not None:
                self._wait_recovery(page, category)
        finally:
            self._last_done = time.monotonic()
        if not self.blocked and 1 not in self.pages:
            raise SiteRefused("{}: страница не запросила выдачу".format(category.name))

    def _scroll(self, page: Page) -> None:
        """Плавно крутит выдачу вниз, пока сайт не запросит следующую страницу."""
        self._pause()
        deadline = time.monotonic() + SCROLL_SECONDS
        try:
            while True:
                page.mouse.wheel(0, SCROLL_STEP_PX)
                page.wait_for_timeout(SCROLL_PAUSE_MS)
                if self._responses or time.monotonic() >= deadline:
                    break
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
            if self._refreshing is not None:
                self._wait_recovery(self.page, category)
            stalls = stalls + 1 if len(self.pages) == loaded else 0
        return self.pages[page_number]

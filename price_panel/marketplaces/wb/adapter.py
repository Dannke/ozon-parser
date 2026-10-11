"""Адаптер Wildberries для общего цикла сбора (core.collect).

Карточки WB сайт берёт со своего домена (u-card v4), и запрос проходит только
с заголовками самого сайта (deviceid, x-spa-version, x-requested-with) и
токеном его проверки браузера. Поэтому адаптер:

  1. открывает главную в системном Chrome: на неё WB отвечает 498 и проверяет
     браузер, проверку проходит JS самого сайта - мы только ждём токен;
  2. открывает карточку первого SKU и запоминает адрес и заголовки запроса,
     которым сайт получил её данные;
  3. повторяет этот запрос из той же вкладки батчами по batch_size товаров,
     подставив свой регион (dest) и свой список SKU, с паузой между батчами.

Отказ WB (403 / 429 / 498) - это antibot у всех SKU батча: капчу и проверки
адаптер не решает, предохранитель ядра останавливает сбор. Подробности
разведки - docs/verification-log.md (11.10.2026).
"""

from __future__ import annotations

import json
import random
import time
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from price_panel.core.models import SkuOutcome
from price_panel.core.quality import problems
from price_panel.infra import browser as browser_utils
from price_panel.infra import config
from price_panel.infra.logger import get_logger
from price_panel.marketplaces.wb.extract import cards_by_sku, product_record

log = get_logger("wb")

HOME_URL = "https://www.wildberries.ru/"
CARD_URL = "https://www.wildberries.ru/catalog/{sku}/detail.aspx"
DETAIL_PATH = "/__internal/u-card/cards/v4/detail"
BLOCK_STATUSES = (403, 429, 498)
TOKEN_COOKIE = "x_wbaas_token"
TOKEN_WAIT_SECONDS = 30
SITE_REQUEST_WAIT_SECONDS = 15
# Заголовки, которые браузер ставит сам: в fetch их передавать нельзя.
BROWSER_HEADERS = (
    "cookie",
    "user-agent",
    "referer",
    "accept-encoding",
    "host",
    "origin",
    "connection",
    "content-length",
)

FETCH_JS = """
async ([url, headers]) => {
    const response = await fetch(url, {credentials: 'include', headers});
    return {status: response.status, body: await response.text()};
}
"""


class SiteRefused(RuntimeError):
    """WB не пропустил браузер или не показал свой запрос карточки."""


@dataclass(frozen=True)
class SiteRequest:
    """Запрос карточки, которым сайт получил данные: адрес и заголовки."""

    url: str
    headers: dict


def own_headers(headers: dict) -> dict:
    """Заголовки запроса сайта, которые fetch может передать сам."""
    return {
        name: value
        for name, value in headers.items()
        if not name.startswith((":", "sec-")) and name.lower() not in BROWSER_HEADERS
    }


def batch_url(site_url: str, dest: str, skus: Sequence[str]) -> str:
    """Адрес запроса сайта со своим регионом и своим списком SKU."""
    parts = urllib.parse.urlsplit(site_url)
    query = dict(urllib.parse.parse_qsl(parts.query), dest=dest, nm=";".join(skus))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query, safe=";-")))


def outcomes(body: dict, skus: Sequence[str], seconds: float, attempts: int = 1) -> list:
    """Итоги SKU батча: карточки нет в ответе - not_found, сомнительная - invalid_data."""
    cards = cards_by_sku(body)
    result = []
    for sku in skus:
        card = cards.get(sku)
        if card is None:
            result.append(
                SkuOutcome(
                    sku=sku,
                    error_type="not_found",
                    seconds=seconds,
                    error_message="WB не вернул карточку",
                    attempts=attempts,
                )
            )
            continue
        product = product_record(card)
        problem = problems(product)
        if problem:
            result.append(
                SkuOutcome(
                    sku=sku,
                    error_type="invalid_data",
                    seconds=seconds,
                    error_message=problem,
                    attempts=attempts,
                )
            )
        else:
            result.append(SkuOutcome(sku=sku, product=product, seconds=seconds, attempts=attempts))
    return result


def pass_browser_check(page: Page) -> None:
    """Открывает главную WB и ждёт токен её проверки браузера (проходит JS сайта).

    :raises SiteRefused: токена нет за TOKEN_WAIT_SECONDS.
    """
    log.info("Открываю %s (проверка браузера WB)", HOME_URL)
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT)
    deadline = time.monotonic() + TOKEN_WAIT_SECONDS
    while TOKEN_COOKIE not in {c.get("name") for c in page.context.cookies()}:
        if time.monotonic() > deadline:
            raise SiteRefused("проверка браузера WB не пройдена за {} с".format(TOKEN_WAIT_SECONDS))
        page.wait_for_timeout(1000)


def wait_site_request(page: Page, seen: list) -> None:
    """Ждёт, пока сайт сам сделает нужный запрос (seen пополняет обработчик page.on)."""
    deadline = time.monotonic() + SITE_REQUEST_WAIT_SECONDS
    while not seen and time.monotonic() < deadline:
        page.wait_for_timeout(500)


def open_site(page: Page, skus: Sequence[str], pause: Callable) -> SiteRequest:
    """Проходит проверку браузера и ловит запрос карточки самого сайта.

    :param pause: пауза перед открытием карточки - за прогон WB открываются
        всего две-четыре страницы, отдельный предел частоты не нужен.

    :raises SiteRefused: токена нет или сайт не запросил карточку.
    """
    seen: list = []
    page.on("request", lambda request: seen.append(request) if DETAIL_PATH in request.url else None)
    pass_browser_check(page)
    for sku in skus[:3]:
        pause()
        log.info("Открываю карточку %s, чтобы взять запрос сайта", sku)
        page.goto(
            CARD_URL.format(sku=sku), wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT
        )
        wait_site_request(page, seen)
        if seen:
            request = seen[0]
            return SiteRequest(url=request.url, headers=own_headers(request.all_headers()))
    raise SiteRefused("сайт не запросил карточку ни для одного из первых SKU")


class WbAdapter:
    """Сбор карточек WB для ядра: браузер, батчи, регион, темп."""

    def __init__(self, dest: str, batch_size: int, delay: float, sleep: Callable = time.sleep):
        self.dest = dest
        self.batch_size = batch_size
        self.delay = delay
        self.sleep = sleep

    def collect(self, skus: list):
        with sync_playwright() as playwright:
            browser = browser_utils.launch(playwright)
            context = browser_utils.new_context(browser)
            try:
                page = context.new_page()
                try:
                    site = open_site(page, skus, lambda: self.sleep(self.delay))
                except (SiteRefused, PlaywrightError) as exc:
                    log.error("WB: %s", exc)
                    # Как отказ сайта: предохранитель остановит сбор, повтор - позже.
                    for sku in skus[: self.batch_size]:
                        yield SkuOutcome(sku=sku, error_type="antibot", error_message=str(exc))
                    return "WB не пропустил браузер: {}".format(exc)

                def fetch(url: str) -> tuple:
                    result = page.evaluate(FETCH_JS, [url, site.headers])
                    return result["status"], result["body"]

                yield from self.batches(skus, site, fetch)
            finally:
                browser_utils.close_quietly(context, browser)
        return None

    def batches(self, skus: Sequence[str], site: SiteRequest, fetch: Callable):
        """Итоги SKU по батчам: fetch(url) -> (HTTP-статус, тело)."""
        for start in range(0, len(skus), self.batch_size):
            chunk = list(skus[start : start + self.batch_size])
            if start:
                self.sleep(self.delay * (1 + random.random() * 0.3))
            started = time.monotonic()
            status, body, attempts = self._fetch_with_retries(
                fetch, batch_url(site.url, self.dest, chunk)
            )
            seconds = (time.monotonic() - started) / len(chunk)
            if status in BLOCK_STATUSES:
                log.error("WB ответил HTTP %s на батч из %s SKU", status, len(chunk))
                for sku in chunk:
                    yield SkuOutcome(
                        sku=sku,
                        error_type="antibot",
                        seconds=seconds,
                        error_message="WB ответил HTTP {}".format(status),
                        attempts=attempts,
                    )
                continue
            try:
                parsed = json.loads(body) if status == 200 else None
            except json.JSONDecodeError:
                parsed = None
            if not isinstance(parsed, dict):
                for sku in chunk:
                    yield SkuOutcome(
                        sku=sku,
                        error_type="fetch_error",
                        seconds=seconds,
                        error_message="WB: HTTP {}, ответ не разобран".format(status),
                        attempts=attempts,
                    )
                continue
            log.info("WB: батч %s SKU, карточек в ответе %s", len(chunk), len(cards_by_sku(parsed)))
            yield from outcomes(parsed, chunk, seconds, attempts)

    def _fetch_with_retries(self, fetch: Callable, url: str) -> tuple:
        """(статус, тело, попыток): сбои сети и 5xx - повтор, отказ WB - сразу."""
        status, body = 0, ""
        for attempt in range(1, config.MAX_RETRIES + 2):
            try:
                status, body = fetch(url)
            except PlaywrightError as exc:
                status, body = 0, str(exc)
            if status == 200 or status in BLOCK_STATUSES or attempt > config.MAX_RETRIES:
                return status, body, attempt
            log.warning("WB: HTTP %s, повтор %s из %s", status, attempt, config.MAX_RETRIES)
            self.sleep(config.REQUEST_DELAY * attempt)
        return status, body, config.MAX_RETRIES + 1

"""Разбор карточек товаров ozon.ru и адаптер Ozon для общего цикла сбора.

OzonAdapter поднимает браузер (с сохранённой сессией, если она есть, - см.
get_cookies.py; для карточек вход не нужен) и отдаёт итог каждого SKU ядру
(core.collect): оно пишет его в базу или CSV и следит за серией отказов.
Этот адаптер используют и конвейер (app/pipeline.py), и старый сценарий
parse_ozon.py (legacy/runner.py).

Почему браузер, а не requests. Ozon закрыт антибот-защитой: запрос из
requests.Session даже с действующими cookies получает HTTP 403 - и на API,
и на HTML-страницу. Защита проверяет, что запрос сделан настоящим браузером
(JS-проверка, отпечаток TLS), поэтому работа идёт в Playwright: cookies
из cookies.json загружаются в контекст браузера так же, как в
requests.Session загружался бы cookie jar.

Откуда берутся данные:
  * внутренний JSON-эндпоинт, из которого фронтенд Ozon собирает страницу:
        /api/entrypoint-api.bx/page/json/v2?url=/product/<sku>/
    Карточка отдаётся двумя запросами: во втором (layout_page_index=2)
    лежат описание и полные характеристики. Запрос проходит только из
    вкладки, где уже открыта карточка этого товара, иначе Ozon отвечает 403;
  * JSON, встроенный в HTML самой карточки: состояния виджетов в атрибутах
    data-state и блок JSON-LD (см. extract.py). Там есть всё, кроме описания
    и полных характеристик.

Режим (ParseOptions.price_source) решает, что основное, а что запасное:
  api  - сначала API, затем HTML (старый сценарий parse_ozon.py: 12 полей
         задания в каждой записи);
  html - сначала HTML: страница всё равно открывается, а запросы к API и
         ожидание полной загрузки уже не нужны (конвейер). Вторая часть
         карточки запрашивается, только когда она нужна этому SKU.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import dataclass

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from price_panel.core.collect import Breaker
from price_panel.core.models import SkuOutcome
from price_panel.infra import browser as browser_utils
from price_panel.infra import config
from price_panel.infra.logger import get_logger
from price_panel.marketplaces.ozon import constants, session
from price_panel.marketplaces.ozon.extract import (
    embedded_page_json,
    parse_html,
    parse_product,
    product_from_embedded,
)

log = get_logger("parse_ozon")

API_BASE = "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2?url="

# JS, который дёргает внутренний API прямо со страницы ozon.ru.
FETCH_JSON_JS = """
async (url) => {
    const response = await fetch(url, {
        credentials: 'include',
        headers: {'x-o3-app-name': 'dweb_client', 'accept': 'application/json'},
    });
    return {status: response.status, body: await response.text()};
}
"""

# Фрагменты сообщений Playwright, означающие, что браузер или связь с ним
# потеряны. Повторять запрос в той же вкладке после этого бессмысленно.
BROWSER_GONE_MARKERS = (
    "has been closed",
    "connection closed",
    "browser closed",
    "target crashed",
)


class ProductNotFound(RuntimeError):
    """Карточка недоступна: удалена, скрыта или SKU не существует."""


class FetchError(RuntimeError):
    """Не удалось получить данные страницы (сеть, антибот, таймаут)."""


class ChallengeFailed(FetchError):
    """Антибот-проверка на странице товара не прошла за CHALLENGE_TIMEOUT.

    Обычная проверка проходит сама за 5-10 с. Если она висит дольше, это
    блокировка (01.10.2026: с этого момента не прошла ни одна страница), и
    повторять тот же SKU бессмысленно - каждая попытка стоит ещё 30 с.
    """


class BrowserGone(RuntimeError):
    """Браузер закрылся или упал - нужен перезапуск."""


@dataclass(frozen=True)
class ParseOptions:
    """Как разбирать карточку: одинаково для всех SKU прогона."""

    # constants.PRICE_SOURCE_API или PRICE_SOURCE_HTML (см. шапку модуля).
    price_source: str = constants.PRICE_SOURCE_API
    # Каким SKU нужна вторая часть карточки: описание и полные
    # характеристики (art_set, has_rich_content). None - всем, как в старом
    # сценарии. Конвейер спрашивает её по расписанию (pipeline.details_schedule):
    # это лишний запрос к Ozon на каждый товар, а меняется она редко.
    details_for: frozenset | None = None

    def wants_details(self, sku: str) -> bool:
        return self.details_for is None or sku in self.details_for


# Когда открывалась последняя страница Ozon (time.monotonic) - для PAGE_INTERVAL.
_last_page_open: float | None = None


def wait_page_slot() -> None:
    """Ждёт, пока с открытия прошлой страницы пройдёт PAGE_INTERVAL.

    Темп задаёт частота страниц, а не скорость разбора: 07.10.2026 разбор из
    HTML при той же паузе 3 с ускорил обход до ~14 карточек в минуту, и пришла
    капча. Ожидание стоит перед каждым page.goto, поэтому предел держится и
    при повторах SKU, и при прогреве после перезапуска браузера.
    """
    global _last_page_open
    if _last_page_open is not None and config.PAGE_INTERVAL > 0:
        wait = config.PAGE_INTERVAL - (time.monotonic() - _last_page_open)
        if wait > 0:
            time.sleep(wait)
    _last_page_open = time.monotonic()


def api_url(sku: str, page_index: int = 1) -> str:
    """Адрес внутреннего API для нужной части карточки.

    Первая часть - заголовок, цена, рейтинг и галерея; вторая
    (layout_page_index=2) - описание и полные характеристики.
    """
    path = "/product/{}/".format(sku)
    if page_index > 1:
        path += "?layout_container=pdpPage2column&layout_page_index={}".format(page_index)
    return API_BASE + urllib.parse.quote(path, safe="")


def browser_is_gone(page: Page, exc: BaseException) -> bool:
    """True, если ошибка означает потерю вкладки, браузера или драйвера."""
    if any(marker in str(exc).lower() for marker in BROWSER_GONE_MARKERS):
        return True
    try:
        browser = page.context.browser
        return page.is_closed() or (browser is not None and not browser.is_connected())
    except Exception:  # noqa: BLE001 - мёртвый драйвер бросает что угодно
        return True


# -------------------------------------------------------------- получение ---
def fetch_page_json(page: Page, sku: str, page_index: int = 1) -> dict:
    """Забирает JSON одной из частей карточки через внутренний API Ozon.

    Вызывается из вкладки, где уже открыта карточка этого товара: с любой
    другой страницы Ozon отвечает 403.
    """
    url = api_url(sku, page_index)

    # Страница Ozon продолжает дозагружаться и после domcontentloaded: если в этот
    # момент она уходит на новый адрес, JS-контекст уничтожается прямо во время
    # вызова. Это лечится одной повторной попыткой.
    result = None
    for attempt in (1, 2):
        try:
            result = page.evaluate(FETCH_JSON_JS, url)
            break
        except PlaywrightError as exc:
            if attempt == 1 and "context was destroyed" in str(exc).lower():
                log.debug("SKU %s: контекст страницы сменился, повторяю запрос", sku)
                page.wait_for_timeout(2_000)
                continue
            raise FetchError("SKU {}: запрос к API не выполнился: {}".format(sku, exc)) from exc
    if result is None:
        raise FetchError("SKU {}: ответ API не получен".format(sku))

    status = result.get("status")
    body = result.get("body") or ""

    if status == 404:
        raise ProductNotFound("SKU {}: карточка не найдена (HTTP 404)".format(sku))
    if status != 200:
        raise FetchError("SKU {}: API ответил HTTP {}".format(sku, status))

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        # Вместо JSON почти всегда приходит HTML антибот-заглушки.
        lowered = body.lower()
        if any(marker in lowered for marker in constants.CHALLENGE_BODY_MARKERS):
            raise FetchError("SKU {}: Ozon показал антибот-проверку".format(sku)) from exc
        raise FetchError("SKU {}: ответ API не разобрался как JSON".format(sku)) from exc

    if not data.get("widgetStates"):
        raise FetchError("SKU {}: в ответе API нет данных карточки".format(sku))
    return data


def open_product_page(page: Page, sku: str, settle: bool = True) -> str:
    """Открывает карточку товара и возвращает её HTML.

    :param settle: дождаться, пока страница устоится (settle_page), - это
        нужно перед запросами к API из вкладки. Для разбора одного HTML ждать
        незачем: сервер отдаёт первую часть карточки уже отрисованной.
    """
    url = config.PRODUCT_URL_TEMPLATE.format(sku=sku)
    wait_page_slot()
    log.info("SKU %s: открываю %s", sku, url)
    response = page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT)
    if response is not None and response.status == 404:
        raise ProductNotFound("SKU {}: страница вернула HTTP 404".format(sku))
    if not browser_utils.pass_challenge(page, response):
        raise ChallengeFailed("SKU {}: антибот-проверка не прошла".format(sku))
    if settle:
        settle_page(page, sku)

    # HTML, отданный сервером, - если после антибот-проверки страница
    # перезагрузилась, актуален уже отрисованный документ.
    try:
        if response is not None and response.status == 200:
            return response.text()
    except PlaywrightError:
        pass
    return page.content()


def settle_page(page: Page, sku: str) -> None:
    """Даёт странице устояться перед запросами к API из неё.

    Сразу после domcontentloaded Ozon нередко делает ещё один переход, и
    запрос к API падает на уничтоженном контексте.
    """
    try:
        page.wait_for_load_state("load", timeout=config.LOAD_STATE_TIMEOUT)
    except PlaywrightTimeout:
        log.debug("SKU %s: страница не догрузилась полностью, продолжаю", sku)
    page.wait_for_timeout(config.PAGE_SETTLE_MS)


def fetch_details(page: Page, sku: str) -> dict | None:
    """Состояния виджетов второй части карточки: описание и полные характеристики.

    None - не получены: карточка разберётся и без них, но art_set и
    has_rich_content останутся неизвестными.
    """
    try:
        extra = fetch_page_json(page, sku, page_index=2)
    except (FetchError, ProductNotFound) as exc:
        log.warning("SKU %s: описание и полные характеристики не получены (%s)", sku, exc)
        return None
    return extra.get("widgetStates") or {}


def html_is_enough(product: dict) -> bool:
    """В HTML нашлась карточка: название и цена (или явное «нет в наличии»)."""
    return bool(product.get("title")) and (
        bool(product.get("price")) or product.get("is_available") is False
    )


def parse_sku(page: Page, sku: str, options: ParseOptions | None = None) -> dict:
    """Собирает данные по одному SKU (порядок источников - см. шапку модуля)."""
    options = options or ParseOptions()
    details = options.wants_details(sku)
    if options.price_source != constants.PRICE_SOURCE_HTML:
        # Карточку открываем всегда: без неё внутренний API отдаёт 403.
        return parse_from_api(page, sku, open_product_page(page, sku), details)

    html = open_product_page(page, sku, settle=False)
    embedded = embedded_page_json(html)
    product = product_from_embedded(embedded, sku)
    if not html_is_enough(product):
        log.warning("SKU %s: в HTML карточки нет названия или цены, беру данные из API", sku)
        settle_page(page, sku)
        return parse_from_api(page, sku, html, details)
    if not details:
        return product
    settle_page(page, sku)
    extra = fetch_details(page, sku)
    return product if extra is None else product_from_embedded(embedded, sku, extra)


def parse_from_api(page: Page, sku: str, html: str, details: bool) -> dict:
    """Данные из внутреннего API, а если он не помог - из JSON в HTML карточки."""
    try:
        data = fetch_page_json(page, sku)
        product = parse_product(data, sku, fetch_details(page, sku) if details else None)
        # Пустой заголовок обычно значит, что вернулась заглушка, а не карточка.
        if product.get("title"):
            return product
        log.warning("SKU %s: в ответе API нет названия товара", sku)
    except FetchError as exc:
        log.warning("SKU %s: %s", sku, exc)

    log.warning("SKU %s: беру данные из JSON, встроенного в HTML карточки", sku)
    product = parse_html(html, sku)
    if not product.get("title"):
        raise FetchError("SKU {}: данные карточки не найдены ни в API, ни в HTML".format(sku))
    return product


def error_type_of(exc: BaseException) -> str:
    """Короткий код ошибки для parse_errors."""
    if isinstance(exc, ProductNotFound):
        return "not_found"
    if isinstance(exc, ChallengeFailed):
        return "antibot"
    if isinstance(exc, PlaywrightTimeout):
        return "timeout"
    if isinstance(exc, FetchError):
        return "fetch_error"
    if isinstance(exc, PlaywrightError):
        return "browser_error"
    return type(exc).__name__


def parse_sku_outcome(
    page: Page, sku: str, retries: int, options: ParseOptions | None = None
) -> SkuOutcome:
    """Разбирает SKU с повторами при временных ошибках и сообщает, чем кончилось.

    :raises BrowserGone: браузер упал - повторять в этой вкладке бессмысленно.
    """
    outcome = SkuOutcome(sku=sku)
    for attempt in range(1, retries + 2):
        outcome.attempts = attempt
        try:
            product = parse_sku(page, sku, options)
            log.info(
                "SKU %s: готово (%s) | %s | цена=%s | рейтинг=%s | отзывов=%s | фото=%s | видео=%s",
                sku,
                product.get("source"),
                (product.get("title") or "")[:60],
                product.get("price"),
                product.get("rating"),
                product.get("reviews_total"),
                product.get("photos_seller"),
                product.get("videos_seller"),
            )
            outcome.product = product
            return outcome
        except ProductNotFound as exc:
            # Повторять бессмысленно - товара просто нет.
            log.error("%s", exc)
            outcome.error_type, outcome.error_message = error_type_of(exc), str(exc)
            return outcome
        except ChallengeFailed as exc:
            # Повторять бессмысленно - это блокировка, а не сбой. Серию таких
            # отказов ловит предохранитель (MAX_CONSECUTIVE_CHALLENGES).
            log.error("%s - без повторов", exc)
            outcome.error_type, outcome.error_message = error_type_of(exc), str(exc)
            return outcome
        except (FetchError, PlaywrightError) as exc:
            if isinstance(exc, PlaywrightError) and browser_is_gone(page, exc):
                raise BrowserGone(str(exc)) from exc
            outcome.error_type, outcome.error_message = error_type_of(exc), str(exc)
            log.warning("SKU %s: попытка %s из %s не удалась: %s", sku, attempt, retries + 1, exc)
            if attempt <= retries:
                pause = config.REQUEST_DELAY * attempt
                log.info("SKU %s: повтор через %.1f с", sku, pause)
                time.sleep(pause)

    log.error("SKU %s: все попытки исчерпаны, пропускаю", sku)
    return outcome


def parse_sku_with_retries(page: Page, sku: str, retries: int) -> dict | None:
    """Оборачивает parse_sku повторными попытками при временных ошибках.

    :raises BrowserGone: браузер упал - повторять в этой вкладке бессмысленно.
    """
    return parse_sku_outcome(page, sku, retries).product


# --------------------------------------------------------------- адаптер ---
def browser_session(pending: list, state: dict | None, total: int, options: ParseOptions):
    """Одна сессия браузера: итоги SKU из pending по одному, пока не кончатся.

    SKU снимается с очереди только после разбора: если браузер упадёт на нём,
    в новой сессии он будет разобран заново. Пауза REQUEST_DELAY - после
    каждого товара, кроме последнего; частоту страниц держит wait_page_slot.

    :param state: сохранённая сессия (cookies.json); None - без неё.
    """
    with sync_playwright() as playwright:
        browser = browser_utils.launch(playwright)
        context = browser_utils.new_context(browser, storage_state=state)
        try:
            page = context.new_page()
            # Прогрев: главная выдаёт антибот-cookies, без них карточки
            # открываются через проверку.
            log.info("Прогреваю сессию на ozon.ru")
            wait_page_slot()
            response = page.goto(
                "https://www.ozon.ru/", wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT
            )
            browser_utils.pass_challenge(page, response)
            page.wait_for_timeout(2_000)

            while pending:
                sku = pending[0]
                log.info("--- [%s/%s] SKU %s ---", total - len(pending) + 1, total, sku)
                started = time.monotonic()
                outcome = parse_sku_outcome(page, sku, config.MAX_RETRIES, options)
                outcome.seconds = time.monotonic() - started
                pending.pop(0)
                yield outcome
                if pending:
                    time.sleep(config.REQUEST_DELAY)
        finally:
            browser_utils.close_quietly(context, browser)


class OzonAdapter:
    """Сбор карточек Ozon для ядра (core.collect): браузер, перезапуски, темп."""

    def __init__(self, options: ParseOptions | None = None):
        self.options = options or ParseOptions()

    def collect(self, skus: list):
        """Итоги SKU по порядку; при сбое браузера - новая сессия, не больше
        MAX_BROWSER_RESTARTS раз. Возвращает причину, если остановился раньше."""
        try:
            state = session.load_session(config.COOKIES_FILE)
        except session.SessionError as exc:
            log.error("%s", exc)
            return "файл сессии Ozon не открылся: {}".format(exc)

        pending = list(skus)
        restarts = 0
        while pending:
            # Что бы ни случилось с браузером - упал процесс, закрыли окно,
            # оборвалась связь с драйвером, - оставшиеся SKU получают ещё один
            # шанс в новом браузере.
            try:
                yield from browser_session(pending, state, len(skus), self.options)
            except BrowserGone as exc:
                log.error("Браузер упал: %s", exc)
            except Exception:  # noqa: BLE001 - теряем браузер, но не очередь
                # Со стек-трейсом: сюда попадают и программные ошибки.
                log.exception("Сессия браузера завершилась аварийно")
            if not pending:
                return None
            restarts += 1
            if restarts > config.MAX_BROWSER_RESTARTS:
                log.error(
                    "Браузер падал %s раз(а) - прекращаю, необработанных SKU: %s",
                    restarts,
                    len(pending),
                )
                return "браузер падал, до SKU не дошла очередь"
            log.warning(
                "Перезапускаю браузер (%s из %s), осталось SKU: %s",
                restarts,
                config.MAX_BROWSER_RESTARTS,
                len(pending),
            )
        return None


def breaker() -> Breaker:
    """Предохранитель с порогами из .env (MAX_CONSECUTIVE_FAILURES / _CHALLENGES)."""
    return Breaker(config.MAX_CONSECUTIVE_FAILURES, config.MAX_CONSECUTIVE_CHALLENGES)

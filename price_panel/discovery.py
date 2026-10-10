"""Discovery: поиск SKU в категориях и формирование sku_panel.

Отдельный этап, не связанный с парсером карточек: он только находит товары и
записывает их в sku_panel (и в CSV со столбцом sku). Парсер по-прежнему
получает обычный список SKU - из panel или из файла.

Источники кандидатов (выбираются у каждой категории в config.yaml):

  ozon_listing - листинг категории на ozon.ru через Playwright. Страница
      категории открывается в браузере (антибот-проверка, cookies), затем
      страницы выдачи запрашиваются у того же внутреннего API, которым
      пользуется фронтенд:  /api/entrypoint-api.bx/page/json/v2?url=/category/...?page=N
      Проверено 29.09.2026: на страницу приходит 8 товаров (виджет
      tileGridDesktop), соседние страницы идут подряд, выдача обрывается на
      646-684-й странице. SKU берётся из ссылки на карточку
      (/product/<slug>-<sku>/), а не из вёрстки.

  data_ozon - отчёт «Аналитика товаров» на data.ozon.ru:
      POST /p-api/exa-auth/analytics_what_to_sell/what_to_sell
      Нужна действующая сессия data.ozon.ru. Отдаёт не больше 100 строк за
      запрос и только окно из первых 1000 строк по выбранной метрике
      (offset >= 1000 -> "offset limit exceeded"), т.е. это выборка из
      топ-1000 категории по продажам, а не из всей категории.

Нагрузка на Ozon минимальна: запрашиваются только страницы, нужные для
выборки (первые страницы для top и случайные глубокие для хвоста), запросы
строго последовательные, между ними - пауза discovery.request_delay (5 с со
случайным разбросом), между категориями - discovery.category_pause.

Темп выбран по живому опыту: при запросе раз в 2 с Ozon после ~110 запросов
начал отвечать HTTP 403 на всё (прогон в Docker 29.09.2026). Если Ozon всё же
ограничил запросы, discovery выжидает, а если ограничение не снимается -
сохраняет найденное и останавливается; повторный discover доберёт остальное.
"""

from __future__ import annotations

import json
import math
import random
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from . import browser as browser_utils
from . import config, constants, session
from .db import DatabaseError
from .logger import get_logger
from .parse import API_BASE, FETCH_JSON_JS
from .sampling import (
    GROUP_TAIL,
    GROUP_TOP,
    Candidate,
    dedupe,
    listing_position,
    make_rng,
    plan_tail_pages,
    select_panel,
    sku_from_url,
)
from .settings import SOURCE_DATA_OZON, CategoryConfig, Settings
from .warehouse import Warehouse

log = get_logger("discovery")

DATA_OZON_APP_URL = "https://data.ozon.ru/app/bestsellers?preset=all"
DATA_OZON_API_URL = "https://data.ozon.ru/p-api/exa-auth/analytics_what_to_sell/what_to_sell"
# Ограничения API, выясненные на живом сервисе 29.09.2026.
DATA_OZON_MAX_LIMIT = 100
DATA_OZON_WINDOW = 1000
# Товаров на странице листинга ozon.ru (29.09.2026). Нужно только для оценки,
# где кончается зона top, когда её страницы в этом запуске не запрашивались.
LISTING_PAGE_SIZE = 8
# Ответы, которыми Ozon ограничивает частоту запросов.
BLOCK_STATUSES = (403, 429)

FETCH_POST_JSON_JS = """
async ([url, body]) => {
    const response = await fetch(url, {
        method: 'POST',
        credentials: 'include',
        headers: {'content-type': 'application/json', 'accept': 'application/json',
                  'x-o3-app-name': 'exa-ui'},
        body: JSON.stringify(body),
    });
    return {status: response.status, body: await response.text()};
}
"""


class DiscoveryError(RuntimeError):
    """Категорию не удалось обойти (антибот, нет сессии, сломался ответ)."""


@dataclass
class CategoryResult:
    """Итог discovery по одной категории - для отчёта и discovery_runs."""

    category: CategoryConfig
    # success | skipped (panel уже собрана) | blocked (Ozon ограничил запросы,
    # найденное сохранено) | stopped (не запускалась после blocked) | failed
    status: str = "success"
    pages_requested: int = 0
    discovered: int = 0
    selected_top: int = 0
    selected_tail: int = 0
    already_in_panel: int = 0
    listing_depth: int | None = None
    note: str = ""
    seen: list = field(default_factory=list)

    @property
    def selected(self) -> int:
        return self.selected_top + self.selected_tail


# ------------------------------------------------------ разбор ответов -----
def listing_api_url(category_path: str, page: int) -> str:
    path = category_path + ("?page={}".format(page) if page > 1 else "")
    return API_BASE + urllib.parse.quote(path, safe="")


def parse_listing(data: dict, page: int) -> list:
    """Кандидаты из ответа API листинга: SKU из ссылок в плитках выдачи."""
    items = []
    for key, raw in (data.get("widgetStates") or {}).items():
        if not key.startswith("tileGrid"):
            continue
        try:
            state = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        items.extend((state or {}).get("items") or [])

    candidates = []
    for index, item in enumerate(items):
        link = ((item or {}).get("action") or {}).get("link") or ""
        sku = sku_from_url(link)
        if sku is None:
            continue
        candidates.append(
            Candidate(
                sku=sku,
                position=listing_position(page, index, len(items)),
                page=page,
                url=urllib.parse.urljoin("https://www.ozon.ru", link.split("?")[0]),
            )
        )
    return candidates


def parse_data_ozon(data: dict, offset: int) -> list:
    """Кандидаты из ответа what_to_sell: SKU из ссылки на карточку."""
    candidates = []
    for index, item in enumerate(data.get("items") or []):
        link = str(item.get("link") or "")
        sku = sku_from_url(link)
        field_sku = str(item.get("sku") or "") or None
        if sku is None:
            sku = field_sku
        elif field_sku and field_sku != sku:
            log.warning(
                "data.ozon.ru: sku=%s расходится со ссылкой %s - беру из ссылки", field_sku, link
            )
        if sku is None:
            continue
        position = offset + index + 1
        candidates.append(
            Candidate(sku=sku, position=position, page=offset // DATA_OZON_MAX_LIMIT + 1, url=link)
        )
    return candidates


def is_challenge_body(body: str) -> bool:
    lowered = (body or "")[:5000].lower()
    return any(marker in lowered for marker in constants.CHALLENGE_BODY_MARKERS)


# ----------------------------------------------------------- источники -----
class ListingSource:
    """Листинг категории ozon.ru через внутренний API фронтенда.

    Два вида неудач обрабатываются по-разному:
      * отказ Ozon (HTTP 403 / 429 или антибот-заглушка вместо JSON). Короткие
        повторы его только продлевают, поэтому ждём по списку block_backoff
        (по умолчанию 10 с, 1, 3, 5 мин) и повторяем в НОВОЙ сессии браузера
        (new_page создаёт чистый контекст): в прогонах 29.09.2026 отказ
        прилипал именно к сессии - в ней не помогало даже 10 минут ожидания,
        а свежая сессия собирала категорию целиком. Если отказ не снялся -
        blocked = True, и источник больше ничего не запрашивает;
      * прочие сбои (обрыв, пустой ответ) - несколько коротких повторов в той
        же сессии, как у парсера карточек (MAX_RETRIES).

    :param new_page: создаёт вкладку в новом чистом контексте браузера.
    """

    def __init__(
        self,
        new_page: Callable[[], Page],
        delay: float,
        jitter: float = 0.0,
        block_backoff: tuple = (),
    ):
        self.new_page = new_page
        self.page = new_page()
        self.delay = delay
        self.jitter = jitter
        self.block_backoff = tuple(block_backoff)
        self.requests = 0
        self.blocked = False
        self._last_done = 0.0

    def _pause(self) -> None:
        """Пауза от конца предыдущего запроса, со случайным разбросом."""
        target = self.delay * random.uniform(1 - self.jitter, 1 + self.jitter)
        wait = target - (time.monotonic() - self._last_done)
        if wait > 0:
            time.sleep(wait)

    def open(self, category: CategoryConfig) -> None:
        """Открывает страницу категории: без неё API отвечает заглушкой."""
        self._pause()
        try:
            response = self.page.goto(
                category.url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT
            )
            if not browser_utils.pass_challenge(self.page, response):
                raise DiscoveryError("{}: антибот-проверка не прошла".format(category.name))
            self.page.wait_for_timeout(config.PAGE_SETTLE_MS)
        finally:
            self._last_done = time.monotonic()

    def _reopen(self, category: CategoryConfig) -> None:
        try:
            self.open(category)
        except (DiscoveryError, PlaywrightError) as exc:
            log.warning(
                "%s: не удалось заново открыть категорию: %s",
                category.name,
                str(exc).splitlines()[0],
            )

    def _request(self, category: CategoryConfig, page_number: int) -> tuple:
        """Один запрос страницы: (товары или None, причина неудачи, отказ Ozon?)."""
        self._pause()
        self.requests += 1
        try:
            result = self.page.evaluate(
                FETCH_JSON_JS, listing_api_url(category.listing_path, page_number)
            )
        except PlaywrightError as exc:
            return None, str(exc).splitlines()[0], False
        finally:
            self._last_done = time.monotonic()

        status = (result or {}).get("status")
        body = (result or {}).get("body") or ""
        if status in BLOCK_STATUSES:
            return None, "HTTP {}".format(status), True
        if status != 200:
            return None, "HTTP {}".format(status), False
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict) and data.get("widgetStates"):
            return parse_listing(data, page_number), "", False
        if is_challenge_body(body):
            return None, "антибот-заглушка", True
        return None, "ответ без виджетов", False

    def fetch(self, category: CategoryConfig, page_number: int) -> list | None:
        """Товары одной страницы выдачи; [] - выдача кончилась; None - не получили."""
        if self.blocked:
            return None
        errors = blocks = 0
        while True:
            items, reason, blocked = self._request(category, page_number)
            if items is not None:
                return items
            if blocked:
                if blocks >= len(self.block_backoff):
                    self.blocked = True
                    log.error(
                        "%s: Ozon ограничил запросы (%s) и не снял ограничение - "
                        "discovery остановлен, найденное сохраняется",
                        category.name,
                        reason,
                    )
                    return None
                wait = self.block_backoff[blocks]
                blocks += 1
                log.warning(
                    "%s: страница %s: Ozon ограничил запросы (%s), жду %.0f с и "
                    "повторяю в новой сессии браузера (ожидание %s из %s)",
                    category.name,
                    page_number,
                    reason,
                    wait,
                    blocks,
                    len(self.block_backoff),
                )
                time.sleep(wait)
                self.page = self.new_page()
            else:
                errors += 1
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
            # Заново открытая категория: без неё API отвечает заглушкой, и так
            # же проходит антибот-проверка.
            self._reopen(category)


class DataOzonSource:
    """Отчёт «Аналитика товаров» data.ozon.ru (нужна сессия data.ozon.ru)."""

    def __init__(self, page: Page, delay: float):
        self.page = page
        self.delay = delay
        self.requests = 0
        self.opened = False

    def open(self) -> None:
        if self.opened:
            return
        response = self.page.goto(
            DATA_OZON_APP_URL, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT
        )
        if not browser_utils.pass_challenge(self.page, response):
            raise DiscoveryError("data.ozon.ru: антибот-проверка не прошла")
        self.page.wait_for_timeout(5_000)
        if "sso.ozon.ru" in self.page.url or "/login" in self.page.url:
            raise DiscoveryError(
                "data.ozon.ru просит войти заново - сессия в {} недействительна. "
                "Обновите её: python get_cookies.py --force".format(config.COOKIES_FILE.name)
            )
        self.opened = True

    def fetch(self, category: CategoryConfig, offset: int, limit: int) -> tuple:
        """(кандидаты, totals) для окна [offset, offset + limit)."""
        body = {
            "filter": {"categories": list(category.category_ids), "brandIds": []},
            "limit": str(limit),
            "offset": str(offset),
            "sort": {"attribute": category.sort_attribute, "order": "desc"},
        }
        time.sleep(self.delay)
        self.requests += 1
        result = self.page.evaluate(FETCH_POST_JSON_JS, [DATA_OZON_API_URL, body])
        status, text = result.get("status"), result.get("body") or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {}
        if status in (401, 403):
            raise DiscoveryError(
                "data.ozon.ru ответил HTTP {} - нужна сессия: python get_cookies.py --force".format(
                    status
                )
            )
        if status != 200:
            raise DiscoveryError(
                "data.ozon.ru ответил HTTP {}: {}".format(status, data.get("message") or text[:200])
            )
        return parse_data_ozon(data, offset), int(data.get("totals") or 0)


# ---------------------------------------------------------- обход категорий --
@dataclass
class ListingCrawl:
    """Что удалось собрать в листинге категории."""

    top: list = field(default_factory=list)
    tail: list = field(default_factory=list)
    # Последняя непустая страница, если конец выдачи встретился.
    depth: int | None = None
    # Ozon ограничил запросы и не снял ограничение: собранное - частичное.
    blocked: bool = False
    skipped_pages: int = 0


def discover_listing(
    source: ListingSource,
    category: CategoryConfig,
    top_needed: int,
    tail_needed: int,
    exclude: set,
    rng,
) -> ListingCrawl:
    """Собирает кандидатов top и хвоста.

    Не падает на середине: если Ozon ограничил запросы, возвращает то, что
    уже нашлось, с blocked=True. Найденное сохраняется в panel, а повторный
    discover доберёт недостающее - прогресс не теряется.
    """
    crawl = ListingCrawl()
    source.open(category)

    # Top: страницы подряд с первой, пока не наберётся нужное число новых SKU.
    page_number = 0
    last_page: int | None = None
    max_top_pages = math.ceil(top_needed / 4) + 5
    while top_needed > 0 and page_number < max_top_pages:
        eligible = [c for c in dedupe(crawl.top) if c.sku not in exclude]
        if len(eligible) >= top_needed:
            break
        page_number += 1
        items = source.fetch(category, page_number)
        if items is None:
            # Top должен идти подряд: с дырой в выдаче дальше не идём.
            # Недобор top доберёт следующий discover.
            crawl.skipped_pages += 1
            break
        if not items:
            last_page = page_number - 1
            break
        log.info(
            "SKU DISCOVERED category=%s page=%s group=top found=%s",
            category.name,
            page_number,
            len(items),
        )
        crawl.top.extend(items)

    # Хвост: случайные страницы глубже top. Пустая страница значит, что выдача
    # кончилась раньше tail_max_page, - диапазон сужается и страница заменяется.
    # Хвост начинается за зоной top всей категории - даже если в этот раз top
    # не дозапрашивался (донабор одного хвоста).
    first = max(page_number + 1, math.ceil(category.top_size / LISTING_PAGE_SIZE) + 1, 2)
    last = min(category.tail_max_page, last_page) if last_page else category.tail_max_page
    visited: set = set()
    per_page = category.tail_items_per_page
    budget = math.ceil(tail_needed / per_page) * 3 + 10

    def tail_capacity() -> int:
        by_page: dict = {}
        top_skus = {c.sku for c in crawl.top}
        for c in dedupe(crawl.tail):
            if c.sku not in exclude and c.sku not in top_skus:
                by_page[c.page] = by_page.get(c.page, 0) + 1
        return sum(min(per_page, n) for n in by_page.values())

    while tail_needed > 0 and budget > 0 and first <= last and not source.blocked:
        missing = tail_needed - tail_capacity()
        if missing <= 0:
            break
        plan = plan_tail_pages(first, last, math.ceil(missing / per_page), rng, visited)
        if not plan:
            break
        for number in plan:
            if number > last or budget <= 0 or source.blocked:
                continue
            budget -= 1
            visited.add(number)
            items = source.fetch(category, number)
            if items is None:
                crawl.skipped_pages += 1
                continue
            if not items:
                last = number - 1
                log.info("%s: выдача кончается раньше страницы %s", category.name, number)
                continue
            log.info(
                "SKU DISCOVERED category=%s page=%s group=tail found=%s",
                category.name,
                number,
                len(items),
            )
            crawl.tail.extend(items)

    crawl.blocked = source.blocked
    crawl.depth = last if last_page or last < category.tail_max_page else None
    return crawl


def discover_data_ozon(source: DataOzonSource, category: CategoryConfig) -> tuple:
    """Всё окно отчёта (до 1000 строк). Возвращает (top, tail, totals из ответа)."""
    source.open()
    candidates: list = []
    offset, totals = 0, None
    while offset < DATA_OZON_WINDOW:
        items, reported = source.fetch(category, offset, DATA_OZON_MAX_LIMIT)
        totals = reported if totals is None else totals
        log.info(
            "SKU DISCOVERED category=%s offset=%s found=%s totals=%s",
            category.name,
            offset,
            len(items),
            reported,
        )
        candidates.extend(items)
        offset += DATA_OZON_MAX_LIMIT
        if not items or offset >= min(totals or 0, DATA_OZON_WINDOW):
            break
    # Top и хвост берутся из одного окна; select_panel сам не допустит
    # пересечения, а хвост - это всё, что ниже отобранного top.
    return candidates, candidates, totals


# ---------------------------------------------------------------- этап -----
def _needed(wh: Warehouse, category: CategoryConfig, rebuild: bool) -> tuple:
    if rebuild:
        return category.top_size, category.tail_size
    counts = wh.panel_counts(category.name)
    return (
        max(category.top_size - counts.get(GROUP_TOP, 0), 0),
        max(category.tail_size - counts.get(GROUP_TAIL, 0), 0),
    )


class BrowserSessions:
    """Чистые сессии браузера (контексты) для discovery.

    Каждая категория обходится в своей сессии, и при отказе Ozon повтор
    тоже идёт в новой. Основание - прогоны 29.09.2026 в Docker: дважды первая
    категория в свежем браузере собиралась целиком (110 и 81 запрос), а
    следующая в той же сессии сразу получала HTTP 403, и 10 минут ожидания
    в этой сессии не помогали. Cookies каждого нового контекста - из
    cookies.json, как и раньше.
    """

    def __init__(self, browser, storage_state: dict | None):
        self.browser = browser
        self.storage_state = storage_state
        self.context = None

    def new_page(self) -> Page:
        """Закрывает текущую сессию и открывает вкладку в новой."""
        self.close()
        self.context = browser_utils.new_context(self.browser, storage_state=self.storage_state)
        return self.context.new_page()

    def close(self) -> None:
        if self.context is not None:
            browser_utils.close_quietly(self.context)
            self.context = None


def discover_category(
    wh: Warehouse,
    settings: Settings,
    category: CategoryConfig,
    sessions: BrowserSessions,
    rebuild: bool = False,
) -> CategoryResult:
    """Обходит одну категорию в новой сессии браузера и дописывает её panel."""
    result = CategoryResult(category=category)
    top_needed, tail_needed = _needed(wh, category, rebuild)
    log.info(
        "CATEGORY START category=%s source=%s panel_size=%s need_top=%s need_tail=%s",
        category.name,
        category.source,
        category.panel_size,
        top_needed,
        tail_needed,
    )

    if top_needed == 0 and tail_needed == 0:
        result.status = "skipped"
        result.note = "panel уже собрана"
        log.info(
            "%s: panel уже собрана (%s SKU) - обход не нужен", category.name, category.panel_size
        )
        return result

    # Уже активные SKU (в любой категории) повторно не берём. При пересборке
    # старая panel этой категории не в счёт: она будет заменена.
    exclude = wh.active_panel_skus()
    if rebuild:
        exclude -= set(wh.panel_skus([category.name]))

    run_id = wh.start_discovery_run(category.name, category.source, settings.discovery.seed)
    rng = make_rng(settings.discovery.seed, category.name)
    try:
        notes = []
        if category.source == SOURCE_DATA_OZON:
            source = DataOzonSource(sessions.new_page(), settings.discovery.request_delay)
            top, tail, totals = discover_data_ozon(source, category)
            notes.append("окно отчёта data.ozon.ru: {} строк".format(totals))
            # Окно целиком уже на руках - хвост равномерно по всему окну.
            per_page = max(tail_needed, 1)
            # Ozon обновляет токены сессии на лету; если не сохранить их, файл
            # сессии устаревает раньше своего срока (так data.ozon.ru и выкинул
            # на вход во время разведки).
            if sessions.context is not None and session.has_auth_cookies(
                sessions.context.cookies()
            ):
                session.save_session(sessions.context, config.COOKIES_FILE)
        else:
            discovery_settings = settings.discovery
            source = ListingSource(
                sessions.new_page,
                discovery_settings.request_delay,
                discovery_settings.request_jitter,
                discovery_settings.block_backoff,
            )
            crawl = discover_listing(source, category, top_needed, tail_needed, exclude, rng)
            top, tail, result.listing_depth = crawl.top, crawl.tail, crawl.depth
            per_page = category.tail_items_per_page
            if crawl.skipped_pages:
                notes.append("страниц не получено: {}".format(crawl.skipped_pages))
            if crawl.blocked:
                result.status = "blocked"
        result.pages_requested = source.requests

        picks = select_panel(top, tail, top_needed, tail_needed, rng, per_page, exclude)
        seen = dedupe(top + tail)
        result.discovered = len(seen)
        result.seen = [c.sku for c in seen]
        result.already_in_panel = sum(1 for c in seen if c.sku in exclude)
        result.selected_top = sum(1 for p in picks if p.group == GROUP_TOP)
        result.selected_tail = sum(1 for p in picks if p.group == GROUP_TAIL)

        if rebuild:
            log.info("%s: выключаю прежнюю panel категории (--rebuild)", category.name)
            wh.deactivate_category(category.name)
        wh.add_to_panel(picks, category.name, category.source, run_id)
        wh.touch_seen(sku for sku in result.seen if sku in exclude)

        shortfall = (top_needed - result.selected_top) + (tail_needed - result.selected_tail)
        if result.status == "blocked":
            notes.insert(
                0,
                "Ozon ограничил запросы: сохранено {} SKU, недостающие {} доберёт "
                "повторный discover".format(result.selected, shortfall),
            )
        elif shortfall > 0:
            notes.append("не хватило кандидатов: {} SKU".format(shortfall))
            log.warning("%s: не хватило кандидатов: %s SKU", category.name, shortfall)
        result.note = "; ".join(notes)
        # В discovery_runs частичный обход - failed с объяснением в error_message.
        wh.finish_discovery_run(
            run_id,
            "failed" if result.status == "blocked" else "success",
            result.pages_requested,
            result.discovered,
            result.selected_top,
            result.selected_tail,
            result.note or None,
        )
    except (DiscoveryError, PlaywrightError) as exc:
        result.status = "failed"
        result.note = str(exc).splitlines()[0]
        log.error("%s: discovery не удался: %s", category.name, result.note)
        wh.finish_discovery_run(run_id, "failed", error_message=result.note)
    return result


def run_discovery(
    wh: Warehouse, settings: Settings, names: list | None = None, rebuild: bool = False
) -> list:
    """Этап discovery целиком. Возвращает CategoryResult по каждой категории."""
    categories = (
        [settings.category(name) for name in names]
        if names
        else list(settings.discovery.categories)
    )
    log.info(
        "DISCOVERY START categories=%s rebuild=%s seed=%s",
        ",".join(c.name for c in categories),
        rebuild,
        settings.discovery.seed,
    )

    state = None
    if config.COOKIES_FILE.exists():
        state = session.load_session(config.COOKIES_FILE)
    elif any(c.source == SOURCE_DATA_OZON for c in categories):
        raise DiscoveryError("Для data_ozon нужна сессия: python get_cookies.py")

    # Категории с уже собранной panel не требуют ни браузера, ни запросов:
    # повторный discover ничего не перетасовывает и не нагружает Ozon.
    results: dict = {}
    pending = []
    for category in categories:
        if not rebuild and _needed(wh, category, False) == (0, 0):
            log.info(
                "CATEGORY START category=%s: panel уже собрана (%s SKU) - пропускаю",
                category.name,
                category.panel_size,
            )
            results[category.name] = CategoryResult(
                category=category,
                status="skipped",
                note="panel уже собрана ({} SKU)".format(category.panel_size),
            )
        else:
            pending.append(category)

    if pending:
        with sync_playwright() as playwright:
            browser = browser_utils.launch(playwright)
            sessions = BrowserSessions(browser, state)
            try:
                stop_note = ""
                for index, category in enumerate(pending):
                    if stop_note:
                        # Ozon ограничил запросы даже в новой сессии: следующая
                        # категория только продлила бы блокировку.
                        results[category.name] = CategoryResult(
                            category=category, status="stopped", note=stop_note
                        )
                        continue
                    if index and settings.discovery.category_pause:
                        log.info(
                            "Пауза между категориями: %.0f с", settings.discovery.category_pause
                        )
                        time.sleep(settings.discovery.category_pause)
                    result = discover_category(wh, settings, category, sessions, rebuild)
                    results[category.name] = result
                    if result.status == "blocked":
                        stop_note = (
                            "не запускалась: Ozon ограничил запросы - повторите "
                            "discover через 30-60 минут"
                        )
            finally:
                sessions.close()
                browser_utils.close_quietly(browser)

    ordered = [results[c.name] for c in categories]
    selected = sum(r.selected for r in ordered)
    log.info("PANEL CREATED added=%s categories=%s", selected, len(ordered))
    return ordered


def export_panel(wh: Warehouse, settings: Settings) -> int | None:
    """Выгружает активную panel в CSV, если это задано в настройках."""
    path = settings.discovery.export_csv
    if path is None:
        return None
    try:
        count = wh.export_panel_csv(path)
    except (OSError, DatabaseError) as exc:
        log.error("Не удалось выгрузить panel в %s: %s", path, exc)
        return None
    log.info("Panel выгружена: %s SKU -> %s", count, path)
    return count

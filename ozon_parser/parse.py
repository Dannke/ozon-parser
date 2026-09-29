"""Парсинг карточек товаров ozon.ru по списку SKU.

Скрипт поднимает браузер с сохранённой сессией (см. get_cookies.py) и для
каждого SKU забирает данные карточки.

Почему браузер, а не requests. Ozon закрыт антибот-защитой: запрос из
requests.Session даже с действующими cookies получает HTTP 403 - и на API,
и на HTML-страницу. Защита проверяет, что запрос сделан настоящим браузером
(JS-проверка, отпечаток TLS), поэтому сессия поднимается в Playwright: cookies
из cookies.json загружаются в контекст браузера так же, как в
requests.Session загружался бы cookie jar.

Откуда берутся данные:
  1. Основной путь - внутренний JSON-эндпоинт, из которого фронтенд Ozon
     собирает страницу:
         /api/entrypoint-api.bx/page/json/v2?url=/product/<sku>/
     Карточка отдаётся двумя запросами: во втором (layout_page_index=2)
     лежат описание и полные характеристики. Запрос проходит только из
     вкладки, где уже открыта карточка этого товара, иначе Ozon отвечает 403.
  2. Запасной путь - JSON, встроенный в HTML самой карточки: состояния
     виджетов в атрибутах data-state и блок JSON-LD (см. extract.py). В нём
     нет описания и полных характеристик, поэтому art_set и has_rich_content
     остаются пустыми.

Запуск:
    python parse_ozon.py                               # SKU из config.DEFAULT_SKUS
    python parse_ozon.py 2359066702 2829800382         # SKU аргументами
    python parse_ozon.py --file skus.txt --storage csv # SKU из файла
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import browser as browser_utils
from . import cli, config, constants, session, storage
from .extract import parse_html, parse_product
from .logger import get_logger

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


class BrowserGone(RuntimeError):
    """Браузер закрылся или упал - нужен перезапуск."""


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


def open_product_page(page: Page, sku: str) -> str:
    """Открывает карточку товара и возвращает её HTML."""
    url = config.PRODUCT_URL_TEMPLATE.format(sku=sku)
    log.info("SKU %s: открываю %s", sku, url)
    response = page.goto(url, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT)
    if response is not None and response.status == 404:
        raise ProductNotFound("SKU {}: страница вернула HTTP 404".format(sku))
    if not browser_utils.pass_challenge(page, response):
        raise FetchError("SKU {}: антибот-проверка не прошла".format(sku))

    # Даём странице устояться: сразу после domcontentloaded Ozon нередко делает
    # ещё один переход, и запрос к API падает на уничтоженном контексте.
    try:
        page.wait_for_load_state("load", timeout=config.LOAD_STATE_TIMEOUT)
    except PlaywrightTimeout:
        log.debug("SKU %s: страница не догрузилась полностью, продолжаю", sku)
    page.wait_for_timeout(config.PAGE_SETTLE_MS)

    # HTML, отданный сервером, - если после антибот-проверки страница
    # перезагрузилась, актуален уже отрисованный документ.
    try:
        if response is not None and response.status == 200:
            return response.text()
    except PlaywrightError:
        pass
    return page.content()


def fetch_card_data(page: Page, sku: str) -> dict:
    """Забирает обе части карточки и склеивает состояния виджетов в один объект.

    Вторая часть необязательна: без неё карточка разберётся, но поля art_set и
    has_rich_content останутся пустыми - данных для них в первой части нет.
    """
    data = fetch_page_json(page, sku)

    try:
        extra = fetch_page_json(page, sku, page_index=2)
    except (FetchError, ProductNotFound) as exc:
        log.warning("SKU %s: описание и полные характеристики не получены (%s)", sku, exc)
        return data

    states = dict(data.get("widgetStates") or {})
    states.update(extra.get("widgetStates") or {})
    data["widgetStates"] = states
    return data


def parse_sku(page: Page, sku: str) -> dict:
    """Собирает данные по одному SKU: сначала через API, затем из HTML."""
    # Карточку открываем всегда: без неё внутренний API отдаёт 403.
    html = open_product_page(page, sku)

    try:
        product = parse_product(fetch_card_data(page, sku), sku)
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


@dataclass
class SkuOutcome:
    """Итог обработки одного SKU: запись о товаре либо причина неудачи."""

    sku: str
    product: Optional[dict] = None
    error_type: str = ""
    error_message: str = ""
    attempts: int = 0


def error_type_of(exc: BaseException) -> str:
    """Короткий код ошибки для parse_errors."""
    if isinstance(exc, ProductNotFound):
        return "not_found"
    if isinstance(exc, PlaywrightTimeout):
        return "timeout"
    if isinstance(exc, FetchError):
        return "fetch_error"
    if isinstance(exc, PlaywrightError):
        return "browser_error"
    return type(exc).__name__


def parse_sku_outcome(page: Page, sku: str, retries: int) -> SkuOutcome:
    """Разбирает SKU с повторами при временных ошибках и сообщает, чем кончилось.

    :raises BrowserGone: браузер упал - повторять в этой вкладке бессмысленно.
    """
    outcome = SkuOutcome(sku=sku)
    for attempt in range(1, retries + 2):
        outcome.attempts = attempt
        try:
            product = parse_sku(page, sku)
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
        except (FetchError, PlaywrightError) as exc:
            if isinstance(exc, PlaywrightError) and browser_is_gone(page, exc):
                raise BrowserGone(str(exc)) from exc
            outcome.error_type, outcome.error_message = error_type_of(exc), str(exc)
            log.warning("SKU %s: попытка %s из %s не удалась: %s",
                        sku, attempt, retries + 1, exc)
            if attempt <= retries:
                pause = config.REQUEST_DELAY * attempt
                log.info("SKU %s: повтор через %.1f с", sku, pause)
                time.sleep(pause)

    log.error("SKU %s: все попытки исчерпаны, пропускаю", sku)
    return outcome


def parse_sku_with_retries(page: Page, sku: str, retries: int) -> Optional[dict]:
    """Оборачивает parse_sku повторными попытками при временных ошибках.

    :raises BrowserGone: браузер упал - повторять в этой вкладке бессмысленно.
    """
    return parse_sku_outcome(page, sku, retries).product


# --------------------------------------------------------------------- run ---
class RunObserver:
    """Хуки на результат каждого SKU. Базовая версия ничего не делает.

    Старый сценарий (CSV / таблица ozon_products) обходится без них; конвейер
    с PostgreSQL (pipeline.py) через них пишет товар в базу сразу после
    разбора - до перехода к следующему SKU, а ошибку - в parse_errors.
    """

    def sku_done(self, sku: str, product: dict, seconds: float) -> None:
        """SKU разобран."""

    def sku_failed(self, sku: str, error_type: str, message: str, seconds: float,
                   attempts: int = 0) -> None:
        """SKU не разобран: товара нет, исчерпаны попытки или до него не дошли."""


@dataclass
class RunProgress:
    """Что собрано за прогон: переживает перезапуски браузера."""

    pending: list
    rows: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    saved: int = 0
    observer: RunObserver = field(default_factory=RunObserver)

    def notify(self, outcome: SkuOutcome, seconds: float) -> None:
        """Передаёт итог SKU наблюдателю. Его сбой не должен ронять прогон."""
        try:
            if outcome.product is not None:
                self.observer.sku_done(outcome.sku, outcome.product, seconds)
            else:
                self.observer.sku_failed(outcome.sku, outcome.error_type or "unknown",
                                         outcome.error_message, seconds, outcome.attempts)
        except Exception:  # noqa: BLE001 - ошибка записи не повод бросать очередь
            log.exception("SKU %s: не удалось записать результат", outcome.sku)


def save_results(rows: list, backend: str, output: Optional[Path],
                 snapshot_date: Optional[dt.date]) -> bool:
    """Сохраняет собранное. Возвращает False, если сохранить не удалось."""
    try:
        storage.save(rows, backend=backend, csv_path=output, snapshot_date=snapshot_date)
        return True
    except storage.StorageError as exc:
        log.error("Сохранение не удалось: %s", exc)
        return False


def parse_in_browser(progress: RunProgress, state: dict, total: int, flush_batch) -> None:
    """Обходит оставшиеся SKU в одном экземпляре браузера.

    SKU снимается с очереди только после обработки: если браузер упадёт на
    нём, после перезапуска он будет обработан заново.
    """
    with sync_playwright() as playwright:
        browser = browser_utils.launch(playwright)
        context = browser_utils.new_context(browser, storage_state=state)
        try:
            page = context.new_page()
            # Прогрев: главная выдаёт антибот-cookies, без них карточки
            # открываются через проверку.
            log.info("Прогреваю сессию на ozon.ru")
            response = page.goto("https://www.ozon.ru/", wait_until="domcontentloaded",
                                 timeout=config.PAGE_TIMEOUT)
            browser_utils.pass_challenge(page, response)
            page.wait_for_timeout(2_000)

            while progress.pending:
                sku = progress.pending[0]
                index = total - len(progress.pending) + 1
                log.info("--- [%s/%s] SKU %s ---", index, total, sku)
                started = time.monotonic()
                outcome = parse_sku_outcome(page, sku, config.MAX_RETRIES)
                progress.pending.pop(0)
                if outcome.product is None:
                    progress.failed.append(sku)
                else:
                    progress.rows.append(outcome.product)
                progress.notify(outcome, time.monotonic() - started)
                flush_batch()

                # Пауза между товарами, чтобы не долбить сайт очередью запросов.
                if progress.pending:
                    time.sleep(config.REQUEST_DELAY)
        finally:
            browser_utils.close_quietly(context, browser)


def run(skus, storage_backend: str = "", output: Optional[Path] = None,
        snapshot_date: Optional[dt.date] = None, batch_size: Optional[int] = None,
        min_success_rate: float = 0.0, observer: Optional[RunObserver] = None) -> int:
    """Парсит список SKU и сохраняет результат. Возвращает код возврата процесса.

    :param snapshot_date: дата среза для таблиц БД (по умолчанию сегодня).
    :param batch_size: сбрасывать собранное в хранилище каждые N товаров
        (0 - только в конце). Промежуточное сохранение безопасно: в БД идёт
        upsert по (sku, parsed_date), CSV переписывается целиком и атомарно.
    :param min_success_rate: минимальная доля успешных SKU от длины входа.
    :param observer: получает итог каждого SKU сразу после его обработки.
    """
    if not skus:
        log.error("Список SKU пуст")
        return 1

    try:
        state = session.load_session(config.COOKIES_FILE)
    except session.SessionError as exc:
        log.error("%s", exc)
        return 1

    batch_size = config.BATCH_SIZE if batch_size is None else batch_size
    progress = RunProgress(pending=list(skus), observer=observer or RunObserver())
    log.info("К обработке SKU: %s", len(skus))

    def flush_batch() -> None:
        if batch_size > 0 and len(progress.rows) - progress.saved >= batch_size:
            log.info("Промежуточное сохранение: собрано %s", len(progress.rows))
            if save_results(progress.rows, storage_backend, output, snapshot_date):
                progress.saved = len(progress.rows)

    # Что бы ни случилось с браузером - упал процесс, закрыли окно, оборвалась
    # связь с драйвером, - собранные строки обязаны дойти до сохранения, а
    # оставшиеся SKU - получить ещё один шанс в новом браузере.
    restarts = 0
    unprocessed = ("not_processed", "браузер падал, до SKU не дошла очередь")
    while progress.pending:
        try:
            parse_in_browser(progress, state, len(skus), flush_batch)
        except KeyboardInterrupt:
            log.warning("Прервано пользователем - сохраняю собранное")
            unprocessed = ("interrupted", "прогон прерван до обработки SKU")
            break
        except Exception as exc:  # noqa: BLE001 - теряем браузер, но не данные
            if isinstance(exc, BrowserGone):
                log.error("Браузер упал: %s", exc)
            else:
                # Со стек-трейсом: сюда попадают и программные ошибки.
                log.exception("Сессия браузера завершилась аварийно")
        if not progress.pending:
            break
        restarts += 1
        if restarts > config.MAX_BROWSER_RESTARTS:
            log.error("Браузер падал %s раз(а) - прекращаю, необработанных SKU: %s",
                      restarts, len(progress.pending))
            break
        log.warning("Перезапускаю браузер (%s из %s), осталось SKU: %s",
                    restarts, config.MAX_BROWSER_RESTARTS, len(progress.pending))

    # Не обработанные из-за сбоя SKU - тоже неудача.
    for sku in progress.pending:
        progress.notify(SkuOutcome(sku=sku, error_type=unprocessed[0],
                                   error_message=unprocessed[1]), 0.0)
    progress.failed.extend(progress.pending)

    # Финальное сохранение делается всегда, даже на пустом результате: иначе
    # на месте остался бы CSV прошлого запуска.
    if not save_results(progress.rows, storage_backend, output, snapshot_date):
        return 1

    rows, failed = progress.rows, progress.failed
    success_rate = len(rows) / len(skus)
    log.info("Итог: успешно %s из %s (%.0f%%), с ошибкой %s",
             len(rows), len(skus), success_rate * 100, len(failed))
    if failed:
        log.warning("Не удалось обработать SKU: %s", ", ".join(failed))

    if not rows:
        return 1
    if min_success_rate > 0 and success_rate < min_success_rate:
        log.error("Доля успеха %.0f%% ниже порога %.0f%% - считаю прогон неудачным",
                  success_rate * 100, min_success_rate * 100)
        return 1
    return 0


def read_skus_file(path: Path) -> list:
    """Читает список SKU из текстового файла (по одному в строке).

    Пустые строки и комментарии (#, в том числе с отступом) пропускаются,
    дубли убираются с сохранением порядка. Понимает и CSV с заголовком sku
    (так выгружает panel команда discover): берётся первый столбец, строка
    заголовка пропускается.
    """
    if not path.exists():
        raise FileNotFoundError("Файл со списком SKU не найден: {}".format(path))
    # utf-8-sig: CSV, сохранённый из Excel, начинается с BOM.
    lines = (line.split(",", 1)[0].strip()
             for line in path.read_text(encoding="utf-8-sig").splitlines())
    return list(dict.fromkeys(
        line for line in lines
        if line and not line.startswith("#") and line.lower() != "sku"
    ))


def select_range(skus: list, offset: int = 0, limit: Optional[int] = None) -> list:
    """Часть списка SKU: с какого начать и сколько взять.

    Позволяет разложить длинный список на несколько задач планировщика: при
    ~10-15 с на товар сорокаминутная задача успевает около двухсот SKU.
    """
    selected = skus[max(offset, 0):]
    if limit is not None and limit >= 0:
        selected = selected[:limit]
    if len(selected) != len(skus):
        log.info("Из списка (%s) взято SKU: %s (offset=%s, limit=%s)",
                 len(skus), len(selected), offset, limit)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Парсер карточек товаров ozon.ru")
    parser.add_argument("skus", nargs="*", help="список SKU через пробел")
    parser.add_argument("--file", type=Path, help="файл со списком SKU (по одному в строке)")
    cli.add_storage_arguments(parser)
    parser.add_argument("--output", type=Path, help="путь к CSV-файлу результата")
    parser.add_argument("--offset", type=int, default=0,
                        help="пропустить первые N SKU списка")
    parser.add_argument("--limit", type=int,
                        help="обработать не больше N SKU (вместе с --offset делит "
                             "длинный список на части)")
    parser.add_argument("--batch-size", type=int, default=None,
                        help="сбрасывать собранное в хранилище каждые N товаров "
                             "(0 - только в конце; по умолчанию из .env)")
    parser.add_argument("--min-success-rate", type=float, default=0.0,
                        help="минимальная доля успешных SKU от длины списка (0..1); "
                             "ниже неё прогон считается неудачным")
    args = parser.parse_args()

    try:
        snapshot_date = cli.parse_date(args.date)
    except ValueError as exc:
        log.error("%s", exc)
        return 1

    try:
        skus = args.skus or (read_skus_file(args.file) if args.file else config.DEFAULT_SKUS)
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1

    return run(select_range(skus, args.offset, args.limit),
               storage_backend=args.storage or "", output=args.output,
               snapshot_date=snapshot_date, batch_size=args.batch_size,
               min_success_rate=args.min_success_rate)


if __name__ == "__main__":
    sys.exit(main())

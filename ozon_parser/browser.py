"""Запуск браузера и общие приёмы работы со страницами Ozon.

По умолчанию берётся Chromium, который ставит сам Playwright. Если на машине
он не стартует (например, Windows ругается на side-by-side configuration),
можно переключиться на установленный в системе браузер:

    BROWSER_CHANNEL=msedge   # или chrome
"""

from __future__ import annotations

import time
from typing import Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from . import config, constants
from .logger import get_logger

log = get_logger("browser")

# Чем пробуем подменить Chromium от Playwright, если он не стартует.
FALLBACK_CHANNELS = ("msedge", "chrome")

# Единые параметры контекста: русская локаль и московский часовой пояс нужны,
# чтобы Ozon отдавал рублёвые цены и русскоязычную вёрстку.
CONTEXT_DEFAULTS = {
    "locale": "ru-RU",
    "timezone_id": "Europe/Moscow",
    "viewport": {"width": 1240, "height": 680},
}

LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


def launch(playwright, headless: Optional[bool] = None):
    """Поднимает браузер согласно настройкам.

    Если канал задан явно (BROWSER_CHANNEL), используется только он. Иначе
    сначала пробуем Chromium от Playwright, а при неудаче - системные браузеры:
    на Windows его сборка нередко не стартует из-за защиты ОС.
    """
    headless = config.HEADLESS if headless is None else headless
    options = {"headless": headless, "args": LAUNCH_ARGS}

    if config.BROWSER_CHANNEL:
        log.info("Запускаю браузер (канал: %s, headless=%s)", config.BROWSER_CHANNEL, headless)
        return playwright.chromium.launch(channel=config.BROWSER_CHANNEL, **options)

    log.info("Запускаю Chromium от Playwright (headless=%s)", headless)
    try:
        return playwright.chromium.launch(**options)
    except PlaywrightError as exc:
        first_error = str(exc).splitlines()[0]
        log.warning("Chromium от Playwright не запустился: %s", first_error)

    for channel in FALLBACK_CHANNELS:
        try:
            browser = playwright.chromium.launch(channel=channel, **options)
        except PlaywrightError as exc:
            log.debug("Канал %s недоступен: %s", channel, str(exc).splitlines()[0])
            continue
        log.info("Использую системный браузер: %s", channel)
        log.info("Чтобы не терять время на лишние попытки, добавьте в .env: "
                 "BROWSER_CHANNEL=%s", channel)
        return browser

    raise PlaywrightError(
        "Не удалось запустить ни одного браузера. Установите браузеры Playwright "
        "(python -m playwright install chromium) либо укажите в .env "
        "BROWSER_CHANNEL=msedge или BROWSER_CHANNEL=chrome, если в системе есть "
        "Microsoft Edge или Google Chrome."
    )


def new_context(browser, storage_state=None):
    """Создаёт контекст с общими настройками и, если передано, с сохранённой сессией."""
    options = dict(CONTEXT_DEFAULTS, user_agent=config.USER_AGENT)
    if storage_state is not None:
        options["storage_state"] = storage_state

    context = browser.new_context(**options)
    context.set_default_timeout(config.PAGE_TIMEOUT)
    return context


def close_quietly(*items) -> None:
    """Закрывает контекст и браузер, не роняя программу на ошибках закрытия.

    Если браузер уже мёртв (окно закрыли руками, процесс упал, оборвалась
    связь с драйвером), close() сам бросает исключение - оно здесь не важно.
    """
    for item in items:
        if item is None:
            continue
        try:
            item.close()
        except Exception as exc:  # noqa: BLE001 - драйвер бросает разные классы
            log.debug("Не удалось корректно закрыть %s: %s", type(item).__name__, exc)


# ------------------------------------------------------ антибот-проверка ----
def looks_like_challenge(page: Page, response=None) -> bool:
    """True, если вместо страницы показана антибот-проверка Ozon.

    Признаки от надёжного к слабому: HTTP 403 (им Ozon отдаёт заглушку),
    заголовок "Antibot Challenge Page", видимый текст. В сыром HTML маркеры
    искать нельзя: заголовок заглушки ставит скрипт, а слова вроде
    "captcha" встречаются в скриптах обычных страниц.
    """
    if response is not None and response.status == 403:
        return True

    try:
        title = (page.title() or "").lower()
    except PlaywrightError:
        title = ""
    if any(marker in title for marker in constants.CHALLENGE_TITLE_MARKERS):
        return True

    try:
        visible = (page.inner_text("body", timeout=5_000) or "").lower()
    except PlaywrightError:
        return False
    return any(marker in visible for marker in constants.CHALLENGE_TEXT_MARKERS)


def pass_challenge(page: Page, response=None, timeout: Optional[int] = None) -> bool:
    """Дожидается, пока антибот-проверка Ozon пройдёт сама. False - не прошла.

    Заглушка "Antibot Challenge Page" - это JS-проверка браузера: обычно
    через 5-10 секунд она сама перезагружает страницу уже с настоящим
    содержимым.

    :param timeout: сколько секунд ждать; по умолчанию CHALLENGE_TIMEOUT.
    """
    if not looks_like_challenge(page, response):
        return True

    timeout = config.CHALLENGE_TIMEOUT if timeout is None else timeout
    log.info("Ozon показал антибот-проверку, жду её прохождения (до %s с)", timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page.wait_for_timeout(1_000)
        if looks_like_challenge(page):
            continue
        # Сразу после перезагрузки заголовок может быть ещё пустым - даём
        # странице загрузиться и проверяем повторно.
        try:
            page.wait_for_load_state("load", timeout=config.LOAD_STATE_TIMEOUT)
        except PlaywrightTimeout:
            pass
        if not looks_like_challenge(page):
            log.info("Антибот-проверка пройдена")
            return True

    log.warning("Антибот-проверка не прошла за %s с", timeout)
    return False

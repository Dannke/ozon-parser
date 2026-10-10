"""Авторизация на https://data.ozon.ru/ и сохранение cookies.

Как устроен вход (проверено на живом сайте):
    1. data.ozon.ru сначала отдаёт антибот-проверку (HTTP 403, "Antibot
       Challenge Page"). Это JS-проверка: через 5-10 секунд она сама
       перезагружает страницу - её нужно переждать, а не сдаваться.
    2. На лендинге нет формы - вход начинается с кнопки «Перейти к аналитике»,
       которая уводит на sso.ozon.ru (Ozon ID).
    3. Ozon ID предлагает два способа входа:
         * по почте (LOGIN_METHOD=email, по умолчанию): вводится OZON_EMAIL,
           Ozon присылает на него письмо с кодом. Почта должна быть привязана
           к аккаунту Ozon и совпадать с ящиком, к которому выдан Gmail API;
         * по телефону (LOGIN_METHOD=phone): способ подтверждения выбирает
           Ozon - QR-код в приложении, звонок, SMS или письмо. Автоматически
           проходится только письмо; на остальных скрипт останавливается с
           объяснением.
    4. Код забирается из Gmail через Gmail API и вводится в форму.
    5. После появления cookies авторизации состояние сессии (cookies +
       localStorage) сохраняется в cookies.json.

Данные о товарах отсюда НЕ забираем - нужны только cookies для parse_ozon.py.

Запуск:
    python get_cookies.py                  # войти, если сессии нет (способ из .env)
    python get_cookies.py --method phone   # вход по телефону
    python get_cookies.py --manual         # вход руками, скрипт только сохранит cookies
    python get_cookies.py --force          # перезаписать действующую сессию
    python get_cookies.py --max-age-days 14 --non-interactive   # так вызывает Airflow
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from price_panel.infra import browser as browser_utils
from price_panel.infra import config, logger
from price_panel.infra.logger import get_logger
from price_panel.marketplaces.ozon import session
from price_panel.marketplaces.ozon.gmail import GmailCodeReader, GmailError, GmailSettings

log = get_logger("get_cookies")

LOGIN_METHODS = ("email", "phone")

# Снимок экрана при неудачном входе - по нему видно, что показал Ozon.
FAILURE_SCREENSHOT = "get_cookies_failure.png"

# Селекторы перечислены списком: вёрстка Ozon регулярно меняется, поэтому
# пробуем несколько вариантов и берём первый видимый.
#
# Кнопки ищутся по точному тексту: на форме Ozon ID рядом с «Войти» есть
# «Войти по почте», «Не могу войти» и «Вернуться на главный экран», и у всех
# type=submit. Текст кнопки лежит во вложенном div, поэтому нужен вариант
# с :has(...).
EXACT_LOGIN_BUTTON = ("button:text-is('Войти')", "button:has(:text-is('Войти'))")

ENTRY_SELECTORS = (
    "button:has-text('Перейти к аналитике')",
    "a:has-text('Перейти к аналитике')",
    *EXACT_LOGIN_BUTTON,
    "a:text-is('Войти')",
)

PHONE_INPUT_SELECTORS = (
    "input[type='tel']",
    "input[name='phone']",
    "input[inputmode='tel']",
    "input[autocomplete='tel']",
)

EMAIL_LOGIN_SELECTORS = (
    "button:has(:text-is('Войти по почте'))",
    "button:text-is('Войти по почте')",
    "a:has-text('Войти по почте')",
)

EMAIL_INPUT_SELECTORS = (
    "input[type='email']",
    "input[name='email']",
    "input[inputmode='email']",
    "input[autocomplete='email']",
)

SUBMIT_SELECTORS = (
    *EXACT_LOGIN_BUTTON,
    "button:has-text('Получить код')",
    "button:has-text('Продолжить')",
    "button:has-text('Далее')",
)

# Подтверждение кода. Без общего button[type='submit']: на экранах Ozon ID
# так размечены и «Вернуться на главный экран», и «Не могу войти».
CONFIRM_SELECTORS = (
    *EXACT_LOGIN_BUTTON,
    "button:has-text('Подтвердить')",
    "button:has-text('Продолжить')",
    "button:has-text('Далее')",
)

CODE_INPUT_SELECTORS = (
    "input[autocomplete='one-time-code']",
    "input[name='code']",
    "input[name='otp']",
    "input[inputmode='numeric']",
    "input[placeholder*='код']",
    "input[maxlength='1']",
)

# На живом экране Ozon ID поле кода - обычный input без особых атрибутов.
# Эти селекторы годятся только ПОСЛЕ ухода с формы почты/телефона: там такой
# же обычный input - поле телефона.
GENERIC_INPUT_SELECTORS = (
    "input[type='text']",
    "input[type='tel']",
    "input[type='number']",
    "input:not([type])",
)

# Текст экрана ввода кода («Введите код. Отправили код на почту ...»).
CODE_SCREEN_MARKERS = ("введите код", "отправили код", "код отправлен", "код из письма")

# При входе по телефону - переключение доставки кода на почту, если Ozon
# предлагает выбор.
EMAIL_DELIVERY_SELECTORS = (
    "button:has-text('код на почту')",
    "button:has-text('на почту')",
    "a:has-text('на почту')",
)

# Экраны, где Ozon подтверждает вход не письмом: QR-код в приложении или
# звонок, после которого нужно ввести последние цифры номера. Письмо с кодом
# в этом случае не придёт, и ждать его бессмысленно.
NON_EMAIL_MARKERS = (
    "qr-код",
    "qr код",
    "qr-code",
    "последние 4 цифры",
    "последние четыре цифры",
    "последние цифры",
)


class LoginError(RuntimeError):
    """Не удалось авторизоваться на Ozon."""


# ------------------------------------------------------------ вспомогательное --
def find_visible(page: Page, selectors, timeout: int = 10_000):
    """Возвращает первый видимый элемент из списка селекторов либо None.

    Скрытые совпадения пропускаются: при переключении формы Ozon ID прежняя
    форма может остаться в разметке, и её «Войти» стоит раньше нужной.
    Общий таймаут делится между кандидатами, чтобы поиск не растягивался
    на десятки секунд при длинном списке.
    """
    per_selector = max(int(timeout / max(len(selectors), 1)), 500)
    for selector in selectors:
        try:
            locator = page.locator(selector).filter(visible=True).first
            locator.wait_for(state="visible", timeout=per_selector)
            return locator
        except (PlaywrightTimeout, PlaywrightError):
            continue
    return None


def is_authorized(context) -> bool:
    """Проверяет наличие cookies авторизации в текущем контексте браузера."""
    try:
        return session.has_auth_cookies(context.cookies())
    except PlaywrightError:
        return False


def national_number(phone: str) -> str:
    """Номер без кода страны: 79991234567 -> 9991234567.

    В Ozon ID код +7 выбран отдельным списком перед полем. Если напечатать
    номер целиком, маска примет ведущую 7 за первую цифру и отбросит последнюю.
    """
    if len(phone) == 11 and phone.startswith("7"):
        return phone[1:]
    return phone


def masked(phone: str) -> str:
    """Номер для логов: только последние четыре цифры."""
    return "***" + phone[-4:]


def masked_email(email: str) -> str:
    """Адрес для логов: первая буква и домен (j***@gmail.com)."""
    name, _, domain = email.partition("@")
    return "{}***@{}".format(name[:1], domain) if domain else "***"


def visible_text(page: Page) -> str:
    """Видимый текст страницы в нижнем регистре (пусто, если не прочитался)."""
    try:
        return (page.inner_text("body", timeout=5_000) or "").lower()
    except PlaywrightError:
        return ""


def type_text(page: Page, field, text: str) -> str:
    """Печатает текст посимвольно и возвращает то, что приняло поле.

    Посимвольно - потому что маски ввода на сайте плохо переваривают
    мгновенный fill().
    """
    field.click()
    field.fill("")
    field.press_sequentially(text, delay=config.TYPING_DELAY_MS)
    page.wait_for_timeout(500)
    return field.input_value()


def click_submit(page: Page, field) -> None:
    """Отправляет форму кнопкой «Войти» (или аналогом), в крайнем случае Enter."""
    submit = find_visible(page, SUBMIT_SELECTORS, timeout=5_000)
    if submit is not None:
        submit.click()
    else:
        log.warning("Кнопка отправки не найдена, пробую Enter")
        field.press("Enter")


def wait_for_auth(page: Page, context, timeout: int = 60) -> bool:
    """Ждёт появления cookies авторизации (секунды)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_authorized(context):
            return True
        page.wait_for_timeout(2_000)
    return False


def save_failure_screenshot(page: Page) -> None:
    """Сохраняет снимок экрана рядом с логами - по нему видно, что показал Ozon."""
    path = logger.LOG_DIR / FAILURE_SCREENSHOT
    try:
        page.screenshot(path=str(path))
        log.info("Снимок экрана на момент ошибки: %s", path)
    except PlaywrightError as exc:
        log.debug("Снимок экрана не сохранён: %s", exc)


# ------------------------------------------------------------------- шаги ----
def open_data_ozon(page: Page) -> None:
    """Открывает data.ozon.ru и дожидается прохождения антибот-проверки."""
    log.info("Открываю %s", config.DATA_OZON_URL)
    response = page.goto(
        config.DATA_OZON_URL, wait_until="domcontentloaded", timeout=config.PAGE_TIMEOUT
    )
    if not browser_utils.pass_challenge(page, response):
        raise LoginError(
            "Антибот-проверка Ozon не прошла за {} с. Попробуйте позже или войдите "
            "вручную: python get_cookies.py --manual".format(config.CHALLENGE_TIMEOUT)
        )


def open_login_form(page: Page) -> None:
    """Доводит браузер до первого экрана Ozon ID (ввод телефона)."""
    open_data_ozon(page)

    if find_visible(page, PHONE_INPUT_SELECTORS, timeout=2_000) is not None:
        return

    entry = find_visible(page, ENTRY_SELECTORS, timeout=10_000)
    if entry is None:
        raise LoginError("На data.ozon.ru не найдена кнопка входа - вероятно, изменилась вёрстка")
    log.info("Перехожу к форме входа Ozon ID")
    entry.click()

    # После клика идёт цепочка редиректов data.ozon.ru/app -> sso.ozon.ru, и
    # на Ozon ID может встретиться своя антибот-проверка. Ждём именно форму.
    deadline = time.monotonic() + config.ELEMENT_TIMEOUT / 1000
    while time.monotonic() < deadline:
        if find_visible(page, PHONE_INPUT_SELECTORS, timeout=1_000) is not None:
            return
        if browser_utils.looks_like_challenge(page):
            if not browser_utils.pass_challenge(page):
                raise LoginError("Антибот-проверка на странице Ozon ID не прошла")
            deadline = time.monotonic() + config.ELEMENT_TIMEOUT / 1000
    raise LoginError("Форма входа Ozon ID не появилась - вероятно, изменилась вёрстка")


def submit_email(page: Page, email: str) -> float:
    """Переключает Ozon ID на вход по почте и запрашивает код.

    Возвращает время запроса (unix, сек): письма старше него не рассматриваются.
    """
    switch = find_visible(page, EMAIL_LOGIN_SELECTORS, timeout=5_000)
    if switch is not None:
        log.info("Выбираю вход по почте")
        switch.click()

    email_input = find_visible(page, EMAIL_INPUT_SELECTORS, timeout=config.ELEMENT_TIMEOUT)
    if email_input is None:
        raise LoginError("Не найдено поле ввода почты - вероятно, изменилась вёрстка формы")

    log.info("Ввожу почту: %s", masked_email(email))
    accepted = type_text(page, email_input, email).strip().lower()
    if accepted != email.lower():
        raise LoginError("Поле почты приняло другое значение - изменилась форма ввода")

    requested_at = time.time()
    click_submit(page, email_input)
    log.info("Код запрошен на почту, жду письмо")
    return requested_at


def submit_phone(page: Page, phone: str) -> float:
    """Вводит телефон и запрашивает код. Возвращает время запроса (unix, сек)."""
    phone_input = find_visible(page, PHONE_INPUT_SELECTORS, timeout=config.ELEMENT_TIMEOUT)
    if phone_input is None:
        raise LoginError("Не найдено поле ввода телефона - вероятно, изменилась вёрстка формы")

    digits = national_number(phone)
    log.info("Ввожу номер телефона: %s", masked(phone))
    # Сверяемся с тем, что приняла маска: код, отправленный на чужой номер,
    # сожжёт попытку входа.
    accepted = re.sub(r"\D", "", type_text(page, phone_input, digits))
    if accepted not in (digits, phone):
        raise LoginError(
            "Поле телефона приняло {} вместо {} - изменилась маска ввода".format(
                masked(accepted), masked(phone)
            )
        )

    requested_at = time.time()
    click_submit(page, phone_input)

    # Если Ozon предлагает выбрать, куда прислать код, - выбираем почту.
    email_option = find_visible(page, EMAIL_DELIVERY_SELECTORS, timeout=3_000)
    if email_option is not None:
        log.info("Выбираю доставку кода на почту")
        email_option.click()

    log.info("Код запрошен, жду письмо")
    return requested_at


def wait_for_code_screen(page: Page) -> None:
    """Дожидается экрана ввода кода из письма.

    Если Ozon выбрал подтверждение через QR-код или звонок, письмо не придёт -
    сообщаем об этом сразу, а не после нескольких минут ожидания почты.
    """
    deadline = time.monotonic() + config.ELEMENT_TIMEOUT / 1000
    while time.monotonic() < deadline:
        text = visible_text(page)
        if any(marker in text for marker in NON_EMAIL_MARKERS):
            raise LoginError(
                "Ozon выбрал подтверждение не письмом, а через QR-код в приложении "
                "или звонок - письмо с кодом не придёт. Войдите по почте "
                "(LOGIN_METHOD=email, почта привязана к аккаунту Ozon) или "
                "вручную: python get_cookies.py --manual"
            )
        if any(marker in text for marker in CODE_SCREEN_MARKERS):
            return
        if find_visible(page, CODE_INPUT_SELECTORS, timeout=1_000) is not None:
            return
    raise LoginError(
        "Экран ввода кода не появился - Ozon мог отклонить запрос "
        "(неизвестная почта, лимит попыток) или изменилась вёрстка"
    )


def fill_code(page: Page, code: str) -> None:
    """Вводит код подтверждения.

    Поддерживаются оба варианта формы: одно поле на весь код и набор полей
    по одной цифре.
    """
    code_input = find_visible(
        page, CODE_INPUT_SELECTORS + GENERIC_INPUT_SELECTORS, timeout=config.ELEMENT_TIMEOUT
    )
    if code_input is None:
        raise LoginError("Не найдено поле ввода кода подтверждения")

    digit_inputs = page.locator("input[maxlength='1']")
    try:
        digit_count = digit_inputs.count()
    except PlaywrightError:
        digit_count = 0

    if digit_count >= len(code):
        log.info("Ввожу код по цифрам (%s полей)", digit_count)
        for index, digit in enumerate(code):
            field = digit_inputs.nth(index)
            field.click()
            field.press_sequentially(digit, delay=config.TYPING_DELAY_MS)
    else:
        log.info("Ввожу код одним полем")
        code_input.click()
        code_input.fill("")
        code_input.press_sequentially(code, delay=config.TYPING_DELAY_MS)

    page.wait_for_timeout(1_000)

    # Обычно форма отправляется сама после последней цифры - кнопку жмём,
    # только если она ещё на экране.
    confirm = find_visible(page, CONFIRM_SELECTORS, timeout=3_000)
    if confirm is not None:
        try:
            confirm.click()
        except PlaywrightError as exc:
            log.debug("Кнопку подтверждения нажать не удалось (%s) - вероятно, форма уже ушла", exc)


def settle_after_login(page: Page) -> None:
    """Даёт редиректам Ozon ID -> data.ozon.ru закончиться перед сохранением.

    Токены появляются раньше, чем браузер вернётся на data.ozon.ru и получит
    cookies этого домена.
    """
    try:
        page.wait_for_load_state("load", timeout=config.LOAD_STATE_TIMEOUT)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(3_000)


# ------------------------------------------------------------------ сценарии --
def login_automatic(page: Page, context, gmail: GmailCodeReader, method: str) -> None:
    """Автоматический вход: почта или телефон -> код из Gmail -> cookies."""
    open_login_form(page)
    if method == "email":
        requested_at = submit_email(page, config.OZON_EMAIL)
    else:
        requested_at = submit_phone(page, config.OZON_PHONE)
    wait_for_code_screen(page)

    code = gmail.wait_for_code(since_ts=requested_at)
    log.info("Получен код подтверждения (%s цифр)", len(code))

    fill_code(page, code)

    if not wait_for_auth(page, context):
        raise LoginError(
            "После ввода кода признаки авторизации не появились. "
            "Возможные причины: код устарел, введён неверно или требуется капча."
        )
    settle_after_login(page)
    log.info("Авторизация успешна")


def login_manual(page: Page, context) -> None:
    """Ручной вход: скрипт открывает сайт и ждёт, пока человек войдёт сам."""
    open_data_ozon(page)
    timeout = config.MANUAL_LOGIN_TIMEOUT
    log.info("Ручной режим: войдите в аккаунт в окне браузера (жду до %s с)", timeout)

    if not wait_for_auth(page, context, timeout=timeout):
        raise LoginError("За {} с вход так и не был выполнен".format(timeout))
    settle_after_login(page)
    log.info("Авторизация успешна")


# --------------------------------------------------------------------- main --
def check_settings(method: str) -> str | None:
    """Текст ошибки, если для выбранного способа входа не хватает настроек."""
    if method not in LOGIN_METHODS:
        return "Неизвестный способ входа LOGIN_METHOD={!r}, допустимо: {}".format(
            method, ", ".join(LOGIN_METHODS)
        )
    if not config.OZON_EMAIL:
        # Нужна при любом способе: по ней выбирается письмо с кодом в ящике.
        return "Не задан OZON_EMAIL. Заполните .env (см. .env.example)"
    if method == "phone" and not config.OZON_PHONE:
        return "Не задан OZON_PHONE. Заполните .env (см. .env.example)"
    return None


def run(
    manual: bool = False,
    force: bool = False,
    method: str = "",
    max_age_days: float | None = None,
    interactive: bool = True,
) -> int:
    """Точка входа. Возвращает код возврата процесса.

    :param force: войти заново, даже если сессия действительна.
    :param max_age_days: считать сессию старше N дней протухшей и войти заново.
    :param interactive: False - запуск без человека (Airflow): ни ручного
        входа, ни окна согласия Google.
    """
    cookies_path: Path = config.COOKIES_FILE
    method = (method or config.LOGIN_METHOD).lower()

    if not force:
        reason = session.refresh_reason(cookies_path, max_age_days)
        if reason is None:
            log.info("Сессия действительна: %s (перезаписать: --force)", cookies_path)
            return 0
        log.info("Нужен новый вход: %s", reason)

    if manual and not interactive:
        log.error("Ручной вход невозможен в фоновом запуске")
        return 1

    gmail: GmailCodeReader | None = None
    if not manual:
        problem = check_settings(method)
        if problem:
            log.error("%s", problem)
            return 1

        # Подключаемся к почте ДО запроса кода: иначе потратим попытку впустую.
        gmail = GmailCodeReader(
            GmailSettings(
                credentials_file=config.GMAIL_CREDENTIALS_FILE,
                token_file=config.GMAIL_TOKEN_FILE,
                sender_filter=config.GMAIL_SENDER_FILTER,
                wait_timeout=config.GMAIL_WAIT_TIMEOUT,
                poll_interval=config.GMAIL_POLL_INTERVAL,
                recipient=config.OZON_EMAIL,
                interactive=interactive,
            )
        )
        try:
            gmail.connect()
        except GmailError as exc:
            log.error("Нет доступа к почте: %s", exc)
            return 1
        log.info("Способ входа: %s", "по почте" if method == "email" else "по телефону")

    with sync_playwright() as playwright:
        # В ручном режиме окно браузера обязано быть видимым.
        browser = browser_utils.launch(playwright, headless=config.HEADLESS and not manual)
        context = browser_utils.new_context(browser)
        page = context.new_page()

        try:
            if gmail is None:
                login_manual(page, context)
            else:
                login_automatic(page, context, gmail, method)
            session.save_session(context, cookies_path)
            return 0
        except (LoginError, GmailError) as exc:
            log.error("Вход не выполнен: %s", exc)
            save_failure_screenshot(page)
        except PlaywrightTimeout as exc:
            log.error("Страница не загрузилась вовремя: %s", exc)
            save_failure_screenshot(page)
        except PlaywrightError as exc:
            log.error("Ошибка браузера: %s", exc)
            save_failure_screenshot(page)
        except OSError as exc:
            log.error("Не удалось записать файл сессии: %s", exc)
        except KeyboardInterrupt:
            log.warning("Прервано пользователем")
            return 130
        finally:
            browser_utils.close_quietly(context, browser)

        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Авторизация на data.ozon.ru и сохранение cookies")
    parser.add_argument(
        "--method",
        choices=LOGIN_METHODS,
        help="способ входа: по почте или по телефону (по умолчанию из .env)",
    )
    parser.add_argument("--manual", action="store_true", help="войти вручную в открытом браузере")
    parser.add_argument("--force", action="store_true", help="перезаписать действующую сессию")
    parser.add_argument(
        "--max-age-days", type=float, help="войти заново, если сессия старше N дней"
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="запуск без человека (Airflow): без ручного входа и без окна согласия Google",
    )
    args = parser.parse_args()
    return run(
        manual=args.manual,
        force=args.force,
        method=args.method or "",
        max_age_days=args.max_age_days,
        interactive=not args.non_interactive,
    )


if __name__ == "__main__":
    sys.exit(main())

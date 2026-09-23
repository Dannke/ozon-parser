"""Офлайн-проверка конвейера целиком: браузер настоящий, Ozon - подменённый.

Все запросы к ozon.ru перехватываются Playwright и обслуживаются фикстурой,
поэтому тест не ходит в сеть и не зависит от доступности сайта. Проверяется
связка, которую не покрывает test_extract.py: запрос к API из контекста
страницы, разбор ответа, ввод кода в форму, сохранение сессии и запись CSV.

Каждая проверка работает в своём контексте браузера, чтобы подменённые
маршруты одной не влияли на другую.

Нужен установленный браузер (см. README) - без него браузерные проверки
пропускаются, а не падают. Запуск:

    pytest tests/test_pipeline.py
"""

from __future__ import annotations

import atexit
import contextlib
import json
import tempfile
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from test_extract import FIXTURE, card_html

from ozon_parser import browser as browser_utils
from ozon_parser import extract, login, parse, session, storage

# Кодировка обязательна: без неё браузер читает кириллицу заглушек как latin-1.
HTML_TYPE = "text/html; charset=utf-8"

# Карточка со встроенным JSON - на неё переходит парсер, если API недоступен.
STUB_PRODUCT_HTML = card_html(
    {"webProductHeading-1-default-1": {"title": "Кресло из HTML"},
     "webPrice-2-default-1": {"price": "9 990 ₽"}},
).replace("<head>", "<head><title>stub</title>")

# Антибот-заглушка, которая через секунду «проходит» сама, как у Ozon.
STUB_CHALLENGE_HTML = """
<html><head><title>Antibot Challenge Page</title></head><body>
  <p>Проверяем браузер</p>
  <script>
    setTimeout(() => {
      document.title = 'Товар';
      document.body.innerHTML = '<h1>Товар</h1>';
    }, 1000);
  </script>
</body></html>
"""

STUB_ENDLESS_CHALLENGE_HTML = (
    "<html><head><title>Antibot Challenge Page</title></head><body>wait</body></html>"
)

# Форма Ozon ID, повторяющая живую: код +7 выбран отдельно, «Войти по почте»
# переключает на форму с полем почты. На ней у «Вернуться на главный экран»
# тоже type=submit, и в разметке она стоит раньше «Войти» - простой
# button[type=submit] нажал бы не ту кнопку. «Войти» показывает экран кода.
STUB_LOGIN_HTML = """
<html><body>
  <form id="phone" onsubmit="return false">
    <span>+7</span><input type="tel" name="autocomplete">
    <button type="submit"><div>Войти</div></button>
    <button type="button" onclick="show('email')"><div>Войти по почте</div></button>
  </form>
  <form id="email" style="display:none" onsubmit="return false">
    <h2>Войдите по почте</h2>
    <input type="email" name="email">
    <button type="submit" onclick="show('phone')"><div>Вернуться на главный экран</div></button>
    <button type="submit" onclick="show('code')"><div>Войти</div></button>
    <button type="submit"><div>Не могу войти</div></button>
  </form>
  <form id="code" style="display:none" onsubmit="return false">
    <p>Мы отправили код на почту</p>
    <input maxlength="1"><input maxlength="1"><input maxlength="1">
    <input maxlength="1"><input maxlength="1"><input maxlength="1">
  </form>
  <script>
    function show(id) {
      for (const form of document.forms) form.style.display = form.id === id ? '' : 'none';
    }
  </script>
</body></html>
"""

# Ozon выбрал подтверждение через приложение - письмо не придёт.
STUB_QR_HTML = """
<html><body><h2>Отсканируйте QR-код</h2><p>Откройте приложение Ozon</p></body></html>
"""

# Поле, маска которого обрезает номер: такой номер отправлять нельзя.
STUB_BROKEN_MASK_HTML = """
<html><body><form onsubmit="return false">
  <input type="tel" maxlength="7"><button type="submit">Войти</button>
</form></body></html>
"""

# Экран кода как на живом Ozon ID: одно обычное поле без особых атрибутов.
STUB_SINGLE_CODE_HTML = """
<html><body>
  <h2>Введите код</h2><p>Отправили код на почту</p>
  <input type="text" placeholder="------">
</body></html>
"""

STUB_CODE_HTML = """
<html><body>
  <form>
    <input maxlength="1"><input maxlength="1"><input maxlength="1">
    <input maxlength="1"><input maxlength="1"><input maxlength="1">
  </form>
</body></html>
"""


# ------------------------------------------------------------------ браузер --
_PLAYWRIGHT = None
_BROWSER = None


def _shutdown() -> None:
    global _PLAYWRIGHT, _BROWSER
    browser_utils.close_quietly(_BROWSER)
    if _PLAYWRIGHT is not None:
        _PLAYWRIGHT.stop()
    _BROWSER = _PLAYWRIGHT = None


def browser():
    """Поднимает браузер один раз на весь модуль (закрывается через atexit)."""
    global _PLAYWRIGHT, _BROWSER
    if _BROWSER is None:
        _PLAYWRIGHT = sync_playwright().start()
        try:
            _BROWSER = browser_utils.launch(_PLAYWRIGHT, headless=True)
        except Exception as exc:  # noqa: BLE001 - драйвер бросает разные классы
            _PLAYWRIGHT.stop()
            _PLAYWRIGHT = None
            pytest.skip("браузер не запустился ({}). См. README: "
                        "python -m playwright install chromium".format(exc))
        atexit.register(_shutdown)
    return _BROWSER


def make_route_handler(api_status: int = 200):
    """Обработчик маршрутов: отдаёт фикстуру вместо ответов ozon.ru."""

    def handler(route, request):
        url = request.url
        if "entrypoint-api.bx" in url:
            if api_status != 200:
                route.fulfill(status=api_status, content_type="application/json", body="{}")
            else:
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(FIXTURE, ensure_ascii=False))
        elif "single-code" in url:
            route.fulfill(status=200, content_type=HTML_TYPE, body=STUB_SINGLE_CODE_HTML)
        elif "/qr" in url:
            route.fulfill(status=200, content_type=HTML_TYPE, body=STUB_QR_HTML)
        elif "broken-mask" in url:
            route.fulfill(status=200, content_type=HTML_TYPE, body=STUB_BROKEN_MASK_HTML)
        elif "login" in url:
            route.fulfill(status=200, content_type=HTML_TYPE, body=STUB_LOGIN_HTML)
        elif "code" in url:
            route.fulfill(status=200, content_type=HTML_TYPE, body=STUB_CODE_HTML)
        elif "endless-challenge" in url:
            route.fulfill(status=403, content_type=HTML_TYPE, body=STUB_ENDLESS_CHALLENGE_HTML)
        elif "challenge" in url:
            route.fulfill(status=403, content_type=HTML_TYPE, body=STUB_CHALLENGE_HTML)
        else:
            route.fulfill(status=200, content_type=HTML_TYPE, body=STUB_PRODUCT_HTML)

    return handler


@contextlib.contextmanager
def stub_context(api_status: int = 200):
    """Свежий контекст браузера с подменёнными ответами Ozon."""
    context = browser_utils.new_context(browser())
    context.route("**/*", make_route_handler(api_status))
    try:
        yield context
    finally:
        browser_utils.close_quietly(context)


def _parse(sku: str, api_status: int = 200) -> dict:
    with stub_context(api_status) as context:
        page = context.new_page()
        page.goto("https://www.ozon.ru/", wait_until="domcontentloaded")
        return parse.parse_sku(page, sku)


# --------------------------------------------------- проверки без браузера --
def test_read_skus_file_skips_comments_and_duplicates():
    """Комментарий с отступом - не SKU, а повтор товара не нужно открывать дважды.

    Регрессия: проверка на "#" шла по неочищенной строке, поэтому "  # текст"
    уезжал в список как SKU и честно отрабатывал все MAX_RETRIES попыток.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "skus.txt"
        path.write_text(
            "# шапка файла\n"
            "2359066702\n"
            "   # комментарий с отступом\n"
            "\n"
            "  2829800382  \n"
            "2359066702\n",
            encoding="utf-8",
        )
        assert parse.read_skus_file(path) == ["2359066702", "2829800382"]


def test_select_range_splits_long_list():
    """--offset/--limit режут длинный список на части для нескольких задач.

    При ~10-15 с на товар сорокаминутная задача успевает около двухсот SKU,
    поэтому список должен делиться без правки файла.
    """
    skus = [str(number) for number in range(10)]
    assert parse.select_range(skus) == skus
    assert parse.select_range(skus, offset=3) == skus[3:]
    assert parse.select_range(skus, limit=4) == skus[:4]
    assert parse.select_range(skus, offset=8, limit=5) == ["8", "9"]
    assert parse.select_range(skus, offset=20) == []
    # Отрицательный offset не должен резать список с конца.
    assert parse.select_range(skus, offset=-3) == skus


def test_national_number_drops_country_code():
    """В Ozon ID код +7 выбран отдельно - в поле идут только 10 цифр."""
    assert login.national_number("79991234567") == "9991234567"
    assert login.national_number("9991234567") == "9991234567"
    assert login.masked("79991234567") == "***4567"
    assert login.masked_email("seller@gmail.com") == "s***@gmail.com"


def test_login_settings_are_checked_per_method(monkeypatch):
    """Каждому способу входа - свои обязательные настройки."""
    from ozon_parser import config

    monkeypatch.setattr(config, "OZON_EMAIL", "seller@gmail.com")
    monkeypatch.setattr(config, "OZON_PHONE", "")
    assert login.check_settings("email") is None
    assert "OZON_PHONE" in (login.check_settings("phone") or "")
    assert "LOGIN_METHOD" in (login.check_settings("sms") or "")

    monkeypatch.setattr(config, "OZON_EMAIL", "")
    assert "OZON_EMAIL" in (login.check_settings("email") or "")


# --------------------------------------------------- проверки с браузером ---
def test_record_contract_is_same_for_api_and_html():
    """Оба пути отдают записи одной формы - иначе в CSV и БД появятся дыры."""
    assert set(_parse("2359066702")) == set(extract.parse_html(STUB_PRODUCT_HTML, "1"))


def test_source_marks_where_data_came_from():
    """Если API недоступен, данные берутся из JSON в HTML и помечаются source=html."""
    assert _parse("2359066702")["source"] == extract.SOURCE_API

    product = _parse("2359066702", api_status=500)
    assert product["source"] == extract.SOURCE_HTML
    assert product["title"] == "Кресло из HTML"
    assert product["price"] == 9990.0
def test_parse_sku_reads_fields_from_api():
    """Запрос к внутреннему API из контекста страницы и разбор ответа."""
    product = _parse("2359066702")

    assert product["title"] == "Кресло офисное компьютерное, серый"
    assert product["price"] == 12490.0
    assert product["photos_seller"] == 3  # дубль из мобильной галереи отброшен
    assert product["videos_seller"] == 1


def test_missing_sku_gives_none_without_retries():
    """404 от API - это ProductNotFound: повторять бессмысленно, товара нет."""
    with stub_context(api_status=404) as context:
        page = context.new_page()
        page.goto("https://www.ozon.ru/", wait_until="domcontentloaded")
        assert parse.parse_sku_with_retries(page, "0000000000", retries=1) is None


def test_login_form_selectors_find_phone_input():
    """Списки селекторов из get_cookies находят поле телефона в форме входа."""
    with stub_context() as context:
        page = context.new_page()
        page.goto("https://data.ozon.ru/login", wait_until="domcontentloaded")
        found = login.find_visible(page, login.PHONE_INPUT_SELECTORS, timeout=3_000)
        assert found is not None, "find_visible не нашёл input[type=tel]"


def test_submit_button_is_not_login_by_email():
    """«Войти» ищется по точному тексту: «Войти по почте» - другая кнопка."""
    with stub_context() as context:
        page = context.new_page()
        page.goto("https://data.ozon.ru/login", wait_until="domcontentloaded")
        submit = login.find_visible(page, login.SUBMIT_SELECTORS, timeout=3_000)
        assert submit is not None and submit.inner_text().strip() == "Войти"
        assert login.find_visible(page, login.EMAIL_DELIVERY_SELECTORS, timeout=1_000) is None


def test_submit_email_switches_form_and_reaches_code_screen():
    """Вход по почте: переключение формы, ввод адреса, «Войти» - и экран кода."""
    with stub_context() as context:
        page = context.new_page()
        page.goto("https://data.ozon.ru/login", wait_until="domcontentloaded")
        login.submit_email(page, "Seller@Gmail.com")
        assert page.input_value("input[type='email']") == "Seller@Gmail.com"
        # Нажата именно «Войти», а не стоящая раньше «Вернуться на главный экран».
        login.wait_for_code_screen(page)
        assert page.is_visible("#code")


def test_plain_code_field_is_recognised_and_filled():
    """Экран кода узнаётся по тексту, код вводится в обычное поле."""
    with stub_context() as context:
        page = context.new_page()
        page.goto("https://sso.ozon.ru/single-code", wait_until="domcontentloaded")
        login.wait_for_code_screen(page)
        login.fill_code(page, "483712")
        assert page.input_value("input") == "483712"


def test_qr_confirmation_fails_fast():
    """Если Ozon выбрал QR-код или звонок, письма не ждём - сразу понятная ошибка."""
    with stub_context() as context:
        page = context.new_page()
        page.goto("https://sso.ozon.ru/qr", wait_until="domcontentloaded")
        with pytest.raises(login.LoginError, match="QR"):
            login.wait_for_code_screen(page)


def test_submit_phone_types_national_number():
    """Телефон вводится без кода страны и сверяется с тем, что приняло поле."""
    with stub_context() as context:
        page = context.new_page()
        page.goto("https://data.ozon.ru/login", wait_until="domcontentloaded")
        login.submit_phone(page, "79991234567")
        assert page.input_value("input[type='tel']") == "9991234567"


def test_submit_phone_refuses_mangled_number():
    """Если маска исказила номер, код не запрашивается."""
    with stub_context() as context:
        page = context.new_page()
        page.goto("https://data.ozon.ru/broken-mask", wait_until="domcontentloaded")
        with pytest.raises(login.LoginError):
            login.submit_phone(page, "79991234567")


def test_challenge_is_waited_out():
    """Антибот-заглушка, которая проходит сама, не считается отказом."""
    with stub_context() as context:
        page = context.new_page()
        response = page.goto("https://data.ozon.ru/challenge", wait_until="domcontentloaded")
        assert browser_utils.looks_like_challenge(page, response)
        assert browser_utils.pass_challenge(page, response, timeout=10) is True
        assert page.title() == "Товар"


def test_endless_challenge_gives_up():
    """Проверка, которая не проходит, - отказ по таймауту, а не вечное ожидание."""
    with stub_context() as context:
        page = context.new_page()
        response = page.goto("https://data.ozon.ru/endless-challenge",
                             wait_until="domcontentloaded")
        assert browser_utils.pass_challenge(page, response, timeout=2) is False


def test_fill_code_types_digit_by_digit():
    """Форма из шести однознаковых полей заполняется по цифре в каждое."""
    with stub_context() as context:
        page = context.new_page()
        page.goto("https://data.ozon.ru/code", wait_until="domcontentloaded")
        login.fill_code(page, "483712")
        entered = page.eval_on_selector_all(
            "input[maxlength='1']", "els => els.map(e => e.value).join('')"
        )
    assert entered == "483712", entered


def test_save_session_writes_storage_state():
    """Состояние сессии выгружается в файл, который потом читает парсер."""
    with stub_context() as context:
        page = context.new_page()
        page.goto("https://www.ozon.ru/", wait_until="domcontentloaded")
        context.add_cookies([{"name": "__Secure-access-token", "value": "x",
                              "domain": ".ozon.ru", "path": "/", "secure": True}])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cookies.json"
            session.save_session(context, path)
            state = session.load_session(path)
            assert session.is_logged_in(path)
    assert "cookies" in state


def test_parsed_product_lands_in_csv():
    """Сквозной путь: браузер -> API -> разбор -> строка в CSV."""
    product = _parse("2359066702")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "products.csv"
        storage.save([product], backend="csv", csv_path=path)
        lines = path.read_text(encoding="utf-8-sig").splitlines()

    assert lines[0].startswith("sku,title,price"), lines[0]
    assert len(lines) == 2, lines
    assert "Кресло офисное компьютерное" in lines[1]


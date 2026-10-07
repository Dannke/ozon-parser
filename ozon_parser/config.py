"""Конфигурация проекта: читается из переменных окружения / файла .env.

Все значения имеют разумные значения по умолчанию, поэтому для быстрого
старта достаточно задать OZON_EMAIL и положить рядом credentials.json.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv

# Корень проекта: пакет лежит уровнем ниже, а .env, cookies.json и data/
# - рядом с точками входа, в корне.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key) or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    value = _env(key).lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "y", "on"}


def _resolve(path_value: str, fallback: str) -> Path:
    """Превращает относительный путь из .env в абсолютный (относительно проекта)."""
    path = Path(path_value or fallback)
    return path if path.is_absolute() else BASE_DIR / path


# --- Авторизация --------------------------------------------------------------
# Способ входа в Ozon ID: email - код приходит письмом на OZON_EMAIL;
# phone - способ подтверждения выбирает Ozon (QR, звонок, SMS или письмо).
LOGIN_METHOD = _env("LOGIN_METHOD", "email").lower()
OZON_PHONE_RAW = _env("OZON_PHONE")
# Почта, привязанная к аккаунту Ozon, - тот же ящик Gmail, что и в Gmail API.
OZON_EMAIL = _env("OZON_EMAIL")

# --- Gmail --------------------------------------------------------------------
GMAIL_CREDENTIALS_FILE = _resolve(_env("GMAIL_CREDENTIALS_FILE"), "credentials.json")
GMAIL_TOKEN_FILE = _resolve(_env("GMAIL_TOKEN_FILE"), "token.json")
# Фильтр по домену отправителя: свободное слово "ozon" находит и чужие письма.
GMAIL_SENDER_FILTER = _env("GMAIL_SENDER_FILTER", "from:(ozon.ru OR ozon.com)")
GMAIL_WAIT_TIMEOUT = _env_int("GMAIL_WAIT_TIMEOUT", 180)
GMAIL_POLL_INTERVAL = _env_int("GMAIL_POLL_INTERVAL", 5)

# --- Файлы --------------------------------------------------------------------
COOKIES_FILE = _resolve(_env("COOKIES_FILE"), "cookies.json")
OUTPUT_CSV = _resolve(_env("OUTPUT_CSV"), "data/products.csv")

# --- Браузер ------------------------------------------------------------------
HEADLESS = _env_bool("HEADLESS", False)
PAGE_TIMEOUT = _env_int("PAGE_TIMEOUT", 60_000)

# Пусто - используется Chromium, установленный самим Playwright.
# Допустимые значения: msedge, chrome (браузер, уже стоящий в системе).
BROWSER_CHANNEL = _env("BROWSER_CHANNEL")

REQUEST_DELAY = _env_float("REQUEST_DELAY", 3.0)
# Карточки открываются не чаще раза в столько секунд, как бы быстро ни шёл
# разбор. Блокирует Ozon не время разбора, а частота страниц: 07.10.2026 при
# ~14 карточках в минуту (та же пауза 3 с, разбор из HTML) капча пришла на
# 14-й минуте, а при ~9 в минуту прогоны идут часами. 6.5 с - не чаще ~9
# карточек в минуту. 0 - без предела, только REQUEST_DELAY.
PAGE_INTERVAL = _env_float("PAGE_INTERVAL", 6.5)
MAX_RETRIES = _env_int("MAX_RETRIES", 2)

# Сколько раз перезапускать браузер, если он упал посреди списка SKU.
MAX_BROWSER_RESTARTS = _env_int("MAX_BROWSER_RESTARTS", 2)

# Предохранитель: столько SKU подряд без данных (кроме «товара нет», 404) -
# и прогон останавливается. Так выглядит блокировка Ozon: 30.09.2026 прогон в
# Docker 5 часов получал 403 на каждый товар. 0 - не останавливаться.
MAX_CONSECUTIVE_FAILURES = _env_int("MAX_CONSECUTIVE_FAILURES", 10)
# Отдельный, меньший порог для самого явного признака блокировки - антибот-
# проверки, которая не прошла за CHALLENGE_TIMEOUT. 01.10.2026 после первой
# такой не прошла ни одна страница. 0 - не учитывать отдельно.
MAX_CONSECUTIVE_CHALLENGES = _env_int("MAX_CONSECUTIVE_CHALLENGES", 3)

# Сколько ждать полной загрузки (load) сверх domcontentloaded, мс.
LOAD_STATE_TIMEOUT = _env_int("LOAD_STATE_TIMEOUT", 15_000)
# Пауза после загрузки перед запросом к API из вкладки, мс: Ozon нередко
# делает ещё один переход сразу после domcontentloaded, и запрос падает на
# уничтоженном контексте. Разбор одного HTML её не ждёт. 06.10.2026 и 500 мс
# обходились без таких сбоев, но 1500 - значение проверенных прогонов.
PAGE_SETTLE_MS = _env_int("PAGE_SETTLE_MS", 1_500)
# Сколько ждать, пока антибот-проверка Ozon пройдёт сама, секунды.
CHALLENGE_TIMEOUT = _env_int("CHALLENGE_TIMEOUT", 30)

# Таймаут появления обязательного элемента формы входа, мс.
ELEMENT_TIMEOUT = _env_int("ELEMENT_TIMEOUT", 20_000)
# Задержка между символами при вводе, мс: маска ввода на сайте плохо
# переваривает мгновенный fill().
TYPING_DELAY_MS = _env_int("TYPING_DELAY_MS", 80)
# Сколько ждать входа человека в режиме --manual, секунды.
MANUAL_LOGIN_TIMEOUT = _env_int("MANUAL_LOGIN_TIMEOUT", 300)

# Через сколько собранных товаров сбрасывать результат в хранилище: сбой на
# последних SKU длинного списка не должен стоить всей работы.
BATCH_SIZE = _env_int("BATCH_SIZE", 50)

USER_AGENT = _env(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
)

# --- Хранилище ----------------------------------------------------------------
STORAGE = _env("STORAGE", "csv").lower()

PG_DSN = _env("PG_DSN")
PG_TABLE = _env("PG_TABLE", "ozon_products")

# Учётная запись и база PostgreSQL в docker-compose (те же переменные читает
# docker-compose.yml). Нужны резервной копии: pg_dump запускается внутри
# контейнера, где вход по локальному сокету не спрашивает пароль.
POSTGRES_USER = _env("POSTGRES_USER", "ozon")
POSTGRES_DB = _env("POSTGRES_DB", "ozon")

CH_HOST = _env("CH_HOST", "localhost")
CH_PORT = _env_int("CH_PORT", 8123)
CH_USER = _env("CH_USER", "default")
CH_PASSWORD = _env("CH_PASSWORD")
CH_DATABASE = _env("CH_DATABASE", "default")
CH_TABLE = _env("CH_TABLE", "ozon_products")

# --- Оповещения о ежедневном прогоне (notify.py) ------------------------------
# Telegram: токен бота от @BotFather и id чата. Пишет только о проблемах.
# Пусто - выключено.
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID")
# «Пульс» для сервиса мониторинга вроде healthchecks.io: успех - этот адрес,
# неудача - адрес + /fail. Пусто - выключено.
HEALTHCHECK_URL = _env("HEALTHCHECK_URL")

# --- Адреса -------------------------------------------------------------------
DATA_OZON_URL = "https://data.ozon.ru/"
PRODUCT_URL_TEMPLATE = "https://www.ozon.ru/product/{sku}/"

# Список SKU по умолчанию (переопределяется аргументами командной строки).
DEFAULT_SKUS = ["2359066702", "2829800382"]


def normalize_phone(raw: str) -> str:
    """Приводит телефон к виду 79991234567: только цифры, код страны 7.

    Принимает любую запись: +7 999 123-45-67, 8 (999) 123-45-67, 9991234567.
    """
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    if len(digits) == 10:  # ввели без кода страны
        digits = "7" + digits
    return digits


OZON_PHONE = normalize_phone(OZON_PHONE_RAW)

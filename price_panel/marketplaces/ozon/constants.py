"""Константы, общие для нескольких модулей."""

from __future__ import annotations

# Токены авторизованной сессии Ozon: наличие любого из них означает, что
# вход выполнен (__Secure-user-id сам по себе доступа не даёт).
AUTH_COOKIE_NAMES = (
    "__Secure-access-token",
    "__Secure-refresh-token",
)

# Признаки антибот-проверки в ТЕЛЕ ответа API: вместо JSON приходит HTML.
CHALLENGE_BODY_MARKERS = (
    "доступ ограничен",
    "access denied",
    "ничего не найдено",
    "captcha",
)

# Откуда парсер берёт цену и остальные поля первой части карточки
# (parse.ParseOptions, parser.price_source в config.yaml).
PRICE_SOURCE_API = "api"
PRICE_SOURCE_HTML = "html"
PRICE_SOURCES = (PRICE_SOURCE_API, PRICE_SOURCE_HTML)

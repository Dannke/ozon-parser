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

# Заголовок страницы у заглушки Ozon ("Antibot Challenge Page").
CHALLENGE_TITLE_MARKERS = ("antibot", "challenge", "доступ ограничен")

# Видимый текст, по которому опознаём проверку, если заголовок ни о чём не
# говорит. Искать эти слова в СЫРОМ html нельзя: они встречаются в скриптах
# совершенно обычных страниц.
CHALLENGE_TEXT_MARKERS = (
    "доступ ограничен",
    "access denied",
    "вы не робот",
    "подтвердите, что вы",
)

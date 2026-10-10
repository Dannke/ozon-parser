"""Файл сессии Ozon (cookies.json): чтение, проверка и запись.

Файл - это ``storage_state`` Playwright: cookies всех доменов Ozon плюс
localStorage. get_cookies.py его пишет и перевыпускает, parse_ozon.py читает.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from price_panel.infra import secrets_fs
from price_panel.infra.logger import get_logger
from price_panel.marketplaces.ozon import constants

log = get_logger("session")


class SessionError(RuntimeError):
    """Файла сессии нет или он не читается."""


def has_auth_cookies(cookies) -> bool:
    """True, если среди cookies есть токены авторизованной сессии."""
    names = {cookie.get("name") for cookie in cookies or []}
    return any(name in names for name in constants.AUTH_COOKIE_NAMES)


def read_state(path: Path) -> dict:
    """Читает файл сессии как есть.

    :raises SessionError: файла нет или он повреждён.
    """
    if not path.exists():
        raise SessionError(
            "Файл cookies не найден: {}. Сначала запустите get_cookies.py".format(path)
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionError("Файл cookies повреждён ({}): {}".format(path, exc)) from exc
    if not isinstance(state, dict):
        raise SessionError("Файл cookies повреждён ({}): ожидался объект JSON".format(path))
    return state


def age_days(path: Path) -> float:
    """Возраст файла сессии в днях (по времени последней записи)."""
    return (time.time() - path.stat().st_mtime) / 86400


def refresh_reason(path: Path, max_age_days: float | None = None) -> str | None:
    """Почему сессию нужно перевыпустить, или None, если она годится.

    :param max_age_days: считать сессию протухшей, если файл старше; None -
        возраст не проверять. Ozon инвалидирует сессию сам, и по возрасту это
        видно раньше, чем по пустому результату парсинга.
    """
    try:
        state = read_state(path)
    except SessionError as exc:
        return str(exc)
    if not has_auth_cookies(state.get("cookies")):
        return "в {} нет токенов авторизации".format(path.name)
    if max_age_days is not None:
        age = age_days(path)
        if age > max_age_days:
            return "сессия обновлялась {:.1f} дн. назад (лимит {:g})".format(age, max_age_days)
    return None


def is_logged_in(path: Path) -> bool:
    """True, если файл сессии есть и в нём сохранены токены авторизации."""
    return refresh_reason(path) is None


def load_session(path: Path) -> dict | None:
    """Читает состояние сессии для ``browser.new_context(storage_state=...)``.

    Вход для карточек ozon.ru не нужен: без файла (None) или без токенов в нём
    парсер работает как гость. Проверено 06.10.2026: сессия от 25.09 при
    загрузке страницы сбрасывалась в гостевую, а прогоны шли 1200 из 1200.

    :raises SessionError: файл есть, но повреждён (например, Docker подменил
        отсутствующий файл каталогом).
    """
    if not path.exists():
        log.info("Файла сессии %s нет - работаю без входа в аккаунт", path.name)
        return None
    state = read_state(path)
    cookies = state.get("cookies") or []
    if not has_auth_cookies(cookies):
        log.info("В %s нет токенов авторизации - работаю без входа в аккаунт", path.name)
    log.info("Загружено cookies: %s", len(cookies))
    return state


def save_session(context, path: Path) -> int:
    """Сохраняет cookies и localStorage контекста. Возвращает число cookies.

    Внутри действующая сессия Ozon, поэтому файл сразу создаётся с правами
    только для владельца (см. secrets_fs).
    """
    state = context.storage_state()
    secrets_fs.write_private(path, json.dumps(state, ensure_ascii=False, indent=2))
    count = len(state.get("cookies") or [])
    log.info("Cookies сохранены: %s (записей: %s)", path, count)
    return count

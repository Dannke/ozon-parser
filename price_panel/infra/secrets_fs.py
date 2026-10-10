"""Запись файлов с секретами так, чтобы их читал только владелец.

``token.json`` хранит refresh-токен к почте, ``cookies.json`` - действующую
сессию Ozon. Обычный ``write_text`` на Linux-сервере при типичном umask 022
создал бы их с правами 0644, то есть доступными на чтение любому
пользователю машины.

Про Windows: POSIX-права там не действуют, ``os.chmod`` умеет переключить
только флаг "только для чтения". Ограничить доступ по-настоящему можно лишь
через ACL (``icacls``), и это осознанно не делается: на рабочей машине
разработчика файл и так лежит в его профиле. Значение имеет сервер, где
запускается Airflow, - там это полноценные права 0600.
"""

from __future__ import annotations

import os
from pathlib import Path

from price_panel.infra.logger import get_logger

log = get_logger("secrets")

OWNER_ONLY = 0o600


def restrict(path: Path) -> None:
    """Оставляет доступ к файлу только владельцу. Не падает, если не вышло."""
    try:
        os.chmod(path, OWNER_ONLY)
    except OSError as exc:  # чужая файловая система, сетевой диск, Windows ACL
        log.debug("Не удалось ограничить права на %s: %s", path, exc)


def write_private(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Записывает текст в файл, доступный только владельцу.

    Файл сразу создаётся с нужными правами, а не выставляет их после записи:
    иначе остаётся окно, в котором секрет уже на диске и ещё доступен всем.
    Для уже существующего файла режим при открытии игнорируется, поэтому
    права дополнительно выставляются следом.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, OWNER_ONLY)
    with os.fdopen(descriptor, "w", encoding=encoding) as handle:
        handle.write(text)
    restrict(path)

"""Файл сессии и решение «входить ли заново» - то, на что опирается DAG.

Браузер и сеть не нужны: проверяется, в каких случаях get_cookies.py
выходит сразу, а в каких идёт на вход, и что фоновый запуск не ждёт человека.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from ozon_parser import config, login, session
from ozon_parser.gmail import GmailCodeReader, GmailError, GmailSettings


def write_session(path: Path, names=("__Secure-access-token",), age_days: float = 0) -> Path:
    """Пишет правдоподобный cookies.json заданного возраста."""
    payload = {"cookies": [{"name": name, "value": "x", "domain": ".ozon.ru"}
                           for name in names]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


def test_fresh_session_needs_no_login(tmp_path):
    path = write_session(tmp_path / "cookies.json", age_days=3)
    assert session.refresh_reason(path, max_age_days=14) is None
    assert session.is_logged_in(path)


@pytest.mark.parametrize("prepare, expected", [
    (lambda path: None, "не найден"),
    (lambda path: path.write_text("{не json", encoding="utf-8"), "повреждён"),
    (lambda path: write_session(path, names=("some-analytics-cookie",)), "нет токенов"),
    (lambda path: write_session(path, age_days=20), "лимит 14"),
])
def test_broken_or_stale_session_needs_login(tmp_path, prepare, expected):
    """Нет файла, битый JSON, нет токенов, файл старше лимита - всё это повод войти."""
    path = tmp_path / "cookies.json"
    prepare(path)
    reason = session.refresh_reason(path, max_age_days=14)
    assert reason is not None and expected in reason, reason


def test_age_is_checked_only_when_limit_given(tmp_path):
    """Без --max-age-days старая, но целая сессия не перевыпускается."""
    path = write_session(tmp_path / "cookies.json", age_days=100)
    assert session.refresh_reason(path) is None


def test_get_cookies_exits_early_for_fresh_session(tmp_path, monkeypatch):
    """Свежая сессия - выход без браузера и без запроса кода."""
    monkeypatch.setattr(config, "COOKIES_FILE", write_session(tmp_path / "cookies.json"))
    monkeypatch.setattr(login, "sync_playwright", lambda: pytest.fail("браузер не нужен"))
    assert login.run(max_age_days=14, interactive=False) == 0


def test_manual_login_is_refused_in_background(tmp_path, monkeypatch):
    """В фоновом запуске ручной вход невозможен - ошибка, а не вечное ожидание."""
    monkeypatch.setattr(config, "COOKIES_FILE", tmp_path / "cookies.json")
    monkeypatch.setattr(login, "sync_playwright", lambda: pytest.fail("браузер не нужен"))
    assert login.run(manual=True, interactive=False) == 1


def test_gmail_consent_is_not_opened_in_background(tmp_path):
    """Без действующего token.json фоновый запуск падает сразу с подсказкой."""
    reader = GmailCodeReader(GmailSettings(
        credentials_file=tmp_path / "credentials.json",
        token_file=tmp_path / "token.json",
        interactive=False,
    ))
    with pytest.raises(GmailError, match="token.json"):
        reader.connect()

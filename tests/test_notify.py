"""Оповещения о ежедневном прогоне: когда пишем в Telegram и куда уходит пульс.

Сеть не нужна: вместо urllib.request.urlopen - объект, который записывает
запросы. Настройки задаются в тесте (conftest.py выключает их из .env).
"""

from __future__ import annotations

import email.message
import logging
import urllib.error
import urllib.parse
from typing import Optional

import pytest

from ozon_parser import config, notify

TOKEN = "123456:SECRET-TOKEN"


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class Recorder:
    """urlopen, который ничего не отправляет: запоминает запросы."""

    def __init__(self, fail_with: Optional[Exception] = None):
        self.requests: list = []
        self.fail_with = fail_with

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        return Response()


class Messages(logging.Handler):
    """Сообщения лога notify (у логгеров проекта propagate=False - caplog их не видит)."""

    def __init__(self):
        super().__init__()
        self.lines: list = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(config, "HEALTHCHECK_URL", "https://hc-ping.com/uuid")


def test_nothing_is_sent_when_not_configured():
    opener = Recorder()
    notify.report_job(3, False, opener=opener)
    assert opener.requests == []


def test_success_pings_and_stays_silent(configured):
    opener = Recorder()
    notify.report_job(0, True, summary=lambda: pytest.fail("сводка не нужна"), opener=opener)
    assert [request.full_url for request in opener.requests] == ["https://hc-ping.com/uuid"]


def test_problems_ping_fail_and_go_to_telegram(configured):
    opener = Recorder()
    notify.report_job(3, False, summary=lambda: "Прогон 12 (daily): blocked, успешно 348 из 1200",
                      opener=opener)

    ping, message = opener.requests
    assert ping.full_url == "https://hc-ping.com/uuid/fail"
    assert message.full_url.endswith("/sendMessage") and message.get_method() == "POST"
    assert isinstance(message.data, bytes)
    form = urllib.parse.parse_qs(message.data.decode("utf-8"))
    assert form["chat_id"] == ["42"]
    text = form["text"][0]
    assert "капча" in text and "копия базы не создана" in text and "348 из 1200" in text


def test_failures_are_logged_without_secrets(configured):
    error = urllib.error.HTTPError("https://api.telegram.org/bot" + TOKEN + "/sendMessage",
                                   401, "Unauthorized", email.message.Message(), None)
    opener = Recorder(fail_with=error)
    messages = Messages()
    notify.log.addHandler(messages)
    try:
        assert notify.send_telegram("тест", opener=opener) is False
        assert notify.ping(True, opener=Recorder(fail_with=TimeoutError())) is False
    finally:
        notify.log.removeHandler(messages)

    text = "\n".join(messages.lines)
    assert "HTTP 401" in text and "TimeoutError" in text
    assert TOKEN not in text and "hc-ping" not in text


def test_healthcheck_url_must_be_http(monkeypatch):
    monkeypatch.setattr(config, "HEALTHCHECK_URL", "file:///etc/passwd")
    opener = Recorder()
    assert notify.ping(True, opener=opener) is False
    assert opener.requests == []

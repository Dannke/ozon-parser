"""Проверки разбора письма с кодом подтверждения.

Сети не требуют: `extract_code` и сборка текста письма - чистые функции, а
опрос почты проверяется на заглушке Gmail API. Цена ошибки здесь высокая:
неверно распознанный код сжигает попытку входа, а Ozon их считает.

    pytest test_gmail.py        (или: python test_gmail.py)
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from price_panel import gmail
from price_panel.gmail import GmailCodeReader, GmailError, GmailSettings

extract_code = GmailCodeReader.extract_code
collect_text = GmailCodeReader._collect_text


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def _part(mime_type: str, text: str) -> dict:
    return {"mimeType": mime_type, "body": {"data": _b64(text)}}


def _message(internal_ms: int, subject: str, text: str) -> dict:
    return {
        "internalDate": str(internal_ms),
        "snippet": text[:60],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "noreply@ozon.ru"},
            ],
            "body": {"data": _b64(text)},
        },
    }


# ------------------------------------------------------------ извлечение ---
def test_code_next_to_keyword():
    """Число рядом с ключевым словом принимается из любого места письма."""
    assert extract_code("Здравствуйте! Ваш код подтверждения: 483712") == "483712"
    assert extract_code("Your code is 4837") == "4837"
    assert extract_code("483712 - ваш код для входа") == "483712"
    assert extract_code("PIN 246810 действует 5 минут") == "246810"


def test_bare_number_taken_only_from_headline():
    """Просто число - код лишь в теме и сниппете, но не в теле письма.

    Регрессия: запасной шаблон применялся ко всему письму и с равным успехом
    уносил номер заказа, сумму или год.
    """
    body = "Заказ 20260923 доставлен, к оплате 1499 рублей"
    assert extract_code(body) is None
    assert extract_code(body, headline="483712 Ozon") == "483712"


def test_css_and_script_numbers_are_ignored():
    """Содержимое <style> и <script> вырезается вместе с тегами.

    Регрессия: снимались только теги, и в CSS письма чисел оставалось больше,
    чем в самом тексте - оттуда и бралось первое попавшееся четырёхзначное.
    """
    html = (
        "<html><head>"
        "<style>.promo{font-size:1499px;width:4837px;color:#123456}</style>"
        "<script>var trackingId = 998877;</script>"
        "</head><body><p>Ваш код: 246810</p></body></html>"
    )
    assert extract_code(html) == "246810"


def test_css_numbers_do_not_become_a_code():
    """Письмо без кода остаётся письмом без кода, даже если в вёрстке есть числа."""
    html = "<style>.x{width:4837px;height:1200px}</style><p>Спасибо за заказ</p>"
    assert extract_code(html, headline="Ozon: спасибо за заказ") is None


def test_html_entities_do_not_glue_numbers():
    """Сущности превращаются в пробел, а не склеивают соседние числа."""
    assert extract_code("код:&nbsp;4837") == "4837"


def test_no_code_in_empty_text():
    assert extract_code("") is None
    assert extract_code("Письмо совсем без цифр") is None


# ---------------------------------------------------------- сборка текста --
def test_plain_text_part_is_preferred():
    """При наличии text/plain HTML-версия не берётся.

    Раньше склеивались обе, и в поиск кода уезжала вся разметка - вдвое
    больше шума при том же содержании.
    """
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            _part("text/plain", "Ваш код: 483712"),
            _part("text/html", "<p>Ваш код: 999999</p>"),
        ],
    }
    text = collect_text(payload)
    assert "483712" in text
    assert "999999" not in text


def test_html_used_when_no_plain_part():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [_part("text/html", "<p>Ваш код: 999999</p>")],
    }
    assert "999999" in collect_text(payload)


def test_falls_back_to_any_part_without_mime_type():
    """Письмо без заявленного MIME-типа всё равно читается."""
    payload = {"body": {"data": _b64("Ваш код: 135790")}}
    assert "135790" in collect_text(payload)


# ------------------------------------------------------ временные ошибки ---
def test_transient_statuses_recognised():
    class _Response:
        def __init__(self, status):
            self.status = status

    class _Error(Exception):
        def __init__(self, status):
            self.resp = _Response(status)

    assert gmail._status_of(_Error(429)) == 429
    assert gmail._status_of(Exception()) == 0
    assert 503 in gmail.TRANSIENT_STATUSES
    assert 404 not in gmail.TRANSIENT_STATUSES


# --------------------------------------------------------- опрос почты -----
class _Executable:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class FakeMessages:
    """Заглушка users().messages(): считает обращения к API."""

    def __init__(self, metas, bodies):
        self.metas = metas
        self.bodies = bodies
        self.list_calls = 0
        self.get_calls = []

    def list(self, **kwargs):
        self.list_calls += 1
        return _Executable({"messages": self.metas})

    def get(self, userId=None, id=None, format=None):  # noqa: A002 - сигнатура API
        self.get_calls.append(id)
        return _Executable(self.bodies[id])


class FakeService:
    def __init__(self, messages):
        self._messages = messages

    def users(self):
        return self

    def messages(self):
        return self._messages


def _reader(fake, **overrides):
    settings = GmailSettings(
        credentials_file=Path("credentials.json"),
        token_file=Path("token.json"),
        poll_interval=overrides.get("poll_interval", 1),
        wait_timeout=overrides.get("wait_timeout", 2),
    )
    reader = GmailCodeReader(settings)
    reader._service = FakeService(fake)  # connect() увидит готовый клиент
    return reader


def test_checked_messages_are_not_refetched():
    """Письмо без кода читается один раз, а не на каждой итерации опроса.

    Регрессия: каждая итерация заново тянула тела всех найденных писем, и за
    три минуты ожидания набегало до 360 полных выборок.
    """
    now_ms = int(time.time() * 1000)
    metas = [{"id": "a"}, {"id": "b"}]
    bodies = {
        "a": _message(now_ms, "Рассылка", "Скидки до 50 процентов"),
        "b": _message(now_ms, "Новости", "Ничего интересного"),
    }
    fake = FakeMessages(metas, bodies)

    try:
        _reader(fake).wait_for_code(since_ts=time.time() - 10, timeout=2)
    except GmailError:
        pass  # кода в письмах нет - так и задумано

    assert fake.list_calls > 1, "опрос должен был повториться хотя бы дважды"
    assert fake.get_calls == ["a", "b"], fake.get_calls


def test_code_found_in_fresh_message():
    now_ms = int(time.time() * 1000)
    fake = FakeMessages([{"id": "a"}], {"a": _message(now_ms, "Вход в Ozon", "Ваш код: 483712")})
    assert _reader(fake).wait_for_code(since_ts=time.time() - 10, timeout=5) == "483712"


def test_old_message_is_skipped():
    """Письмо, пришедшее ДО запроса кода, не годится: код в нём уже протух."""
    old_ms = int((time.time() - 3600) * 1000)
    fake = FakeMessages([{"id": "a"}], {"a": _message(old_ms, "Вход в Ozon", "Ваш код: 483712")})
    try:
        _reader(fake).wait_for_code(since_ts=time.time(), timeout=2)
    except GmailError:
        return
    raise AssertionError("старое письмо не должно было подойти")

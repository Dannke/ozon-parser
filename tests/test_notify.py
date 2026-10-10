"""Оповещения о ежедневном прогоне: что пишем в Telegram и куда уходит пульс.

Сеть не нужна: вместо urllib.request.urlopen - объект, который записывает
запросы. Настройки задаются в тесте (conftest.py выключает их из .env).
"""

from __future__ import annotations

import email.message
import io
import json
import logging
import urllib.error
import urllib.parse

import pytest

from price_panel import config, notify

TOKEN = "123456:SECRET-TOKEN"


class Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class Recorder:
    """urlopen, который ничего не отправляет: запоминает запросы."""

    def __init__(self, fail_with: Exception | None = None):
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


@pytest.fixture(autouse=True)
def outbox(tmp_path, monkeypatch):
    """Очередь задержанных сообщений - во временном каталоге, не в data/."""
    path = tmp_path / "telegram_outbox.json"
    monkeypatch.setattr(notify, "OUTBOX_FILE", path)
    return path


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(config, "HEALTHCHECK_URL", "https://hc-ping.com/uuid")


def sent_text(request) -> str:
    """Текст сообщения из запроса sendMessage."""
    assert request.full_url.endswith("/sendMessage") and request.get_method() == "POST"
    form = urllib.parse.parse_qs(request.data.decode("utf-8"))
    assert form["chat_id"] == ["42"]
    return form["text"][0]


def test_nothing_is_sent_when_not_configured():
    """Без настроек ни запросов, ни обращений к базе за подробностями."""
    opener = Recorder()

    def no_db() -> str:
        pytest.fail("подробности без Telegram не нужны")

    notify.report_start(details=no_db, opener=opener)
    notify.report_retry("16:00 MSK", 1, 1, details=no_db, opener=opener)
    notify.report_job(3, False, summary=no_db, opener=opener)
    notify.report_job(0, True, summary=no_db, opener=opener)
    notify.report_failure("PostgreSQL: нет соединения", opener=opener)
    assert opener.requests == []


def test_start_is_reported(configured):
    opener = Recorder()
    notify.report_start(details=lambda: "К сбору 1200 из 1200 SKU panel", opener=opener)
    (message,) = opener.requests  # пульс - только по итогу прогона
    text = sent_text(message)
    assert "начался ежедневный сбор" in text and "1200 из 1200" in text


def test_retry_after_block_is_reported(configured):
    opener = Recorder()
    notify.report_retry(
        "16:00 MSK", 1, 2, details=lambda: "За сегодня собрано 348 из 1200", opener=opener
    )
    (message,) = opener.requests
    text = sent_text(message)
    assert "капча" in text and "Повтор 1 из 2 в 16:00 MSK" in text and "348 из 1200" in text


def test_success_pings_and_reports_to_telegram(configured):
    opener = Recorder()
    notify.report_job(
        0, True, summary=lambda: "За сегодня собрано 1200 из 1200 SKU panel", opener=opener
    )

    ping, message = opener.requests
    assert ping.full_url == "https://hc-ping.com/uuid"
    text = sent_text(message)
    assert "сбор завершён" in text and "1200 из 1200" in text
    assert "копия базы создана" in text and "Подробности" not in text


def test_problems_ping_fail_and_go_to_telegram(configured):
    opener = Recorder()
    notify.report_job(
        3, False, summary=lambda: "Прогон 12 (daily): blocked, успешно 348 из 1200", opener=opener
    )

    ping, message = opener.requests
    assert ping.full_url == "https://hc-ping.com/uuid/fail"
    assert message.full_url.endswith("/sendMessage") and message.get_method() == "POST"
    assert isinstance(message.data, bytes)
    form = urllib.parse.parse_qs(message.data.decode("utf-8"))
    assert form["chat_id"] == ["42"]
    text = form["text"][0]
    assert "капча" in text and "копия базы не создана" in text and "348 из 1200" in text


def test_failure_before_the_run_is_reported_without_db_password(configured):
    opener = Recorder()
    notify.report_failure(
        "PostgreSQL: invalid dsn: postgresql://ozon:s3cret@127.0.0.1:5433/ozon (password=s3cret)",
        opener=opener,
    )

    ping, message = opener.requests
    assert ping.full_url == "https://hc-ping.com/uuid/fail"
    text = sent_text(message)
    assert "сбор не начался" in text and "postgresql://ozon:***@127.0.0.1" in text
    assert "s3cret" not in text


def test_failures_are_logged_without_secrets(configured):
    body = io.BytesIO(
        json.dumps(
            {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        ).encode()
    )
    error = urllib.error.HTTPError(
        "https://api.telegram.org/bot" + TOKEN + "/sendMessage",
        400,
        "Bad Request",
        email.message.Message(),
        body,
    )
    opener = Recorder(fail_with=error)
    messages = Messages()
    notify.log.addHandler(messages)
    try:
        assert notify.send_telegram("тест", opener=opener) is False
        assert notify.ping(True, opener=Recorder(fail_with=TimeoutError())) is False
    finally:
        notify.log.removeHandler(messages)

    text = "\n".join(messages.lines)
    # Причина из ответа Telegram - чтобы при настройке было понятно, что не так.
    assert "HTTP 400 (Bad Request: chat not found)" in text and "TimeoutError" in text
    assert TOKEN not in text and "hc-ping" not in text


class JsonResponse(Response):
    def __init__(self, payload: dict):
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_find_chats_lists_who_wrote_to_the_bot(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    updates = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": 42, "type": "private", "first_name": "Никита", "username": "nik"}
                },
            },
            {"update_id": 2, "message": {"chat": {"id": 42, "type": "private", "username": "nik"}}},
            {
                "update_id": 3,
                "my_chat_member": {"chat": {"id": -100500, "type": "group", "title": "Цены Ozon"}},
            },
        ],
    }
    requests: list = []

    def opener(request, timeout):
        requests.append(request)
        return JsonResponse(updates)

    assert notify.find_chats(opener=opener) == [("42", "nik"), ("-100500", "Цены Ozon")]
    assert requests[0].full_url.endswith("/getUpdates")


def test_find_chats_reports_failure(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    assert notify.find_chats(opener=Recorder(fail_with=TimeoutError())) is None


def test_healthcheck_url_must_be_http(monkeypatch):
    monkeypatch.setattr(config, "HEALTHCHECK_URL", "file:///etc/passwd")
    opener = Recorder()
    assert notify.ping(True, opener=opener) is False
    assert opener.requests == []


def run_notify_command(monkeypatch, capsys) -> tuple:
    """python -m price_panel notify без config.yaml и базы: (код, вывод)."""
    from price_panel import __main__ as cli

    monkeypatch.setattr(cli, "load_settings", lambda path: None)
    code = cli.main(["notify"])
    return code, capsys.readouterr().out


def test_notify_command_without_token_explains_setup(monkeypatch, capsys):
    code, out = run_notify_command(monkeypatch, capsys)
    assert code == 1 and "TELEGRAM_BOT_TOKEN" in out


def test_notify_command_finds_chat_id(monkeypatch, capsys):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(notify, "find_chats", lambda: [("42", "nik")])
    code, out = run_notify_command(monkeypatch, capsys)
    assert code == 0 and "42" in out and "TELEGRAM_CHAT_ID" in out


def test_notify_command_sends_test_message(monkeypatch, capsys, configured):
    sent: list = []
    monkeypatch.setattr(
        notify, "send_telegram", lambda text, queue: sent.append((text, queue)) or True
    )
    code, out = run_notify_command(monkeypatch, capsys)
    assert code == 0 and "отправлено" in out
    # Проверочное сообщение в очередь не встаёт: результат виден сразу.
    assert len(sent) == 1 and sent[0][1] is False


# ---------------------------------------------------- нет связи с Telegram --
class Flaky(Recorder):
    """urlopen, где первые fail_times отправок в Telegram падают с ошибкой сети;
    пульс healthcheck проходит."""

    def __init__(self, fail_times: int, error: Exception | None = None):
        super().__init__()
        self.fail_times = fail_times
        self.error = error or urllib.error.URLError(TimeoutError("timed out"))

    def __call__(self, request, timeout):
        self.requests.append(request)
        if "/sendMessage" in request.full_url:
            self.fail_times -= 1
            if self.fail_times >= 0:
                raise self.error
        return Response()


def telegram_texts(opener) -> list:
    return [sent_text(r) for r in opener.requests if "/sendMessage" in r.full_url]


def no_sleep(seconds):
    pytest.fail("промежуточное сообщение не должно ждать сеть")


def test_unsent_message_waits_in_outbox_and_goes_first_next_time(configured, outbox, monkeypatch):
    """08.10.2026: без VPN сообщение о начале сбора пропало - теперь оно ждёт."""
    monkeypatch.setattr(notify, "_clock", lambda: 1_000_000.0)
    notify.report_start(
        details=lambda: "К сбору 1200 из 1200 SKU panel", opener=Flaky(fail_times=1)
    )
    (entry,) = json.loads(outbox.read_text(encoding="utf-8"))
    assert "начался ежедневный сбор" in entry["text"]

    # Через 2 ч связь есть: сначала задержанное (с пометкой), потом новое.
    monkeypatch.setattr(notify, "_clock", lambda: 1_000_000.0 + 7200)
    opener = Recorder()
    notify.report_job(
        0,
        None,
        summary=lambda: "За сегодня собрано 1200 из 1200 SKU panel",
        opener=opener,
        sleep=no_sleep,
    )
    late, result = telegram_texts(opener)
    assert late.startswith("⏳ Задержано: должно было прийти ")
    assert "начался ежедневный сбор" in late
    assert result.startswith("✅") and "Задержано" not in result
    assert not outbox.exists()


def test_final_message_retries_while_network_is_down(configured, outbox):
    slept: list = []
    opener = Flaky(fail_times=2)
    notify.report_job(0, True, opener=opener, sleep=slept.append)
    assert slept == list(notify.FINAL_RETRY_DELAYS[:2])
    assert telegram_texts(opener)[-1].startswith("✅")
    assert not outbox.exists()


def test_final_message_stays_queued_after_all_retries(configured, outbox):
    slept: list = []
    notify.report_failure(
        "PostgreSQL: нет соединения", opener=Flaky(fail_times=99), sleep=slept.append
    )
    assert slept == list(notify.FINAL_RETRY_DELAYS)
    (entry,) = json.loads(outbox.read_text(encoding="utf-8"))
    assert "сбор не начался" in entry["text"]


def test_final_message_survives_a_killed_process(configured, outbox):
    """10.10.2026: процесс прервали между повторами - итог не должен пропасть."""

    def killed(seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        notify.report_job(1, True, opener=Flaky(fail_times=99), sleep=killed)
    (entry,) = json.loads(outbox.read_text(encoding="utf-8"))
    assert entry["text"].startswith("❌")


def test_rejected_message_is_neither_retried_nor_queued(configured, outbox):
    """HTTP 4xx (неверный чат, токен) - повторять бессмысленно; очередь не застревает."""
    error = urllib.error.HTTPError(
        "https://api.telegram.org/", 400, "Bad Request", email.message.Message(), None
    )
    notify.report_job(0, None, opener=Recorder(fail_with=error), sleep=no_sleep)
    assert not outbox.exists()


def test_telegram_overload_is_retried(configured, outbox):
    error = urllib.error.HTTPError(
        "https://api.telegram.org/", 429, "Too Many Requests", email.message.Message(), None
    )
    slept: list = []
    opener = Flaky(fail_times=1, error=error)
    notify.report_job(0, None, opener=opener, sleep=slept.append)
    assert slept == [notify.FINAL_RETRY_DELAYS[0]] and len(telegram_texts(opener)) == 2


def test_outbox_drops_stale_and_extra_messages(configured, outbox, monkeypatch):
    now = 10_000_000.0
    monkeypatch.setattr(notify, "_clock", lambda: now)
    stale = {"created": now - notify.OUTBOX_MAX_AGE_SECONDS - 1, "text": "старое"}
    fresh = [
        {"created": now - 60 + i, "text": "сообщение {}".format(i)}
        for i in range(notify.OUTBOX_LIMIT)
    ]
    outbox.write_text(json.dumps([stale, "мусор", *fresh]), encoding="utf-8")
    assert notify.load_outbox() == fresh

    notify.send_telegram("новое", opener=Flaky(fail_times=99))
    kept = json.loads(outbox.read_text(encoding="utf-8"))
    assert len(kept) == notify.OUTBOX_LIMIT and kept[-1]["text"] == "новое"
    assert kept[0]["text"] == "сообщение 1"  # самое старое вытеснено


def test_broken_outbox_file_does_not_stop_sending(configured, outbox):
    outbox.write_text("{не json", encoding="utf-8")
    opener = Recorder()
    assert notify.send_telegram("тест", opener=opener) is True
    assert len(opener.requests) == 1


def test_network_failure_reason_is_logged(configured):
    messages = Messages()
    notify.log.addHandler(messages)
    try:
        notify.send_telegram("тест", opener=Flaky(fail_times=1))
    finally:
        notify.log.removeHandler(messages)
    assert any("URLError: timed out" in line for line in messages.lines)
    assert all(TOKEN not in line for line in messages.lines)


def test_notify_command_delivers_queued_messages(monkeypatch, capsys, configured, outbox):
    outbox.write_text(
        json.dumps([{"created": notify._clock() - 3600, "text": "итог"}]), encoding="utf-8"
    )
    requests: list = []

    def opener(request, timeout):
        requests.append(request)
        return Response()

    send = notify.send_telegram
    monkeypatch.setattr(
        notify, "send_telegram", lambda text, queue: send(text, opener=opener, queue=queue)
    )
    code, out = run_notify_command(monkeypatch, capsys)
    assert code == 0 and "задержанные сообщения из очереди: 1" in out
    assert [sent_text(r).splitlines()[-1] for r in requests] == [
        "итог",
        "✅ Price panel: оповещения настроены, бот на связи",
    ]
    assert not outbox.exists()

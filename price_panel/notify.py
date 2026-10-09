"""Оповещения о ежедневном прогоне: сообщения в Telegram и «пульс» мониторинга.

Telegram получает сообщение на каждом этапе ежедневного сбора:

    начало   - сколько SKU panel осталось собрать за день;
    повтор   - Ozon остановил прогон капчей, когда будет повтор;
    итог     - сколько собрано за день, итог прогона, ошибки по типам, копия базы;
    сбой     - сбор не начался: база недоступна, сломан config.yaml.

Сообщение, которое не ушло из-за сети, не теряется: оно ждёт в очереди
OUTBOX_FILE и уходит перед следующим сообщением с пометкой «задержано».
08.10.2026 так пропало сообщение о начале сбора: api.telegram.org не отвечал,
пока после перезагрузки компьютера не подключили VPN. Итог и сбой - последние
сообщения прогона, после них в этот день писать некому, поэтому они повторяют
попытки около получаса (FINAL_RETRY_DELAYS). Отказ самого Telegram (HTTP 4xx,
кроме 429: неверный токен или чат) не повторяется - причина в логе.

Пульс (healthchecks.io и подобные сервисы) уходит после каждого прогона:
успех - на HEALTHCHECK_URL, неудача - на HEALTHCHECK_URL + "/fail". Если пульса
нет дольше расписания (компьютер был выключен или спал, процесс убит), сервис
сообщит сам: изнутри такого прогона сообщение не отправить.

Оба канала выключены, пока в .env не заданы TELEGRAM_BOT_TOKEN и
TELEGRAM_CHAT_ID, HEALTHCHECK_URL. Сбой оповещения пишется в лог и не меняет
итог прогона. Токен бота и адреса в лог не попадают, пароли из DSN в текст
сообщения - тоже.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

from . import config
from .logger import get_logger
from .pipeline import EXIT_BLOCKED

log = get_logger("notify")

TIMEOUT_SECONDS = 15
TELEGRAM_URL = "https://api.telegram.org/bot{token}/{method}"
# Предел Telegram - 4096 символов; сообщения прогона короче, это страховка от
# длинного текста ошибки.
MAX_MESSAGE_CHARS = 3500
TITLE = "Price panel"

# Очередь сообщений, не ушедших из-за сети (каталог data/ не попадает в git).
OUTBOX_FILE = config.BASE_DIR / "data" / "telegram_outbox.json"
# В очереди - не больше OUTBOX_LIMIT последних сообщений и не старше
# OUTBOX_MAX_AGE_SECONDS: итог прогона недельной давности уже не нужен.
OUTBOX_LIMIT = 20
OUTBOX_MAX_AGE_SECONDS = 3 * 24 * 3600
# Сообщение, доставленное позже этого срока, помечается как задержанное.
LATE_AFTER_SECONDS = 120
# Паузы перед повторами итогового сообщения, секунды: всего около 30 минут.
FINAL_RETRY_DELAYS = (60, 180, 600, 900)

# Итог запроса к сервису: доставлено / повторить позже (сеть, 429, 5xx) /
# отказ, повторять бессмысленно (прочие 4xx).
SENT, RETRY, REJECTED = "sent", "retry", "rejected"

# Что значат коды выхода шага parse (см. docs/cli.md, «Коды возврата»).
EXIT_MEANINGS = {
    1: "прогон не удался",
    EXIT_BLOCKED: "Ozon остановил прогон (капча или отказы подряд)",
}

# Пароль в DSN (postgresql://user:пароль@host, password=пароль): текст ошибки
# psycopg2 может содержать строку подключения целиком.
DSN_PASSWORD_RES = [
    (re.compile(r"(://[^:/@\s]+:)[^@\s]+@"), r"\1***@"),
    (re.compile(r"(password\s*=\s*)\S+", re.IGNORECASE), r"\1***"),
]

Details = Callable[[], str]

# Часы для очереди; тесты подменяют.
_clock: Callable[[], float] = time.time


def _no_details() -> str:
    return ""


def _hide_token(text: str) -> str:
    token = config.TELEGRAM_BOT_TOKEN
    return text.replace(token, "***") if token else text


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    """«HTTP 400 (Bad Request: chat not found)» - причина из ответа Telegram."""
    text = "HTTP {}".format(exc.code)
    try:
        description = json.loads(exc.read().decode("utf-8")).get("description")
    except (ValueError, OSError, AttributeError):
        description = None
    return "{} ({})".format(text, description) if description else text


def _network_error_text(exc: OSError) -> str:
    """«URLError: timed out» - тип ошибки и причина (адреса в ней нет)."""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    detail = _hide_token(str(reason).strip())
    return "{}: {}".format(type(exc).__name__, detail) if detail else type(exc).__name__


def _request(request: urllib.request.Request, opener: Callable) -> str:
    """Выполняет запрос: SENT, RETRY или REJECTED."""
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return SENT if 200 <= getattr(response, "status", 200) < 300 else REJECTED
    except urllib.error.HTTPError as exc:
        log.warning("Оповещение не отправлено: %s", _http_error_text(exc))
        return RETRY if exc.code == 429 or exc.code >= 500 else REJECTED
    except OSError as exc:  # нет сети, таймаут, DNS (URLError - тоже OSError)
        log.warning("Оповещение не отправлено: %s", _network_error_text(exc))
        return RETRY


def redact(text: str) -> str:
    """Текст без паролей из строк подключения."""
    for pattern, replacement in DSN_PASSWORD_RES:
        text = pattern.sub(replacement, text)
    return text


def telegram_enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


# ------------------------------------------------------------------ очередь --
def _valid_entry(entry, now: float) -> bool:
    try:
        return (isinstance(entry.get("text"), str)
                and now - float(entry["created"]) <= OUTBOX_MAX_AGE_SECONDS)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def load_outbox() -> list:
    """Задержанные сообщения: [{"created": время, "text": текст}], старые - первыми."""
    try:
        entries = json.loads(OUTBOX_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("Очередь оповещений не прочитана (%s) - начинаю новую", type(exc).__name__)
        return []
    if not isinstance(entries, list):
        return []
    now = _clock()
    return [entry for entry in entries if _valid_entry(entry, now)]


def _save_outbox(entries: list) -> None:
    if len(entries) > OUTBOX_LIMIT:
        log.warning("Очередь оповещений переполнена: самые старые %s сообщений отброшены",
                    len(entries) - OUTBOX_LIMIT)
        entries = entries[-OUTBOX_LIMIT:]
    try:
        if not entries:
            if OUTBOX_FILE.exists():
                OUTBOX_FILE.unlink()
            return
        OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = OUTBOX_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=1),
                             encoding="utf-8")
        temporary.replace(OUTBOX_FILE)
    except OSError as exc:
        log.warning("Очередь оповещений не сохранена: %s", type(exc).__name__)


def _post(entry: dict, opener: Callable) -> str:
    text = entry["text"]
    late = _clock() - float(entry["created"]) > LATE_AFTER_SECONDS
    if late:
        text = "⏳ Задержано: должно было прийти {}\n{}".format(
            time.strftime("%d.%m в %H:%M", time.localtime(float(entry["created"]))), text)
    data = urllib.parse.urlencode({"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                                   "disable_web_page_preview": "true"}).encode("utf-8")
    request = urllib.request.Request(
        TELEGRAM_URL.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage"),
        data=data, method="POST")
    result = _request(request, opener)
    if result == SENT:
        log.info("%s в Telegram: %s", "Доставлено задержанное сообщение" if late
                 else "Сообщение отправлено", entry["text"].splitlines()[0])
    return result


def _deliver(pending: list, opener: Callable) -> tuple:
    """Отправляет по порядку до первого сбоя сети: (неотправленные, доставленные).

    Сообщение, которое Telegram отверг (4xx), из очереди убирается: иначе оно
    навсегда загородило бы все следующие.
    """
    delivered = []
    for index, entry in enumerate(pending):
        result = _post(entry, opener)
        if result == RETRY:
            return pending[index:], delivered
        if result == SENT:
            delivered.append(entry)
    return [], delivered


def send_telegram(text: str, opener: Callable = urllib.request.urlopen,
                  retry_delays: Sequence[float] = (), sleep: Callable = time.sleep,
                  queue: bool = True) -> bool:
    """Сообщение в чат TELEGRAM_CHAT_ID, а перед ним - задержанные из очереди.

    :param retry_delays: паузы перед повторами, если нет связи; пусто - одна попытка.
    :param queue: не ушедшее из-за сети сообщение оставить в очереди до
        следующей отправки (проверочное сообщение команды notify - не нужно).
    :returns: True - сообщение text доставлено.
    """
    if not telegram_enabled():
        return False
    text = redact(text)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS - 1] + "…"
    entry = {"created": _clock(), "text": text}
    pending = load_outbox() + [entry]
    if queue:
        # Сначала на диск: 10.10.2026 процесс прервали между повторами, и итог
        # дня пропал вместе с ним. Из очереди сообщение уберёт доставка.
        _save_outbox(pending)
    delivered: list = []
    for delay in (0, *retry_delays):
        if delay:
            log.info("Telegram недоступен - повтор через %.0f с", delay)
            sleep(delay)
        pending, sent = _deliver(pending, opener)
        delivered += sent
        if not pending:
            break
    if not queue:
        pending = [item for item in pending if item is not entry]
    if pending:
        log.warning("Telegram недоступен: в очереди %s сообщ., уйдут со следующим оповещением",
                    len(pending))
    _save_outbox(pending)
    return any(item is entry for item in delivered)


def _send(headline: str, details: Details, footer: str = "",
          opener: Callable = urllib.request.urlopen, final: bool = False,
          sleep: Callable = time.sleep) -> None:
    """Сообщение из заголовка и подробностей; details вызывается, только если
    Telegram настроен, - иначе прогон не ходит в базу ради несуществующего письма.

    :param final: последнее сообщение прогона - повторять попытки FINAL_RETRY_DELAYS.
    """
    if not telegram_enabled():
        return
    lines = [headline]
    extra = details()
    if extra:
        lines.append(extra)
    if footer:
        lines.append(footer)
    send_telegram("\n".join(lines), opener,
                  retry_delays=FINAL_RETRY_DELAYS if final else (), sleep=sleep)


def ping(ok: bool, opener: Callable = urllib.request.urlopen) -> bool:
    """Пульс в сервис мониторинга. False - не настроено или не отправлено."""
    url = config.HEALTHCHECK_URL
    if not url:
        return False
    if not url.startswith(("https://", "http://")):
        log.warning("HEALTHCHECK_URL должен начинаться с https:// - пульс не отправлен")
        return False
    request = urllib.request.Request(url.rstrip("/") + ("" if ok else "/fail"))
    return _request(request, opener) == SENT


def job_problems(parse_code: int, backup_ok: bool | None) -> list:
    """Что пошло не так в ежедневном прогоне; пустой список - всё в порядке.

    :param backup_ok: None - копия не снималась (выключена в config.yaml).
    """
    problems = []
    if parse_code != 0:
        problems.append(EXIT_MEANINGS.get(
            parse_code, "шаг parse завершился с кодом {}".format(parse_code)))
    if backup_ok is False:
        problems.append("резервная копия базы не создана")
    return problems


def report_start(details: Details = _no_details,
                 opener: Callable = urllib.request.urlopen) -> None:
    """Начало ежедневного сбора. Не ждёт сети: прогон не откладывается ради
    сообщения, не ушедшее уйдёт вместе со следующим.

    :param details: сколько SKU предстоит собрать (строка из базы).
    """
    _send("▶️ {}: начался ежедневный сбор".format(TITLE), details, opener=opener)


def report_retry(retry_at: str, attempt: int, attempts: int, details: Details = _no_details,
                 opener: Callable = urllib.request.urlopen) -> None:
    """Ozon остановил прогон, повтор по недостающим SKU назначен на retry_at."""
    _send("⚠️ {}: {}. Повтор {} из {} в {} - только несобранные SKU".format(
        TITLE, EXIT_MEANINGS[EXIT_BLOCKED], attempt, attempts, retry_at), details,
        opener=opener)


def report_job(parse_code: int, backup_ok: bool | None, summary: Details = _no_details,
               opener: Callable = urllib.request.urlopen, sleep: Callable = time.sleep) -> None:
    """Итог ежедневного прогона: пульс и сообщение в Telegram.

    :param summary: итог дня из базы; вызывается, только если сообщение уходит.
    """
    problems = job_problems(parse_code, backup_ok)
    ping(not problems, opener)
    if problems:
        _send("❌ {}: {}".format(TITLE, "; ".join(problems)), summary,
              footer="Подробности: python -m price_panel runs, logs/scheduler.log",
              opener=opener, final=True, sleep=sleep)
        return

    def details() -> str:
        lines = [summary()]
        if backup_ok:
            lines.append("Резервная копия базы создана")
        return "\n".join(line for line in lines if line)

    _send("✅ {}: ежедневный сбор завершён".format(TITLE), details, opener=opener,
          final=True, sleep=sleep)


def report_failure(problem: str, opener: Callable = urllib.request.urlopen,
                   sleep: Callable = time.sleep) -> None:
    """Ежедневный сбор не начался (база недоступна, ошибка в config.yaml)."""
    ping(False, opener)
    _send("❌ {}: ежедневный сбор не начался".format(TITLE), lambda: problem,
          footer="Подробности: logs/pipeline.log", opener=opener, final=True, sleep=sleep)


def find_chats(opener: Callable = urllib.request.urlopen) -> list | None:
    """Чаты, которые недавно писали боту: [(chat_id, название)].

    Нужен, чтобы узнать TELEGRAM_CHAT_ID: Telegram отдаёт обновления бота за
    последние сутки. None - запрос не удался (причина в логе).
    """
    url = TELEGRAM_URL.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates")
    try:
        with opener(urllib.request.Request(url), timeout=TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        log.warning("Обновления бота не получены: %s", _http_error_text(exc))
        return None
    except OSError as exc:
        log.warning("Обновления бота не получены: %s", _network_error_text(exc))
        return None
    except ValueError as exc:
        log.warning("Обновления бота не получены: %s", type(exc).__name__)
        return None

    chats: dict = {}
    for update in payload.get("result", []):
        for key in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = (update.get(key) or {}).get("chat")
            if chat and "id" in chat:
                name = (chat.get("title") or chat.get("username")
                        or " ".join(filter(None, [chat.get("first_name"),
                                                  chat.get("last_name")])))
                chats[str(chat["id"])] = name or chat.get("type", "")
    return list(chats.items())

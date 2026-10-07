"""Оповещения о ежедневном прогоне: сообщение в Telegram и «пульс» мониторинга.

Telegram пишет только о проблемах - прогон не удался или резервная копия не
снята, - чтобы сообщения не превратились в шум. Пульс (healthchecks.io и
подобные сервисы) уходит после каждого прогона: успех - на HEALTHCHECK_URL,
неудача - на HEALTHCHECK_URL + "/fail". Если пульса нет дольше расписания
(компьютер был выключен или спал), сервис сообщит сам: из выключенного
компьютера сообщение не отправить.

Оба канала выключены, пока в .env не заданы TELEGRAM_BOT_TOKEN и
TELEGRAM_CHAT_ID, HEALTHCHECK_URL. Сбой оповещения пишется в лог и не меняет
итог прогона. Токен бота и адреса в лог не попадают.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from . import config
from .logger import get_logger
from .pipeline import EXIT_BLOCKED

log = get_logger("notify")

TIMEOUT_SECONDS = 15
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Что значат коды выхода шага parse (см. docs/cli.md, «Коды возврата»).
EXIT_MEANINGS = {
    1: "прогон не удался",
    EXIT_BLOCKED: "Ozon остановил прогон (капча или отказы подряд)",
}


def _open(request: urllib.request.Request, opener: Callable) -> bool:
    """Выполняет запрос; True - сервис ответил 2xx."""
    try:
        with opener(request, timeout=TIMEOUT_SECONDS) as response:
            return 200 <= getattr(response, "status", 200) < 300
    except urllib.error.HTTPError as exc:
        log.warning("Оповещение не отправлено: HTTP %s", exc.code)
    except OSError as exc:  # нет сети, таймаут, DNS (URLError - тоже OSError)
        log.warning("Оповещение не отправлено: %s", type(exc).__name__)
    return False


def telegram_enabled() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def send_telegram(text: str, opener: Callable = urllib.request.urlopen) -> bool:
    """Сообщение в чат TELEGRAM_CHAT_ID. False - не настроено или не отправлено."""
    if not telegram_enabled():
        return False
    data = urllib.parse.urlencode({"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                                   "disable_web_page_preview": "true"}).encode("utf-8")
    request = urllib.request.Request(TELEGRAM_URL.format(token=config.TELEGRAM_BOT_TOKEN),
                                     data=data, method="POST")
    sent = _open(request, opener)
    if sent:
        log.info("Сообщение о прогоне отправлено в Telegram")
    return sent


def ping(ok: bool, opener: Callable = urllib.request.urlopen) -> bool:
    """Пульс в сервис мониторинга. False - не настроено или не отправлено."""
    url = config.HEALTHCHECK_URL
    if not url:
        return False
    if not url.startswith(("https://", "http://")):
        log.warning("HEALTHCHECK_URL должен начинаться с https:// - пульс не отправлен")
        return False
    return _open(urllib.request.Request(url.rstrip("/") + ("" if ok else "/fail")), opener)


def job_problems(parse_code: int, backup_ok: Optional[bool]) -> list:
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


def report_job(parse_code: int, backup_ok: Optional[bool],
               summary: Callable[[], str] = lambda: "",
               opener: Callable = urllib.request.urlopen) -> None:
    """Итог ежедневного прогона: пульс - всегда, Telegram - только о проблемах.

    :param summary: строка о последнем прогоне из базы; вызывается, только
        если сообщение действительно уходит.
    """
    problems = job_problems(parse_code, backup_ok)
    ping(not problems, opener)
    if not problems or not telegram_enabled():
        return
    lines = ["Ozon parser: " + "; ".join(problems)]
    details = summary()
    if details:
        lines.append(details)
    lines.append("Подробности: python -m ozon_parser runs, logs/scheduler.log")
    send_telegram("\n".join(lines), opener)

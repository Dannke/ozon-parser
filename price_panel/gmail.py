"""Получение кода подтверждения Ozon из почты Gmail через Gmail API.

Как подготовить доступ:
    1. Google Cloud Console -> создать проект -> включить Gmail API.
    2. "Credentials" -> "Create credentials" -> "OAuth client ID" -> тип "Desktop app".
    3. Скачать JSON и положить рядом со скриптом как credentials.json.
    4. При первом запуске откроется браузер для подтверждения доступа,
       после чего refresh-токен ляжет в token.json и больше не потребуется.

Используется только scope gmail.readonly - скрипт умеет лишь читать почту.

Про поиск кода в письме. Ошибиться здесь дорого: неверный код сжигает попытку
входа, а Ozon их считает. Поэтому:
  * берётся text/plain, если он в письме есть, а не склейка plain + html;
  * содержимое <style> и <script> вырезается целиком - в CSS письма чисел
    больше, чем в тексте;
  * просто отдельно стоящее число нужной длины принимается за код только в
    теме и сниппете. В теле письма код обязан стоять рядом со словом
    "код" / "code" / "pin".
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .logger import get_logger
from .secrets_fs import write_private

log = get_logger("gmail")

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Код Ozon - 4-6 цифр рядом с ключевым словом. Эти шаблоны применяются ко
# всему письму: привязка к слову делает совпадение достаточно надёжным.
# Границы слова и числа обязательны: без них "barcode 12345678" дал бы код
# 123456, а "shipping" сошёл бы за "pin".
KEYWORD_CODE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\b(?:код(?:\s+подтверждения)?|code|pin)\b\D{0,20}?(?<!\d)(\d{4,6})(?!\d)",
               re.IGNORECASE),
    re.compile(r"(?<!\d)(\d{4,6})(?!\d)\s*[-:—]?\s*(?:ваш\s+)?код\b", re.IGNORECASE),
)

# Запасные шаблоны: просто число нужной длины. Применяются ТОЛЬКО к теме и
# сниппету - они короткие и почти не содержат посторонних чисел, а в теле
# письма такой шаблон ловит размеры шрифта и номера заказов.
BARE_CODE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"(?<!\d)(\d{6})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4})(?!\d)"),
)

# Разметка, которую нужно убирать вместе с содержимым, а не только теги.
MARKUP_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_ENTITY_RE = re.compile(r"&[#a-z0-9]+;", re.IGNORECASE)

# Коды ответа Gmail, при которых имеет смысл повторить, а не сдаваться.
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 60


class GmailError(RuntimeError):
    """Ошибка работы с почтой: нет доступа, нет письма или код не распознан."""


class GmailTransientError(GmailError):
    """Временный сбой Gmail API (429/5xx): стоит подождать и повторить."""


def _status_of(exc: Exception) -> int:
    """Код ответа из HttpError (0 - кода нет), не полагаясь на версию библиотеки."""
    response = getattr(exc, "resp", None)
    try:
        return int(getattr(response, "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class GmailSettings:
    """Параметры подключения к почте и поиска письма с кодом."""

    credentials_file: Path
    token_file: Path
    sender_filter: str = "from:(ozon.ru OR ozon.com)"
    wait_timeout: int = 180
    poll_interval: int = 5
    recipient: str = ""
    # False - запуск без человека (Airflow): окно согласия Google открыть
    # некому, поэтому без действующего токена - сразу понятная ошибка.
    interactive: bool = True


class GmailCodeReader:
    """Обёртка над Gmail API: ищет свежее письмо от Ozon и достаёт из него код."""

    def __init__(self, settings: GmailSettings) -> None:
        self.settings = settings
        # Клиент Gmail API. Его методы (users(), messages() ...) создаются
        # динамически из описания API, поэтому статически он типизируется как Any.
        self._service: Optional[Any] = None

    # --------------------------------------------------------------- доступ --
    def _authorize(self) -> Credentials:
        """Возвращает валидные учётные данные, при необходимости обновляя токен.

        Порядок: сохранённый token.json -> его обновление по refresh-токену ->
        окно согласия Google (только при запуске с человеком).
        """
        creds = self._load_saved_token()
        if creds is not None and creds.valid:
            return creds

        if creds is not None and creds.expired and creds.refresh_token:
            creds = self._refresh(creds)
        if creds is None or not creds.valid:
            creds = self._request_consent()

        # В файле лежит refresh-токен к почте - он доступен только владельцу.
        try:
            write_private(self.settings.token_file, creds.to_json())
            log.info("Токен Gmail сохранён: %s", self.settings.token_file)
        except OSError as exc:
            log.warning("Не удалось сохранить токен: %s", exc)
        return creds

    def _load_saved_token(self) -> Optional[Credentials]:
        """Учётные данные из token.json; None - файла нет или он повреждён."""
        token_file = self.settings.token_file
        if not token_file.exists():
            return None
        try:
            return Credentials.from_authorized_user_file(str(token_file), SCOPES)
        except (ValueError, OSError) as exc:
            log.warning("Файл токена повреждён (%s), будет создан заново", exc)
            return None

    @staticmethod
    def _refresh(creds: Credentials) -> Optional[Credentials]:
        """Обновляет протухший токен без участия человека; None - не вышло."""
        try:
            log.info("Обновляю истёкший токен Gmail")
            creds.refresh(Request())
        except RefreshError as exc:
            log.warning("Обновить токен не вышло (%s), нужна повторная авторизация", exc)
            return None
        except TransportError as exc:
            raise GmailError("Нет связи с Google для обновления токена: {}".format(exc)) from exc
        return creds

    def _request_consent(self) -> Credentials:
        """Открывает окно согласия Google и возвращает новые учётные данные."""
        if not self.settings.interactive:
            raise GmailError(
                "Токен Gmail ({}) отсутствует или отозван, а окно согласия Google "
                "в фоновом запуске открыть некому. Выполните python get_cookies.py "
                "--force на машине с браузером и перенесите token.json. Если токен "
                "протухает раз в неделю, OAuth-приложение в Google Cloud в статусе "
                "Testing - переведите его в Production.".format(self.settings.token_file.name)
            )
        if not self.settings.credentials_file.exists():
            raise GmailError(
                "Не найден файл OAuth-клиента: {}. "
                "Скачайте credentials.json из Google Cloud Console.".format(
                    self.settings.credentials_file
                )
            )

        log.info("Запускаю OAuth-авторизацию Gmail (откроется окно браузера)")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.settings.credentials_file), SCOPES
        )
        creds = flow.run_local_server(port=0)
        # Библиотека объявляет два возможных класса учётных данных, но для
        # OAuth-клиента типа «Desktop app» это всегда google.oauth2 Credentials.
        if not isinstance(creds, Credentials):
            raise GmailError(
                "Google вернул учётные данные неожиданного типа: {}. Проверьте, что "
                "credentials.json - OAuth-клиент типа Desktop app".format(type(creds).__name__)
            )
        return creds

    @property
    def _api(self) -> Any:
        """Клиент Gmail API; обращаться к нему можно только после connect()."""
        if self._service is None:
            raise GmailError("Gmail API не подключён: сначала вызовите connect()")
        return self._service

    def connect(self) -> None:
        """Создаёт клиент Gmail API.

        Вызывается заранее, ДО запроса кода на сайте: если доступа к почте нет,
        лучше упасть до того, как Ozon потратит одну из попыток отправки кода.
        """
        if self._service is not None:
            return
        try:
            self._service = build(
                "gmail", "v1", credentials=self._authorize(), cache_discovery=False
            )
            log.info("Подключение к Gmail API установлено")
        except HttpError as exc:
            raise GmailError("Gmail API вернул ошибку при подключении: {}".format(exc)) from exc

    # ----------------------------------------------------------- извлечение --
    @staticmethod
    def _decode(data: str) -> str:
        """Декодирует base64url-тело письма, не падая на битых символах."""
        try:
            return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""

    @classmethod
    def _collect_by_mime(cls, payload: dict, mime_type: str) -> str:
        """Рекурсивно собирает части письма с нужным MIME-типом."""
        chunks: list[str] = []
        if (payload.get("mimeType") or "").lower().startswith(mime_type):
            body = payload.get("body") or {}
            if body.get("data"):
                chunks.append(cls._decode(body["data"]))
        for part in payload.get("parts") or []:
            chunk = cls._collect_by_mime(part, mime_type)
            if chunk:
                chunks.append(chunk)
        return "\n".join(chunks)

    @classmethod
    def _collect_any(cls, payload: dict) -> str:
        """Всё, что удаётся вытащить, без разбора типов - на случай письма
        без заявленного MIME-типа."""
        chunks: list[str] = []
        body = payload.get("body") or {}
        if body.get("data"):
            chunks.append(cls._decode(body["data"]))
        for part in payload.get("parts") or []:
            chunks.append(cls._collect_any(part))
        return "\n".join(chunk for chunk in chunks if chunk)

    @classmethod
    def _collect_text(cls, payload: dict) -> str:
        """Текст письма: предпочитаем text/plain, HTML берём только без него.

        Обе версии несут одно содержание, но в HTML вдобавок стили и скрипты -
        лишний шум для поиска кода.
        """
        plain = cls._collect_by_mime(payload, "text/plain")
        if plain:
            return plain
        html = cls._collect_by_mime(payload, "text/html")
        return html or cls._collect_any(payload)

    @staticmethod
    def _headers(payload: dict) -> dict:
        """Заголовки письма в виде словаря с ключами в нижнем регистре."""
        return {
            header.get("name", "").lower(): header.get("value", "")
            for header in (payload.get("headers") or [])
        }

    @staticmethod
    def strip_markup(text: str) -> str:
        """Убирает разметку вместе с содержимым <style>, <script> и комментариев.

        Снимать одни теги мало: в CSS письма чисел заметно больше, чем в самом
        тексте (размеры, цвета, идентификаторы трекинга).
        """
        if not text:
            return ""
        clean = MARKUP_BLOCK_RE.sub(" ", text)
        clean = HTML_COMMENT_RE.sub(" ", clean)
        clean = HTML_TAG_RE.sub(" ", clean)
        return HTML_ENTITY_RE.sub(" ", clean)

    @classmethod
    def extract_code(cls, text: str, headline: str = "") -> Optional[str]:
        """Достаёт код подтверждения из письма (или None, если не нашёлся).

        :param text: тело письма; здесь код обязан стоять рядом с ключевым словом.
        :param headline: тема и сниппет - короткие и почти без посторонних
            чисел, поэтому в них принимается и просто отдельно стоящее число.
        """
        body = cls.strip_markup(text)
        head = cls.strip_markup(headline)
        both = " ".join(part for part in (head, body) if part)

        for pattern in KEYWORD_CODE_PATTERNS:
            match = pattern.search(both)
            if match:
                return match.group(1)

        for pattern in BARE_CODE_PATTERNS:
            match = pattern.search(head)
            if match:
                return match.group(1)
        return None

    # ----------------------------------------------------------------- поиск --
    def _search_messages(self, query: str, limit: int = 10) -> list:
        """Список id свежих писем, подходящих под поисковый запрос Gmail."""
        try:
            response = (
                self._api.users()
                .messages()
                # Письмо с кодом нередко попадает в «Спам» - ищем и там.
                .list(userId="me", q=query, maxResults=limit, includeSpamTrash=True)
                .execute()
            )
        except HttpError as exc:
            if _status_of(exc) in TRANSIENT_STATUSES:
                raise GmailTransientError("Gmail API: {}".format(exc)) from exc
            raise GmailError("Ошибка поиска писем: {}".format(exc)) from exc
        return response.get("messages", [])

    def _fetch_message(self, message_id: str) -> Optional[dict]:
        try:
            return (
                self._api.users()
                .messages()
                .get(userId="me", id=message_id, format="full")
                .execute()
            )
        except HttpError as exc:
            log.warning("Не удалось прочитать письмо %s: %s", message_id, exc)
            return None

    def _code_from_messages(self, messages: Iterable, since_ms: int,
                            seen: Optional[set] = None) -> Optional[str]:
        """Проверяет письма (от новых к старым) и возвращает первый найденный код.

        Идентификаторы просмотренных писем складываются в ``seen``, чтобы
        следующая итерация опроса не тянула их тела заново.
        """
        for meta in messages:
            message_id = meta["id"]
            message = self._fetch_message(message_id)
            if message is None:
                continue  # не прочиталось - в seen не кладём, попробуем позже

            if seen is not None:
                seen.add(message_id)

            # Отсекаем письма, пришедшие ДО запроса кода: иначе легко подставить
            # старый код от предыдущей попытки входа и получить "неверный код".
            internal_ms = int(message.get("internalDate", 0))
            if internal_ms < since_ms:
                continue

            payload = message.get("payload") or {}
            headers = self._headers(payload)

            # Если в одном ящике несколько аккаунтов - сверяем получателя.
            if self.settings.recipient:
                recipients = " ".join(
                    headers.get(key, "") for key in ("to", "delivered-to", "cc")
                ).lower()
                if recipients and self.settings.recipient.lower() not in recipients:
                    continue

            headline = " ".join(part for part in (headers.get("subject", ""),
                                                  message.get("snippet", "")) if part)
            code = self.extract_code(self._collect_text(payload), headline=headline)
            if code:
                log.info("Код найден в письме от %s", headers.get("from", "?"))
                return code
            log.debug("В письме %s код не распознан", message_id)
        return None

    def wait_for_code(self, since_ts: float, timeout: Optional[int] = None) -> str:
        """Опрашивает почту, пока не придёт письмо с кодом подтверждения.

        Просмотренные письма запоминаются, чтобы каждая итерация опроса не
        тянула их тела заново.

        :param since_ts: unix-время (сек) момента запроса кода; письма старше игнорируются.
        :param timeout: сколько секунд ждать; по умолчанию берётся из настроек.
        :raises GmailError: письмо не пришло за отведённое время или код не распознан.
        """
        self.connect()

        timeout = timeout or self.settings.wait_timeout
        # Запас в минуту назад: часы клиента и серверов Gmail могут слегка расходиться.
        since_ms = int((since_ts - 60) * 1000)
        query = "{} newer_than:1d".format(self.settings.sender_filter).strip()
        deadline = time.monotonic() + timeout

        log.info("Жду письмо с кодом (до %s с), поисковый запрос: %r", timeout, query)
        seen: set = set()
        attempt = 0
        backoff = 0

        while time.monotonic() < deadline:
            attempt += 1
            try:
                messages = self._search_messages(query)
            except GmailTransientError as exc:
                backoff = min(backoff * 2 or self.settings.poll_interval, MAX_BACKOFF_SECONDS)
                log.warning("Попытка %s: временная ошибка Gmail (%s), жду %s с",
                            attempt, exc, backoff)
                time.sleep(backoff)
                continue
            backoff = 0

            fresh = [meta for meta in messages if meta.get("id") not in seen]
            if fresh:
                code = self._code_from_messages(fresh, since_ms, seen)
                if code:
                    return code
                log.info("Попытка %s: в новых письмах (%s) кода нет", attempt, len(fresh))
            elif messages:
                log.info("Попытка %s: новых писем нет, проверено всего %s", attempt, len(seen))
            else:
                log.info("Попытка %s: писем по фильтру не найдено", attempt)
            time.sleep(self.settings.poll_interval)

        raise GmailError(
            "Письмо с кодом не пришло за {} с. Проверьте GMAIL_SENDER_FILTER "
            "и OZON_EMAIL, а также куда Ozon отправляет код для этого аккаунта "
            "(почта или SMS).".format(timeout)
        )

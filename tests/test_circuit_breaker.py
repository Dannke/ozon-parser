"""Предохранитель парсера: серия SKU без данных останавливает прогон.

Повод - прогон 30.09.2026 в Docker: Ozon 5 часов отвечал 403 на каждый товар,
а парсер честно делал по 3 попытки на каждый из 957 SKU.

Работает настоящий цикл ядра (core.collect) с адаптером Ozon; подменены только
браузер (Playwright) и разбор одного SKU.
"""

from __future__ import annotations

import contextlib
from typing import cast

import pytest
from playwright.sync_api import Page

from price_panel.core.collect import RunObserver, collect
from price_panel.core.models import SkuOutcome
from price_panel.infra import config
from price_panel.marketplaces.ozon import parse, session


class FakePage:
    def goto(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, ms):
        pass


class FakeContext:
    def new_page(self):
        return FakePage()

    def close(self):
        pass


class FakeBrowser:
    def close(self):
        pass


class Recorder(RunObserver):
    def __init__(self):
        self.done: list = []
        self.failed: list = []

    def sku_done(self, sku, product, seconds):
        self.done.append(sku)

    def sku_failed(self, sku, error_type, message, seconds, attempts=0):
        self.failed.append((sku, error_type))


@pytest.fixture
def scripted(monkeypatch):
    """Подменяет браузер; возвращает функцию, задающую итоги SKU по сценарию."""
    monkeypatch.setattr(session, "load_session", lambda path: {"cookies": []})
    monkeypatch.setattr(parse, "sync_playwright", contextlib.nullcontext)
    monkeypatch.setattr(parse.browser_utils, "launch", lambda playwright: FakeBrowser())
    monkeypatch.setattr(
        parse.browser_utils, "new_context", lambda browser, storage_state=None: FakeContext()
    )
    monkeypatch.setattr(parse.browser_utils, "pass_challenge", lambda page, response: True)
    monkeypatch.setattr(parse.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(config, "MAX_CONSECUTIVE_FAILURES", 4)
    monkeypatch.setattr(config, "MAX_CONSECUTIVE_CHALLENGES", 2)
    calls: list = []

    def use(results: dict):
        def outcome(page, sku, retries, options=None):
            calls.append(sku)
            kind = results.get(sku, "fetch_error")
            if kind == "ok":
                return SkuOutcome(sku=sku, product={"sku": sku, "title": "t"}, attempts=1)
            return SkuOutcome(sku=sku, error_type=kind, error_message="403", attempts=3)

        monkeypatch.setattr(parse, "parse_sku_outcome", outcome)
        return calls

    return use


def skus(count):
    return [str(n) for n in range(1, count + 1)]


def run(count, observer):
    """Сбор count SKU адаптером Ozon с предохранителем из .env."""
    return collect(parse.OzonAdapter(), skus(count), observer, parse.breaker(), parse.log)


def test_series_of_failures_stops_the_run(scripted):
    calls = scripted({"1": "ok"})
    observer = Recorder()
    assert run(10, observer).blocked

    assert calls == ["1", "2", "3", "4", "5"], "после 4 отказов подряд новых SKU не берём"
    assert observer.done == ["1"]
    assert [t for _, t in observer.failed] == ["fetch_error"] * 4 + ["blocked"] * 5


def test_success_and_not_found_break_the_series(scripted):
    # 3 отказа, «товара нет», 3 отказа, успех, 3 отказа - серии по 4 не набирается.
    results = {"4": "not_found", "8": "ok"}
    calls = scripted(results)
    observer = Recorder()
    run(11, observer)

    assert len(calls) == 11
    assert not any(t == "blocked" for _, t in observer.failed)


def test_zero_disables_the_breaker(scripted, monkeypatch):
    calls = scripted({})
    monkeypatch.setattr(config, "MAX_CONSECUTIVE_FAILURES", 0)
    run(12, Recorder())
    assert len(calls) == 12


def test_failed_antibot_checks_stop_the_run_sooner(scripted):
    """Непройденная антибот-проверка - явная блокировка: порог меньше (2, а не 4)."""
    calls = scripted({"1": "ok", "2": "antibot", "3": "antibot"})
    observer = Recorder()
    assert run(10, observer).blocked
    assert calls == ["1", "2", "3"]
    assert [t for _, t in observer.failed] == ["antibot"] * 2 + ["blocked"] * 7


def test_antibot_series_is_broken_by_other_outcomes(scripted):
    # antibot, fetch_error, antibot, ok, antibot - двух проверок подряд нет,
    # а серия любых неудач (3) не дотягивает до 4.
    results = {
        "1": "antibot",
        "2": "fetch_error",
        "3": "antibot",
        "4": "ok",
        "5": "antibot",
        "6": "ok",
    }
    calls = scripted(results)
    observer = Recorder()
    run(6, observer)
    assert len(calls) == 6
    assert not any(t == "blocked" for _, t in observer.failed)


def test_failed_antibot_check_is_not_retried(monkeypatch):
    """Каждая попытка стоит 30 с ожидания - при блокировке повторять тот же SKU незачем."""
    attempts: list = []

    def blocked_page(page, sku, options=None):
        attempts.append(sku)
        raise parse.ChallengeFailed("SKU {}: антибот-проверка не прошла".format(sku))

    monkeypatch.setattr(parse, "parse_sku", blocked_page)
    monkeypatch.setattr(parse.time, "sleep", lambda seconds: None)
    outcome = parse.parse_sku_outcome(cast(Page, None), "123", retries=2)
    assert attempts == ["123"]
    assert (outcome.error_type, outcome.attempts) == ("antibot", 1)


def test_ordinary_fetch_errors_are_still_retried(monkeypatch):
    attempts: list = []

    def flaky(page, sku, options=None):
        attempts.append(sku)
        raise parse.FetchError("SKU {}: данные карточки не найдены".format(sku))

    monkeypatch.setattr(parse, "parse_sku", flaky)
    monkeypatch.setattr(parse.time, "sleep", lambda seconds: None)
    outcome = parse.parse_sku_outcome(cast(Page, None), "123", retries=2)
    assert len(attempts) == 3
    assert outcome.error_type == "fetch_error"


def test_blocked_run_is_not_restarted_in_a_new_browser(scripted, monkeypatch):
    """Остановка предохранителем - не падение браузера: перезапуска нет."""
    launches: list = []
    monkeypatch.setattr(
        parse.browser_utils, "launch", lambda playwright: launches.append(1) or FakeBrowser()
    )
    monkeypatch.setattr(config, "MAX_BROWSER_RESTARTS", 2)
    scripted({})
    run(10, Recorder())
    assert len(launches) == 1

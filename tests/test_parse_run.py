"""Прогон списка SKU целиком: перезапуск упавшего браузера и итоговый статус.

Браузер здесь не нужен: parse_in_browser подменяется сценарием, который
обрабатывает SKU и «падает» в заданный момент.
"""

from __future__ import annotations

import pytest

from price_panel import config, parse, session, storage


@pytest.fixture
def saved(monkeypatch):
    """Подменяет сессию и хранилище; возвращает список сохранённых срезов."""
    snapshots: list = []
    monkeypatch.setattr(session, "load_session", lambda path: {"cookies": []})
    monkeypatch.setattr(storage, "save",
                        lambda rows, **kwargs: snapshots.append(list(rows)))
    monkeypatch.setattr(config, "MAX_BROWSER_RESTARTS", 2)
    return snapshots


def fake_browser(crash_on: set):
    """parse_in_browser, который падает, дойдя до SKU из crash_on (один раз на SKU)."""
    launches: list = []

    def run_session(progress, state, total, flush_batch):
        launches.append(list(progress.pending))
        while progress.pending:
            sku = progress.pending[0]
            if sku in crash_on:
                crash_on.discard(sku)
                raise parse.BrowserGone("Target page, context or browser has been closed")
            progress.pending.pop(0)
            progress.rows.append({"sku": sku, "title": "товар " + sku})
            flush_batch()

    return run_session, launches


def test_crashed_browser_is_restarted_and_sku_retried(monkeypatch, saved):
    """SKU, на котором упал браузер, обрабатывается заново в новом браузере."""
    run_session, launches = fake_browser(crash_on={"2"})
    monkeypatch.setattr(parse, "parse_in_browser", run_session)

    assert parse.run(["1", "2", "3"], batch_size=0) == 0
    assert launches == [["1", "2", "3"], ["2", "3"]]
    assert [row["sku"] for row in saved[-1]] == ["1", "2", "3"]


def test_restarts_are_limited(monkeypatch, saved):
    """Браузер, который падает раз за разом, не перезапускается бесконечно."""
    def always_crash(progress, state, total, flush_batch):
        raise RuntimeError("Connection closed while reading from the driver")

    monkeypatch.setattr(parse, "parse_in_browser", always_crash)
    assert parse.run(["1", "2"], batch_size=0) == 1
    assert saved[-1] == []  # пустой срез всё равно записан поверх старого


def test_success_rate_counts_unprocessed_skus(monkeypatch, saved):
    """SKU, до которых не дошла очередь, - неудача, а не «не считаются»."""
    run_session, _ = fake_browser(crash_on={"2"})
    monkeypatch.setattr(parse, "parse_in_browser", run_session)
    monkeypatch.setattr(config, "MAX_BROWSER_RESTARTS", 0)
    assert parse.run(["1", "2", "3", "4"], batch_size=0, min_success_rate=0.5) == 1
    assert [row["sku"] for row in saved[-1]] == ["1"]


@pytest.mark.parametrize("interval, since_last_open, expected", [
    (6.5, 3.8, [2.7]),  # быстрый разбор (html) + пауза 3 с: ждём до 6,5 с от прошлой карточки
    (6.5, 6.6, []),     # обычный разбор (api) уже дольше предела - не ждём
    (0.0, 3.8, []),     # предел выключен
])
def test_pages_open_no_faster_than_page_interval(monkeypatch, interval, since_last_open,
                                                 expected):
    """07.10.2026: та же пауза 3 с при быстром разборе дала ~14 карточек в минуту и капчу."""
    slept: list = []
    monkeypatch.setattr(config, "PAGE_INTERVAL", interval)
    monkeypatch.setattr(parse, "_last_page_open", 100.0)
    monkeypatch.setattr(parse.time, "monotonic", lambda: 100.0 + since_last_open)
    monkeypatch.setattr(parse.time, "sleep", slept.append)

    parse.wait_page_slot()
    assert slept == [pytest.approx(value) for value in expected]
    assert parse._last_page_open == 100.0 + since_last_open  # отсчёт - от этого открытия


def test_batches_are_flushed_during_run(monkeypatch, saved):
    """Промежуточные сохранения идут по ходу прогона, а не только в конце."""
    run_session, _ = fake_browser(crash_on=set())
    monkeypatch.setattr(parse, "parse_in_browser", run_session)

    assert parse.run(["1", "2", "3", "4", "5"], batch_size=2) == 0
    assert [len(rows) for rows in saved] == [2, 4, 5]

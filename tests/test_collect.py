"""Контракт ядра с адаптером маркетплейса (core.collect) - без сайта и браузера.

На этот контракт опирается каждый адаптер: итоги SKU по порядку, причина
ранней остановки, закрытие генератора, когда ядро останавливает сбор.
"""

from __future__ import annotations

import logging

from price_panel.core.collect import Breaker, RunObserver, collect
from price_panel.core.models import SkuOutcome

LOG = logging.getLogger("test_collect")


class Recorder(RunObserver):
    def __init__(self):
        self.events: list = []

    def sku_done(self, sku, product, seconds):
        self.events.append((sku, "ok"))

    def sku_failed(self, sku, error_type, message, seconds, attempts=0):
        self.events.append((sku, error_type, message))


class Adapter:
    """Отдаёт итоги по сценарию; closed - закрыл ли ядро его генератор."""

    def __init__(self, kinds: dict, stop: str | None = None, interrupt_at: str = ""):
        self.kinds = kinds
        self.stop = stop
        self.interrupt_at = interrupt_at
        self.closed = False

    def collect(self, skus):
        try:
            for sku in skus:
                if sku == self.interrupt_at:
                    raise KeyboardInterrupt
                kind = self.kinds.get(sku)
                if kind is None:
                    return self.stop
                if kind == "ok":
                    yield SkuOutcome(sku=sku, product={"sku": sku})
                else:
                    yield SkuOutcome(sku=sku, error_type=kind)
            return None
        finally:
            self.closed = True


def test_skus_after_an_early_stop_get_the_adapter_reason():
    recorder = Recorder()
    result = collect(
        Adapter({"1": "ok"}, stop="браузер падал"), ["1", "2", "3"], recorder, Breaker(0, 0), LOG
    )
    assert recorder.events == [
        ("1", "ok"),
        ("2", "not_processed", "браузер падал"),
        ("3", "not_processed", "браузер падал"),
    ]
    assert (result.success, result.failed, result.blocked) == (1, ["2", "3"], False)


def test_breaker_stops_collection_and_closes_the_adapter():
    """После серии отказов ядро закрывает генератор - адаптер закрывает свой браузер."""
    adapter = Adapter({str(n): "fetch_error" for n in range(1, 6)})
    recorder = Recorder()
    result = collect(adapter, ["1", "2", "3", "4", "5"], recorder, Breaker(2, 0), LOG)
    assert result.blocked and adapter.closed
    assert [event[1] for event in recorder.events] == ["fetch_error"] * 2 + ["blocked"] * 3


def test_interrupt_keeps_what_was_collected():
    recorder = Recorder()
    result = collect(
        Adapter({"1": "ok", "2": "ok"}, interrupt_at="2"),
        ["1", "2", "3"],
        recorder,
        Breaker(0, 0),
        LOG,
    )
    assert recorder.events[0] == ("1", "ok")
    assert [event[1] for event in recorder.events[1:]] == ["interrupted", "interrupted"]
    assert result.success == 1


def test_observer_failure_does_not_stop_collection():
    class Broken(RunObserver):
        def sku_done(self, sku, product, seconds):
            raise RuntimeError("база недоступна")

    result = collect(Adapter({"1": "ok", "2": "ok"}), ["1", "2"], Broken(), Breaker(0, 0), LOG)
    assert result.success == 2

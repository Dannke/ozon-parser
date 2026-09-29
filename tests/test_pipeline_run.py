"""Учёт прогона по panel: parse_runs, история, ошибки по SKU.

Браузер и база не нужны: parse_in_browser подменяется сценарием, который
отдаёт итоги SKU через RunProgress.notify, а Warehouse - объектом в памяти.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from ozon_parser import config, parse, pipeline, session, storage
from ozon_parser.db import DatabaseError
from ozon_parser.parse import SkuOutcome
from ozon_parser.settings import parse_settings
from ozon_parser.warehouse import Warehouse

SETTINGS = parse_settings({
    "discovery": {"categories": [{"name": "phones", "panel_size": 5,
                                  "url": "https://www.ozon.ru/category/smartfony-15502/"}]},
    "parser": {"min_success_rate": 0.5},
})


class FakeWarehouse(Warehouse):
    """Хранилище в памяти с тем же интерфейсом, что у Warehouse."""

    def __init__(self, broken_skus=(), locked=False):  # без подключения к базе
        self.broken = set(broken_skus)
        self.locked = locked
        self.products: list = []
        self.errors: list = []
        self.runs: dict = {}

    def try_parse_lock(self):
        return not self.locked

    def close_stale_runs(self):
        return 0

    def start_parse_run(self, kind, sku_source, total_sku, request_delay):
        self.runs[1] = {"kind": kind, "source": sku_source, "total": total_sku}
        return 1

    def finish_parse_run(self, run_id, status, processed, success, errors, duration_seconds,
                         sku_per_minute, avg_sku_seconds):
        self.runs[run_id].update(status=status, processed=processed, success=success,
                                 errors=errors, speed=sku_per_minute, avg=avg_sku_seconds)

    def record_product(self, run_id, product, collected_at=None):
        if product["sku"] in self.broken:
            raise DatabaseError("Ошибка PostgreSQL: connection refused")
        self.products.append(product["sku"])

    def record_error(self, run_id, sku, error_type, message, attempts=None):
        self.errors.append((sku, error_type))


def scripted_browser(results: dict):
    """parse_in_browser, который отдаёт заранее заданные итоги SKU."""
    def run_session(progress, state, total, flush_batch):
        while progress.pending:
            sku = progress.pending.pop(0)
            kind = results.get(sku, "ok")
            if kind == "ok":
                outcome = SkuOutcome(sku=sku, product={"sku": sku, "title": "t", "price": 1.0},
                                     attempts=1)
                progress.rows.append(outcome.product)
            else:
                outcome = SkuOutcome(sku=sku, error_type=kind, error_message="fail", attempts=3)
                progress.failed.append(sku)
            progress.notify(outcome, 2.0)
            flush_batch()
    return run_session


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    monkeypatch.setattr(session, "load_session", lambda path: {"cookies": []})
    saved: list = []
    monkeypatch.setattr(storage, "save", lambda rows, **kw: saved.append(kw.get("backend")))
    monkeypatch.setattr(config, "MAX_BROWSER_RESTARTS", 0)
    return saved


def test_every_sku_ends_up_in_history_or_errors(monkeypatch):
    monkeypatch.setattr(parse, "parse_in_browser",
                        scripted_browser({"2": "not_found", "3": "fetch_error"}))
    wh = FakeWarehouse()
    report = pipeline.run_parse(wh, ["1", "2", "3", "4"], SETTINGS)

    assert wh.products == ["1", "4"]
    assert wh.errors == [("2", "not_found"), ("3", "fetch_error")]
    assert report.status == "partial"
    assert report.processed == 4
    assert report.exit_code == 0                      # 50% >= min_success_rate 0.5
    assert wh.runs[1]["status"] == "partial"
    assert wh.runs[1]["success"] == 2 and wh.runs[1]["errors"] == 2
    assert wh.runs[1]["avg"] == 2.0


def test_storage_failure_is_an_error_not_a_success(monkeypatch):
    monkeypatch.setattr(parse, "parse_in_browser", scripted_browser({}))
    wh = FakeWarehouse(broken_skus={"2"})
    report = pipeline.run_parse(wh, ["1", "2"], SETTINGS)
    assert report.success == 1
    assert wh.errors == [("2", "storage_error")]


def test_crashed_browser_leaves_skus_as_not_processed(monkeypatch):
    def always_crash(progress, state, total, flush_batch):
        raise parse.BrowserGone("Target page, context or browser has been closed")

    monkeypatch.setattr(parse, "parse_in_browser", always_crash)
    wh = FakeWarehouse()
    report = pipeline.run_parse(wh, ["1", "2"], SETTINGS)

    assert wh.errors == [("1", "not_processed"), ("2", "not_processed")]
    assert report.status == "failed"
    assert report.processed == 0 and report.sku_per_minute is None
    assert report.exit_code == 1


def test_missing_session_is_visible_in_run_accounting(monkeypatch):
    """parse.run выходит сразу, но SKU не пропадают из учёта молча."""
    def no_session(path):
        raise session.SessionError("Файл cookies не найден")

    monkeypatch.setattr(session, "load_session", no_session)
    wh = FakeWarehouse()
    report = pipeline.run_parse(wh, ["1"], SETTINGS)
    assert wh.errors == [("1", "not_processed")]
    assert report.status == "failed"


def test_second_parallel_run_is_refused():
    with pytest.raises(pipeline.ParseLockBusy):
        pipeline.run_parse(FakeWarehouse(locked=True), ["1"], SETTINGS)


def test_csv_export_can_be_disabled(monkeypatch, no_side_effects):
    """Без csv_export прогон не трогает data/products.csv старого сценария."""
    monkeypatch.setattr(parse, "parse_in_browser", scripted_browser({}))
    pipeline.run_parse(FakeWarehouse(), ["1"], SETTINGS)
    assert set(no_side_effects) == {"none"}


def test_storage_none_backend_writes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "products.csv"
        storage.save([{"sku": "1"}], backend="none", csv_path=path)
        assert not path.exists()


def test_run_status():
    from collections import Counter
    assert pipeline.run_status(3, Counter()) == "success"
    assert pipeline.run_status(3, Counter(not_found=1)) == "partial"
    assert pipeline.run_status(0, Counter(fetch_error=2)) == "failed"
    assert pipeline.run_status(3, Counter(interrupted=1)) == "interrupted"


def test_capacity_and_report():
    assert pipeline.capacity(5.0, 8, 0.8) == (2400, 1920)
    assert pipeline.format_duration(522) == "8m 42s"
    assert pipeline.format_duration(3725) == "1h 02m 05s"


def test_panel_csv_with_header_is_read_by_legacy_reader():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "panel.csv"
        path.write_text("﻿sku\n111111\n222222\n111111\n", encoding="utf-8")
        assert parse.read_skus_file(path) == ["111111", "222222"]
        path.write_text("sku,category\n111111,phones\n", encoding="utf-8")
        assert parse.read_skus_file(path) == ["111111"]

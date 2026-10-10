"""Учёт прогона по panel: parse_runs, история, ошибки по SKU.

Браузер и база не нужны: parse_in_browser подменяется сценарием, который
отдаёт итоги SKU через RunProgress.notify, а Warehouse - объектом в памяти.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path

import pytest

from price_panel.app import pipeline
from price_panel.app.settings import parse_settings
from price_panel.infra import config
from price_panel.infra.db import DatabaseError
from price_panel.infra.warehouse import Warehouse
from price_panel.legacy import storage
from price_panel.marketplaces.ozon import parse, session
from price_panel.marketplaces.ozon.parse import SkuOutcome

SETTINGS_DATA = {
    "discovery": {
        "categories": [
            {
                "name": "phones",
                "panel_size": 5,
                "url": "https://www.ozon.ru/category/smartfony-15502/",
            }
        ]
    },
    "parser": {"min_success_rate": 0.5},
}
SETTINGS = parse_settings(SETTINGS_DATA)


class FakeWarehouse(Warehouse):
    """Хранилище в памяти с тем же интерфейсом, что у Warehouse."""

    def __init__(self, broken_skus=(), locked=False):  # без подключения к базе
        self.broken = set(broken_skus)
        self.locked = locked
        self.products: list = []
        self.errors: list = []
        self.runs: dict = {}
        self.without_details: set = set()

    def try_parse_lock(self):
        return not self.locked

    def close_stale_runs(self):
        return 0

    def start_parse_run(self, kind, sku_source, total_sku, request_delay):
        self.runs[1] = {"kind": kind, "source": sku_source, "total": total_sku}
        return 1

    def finish_parse_run(
        self,
        run_id,
        status,
        processed,
        success,
        errors,
        duration_seconds,
        sku_per_minute,
        avg_sku_seconds,
    ):
        self.runs[run_id].update(
            status=status,
            processed=processed,
            success=success,
            errors=errors,
            speed=sku_per_minute,
            avg=avg_sku_seconds,
        )

    def record_product(self, run_id, product, collected_at=None):
        if product["sku"] in self.broken:
            raise DatabaseError("Ошибка PostgreSQL: connection refused")
        self.products.append(product["sku"])

    def record_error(self, run_id, sku, error_type, message, attempts=None):
        self.errors.append((sku, error_type))

    def skus_without_details(self, skus):
        return set(self.without_details)


def scripted_browser(results: dict, seen_options: list | None = None):
    """parse_in_browser, который отдаёт заранее заданные итоги SKU."""

    def run_session(progress, state, total, flush_batch):
        if seen_options is not None:
            seen_options.append(progress.options)
        while progress.pending:
            sku = progress.pending.pop(0)
            kind = results.get(sku, "ok")
            if kind == "ok":
                outcome = SkuOutcome(
                    sku=sku, product={"sku": sku, "title": "t", "price": 1.0}, attempts=1
                )
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
    monkeypatch.setattr(
        parse, "parse_in_browser", scripted_browser({"2": "not_found", "3": "fetch_error"})
    )
    wh = FakeWarehouse()
    report = pipeline.run_parse(wh, ["1", "2", "3", "4"], SETTINGS)

    assert wh.products == ["1", "4"]
    assert wh.errors == [("2", "not_found"), ("3", "fetch_error")]
    assert report.status == "partial"
    assert report.processed == 4
    assert report.exit_code == 0  # 50% >= min_success_rate 0.5
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


def test_blocked_run_has_its_own_exit_code(monkeypatch):
    """Планировщик по коду выхода решает, повторять ли прогон позже."""
    monkeypatch.setattr(
        parse, "parse_in_browser", scripted_browser({"2": "antibot", "3": "blocked"})
    )
    report = pipeline.run_parse(FakeWarehouse(), ["1", "2", "3"], SETTINGS)
    assert report.status == "blocked"
    assert report.exit_code == pipeline.EXIT_BLOCKED != 1


def test_broken_session_is_visible_in_run_accounting(monkeypatch):
    """parse.run выходит сразу, но SKU не пропадают из учёта молча.

    Отсутствие файла сессии - не ошибка (карточки открываются без входа), а
    повреждённый файл (например, каталог вместо него от Docker) - ошибка.
    """

    def broken_session(path):
        raise session.SessionError("Файл cookies повреждён")

    monkeypatch.setattr(session, "load_session", broken_session)
    wh = FakeWarehouse()
    report = pipeline.run_parse(wh, ["1"], SETTINGS)
    assert wh.errors == [("1", "not_processed")]
    assert report.status == "failed"


def test_parser_settings_reach_the_browser_loop(monkeypatch):
    """price_source и расписание описаний из config.yaml доходят до разбора SKU."""
    seen: list = []
    monkeypatch.setattr(parse, "parse_in_browser", scripted_browser({}, seen))
    settings = parse_settings(
        {
            "discovery": SETTINGS_DATA["discovery"],
            "parser": {"price_source": "html", "details_refresh_days": 0},
        }
    )
    wh = FakeWarehouse()
    wh.without_details = {"2"}
    pipeline.run_parse(wh, ["1", "2"], settings)

    (options,) = seen
    assert options.price_source == "html"
    assert [options.wants_details(sku) for sku in ("1", "2")] == [False, True]


def test_details_are_spread_over_the_week():
    """Раз в N дней - у каждого SKU ровно один день из N, а не вся panel разом."""
    wh = FakeWarehouse()
    wh.without_details = {"new"}
    skus = [str(1_000_000 + n) for n in range(700)]
    week = [
        pipeline.details_schedule(
            wh, skus + ["new"], 7, dt.date(2026, 10, 6) + dt.timedelta(days=day)
        )
        for day in range(7)
    ]

    assert all(due is not None for due in week)
    assert all(sum(sku in due for due in week if due) == 1 for sku in skus)
    assert all("new" in due for due in week if due)  # без описания - каждый день
    assert 60 < len((week[0] or set()) - {"new"}) < 140  # около 1/7 panel в день

    # Раз в день - всем (None), без запроса к базе; 0 - только SKU без описания.
    assert pipeline.details_schedule(wh, skus, 1) is None
    assert pipeline.details_schedule(wh, skus + ["new"], 0) == {"new"}


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
    # Прогон, остановленный предохранителем, виден в parse_runs отдельно.
    assert pipeline.run_status(1, Counter(fetch_error=10, blocked=900)) == "blocked"


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


class QueueWarehouse(FakeWarehouse):
    """panel_skus как в базе: missing_since убирает SKU, собранные сегодня."""

    def __init__(self, panel, collected_today=()):
        super().__init__()
        self.panel = list(panel)
        self.collected_today = set(collected_today)
        self.since = None

    def panel_skus(self, categories=None, limit=None, missing_since=None):
        self.since = missing_since or self.since
        skus = [s for s in self.panel if missing_since is None or s not in self.collected_today]
        return skus[:limit] if limit is not None else skus

    def migrate(self):
        return []

    def close(self):
        pass


def run_cli(monkeypatch, wh, *argv):
    from price_panel import __main__ as cli

    monkeypatch.setattr(cli, "Warehouse", lambda: wh)
    monkeypatch.setattr(cli, "load_settings", lambda path: SETTINGS)
    monkeypatch.setattr(parse, "parse_in_browser", scripted_browser({}))
    return cli.main(["parse", *argv])


def test_missing_today_parses_only_what_is_not_collected(monkeypatch):
    wh = QueueWarehouse(["1", "2", "3"], collected_today={"2"})
    assert run_cli(monkeypatch, wh, "--kind", "daily", "--missing-today") == 0
    assert wh.products == ["1", "3"]
    assert wh.runs[1]["total"] == 2
    # Граница - полночь по поясу расписания.
    assert wh.since is not None and wh.since.utcoffset() == dt.timedelta(hours=3)
    assert (wh.since.hour, wh.since.minute) == (0, 0)


def test_missing_today_with_everything_collected_is_success(monkeypatch):
    wh = QueueWarehouse(["1", "2"], collected_today={"1", "2"})
    assert run_cli(monkeypatch, wh, "--missing-today") == 0
    assert wh.runs == {}


def test_missing_today_needs_panel(monkeypatch):
    assert run_cli(monkeypatch, QueueWarehouse(["1"]), "--missing-today", "1") == 2

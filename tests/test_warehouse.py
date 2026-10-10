"""Хранилище конвейера: миграции, panel, история цен, учёт прогонов.

Проверки без базы сверяют SQL и подготовку параметров. Проверки с базой
запускаются, если задан TEST_PG_DSN, и только на базе, в имени которой есть
"test": фикстура wh (conftest.py) каждый раз пересоздаёт схему public.

    TEST_PG_DSN=postgresql://ozon:ozon@localhost:5433/ozon_test pytest tests/test_warehouse.py
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
from pathlib import Path

import pytest

from price_panel.core.sampling import GROUP_TAIL, GROUP_TOP, Candidate, PanelPick
from price_panel.infra import warehouse
from price_panel.infra.skus import read_skus_file
from price_panel.infra.warehouse import Warehouse

TEST_DSN = os.getenv("TEST_PG_DSN", "")

PRODUCT = {
    "sku": "2359066702",
    "source": "api",
    "title": "Раскраска по номерам",
    "price": 1463.0,
    "card_price": 1317.0,
    "old_price": 7990.0,
    "discount_pct": 81.69,
    "is_available": True,
    "rating": 4.9,
    "reviews_total": 1627,
    "cover_image": "https://ir.ozone.ru/cover.jpg",
    "photos_seller": 19,
    "videos_seller": 2,
    "color": "Темно-розовый",
    "material": "Бумага",
    "art_set": "Раскраска",
    "has_rich_content": True,
    "details": True,
}


# ------------------------------------------------------------- без базы ----
def test_migrations_are_ordered_and_unique():
    files = warehouse.migration_files()
    versions = [path.stem for path in files]
    assert versions[0].startswith("0001_")
    assert versions == sorted(versions)
    assert len({v[:4] for v in versions}) == len(versions), "номера миграций повторяются"


def test_history_key_matches_schema():
    """Upsert истории опирается на ключ (run_id, sku), который задан в схеме."""
    schema = (warehouse.MIGRATIONS_DIR / "0001_pipeline_schema.sql").read_text(encoding="utf-8")
    assert "UNIQUE (run_id, sku)" in schema
    assert "ON CONFLICT (run_id, sku)" in warehouse.PRICE_UPSERT
    assert "WHERE NOT sku_panel.is_active" in warehouse.PANEL_UPSERT


def test_html_source_does_not_claim_missing_rich_content():
    """В HTML нет описания: False оттуда - «не знаем», а не «нет rich-контента»."""
    at = dt.datetime(2026, 9, 29, tzinfo=dt.UTC)
    api = warehouse.product_params(7, PRODUCT, at)
    html = warehouse.product_params(
        7, dict(PRODUCT, source="html", has_rich_content=False, details=False), at
    )
    assert api["has_rich_content"] is True and api["run_id"] == 7
    assert html["has_rich_content"] is None
    assert api["collected_at"] == at


def test_slow_attributes_wait_for_the_description():
    """Без второй части карточки описание и характеристики не перезаписываются.

    None не затирает известное в products (COALESCE), а цена из той же записи
    в историю попадает как обычно.
    """
    at = dt.datetime(2026, 10, 6, tzinfo=dt.UTC)
    without = warehouse.product_params(7, dict(PRODUCT, source="html", details=False), at)
    assert [without[name] for name in warehouse.DETAIL_FIELDS] == [None] * 4
    assert (without["price"], without["title"]) == (1463.0, "Раскраска по номерам")

    full = warehouse.product_params(7, dict(PRODUCT, source="html", details=True), at)
    assert (full["has_rich_content"], full["art_set"], full["color"]) == (
        True,
        "Раскраска",
        "Темно-розовый",
    )


# ------------------------------------------------------------- с базой -----
def picks(*skus, group=GROUP_TOP, page=1):
    return [
        PanelPick(Candidate(sku=s, position=i + 1, page=page), group) for i, s in enumerate(skus)
    ]


def test_migrate_is_idempotent(wh):
    assert wh.migrate() == []


def test_panel_is_not_reshuffled_by_repeated_discovery(wh):
    run_id = wh.start_discovery_run("phones", "ozon_listing", "seed")
    assert (
        wh.add_to_panel(
            picks("1", "2") + picks("3", group=GROUP_TAIL, page=90),
            "phones",
            "ozon_listing",
            run_id,
        )
        == 3
    )
    # Тот же набор повторно - ни одной новой строки, группы не меняются.
    assert (
        wh.add_to_panel(picks("1", "2", "3", group=GROUP_TAIL), "phones", "ozon_listing", run_id)
        == 0
    )
    assert wh.panel_counts("phones") == {GROUP_TOP: 2, GROUP_TAIL: 1}
    assert wh.active_panel_skus() == {"1", "2", "3"}


def test_rebuild_deactivates_and_reactivates(wh):
    wh.add_to_panel(picks("1", "2"), "phones", "ozon_listing", None)
    assert wh.deactivate_category("phones") == 2
    assert wh.panel_counts("phones") == {}
    assert wh.add_to_panel(picks("2", "3"), "phones", "ozon_listing", None) == 2
    assert set(wh.panel_skus(["phones"])) == {"2", "3"}
    summary = {(row[0], row[1]): (row[2], row[3]) for row in wh.panel_summary()}
    assert summary[("phones", GROUP_TOP)] == (2, 1)


def test_panel_skus_filters_and_limits(wh):
    wh.add_to_panel(picks("1", "2", "3"), "phones", "ozon_listing", None)
    wh.add_to_panel(picks("4", "5"), "cases", "ozon_listing", None)
    assert set(wh.panel_skus(["cases"])) == {"4", "5"}
    assert len(wh.panel_skus(limit=2)) == 2
    assert wh.panel_skus() == wh.panel_skus(), "порядок должен быть стабильным"


def test_history_grows_between_runs_but_not_within_one(wh):
    first = wh.start_parse_run("manual", "panel", 1, 3.0)
    day1 = dt.datetime(2026, 9, 28, 3, 0, tzinfo=dt.UTC)
    wh.record_product(first, PRODUCT, day1)
    wh.record_product(first, dict(PRODUCT, price=1500.0), day1)  # повтор в том же прогоне

    second = wh.start_parse_run("daily", "panel", 1, 3.0)
    day2 = dt.datetime(2026, 9, 29, 3, 0, tzinfo=dt.UTC)
    # Запасной путь (HTML): пустые характеристики не затирают известные.
    wh.record_product(
        second,
        dict(
            PRODUCT, price=1400.0, source="html", color=None, has_rich_content=False, details=False
        ),
        day2,
    )

    assert wh.history_counts() == (2, 1, 2)

    def daily(cursor):
        cursor.execute(
            "SELECT observed_date, price, prev_price, price_change, price_change_pct "
            "FROM v_price_daily ORDER BY observed_date"
        )
        return cursor.fetchall()

    rows = wh.run(daily)
    assert [float(r[1]) for r in rows] == [1500.0, 1400.0]
    assert rows[1][2] is not None and float(rows[1][3]) == -100.0
    assert float(rows[1][4]) == -6.67

    def product(cursor):
        cursor.execute("SELECT color, has_rich_content, last_run_id FROM products")
        return cursor.fetchone()

    assert wh.run(product) == ("Темно-розовый", True, second)


def test_errors_and_run_totals_are_recorded(wh):
    run_id = wh.start_parse_run("manual", "file", 2, 3.0)
    wh.record_error(run_id, "0000000000", "not_found", "HTTP 404", 1)
    wh.finish_parse_run(run_id, "partial", 2, 1, 1, 61.23, 1.96, 20.5)

    def read(cursor):
        cursor.execute("SELECT sku, error_type, attempts FROM parse_errors")
        errors = cursor.fetchall()
        cursor.execute("SELECT status, success_pct, duration_seconds FROM v_parse_runs")
        return errors, cursor.fetchone()

    errors, run = wh.run(read)
    assert errors == [("0000000000", "not_found", 1)]
    assert run[0] == "partial" and float(run[1]) == 50.0 and float(run[2]) == 61.2


def test_error_counts_by_type_for_notification(wh):
    run_id = wh.start_parse_run("daily", "panel", 4, 3.0)
    other = wh.start_parse_run("daily", "panel", 1, 3.0)
    wh.record_error(run_id, "1", "timeout", "таймаут", 3)
    wh.record_error(run_id, "2", "blocked", "прогон остановлен", None)
    wh.record_error(run_id, "3", "blocked", "прогон остановлен", None)
    wh.record_error(other, "4", "not_found", "HTTP 404", 1)
    assert wh.error_counts(run_id) == [("blocked", 2), ("timeout", 1)]


def test_parse_lock_is_exclusive_and_stale_runs_are_closed(wh):
    stale = wh.start_parse_run("daily", "panel", 5, 3.0)
    other = Warehouse(TEST_DSN)
    try:
        assert wh.try_parse_lock() is True
        assert other.try_parse_lock() is False
        assert wh.close_stale_runs() == 1
        wh.close()  # соединение закрыто - блокировка снята
        assert other.try_parse_lock() is True
    finally:
        other.close()

    def status(cursor):
        cursor.execute("SELECT status FROM parse_runs WHERE run_id = %s", (stale,))
        return cursor.fetchone()[0]

    assert wh.run(status) == "interrupted"


def test_stale_run_totals_are_recovered_from_per_sku_rows(wh):
    """Прогон, убитый вместе с компьютером, не остаётся с нулями в итогах."""
    run_id = wh.start_parse_run("daily", "panel", 1200, 3.0)
    late = dt.datetime(2026, 9, 30, 20, 19, tzinfo=dt.UTC)
    wh.record_product(run_id, PRODUCT, late - dt.timedelta(hours=1))
    wh.record_error(run_id, "111", "fetch_error", "403", 3)
    wh.record_error(run_id, "222", "fetch_error", "403", 3)
    assert wh.close_stale_runs() == 1

    def totals(cursor):
        cursor.execute(
            "SELECT status, success_count, error_count, processed_count "
            "FROM parse_runs WHERE run_id = %s",
            (run_id,),
        )
        return cursor.fetchone()

    assert wh.run(totals) == ("interrupted", 1, 2, 3)


def test_parse_queue_starts_with_least_recently_attempted(wh):
    """Обрывающиеся прогоны не должны обходить одни и те же SKU каждый день."""
    wh.add_to_panel(picks("a", "b", "c", "d", "e"), "phones", "ozon_listing", None)
    run_id = wh.start_parse_run("daily", "panel", 5, 3.0)
    day1 = dt.datetime(2026, 10, 1, 15, 0, tzinfo=dt.UTC)
    wh.record_product(run_id, dict(PRODUCT, sku="a"), day1)  # успех вчера
    wh.record_product(run_id, dict(PRODUCT, sku="b"), day1 - dt.timedelta(days=1))  # позавчера
    wh.record_error(run_id, "c", "antibot", "антибот-проверка не прошла", 1)  # попытка сейчас
    wh.record_error(run_id, "d", "blocked", "прогон остановлен", None)  # не дошли

    queue = wh.panel_skus()
    # d и e ещё ни разу не пробовали (blocked - не попытка) - они первые;
    # затем b (позавчера), a (вчера), c (только что).
    assert set(queue[:2]) == {"d", "e"}
    assert queue[2:] == ["b", "a", "c"]
    assert wh.panel_skus(limit=2) == queue[:2]


def test_parse_queue_can_skip_skus_collected_since(wh):
    """Повтор после блокировки добирает только то, чего за день ещё нет."""
    wh.add_to_panel(picks("a", "b", "c"), "phones", "ozon_listing", None)
    run_id = wh.start_parse_run("daily", "panel", 3, 3.0)
    today = dt.datetime(2026, 10, 5, 6, 0, tzinfo=dt.UTC)
    wh.record_product(run_id, dict(PRODUCT, sku="a"), today)  # сегодня
    wh.record_product(run_id, dict(PRODUCT, sku="b"), today - dt.timedelta(days=1))  # вчера
    wh.record_error(run_id, "c", "antibot", "антибот-проверка не прошла", 1)  # ошибка
    midnight = dt.datetime(2026, 10, 4, 21, 0, tzinfo=dt.UTC)  # 00:00 МСК
    assert set(wh.panel_skus(missing_since=midnight)) == {"b", "c"}
    assert set(wh.panel_skus(["phones"], missing_since=midnight)) == {"b", "c"}
    assert len(wh.panel_skus()) == 3


def test_first_connection_failure_is_not_retried(monkeypatch):
    """Неверный пароль - не «потерянное соединение»: без повтора и без такого лога."""
    import psycopg2

    attempts: list = []

    def refuse(dsn):
        attempts.append(dsn)
        raise psycopg2.OperationalError('password authentication failed for user "user"')

    monkeypatch.setattr(psycopg2, "connect", refuse)
    store = Warehouse("postgresql://user:secret@127.0.0.1:5433/ozon")
    with pytest.raises(warehouse.db.DatabaseError, match="password authentication failed"):
        store.run(lambda cursor: None)
    assert len(attempts) == 1


def test_exported_panel_is_readable_by_legacy_parser(wh):
    """CSV из discover годится для parse_ozon.py --file (старый сценарий)."""
    wh.add_to_panel(picks("111111", "222222"), "phones", "ozon_listing", None)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "panel_skus.csv"
        assert wh.export_panel_csv(path) == 2
        assert path.read_text(encoding="utf-8").splitlines()[0] == "sku"
        assert sorted(read_skus_file(path)) == ["111111", "222222"]

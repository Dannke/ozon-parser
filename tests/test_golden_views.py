"""Эталон аналитических представлений на фиксированном наборе наблюдений.

Фиксирует, что отвечают v_price_*, v_category_daily, v_panel_summary и
v_parse_runs сейчас, чтобы пересборка схемы (новые колонки, ключи,
пересоздание представлений) не изменила ответы молча. Нужна тестовая база
(TEST_PG_DSN, см. conftest.py); эталон - tests/golden/views.json.

В наборе есть то, на чём представления легко сломать: два наблюдения SKU за
один день по Москве (берётся последнее), наблюдение после полуночи по Москве,
но до полуночи по UTC, пропущенный из-за ошибки день и товар, ушедший из
наличия.
"""

from __future__ import annotations

import datetime as dt

from price_panel.sampling import GROUP_TAIL, GROUP_TOP, Candidate, PanelPick

UTC = dt.timezone.utc


def observation(sku: str, price: float, old_price=None, available: bool = True,
                reviews: int = 10) -> dict:
    discount = round(100 * (old_price - price) / old_price, 2) if old_price else None
    return {
        "sku": sku, "source": "api", "title": "Товар {}".format(sku), "price": price,
        "card_price": None, "old_price": old_price, "discount_pct": discount,
        "is_available": available, "rating": 4.5, "reviews_total": reviews,
        "cover_image": None, "photos_seller": 3, "videos_seller": 0, "color": None,
        "material": None, "art_set": None, "has_rich_content": False, "details": True,
    }


def fill(wh) -> None:
    panel = [PanelPick(Candidate(sku="1", position=1, page=1), GROUP_TOP),
             PanelPick(Candidate(sku="2", position=2, page=1), GROUP_TOP)]
    wh.add_to_panel(panel, "phones", "ozon_listing", None)
    wh.add_to_panel([PanelPick(Candidate(sku="3", position=721, page=90), GROUP_TAIL)],
                    "cases", "ozon_listing", None)

    day1 = wh.start_parse_run("daily", "panel", 3, 3.0)
    at = dt.datetime(2026, 9, 28, 3, 0, tzinfo=UTC)            # 06:00 МСК 28.09
    wh.record_product(day1, observation("1", 100.0, old_price=150.0, reviews=10), at)
    wh.record_product(day1, observation("2", 200.0), at)
    wh.record_product(day1, observation("3", 50.0, old_price=80.0), at)
    wh.finish_parse_run(day1, "success", 3, 3, 0, 600.0, 0.3, 4.0)

    day2 = wh.start_parse_run("daily", "panel", 3, 3.0)
    at = dt.datetime(2026, 9, 29, 3, 0, tzinfo=UTC)
    wh.record_product(day2, observation("1", 110.0, old_price=150.0, reviews=12), at)
    wh.record_error(day2, "2", "fetch_error", "API ответил HTTP 500", 2)
    wh.record_product(day2, observation("3", 40.0, old_price=80.0), at)
    wh.finish_parse_run(day2, "partial", 3, 2, 1, 700.0, 0.26, 4.5)

    # 21:30 UTC 29.09 - это уже 00:30 МСК 30.09: наблюдение относится к 30.09.
    late = wh.start_parse_run("manual", "args", 1, 3.0)
    wh.record_product(late, observation("1", 115.0, old_price=150.0, reviews=13),
                      dt.datetime(2026, 9, 29, 21, 30, tzinfo=UTC))
    wh.finish_parse_run(late, "success", 1, 1, 0, 10.0, 6.0, 4.0)

    # Тот же московский день 30.09, позже - в v_price_daily побеждает это наблюдение.
    day3 = wh.start_parse_run("daily", "panel", 3, 3.0)
    at = dt.datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
    wh.record_product(day3, observation("1", 120.0, old_price=150.0, reviews=15), at)
    wh.record_product(day3, observation("2", 190.0), at)
    wh.record_product(day3, observation("3", 40.0, old_price=80.0, available=False), at)
    wh.finish_parse_run(day3, "success", 3, 3, 0, 650.0, 0.28, 4.2)


def test_views_answer_as_before(wh, select, golden):
    fill(wh)
    golden("views", {
        "v_price_daily": select("SELECT * FROM v_price_daily ORDER BY sku, observed_date"),
        "v_price_latest": select("SELECT * FROM v_price_latest ORDER BY sku"),
        "v_price_volatility": select("SELECT * FROM v_price_volatility ORDER BY sku"),
        "v_category_daily": select(
            "SELECT * FROM v_category_daily ORDER BY category, observed_date"),
        "v_panel_summary": select(
            "SELECT * FROM v_panel_summary ORDER BY category, sampling_group"),
        # Время запуска и окончания ставит now() - в эталон не идёт.
        "v_parse_runs": select(
            "SELECT run_id, kind, sku_source, status, total_sku, processed_count, "
            "success_count, error_count, success_pct, duration_seconds, sku_per_minute, "
            "avg_sku_seconds, request_delay FROM v_parse_runs ORDER BY run_id"),
    })

"""Сквозной ежедневный прогон: CLI parse -> браузер -> подменённый Ozon -> PostgreSQL.

Характеризационный тест: смотрит только на вход (команда, panel, ответы
сайта) и выход (код возврата и строки в базе), а не на внутренности. Поэтому
он должен пройти без изменений через переименование и перестройку пакета - и
поймать любое изменение поведения ежедневного сбора. Эталон -
tests/golden/e2e_daily.json.

Нужны тестовая база (TEST_PG_DSN, см. conftest.py) и браузер Playwright.
Запросы к ozon.ru обслуживает заглушка: в сеть тест не ходит.
"""

from __future__ import annotations

import json
import re
import urllib.parse

from test_extract import FIXTURE, FIXTURE_MINIMAL

from price_panel import __main__ as cli
from price_panel import browser as browser_utils
from price_panel import config
from price_panel.sampling import GROUP_TOP, Candidate, PanelPick

CARDS = {"1001": FIXTURE, "1002": FIXTURE_MINIMAL}
MISSING = "404404"
PAGE = "<html><head><title>Ozon</title></head><body>stub</body></html>"

# csv_export пустой: иначе прогон переписал бы data/panel_products.csv рабочего дерева.
CONFIG = """
discovery:
  categories:
    - name: phones
      url: https://www.ozon.ru/category/smartfony-15502/
      panel_size: 3
parser:
  csv_export: ""
  min_success_rate: 0.5
  details_refresh_days: 1
"""


def ozon_stub(route, request) -> None:
    """Ответы «Ozon»: карточки из CARDS, для MISSING - HTTP 404."""
    url = urllib.parse.unquote(request.url)
    found = re.search(r"/product/(\d+)/", url)
    sku = found.group(1) if found else None
    if "entrypoint-api.bx" in url:
        card = CARDS.get(sku or "")
        route.fulfill(status=200 if card else 404, content_type="application/json",
                      body=json.dumps(card or {}, ensure_ascii=False))
    else:
        route.fulfill(status=404 if sku == MISSING else 200,
                      content_type="text/html; charset=utf-8", body=PAGE)


def test_daily_run_writes_history_errors_and_accounting(wh, select, golden, monkeypatch,
                                                         tmp_path):
    original = browser_utils.new_context

    def stubbed_context(browser, storage_state=None):
        context = original(browser, storage_state=storage_state)
        context.route("**/*", ozon_stub)
        return context

    monkeypatch.setattr(browser_utils, "new_context", stubbed_context)
    for name, value in {"PG_DSN": wh.dsn, "HEADLESS": True, "REQUEST_DELAY": 0.0,
                        "PAGE_SETTLE_MS": 0, "COOKIES_FILE": tmp_path / "cookies.json"}.items():
        monkeypatch.setattr(config, name, value)
    settings = tmp_path / "config.yaml"
    settings.write_text(CONFIG, encoding="utf-8")
    wh.add_to_panel([PanelPick(Candidate(sku=sku, position=i + 1, page=1), GROUP_TOP)
                     for i, sku in enumerate(["1001", "1002", MISSING])],
                    "phones", "ozon_listing", None)

    daily = ["--config", str(settings), "parse", "--kind", "daily", "--missing-today"]
    first = cli.main(daily)
    # Повтор за тот же день берёт только SKU без успешного наблюдения - здесь
    # это карточка, которой нет: прогон без единого успеха, код 1.
    second = cli.main(daily)

    golden("e2e_daily", {
        "exit_codes": [first, second],
        "parse_runs": select(
            "SELECT run_id, kind, sku_source, status, total_sku, processed_count, "
            "success_count, error_count, request_delay FROM parse_runs ORDER BY run_id"),
        "price_history": select(
            "SELECT run_id, sku, price, card_price, old_price, discount_pct, is_available, "
            "rating, reviews_total, source FROM price_history ORDER BY run_id, sku"),
        "products": select(
            "SELECT sku, title, cover_image, color, material, art_set, has_rich_content, "
            "photos_seller, videos_seller, last_run_id FROM products ORDER BY sku"),
        "parse_errors": select(
            "SELECT run_id, sku, error_type, attempts FROM parse_errors ORDER BY run_id, sku"),
    })

"""Wildberries: разбор карточек u-card v4 и батчи адаптера.

Сеть и браузер не нужны: карточки - сохранённый ответ сайта
(tests/fixtures/wb/u_card_v4_detail.json, 11.10.2026), запрос сайта
подменяется функцией fetch(url) -> (HTTP-статус, тело).
"""

from __future__ import annotations

import json
import urllib.parse
from decimal import Decimal
from pathlib import Path

import pytest

from price_panel.core.collect import Breaker, RunObserver, collect
from price_panel.core.quality import problems
from price_panel.infra import config
from price_panel.marketplaces.wb import adapter as wb
from price_panel.marketplaces.wb.extract import cards_by_sku, product_record

FIXTURE = Path(__file__).parent / "fixtures" / "wb" / "u_card_v4_detail.json"
BODY = json.loads(FIXTURE.read_text(encoding="utf-8"))
CARDS = cards_by_sku(BODY)
SITE = wb.SiteRequest(
    url="https://www.wildberries.ru" + wb.DETAIL_PATH + "?appType=1&curr=rub&dest=1259570991"
    "&spp=30&lang=ru&nm=1343721205",
    headers={"deviceid": "site_1", "x-spa-version": "14.28.1"},
)


def record(sku: str) -> dict:
    return product_record(CARDS[sku])


# ----------------------------------------------------------------- разбор --
@pytest.mark.parametrize(
    ("sku", "price", "old_price", "discount"),
    [
        # Цены на странице 11.10.2026: 7099 / 13148, 12695 / 70530, 13906 / 34000.
        ("1343721205", "7099", "13148", "46.01"),
        ("1468131190", "12695", "70530", "82.00"),
        ("1650108423", "13906", "34000", "59.10"),
    ],
)
def test_price_is_what_the_page_shows(sku, price, old_price, discount):
    product = record(sku)
    assert product["price"] == Decimal(price)
    assert product["old_price"] == Decimal(old_price)
    assert product["discount_pct"] == Decimal(discount)
    # Цену с WB Кошельком считает сайт, в ответе её нет.
    assert product["card_price"] is None
    assert product["is_available"] is True
    assert problems(product) == ""


def test_card_fields_follow_the_common_contract():
    product = record("1468131190")
    assert product["sku"] == "1468131190" and "Infinix Note 60 Pro" in product["title"]
    assert product["color"] == "серый" and product["photos_seller"] == 17
    assert (product["rating"], product["reviews_total"]) == (5, 12)
    assert product["source"] == "api" and product["details"] is True
    # Сырой ответ - без служебных полей, но с ценами размеров.
    assert "logs" not in product["raw"] and product["raw"]["sizes"][0]["price"]["product"]


def test_out_of_stock_card_has_no_price_and_is_valid():
    product = record("1149093062")
    assert product["price"] is None and product["old_price"] is None
    assert product["is_available"] is False
    assert problems(product) == ""


def test_rating_without_reviews_is_unknown_not_zero():
    product = record("1506835913")
    assert product["rating"] is None and product["reviews_total"] == 0


def test_strikethrough_not_above_price_is_dropped():
    card = json.loads(json.dumps(CARDS["1343721205"]))
    card["sizes"][0]["price"]["basic"] = card["sizes"][0]["price"]["product"]
    product = product_record(card)
    assert product["old_price"] is None and product["discount_pct"] is None


def test_cheapest_size_with_price_wins():
    card = json.loads(json.dumps(CARDS["1343721205"]))
    cheaper = json.loads(json.dumps(card["sizes"][0]))
    cheaper["price"]["product"] = 500000
    card["sizes"] = [{"price": None}, card["sizes"][0], cheaper]
    assert product_record(card)["price"] == Decimal("5000")


def test_cards_by_sku_reads_both_answer_shapes():
    assert set(cards_by_sku({"data": {"products": [{"id": 1}]}})) == {"1"}
    assert cards_by_sku({"products": None}) == {}


# ----------------------------------------------------------------- батчи --
def test_batch_url_keeps_site_query_and_sets_region_and_skus():
    url = wb.batch_url(SITE.url, "-365341", ["1", "2", "3"])
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert query["dest"] == "-365341" and query["nm"] == "1;2;3"
    assert query["appType"] == "1" and query["spp"] == "30"
    assert "nm=1;2;3" in url and "dest=-365341" in url


def test_own_headers_drop_what_the_browser_sets_itself():
    headers = {
        ":authority": "www.wildberries.ru",
        "cookie": "x",
        "user-agent": "ua",
        "sec-ch-ua": "x",
        "deviceid": "site_1",
        "x-requested-with": "XMLHttpRequest",
    }
    assert wb.own_headers(headers) == {"deviceid": "site_1", "x-requested-with": "XMLHttpRequest"}


class FakeSite:
    """Сайт WB: на каждый запрос - следующий ответ сценария, (статус, тело)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.urls: list = []

    def __call__(self, url: str) -> tuple:
        self.urls.append(url)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


def adapter(batch_size=3, sleeps=None) -> wb.WbAdapter:
    return wb.WbAdapter(
        "-365341", batch_size, 5.0, sleep=(sleeps.append if sleeps is not None else lambda s: None)
    )


def answer(*skus) -> tuple:
    return 200, json.dumps({"products": [CARDS[sku] for sku in skus]})


def test_batches_split_skus_and_pause_between_requests():
    skus = ["1343721205", "1468131190", "1506835913", "1650108423", "404"]
    site = FakeSite(answer(*skus[:3]), answer("1650108423"))
    sleeps: list = []
    outcomes = list(adapter(sleeps=sleeps).batches(skus, SITE, site))

    assert [o.sku for o in outcomes] == skus
    assert [o.error_type for o in outcomes] == ["", "", "", "", "not_found"]
    assert len(site.urls) == 2 and "nm=1650108423;404" in site.urls[1]
    # Пауза только между батчами: 5 с плюс до 30 % разброса.
    assert len(sleeps) == 1 and 5.0 <= sleeps[0] <= 6.5


def test_invalid_card_is_not_saved():
    card = json.loads(json.dumps(CARDS["1343721205"]))
    card["sizes"][0]["price"]["product"] = 0
    site = FakeSite((200, json.dumps({"products": [card]})))
    (outcome,) = adapter().batches(["1343721205"], SITE, site)
    assert outcome.product is None and outcome.error_type == "invalid_data"


@pytest.mark.parametrize("status", wb.BLOCK_STATUSES)
def test_refusal_is_antibot_for_the_whole_batch_without_retries(status):
    site = FakeSite((status, ""))
    outcomes = list(adapter().batches(["1", "2"], SITE, site))
    assert [o.error_type for o in outcomes] == ["antibot", "antibot"]
    assert len(site.urls) == 1


def test_token_refresh_lets_the_site_renew_and_retries_the_batch():
    """498 - сайт обновляет токен на открытой карточке, батч повторяется."""
    site = FakeSite((498, ""), answer("1343721205"))
    refreshes: list = []
    (outcome,) = adapter().batches(["1343721205"], SITE, site, lambda: refreshes.append(1) or True)
    assert outcome.product is not None and refreshes == [1] and len(site.urls) == 2


def test_token_refresh_that_does_not_help_is_antibot():
    site = FakeSite((498, ""))
    refreshes: list = []
    (outcome,) = adapter().batches(["1"], SITE, site, lambda: refreshes.append(1) or True)
    assert outcome.error_type == "antibot" and len(refreshes) == wb.TOKEN_REFRESHES
    assert len(site.urls) == 1 + wb.TOKEN_REFRESHES
    # Карточка не открылась - повторять батч незачем.
    site = FakeSite((498, ""))
    (outcome,) = adapter().batches(["1"], SITE, site, lambda: False)
    assert outcome.error_type == "antibot" and len(site.urls) == 1


def test_server_errors_are_retried_then_reported(monkeypatch):
    monkeypatch.setattr(config, "MAX_RETRIES", 2)
    site = FakeSite(
        (502, ""), wb.PlaywrightError("net::ERR_CONNECTION_RESET"), answer("1343721205")
    )
    (outcome,) = adapter().batches(["1343721205"], SITE, site)
    assert outcome.product is not None and outcome.attempts == 3

    broken = FakeSite((200, "<html>не JSON</html>"))
    (outcome,) = adapter().batches(["1343721205"], SITE, broken)
    assert outcome.error_type == "fetch_error" and len(broken.urls) == 1


def test_refused_batch_trips_the_breaker_and_stops_collection():
    """Ядро останавливает сбор на первом отказавшем батче - дальше WB не дёргаем."""

    class Adapter(wb.WbAdapter):
        def collect(self, skus):
            yield from self.batches(skus, SITE, site)
            return None

    site = FakeSite((498, ""))
    failed: list = []

    class Observer(RunObserver):
        def sku_failed(self, sku, error_type, message, seconds, attempts=0):
            failed.append(error_type)

    skus = [str(n) for n in range(10)]
    result = collect(
        Adapter("-365341", 3, 5.0, sleep=lambda s: None),
        skus,
        Observer(),
        Breaker(max_failures=0, max_challenges=3),
        wb.log,
    )
    assert result.blocked and len(site.urls) == 1
    assert failed == ["antibot"] * 3 + ["blocked"] * 7

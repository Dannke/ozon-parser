"""Выборка panel: SKU из URL, дедупликация, top + tail_random.

Сеть не нужна: кандидаты строятся вручную так, как их отдаёт discovery.
"""

from __future__ import annotations

import pytest

from price_panel.sampling import (
    GROUP_TAIL,
    GROUP_TOP,
    Candidate,
    dedupe,
    listing_position,
    make_rng,
    pick_tail,
    pick_top,
    plan_tail_pages,
    select_panel,
    sku_from_url,
)


@pytest.mark.parametrize("url, sku", [
    # Формат ссылок листинга, проверенный на живом ozon.ru 29.09.2026.
    ("/product/glaner-smartfon-zashchishchennyy-16-512-gb-nano-sim-chernyy-4952909433/"
     "?at=1gin1PCp9FXKnaJx28JEmZCM0519kVL1", "4952909433"),
    ("https://www.ozon.ru/product/chehol-na-ayfon-16-pro-2060309414/", "2060309414"),
    # Ссылка из data.ozon.ru: без слага и без завершающего слеша.
    ("https://www.ozon.ru/product/149222760", "149222760"),
    ("https://ozon.ru/product/kofe-v-zernah-643726547/#reviews", "643726547"),
    ("/product/2359066702/", "2359066702"),
])
def test_sku_from_product_url(url, sku):
    assert sku_from_url(url) == sku


@pytest.mark.parametrize("url", [
    "",
    "https://www.ozon.ru/category/smartfony-15502/",
    "https://ozon.ru/t/AbCdEf",                         # короткая ссылка
    "https://www.ozon.ru/product/",                     # нет SKU
    "https://example.com/product/phone-4952909433/",    # чужой домен
    "/product/iphone-15/",                              # число в слаге - не SKU
    "/product/some-card-4952909433/reviews/",           # не сама карточка
])
def test_not_a_product_url(url):
    assert sku_from_url(url) is None


def test_listing_position():
    assert listing_position(page=1, index=0, page_size=8) == 1
    assert listing_position(page=3, index=2, page_size=8) == 19


def c(sku, position, page=1):
    return Candidate(sku=str(sku), position=position, page=page)


def test_dedupe_keeps_best_position():
    """Соседние страницы перекрываются: остаётся самое высокое место."""
    result = dedupe([c(1, 5), c(2, 3), c(1, 2), c(3, 9), c(2, 7)])
    assert [(x.sku, x.position) for x in result] == [("1", 2), ("2", 3), ("3", 9)]


def test_plan_tail_pages_is_random_but_reproducible():
    pages = plan_tail_pages(10, 300, 6, make_rng("seed", "phones"))
    assert len(pages) == 6 == len(set(pages))
    assert pages == sorted(pages)
    assert all(10 <= p <= 300 for p in pages)
    assert pages == plan_tail_pages(10, 300, 6, make_rng("seed", "phones"))
    # Другая категория - другие страницы, даже с тем же зерном.
    assert pages != plan_tail_pages(10, 300, 6, make_rng("seed", "cases"))


def test_plan_tail_pages_skips_visited_and_small_range():
    rng = make_rng("s", "c")
    assert plan_tail_pages(2, 5, 10, rng) == [2, 3, 4, 5]
    assert plan_tail_pages(2, 5, 10, rng, exclude=[3]) == [2, 4, 5]
    assert plan_tail_pages(6, 5, 3, rng) == []


def test_pick_top_takes_first_positions_and_skips_known():
    candidates = [c(i, i) for i in range(1, 11)]
    assert [x.sku for x in pick_top(candidates, 3)] == ["1", "2", "3"]
    assert [x.sku for x in pick_top(candidates, 3, exclude={"2"})] == ["1", "3", "4"]


def test_pick_tail_limits_items_per_page():
    """С одной страницы хвоста - не больше per_page товаров."""
    candidates = [c("{}-{}".format(page, i), page * 8 + i, page)
                  for page in (40, 90, 150) for i in range(8)]
    picked = pick_tail(candidates, 6, make_rng("s", "c"), per_page=2)
    pages = [x.page for x in picked]
    assert len(picked) == 6
    assert sorted(pages) == [40, 40, 90, 90, 150, 150]


def test_pick_tail_trims_to_size_randomly():
    candidates = [c("{}-{}".format(page, i), page * 8 + i, page)
                  for page in range(20, 40) for i in range(8)]
    picked = pick_tail(candidates, 5, make_rng("s", "c"), per_page=2)
    assert len(picked) == 5
    assert len({x.sku for x in picked}) == 5


def test_select_panel_mixes_top_and_tail_without_overlap():
    top = [c(i, i, 1) for i in range(1, 9)]
    tail = [c(i, i, (i - 1) // 8 + 1) for i in range(5, 400)]  # хвост задевает зону top
    picks = select_panel(top, tail, top_size=4, tail_size=6, rng=make_rng("s", "c"),
                         per_page=2)
    groups = [p.group for p in picks]
    skus = [p.candidate.sku for p in picks]

    assert groups.count(GROUP_TOP) == 4
    assert groups.count(GROUP_TAIL) == 6
    assert len(set(skus)) == 10, "top и хвост не должны пересекаться"
    assert [p.candidate.sku for p in picks if p.group == GROUP_TOP] == ["1", "2", "3", "4"]


def test_select_panel_respects_existing_panel():
    """SKU, уже стоящие в panel (в том числе другой категории), не берутся повторно."""
    top = [c(i, i) for i in range(1, 9)]
    picks = select_panel(top, [], top_size=3, tail_size=0, rng=make_rng("s", "c"),
                         per_page=2, exclude={"1", "3"})
    assert [p.candidate.sku for p in picks] == ["2", "4", "5"]


def test_select_panel_is_reproducible():
    top = [c(i, i) for i in range(1, 9)]
    tail = [c(i, i, i // 8 + 1) for i in range(9, 800)]

    def run():
        picks = select_panel(top, tail, 3, 7, make_rng("seed", "cat"), per_page=2)
        return [p.candidate.sku for p in picks]

    assert run() == run()


def test_short_pool_gives_what_exists():
    """Если кандидатов не хватило, выборка не придумывает лишнего."""
    picks = select_panel([c(1, 1)], [c(2, 50, 7)], top_size=5, tail_size=5,
                         rng=make_rng("s", "c"), per_page=2)
    assert [(p.candidate.sku, p.group) for p in picks] == [("1", GROUP_TOP), ("2", GROUP_TAIL)]

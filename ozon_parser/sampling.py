"""Формирование panel: SKU из URL, дедупликация и выборка top + tail_random.

Модуль не ходит в сеть - источники (discovery.py) отдают сюда кандидатов в
порядке выдачи, а выборка и все её правила живут отдельно и легко тестируются.

Почему не просто первые N товаров. Верх выдачи - это самые популярные и
продвигаемые карточки; panel только из них показывала бы динамику лидеров, а
не категории. Поэтому panel смешивает две группы:
  * top          - первые позиции выдачи (доля top_ratio);
  * tail_random  - случайные товары со случайных глубоких страниц выдачи.
С одной страницы хвоста берётся не больше tail_items_per_page товаров: так
выборка растягивается по всей глубине, а не собирается с пары страниц.

Это НЕ случайная выборка всех товаров Ozon: кандидаты берутся из
ранжированной выдачи категории, и глубже, чем отдаёт Ozon, заглянуть нельзя.
"""

from __future__ import annotations

import random
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Optional

GROUP_TOP = "top"
GROUP_TAIL = "tail_random"

# SKU - число в конце последнего сегмента пути /product/...:
#   /product/chehol-dlya-iphone-15-1234567890/?at=...  -> 1234567890
#   https://www.ozon.ru/product/149222760               -> 149222760
# Числа внутри слага ("iphone-15") не мешают: берётся хвост сегмента целиком.
PRODUCT_PATH_RE = re.compile(r"^/product/(?:[^/]*-)?(\d{5,})/?$")


@dataclass(frozen=True)
class Candidate:
    """Товар, найденный в выдаче категории."""

    sku: str
    # Оценка места в выдаче: 1 - первая позиция. Для листинга ozon.ru это
    # (страница - 1) * размер страницы + место на странице.
    position: int
    page: int
    url: str = ""


@dataclass(frozen=True)
class PanelPick:
    """Товар, отобранный в panel, и группа выборки, в которую он попал."""

    candidate: Candidate
    group: str


def sku_from_url(url: str) -> Optional[str]:
    """SKU из ссылки на карточку Ozon или None, если это не карточка товара.

    Принимает абсолютные и относительные ссылки, с параметрами и без
    завершающего слеша. Короткие ссылки (/t/...) и чужие домены - None.
    """
    if not url:
        return None
    parts = urllib.parse.urlsplit(url.strip())
    host = parts.netloc.lower()
    if host and host != "ozon.ru" and not host.endswith(".ozon.ru"):
        return None
    match = PRODUCT_PATH_RE.match(parts.path)
    return match.group(1) if match else None


def listing_position(page: int, index: int, page_size: int) -> int:
    """Оценка места товара в выдаче по номеру страницы и месту на ней."""
    return (page - 1) * page_size + index + 1


def dedupe(candidates: Iterable[Candidate]) -> list:
    """Убирает повторы SKU, оставляя самое высокое место в выдаче.

    Повторы - норма: соседние страницы листинга Ozon перекрываются, а
    рекламные карточки встречаются на нескольких страницах.
    """
    best: dict = {}
    for candidate in candidates:
        current = best.get(candidate.sku)
        if current is None or candidate.position < current.position:
            best[candidate.sku] = candidate
    return sorted(best.values(), key=lambda item: (item.position, item.sku))


def make_rng(seed: str, category: str) -> random.Random:
    """Генератор, воспроизводимый для пары (seed, категория)."""
    return random.Random("{}:{}".format(seed, category))


def plan_tail_pages(first: int, last: int, count: int, rng: random.Random,
                    exclude: Iterable[int] = ()) -> list:
    """Случайные номера страниц хвоста из [first, last] без повторов, по возрастанию."""
    skip = set(exclude)
    pool = [page for page in range(first, last + 1) if page not in skip]
    if count >= len(pool):
        return pool
    return sorted(rng.sample(pool, count))


def pick_top(candidates: Iterable[Candidate], size: int, exclude: Iterable[str] = ()) -> list:
    """Первые size товаров выдачи, кроме уже известных."""
    skip = set(exclude)
    return [c for c in dedupe(candidates) if c.sku not in skip][:max(size, 0)]


def pick_tail(candidates: Iterable[Candidate], size: int, rng: random.Random,
              per_page: int, exclude: Iterable[str] = ()) -> list:
    """Случайные товары хвоста: не больше per_page с одной страницы, всего size."""
    if size <= 0:
        return []
    skip = set(exclude)
    by_page: dict = {}
    for candidate in dedupe(candidates):
        if candidate.sku not in skip:
            by_page.setdefault(candidate.page, []).append(candidate)

    picked = []
    for page in sorted(by_page):
        items = by_page[page]
        picked.extend(rng.sample(items, min(per_page, len(items))))
    if len(picked) > size:
        picked = rng.sample(picked, size)
    return sorted(picked, key=lambda item: (item.position, item.sku))


def select_panel(top_candidates: Iterable[Candidate], tail_candidates: Iterable[Candidate],
                 top_size: int, tail_size: int, rng: random.Random, per_page: int,
                 exclude: Iterable[str] = ()) -> list:
    """Собирает panel категории: сначала top, затем хвост без пересечений с ним.

    :param exclude: SKU, которые уже есть в panel (в этой или другой
        категории) - повторно их не берём.
    """
    skip = set(exclude)
    top = pick_top(top_candidates, top_size, skip)
    skip.update(c.sku for c in top)
    tail = pick_tail(tail_candidates, tail_size, rng, per_page, skip)
    return ([PanelPick(c, GROUP_TOP) for c in top]
            + [PanelPick(c, GROUP_TAIL) for c in tail])

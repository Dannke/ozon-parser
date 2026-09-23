"""Извлечение полей карточки товара из JSON-данных Ozon.

Страница товара на ozon.ru собирается из «виджетов». Состояние каждого
виджета - JSON-строка под ключом вида ``webProductHeading-3385933-default-1``.
Эти состояния доступны двумя путями:

  * внутренний API, из которого фронтенд дозагружает страницу:
        /api/entrypoint-api.bx/page/json/v2?url=/product/<sku>/
    ответ - объект, у которого состояния лежат в ``widgetStates``;
  * сам HTML карточки: сервер отдаёт первую часть страницы уже отрисованной,
    и каждый виджет - это ``<div id="state-<ключ>" data-state='{...}'>``.
    Рядом лежит блок ``<script type="application/ld+json">`` со schema.org
    Product (название, цена, рейтинг, картинка).

Оба пути приводятся к одной форме ``{"widgetStates": {...}}`` и разбираются
одним кодом. Разбирать JSON надёжнее, чем DOM: вёрстка и классы у Ozon
меняются часто, а имена виджетов живут заметно дольше. Модуль не делает
сетевых запросов, поэтому логику легко тестировать.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any, Optional

# Порядок колонок в CSV и таблицах БД.
FIELDS = (
    "sku",
    "title",
    "price",
    "rating",
    "reviews_total",
    "cover_image",
    "photos_seller",
    "videos_seller",
    "color",
    "material",
    "art_set",
    "has_rich_content",
)

# Откуда взялись данные карточки. Пишется в БД (в CSV - только 12 полей
# задания): в HTML нет описания и полных характеристик, поэтому у записей
# из него art_set и has_rich_content заведомо пусты.
SOURCE_API = "api"
SOURCE_HTML = "html"

# Имена виджетов, из которых берутся данные (сопоставление по префиксу).
W_HEADING = "webProductHeading"
# Порядок значим: это приоритет источника цены, а не просто список имён.
W_PRICE = ("webPrice", "webSale", "webOutOfStock")
W_SCORE = ("webReviewProductScore", "webSingleProductScore", "webReviewProductScoreV2")
W_GALLERY = "webGallery"
W_CHARACTERISTICS = ("webCharacteristics", "webShortCharacteristics")
W_DESCRIPTION = ("webDescription", "webRichAnnotationJson", "webRichContent")
W_ASPECTS = "webAspects"

# Ключевые слова характеристик для полей color / material / art_set, по
# убыванию приоритета. Характеристика "Артикул" у Ozon - это сам SKU, поэтому
# её здесь нет.
COLOR_KEYS = ("цвет",)
MATERIAL_KEYS = ("материал", "ткань", "состав")
ART_SET_KEYS = (
    "артикул производителя",
    "партномер",
    "код производителя",
    "комплектация",
    "в комплекте",
    "состав комплекта",
)
# Названия, которые содержат ключевое слово, но означают другое:
# "Количество цветов" - не цвет, "Состав комплекта" - не материал.
COLOR_EXCLUDE = ("количество", "число")
MATERIAL_EXCLUDE = ("комплект",)

# Признаки «богатого» описания: картинки, таблицы, списки. Ищем по границе
# тега, чтобы <link> не принимался за <li>.
RICH_HTML_RE = re.compile(r"<(img|table|ul|ol|li|picture|figure)[\s/>]")

# Имена блоков структурного rich-описания (richAnnotationJson). Общие слова
# вроде "image" или "table" не годятся: они встречаются в любом описании.
RICH_JSON_MARKERS = (
    "rapicture", "raimage", "ratable", "ralist", "rashowcase", "racolumns",
    "ragallery", "rabillet", "ratext_block",
)

# Префикс id у встроенных в HTML состояний виджетов.
STATE_ID_PREFIX = "state-"


# ---------------------------------------------------------------- примитивы --
def to_number(value: Any) -> Optional[float]:
    """Превращает '5 990 ₽', '4,8', 1234 в число. Возвращает None, если не вышло."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    # Всё, кроме цифр и разделителей, - валюта и пробелы любых видов.
    cleaned = re.sub(r"[^\d,.\-]", "", value)
    cleaned = cleaned.replace(",", ".").strip(".-")
    if not cleaned:
        return None
    # Если точек несколько, это разделители разрядов ("1.234.567") - убираем.
    if cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def to_int(value: Any) -> Optional[int]:
    """Целое число из строки вида '1 234 отзыва'."""
    number = to_number(value)
    return int(number) if number is not None else None


def text_of(node: Any) -> str:
    """Собирает читаемый текст из разных представлений Ozon.

    Встречаются: обычная строка, {'text': ...}, {'textRs': [{'content': ...}]},
    список таких элементов.
    """
    if node is None or isinstance(node, bool):
        return ""
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, list):
        # Значения Ozon нередко уже содержат хвостовую запятую ("Бежевый,"),
        # поэтому перед склейкой её убираем - иначе получается "Бежевый,, жёлтый".
        parts = [text_of(item).strip().strip(",").strip() for item in node]
        return ", ".join(part for part in parts if part)
    if isinstance(node, dict):
        for key in ("textRs", "text", "content", "title", "value", "name", "searchableText"):
            if key in node:
                text = text_of(node[key])
                if text:
                    return text
    return ""


# ------------------------------------------------------------------ виджеты --
def iter_widgets(page_json: dict) -> Iterator[tuple]:
    """Перебирает виджеты страницы, отдавая пары (имя_без_суффикса, состояние).

    Состояние в ответе - JSON-строка; невалидные значения пропускаются.
    """
    states = (page_json or {}).get("widgetStates") or {}
    for raw_key, raw_value in states.items():
        name = raw_key.split("-", 1)[0]
        if isinstance(raw_value, (dict, list)):
            yield name, raw_value
            continue
        if not isinstance(raw_value, str):
            continue
        try:
            yield name, json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            continue


def widgets_by_name(page_json: dict, names) -> list:
    """Все состояния виджетов, чьё имя начинается с одного из указанных префиксов."""
    if isinstance(names, str):
        names = (names,)
    return [state for name, state in iter_widgets(page_json)
            if any(name.startswith(prefix) for prefix in names)]


# ----------------------------------------------------------------- HTML -----
class _EmbeddedJsonCollector(HTMLParser):
    """Собирает из HTML состояния виджетов и блоки JSON-LD.

    Стандартный HTMLParser сам раскрывает сущности в значениях атрибутов,
    поэтому data-state приходит уже готовой JSON-строкой.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.states: dict = {}
        self.json_ld: list = []
        self._ld_chunks: Optional[list] = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id") or ""
        state = attributes.get("data-state")
        if element_id.startswith(STATE_ID_PREFIX) and state:
            self.states[element_id[len(STATE_ID_PREFIX):]] = state
        if tag == "script" and attributes.get("type") == "application/ld+json":
            self._ld_chunks = []

    def handle_data(self, data: str) -> None:
        if self._ld_chunks is not None:
            self._ld_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._ld_chunks is not None:
            self.json_ld.append("".join(self._ld_chunks))
            self._ld_chunks = None


def _collect_embedded(html: str) -> _EmbeddedJsonCollector:
    collector = _EmbeddedJsonCollector()
    collector.feed(html or "")
    collector.close()
    return collector


def _find_ld_product(node: Any) -> Optional[dict]:
    """Объект schema.org Product внутри JSON-LD (бывает списком или в @graph)."""
    if isinstance(node, list):
        for item in node:
            found = _find_ld_product(item)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None
    if node.get("@type") == "Product":
        return node
    return _find_ld_product(node.get("@graph"))


def embedded_page_json(html: str) -> dict:
    """JSON, встроенный в HTML карточки, в форме ответа API.

    Возвращает ``{"widgetStates": {...}, "jsonLd": {...}}``: состояния
    виджетов из атрибутов data-state и schema.org Product из JSON-LD.
    """
    collected = _collect_embedded(html)
    product_ld: dict = {}
    for block in collected.json_ld:
        try:
            found = _find_ld_product(json.loads(block))
        except json.JSONDecodeError:
            continue
        if found:
            product_ld = found
            break
    return {"widgetStates": collected.states, "jsonLd": product_ld}


# ------------------------------------------------------- отдельные поля ------
def extract_title(page_json: dict) -> Optional[str]:
    """Название товара: из виджета заголовка, при неудаче - из SEO-блока."""
    for state in widgets_by_name(page_json, W_HEADING):
        if isinstance(state, dict):
            title = text_of(state.get("title"))
            if title:
                return title

    seo = (page_json or {}).get("seo") or {}
    title = text_of(seo.get("title"))
    if title:
        # В SEO-заголовке хвост вида " - купить по выгодной цене ..." лишний.
        return re.split(r"\s+(?:-|–|—)\s+купить", title)[0].strip()
    return None


def extract_price(page_json: dict) -> Optional[float]:
    """Актуальная цена.

    Виджеты перебираются в порядке W_PRICE, а не в порядке их появления в
    ответе: webOutOfStock может прийти раньше webPrice и подсунуть старую
    цену. Внутри виджета приоритет ключей - обычная цена, цена с Ozon Картой,
    старая, итоговая. Ноль ценой не считается - так Ozon помечает заглушку.
    """
    for prefix in W_PRICE:
        for state in widgets_by_name(page_json, prefix):
            if not isinstance(state, dict):
                continue
            for key in ("price", "cardPrice", "originalPrice", "finalPrice"):
                price = to_number(state.get(key))
                if price:
                    return price
    return None


def extract_score(page_json: dict) -> tuple:
    """Кортеж (рейтинг, количество отзывов)."""
    rating: Optional[float] = None
    reviews: Optional[int] = None

    for state in widgets_by_name(page_json, W_SCORE):
        if not isinstance(state, dict):
            continue
        if rating is None:
            for key in ("totalScore", "score", "rating"):
                value = to_number(state.get(key))
                # Рейтинг Ozon всегда в диапазоне 0..5 - отсекаем случайные числа.
                if value is not None and 0 < value <= 5:
                    rating = value
                    break
        if reviews is None:
            for key in ("reviewsCount", "totalReviews", "commentsCount", "count"):
                value = to_int(state.get(key))
                if value is not None:
                    reviews = value
                    break
        if rating is not None and reviews is not None:
            break

    return rating, reviews


def _media_url(item: Any, *keys: str) -> Optional[str]:
    """Ссылка на файл из элемента галереи: формат поля менялся не раз."""
    if isinstance(item, str):
        return item
    for key in keys:
        url = (item or {}).get(key)
        if url:
            return url
    return None


def extract_media(page_json: dict) -> tuple:
    """Кортеж (обложка, количество фото, количество видео)."""
    cover: Optional[str] = None
    images: list = []
    videos: list = []

    for state in widgets_by_name(page_json, W_GALLERY):
        if not isinstance(state, dict):
            continue
        images.extend(filter(None, (_media_url(item, "src", "url")
                                    for item in state.get("images") or [])))
        for key in ("videos", "video"):
            videos.extend(filter(None, (_media_url(item, "url", "src")
                                        for item in state.get(key) or [])))
        if cover is None:
            cover = text_of(state.get("coverImage")) or None

    # Один и тот же файл может прийти из нескольких виджетов галереи
    # (основная и мобильная версии) - считаем уникальные.
    images = list(dict.fromkeys(images))
    videos = list(dict.fromkeys(videos))

    if not cover and images:
        cover = images[0]
    if not cover:
        seo = (page_json or {}).get("seo") or {}
        cover = text_of(seo.get("image")) or None

    return cover, len(images), len(videos)


def collect_characteristics(page_json: dict) -> dict:
    """Характеристики товара в виде {название в нижнем регистре: значение}.

    Ozon отдаёт их в нескольких несовместимых формах, поэтому состояние
    обходится рекурсивно: любой узел с полем ``values`` и названием считается
    характеристикой.
    """
    result: dict = {}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return

        if "values" in node:
            name = ""
            for key in ("name", "key", "title"):
                name = text_of(node.get(key))
                if name:
                    break
            value = text_of(node.get("values"))
            if name and value and name.lower() not in result:
                result[name.lower()] = value

        for value in node.values():
            walk(value)

    for state in widgets_by_name(page_json, W_CHARACTERISTICS):
        walk(state)
    return result


def find_characteristic(characteristics: dict, keywords, exclude=()) -> Optional[str]:
    """Значение характеристики по ключевым словам.

    Название сравнивается по убыванию точности: целиком, по началу, по
    вхождению. Так "Цвет" важнее "Цвета рамки", а тот - "Основного цвета".
    Названия, содержащие слово из ``exclude``, не рассматриваются.
    """
    candidates = {name: value for name, value in characteristics.items()
                  if not any(word in name for word in exclude)}
    for matches in (str.__eq__, str.startswith, str.__contains__):
        for keyword in keywords:
            for name, value in candidates.items():
                if matches(name, keyword):
                    return value
    return None


def extract_color(page_json: dict, characteristics: dict) -> Optional[str]:
    """Цвет: из характеристик, иначе из активного варианта в блоке аспектов."""
    color = find_characteristic(characteristics, COLOR_KEYS, exclude=COLOR_EXCLUDE)
    if color:
        return color

    for state in widgets_by_name(page_json, W_ASPECTS):
        if not isinstance(state, dict):
            continue
        for aspect in state.get("aspects") or []:
            if "цвет" not in text_of(aspect.get("title")).lower():
                continue
            for variant in aspect.get("variants") or []:
                if variant.get("active"):
                    value = text_of(variant.get("data")) or text_of(variant)
                    if value:
                        return value
    return None


def extract_has_rich_content(page_json: dict) -> bool:
    """True, если в описании есть изображения, таблицы или списки (rich-контент)."""
    for state in widgets_by_name(page_json, W_DESCRIPTION):
        if not isinstance(state, (dict, list)):
            continue

        if isinstance(state, dict):
            # Структурированное rich-описание: блоки картинок, таблиц, списков.
            rich_json = state.get("richAnnotationJson") or state.get("richAnnotation")
            if isinstance(rich_json, dict) and rich_json.get("content"):
                dump = json.dumps(rich_json, ensure_ascii=False).lower()
                if any(marker in dump for marker in RICH_JSON_MARKERS):
                    return True

        # HTML-описание: ищем теги картинок, таблиц и списков.
        dump = json.dumps(state, ensure_ascii=False).lower()
        # В JSON-строках теги бывают экранированы как < - приводим к обычному виду.
        dump = dump.replace("\\u003c", "<").replace("\\u003e", ">")
        if RICH_HTML_RE.search(dump):
            return True
    return False


# -------------------------------------------------------------- сборка ------
def parse_product(page_json: dict, sku: str) -> dict:
    """Собирает запись о товаре из ответа API (или совместимого с ним JSON)."""
    characteristics = collect_characteristics(page_json)
    rating, reviews_total = extract_score(page_json)
    cover_image, photos_seller, videos_seller = extract_media(page_json)

    return {
        "sku": str(sku),
        "source": SOURCE_API,
        "title": extract_title(page_json),
        "price": extract_price(page_json),
        "rating": rating,
        "reviews_total": reviews_total,
        "cover_image": cover_image,
        "photos_seller": photos_seller,
        "videos_seller": videos_seller,
        "color": extract_color(page_json, characteristics),
        "material": find_characteristic(characteristics, MATERIAL_KEYS,
                                        exclude=MATERIAL_EXCLUDE),
        "art_set": find_characteristic(characteristics, ART_SET_KEYS),
        "has_rich_content": extract_has_rich_content(page_json),
    }


def parse_html(html: str, sku: str) -> dict:
    """Собирает запись о товаре из JSON, встроенного в HTML карточки.

    Состояния виджетов разбираются тем же кодом, что и ответ API. Чего в них
    не нашлось (название, цена, рейтинг, обложка), добирается из JSON-LD.
    """
    page_json = embedded_page_json(html)
    product = parse_product(page_json, sku)
    product["source"] = SOURCE_HTML

    ld = page_json["jsonLd"]
    offers = ld.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    rating = ld.get("aggregateRating") or {}
    image = ld.get("image")
    if isinstance(image, list):
        image = image[0] if image else None

    fallbacks = {
        "title": text_of(ld.get("name")) or None,
        "price": to_number(offers.get("price")) or None,
        "rating": to_number(rating.get("ratingValue")),
        "reviews_total": to_int(rating.get("reviewCount")),
        "cover_image": image if isinstance(image, str) else None,
    }
    for field, value in fallbacks.items():
        if product.get(field) is None and value is not None:
            product[field] = value
    return product

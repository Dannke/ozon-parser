"""Проверка разбора полей карточки на фикстурах ответа Ozon и HTML карточки.

Сетевых запросов не делает, поэтому гоняется в любой среде:
    pytest tests/test_extract.py
"""

from __future__ import annotations

import html
import json

from price_panel.extract import (
    SOURCE_API,
    SOURCE_HTML,
    embedded_page_json,
    find_characteristic,
    parse_html,
    parse_product,
    to_int,
    to_number,
)

# Урезанный, но структурно достоверный ответ /api/entrypoint-api.bx/page/json/v2.
# Значения виджетов в реальном ответе - именно строки с JSON, как здесь.
FIXTURE = {
    "seo": {
        "title": "Кресло офисное, серый - купить по выгодной цене | Ozon",
        "image": "https://cdn1.ozone.ru/s3/seo-cover.jpg",
    },
    "widgetStates": {
        "webProductHeading-3385933-default-1": json.dumps(
            {"title": "Кресло офисное компьютерное, серый"}, ensure_ascii=False
        ),
        "webPrice-3121879-default-1": json.dumps(
            {
                "price": "12 490 ₽",
                "cardPrice": "11 240 ₽",
                "originalPrice": "19 990 ₽",
                "isAvailable": True,
            },
            ensure_ascii=False,
        ),
        "webReviewProductScore-3385934-default-1": json.dumps(
            {"totalScore": "4.8", "reviewsCount": "1 253"}, ensure_ascii=False
        ),
        "webGallery-3311629-default-1": json.dumps(
            {
                "coverImage": "https://cdn1.ozone.ru/s3/multimedia-1/cover.jpg",
                "images": [
                    {"src": "https://cdn1.ozone.ru/s3/multimedia-1/photo-1.jpg"},
                    {"src": "https://cdn1.ozone.ru/s3/multimedia-1/photo-2.jpg"},
                    {"src": "https://cdn1.ozone.ru/s3/multimedia-1/photo-3.jpg"},
                    # Дубль из мобильного виджета - не должен увеличивать счётчик.
                    {"src": "https://cdn1.ozone.ru/s3/multimedia-1/photo-1.jpg"},
                ],
                "videos": [{"url": "https://cdn1.ozone.ru/s3/video/clip.m3u8"}],
            },
            ensure_ascii=False,
        ),
        "webCharacteristics-3231710-default-1": json.dumps(
            {
                "characteristics": [
                    {
                        "title": "Общие",
                        "short": [
                            {"key": "Color", "name": "Цвет товара",
                             "values": [{"text": "серый"}]},
                            {"key": "Material", "name": "Материал обивки",
                             "values": [{"text": "экокожа"}]},
                            {"key": "PartNumber", "name": "Артикул производителя",
                             "values": [{"text": "CH-545-GREY"}]},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        "webDescription-3311644-default-1": json.dumps(
            {
                "richAnnotationJson": {
                    "content": [
                        {"widgetName": "raPicture",
                         "img": {"src": "https://cdn1.ozone.ru/s3/rich/block-1.jpg"}},
                        {"widgetName": "raTextBlock", "text": "Удобное кресло"},
                    ]
                }
            },
            ensure_ascii=False,
        ),
    },
}

# Карточка без rich-описания, без видео и с другой формой виджета рейтинга.
FIXTURE_MINIMAL = {
    "widgetStates": {
        "webProductHeading-1-default-1": json.dumps({"title": "Ручка шариковая"},
                                                    ensure_ascii=False),
        "webSale-2-default-1": json.dumps({"price": "59 ₽"}, ensure_ascii=False),
        "webSingleProductScore-3-default-1": json.dumps({"score": 4.2, "reviewsCount": 7}),
        "webGallery-4-default-1": json.dumps(
            {"images": ["https://cdn1.ozone.ru/s3/pen.jpg"]}, ensure_ascii=False
        ),
        "webDescription-5-default-1": json.dumps(
            {"richAnnotation": "<p>Обычная ручка без картинок</p>"}, ensure_ascii=False
        ),
    }
}


def test_numbers():
    assert to_number("12 490 ₽") == 12490.0
    assert to_number("4,8") == 4.8
    assert to_number("1\u00a0253") == 1253.0
    assert to_number("нет данных") is None
    assert to_int("1 253 отзыва") == 1253


def test_numbers_with_any_digit_spaces():
    """Разряды Ozon разделяет разными юникод-пробелами - понимать надо все.

    Узкий неразрывный пробел (U+202F) в вёрстке встречается чаще всех.
    """
    for space in (" ", " ", " ", " ", " "):
        text = "12{}490 ₽".format(space)
        assert to_number(text) == 12490.0, repr(text)


def test_characteristic_matching_prefers_exact_name():
    """Точное название важнее совпадения по началу, а то - вхождения."""
    chars = {"основной цвет": "синий", "цвет рамки": "чёрный", "цвет": "белый"}
    assert find_characteristic(chars, ("цвет",)) == "белый"
    chars.pop("цвет")
    assert find_characteristic(chars, ("цвет",)) == "чёрный"
    chars.pop("цвет рамки")
    assert find_characteristic(chars, ("цвет",)) == "синий"


def test_characteristic_traps_from_live_cards():
    """Ловушки, найденные на живых карточках Ozon.

    "Артикул" у Ozon - это сам SKU, "Состав комплекта" - не материал,
    "Количество цветов" - не цвет.
    """
    page = {
        "widgetStates": {
            "webCharacteristics-1-default-1": json.dumps(
                {"characteristics": [
                    {"name": "Количество цветов", "values": [{"text": "24"}]},
                    {"name": "Артикул", "values": [{"text": "2359066702"}]},
                    {"name": "Состав комплекта", "values": [{"text": "Раскраска, кисть"}]},
                ]},
                ensure_ascii=False,
            )
        }
    }
    product = parse_product(page, "2359066702")
    assert product["color"] is None
    assert product["material"] is None
    assert product["art_set"] == "Раскраска, кисть"

    only_sku = {"widgetStates": {"webCharacteristics-1-default-1": json.dumps(
        {"characteristics": [{"name": "Артикул", "values": [{"text": "2359066702"}]}]},
        ensure_ascii=False)}}
    assert parse_product(only_sku, "2359066702")["art_set"] is None


def test_multivalue_characteristic():
    """Значения с хвостовыми запятыми склеиваются без задвоения разделителя.

    Реальный случай с ozon.ru: цвет приходит как ['Бежевый,', 'желтый,', 'красный'].
    """
    page = {
        "widgetStates": {
            "webShortCharacteristics-1-default-1": json.dumps(
                {
                    "characteristics": [
                        {
                            "name": "Цвет",
                            "values": [
                                {"text": "Бежевый,"},
                                {"text": "желтый,"},
                                {"text": "красный"},
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        }
    }
    product = parse_product(page, "1")
    assert product["color"] == "Бежевый, желтый, красный", product["color"]


def test_full_card():
    product = parse_product(FIXTURE, "2359066702")

    assert product["sku"] == "2359066702"
    assert product["title"] == "Кресло офисное компьютерное, серый"
    assert product["price"] == 12490.0
    assert product["rating"] == 4.8
    assert product["reviews_total"] == 1253
    assert product["cover_image"].endswith("cover.jpg")
    assert product["photos_seller"] == 3  # дубль отброшен
    assert product["videos_seller"] == 1
    assert product["color"] == "серый"
    assert product["material"] == "экокожа"
    assert product["art_set"] == "CH-545-GREY"
    assert product["has_rich_content"] is True


def test_minimal_card():
    product = parse_product(FIXTURE_MINIMAL, "1111111111")

    assert product["title"] == "Ручка шариковая"
    assert product["price"] == 59.0
    assert product["rating"] == 4.2
    assert product["reviews_total"] == 7
    assert product["cover_image"] == "https://cdn1.ozone.ru/s3/pen.jpg"
    assert product["photos_seller"] == 1
    assert product["videos_seller"] == 0
    assert product["color"] is None
    assert product["has_rich_content"] is False


def test_rich_content_variants():
    """Rich-описание у Ozon бывает двух видов - оба разбираются верно.

    Оба случая сняты с живых карточек: SKU 2359066702 отдаёт структурный
    raShowcase с картинками, SKU 2829800382 - простой текст в HTML.
    """
    showcase = {
        "widgetStates": {
            "webDescription-1-default-1": json.dumps(
                {
                    "richAnnotationType": "JSON",
                    "richAnnotationJson": {
                        "content": [
                            {
                                "widgetName": "raShowcase",
                                "type": "roll",
                                "blocks": [
                                    {"img": {"src": "https://ir.ozone.ru/s3/a.jpg"}},
                                    {"img": {"src": "https://ir.ozone.ru/s3/b.jpg"}},
                                ],
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
        }
    }
    assert parse_product(showcase, "1")["has_rich_content"] is True

    plain = {
        "widgetStates": {
            "webDescription-1-default-1": json.dumps(
                {
                    "richAnnotationType": "HTML",
                    "richAnnotation": "Это не просто книга. Это приключение.",
                },
                ensure_ascii=False,
            )
        }
    }
    assert parse_product(plain, "2")["has_rich_content"] is False


def test_empty_response():
    """На пустом ответе разбор не должен падать - только отдать пустые поля."""
    product = parse_product({}, "0")
    assert product["sku"] == "0"
    assert product["title"] is None
    assert product["price"] is None
    assert product["photos_seller"] == 0
    assert product["has_rich_content"] is False


def test_seo_title_fallback():
    """Если виджета заголовка нет, название берётся из SEO-блока без рекламного хвоста."""
    page = {"seo": FIXTURE["seo"], "widgetStates": {}}
    product = parse_product(page, "2359066702")
    assert product["title"] == "Кресло офисное, серый"
    assert product["cover_image"] == "https://cdn1.ozone.ru/s3/seo-cover.jpg"


def test_price_prefers_actual_widget_over_out_of_stock():
    """Цена выбирается по приоритету виджета, а не по порядку в ответе.

    Регрессия: брался первый подошедший виджет, поэтому пришедший раньше
    webOutOfStock подсовывал старую цену вместо актуальной.
    """
    page = {
        "widgetStates": {
            "webOutOfStock-1-default-1": json.dumps({"price": "19 990 ₽"}, ensure_ascii=False),
            "webPrice-2-default-1": json.dumps({"price": "5 990 ₽"}, ensure_ascii=False),
        }
    }
    assert parse_product(page, "1")["price"] == 5990.0

    # Если актуального виджета нет, цена всё же берётся из запасного.
    only_sale = {"widgetStates": {
        "webSale-1-default-1": json.dumps({"price": "7 490 ₽"}, ensure_ascii=False)}}
    assert parse_product(only_sale, "1")["price"] == 7490.0


def test_rich_content_ignores_link_tag():
    """<link> не должен приниматься за <li>: маркеры ищутся по границе тега."""
    def described(html):
        page = {"widgetStates": {"webDescription-1-default-1": json.dumps(
            {"richAnnotationType": "HTML", "richAnnotation": html}, ensure_ascii=False)}}
        return parse_product(page, "1")["has_rich_content"]

    assert described("<p>Обычный текст</p><link rel='stylesheet'>") is False
    assert described("<ul><li>пункт</li></ul>") is True
    assert described("<img src='https://ir.ozone.ru/s3/a.jpg'>") is True
    assert described("<table><tr><td>1</td></tr></table>") is True


# ---------------------------------------------------- JSON, встроенный в HTML --
def card_html(states: dict, json_ld=None) -> str:
    """HTML карточки в форме, которую отдаёт сервер Ozon.

    Состояние виджета лежит в атрибуте data-state в одинарных кавычках, как
    у Ozon; сущности экранируются так же, как это делает браузер.
    """
    divs = "".join(
        "<div id=\"state-{}\" data-state='{}'></div>".format(
            key, html.escape(json.dumps(value, ensure_ascii=False), quote=True))
        for key, value in states.items()
    )
    script = ""
    if json_ld is not None:
        script = '<script type="application/ld+json">{}</script>'.format(
            json.dumps(json_ld, ensure_ascii=False))
    return "<html><head>{}</head><body>{}</body></html>".format(script, divs)


def test_embedded_widget_states_are_parsed_like_api():
    """Состояния из data-state разбираются тем же кодом, что и ответ API."""
    states = {key: json.loads(value) for key, value in FIXTURE["widgetStates"].items()}
    product = parse_html(card_html(states), "2359066702")

    assert product["source"] == SOURCE_HTML
    assert product["title"] == "Кресло офисное компьютерное, серый"
    assert product["price"] == 12490.0
    assert product["rating"] == 4.8
    assert product["reviews_total"] == 1253
    assert product["photos_seller"] == 3
    assert product["videos_seller"] == 1
    assert product["color"] == "серый"
    assert product["has_rich_content"] is True


def test_embedded_state_survives_quotes_and_markup():
    """Кавычки и теги внутри значений не ломают разбор атрибута."""
    title = "Кресло \"Босс\" <серое> & мягкое"
    page_json = embedded_page_json(card_html({"webProductHeading-1-default-1": {"title": title}}))
    product = parse_product(page_json, "1")
    assert product["title"] == title


def test_json_ld_fills_missing_fields():
    """Чего нет в виджетах, берётся из schema.org Product в JSON-LD."""
    json_ld = {
        "@context": "http://schema.org",
        "@type": "Product",
        "name": "Раскраска по номерам",
        "image": "https://ir.ozone.ru/s3/cover.jpg",
        "offers": {"@type": "Offer", "price": "1672", "priceCurrency": "RUB"},
        "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.9",
                            "reviewCount": "1628"},
    }
    product = parse_html(card_html({}, json_ld), "2359066702")
    assert product["title"] == "Раскраска по номерам"
    assert product["price"] == 1672.0
    assert product["rating"] == 4.9
    assert product["reviews_total"] == 1628
    assert product["cover_image"] == "https://ir.ozone.ru/s3/cover.jpg"


def test_widgets_take_priority_over_json_ld():
    """JSON-LD только дополняет: данные виджетов точнее и не перезаписываются."""
    json_ld = {"@type": "Product", "name": "SEO-название", "offers": {"price": "1"}}
    states = {"webProductHeading-1-default-1": {"title": "Название из виджета"},
              "webPrice-2-default-1": {"price": "5 990 ₽"}}
    product = parse_html(card_html(states, json_ld), "1")
    assert product["title"] == "Название из виджета"
    assert product["price"] == 5990.0


def test_html_without_data_gives_empty_record():
    """Страница без встроенного JSON (например, заглушка) - пустая запись, не исключение."""
    product = parse_html("<html><body><h1>Доступ ограничен</h1></body></html>", "1")
    assert product["title"] is None
    assert set(product) == set(parse_product({}, "1"))
    assert parse_product({}, "1")["source"] == SOURCE_API

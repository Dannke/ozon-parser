-- ===========================================================================
--  Витрины для DataLens поверх таблицы ozon_products.
--
--  Саму таблицу создаёт парсер при первом запуске (см. storage.py), здесь
--  только представления - на них удобно строить датасеты в DataLens:
--
--    v_ozon_products_latest  - текущее состояние, по одной строке на SKU;
--    v_ozon_products_daily   - динамика по дням с изменением цены и отзывов.
--
--  Выберите блок под вашу СУБД. Имя таблицы при необходимости поправьте -
--  оно задаётся переменными PG_TABLE / CH_TABLE в .env.
-- ===========================================================================


-- ===========================  PostgreSQL  ==================================

-- Последний срез по каждому товару: для карточек, таблиц и текущих цен.
CREATE OR REPLACE VIEW v_ozon_products_latest AS
SELECT DISTINCT ON (sku)
    sku,
    parsed_date,
    -- "api" или "html": у карточек, собранных из JSON в HTML страницы,
    -- art_set и has_rich_content заведомо пусты - это не потеря данных.
    source,
    title,
    price,
    rating,
    reviews_total,
    cover_image,
    photos_seller,
    videos_seller,
    color,
    material,
    art_set,
    has_rich_content,
    -- Сколько всего медиа у карточки: удобная метрика "наполненности".
    coalesce(photos_seller, 0) + coalesce(videos_seller, 0) AS media_total
FROM ozon_products
ORDER BY sku, parsed_date DESC;


-- Динамика по дням: изменение цены и прирост отзывов относительно прошлого среза.
-- На этой витрине строятся линейные графики в DataLens.
CREATE OR REPLACE VIEW v_ozon_products_daily AS
SELECT
    sku,
    parsed_date,
    title,
    price,
    rating,
    reviews_total,
    photos_seller,
    videos_seller,
    has_rich_content,
    price - lag(price) OVER w                   AS price_change,
    reviews_total - lag(reviews_total) OVER w   AS reviews_change,
    -- Изменение цены в процентах; nullif защищает от деления на ноль.
    round(
        100.0 * (price - lag(price) OVER w) / nullif(lag(price) OVER w, 0),
        2
    )                                           AS price_change_pct
FROM ozon_products
WINDOW w AS (PARTITION BY sku ORDER BY parsed_date);


-- ===========================  ClickHouse  ==================================
-- Для ClickHouse выполняйте блок ниже вместо блока PostgreSQL.
--
-- CREATE OR REPLACE VIEW v_ozon_products_latest AS
-- SELECT
--     sku,
--     parsed_date,
--     source,
--     title,
--     price,
--     rating,
--     reviews_total,
--     cover_image,
--     photos_seller,
--     videos_seller,
--     color,
--     material,
--     art_set,
--     has_rich_content,
--     ifNull(photos_seller, 0) + ifNull(videos_seller, 0) AS media_total
-- FROM ozon_products FINAL
-- ORDER BY sku, parsed_date DESC
-- LIMIT 1 BY sku;
--
-- CREATE OR REPLACE VIEW v_ozon_products_daily AS
-- SELECT
--     sku,
--     parsed_date,
--     title,
--     price,
--     rating,
--     reviews_total,
--     photos_seller,
--     videos_seller,
--     has_rich_content,
--     price - lagInFrame(price) OVER w                 AS price_change,
--     reviews_total - lagInFrame(reviews_total) OVER w AS reviews_change,
--     round(
--         100.0 * (price - lagInFrame(price) OVER w) / nullIf(lagInFrame(price) OVER w, 0),
--         2
--     )                                                AS price_change_pct
-- FROM ozon_products FINAL
-- WINDOW w AS (PARTITION BY sku ORDER BY parsed_date
--              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW);

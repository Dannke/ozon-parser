-- ===========================================================================
--  Аналитические представления поверх price_history.
--
--  День наблюдения считается по Москве: запуск в 05:30 МСК - это 02:30 UTC,
--  и дата в UTC совпадала бы, но ручной запуск вечером уже нет.
-- ===========================================================================

-- Последнее наблюдение за день по каждому SKU и изменение к прошлому дню
-- наблюдения. Ответ на вопросы «какая цена была вчера / сегодня и насколько
-- она изменилась».
CREATE VIEW v_price_daily AS
WITH daily AS (
    SELECT DISTINCT ON (ph.sku, (ph.collected_at AT TIME ZONE 'Europe/Moscow')::date)
        ph.sku,
        (ph.collected_at AT TIME ZONE 'Europe/Moscow')::date AS observed_date,
        ph.collected_at,
        ph.run_id,
        ph.price,
        ph.card_price,
        ph.old_price,
        ph.discount_pct,
        ph.is_available,
        ph.rating,
        ph.reviews_total
    FROM price_history ph
    ORDER BY ph.sku, (ph.collected_at AT TIME ZONE 'Europe/Moscow')::date, ph.collected_at DESC
)
SELECT
    d.sku,
    sp.category,
    sp.sampling_group,
    p.title,
    d.observed_date,
    d.collected_at,
    d.run_id,
    d.price,
    d.card_price,
    d.old_price,
    d.discount_pct,
    d.is_available,
    d.rating,
    d.reviews_total,
    lag(d.observed_date) OVER w                            AS prev_date,
    lag(d.price) OVER w                                    AS prev_price,
    d.price - lag(d.price) OVER w                          AS price_change,
    round(100.0 * (d.price - lag(d.price) OVER w)
          / nullif(lag(d.price) OVER w, 0), 2)             AS price_change_pct,
    d.reviews_total - lag(d.reviews_total) OVER w          AS reviews_change
FROM daily d
LEFT JOIN sku_panel sp ON sp.sku = d.sku
LEFT JOIN products p ON p.sku = d.sku
WINDOW w AS (PARTITION BY d.sku ORDER BY d.observed_date);

-- Текущее состояние: последнее наблюдение по каждому SKU.
CREATE VIEW v_price_latest AS
SELECT DISTINCT ON (ph.sku)
    ph.sku,
    sp.category,
    sp.sampling_group,
    sp.is_active,
    p.title,
    ph.collected_at,
    ph.price,
    ph.card_price,
    ph.old_price,
    ph.discount_pct,
    ph.is_available,
    ph.rating,
    ph.reviews_total
FROM price_history ph
LEFT JOIN sku_panel sp ON sp.sku = ph.sku
LEFT JOIN products p ON p.sku = ph.sku
ORDER BY ph.sku, ph.collected_at DESC;

-- Какие товары часто меняют цену: число смен цены между днями наблюдения
-- и размах цены за весь период.
CREATE VIEW v_price_volatility AS
SELECT
    sku,
    category,
    sampling_group,
    max(title)                                                    AS title,
    count(*)                                                      AS days_observed,
    count(*) FILTER (WHERE price_change IS NOT NULL
                       AND price_change <> 0)                     AS price_changes,
    min(price)                                                    AS min_price,
    max(price)                                                    AS max_price,
    round(100.0 * (max(price) - min(price)) / nullif(min(price), 0), 2) AS price_range_pct,
    min(observed_date)                                            AS first_date,
    max(observed_date)                                            AS last_date
FROM v_price_daily
GROUP BY sku, category, sampling_group;

-- Динамика цен внутри категории по дням.
CREATE VIEW v_category_daily AS
SELECT
    category,
    observed_date,
    count(*)                                                      AS sku_observed,
    round(avg(price), 2)                                          AS avg_price,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY price)            AS median_price,
    count(*) FILTER (WHERE price_change <> 0)                     AS sku_price_changed,
    round(avg(price_change_pct), 2)                               AS avg_price_change_pct,
    round(avg(discount_pct), 2)                                   AS avg_discount_pct,
    round(100.0 * count(*) FILTER (WHERE is_available) / count(*), 1) AS available_pct
FROM v_price_daily
GROUP BY category, observed_date;

-- Состав panel: сколько SKU в каждой категории и группе выборки.
CREATE VIEW v_panel_summary AS
SELECT
    category,
    sampling_group,
    count(*) FILTER (WHERE is_active)       AS active_sku,
    count(*) FILTER (WHERE NOT is_active)   AS inactive_sku,
    min(position) FILTER (WHERE is_active)  AS min_position,
    max(position) FILTER (WHERE is_active)  AS max_position
FROM sku_panel
GROUP BY category, sampling_group;

-- Запуски парсера с долей успеха.
CREATE VIEW v_parse_runs AS
SELECT
    run_id,
    kind,
    sku_source,
    status,
    started_at,
    finished_at,
    total_sku,
    processed_count,
    success_count,
    error_count,
    round(100.0 * success_count / nullif(total_sku, 0), 1) AS success_pct,
    duration_seconds,
    sku_per_minute,
    avg_sku_seconds,
    request_delay
FROM parse_runs;

-- ===========================================================================
--  Несколько маркетплейсов в одних таблицах (docs/adr/0002-multi-marketplace-data.md).
--
--  У каждой строки появляется маркетплейс - ссылка на справочник marketplaces.
--  Всё, что собрано до этой миграции, - Ozon: другого конвейер не собирал.
--
--  Ключи товара становятся составными (marketplace, sku): артикулы разных
--  маркетплейсов могут совпасть. Составные внешние ключи не дают записать
--  наблюдение, ошибку или карточку в прогон другого маркетплейса.
--
--  DEFAULT 'ozon' нужен только на время миграции, чтобы заполнить историю.
--  Потом он снимается: запись без маркетплейса падает, а не становится «ozon»
--  молча.
-- ===========================================================================

CREATE TABLE marketplaces (
    code   TEXT PRIMARY KEY,
    title  TEXT NOT NULL
);

INSERT INTO marketplaces (code, title) VALUES ('ozon', 'Ozon');

-- --------------------------------------------------------------- колонка ---
ALTER TABLE discovery_runs ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'ozon'
    REFERENCES marketplaces (code);
ALTER TABLE sku_panel ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'ozon'
    REFERENCES marketplaces (code);
ALTER TABLE parse_runs ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'ozon'
    REFERENCES marketplaces (code);
ALTER TABLE products ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'ozon'
    REFERENCES marketplaces (code);
ALTER TABLE price_history ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'ozon'
    REFERENCES marketplaces (code);
ALTER TABLE parse_errors ADD COLUMN marketplace TEXT NOT NULL DEFAULT 'ozon'
    REFERENCES marketplaces (code);

ALTER TABLE discovery_runs ALTER COLUMN marketplace DROP DEFAULT;
ALTER TABLE sku_panel ALTER COLUMN marketplace DROP DEFAULT;
ALTER TABLE parse_runs ALTER COLUMN marketplace DROP DEFAULT;
ALTER TABLE products ALTER COLUMN marketplace DROP DEFAULT;
ALTER TABLE price_history ALTER COLUMN marketplace DROP DEFAULT;
ALTER TABLE parse_errors ALTER COLUMN marketplace DROP DEFAULT;

-- Регион сбора: у Wildberries - явный dest, у Ozon NULL - регион он
-- определяет сам по IP (issue #4).
ALTER TABLE parse_runs ADD COLUMN region TEXT;

-- ----------------------------------------------------------------- ключи ---
-- Цели составных внешних ключей: прогон принадлежит одному маркетплейсу.
ALTER TABLE parse_runs
    ADD CONSTRAINT parse_runs_run_marketplace_key UNIQUE (run_id, marketplace);
ALTER TABLE discovery_runs
    ADD CONSTRAINT discovery_runs_id_marketplace_key UNIQUE (id, marketplace);

-- Карточка: ключ (marketplace, sku).
ALTER TABLE price_history DROP CONSTRAINT price_history_sku_fkey;
ALTER TABLE products DROP CONSTRAINT products_pkey;
ALTER TABLE products ADD CONSTRAINT products_pkey PRIMARY KEY (marketplace, sku);
ALTER TABLE price_history ADD CONSTRAINT price_history_product_fkey
    FOREIGN KEY (marketplace, sku) REFERENCES products (marketplace, sku);

-- Panel: SKU уникален внутри маркетплейса.
ALTER TABLE sku_panel DROP CONSTRAINT sku_panel_sku_key;
ALTER TABLE sku_panel ADD CONSTRAINT sku_panel_marketplace_sku_key UNIQUE (marketplace, sku);

-- Наблюдение, ошибка, карточка и строка panel - только в прогон своего
-- маркетплейса. Ключ истории (run_id, sku) остаётся: прогон уже задаёт маркетплейс.
ALTER TABLE price_history DROP CONSTRAINT price_history_run_id_fkey;
ALTER TABLE price_history ADD CONSTRAINT price_history_run_fkey
    FOREIGN KEY (run_id, marketplace) REFERENCES parse_runs (run_id, marketplace);
ALTER TABLE parse_errors DROP CONSTRAINT parse_errors_run_id_fkey;
ALTER TABLE parse_errors ADD CONSTRAINT parse_errors_run_fkey
    FOREIGN KEY (run_id, marketplace) REFERENCES parse_runs (run_id, marketplace);
ALTER TABLE products DROP CONSTRAINT products_last_run_id_fkey;
ALTER TABLE products ADD CONSTRAINT products_last_run_fkey
    FOREIGN KEY (last_run_id, marketplace) REFERENCES parse_runs (run_id, marketplace);
ALTER TABLE sku_panel DROP CONSTRAINT sku_panel_discovery_run_id_fkey;
ALTER TABLE sku_panel ADD CONSTRAINT sku_panel_discovery_run_fkey
    FOREIGN KEY (discovery_run_id, marketplace) REFERENCES discovery_runs (id, marketplace);

-- --------------------------------------------------------------- индексы ---
DROP INDEX price_history_sku_time_idx;
CREATE INDEX price_history_sku_time_idx ON price_history (marketplace, sku, collected_at);
DROP INDEX parse_errors_sku_idx;
CREATE INDEX parse_errors_sku_idx ON parse_errors (marketplace, sku);
DROP INDEX sku_panel_active_category_idx;
CREATE INDEX sku_panel_active_category_idx ON sku_panel (marketplace, category) WHERE is_active;

-- ---------------------------------------------------------- представления ---
-- CREATE OR REPLACE, колонка marketplace - последней: прежние колонки и права
-- на представления остаются, запросы с явным списком колонок не ломаются.
-- Окна, соединения и группировки - внутри маркетплейса.

CREATE OR REPLACE VIEW v_price_daily AS
WITH daily AS (
    SELECT DISTINCT ON (ph.marketplace, ph.sku,
                        (ph.collected_at AT TIME ZONE 'Europe/Moscow')::date)
        ph.marketplace,
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
    ORDER BY ph.marketplace, ph.sku, (ph.collected_at AT TIME ZONE 'Europe/Moscow')::date,
             ph.collected_at DESC
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
    d.reviews_total - lag(d.reviews_total) OVER w          AS reviews_change,
    d.marketplace
FROM daily d
LEFT JOIN sku_panel sp ON sp.marketplace = d.marketplace AND sp.sku = d.sku
LEFT JOIN products p ON p.marketplace = d.marketplace AND p.sku = d.sku
WINDOW w AS (PARTITION BY d.marketplace, d.sku ORDER BY d.observed_date);

CREATE OR REPLACE VIEW v_price_latest AS
SELECT DISTINCT ON (ph.marketplace, ph.sku)
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
    ph.reviews_total,
    ph.marketplace
FROM price_history ph
LEFT JOIN sku_panel sp ON sp.marketplace = ph.marketplace AND sp.sku = ph.sku
LEFT JOIN products p ON p.marketplace = ph.marketplace AND p.sku = ph.sku
ORDER BY ph.marketplace, ph.sku, ph.collected_at DESC;

CREATE OR REPLACE VIEW v_price_volatility AS
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
    max(observed_date)                                            AS last_date,
    marketplace
FROM v_price_daily
GROUP BY marketplace, sku, category, sampling_group;

CREATE OR REPLACE VIEW v_category_daily AS
SELECT
    category,
    observed_date,
    count(*)                                                      AS sku_observed,
    round(avg(price), 2)                                          AS avg_price,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY price)            AS median_price,
    count(*) FILTER (WHERE price_change <> 0)                     AS sku_price_changed,
    round(avg(price_change_pct), 2)                               AS avg_price_change_pct,
    round(avg(discount_pct), 2)                                   AS avg_discount_pct,
    round(100.0 * count(*) FILTER (WHERE is_available) / count(*), 1) AS available_pct,
    marketplace
FROM v_price_daily
GROUP BY marketplace, category, observed_date;

CREATE OR REPLACE VIEW v_panel_summary AS
SELECT
    category,
    sampling_group,
    count(*) FILTER (WHERE is_active)       AS active_sku,
    count(*) FILTER (WHERE NOT is_active)   AS inactive_sku,
    min(position) FILTER (WHERE is_active)  AS min_position,
    max(position) FILTER (WHERE is_active)  AS max_position,
    marketplace
FROM sku_panel
GROUP BY marketplace, category, sampling_group;

CREATE OR REPLACE VIEW v_parse_runs AS
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
    request_delay,
    marketplace,
    region
FROM parse_runs;

-- ===========================================================================
--  Схема конвейера discovery -> panel -> parse.
--
--  Применяется автоматически (ozon_parser.warehouse.migrate) при запуске любой
--  команды python -m ozon_parser. Вручную запускать не нужно.
--
--  Таблица ozon_products старого сценария (parse_ozon.py --storage postgres)
--  здесь не трогается: оба сценария живут в одной базе независимо.
-- ===========================================================================

-- Запуски discovery: что искали, сколько нашли и отобрали, с каким зерном.
CREATE TABLE discovery_runs (
    id               BIGSERIAL PRIMARY KEY,
    category         TEXT        NOT NULL,
    source           TEXT        NOT NULL,
    seed             TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at      TIMESTAMPTZ,
    status           TEXT        NOT NULL DEFAULT 'running',
    pages_requested  INTEGER     NOT NULL DEFAULT 0,
    discovered       INTEGER     NOT NULL DEFAULT 0,
    selected_top     INTEGER     NOT NULL DEFAULT 0,
    selected_tail    INTEGER     NOT NULL DEFAULT 0,
    error_message    TEXT,
    CONSTRAINT discovery_runs_status_check
        CHECK (status IN ('running', 'success', 'failed', 'skipped'))
);

-- Постоянный набор наблюдаемых товаров. SKU уникален во всей panel: товар
-- относится к одной категории, иначе категорийная аналитика считала бы его
-- дважды. Строки не удаляются, а выключаются (is_active = false).
CREATE TABLE sku_panel (
    id                BIGSERIAL PRIMARY KEY,
    sku               TEXT        NOT NULL UNIQUE,
    category          TEXT        NOT NULL,
    source            TEXT        NOT NULL,
    source_url        TEXT,
    -- Место в выдаче на момент отбора (оценка: страница * 8 + место на ней).
    position          INTEGER,
    page              INTEGER,
    sampling_group    TEXT        NOT NULL,
    is_active         BOOLEAN     NOT NULL DEFAULT TRUE,
    discovery_run_id  BIGINT      REFERENCES discovery_runs (id),
    -- Когда discovery впервые и в последний раз видел товар в выдаче.
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deactivated_at    TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sku_panel_group_check
        CHECK (sampling_group IN ('top', 'tail_random', 'manual'))
);

CREATE INDEX sku_panel_active_category_idx ON sku_panel (category) WHERE is_active;

-- Запуски парсера: план, итог и скорость.
CREATE TABLE parse_runs (
    run_id            BIGSERIAL PRIMARY KEY,
    -- daily - планировщик, manual - запуск руками, benchmark - замер скорости.
    kind              TEXT        NOT NULL DEFAULT 'manual',
    -- panel | file | args: откуда взят список SKU.
    sku_source        TEXT        NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'running',
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    total_sku         INTEGER     NOT NULL,
    processed_count   INTEGER     NOT NULL DEFAULT 0,
    success_count     INTEGER     NOT NULL DEFAULT 0,
    error_count       INTEGER     NOT NULL DEFAULT 0,
    duration_seconds  NUMERIC(10, 1),
    -- Обработано SKU в минуту по полному времени прогона, с паузами.
    sku_per_minute    NUMERIC(8, 2),
    -- Среднее время разбора одного SKU (с повторами, без паузы между SKU).
    avg_sku_seconds   NUMERIC(8, 2),
    -- При какой паузе между SKU (REQUEST_DELAY) сделан замер.
    request_delay     NUMERIC(6, 2),
    CONSTRAINT parse_runs_kind_check CHECK (kind IN ('daily', 'manual', 'benchmark')),
    CONSTRAINT parse_runs_status_check
        CHECK (status IN ('running', 'success', 'partial', 'failed', 'interrupted'))
);

-- Карточка товара: последние известные «медленные» атрибуты (SCD1).
-- Цена, рейтинг и отзывы меняются каждый день и живут в price_history.
CREATE TABLE products (
    sku               TEXT        PRIMARY KEY,
    title             TEXT,
    cover_image       TEXT,
    color             TEXT,
    material          TEXT,
    art_set           TEXT,
    has_rich_content  BOOLEAN,
    photos_seller     INTEGER,
    videos_seller     INTEGER,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_run_id       BIGINT      REFERENCES parse_runs (run_id)
);

-- История наблюдений: одна строка на SKU за каждый успешный сбор. Строки
-- только добавляются; повтор записи в том же прогоне обновляет ту же строку.
CREATE TABLE price_history (
    id             BIGSERIAL PRIMARY KEY,
    run_id         BIGINT        NOT NULL REFERENCES parse_runs (run_id),
    sku            TEXT          NOT NULL REFERENCES products (sku),
    -- Момент сбора карточки, а не дата запуска и не имя файла.
    collected_at   TIMESTAMPTZ   NOT NULL,
    price          NUMERIC(12, 2),
    -- Цена с Ozon Картой (банком Ozon).
    card_price     NUMERIC(12, 2),
    -- Зачёркнутая цена «до скидки», как её показывает Ozon.
    old_price      NUMERIC(12, 2),
    -- Скидка price относительно old_price, %.
    discount_pct   NUMERIC(5, 2),
    is_available   BOOLEAN,
    rating         NUMERIC(3, 2),
    reviews_total  INTEGER,
    -- api | html: откуда разобрана карточка.
    source         TEXT,
    CONSTRAINT price_history_run_sku_key UNIQUE (run_id, sku)
);

CREATE INDEX price_history_sku_time_idx ON price_history (sku, collected_at);

-- Ошибки по отдельным SKU: один проблемный товар не останавливает прогон.
CREATE TABLE parse_errors (
    id             BIGSERIAL PRIMARY KEY,
    run_id         BIGINT      NOT NULL REFERENCES parse_runs (run_id),
    sku            TEXT        NOT NULL,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- not_found | fetch_error | not_processed | interrupted | storage_error
    error_type     TEXT        NOT NULL,
    error_message  TEXT,
    attempts       INTEGER
);

CREATE INDEX parse_errors_run_idx ON parse_errors (run_id);
CREATE INDEX parse_errors_sku_idx ON parse_errors (sku);

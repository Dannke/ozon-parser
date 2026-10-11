-- ===========================================================================
--  Wildberries: строка в справочнике и сырые ответы карточек.
--
--  Сырой ответ хранится рядом с наблюдением (та же транзакция, тот же прогон):
--  если правило разбора цены окажется неверным, историю можно будет
--  пересчитать, а не потерять - заново её не собрать. Хранится 90 дней
--  (Warehouse.prune_raw после каждого прогона). Пишет его адаптер, у которого
--  он есть (сейчас WB); у Ozon строк здесь нет.
-- ===========================================================================

INSERT INTO marketplaces (code, title) VALUES ('wb', 'Wildberries');

CREATE TABLE raw_products (
    marketplace   TEXT        NOT NULL,
    run_id        BIGINT      NOT NULL,
    sku           TEXT        NOT NULL,
    collected_at  TIMESTAMPTZ NOT NULL,
    payload       JSONB       NOT NULL,
    CONSTRAINT raw_products_pkey PRIMARY KEY (run_id, sku),
    CONSTRAINT raw_products_run_fkey
        FOREIGN KEY (run_id, marketplace) REFERENCES parse_runs (run_id, marketplace)
);

CREATE INDEX raw_products_collected_idx ON raw_products (collected_at);

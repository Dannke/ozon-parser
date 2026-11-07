-- ===========================================================================
--  price_history.list_price: зачёркнутая цена WB как её отдаёт сайт.
--
--  old_price пишется, только если зачёркнутая выше цены (так её показывает
--  страница), поэтому дни, когда цена дошла до зачёркнутой, в old_price не
--  видны - а для проверки «подходила ли цена к зачёркнутой» нужны именно они
--  (08.11.2026: 96 из 103 SKU-дней WB без old_price имели basic = цене или
--  ниже). Сырые ответы удаляются через 90 дней, поэтому значение хранится в
--  истории. У Ozon пусто.
--
--  Существующие строки WB заполняются из raw_products тем же правилом, что
--  у marketplaces/wb/extract.py: basic размера с минимальной ценой product.
-- ===========================================================================

ALTER TABLE price_history ADD COLUMN list_price NUMERIC(12, 2);

COMMENT ON COLUMN price_history.list_price IS
    'WB: sizes[].price.basic / 100 - зачёркнутая цена с сайта, даже если не выше price; у Ozon пусто';

UPDATE price_history ph
SET list_price = raw.basic / 100
FROM (
    SELECT r.run_id, r.sku,
           -- Не число - пусто, а не ошибка: упавшая миграция остановила бы сбор.
           (SELECT CASE WHEN jsonb_typeof(size -> 'price' -> 'basic') = 'number'
                        THEN NULLIF((size -> 'price' ->> 'basic')::numeric, 0) END
            FROM jsonb_array_elements(r.payload -> 'sizes') AS size
            WHERE jsonb_typeof(size -> 'price' -> 'product') = 'number'
            ORDER BY (size -> 'price' ->> 'product')::numeric
            LIMIT 1) AS basic
    FROM raw_products r
    WHERE r.marketplace = 'wb' AND jsonb_typeof(r.payload -> 'sizes') = 'array'
) raw
WHERE ph.marketplace = 'wb'
  AND ph.run_id = raw.run_id
  AND ph.sku = raw.sku
  AND ph.price IS NOT NULL
  AND raw.basic IS NOT NULL;

-- ===========================================================================
--  Статус прогона blocked: парсер остановился сам, потому что Ozon подряд не
--  отдавал данные (MAX_CONSECUTIVE_FAILURES SKU). Повод - прогон 30.09.2026 в
--  Docker, который 5 часов получал HTTP 403 на каждый товар.
--
--  Необработанные SKU такого прогона пишутся в parse_errors с error_type
--  'blocked'.
-- ===========================================================================

ALTER TABLE parse_runs DROP CONSTRAINT parse_runs_status_check;

ALTER TABLE parse_runs ADD CONSTRAINT parse_runs_status_check
    CHECK (status IN ('running', 'success', 'partial', 'failed', 'interrupted', 'blocked'));

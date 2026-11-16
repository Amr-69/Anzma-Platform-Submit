-- 005_alert_uniqueness.sql  (Day 2, C1/P0). Load order: base schema -> 003 -> 004 -> this file. Idempotent.
-- Adds ONLY: a UNIQUE index on alerts (rule_id, asset_id, first_ts) -- the key store_alerts() already treats as "same alert".
-- Guard: if duplicates already exist the migration aborts with a clear message instead of failing half-way or deleting data.
BEGIN;
DO $$
DECLARE dup_count BIGINT;
BEGIN
    SELECT count(*) INTO dup_count FROM (
        SELECT 1 FROM alerts GROUP BY rule_id, asset_id, first_ts HAVING count(*) > 1) d;
    IF dup_count > 0 THEN
        RAISE EXCEPTION '005_alert_uniqueness: % duplicate (rule_id, asset_id, first_ts) group(s) exist in alerts; resolve them manually before applying', dup_count;
    END IF;
END $$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_rule_asset_first_ts ON alerts (rule_id, asset_id, first_ts);
COMMIT;

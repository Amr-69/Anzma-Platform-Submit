-- 003_incidents.sql  (Day 2, Ahmed). Load order: 1) anzma_schema_and_seed.sql  2) this file. Idempotent.
-- Adds ONLY: the incidents table, its indexes and the nullable alerts.incident_id. Nothing else is touched.
-- An incident groups LINKED alerts (same asset, or same system type in the same location) within 15 minutes of
-- each other. Alerts stay the detections; the incident is the managed record (open -> acknowledged -> resolved).
-- If an earlier version of this file was applied to a database, rebuild that database (the rule name changed).
BEGIN;
CREATE TABLE IF NOT EXISTS incidents (
    incident_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    location_id     BIGINT NOT NULL REFERENCES locations(location_id),
    title           TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
    grouping_rule   TEXT NOT NULL DEFAULT 'same_asset_or_system_within_15_min',
    first_ts        TIMESTAMPTZ NOT NULL,
    last_ts         TIMESTAMPTZ NOT NULL,
    acknowledged_ts TIMESTAMPTZ,
    resolved_ts     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (last_ts >= first_ts),
    CHECK ((status = 'resolved') = (resolved_ts IS NOT NULL))
);
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS incident_id BIGINT REFERENCES incidents(incident_id);
CREATE INDEX IF NOT EXISTS idx_alerts_incident ON alerts(incident_id);
CREATE INDEX IF NOT EXISTS idx_incidents_location_last ON incidents(location_id, last_ts);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);
COMMIT;

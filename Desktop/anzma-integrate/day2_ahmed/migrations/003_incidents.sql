-- 003_incidents.sql  (Day 2, Ahmed) - additive: nothing in 001/002 changes except one new nullable column.
-- An incident groups alerts raised in the same location within 15 minutes of each other.
-- Alerts stay the detections; the incident is the managed record with a lifecycle (open -> acknowledged -> resolved).
BEGIN;
CREATE TABLE IF NOT EXISTS incidents (
    incident_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    location_id     BIGINT NOT NULL REFERENCES locations(location_id),
    title           TEXT NOT NULL,
    severity        TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','acknowledged','resolved')),
    grouping_rule   TEXT NOT NULL DEFAULT 'same_location_within_15_min',
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

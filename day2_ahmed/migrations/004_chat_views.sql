-- 004_chat_views.sql. Load order: 1) anzma_schema_and_seed.sql  2) 003_incidents.sql  3) this file. Idempotent, read-only views.
-- Purpose: the chat assistant queries these views instead of hand-writing joins. Each view uses the SAME definition as the
-- dashboard, so the chat and the dashboard always give the same numbers. "Now" = the latest stored record (v_data_clock),
-- exactly like the dashboard, never the server clock.
BEGIN;

CREATE OR REPLACE VIEW v_data_clock AS
SELECT GREATEST((SELECT max(ts) FROM telemetry_records), (SELECT max(ts) FROM event_records)) AS as_of;

-- One row per asset. health_24h follows the dashboard "Healthy assets" rule over the 24 h ending at as_of:
-- critical (>=1 critical event) > no_data (reports normally, sent nothing) > warning (>=1 warning event) > normal.
CREATE OR REPLACE VIEW v_asset_overview AS
WITH clk AS (SELECT as_of FROM v_data_clock),
ev AS (
    SELECT er.asset_id,
           count(*) FILTER (WHERE er.severity = 'critical') AS critical_events_24h,
           count(*) FILTER (WHERE er.severity = 'warning')  AS warning_events_24h,
           count(*) FILTER (WHERE et.name = 'heartbeat')    AS heartbeats_24h
    FROM event_records er
    JOIN event_types et ON et.event_type_id = er.event_type_id, clk
    WHERE er.ts > clk.as_of - interval '24 hours' AND er.ts <= clk.as_of
    GROUP BY er.asset_id),
tel AS (
    SELECT t.asset_id, count(*) AS telemetry_24h
    FROM telemetry_records t, clk
    WHERE t.ts > clk.as_of - interval '24 hours' AND t.ts <= clk.as_of
    GROUP BY t.asset_id),
reports AS (
    SELECT asset_id FROM telemetry_records GROUP BY asset_id
    UNION
    SELECT er.asset_id FROM event_records er JOIN event_types et ON et.event_type_id = er.event_type_id
    WHERE et.name = 'heartbeat'),
seen AS (
    SELECT asset_id, max(ts) AS last_seen
    FROM (SELECT asset_id, ts FROM telemetry_records UNION ALL SELECT asset_id, ts FROM event_records) u
    GROUP BY asset_id),
alr AS (
    SELECT asset_id, count(*) AS open_alerts
    FROM alerts WHERE status <> 'resolved' GROUP BY asset_id)
SELECT a.asset_id, a.name AS asset_name, a.asset_type, st.code AS system, l.name AS zone, l.floor, b.name AS building,
       CASE
         WHEN COALESCE(ev.critical_events_24h, 0) > 0 THEN 'critical'
         WHEN a.asset_id IN (SELECT asset_id FROM reports)
              AND COALESCE(tel.telemetry_24h, 0) = 0 AND COALESCE(ev.heartbeats_24h, 0) = 0 THEN 'no_data'
         WHEN COALESCE(ev.warning_events_24h, 0) > 0 THEN 'warning'
         ELSE 'normal'
       END AS health_24h,
       COALESCE(ev.critical_events_24h, 0) AS critical_events_24h,
       COALESCE(ev.warning_events_24h, 0)  AS warning_events_24h,
       COALESCE(alr.open_alerts, 0)        AS open_alerts,
       seen.last_seen
FROM assets a
JOIN system_types st ON st.system_type_id = a.system_type_id
JOIN locations l ON l.location_id = a.location_id
JOIN buildings b ON b.building_id = l.building_id
LEFT JOIN ev  ON ev.asset_id  = a.asset_id
LEFT JOIN tel ON tel.asset_id = a.asset_id
LEFT JOIN seen ON seen.asset_id = a.asset_id
LEFT JOIN alr ON alr.asset_id = a.asset_id;

-- Incidents, with the zone and the number of alerts. is_active = the dashboard "Open incidents" rule (status <> 'resolved').
CREATE OR REPLACE VIEW v_incident_overview AS
SELECT i.incident_id, i.title, i.severity, i.status, (i.status <> 'resolved') AS is_active,
       l.name AS zone, i.first_ts, i.last_ts, i.acknowledged_ts, i.resolved_ts,
       (SELECT count(*) FROM alerts a WHERE a.incident_id = i.incident_id) AS alert_count
FROM incidents i
JOIN locations l ON l.location_id = i.location_id;

-- Alerts with the asset, system and zone. is_active = status <> 'resolved'.
CREATE OR REPLACE VIEW v_alert_overview AS
SELECT al.alert_id, al.rule_id, al.detection_method, al.severity, al.status, (al.status <> 'resolved') AS is_active,
       al.title, al.summary, al.first_ts, al.last_ts, al.incident_id,
       a.name AS asset_name, a.asset_type, st.code AS system, l.name AS zone
FROM alerts al
JOIN assets a ON a.asset_id = al.asset_id
JOIN system_types st ON st.system_type_id = a.system_type_id
JOIN locations l ON l.location_id = a.location_id;

-- Cameras: offline when the latest camera_offline event is newer than the latest camera_online event.
CREATE OR REPLACE VIEW v_camera_status AS
SELECT a.asset_id, a.name AS asset_name, l.name AS zone,
       max(er.ts) FILTER (WHERE et.name = 'camera_offline') AS last_offline,
       max(er.ts) FILTER (WHERE et.name = 'camera_online')  AS last_online,
       COALESCE(max(er.ts) FILTER (WHERE et.name = 'camera_offline')
                > COALESCE(max(er.ts) FILTER (WHERE et.name = 'camera_online'), '-infinity'::timestamptz), false) AS is_offline
FROM assets a
JOIN locations l ON l.location_id = a.location_id
LEFT JOIN event_records er ON er.asset_id = a.asset_id
LEFT JOIN event_types et ON et.event_type_id = er.event_type_id AND et.name IN ('camera_offline', 'camera_online')
WHERE a.asset_type = 'camera'
GROUP BY a.asset_id, a.name, l.name;

COMMIT;

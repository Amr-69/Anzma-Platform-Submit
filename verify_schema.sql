-- verify_schema.sql -- READ-ONLY catalog check of a live database against the authoritative schema
-- (base schema -> 003 -> 004 -> 005; see DATABASE_BASELINE.md). Changes nothing: SELECT only.
-- Run:  psql -U anzma -d <db> -f verify_schema.sql        (every row must show status = OK)
-- Exit status of psql is not affected by a failed check; read the status column (final row = overall verdict).
WITH expected_tables(name) AS (VALUES
  ('buildings'),('locations'),('system_types'),('assets'),('metric_types'),('event_types'),
  ('metric_type_system_compat'),('event_type_system_compat'),('telemetry_records'),('event_records'),
  ('alerts'),('incidents'),('scenario_runs')),
expected_views(name) AS (VALUES
  ('v_data_clock'),('v_asset_overview'),('v_incident_overview'),('v_alert_overview'),('v_camera_status')),
expected_indexes(name) AS (VALUES
  ('idx_assets_location'),('idx_assets_system_type'),('idx_telemetry_ts'),('idx_event_ts'),('idx_event_asset_ts'),
  ('idx_alerts_first_ts'),('idx_alerts_asset_ts'),('idx_alerts_severity'),('idx_alerts_open'),
  ('idx_alerts_incident'),('idx_incidents_location_last'),('idx_incidents_status'),('uq_alerts_rule_asset_first_ts')),
expected_columns(tbl, col) AS (VALUES
  ('alerts','incident_id'),('alerts','evidence'),('alerts','resolved_ts'),('alerts','explanation'),
  ('alerts','explanation_source'),('alerts','explained_at'),('incidents','grouping_rule'),
  ('telemetry_records','value'),('event_records','details'),('scenario_runs','expected_rules')),
checks AS (
  SELECT 1 AS n, 'tables missing' AS check_name, '0' AS expected,
         (SELECT count(*) FROM expected_tables e WHERE NOT EXISTS
            (SELECT 1 FROM information_schema.tables t WHERE t.table_schema='public' AND t.table_type='BASE TABLE' AND t.table_name=e.name))::text AS actual
  UNION ALL SELECT 2, 'unexpected extra tables (data-model.sql draft leaked in?)', '0',
         (SELECT count(*) FROM information_schema.tables t WHERE t.table_schema='public' AND t.table_type='BASE TABLE'
            AND t.table_name NOT IN (SELECT name FROM expected_tables))::text
  UNION ALL SELECT 3, 'views missing', '0',
         (SELECT count(*) FROM expected_views e WHERE NOT EXISTS
            (SELECT 1 FROM information_schema.views v WHERE v.table_schema='public' AND v.table_name=e.name))::text
  UNION ALL SELECT 4, 'indexes missing', '0',
         (SELECT count(*) FROM expected_indexes e WHERE NOT EXISTS
            (SELECT 1 FROM pg_indexes i WHERE i.schemaname='public' AND i.indexname=e.name))::text
  UNION ALL SELECT 5, 'key columns missing', '0',
         (SELECT count(*) FROM expected_columns e WHERE NOT EXISTS
            (SELECT 1 FROM information_schema.columns c WHERE c.table_schema='public' AND c.table_name=e.tbl AND c.column_name=e.col))::text
  UNION ALL SELECT 6, 'alerts unique index is UNIQUE on (rule_id, asset_id, first_ts)', '1',
         (SELECT count(*) FROM pg_indexes WHERE schemaname='public' AND tablename='alerts' AND indexname='uq_alerts_rule_asset_first_ts'
            AND indexdef LIKE 'CREATE UNIQUE INDEX%(rule_id, asset_id, first_ts)')::text
  UNION ALL SELECT 7, 'duplicate alerts on (rule_id, asset_id, first_ts)', '0',
         (SELECT count(*) FROM (SELECT 1 FROM alerts GROUP BY rule_id, asset_id, first_ts HAVING count(*) > 1) d)::text
  UNION ALL SELECT 8, 'CHECK constraints in public (base schema + incidents)', '>=14',
         (SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND c.contype='c')::text
  UNION ALL SELECT 9, 'UNIQUE constraints in public (telemetry/event/assets/etc.)', '>=8',
         (SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND c.contype='u')::text
  UNION ALL SELECT 10, 'FOREIGN KEY constraints in public', '>=13',
         (SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND c.contype='f')::text
)
SELECT n, check_name, expected, actual,
       CASE WHEN expected LIKE '>=%' THEN CASE WHEN actual::int >= substr(expected,3)::int THEN 'OK' ELSE 'FAIL' END
            WHEN actual = expected THEN 'OK' ELSE 'FAIL' END AS status
FROM checks
UNION ALL
SELECT 99, 'OVERALL', 'all OK', '', CASE WHEN bool_and(CASE WHEN expected LIKE '>=%' THEN actual::int >= substr(expected,3)::int ELSE actual = expected END) THEN 'OK' ELSE 'FAIL' END
FROM checks
ORDER BY n;

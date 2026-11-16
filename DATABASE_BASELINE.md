# DATABASE_BASELINE.md — single source of truth for the Anzma database

Status legend: **DONE** = file exists in the working tree; **VERIFIED (evidence)** = shown by output in `anzma_evidence/` captured by the previous session on commit `87e084d` + local changes; **EXECUTED** = run successfully by Ahmed (see §9). Related: [DAY2_IMPLEMENTATION_REPORT.md](DAY2_IMPLEMENTATION_REPORT.md), [DAY2_HANDOVER.md](DAY2_HANDOVER.md).

## 1. Authoritative architecture
PostgreSQL 16 (Docker Compose, `postgres:16`). **13 tables + 5 views.** The authoritative schema is exactly the files below, in this order. There is no migration runner and no `schema_migrations` table (deliberate; needs a deployment decision, §8).

| # | File | Adds | State |
|---|---|---|---|
| 1 | `db/init/anzma_schema_and_seed.sql` | 12 tables (reference, facts, alerts, scenario_runs) + seed | tracked; clean install exit 0 (evidence) |
| 2 | `day2_ahmed/migrations/003_incidents.sql` | `incidents`, `alerts.incident_id`, 3 indexes | tracked; idempotent |
| 3 | `day2_ahmed/migrations/004_chat_views.sql` | 5 views `v_*` | **untracked in git → must be committed** |
| 4 | `day2_ahmed/migrations/005_alert_uniqueness.sql` | UNIQUE index `uq_alerts_rule_asset_first_ts` | **new, untracked, EXECUTED (tests pass)** |

Not authoritative: `data-model.sql` (22-table draft, not loaded by Compose, unused). `day2_ahmed/anzma_schema_and_seed.sql` is a byte-identical copy of the db/init file (`diff` clean) used by `test_day2_integration.py`; do not delete it without changing that test.

## 2. Tables (13)
`buildings`, `locations` (UNIQUE building_id+name), `system_types` (CHECK code in HVAC/NETWORKING/ACCESS_CONTROL/CCTV/FIRE_ALARM), `assets` (asset_code UNIQUE; FK system_type, location), `metric_types` (name UNIQUE, unit), `event_types` (name UNIQUE), `metric_type_system_compat`, `event_type_system_compat` (composite PKs), `telemetry_records` (UNIQUE asset+metric+ts; CHECK value not NaN/±Infinity), `event_records` (UNIQUE asset+event_type+ts; CHECK severity), `alerts`, `incidents` (003), `scenario_runs` (CHECK end_ts ≥ start_ts; array columns without FKs).

`alerts`: CHECK detection_method ∈ threshold/anomaly/compound; severity ∈ info/warning/critical; status ∈ open/acknowledged/resolved; explanation_source ∈ ollama/fallback; `last_ts ≥ first_ts`; `(status='resolved') = (resolved_ts IS NOT NULL)`; FK asset_id; FK incident_id (003); **UNIQUE (rule_id, asset_id, first_ts) from 005**. `incidents` carries the same last_ts and resolved checks.

Indexes: idx_assets_location, idx_assets_system_type, idx_telemetry_ts, idx_event_ts, idx_event_asset_ts, idx_alerts_first_ts, idx_alerts_asset_ts, idx_alerts_severity, idx_alerts_open (partial, status<>'resolved'), idx_alerts_incident, idx_incidents_location_last, idx_incidents_status, uq_alerts_rule_asset_first_ts.

## 3. Views (5, from 004; read-only)
`v_data_clock(as_of)`, `v_asset_overview`, `v_incident_overview`, `v_alert_overview`, `v_camera_status`. "Now" in every view is the **latest stored record** (`v_data_clock`), never the server clock. Column lists are asserted in `day2_ahmed/test_data_integrity.py`.

## 4. Data integrity findings (VERIFIED (evidence), commit 87e084d, DB snapshot of 2026-11-16)
- Row counts: buildings 2, locations 6, system_types 5, assets 12, metric_types 8, event_types 12, metric compat 8, event compat 16, telemetry 4669, events 959, alerts 29, incidents 13, scenario_runs 0. A clean install has 955 events, 0 alerts, 0 incidents (`catalog_diff.txt`).
- Orphans: 0 (telemetry/events/alerts). Compatibility violations: 0. Duplicate alerts by (rule_id, asset_id, first_ts): 0 rows (section E), so 005 can be applied to that database.
- Before 005 the database had **no** unique constraint on alerts; uniqueness lived only in `store_alerts` (`WHERE NOT EXISTS`) → race between the detection loop and `POST /alerts` / `/alerts/run`.
- `*_compat` tables are **not enforced** by the database (no FK/trigger from telemetry/events). Enforcement is application-level (`integrity_rules.py`, `validation.py`). `test_data_integrity.py` pins this current behaviour.
- Building is derived through one path only (assets → locations → buildings), so an asset cannot be attached to a wrong building. Adding a duplicated `building_id` column would make that possible (reason to defer it).

## 5. Timestamp / data-clock finding (DOCUMENTED, no change made)
Evidence (`integrity_output.txt`, 2026-11-16 ~10:50 UTC): data clock `as_of` = 2026-11-11 08:00:02+00, wall clock 5 d 02:50 later. Cause visible in the event list: three `hvac_fault` events (ids 957–959, `{"note":"manual test"}`) were inserted manually at 2026-11-11 08:00:00–02 for HVAC-AHU-01, after the seeded data ends (telemetry last 2026-11-10 12:45). Because "now" = max record, the clock jumped ~19 h, so the other 11 assets show `age_vs_data_clock` of 19 h–1 d and can be classified `no_data` by `v_asset_overview`. Impact: dashboard/chat health and the 20-min/1-h gap rules depend on the latest record. **Decision pending (owner Amr):** rebuild the demo DB from the seed, or define the clock differently. Not changed here.

## 6. Known limitations / confirmed defect C5 — bandwidth unit (NOT IMPLEMENTED, decision pending)
`bandwidth_util` has unit `%`, 626 rows, min 15.57, max 72.32, mean 43.03 (evidence section G), while the alert thresholds are 900 and 980 (Mbps-style). The thresholds can never fire. Recommended: keep `%` (no link-capacity column exists to convert) and re-set thresholds/labels in %. Threshold values are an operational decision → **Eng. Mohsen / Amr must decide**; fixing will change alert results. No threshold or behaviour was changed in this work.

Other limitations: telemetry/event tables have no `received_at`, `source`, `quality`; no timezone on buildings/locations; alerts never UPDATE first_ts/last_ts.

## 7. Clean install and deployment
1. Compose (`docker compose up`) runs init files only on an **empty** volume, in filename order. Current `docker-compose.yml` (Amr's local, staged, not committed) mounts base (01), 003 (02), 004 (03). **005 is not mounted yet** — exact line for Amr in [DAY2_HANDOVER.md](DAY2_HANDOVER.md).
2. Existing databases (volume already initialised) are not re-run by Compose: apply 003/004/005 manually with `psql -f`. **Apply 005 before running the new code against an existing DB**: `store_alerts` now uses `ON CONFLICT (rule_id, asset_id, first_ts)`, which PostgreSQL rejects (SQLSTATE 42P10) if the unique index does not exist.
3. 005 aborts with a clear message if duplicates exist (it never deletes data).
4. Verify any database (read-only): `psql -U <user> -d <db> -f verify_schema.sql` → every row `OK`, final row `OVERALL OK`. **EXECUTED: OVERALL OK** (`test_logs/5_verify.txt`).

## 8. Deferred (not implemented)
`source_event_id` event dedup and `received_at`/ingestion metadata (when a connector exists); migration runner + `schema_migrations` (needs Amr's deployment decision); the 12 PROPOSED entities (organizations, asset_external_ids, data_sources, unit_conversions, reporting_expectations, asset_state_current/history, data_gaps, alert_evidence, ingestion_batches/rejections, retention_policies); duplicated `building_id`; relaxing the CHECK on `system_types.code`; `test_chat_consistency` (assigned to Amr); data-clock fix (§5); bandwidth fix (§6).

## 10. Task 3 analysis (static audit of the code and the evidence; nothing here was changed except 005)

| Topic | What the code/schema actually does | Minimum change for Core v0.1 |
|---|---|---|
| Source / asset identification | Asset = `assets.asset_id` (numeric, what the API uses) + `asset_code` (UNIQUE, e.g. `HVAC-AHU-01`) + `name`. No vendor/external id; no `source` on telemetry, events or alerts. Manual origin is only visible in `event_records.details` (`manual test`) and `alerts.evidence.source = "manual"`. | None now. Treat `asset_code` as the stable external key. `asset_external_ids` / `data_sources` stay PROPOSED. |
| Timestamps | All `ts` columns are `TIMESTAMPTZ`; `db_adapter` converts to UTC; no `received_at` (cannot tell event time from arrival time). | None now (`received_at` deferred until a connector exists). |
| Measurement consistency | `metric_types.unit` holds the unit; telemetry CHECK rejects NaN/±Infinity. `bandwidth_util` is `%` (15.57–72.32) but `alert_config` thresholds are 900/980 and the anomaly rule in `alert_engine.py` labels it `Mbps` → unit mismatch also in labels (C5). | None (C5 pending Eng. Mohsen/Amr). |
| Duplicate prevention | telemetry UNIQUE(asset,metric,ts); events UNIQUE(asset,event_type,ts) and `POST /events` uses `ON CONFLICT DO NOTHING` → HTTP 409; alerts: **005** (before it: code only). Incidents: grouping logic in `incident_engine.py`, no DB uniqueness. | 005 (done, not yet run). |
| Compatibility rules | `*_compat` tables are not enforced by the DB. `POST /events` enforces event/system compatibility in code; no code path inserts telemetry (only the seed does). | None; pinned by a test. |
| Equipment state | Not stored. Computed at query time: `v_asset_overview.health_24h` = critical > no_data > warning > normal over the 24 h before the data clock; cameras by latest `camera_offline` vs `camera_online` (`v_camera_status`). | None. `asset_state_current` stays PROPOSED. |
| Freshness | "Now" = `max(ts)` of telemetry/events (`v_data_clock`, `da.latest_ts`), never the wall clock; `no_data` and the 20-min reading-gap rule (`reading_gap_min=20`) depend on it. A manual event moves the clock (§5). | None; decision pending (§5). |
| Current vs historical | History = `event_records` + `telemetry_records` (append-only facts); current status is derived from them; alerts/incidents have their own lifecycle. | None. |

### Audit findings added in this session
1. `day2_ahmed/e2e_s1.py` built its database from base + 003 only. With the new `ON CONFLICT` in `store_alerts` it would fail without 005. **Fixed** (now applies 003, 004, 005).
2. `day2_ahmed/alert_engine.py::store_alerts(cur, alerts, start, end)` is legacy: it `DELETE`s alerts in a window and re-inserts them with no conflict handling. Nothing calls it (grep: only `pipeline.py` and `App.py` call `db_adapter.store_alerts`). **Not changed**; recommend removing it in a separate change because it would orphan `alerts.incident_id` links if ever used.
3. Contradictory schema references. **Fixed:** `DATA_MODEL.md` and `data-model.sql` now carry a DRAFT / NOT AUTHORITATIVE banner. **Left for Amr (README/RUN_DEMO are not mine):** `README.md` line 59 and `RUN_DEMO.md` line 9 say the init loads only base + 003; they should list 004 and 005. `DECISION_MATRIX.md` line ~530 mentions `migrations/001_unified_schema.sql` (historical plan).
4. The two schema copies (`db/init/…` and `day2_ahmed/…`) are identical (`diff` clean, checked this session).

## 9. Test status
Fresh run by Ahmed on commit `87e084d` + dirty tree (details and logs: [DAY2_TEST_RESULTS.md](DAY2_TEST_RESULTS.md), `test_logs/`): 25 + 43 + 18 + 17 = **103/103 PASS**; `verify_schema.sql` **OVERALL OK**. Not covered: live Ollama, compose clean install with 005, `e2e_s1.py`, `test_chat_consistency`.

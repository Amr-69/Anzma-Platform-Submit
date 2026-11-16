# DAY2_IMPLEMENTATION_REPORT.md
Schema, constraints, limitations and decisions live in [DATABASE_BASELINE.md](DATABASE_BASELINE.md); this file records **what was changed in this session and how it is tested**. Handover: [DAY2_HANDOVER.md](DAY2_HANDOVER.md). Older Day 2 notes: `day2_ahmed/DAY2_AHMED_HANDOVER.md`.

## 1. Starting state (inspected, not assumed)
Branch `final`, HEAD `87e084d`. Pre-existing local changes, all preserved untouched: staged `app/chat_facts.py`, `app/chat_lite.py`, `docker-compose.yml` (Amr: adds the 004 mount and `OLLAMA_HOST` default); untracked `004_chat_views.sql`; 2 stashes. None of C1–C4 existed (no 005, no integrity test, no docs).

## 2. Changes made in this session
| Item | File | Kind |
|---|---|---|
| C1 | `day2_ahmed/migrations/005_alert_uniqueness.sql` | new: duplicate guard + `CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_rule_asset_first_ts` |
| C1 | `day2_ahmed/db_adapter.py` `store_alerts` | modified: `INSERT … ON CONFLICT (rule_id, asset_id, first_ts) DO NOTHING RETURNING alert_id` |
| C1 | `App.py` `POST /alerts` | **reviewed, unchanged**: already returns 409 when `store_alerts` returns no id (now DB-guaranteed); only `HTTPException` is re-raised and DB errors keep mapping to 503, so unrelated errors are not misclassified as 409 |
| C2 | `day2_ahmed/test_data_integrity.py` | new: 18 tests on throw-away DB `anzma_integrity_test` |
| C4 | `day2_ahmed/test_day2_integration.py` | modified: bootstrap applies 004 then 005 (each twice) after the 003 snapshot |
| C4 | `test_frontend_api.py` | modified: applies 003, 004, 005 |
| Audit fix | `day2_ahmed/e2e_s1.py` | modified: DB build now applies 003, 004, 005 (needed by the new `store_alerts`) |
| Docs fix | `DATA_MODEL.md`, `data-model.sql` | modified: DRAFT / NOT AUTHORITATIVE banner only |
| C3 | `verify_schema.sql`, `DATABASE_BASELINE.md`, `DAY2_IMPLEMENTATION_REPORT.md`, `DAY2_HANDOVER.md` | new |
Not touched: `docker-compose.yml`, bandwidth thresholds, any commit/push.

## 3. Migration history
base → 003 (incidents) → 004 (chat views; was untracked) → 005 (alert uniqueness, this session). Idempotent each; no runner.

## 4. Tests
Freshly executed by Ahmed: 25/25, 43/43, 18/18, 17/17 = **103/103 PASS**, `verify_schema.sql` OVERALL OK. Commands, logs and caveats: [DAY2_TEST_RESULTS.md](DAY2_TEST_RESULTS.md). Not run: `e2e_s1.py`, `test_dashboard.py`, live Ollama, compose clean install with 005.

## 5. Ownership
| Area | Owner | Status |
|---|---|---|
| Authoritative schema, migration order, DATABASE_BASELINE.md, 005, integrity tests, verify_schema.sql, test bootstraps, draft-schema labels | Ahmed | written and tested (103/103) |
| docker-compose.yml (005 mount), runtime/startup, RUNTIME_VERIFICATION.md, readiness check, live Qwen vs fallback test, README/RUN_DEMO schema sentences, `test_chat_consistency`, demo recording | Amr | not started by me; instructions in DAY2_HANDOVER.md |
| Bandwidth thresholds/unit (C5) | Eng. Mohsen / Amr | decision pending; nothing changed |
| Data clock / demo-data strategy | Amr | decision pending; nothing changed |
| Removal of legacy `alert_engine.store_alerts` | Ahmed (separate change) | proposed |

## 6. Not done / limitations
Nothing was committed. Tests were executed on Ahmed's machine (see DAY2_TEST_RESULTS.md); `e2e_s1.py` was not. C5, data clock, migration runner, `source_event_id`, `received_at`, and the 12 proposed entities are deliberately out of scope.

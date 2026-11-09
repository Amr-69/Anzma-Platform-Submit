# Day 2 - Ahmed: hand-over

Workflow covered: stored facts -> alert detection -> incident -> evidence -> explanation -> validation -> recommendation.
Tested on real PostgreSQL 16 with `migrations/001` + `002` (Database schema and seed, unchanged) + `003` (mine, additive).
**Not run:** real Ollama (fallback was tested with an unreachable host), the HTTP API and the dashboard.

## Files (copy into the repo root; `migrations/003_incidents.sql` into `migrations/`)
| File | What it does |
|---|---|
| `migrations/003_incidents.sql` | `incidents` table + nullable `alerts.incident_id`. Nothing in 001/002 changes. |
| `db_adapter.py` | Only place that reads/writes the new tables (names translated like `db_map.py`). Swap its maps for `db_map` imports when merged. |
| `alert_engine.py` (patched) | Reads through the adapter; evidence rows carry `source` + `record_id` + exactly one of `event_id`/`telemetry_id`; downtime computed from stored timestamps. |
| `alert_config.py` (patched) | `reading_gap_min` 10 -> 20 (data is every 15 min); `denial_warn` 5 -> 3 (seed scenario = 3 denials). |
| `incident_engine.py` | Grouping (same location, <= 15 min), severity = worst alert, ranking, lifecycle open/acknowledged/resolved with carry-over to alerts. |
| `evidence.py` | `incident_evidence()`: evidence of an incident, every row re-checked against the fact tables. |
| `integrity_rules.py` (extended) | `check_record_db()` for ingestion, `stored_violations()` for the seed. |
| `pipeline.py` | `detect()`, `explain_alert()`, `explain_incident()` - what the API/dashboard should call. |
| `validation.py`, `recommendations.py`, `ai_explainer.py` | As before; validation now runs on stored alerts. |
| `test_day2_integration.py` | 28 tests on a throw-away database built from the migrations. `test_day2_ahmed.py` (21 offline tests) still passes. |

## Run
```
pip install psycopg2-binary
export PGHOST=localhost PGUSER=... PGPASSWORD=...     # a user allowed to CREATE DATABASE
python test_day2_integration.py                        # builds anzma_day2_test, prints PASS/FAIL
# on the real database:
psql -d <db> -f migrations/003_incidents.sql
python -c "import pipeline,psycopg2,psycopg2.extras as x; c=psycopg2.connect('<dsn>',cursor_factory=x.RealDictCursor); cur=c.cursor(); print(pipeline.detect(cur)); c.commit()"
```

## Known limitations
1. `hvac_fault` / `hvac_recovered` events are not mapped to any rule, so the CRAC alert is **warning** (peak 32.0 C, threshold > 32) even though the unit itself logged a critical `hvac_fault`. Decide whether that event should raise or escalate.
2. No fan state, filter fault, battery, motion or access_granted data: `HVAC_FAN`, `HVAC_FILTER`, `FAN_FAILURE` are never offered; recommendations stay generic.
3. `bandwidth_util` is in % but the rules/thresholds are in Mbps: `NET_BANDWIDTH_HIGH` and the bandwidth anomaly cannot fire, and the packet-loss "congestion vs link quality" split always picks link quality. Needs % thresholds.
4. Denial alerts have no reason -> `data_sufficiency = partial`.
5. The seed's 5 hand-written alerts (`002`, rule ids like `HVAC-TEMP-HIGH`, evidence without `facts`/`events`) do not match `alert_config` rules and cannot be validated. The tests use `002` **without** that block; delete it from 002 and let the engine generate alerts.
6. Validation is a grounding check (values, citations, playbook membership), not a semantic check - the DATA_MODEL rule 7 vs contract decision is still open with Amr.
7. Incident rerun: new alerts attach to open/acknowledged incidents; a resolved incident is never reopened (a new one is created).
8. Evidence lists at most 10 events per alert (first, last, peak always included); the rest is disclosed in `limitations`.

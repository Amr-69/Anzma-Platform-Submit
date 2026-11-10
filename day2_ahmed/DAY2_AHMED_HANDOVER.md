# Day 2 - Ahmed: hand-over

Workflow covered: stored facts -> alert detection -> incident -> evidence -> explanation -> grounding check -> recommendation.

## Schema and load order
Authoritative schema for Day 2 (decision D2): **`anzma_schema_and_seed.sql`** (schema + demo seed in one file) plus
**`migrations/003_incidents.sql`**, which adds only the `incidents` table, its indexes and the nullable
`alerts.incident_id`. Nothing in the schema file is renamed or restructured; the old `001_unified_schema.sql` /
`002_seed_demo.sql` no longer exist. The seed contains **no hand-written alerts and no "demo" events**: every alert
and incident is produced by the engine from stored telemetry/events.

Load order, always: **1) `anzma_schema_and_seed.sql`  2) `migrations/003_incidents.sql`** (003 is idempotent).
If an earlier version of 003 was applied to a database, rebuild that database (the grouping rule name changed).

## Files
| File | What it does |
|---|---|
| `anzma_schema_and_seed.sql` | Schema + seed. Only edit in Day 2: removed the 4 hand-written alerts and the `"demo":true` fire event. |
| `migrations/003_incidents.sql` | incidents table, indexes, `alerts.incident_id`. |
| `db_adapter.py` | Only place that touches the tables. Works with psycopg2 **or** psycopg v3 (`ANZMA_DRIVER` forces one); `connect()` / `guard()` raise `DatabaseUnavailable` when the DB is unreachable or lost. |
| `alert_engine.py`, `alert_config.py` | Rules, thresholds, playbooks (every action has `status` + `approver`; only `approved` is ever returned). |
| `incident_engine.py` | Grouping (same asset, or same system type in the same location, within 15 min), severity, ranking, lifecycle. |
| `evidence.py` | `alert_evidence()` / `incident_evidence()`: rows re-read from the fact tables and verified. |
| `validation.py` | Grounding check. Status `grounding_supported` / `failed` / `not_validated`, `validation.scope = "grounding"`. |
| `pipeline.py` | `detect()` (returns `as_of`), `explain_alert()`, `explain_incident()`. |
| `recommendations.py`, `ai_explainer.py`, `integrity_rules.py` | As described in their docstrings. |
| `e2e_s1.py` | Evidence run for the S1 cooling failure on stored data: rows before detection, alert + incident, verified-evidence count, explanation + grounding check + recommendation, idempotent re-run. Output saved in `E2E_S1_OUTPUT.txt`. |
| `CONTRACT_CHANGES.md` | What the API owner must align. |
| `test_day2_integration.py` (43 tests, real PostgreSQL) and `test_day2_ahmed.py` (25 tests, offline) | See `TEST_RESULTS_DAY2.txt`. |

## Run
```
pip install psycopg2-binary            # or: pip install "psycopg[binary]"
export PGHOST=localhost PGPORT=5432 PGUSER=postgres PGPASSWORD=...   # a user that may CREATE DATABASE
python test_day2_ahmed.py              # offline
python test_day2_integration.py        # builds anzma_day2_test from the schema file + 003, prints PASS/FAIL
python e2e_s1.py --build               # S1 evidence run on a fresh database; add --llm to try Ollama (A7/A8)
```
Throw-away Postgres in Docker: `docker run --rm -d --name pg -e POSTGRES_PASSWORD=pw -p 5432:5432 postgres:16`
then `export PGUSER=postgres PGPASSWORD=pw`.

## Faults that still raise alerts (seed after the clean-up)
| Fault in the seed | Rule(s) |
|---|---|
| CRAC 1 temperature +9 C (S1) | `HVAC_HIGH_TEMP` + `HVAC_TEMP_ANOMALY` (one incident) |
| Core Switch 1 packet loss and link down | `NET_PACKET_LOSS`, `NET_LINK_DOWN` |
| 3 denied badges at the Server Room reader | `ACC_DENIAL_SPIKE` |
| Server Room door forced | `ACC_DOOR_FORCED` |
| Main door held open 240 s | `ACC_DOOR_HELD_OPEN` |
| Control Room camera offline (closed 60 min outage and the open one) | `CCTV_OFFLINE_LONG` x2 |
| Smoke at the Server Room detector (cleared by operator) | `FIRE_SMOKE` (closed/historical) |
Gone with the demo event: the open fire alarm at Fire Panel Main. The cleared smoke alarm at FIRE-SMK-01 is a
separate seeded scenario and was kept, so one **historical** (not active) fire alert/incident still exists.

## Known limitations (final state)
1. **Validation is grounding-only.** `grounding_supported` means numbers, times, dates, citations and playbook membership match the stored evidence. No semantic check exists, so nothing is called "validated" (D4). The DATA_MODEL rule 7 vs contract decision stays open.
2. `hvac_fault` / `hvac_recovered` events are not mapped to any rule. The CRAC alert severity comes only from the temperature threshold (> 32 C = critical); because the seed's temperature follows a daily sine curve anchored to the load time, the peak (and so warning vs critical) can differ between loads. Escalating on `hvac_fault` was not done: it would contradict the existing threshold-severity test.
3. `bandwidth_util` is stored in % but the rules/thresholds are in Mbps: `NET_BANDWIDTH_HIGH` and the bandwidth anomaly cannot fire and the packet-loss congestion-vs-link-quality split always chooses link quality.
4. Denial events carry no reason: denial alerts are `data_sufficiency = partial`.
5. No fan, filter, battery, motion or access_granted data: `HVAC_FAN`, `HVAC_FILTER`, `FAN_FAILURE` are never offered.
6. **Playbook approvals are placeholders**: every existing action is `approved` with approver "Ahmed (Day 2 playbook owner)". A real engineer must review and replace them (`DEFAULT_APPROVER` in `alert_config.py`).
7. Incident links: no parent/child dependency table exists, so only "same asset" and "same system in the same location" link alerts. An alert that links two open incidents is attached to the most recent one.
8. `as_of` is the latest stored record, not the wall clock; a stale dataset is reported as stale, not as "now".
9. Evidence lists at most 10 events per alert (first, last, peak always included); the rest is disclosed in `limitations`.

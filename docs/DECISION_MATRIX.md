# Decision Matrix: Merging Amr's and Ahmed's Components into Anzma AI Insight

| | |
|---|---|
| **Status** | Draft v2 for team review (replaces the first, incomplete draft) |
| **Date** | 2026-11-08 |
| **Target** | One platform on PostgreSQL 16 using the unified schema (`001` migration, "Anzma AI Insight - unified schema") |
| **Code read for this version** | `Anzma-Working-main.zip` = **Ahmed's repository** (files dated 2026-11-05; archive comment `5f0d071a985fa4a83b5e335e3a90f039e2bd6531`, most likely the commit SHA) |
| **Code not available** | **Amr's repository** was not in the archive |

---

## 0. How to read this document

**Two evidence levels.** The first draft said "these are provisional until we read the code." This version has read Ahmed's code; it has not read Amr's.

| Tag | Meaning |
|---|---|
| **(verified)** | Checked against Ahmed's source files in the archive. File and function names are given so you can re-check. |
| **[R] reported** | Taken from the first draft of this matrix. Amr's code was not available, so nothing tagged [R] has been confirmed. |

A decision that rests only on an [R] claim stays **provisional**. Section 8 lists exactly what to check in Amr's repository to promote each one.

**Decision verbs**

| Verb | Meaning |
|---|---|
| **RETAIN** | Take as is, then extend. |
| **ADAPT** | Take the design, change it to fit. |
| **MERGE** | Combine named parts from both sides. |
| **REBUILD** | New work; nothing worth reusing. |
| **DEFER** | No decision until the code has been reviewed. |

**IDs.** Each decision has an ID (`DM-01` ... `DM-11`) so tickets, commits and reviews can refer to it. Open questions are `D1` ... `D8` (section 7). Cross-cutting code findings are `F1` ... `F14` (section 4).

---

## 1. Summary matrix

| ID | Component | Amr [R] | Ahmed (verified) | Decision | Confidence | Size* |
|---|---|---|---|---|---|---|
| DM-01 | Database engine | SQLite | PostgreSQL | **PostgreSQL 16** (Ahmed's engine; migration 001 is the schema) | High | S |
| DM-02 | Schema and ingestion | Normalized reference tables, telemetry/events split, 409 on duplicates, strict validation | Atomic 1-5000 batch, future-timestamp check, single `events` table | **MERGE**: Amr's schema design and validation rules on Postgres, plus Ahmed's atomic batch and future-time rule, plus the missing compatibility tables | Medium | L |
| DM-03 | KPI engine | 8 KPIs in `dashboard_core.py`, tests, building and custom-range filters | 8 KPIs in `dashboard_api.py` with open-incident netting | **MERGE**: Amr's structure, tests, filters; Ahmed's recovery netting and active-fire logic; each KPI defined once | Medium | M |
| DM-04 | Dashboard UI | SVG charts, filters, health summary | Chart.js (bundled), system filter, live alert toasts | **ADAPT** Amr's as the shell; add "No data" status and heartbeat compliance; take Ahmed's trend charts only where SVG cannot do the job | Low | M |
| DM-05 | Alert and anomaly | Exists, unseen | 16 rule IDs (14 threshold/compound + 2 anomaly), MAD z-score, evidence, evaluation | **ADAPT Ahmed's**: add status lifecycle, auto-close, incremental processing, precision check. Amr's alerting: **DEFER** | High (Ahmed) | L |
| DM-06 | Incident management | Unknown | None | **REBUILD** (new). Seed from Ahmed's open-incident logic and the S4 compound rule | High | L |
| DM-07 | AI orchestration | LLM tool routing, 6 tools, memory | Keyword router, SQL collectors producing numbered facts | **MERGE**: Amr's tool registry and memory; Ahmed's collectors become the tool implementations | Medium | M |
| DM-08 | Evidence and validation | Not seen | Grounding validators (alerts and chat), fallback, stored evidence | **RETAIN** Ahmed's, extend with semantic checks; Amr's assistant must go through it | High | M |
| DM-09 | Recommendations | `get_corrective_priorities` | Playbook plus repair-priority score | **ADAPT** Ahmed's playbook (as versioned, approvable data), Amr's tool as the wrapper | High | M |
| DM-10 | Model integration | Cloud providers, selectable | Ollama/Qwen, env config, graceful fallback | **ADAPT**: one `ModelProvider` interface, `OllamaProvider` first; cloud providers off by default (see D1) | High | S |
| DM-11 | API and layout | Packages, `/api/v1`, Pydantic schemas | Flat root files, router factories | **Amr's layout as the skeleton**; Ahmed's modules move into it | Medium | M |

\* Rough relative size only (S, M, L). No effort estimate is implied.

**Reading the confidence column.** High = the decision rests on code that has been read and, where applicable, measured. Medium = it depends partly on an [R] claim. Low = it depends mainly on an [R] claim.

---

## 2. Tie-breakers used for every decision

These are the rules applied when the two sides disagreed. If the team disagrees with a decision, argue with the rule, not just the outcome.

1. **Traceability first.** Every alert and every answer must trace to stored rows. This is the stated success criterion of Day 4 (`ALERTING.md`) and is built into migration 001 (hard foreign keys from `evidence` to source rows).
2. **Facts first, model second.** Numbers are computed in SQL or code; the model only phrases; a validator checks; a deterministic fallback exists.
3. **Silence is never healthy.** A device that sends nothing is `UNKNOWN`/`STALE`, not `OK`. Migration 001 enforces this in a `CHECK` on `asset_state`; Ahmed's chat collectors already do it in `coverage()`.
4. **Configuration is data, not code.** Thresholds, working hours and playbooks live in tables, not Python constants.
5. **Append-only facts, nothing deleted.** Telemetry and events are never updated or deleted; every foreign key is `ON DELETE RESTRICT`.
6. **Define each thing once.** One definition per KPI, one status calculation, one rule catalogue.
7. **Prefer what has been run and measured** over what has not been seen. This is why Ahmed's alerting and validation are retained, and why Amr's alerting is deferred rather than rejected.

---

## 3. Component decisions

### DM-01 Database engine

**Decision: PostgreSQL 16.** Ahmed's engine; migration 001 is the schema.

**What exists (verified)**
- `App.py`: `psycopg2` `SimpleConnectionPool(1, 10)`, `RealDictCursor`, a `db()` context manager that commits on success and rolls back on error; batch insert with `execute_values`.
- `schema.sql` / `schema_day4.sql`: `TIMESTAMPTZ`, `JSONB` (`events.details`, `alerts.evidence`, `alerts.explanation`), arrays (`INT[]`, `BIGINT[]`, `TEXT[]` in `scenario_runs`).
- Dashboard SQL uses `DISTINCT ON`, `FILTER (WHERE ...)` and `details->>'downtime_sec'`.

**Amr [R]:** SQLite.

**Why Postgres**
- The platform has several concurrent actors: live ingestion, the alert run, dashboard reads and chat. SQLite allows many readers but one writer at a time.
- Timestamps must be timezone-aware (`TIMESTAMPTZ`), and device-specific payloads need `JSONB`.
- Migration 001 uses identity columns, plpgsql triggers, `pg_timezone_names` (IANA validation), JSONB type checks and `TEXT[]`. None of these are portable to SQLite.

**Cost of this choice**
- Anything Amr wrote in SQLite dialect (placeholders, date functions, autoincrement) must be ported.
- Data in Amr's SQLite files is loaded with `source = 'legacy-migration'` (the column default in migration 001 anticipates this).

**Acceptance criteria**
- Migration 001 applies cleanly on an empty PostgreSQL 16 database.
- The existing 48-hour generated dataset loads through the new ingestion path (DM-02).
- All integration tests run against Postgres only; no SQLite code path remains.

---

### DM-02 Schema and ingestion

**Decision: MERGE.**

| Contribution | From | Status |
|---|---|---|
| Normalized reference tables; telemetry/events split | Amr | [R]; matches migration 001 |
| 409 on duplicates | Amr | [R] |
| Strict validation | Amr | [R] |
| Atomic batch of 1-5000 | Ahmed | (verified) `IngestBatch` min 1, max 5000; one transaction |
| Future-timestamp rule (> 5 min ahead rejected; naive timestamps read as UTC) | Ahmed | (verified) `EventIn.utc_and_not_future` |
| Per-item error list with index; whole batch rejected on an unknown asset (HTTP 422) | Ahmed | (verified) `ingest()` |
| `event_type` normalised to `UPPER_SNAKE_CASE` | Ahmed | (verified) |
| **Event-type and metric-type compatibility tables** | neither | Missing in both; present in migration 001 as `event_type_asset_types` and `metric_type_asset_types` |

**Why the compatibility tables are needed (verified)**
- In Ahmed's schema `events.event_type` is free text (`VARCHAR(50)`); nothing says which asset may emit which type.
- The committed outputs (`README-Day5 (3).md`, `TEST_RESULTS.md`) show `HVAC-AHU-01` with an open "Smoke alarm active" incident. None of the repository's generators or seed scripts writes `SMOKE_DETECTED` to an HVAC asset (`gen_fire`, scenario S5 and `seed_events.py` all target fire panels), so the origin of that row is **unverified**. The point stands either way: the current schema would accept it, the compatibility matrix would reject it.
- `seed_events.py` writes `PACKET_LOSS` events that no rule reads (the engine reads `packet_loss_pct` from `BANDWIDTH_USAGE.details`).
- `seed_history.py` writes `ENERGY_READING` to an HVAC asset because no energy asset exists (`METER` falls back to `HVAC`).

**Gaps in Ahmed's ingestion that the merge must close (verified)**
- No duplicate protection: `/ingest` is a plain `INSERT` and `schema.sql` has no unique constraint, so replaying a batch doubles the data.
- No plausibility check on `value`/`unit`, and no check that the severity matches the type.

**Target ingestion flow**

| Step | Rule | Source |
|---|---|---|
| 1. Parse | Batch size 1-5000; timestamps to UTC; reject > 5 min in the future (also a DB `CHECK`) | Ahmed |
| 2. Resolve asset | By id or by code/name; unknown asset rejects the batch | Ahmed |
| 3. Classify | Type found in `metric_types` becomes a `telemetry` row; type found in `event_types` becomes an `events` row; unknown type rejected (422) | Amr [R] |
| 4. Compatibility | Type must be allowed for the asset's type | Migration 001 |
| 5. Range / boolean check | Value inside `[min_value, max_value]`; boolean metrics accept only 0 or 1 | Migration 001 trigger |
| 6. Duplicates | Telemetry: primary key `(asset_id, metric_type_id, source_ts)`. Events: `idempotency_key = sha256(asset_code \| event_type \| source_ts \| canonical payload)` | Migration 001 |
| 7. Insert | Atomic: all rows or none | Ahmed |

**Implementation notes**
- **Pre-validate in the API** (from a cached catalogue) and keep the database triggers as the backstop. Otherwise a range violation surfaces as a database exception for the whole batch with no item index, instead of the indexed 422 Ahmed's API gives today.
- Telemetry has no `details` column. The engine currently reads `fan_on`, `setpoint`, `humidity`, `packet_loss_pct`, `battery_pct` and `mains_ok` from event `details`. These become separate metrics (boolean metrics exist for exactly this). See Appendix B and DM-05.
- Status codes: 201 success, 409 duplicate, 422 validation failure. Open question **D2** covers a duplicate inside an otherwise valid atomic batch.
- Proposed (not in either side): reject ingestion for assets with `lifecycle_status = 'DECOMMISSIONED'`.

**Test-data tooling must change (see F7).** Migration 001 makes `events` and `telemetry` append-only with `ON DELETE RESTRICT` everywhere. Ahmed's tooling relies on deletion:
- `generateData.py --reset` runs `TRUNCATE events RESTART IDENTITY`; with foreign keys from `alerts`/`evidence` this fails without `CASCADE`.
- `inject_scenarios.py` replaces baseline readings inside each scenario window with `DELETE FROM events ...`, and `reset()` deletes injected events by id. The new delete trigger rejects these.
- The generator must therefore compose scenario windows at generation time (baseline replaced before insert), and "reset" becomes drop-and-recreate of a scratch database or schema.

**Acceptance criteria**
- Re-posting an identical batch inserts nothing and returns 409.
- A batch with one unknown asset stores nothing and returns 422 with the item index.
- An HVAC asset cannot record `SMOKE_DETECTED`.
- A temperature of 900 °C is rejected with an indexed 422, not a 500.
- A timestamp 6 minutes ahead is rejected at the API and, if bypassed, at the database.

---

### DM-03 KPI engine

**Decision: MERGE.** Keep Amr's structure, tests and filters [R]. Use Ahmed's logic for recovery netting and active fire alarms. Define each KPI once.

**What exists (verified)** (`dashboard_api.py`)
- 8 KPIs computed at request time from `events`.
- "Now" is `MAX(ts)` of `events`. Window is `(now - hours, now]`.
- Asset status: an open incident (latest event of a start/end pair is the start event: `LINK_DOWN`/`LINK_UP`, `CAMERA_OFFLINE`/`CAMERA_ONLINE`, `SMOKE_DETECTED`/`ALARM_RESET`) or a fault event in the last 60 minutes (`STATUS_WINDOW_MIN`). System status is the worst asset status.
- Active fire alarms: latest `SMOKE_DETECTED`/`ALARM_RESET` per fire panel, not limited to the window.

**Amr [R]:** 8 KPIs in `dashboard_core.py`, with tests and building and custom-range filters. Whether his 8 KPIs are the same 8 is unknown.

**Problems found in the current KPI definitions (verified)**

| KPI | Issue | Target definition |
|---|---|---|
| Total events | After the telemetry/events split, "event" no longer means what it did (today it is dominated by temperature and bandwidth readings). | Count rows in `events` only; show telemetry volume separately as "data points received". |
| Critical alerts | Counts raw events with device-reported severity `CRITICAL`, including ones already recovered. It is not the alert engine's count. | Rename "Critical events"; add "Open critical alerts" from `alerts` (status `OPEN`/`ACKNOWLEDGED`). |
| Network availability | Counts downtime only when the `LINK_UP` falls inside the window, from the device-reported `downtime_sec`; an outage still in progress is excluded; the result is clamped at 0. | Time-weighted from `LINK_DOWN`/`LINK_UP` pairs, clipped to the window; an open outage counts up to `as_of`. |
| Camera offline incidents | Counts `CAMERA_OFFLINE` events, including recovered ones. | Keep as "incidents in window"; add "cameras offline now". |
| Active fire alarms | Correct logic. | Read from `asset_state.open_fault_event_id`. |
| Avg bandwidth, access denial rate | Fine. | Unchanged. |
| Avg HVAC temperature | Mixes `TEMPERATURE_READING` and `HIGH_TEMPERATURE`. | Depends on D6 (how `HIGH_TEMPERATURE` is stored). |

**Define each KPI once (verified duplication)**
- `chat_engine.asset_states()` re-implements `dashboard_api.asset_health()` (its docstring says "same status logic").
- `chat_engine.net_availability()` re-implements availability.
- `chat_engine` imports `FAULT_TYPES`, `OPENERS`, `INCIDENT_TYPES` from `dashboard_api`, which are three hand-maintained dictionaries.

**Target design**
- A single **KPI registry**: key, label, unit, system, definition text, function. The dashboard, the chat `kpi` intent and `KPI_DEFINITIONS.md` are all generated from it.
- `FAULT_TYPES` is derived from `event_types.is_fault` plus the compatibility matrix. `OPENERS`/`INCIDENT_TYPES` are derived from `event_types.resolves_event_type_id` (a `RECOVERY` event points at the event it closes). The three hard-coded dictionaries disappear.
- Every KPI and status function takes an explicit `as_of`. Default is the wall clock. Demo and evaluation data pass an explicit replay time. This removes the "latest event is now" shortcut (F6) without breaking the synthetic demo (open question **D4**).
- Asset status moves to `asset_state` (`operational_status`, `freshness`, `status_reason`, `status_since`, `computed_as_of`).

**Tests.** Ahmed's repository has no unit tests for KPIs; the only automated checks are `test_chat.py` (end-to-end, needs the live API, database and Ollama) and a 23-line `test_citations.py`. Amr's tests [R] are taken as the base. Each KPI gets a fixed fixture dataset with hand-computed expected values.

**Acceptance criteria**
- Each KPI has exactly one implementation; searching the code for its formula finds one place.
- A network outage that starts before the window and ends inside it contributes only its in-window part.
- An ongoing outage reduces availability.
- With no events at all from a system, its KPIs show "n/a" and its status is not `OK`.
- The dashboard KPI table and `KPI_DEFINITIONS.md` cannot disagree (generated from the same registry).

**Depends on:** DM-01, DM-02 (event catalogue).

---

### DM-04 Dashboard UI

**Decision: ADAPT** Amr's as the shell [R]. Add "No data" status and heartbeat compliance. Bring in Ahmed's trend charts only where SVG cannot do the job.

**What exists (verified)** (`static/dashboard.html`, 1274 lines)
- Chart.js bundled as `static/chart.umd.js` (345 KB). Four trend charts: events by system, HVAC average temperature, bandwidth (average and peak), access granted vs denied.
- System filter and a 6/12/24/48-hour window selector; KPI cards with hover definitions; equipment-health table sorted by urgency; an Alert Center panel; Socket.IO toasts for new alerts.
- **The page is not fully offline:** it loads the Socket.IO client from `https://cdn.socket.io/...` (F9), although the README says Chart.js is bundled "so the dashboard works offline".

**Amr [R]:** SVG charts, filters, health summary. Not seen.

**What to take, from where**

| Item | Source |
|---|---|
| Page shell, filters (building, custom range), health summary | Amr [R] |
| SVG chart components | Amr [R] |
| Hover definitions on KPIs | Ahmed (verified); now generated from the KPI registry (DM-03) |
| Four trend charts | Ahmed; re-draw in SVG if Amr's components can do them, otherwise keep Chart.js. **Do not ship two libraries rendering the same chart** (tie-breaker 6) |
| Live alert push (`new_alert` over Socket.IO) and Alert Center | Ahmed (verified); vendor the client file into `static/` |
| **"No data" status** | New: from `asset_state.freshness` (`NO_DATA`, `STALE`) |
| **Heartbeat compliance** | New: share of assets whose `last_seen_at` is within `asset_types.stale_after_s` |

**Acceptance criteria**
- A system with no events shows "No data", never "OK" (tie-breaker 3).
- A stale asset shows `STALE` with its last-seen time.
- The page works with the network disconnected (no CDN requests).
- Every KPI card shows the definition from the registry.

**Depends on:** DM-03 (registry, `asset_state`), DM-11 (API shape). **Confidence is low** because Amr's UI has not been seen; see section 8.

---

### DM-05 Alert and anomaly engine

**Decision: ADAPT Ahmed's.** Add an alert status lifecycle, auto-close, incremental processing and a precision check. Amr's alerting is **DEFER** pending code review.

**What exists (verified)**
- `alert_config.py`: `RULES` has **16 rule IDs**: 13 threshold, 1 compound (`SEC_BLIND_SPOT_ACCESS`), 2 anomaly. (`ALERTING.md` tabulates 14 rules, which is the 13 + 1 without the anomaly rules; the 16 includes them.) Appendix A lists all 16.
- `alert_engine.py`: sustained-condition runs, burst windows, a compound rule, and a robust z-score anomaly detector: `z = 0.6745 x (value - median) / MAD`, per-asset baselines split into working and off hours from 7 days of history, flagged at `|z| >= 4` **and** a minimum absolute deviation, alert after 3 consecutive same-direction readings.
- Each alert stores evidence (up to 10 events: first, last, peak, even sample), measured facts and thresholds.
- Measured (from `ALERTING.md`): 6 of 6 scenarios detected; 14 of 14 expected rules fired; mean time to first expected alert 12.9 min; 93 of 93 evidence events found unchanged in `events`.

**What changes**

| Area | Today (verified) | Target |
|---|---|---|
| Persistence | `store_alerts()` **deletes** every alert in the window and re-inserts. Alert ids change per run; stored explanations are wiped. Conflicts with migration 001 (`dedup_key` upsert, `ON DELETE RESTRICT`). | Upsert on `dedup_key = rule_id\|asset_id\|first_ts`; never delete; update `last_ts`, `facts`, and escalate severity. |
| Lifecycle | `status` column allows `OPEN/ACKNOWLEDGED/RESOLVED` but **nothing ever changes it** (no endpoint, no engine step). "Open alerts" only grows (F2; the Day 5 reflection notes 16 open critical alerts). | `OPEN`, `ACKNOWLEDGED` (manual), `RESOLVED` (manual), `AUTO_RESOLVED` per rule (`resolution_mode`). Proposed mode per rule in Appendix A. |
| Execution | Batch: `POST /alerts/run?hours=` reloads 7 days of events for every asset on each run. | Incremental: keep a watermark on `received_at`; re-evaluate only affected asset/rule windows. Keep batch mode for backfill and replay. |
| Configuration | Thresholds in a Python dict `T`; working hours `LOCAL_UTC_OFFSET = 2`, 08-18 in code; the generator also writes `details.after_hours`, which `CCTV_AFTER_HOURS_MOTION` trusts. | Thresholds in `alert_rules.config`; working hours from `buildings.timezone`, `work_start_local`, `work_end_local`; `after_hours` always computed from the timestamp, never trusted from the payload (F5). |
| Zone logic | Rules test `zone == "Server Room"` by name. | Use `locations.zone_type` / `is_sensitive` (migration 001 comment: "rules use this, not the zone name"). |
| Inputs | Rules read `fan_on`, `packet_loss_pct`, `battery_pct`, `mains_ok` from event `details`. | Read them as telemetry metrics (boolean and numeric) joined on timestamp (Appendix B). |
| Liveness | No "device went silent" rule; `PANEL_HEARTBEAT` is used only for battery. | Liveness rules driven by `asset_types.expected_interval_s` / `stale_after_s`, feeding `asset_state.freshness`. |
| Evaluation | Recall measured on 6 injected scenarios; **precision not measured**. Alerts on the generator's background faults are listed but not scored. | Add a precision check (below). |
| Ground truth | `scenario_runs` in the public schema. | `eval.scenario_runs`; the engine must never read the `eval` schema. |
| Push | Compares (rule, asset, first_ts) before and after the run to find "new" alerts. | With upsert, "new" = rows inserted by this run. |

**Precision check (new).** Recall alone rewards noisy rules.
- Add **quiet scenarios**: windows with no injected fault, stored with `expected_rules = '{}'` (allowed by `eval.scenario_runs`). Target: zero alerts in a quiet window.
- Label the generator's background faults as expected (they are real events in the data) so alerts on them count as true positives, and report per-rule precision = true alerts / all alerts.
- Report both numbers next to recall and latency in the existing evaluation output.

**Keep unchanged in the first merge:** the detection logic of every rule and the anomaly detector (only their parameters move to config). Changing method and plumbing at the same time would make the evaluation numbers uninterpretable. The known weakness that the baseline includes the window under test (`ALERTING.md`, "Baseline contamination") is a later change, measured by the new precision check.

**Amr's alerting [R] (DEFER).** When his code is available, compare it against Ahmed's six-scenario evaluation (recall, latency, evidence per alert). Adopt only rules that add coverage **and** can supply evidence rows with hard foreign keys.

**Acceptance criteria**
- Re-running the engine over the same window changes no alert id, status or explanation.
- A `LINK_UP` after a `LINK_DOWN` moves the `NET_LINK_DOWN` alert to `AUTO_RESOLVED`.
- The six scenarios still detect 6 of 6, with 14 of 14 expected rules.
- A quiet window produces zero alerts.
- No threshold, working-hour value or zone name remains in Python code.

**Depends on:** DM-01, DM-02, DM-03 (`asset_state`), the `alert_rules`/`playbook_entries` tables.

---

### DM-06 Incident management

**Decision: REBUILD (new).** Nothing to reuse except Ahmed's open-fault logic and his S4 compound rule as the seed for correlation.

**What exists (verified)**
- No grouping of alerts. `ALERTING.md` states it: "several rules can fire for one underlying fault ... they are listed separately."
- The word "incident" in Ahmed's code means something else: an unresolved start/end event pair (`LINK_DOWN` without `LINK_UP`, and so on) used to set asset status. This is **not** the `incidents` table of migration 001, which groups alerts. To avoid two meanings, Ahmed's concept is renamed **open fault** (`asset_state.open_fault_event_id`); "incident" is reserved for the grouping.
- `SEC_BLIND_SPOT_ACCESS` (camera offline while the door is active, same location) is the one place where two assets are already correlated by a rule.

**Amr [R]:** unknown.

**Why it is needed (from the six scenarios in `ALERTING.md`)**

| Scenario | Rules that fire for one underlying fault |
|---|---|
| S1 Server-room cooling failure | `HVAC_TEMP_ANOMALY`, `HVAC_HIGH_TEMP` |
| S2 Core switch saturation and flapping | `NET_BANDWIDTH_HIGH`, `NET_PACKET_LOSS`, `NET_LINK_FLAPPING` |
| S4 Camera blind spot | `CCTV_OFFLINE_LONG`, `SEC_BLIND_SPOT_ACCESS`, `ACC_DOOR_HELD_OPEN` (camera and door are different assets) |
| S5 Fire panel on battery, then smoke | `FIRE_BATTERY_LOW`, `FIRE_SMOKE` |
| S6 Clogged filter and drift | `HVAC_FILTER_FAULT`, `HVAC_TEMP_ANOMALY`, `HVAC_HIGH_TEMP` |

An operator should see one incident per underlying fault, not three to five alerts.

**Target model (migration 001)**
- `incidents` with `correlation_key`; a partial unique index allows **one open incident per key**.
- `incident_alerts` with `alert_id UNIQUE`: an alert belongs to at most one incident. `link_reason` is one of `SAME_ASSET`, `SAME_LOCATION`, `COMPOUND_RULE`, `MANUAL`.

**Proposed correlation rules (to be calibrated on the six scenarios; not yet implemented anywhere)**

| Link reason | Rule |
|---|---|
| `SAME_ASSET` | Alerts on the same asset whose `[first_ts, last_ts]` intervals overlap or are within a gap G of each other (start with G = 60 min). |
| `COMPOUND_RULE` | A compound rule's alert joins the incident of the alerts that satisfied its conditions. |
| `SAME_LOCATION` | Only within the **security family** (`ACC_*`, `CCTV_*`, `SEC_*`), same location, within G. Restricting it matters: the server room holds the CRAC unit, the core switch, a door and a camera, so an unrestricted location rule would merge unrelated faults that happen at the same time. |
| `MANUAL` | An operator links or unlinks. (Merging two open incidents is out of scope for the first version.) |

- **Correlation key** has no time component so that a later alert can join the open incident: for example `TOWER-A|SW-CORE-01|NETWORK` for asset links, `TOWER-A|1|Server Room|SECURITY` for location links. Once the incident is resolved the key becomes free.
- **Severity** = highest member severity. **`opened_at`** = earliest member `first_ts`. **Resolution:** the incident resolves when every member alert is resolved (`resolved_at >= opened_at` is enforced by the schema).
- **Explanations and recommendations** may attach to an incident (`incident_id` columns exist). The first version shows a deterministic incident summary built from its alerts and reuses per-alert explanations.

**Acceptance criteria**
- Each of S1-S6 yields exactly one incident containing the alerts of all its expected rules.
- No incident mixes two scenarios. **Check against the injected windows first:** `inject_scenarios.py` chooses the scenario times, and S3 and S4 both involve the server-room door at night; if two scenarios touch the same asset within G, reduce G or record the merge as expected.
- Two open incidents never share a correlation key (schema-enforced).
- The dashboard and chat report "open incidents" from this table, and "open faults" for the old start/end concept.

**Depends on:** DM-05 (lifecycle, so incidents can resolve), DM-01.

---

### DM-07 AI orchestration

**Decision: MERGE.** Amr's tool registry and memory [R]. Ahmed's collectors become the implementations of those tools.

**What exists (verified)** (`chat_engine.py`, 694 lines; `chat_api.py`)
- Pipeline: classify, gather, compose, validate, fallback.
- `classify()`: Arabic-normalised keyword matching in a fixed order (energy, cameras, network, repair, faults, kpi, history, status); Ollama is asked to classify only when no keyword matches; otherwise `out_of_scope`, which returns a fixed reply without touching the database.
- Eight collectors (`c_status`, `c_faults`, `c_cameras`, `c_network`, `c_repair`, `c_energy`, `c_kpi`, `c_history`) write numbered facts `F1, F2, ...` in the user's language, with `data_sufficient` and `required` (must-cite) markers.
- Window: parsed from the question ("last 3 days", "اخر 12 ساعة") or the `hours` field; default 72 h; ends at the latest event. "Yesterday" means the previous full UTC day.
- The database cursor is released before any model call.
- Tests: 20 questions (English and Arabic, both modes), 41 of 41 checks pass (`test_chat.py`, `TEST_RESULTS.md`).

**Amr [R]:** LLM tool routing, 6 tools, memory. The only tool named anywhere is `get_corrective_priorities`.

**Merge design**

| Ahmed collector (verified) | Proposed tool | Note |
|---|---|---|
| `c_status` | building status | |
| `c_faults` | recent faults | |
| `c_cameras` | camera availability | |
| `c_network` | network issues | |
| `c_repair` | `get_corrective_priorities` [R] | Wrapper over the repair-priority score (DM-09) |
| `c_energy` | energy change | Reports "insufficient data" until real meter data exists |
| `c_kpi`, `c_history` | KPI, historical reading | No known Amr equivalent; keep as additional tools |

The mapping assumes Amr's six tools correspond to the six mandatory questions. That is unverified; the tool names other than `get_corrective_priorities` are placeholders.

**Rules for the merged assistant**
1. **A tool returns facts, never free text or raw rows.** The model never queries the database. This keeps tie-breakers 1 and 2.
2. **Every answer goes through the validator** (DM-08). No code path may return model text unchecked.
3. **Routing.** Recommended: keep Ahmed's keyword pass first (cheap, predictable, tested) and let Amr's LLM tool selection handle unmatched or multi-tool questions; log every routing decision. Ahmed's own reflection says keyword routing is brittle to unusual phrasing, so let a **labelled question set** decide: measure intent accuracy of both and keep whichever order scores better.
4. **Memory stores intent, window and entities only, never facts or previous answers.** Follow-ups ("and yesterday?", "which one is the worst?") re-run the collectors with updated parameters, so a remembered answer cannot go stale. This closes Ahmed's documented limitation (no conversation memory).
5. **Feed freshness.** If the latest event is older than a threshold, add a required fact saying so, so a stalled feed is never read as a calm building (F6, Ahmed's reflection item 9).
6. The answer language follows the question's language (Ahmed); keep.

**Acceptance criteria**
- All 20 existing test questions still pass in both modes through the merged path.
- "And yesterday?" after a temperature question returns yesterday's temperature.
- A tool cannot be called with parameters outside its schema.
- Intent accuracy on a labelled question set is reported with each change to routing.
- With the feed stopped for longer than the threshold, status answers say so.

**Depends on:** DM-03 (registry, `as_of`), DM-08, DM-10.

---

### DM-08 Evidence and validation

**Decision: RETAIN Ahmed's, then extend with semantic checks.** Amr's assistant must go through it.

**What exists (verified)**
- Two validators with the same design:
  - `ai_explainer.check_text` (alerts): at least one `[E#]` citation and every citation exists; every date, time and number must appear in the evidence package (numbers within 0.051 or equal as integers); square brackets reserved for citations. `normalize_summary` fixes citation formatting only. One corrective retry, then a deterministic fallback.
  - `chat_engine.check` (chat): `[F#]` citations; asset names copied exactly; facts marked `required` must be cited; dates, times and numbers must appear in the cited facts (or the window fact); a fixed list of judgment phrases ("without issues", "working well", ...) is rejected unless a fact states it. One retry, then fallback.
- Evidence is stored inside `alerts.evidence` as JSONB. `event_id` values point at `events` rows but are **not foreign keys**; at most 10 events are kept (first, last, peak, even sample); only whitelisted detail keys are kept (`SAFE_DETAIL_KEYS`).
- Measured: alert explanations 14 of 14 passed on the first attempt (`ALERTING.md`). Chat results differ between the committed runs (F11): 15 or 16 of 20 answers validated from the model, the rest fell back.
- Known weakness (`REFLECTION.md`): the validator checks facts, not meaning. Accepted answers still included a network described as "operating without issues" despite 6 critical events, `DOOR_FORCED` rendered in Arabic as "door opened", a fault list that skipped the top network fault, and garbled citation placement. Numbers written as words are not checked.

**What changes**

1. **Evidence moves to the `evidence` table.** Keep `ref_label` (`E1`, `E2`, ...) so both validators run unchanged. Hard foreign keys to `events` or to `telemetry` (composite key); `snapshot` JSONB holds the verbatim row. The 10-event cap applies to what is **sent to the model**, not to what is stored. Ahmed's evidence has no roles; proposed assignment: recovery events (`LINK_UP`, `CAMERA_ONLINE`, `ALARM_RESET`) are `RECOVERY`; the filter fault attached to an HVAC alert is `CONTEXT`; anomaly baseline statistics are `BASELINE`; everything else is `TRIGGER`.
2. **Record validation honestly in `explanations`.** What the current validators prove (`numbers`, `times`, `citations`, plus `names` for chat) earns `EVIDENCE_SUPPORTED`. `VALIDATED` is reserved for answers where a semantic check ran; migration 001 enforces this with a `CHECK` that `'semantic'` is in `checks_run`. A failed check stores `UNSUPPORTED` or `PARTIALLY_SUPPORTED` with the violations. Origin: `OLLAMA` becomes `LLM`, `FALLBACK` becomes `FALLBACK_FACTS`.
3. **Add semantic checks, in this order** (from Ahmed's reflection):
   1. *Coverage:* every critical or open item in the facts must be mentioned (extends the existing `required` mechanism).
   2. *Per-sentence citation:* every sentence that states a number, time or status cites a fact.
   3. *Unsupported qualitative claims:* rule-based detection that goes beyond the fixed phrase list (for example health or absence statements when the facts carry critical items).
   4. *Optional verifier pass* (rules first, a small verifier model later).
4. **Track quality metrics:** validator rejection rate and first-attempt acceptance rate, per intent and per model.

**Acceptance criteria**
- The 14 alert explanations and the 41 chat checks still pass.
- A test reproduces the "operating without issues" case and the validator rejects it.
- Every `evidence` row resolves by foreign key to a real source row.
- A row cannot be stored as `VALIDATED` unless a semantic check is recorded (database `CHECK`).

**Depends on:** DM-05 (evidence rows), DM-07, DM-10. Open question **D7** covers whether chat answers and facts are persisted.

---

### DM-09 Recommendations

**Decision: ADAPT** Ahmed's playbook, with Amr's tool as the wrapper. Move the playbook to versioned data with an approval status.

**What exists (verified)**
- `alert_config.PLAYBOOKS`: **15 playbooks, 25 causes, 46 actions**; 19 actions are conditional. A condition is a dictionary of `fact: value` or `fact: (op, value)`, all must hold (`holds()`); `describe()` produces the `supported_by` and `because` text; `eligible()` filters causes and actions by the measured facts before the model sees them. The model selects **ids only** and cannot add causes or actions; wording always comes from the playbook. `HVAC_TEMP` serves two rules.
- Repair-priority score (`chat_engine.c_repair`): open critical fault 100, open warning fault 50, each open critical alert 20, each open warning alert 8, each fault event in the last 60 minutes 3. The rule is returned as a fact so answers can explain the ranking.

**Amr [R]:** `get_corrective_priorities` (not seen).

**What changes**

| Area | Change |
|---|---|
| Storage | Entries become rows in `playbook_entries` (`entry_id`, `version`, `entry_kind` CAUSE/ACTION, `priority` required for actions, `text`, `condition`, `approval_status`, `approved_by`, `approved_at`). Conditions convert from dictionary to list: `{"fact": ..., "op": ">=", "value": 1.0}`; a bare value becomes `op "="`. Only `=`, `>`, `>=`, `<`, `<=` exist today, matching the schema. |
| Approval | **All 71 entries start as `DRAFT`.** They were written by the development team; no engineer has approved them, and several are safety-relevant ("Dispatch security ...", "follow the site evacuation procedure and call the fire brigade", "be ready to shed non-critical server load"). `APPROVED` requires `approved_by` and `approved_at` (database `CHECK`). Production recommendations use `APPROVED` entries only; `DRAFT` entries appear only in a labelled review mode (open question **D3**). |
| Recommendation record | `recommendations` stores the exact `action_text`, the `satisfied_facts` (today's `because`), `requires_human_confirmation` (default true), and links to `evidence` rows through `recommendation_evidence`. |
| Priority score | Keep the score and publish the weights as configuration. In the score, "open incident" means **open fault** (see DM-06 terminology). Once alert auto-close exists (DM-05), "open alerts" stops being inflated by F2, so rankings will change; record before and after. |
| Wrapper | `get_corrective_priorities` calls the score collector and returns facts (DM-07). |

**Schema wrinkle found while writing this (F14).** The foreign key from `recommendations` to `playbook_entries(entry_id, version, approval_status)` uses `ON UPDATE RESTRICT`. Once any recommendation references an entry version, that version's status can never change: it cannot be set to `RETIRED`, and a `DRAFT` that was used cannot later be approved in place. This keeps history honest but leaves no way to retire a version. Options are in **D5**.

**Acceptance criteria**
- **Golden test:** for the stored facts of every scenario alert, the eligible causes and actions from the database playbook equal those from `alert_config.py` before migration.
- No `DRAFT` entry reaches a normal user.
- Every recommendation has `satisfied_facts` and at least one linked evidence row.
- The score weights are read from configuration; changing one changes the ranking in a test.

**Depends on:** DM-05, DM-08.

---

### DM-10 Model integration

**Decision: ADAPT.** One `ModelProvider` interface, `OllamaProvider` first (Ahmed's code). Amr's provider structure is the pattern [R]. Cloud providers stay out of the default build (open question **D1**).

**What exists (verified)** (`ai_explainer.py`)
- Configuration by environment: `OLLAMA_HOST` (default `http://localhost:11434`), `OLLAMA_MODEL` (default `qwen2.5:7b` in code), `OLLAMA_TIMEOUT` (default 120 s).
- One call site, `call_ollama(messages)`: `POST /api/chat`, `stream: false`, `format: "json"`, `temperature: 0`, using only the standard library. `chat_engine` reuses the same function, so swapping providers touches one function.
- Graceful degradation: any exception (server down, timeout, invalid JSON) returns the deterministic fallback with the error recorded; two validation failures also fall back; a failed classification falls to `out_of_scope`.
- Quality limit (`REFLECTION.md`): a 7B local model needs a second attempt or the fallback too often.

**Amr [R]:** cloud providers, selectable. Not seen.

**Interface sketch**

```python
class ModelProvider(Protocol):
    name: str                      # "ollama"
    model: str                     # stored in explanations.model
    def chat_json(self, messages: list[dict], *, timeout_s: int) -> ModelReply: ...

# ModelReply: text, model, latency_ms
# Errors normalised to: ProviderUnavailable, ProviderTimeout, ProviderBadOutput
```

**Contract**
- A provider receives prompts only (the evidence package or the facts). It never has database access.
- Provider errors map onto the **existing** fallback paths; orchestration code does not change.
- Provider and model are recorded for every model-written explanation (`explanations.origin = 'LLM'` requires `model` by `CHECK`).
- Selection by `MODEL_PROVIDER`; keep the `OLLAMA_*` variable names.
- Resolve the documented default mismatch (F11): code says `qwen2.5:7b`, `ALERTING.md` says `llama3.2:3b`.

**Why cloud providers are off by default.** The prompts contain access card identifiers, door and forced-entry events, and security blind-spot information. Sending them to a third party is a data-governance decision the team has not made (D1). A local default also keeps the system working with no internet.

**Acceptance criteria**
- Replacing the provider changes no file outside the providers package and configuration.
- With Ollama stopped, every endpoint still answers (fallback) within the timeout.
- Every model-written explanation row names its provider and model.
- A fake provider is used in unit tests, so validator and routing tests no longer need a live Ollama (today `test_chat.py` LLM mode does).

**Depends on:** none (can be done early).

---

### DM-11 API and layout

**Decision: Amr's layout as the skeleton** (packages, `/api/v1`, Pydantic schemas) [R]. Ahmed's modules move into it.

**What exists (verified)**
- Flat root files and router factories: `create_router(db)`, `create_alerts_router(db, push_alert)`, `create_chat_router(db)`.
- `App.py` carries ingestion models, retrieval endpoints, the Socket.IO server, imports placed in the middle of the file and deprecated `@app.on_event` hooks.
- `POST /debug/fake-alert` is live (hidden from the docs only by `include_in_schema=False`), unauthenticated, and emits fake alerts to every connected client. Socket.IO runs with `cors_allowed_origins="*"`. There is no authentication anywhere (F8).

**Endpoint mapping**

| Today (verified) | Target |
|---|---|
| `POST /ingest` | `POST /api/v1/ingest` |
| `GET /events`, `GET /assets/{id}/events` | `/api/v1/events`, `/api/v1/assets/{id}/events` |
| `GET /assets`, `/assets/{id}`, `/locations`, `/systems` | `/api/v1/assets`, `/api/v1/locations`, `/api/v1/systems` |
| `GET /dashboard/summary`, `/trends`, `/equipment-health` | `/api/v1/dashboard/...` |
| `POST /alerts/run`, `GET /alerts`, `/alerts/{id}`, `GET`+`POST /alerts/{id}/explain` | `/api/v1/alerts/...`; **new** `PATCH /api/v1/alerts/{id}/status` (acknowledge, resolve) |
| `POST /chat`, `GET /chat/questions` | `/api/v1/chat`, `/api/v1/chat/questions` |
| none | **new** `GET /api/v1/incidents`, `GET /api/v1/incidents/{id}`, `PATCH .../status` (DM-06) |
| `GET /health`, `/dashboard` (HTML), `/static` | unchanged at the root |
| `POST /debug/fake-alert` | removed from production builds; available only with a development flag |

Keep the old unversioned paths as aliases for one release: `seed_events.py`, `seed_history.py`, `test_chat.py` and every README `curl` example call them.

**Proposed package layout** (illustrative; align names with Amr's packages once confirmed)

```
app/
  main.py                # app factory, lifespan, router registration
  config.py              # environment, provider selection
  db.py                  # pool, transaction helper
  api/v1/                # routers and Pydantic schemas: ingest, events, assets, dashboard, alerts, incidents, chat
  services/
    ingestion/  kpi/  alerting/  incidents/  recommendations/
    assistant/           # tools, memory, router, collectors
    explain/             # evidence package, validators, fallback
  providers/             # ModelProvider, ollama
  realtime/              # Socket.IO
migrations/              # 001_unified_schema.sql, ...
eval/                    # scenario injection and evaluation; never imported by app/
tests/
static/
docs/
```

**Where Ahmed's files go**

| File | Destination |
|---|---|
| `App.py` | `main.py` plus `api/v1/{ingest,events,assets}` |
| `dashboard_api.py` | `api/v1/dashboard` plus `services/kpi` |
| `alert_engine.py`, `alert_config.py` | `services/alerting` plus database tables (`alert_rules`, `playbook_entries`) |
| `alerts_api.py`, `chat_api.py` | `api/v1/alerts`, `api/v1/chat` |
| `ai_explainer.py` | `services/explain` plus `providers/ollama` |
| `chat_engine.py` | `services/assistant` (collectors as tools, validation in `services/explain`) |
| `generateData.py`, `inject_scenarios.py`, `evaluate_alerts.py`, `measure_explain.py`, `seed_*.py` | `eval/` and `scripts/` |
| `static/*` | `static/` |

**Acceptance criteria**
- One documented entry point (resolves the `App:app` vs `App:asgi_app` conflict in F11; Socket.IO only works through `asgi_app`).
- Routers hold no business logic; every request and response has a Pydantic schema.
- OpenAPI lists every `/api/v1` route grouped by tag.
- The debug endpoint is absent unless the development flag is set.
- Old paths still answer during the transition.

**Depends on:** none for the skeleton (do it first, DM-02 lands inside it).

---

## 4. Findings from reading Ahmed's code

Everything here is (verified) unless marked otherwise. "Decision" points to where the finding is handled.

| ID | Finding | Where | Impact | Decision |
|---|---|---|---|---|
| F1 | `store_alerts()` deletes all alerts in the window and re-inserts them. Alert ids change per run and stored explanations are lost. | `alert_engine.py` | Breaks lifecycle, explanations and the migration's `dedup_key` upsert model | DM-05 |
| F2 | Alert `status` can never change: no endpoint and no engine step sets it. "Open alerts" only grows (Ahmed's reflection: 16 open critical alerts). | `alerts_api.py`, `alert_engine.py` | Inflates repair priorities and "open alerts" | DM-05, DM-09 |
| F3 | The KPI "Critical alerts" counts raw events with severity `CRITICAL` (including recovered ones), not alerts. | `dashboard_api.py` | Name and meaning disagree | DM-03 |
| F4 | Asset status and network availability are implemented twice (`dashboard_api` and `chat_engine`). | `chat_engine.asset_states`, `net_availability` | Two answers can drift apart | DM-03 |
| F5 | Time-of-day policy is hard-coded in several places (`LOCAL_UTC_OFFSET = 2`, 08-18) and baked into the data: the generator writes `details.after_hours`, which `CCTV_AFTER_HOURS_MOTION` trusts. | `alert_config.py`, `generateData.py`, `inject_scenarios.py` | Wrong for any other building or timezone | DM-05 |
| F6 | "Now" is the latest event timestamp in the database, in the dashboard, the alert run and the chat. | `dashboard_api`, `alerts_api`, `chat_engine` | A stalled feed looks like a calm building | DM-03, DM-07, D4 |
| F7 | Test-data tooling depends on deletion (`TRUNCATE events`, `DELETE FROM events`). | `generateData.py`, `inject_scenarios.py` | Incompatible with the append-only schema | DM-02 |
| F8 | No authentication anywhere. `/debug/fake-alert` is live and unauthenticated; Socket.IO accepts any origin. | `App.py` | Unsafe outside a lab | DM-11, D8 |
| F9 | The dashboard loads the Socket.IO client from a CDN, although Chart.js is bundled "so the dashboard works offline". | `static/dashboard.html` | Not offline-capable | DM-04 |
| F10 | Seed scripts disagree with the engine: `seed_events.py` writes `badge` where the engine reads `card_id` (so `distinct_cards` is 1 and `unknown_card_share` is 0 for those events); writes `PACKET_LOSS` events nothing reads; `MOTION_DETECTED.value` is a score in `generateData.py` but a count in `seed_events.py`; `ENERGY_READING` is stored on an HVAC asset. | `seed_events.py`, `seed_history.py` | Misleading facts and test data | DM-02 |
| F11 | Documentation contradicts the code or itself (details in Appendix C). | README files, `ALERTING.md` | Wrong instructions, ambiguous "latest results" | DM-10, DM-11 |
| F12 | The committed outputs show `HVAC-AHU-01` with an open "Smoke alarm active" incident. No script in the repository writes `SMOKE_DETECTED` to an HVAC asset, so the origin is unverified. | `README-Day5 (3).md`, `TEST_RESULTS.md` | Evidence that the schema accepted a nonsensical row | DM-02 |
| F13 | `/ingest` has no duplicate protection (plain `INSERT`, no unique constraint) and no value/unit plausibility check. | `App.py`, `schema.sql` | Replays double the data; bad values enter silently | DM-02 |
| F14 | In migration 001, the foreign key from `recommendations` to `playbook_entries(entry_id, version, approval_status)` is `ON UPDATE RESTRICT`: a referenced version's status can never change. | migration 001 | A used playbook version cannot be retired | DM-09, D5 |

**Minor:** `ai_explainer.fallback()` builds a list of retry hints and never uses it (dead code). The only automated checks are `test_chat.py` (end-to-end; needs the live API, the database and, for LLM mode, Ollama) and a 23-line `test_citations.py`; there are no unit tests for the KPI or alert engines.

---

## 5. Mapping Ahmed's current schema to the unified schema

For the data migration and for deciding what the loader must do.

| Ahmed (`schema.sql`, `schema_day4.sql`) | Unified schema (migration 001) | Notes |
|---|---|---|
| `locations(building, floor, zone)` | `buildings` + `locations` | `building` text becomes `buildings.code`. **`zone_type` and `is_sensitive` are new and must be assigned** (for example "Server Room" becomes `SERVER_ROOM`, sensitive); rules switch from zone names to these. |
| Fixed offset UTC+2, 08-18 | `buildings.timezone`, `work_start_local`, `work_end_local` | `timezone` must be an IANA name (a trigger validates it). The legacy value is a fixed offset, not a zone. If the real zone observes daylight saving, working hours shift against UTC for part of the year; use a fixed-offset name such as `Etc/GMT-2` only if a fixed offset is truly intended. |
| `systems(type)` with five allowed values | `systems(code, name, legacy_code)` | Keep the old value in `legacy_code`. |
| *(none)* | `asset_types`, `metric_types`, `event_types`, compatibility tables | New catalogues; they replace `FAULT_TYPES`, `OPENERS`, `INCIDENT_TYPES` (DM-03). |
| `assets.status` (`ACTIVE`, `INACTIVE`, `MAINTENANCE`, `FAULT`) | `assets.lifecycle_status` (`ACTIVE`, `INACTIVE`, `MAINTENANCE`, `DECOMMISSIONED`) | `FAULT` no longer exists as a lifecycle value; current condition lives in `asset_state`. Map `FAULT` to `ACTIVE`. |
| `events` (one table) | `telemetry` + `events` | Split per Appendix B. `ts` becomes `source_ts`. For legacy rows set `received_at` to the load time and `source = 'legacy-migration'`. Compute `idempotency_key` with the same sha256 rule as live ingestion. |
| `events.severity` | `events.reported_severity` | Device-reported, not the alert severity. Telemetry has no severity. |
| `events.details` (JSONB) | `events.details`, or separate metrics | Numeric and boolean fields the engine needs (`fan_on`, `packet_loss_pct`, `battery_pct`, `mains_ok`, `humidity`, `setpoint`) become telemetry metrics. |
| `alerts.evidence` (JSONB: thresholds, facts, events) | `alerts.thresholds`, `alerts.facts`, `evidence` rows | `ref_label` kept (`E1`...). Hard foreign keys to the source row; `snapshot` keeps the stored copy. |
| `alerts.status` (`OPEN`, `ACKNOWLEDGED`, `RESOLVED`) | adds `AUTO_RESOLVED`; adds `rule_version`, `dedup_key`, `status_changed_at`, `resolution_note` | `dedup_key = rule_id\|asset_id\|first_ts` for legacy alerts. |
| `alerts.explanation`, `explanation_source` (`OLLAMA`, `FALLBACK`) | `explanations` + `recommendations` | `OLLAMA` becomes `LLM`, `FALLBACK` becomes `FALLBACK_FACTS`. A passed grounding check becomes `EVIDENCE_SUPPORTED` with `checks_run = {numbers, times, citations}`; never `VALIDATED`. |
| `alert_config.RULES`, `T` | `alert_rules` (+ `config` JSONB) | 16 rows; `resolution_mode` per Appendix A. |
| `alert_config.PLAYBOOKS` | `playbook_entries` | 71 entries, all `DRAFT` (DM-09). |
| `scenario_runs` | `eval.scenario_runs` | The migration drops `event_ids`; the old reset logic that deleted injected events by id no longer applies (F7). |
| *(none)* | `asset_state`, `incidents`, `incident_alerts` | Computed or created by the engine, not migrated. |

---

## 6. Suggested order of work

Dependencies decide the order; nothing here is a schedule.

| Phase | Work | Decisions | Exit check |
|---|---|---|---|
| 0 | Application skeleton and entry point; apply migration 001; seed catalogues (systems, asset/metric/event types, compatibility tables) and the building, locations and assets from the generator's constants; define `ModelProvider` | DM-11 (skeleton), DM-01, DM-10 | Migration applies; catalogues loaded; one documented way to start the app |
| 1 | Ingestion on the new tables; rebuild generator and scenario injector for append-only data | DM-02 | The five ingestion acceptance tests pass; the 48 h dataset loads |
| 2 | `asset_state` and the KPI registry; dashboard shell reading them | DM-03, DM-04 (part) | Each KPI has one implementation; "No data" and `STALE` shown |
| 3 | Alert engine port: rules and thresholds to the database, upsert, lifecycle, precision harness; evidence rows | DM-05, DM-08 (part 1) | 6 of 6 scenarios and 14 of 14 rules still detected; re-run changes nothing; quiet window gives zero alerts |
| 4 | Playbook as data, recommendations, honest validation status | DM-09, DM-08 (part 2) | Golden test passes; `DRAFT` hidden |
| 5 | Incident grouping | DM-06 | One incident per scenario |
| 6 | Assistant merge, memory, semantic checks | DM-07, DM-08 (part 3) | 41 of 41 chat checks; follow-ups work; the "without issues" case is rejected |
| 7 | UI completion, old-path aliases removed | DM-04, DM-11 | Works offline; aliases removed |

---

## 7. Open decisions for the team

Each has a recommendation, but these are choices for the team, not for this document.

| ID | Question | Options | Recommendation | Blocks |
|---|---|---|---|---|
| **D1** | May cloud model providers be used? | (a) Never. (b) Opt-in per deployment, after written data-handling sign-off and minimisation of card ids and security events. (c) On by default. | (a) for the default build; (b) only after sign-off. | Nothing in the first merge |
| **D2** | A batch is atomic but contains an exact duplicate. What happens? | (a) Whole batch rejected with 409 listing the duplicate indexes. (b) Duplicates skipped, rest stored, 201 with counts. (c) 409 by default, `?on_duplicate=skip` opt-in. | (c): safe default, and clients that retry can opt in to skipping. | DM-02 |
| **D3** | Who approves playbook entries, and may `DRAFT` entries be shown? | Name an approving engineer per family (HVAC, network, security, fire); show `DRAFT` never / only in review mode / always labelled. | Approvers named per family, fire first; `DRAFT` only in review mode. | DM-09 |
| **D4** | What is "now" for KPIs, status and the assistant? | (a) Latest event (today). (b) Wall clock only. (c) Explicit `as_of`, default wall clock, replay time for demos and evaluation. | (c). | DM-03, DM-07 |
| **D5** | How is a used playbook version retired (F14)? | (a) Drop `approval_status` from the foreign key. (b) Add a `retired_at` column outside the key and remove `RETIRED` from `approval_status`. (c) Accept: retire by publishing a new version and leaving the old one `APPROVED`. | (b): history stays honest and retirement stays possible. | DM-09 |
| **D6** | How is `HIGH_TEMPERATURE` stored (it carries a temperature value)? | (a) Event only (the temperature series then has gaps and rules must union two sources). (b) A telemetry sample **and** an `ALARM` event at the same timestamp. (c) Telemetry only, alarm derived by a rule. | (b): complete series, explicit fault. | DM-02, DM-03 |
| **D7** | Are chat questions, answers and facts persisted? | (a) No. (b) Yes, in a new table with a retention period and an access rule. | (b). Ahmed's reflection asks for request logging "with the facts used"; migration 001's `explanations` cannot hold chat turns (it requires an alert or incident), so this needs its own table. | DM-07, DM-08 |
| **D8** | What authentication and roles exist? | None today. Roles at least for: operator (read, acknowledge), engineer (resolve, approve playbook), admin. | Per-user authentication before any non-local deployment. Note that migration 001 records `approved_by` but has **no actor columns** for acknowledging or resolving alerts and incidents; add them with this decision. | DM-05, DM-06, DM-09 |

---

## 8. What to check in Amr's repository

Every decision tagged [R] stays provisional until these are confirmed. A decision is **confirmed** if the check matches the reported claim and **revised** if not.

| Decision | Confirm in Amr's code | If it differs |
|---|---|---|
| DM-01 | The database is SQLite; any SQLite-specific SQL; size and value of data worth migrating | Reopen only if he already runs Postgres |
| DM-02 | List of reference tables; telemetry/events split rule; the actual validation rules; where the 409 is raised; batch size and atomicity | Adopt his rules that migration 001 lacks; drop ones it already covers |
| DM-03 | The 8 KPIs and their formulas (same as Ahmed's 8?); test inventory and what they cover; how building and custom-range filters work; contents of `dashboard_core.py` | KPI registry holds the union, with one definition each |
| DM-04 | UI stack; the SVG components available; how "heartbeat compliance" is defined; how "No data" is shown; whether it works offline | If it cannot show `STALE`/`NO_DATA` or is not offline-capable, reconsider using it as the shell |
| DM-05 | Whether alerting code exists; rules, detection methods, evidence produced, tests | Promote DEFER to ADAPT/MERGE only for rules that add coverage and can supply evidence rows |
| DM-06 | Any incident or grouping code | Reuse if found |
| DM-07 | Tool registry (names, parameter schemas, outputs), the routing prompt, the six tools, how memory is stored and what it contains | Replace the placeholder tool names in DM-07; if tools return text rather than facts, wrap them |
| DM-08 | Any answer validation in his assistant | If none, all his answers must be routed through Ahmed's validators |
| DM-09 | Logic and weights of `get_corrective_priorities` | Reconcile with Ahmed's score; one score only |
| DM-10 | Provider list, interface, configuration; what data each provider receives | Feed the D1 discussion with concrete data flows |
| DM-11 | Package names, `/api/v1` routes, Pydantic schemas, any authentication | Align the proposed layout with the real names |

---

## Appendix A. Rule catalogue and proposed resolution mode

The 16 rule IDs in `alert_config.RULES` (verified), with the proposed `alert_rules.resolution_mode`. **The last column is a proposal; each rule's owning engineer must confirm it.** The schema requires `resolution_mode = RECOVERY_EVENT` exactly when `recovery_event_type_id` is set.

| Rule | Method | Severity logic | Playbook | Proposed resolution |
|---|---|---|---|---|
| `HVAC_HIGH_TEMP` | THRESHOLD | WARNING above 29 °C (25 server room); CRITICAL above 32 °C (30) | `HVAC_TEMP` | `CONDITION_CLEARED` |
| `HVAC_TEMP_ANOMALY` | ANOMALY | WARNING | `HVAC_TEMP` | `CONDITION_CLEARED` |
| `HVAC_FILTER_FAULT` | THRESHOLD | WARNING | `HVAC_FILTER` | `MANUAL` (no recovery event exists) |
| `NET_LINK_DOWN` | THRESHOLD | CRITICAL | `NET_LINK_DOWN` | `RECOVERY_EVENT` (`LINK_UP`) |
| `NET_LINK_FLAPPING` | THRESHOLD | CRITICAL (3 or more downs in 30 min) | `NET_FLAPPING` | `CONDITION_CLEARED` |
| `NET_BANDWIDTH_HIGH` | THRESHOLD | WARNING above 900 Mbps for 3 readings; CRITICAL if mean 980 or more | `NET_BANDWIDTH` | `CONDITION_CLEARED` |
| `NET_PACKET_LOSS` | THRESHOLD | WARNING above 1% for 3 readings; CRITICAL if mean 3% or more | `NET_LOSS` | `CONDITION_CLEARED` |
| `NET_BANDWIDTH_ANOMALY` | ANOMALY | WARNING | `NET_ANOMALY` | `CONDITION_CLEARED` |
| `ACC_DOOR_FORCED` | THRESHOLD | CRITICAL | `DOOR_FORCED` | `MANUAL` |
| `ACC_DOOR_HELD_OPEN` | THRESHOLD | WARNING (over 120 s) | `DOOR_HELD` | `MANUAL` |
| `ACC_DENIAL_SPIKE` | THRESHOLD | WARNING at 5 in 15 min; CRITICAL at 10 | `DENIAL` | `MANUAL` (needs human review) |
| `CCTV_OFFLINE_LONG` | THRESHOLD | WARNING (15 min or more) | `CAM_OFFLINE` | `RECOVERY_EVENT` (`CAMERA_ONLINE`) |
| `CCTV_AFTER_HOURS_MOTION` | THRESHOLD | WARNING (5 in 30 min, server room) | `MOTION` | `MANUAL` |
| `SEC_BLIND_SPOT_ACCESS` | COMPOUND | CRITICAL | `BLIND_SPOT` | `MANUAL` |
| `FIRE_SMOKE` | THRESHOLD | CRITICAL | `SMOKE` | `MANUAL`, with the `ALARM_RESET` attached as `RECOVERY` evidence. A reset does not prove there was no fire, so automatic closure is not proposed for a safety-critical alarm. Team to confirm. |
| `FIRE_BATTERY_LOW` | THRESHOLD | WARNING below 20%; CRITICAL below 10% | `BATTERY` | `CONDITION_CLEARED` (battery at or above 20% and mains normal) |

---

## Appendix B. Event types in use and proposed classification

The 20 event types found across the repository's generators, seed scripts and engines (verified), with a proposed home in the split schema. All proposals are subject to D6 and the catalogue review.

| Event type | Used by | Proposed target | Notes |
|---|---|---|---|
| `TEMPERATURE_READING` | HVAC generator, engine, KPI | telemetry `temperature_c` | `setpoint`, `fan_on`, `humidity` in `details` become metrics (`setpoint_c`, `fan_on` boolean, `humidity_pct`) |
| `HIGH_TEMPERATURE` | HVAC generator, engine, KPI | `ALARM`/`FAULT` event **and** telemetry sample (D6) | Counted as a fault in `FAULT_TYPES` |
| `FILTER_FAULT` | HVAC | `FAULT` event | |
| `BANDWIDTH_USAGE` | network generator, engine, KPI | telemetry `bandwidth_mbps` | `details.packet_loss_pct` becomes telemetry `packet_loss_pct` at the same timestamp |
| `UPTIME_REPORT` | network generator (hourly) | telemetry `uptime_s` | Not used by any rule or KPI today |
| `LINK_DOWN` | network | `FAULT` event (opener) | |
| `LINK_UP` | network | `RECOVERY` event, resolves `LINK_DOWN` | `downtime_sec` in `details` |
| `PACKET_LOSS` | `seed_events.py` only | none | No consumer; map to the `packet_loss_pct` metric or drop |
| `ACCESS_GRANTED` | access | `OCCURRENCE` event | Not a fault |
| `ACCESS_DENIED` | access | `OCCURRENCE` event | Not a fault (KPI doc says so explicitly); `card_id`, `reason` in `details` |
| `DOOR_HELD_OPEN` | access | `FAULT` event | `value` = seconds |
| `DOOR_FORCED` | access | `ALARM` event | |
| `MOTION_DETECTED` | CCTV | `OCCURRENCE` event | `value` semantics differ between scripts (F10); `after_hours` must be computed, not stored (F5) |
| `CAMERA_OFFLINE` | CCTV | `FAULT` event (opener) | |
| `CAMERA_ONLINE` | CCTV | `RECOVERY` event, resolves `CAMERA_OFFLINE` | |
| `PANEL_HEARTBEAT` | fire panel (hourly) | `LIVENESS` event, plus telemetry `battery_pct`, `mains_ok` (boolean) | Drives freshness |
| `BATTERY_LOW` | fire panel | `FAULT` event | `value` = battery % |
| `SMOKE_DETECTED` | fire panel | `ALARM` event (opener) | Allowed on fire-panel assets only |
| `ALARM_RESET` | fire panel | `RECOVERY` event, resolves `SMOKE_DETECTED` | |
| `ENERGY_READING` | `seed_history.py` only | telemetry `energy_kwh` | Needs an energy-meter asset type; today it is stored on an HVAC asset |

Consequence: `FAULT_TYPES` (KPI), `OPENERS` and `INCIDENT_TYPES` can all be **derived** from `event_types.is_fault` and `event_types.resolves_event_type_id` instead of being maintained by hand.

---

## Appendix C. Sources and documentation conflicts

**Read for this version (Ahmed's repository, snapshot 2026-11-05)**
- Fully read: `README.md`, `README-Day5 (3).md`, `ALERTING.md`, `KPI_DEFINITIONS.md`, `REFLECTION.md`, `schema.sql`, `schema_day4.sql`, `requirements.txt`, `App.py`, `dashboard_api.py`, `alert_config.py`, `alert_engine.py`, `alerts_api.py`, `ai_explainer.py`, `chat_api.py`, `seed_events.py`.
- Read in part: `chat_engine.py` (intents, facts, collectors `c_repair`, grounding, composition), `generateData.py`, `inject_scenarios.py`, `evaluate_alerts.py`, `seed_history.py`, `TEST_RESULTS.md`, `static/dashboard.html` (structure and script includes only).
- **Not read:** `chat-widget.js`, `chat.html`, `style.css`, `test_chat.py`, `test_citations.py` body, `measure_explain.py`, `debug_explain.py`, `test_results.json`, `review_explain.txt`, and the two PNG diagrams (`ER-Diagram.png`, `Data-Flow-Diagram.png`).
- Nothing was executed: no test run, no database, no model. Figures quoted as "measured" come from the repository's own documents.

**Not available:** Amr's repository (every [R] claim).

**Conflicts between documents and code (feeds F11)**

| # | Conflict |
|---|---|
| 1 | Default Ollama model: code (`ai_explainer.py`) uses `qwen2.5:7b`; `ALERTING.md` says `llama3.2:3b`. |
| 2 | Start command: `README.md` uses `uvicorn App:app`; `README-Day5 (3).md` uses `uvicorn App:asgi_app`. Socket.IO only works through `asgi_app`. |
| 3 | `README.md` says Chart.js is bundled so the dashboard works offline; `dashboard.html` also loads Socket.IO from a CDN. |
| 4 | Chat LLM results: `README-Day5` reports 16 of 20 model answers accepted and 4 fallbacks (`--energy missing`); `TEST_RESULTS.md` reports 15 and 5 (`energy expected: present`); `REFLECTION.md` reports 13 of 18 in-scope answers from the model (8 first try, 5 after a retry) and 15 rejected attempts. They are different runs; keep one results file per run, with a run id and date. |
| 5 | Layout: `README-Day5` and `REFLECTION.md` refer to `tests/test_chat.py` and a folder `ANZMA-DATABASE/`; in the archive the files are at the repository root. |
| 6 | `ALERTING.md` tabulates 14 rules and elsewhere refers to "16 rules"; the engine has 16 rule IDs (13 threshold, 1 compound, 2 anomaly). |
| 7 | `ALERTING.md` shows `app.include_router(create_alerts_router(db))`; `App.py` passes `push_alert` as well. |

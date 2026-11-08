# INTEGRATION_PLAN.md - Anzma Unified Platform

**Owner:** Amr (reviewed by: Ahmed - `[TBD: confirm]`)
Tags: **[Verified]** seen in code or repo, **[Proposed]** needs both engineers' agreement, **[TBD]** unknown.

## 1. Scope and goal

**Day 2 goal (minimum):** one end-to-end scenario, **S1 (server-room cooling failure)**, running on the unified platform from the simulated fault to the dashboard and the assistant. **[Proposed]**

```
inject S1 -> PostgreSQL -> alert engine -> incident -> dashboard shows it
                                                   -> assistant explains it (validated, with evidence)
```

S1 is chosen because it exists on both sides (Ahmed: server-room cooling failure with rules `HVAC_TEMP_ANOMALY` and `HVAC_HIGH_TEMP`; Amr: AHU-02 cooling failure) **[Verified]** and exercises ingestion, alerting, KPIs, recommendations and the assistant.

Out of scope for Day 2: authentication, cloud model providers, WebSockets, S2-S6 end to end, production deployment.

## 2. Shared repository

**Repository:** `https://github.com/Amr-69/Anzma-Platform-Submit.git` **[Verified: it exists and is empty; no commits and no branches yet]**.

### 2.1 Initial structure **[Proposed]**

```
Anzma-Platform-Submit/
  README.md                  # startup instructions (section 2.3)
  ARCHITECTURE.md  DECISION_MATRIX.md  DATA_MODEL.md  API_CONTRACT.md  INTEGRATION_PLAN.md
  docker-compose.yml         # PostgreSQL for both laptops
  .env.example
  requirements.txt
  app/
    main.py  config.py  db.py
    api/v1/        ingestion.py  retrieval.py  dashboard.py  alerts.py  incidents.py  assistant.py
    schemas/       (Pydantic models)
    services/
      kpi/  alerting/  incidents/  recommendations/  evidence/
    assistant/
      orchestrator.py  memory.py  validator.py  fallback.py
      tools/       (six tools wrapping the collectors)
      providers/   base.py  ollama.py
    static/        (dashboard shell + chat panel)
  db/
    schema.sql  views.sql  seed_reference.sql
  scenarios/       inject_scenarios.py  generate_data.py  mapping_amr_to_ahmed.md
  tests/           (pytest, one folder per layer)
  legacy/          (reference copies of Ahmed's root files and Amr's Task 2/4/5, read-only)
```

Why `legacy/`: both source repos contain duplicated or flat layouts (Amr's `Task 2/4/5` are copies of the same project; Ahmed's modules sit in the repo root) **[Verified]**. Moving code from `legacy/` into `app/` keeps history readable and avoids a rewrite.

### 2.2 Branches and rules **[Proposed]**

| Item | Rule |
|---|---|
| `main` | Protected. Only merged through pull requests. Always starts and passes tests. |
| `develop` | Integration branch for Day 2. `[TBD: or merge straight to main]` |
| Feature branches | `feat/<module>-<owner>`, short-lived, for example `feat/ingestion-amr`, `feat/alert-engine-ahmed`. |
| Pull requests | Reviewed by the other engineer. Tests must pass first. |
| Commits | Small, one concern each. |
| Collaborator | Ahmed is added as collaborator by the owner of the repo. `[TBD: done?]` |

### 2.3 Startup instructions (goes into `README.md`) **[Proposed]**

```bash
git clone https://github.com/Amr-69/Anzma-Platform-Submit.git
cd Anzma-Platform-Submit
cp .env.example .env            # edit values if needed
docker compose up -d db         # PostgreSQL
pip install -r requirements.txt
psql "$DATABASE_URL" -f db/schema.sql -f db/views.sql
python scenarios/generate_data.py --reset
python scenarios/inject_scenarios.py
uvicorn app.main:app --reload
# optional: ollama pull qwen2.5:3b
```

`[TBD]` Whether both laptops can run Docker. Amr's laptop has about 8 GB RAM and Windows **[Verified]** in `anzma-ai-insight`; Docker Desktop plus Ollama may be tight. Fallback: native PostgreSQL install.

## 3. Ownership **[Proposed]**

| Area | Amr | Ahmed |
|---|---|---|
| Repo layout, CI, `README`, `docker-compose` | Owner | Reviewer |
| Ingestion API and schemas | Owner | Reviewer |
| PostgreSQL schema merge (`schema.sql`, `events_unified` view) | **Shared** | **Shared** |
| Data generator and scenario injector on the new schema | Reviewer | Owner |
| Alert engine port, status lifecycle | Reviewer | Owner |
| Incident grouping and ranking | Endpoints, ranking reuse | Grouping rule (compound seed) |
| KPI engine merge | Structure, tests, filters | Netting and active-fire logic |
| Recommendations and playbooks (versioned data) | Tool wrapper | Owner |
| Assistant orchestrator, tool registry, memory, `ModelProvider` | Owner | Reviewer |
| Collectors as tools, validator, fallback | Reviewer | Owner |
| Dashboard shell and chat panel | Owner | Reviewer |
| Integration tests, S1 end-to-end | Owner | Co-owner |
| Documents (`DECISION_MATRIX.md`, `DATA_MODEL.md`) | Reviewer | Owner |

Amr owns `ARCHITECTURE.md`, `API_CONTRACT.md` and `INTEGRATION_PLAN.md`, as you described. `DECISION_MATRIX.md` and `DATA_MODEL.md` are assumed to be Ahmed's **[Proposed]**; the CEO text you pasted does not say who writes them. `[TBD: confirm with Ahmed]`

## 4. Day 2 implementation sequence

No hour estimates are given because working hours and checkpoint times are unknown `[TBD]`. Steps are ordered by dependency; a step starts when its predecessor's pull request is merged.

| # | Step | Owner | Depends on | Done when |
|---|---|---|---|---|
| 0 | Initialize repo: structure, `docker-compose`, `.env.example`, empty test run, copy source into `legacy/` | Amr | - | Both can clone and start an empty app |
| 1 | Merged schema on PostgreSQL: reference tables, telemetry/events split, event-type compatibility table, `alerts`, `scenario_runs`, `events_unified` view | Shared | 0 | `schema.sql` loads cleanly on both laptops |
| 2 | Data generator and S1 injector writing to the new schema | Ahmed | 1 | `events_unified` returns S1 rows |
| 3 | Ingestion endpoints with merged rules (atomic 1-5000, future-time, 409, 422) | Amr | 1 | Contract tests pass |
| 4 | Alert engine reading `events_unified`; `/alerts` endpoints; status lifecycle | Ahmed (engine), Amr (routers) | 2 | S1 raises `HVAC_TEMP_ANOMALY` and `HVAC_HIGH_TEMP` |
| 5 | Incident grouping (same location, 15-minute window) and `/incidents` | Ahmed + Amr | 4 | S1 alerts appear as one incident with a stable ID |
| 6 | KPI merge into Amr's structure; one definition per KPI | Ahmed (logic), Amr (structure) | 1 | Existing KPI tests pass on PostgreSQL |
| 7 | `ModelProvider`, `OllamaProvider`, tool registry, server-side memory | Amr | 1 | Tool selection works with the model and with the keyword router |
| 8 | Collectors wrapped as six tools; validator and fallback wired into the orchestrator | Ahmed + Amr | 5, 7 | Chat returns `validation.status` truthfully |
| 9 | Dashboard shell: overview, S1 incident, chat panel | Amr | 5, 6, 8 | S1 visible on the dashboard |
| 10 | End-to-end test of S1 and demo rehearsal | Both | 9 | Section 6 checklist passes |

Parallel work: steps 2, 3, 6 and 7 can run in parallel after step 1.

### Cut line (if time runs out) **[Proposed]**

| Priority | Items | If late |
|---|---|---|
| Must | Steps 0-4, 7, 8 (S1 alert and a validated assistant answer) | Never cut. |
| Should | Steps 5, 9 | Show the alert list and the chat answer without incident grouping. |
| Could | Step 6 merge, Chart.js trends, alert auto-close, incremental processing, precision check | Keep the existing KPI code path, documented as a known gap. |

## 5. Dependencies and risks

| # | Risk | Likelihood / impact | Mitigation | Owner |
|---|---|---|---|---|
| R1 | Database migration: Ahmed's engine reads one `events` table; the merged schema splits telemetry and events | High / High | `events_unified` view so the engine runs unchanged first; refactor later | Shared |
| R2 | Time handling: Amr stores text timestamps, Ahmed `TIMESTAMPTZ`, and Ahmed's code uses a fixed local offset (`LOCAL_UTC_OFFSET = 2`) **[Verified]** | Medium / High | Everything UTC in storage and API; local time only for display and after-hours rules; test on S1 | Shared |
| R3 | Duplicate or conflicting KPI definitions (both have 8 KPIs with different logic) | High / Medium | One definition per KPI in `KPI_DEFINITIONS.md`; tests from Amr run against the merged code | Ahmed + Amr |
| R4 | Validator coverage: it checks facts, not meaning (his own `REFLECTION.md`) **[Verified]** | High / Medium | State this in `limitations`; extend judgment-phrase guard; never label `validated` without checks | Ahmed |
| R5 | Amr's chat is unvalidated today **[Verified]** | Certain / High | Step 8 routes every answer through the validator | Amr |
| R6 | LLM latency and weak hardware (8 GB RAM, 4 GB VRAM, 3B models) **[Verified]** | High / Medium | Deterministic fallback; `qwen2.5:3b` default; request timeout; demo with fallback visible | Amr |
| R7 | Scenario numbering differs (S2-S5 mean different things on each side) **[Verified]** | Certain / Low | Ahmed's S1-S6 canonical; mapping file | Ahmed |
| R8 | Scenarios on PostgreSQL: Amr's per-scenario `.db` switching disappears | Medium / Medium | Single dataset with `scenario_runs` ground truth (Ahmed's approach) | Ahmed |
| R9 | Docker/PostgreSQL setup problems on a laptop | Medium / High | Native PostgreSQL fallback; setup check before step 1 | Amr |
| R10 | README claims not matching code (cloud models) | Certain / Medium | Remove the claim in the new repo; list cloud providers as not implemented | Amr |
| R11 | Scope growth (incident correlation, lifecycle, precision) | High / High | Cut line in section 4 | Both |

## 6. Day 2 acceptance checklist **[Proposed]**

- [ ] Both engineers clone the repo and start the app with the steps in section 2.3.
- [ ] S1 is injected; the alert engine raises the expected rules on PostgreSQL.
- [ ] The dashboard shows the S1 incident and the KPI changes.
- [ ] The assistant answers "What is wrong with the building?" with evidence, and `validation.status` reflects checks that really ran.
- [ ] With Ollama stopped, the assistant still answers (fallback) and says so (`ai_source = "fallback"`).
- [ ] A batch with a duplicate returns 409, a future timestamp returns 422, and nothing is stored in either case.
- [ ] Every item on the cut line is either done or listed as a known gap.

## 7. Day 1 closing: presentation and alignment

| Item | Status |
|---|---|
| Day 1 due | Today (Sunday 8 November 2026) per Amr. `[TBD: exact time]` |
| 10-minute joint presentation | `[TBD: time]`. Slides are dropped for now per Amr's decision; the discussion uses `ARCHITECTURE.md` sections 3 and 5.3. |
| Day 2 checkpoints | `[TBD]` |
| Ahmed's review of this file | `[TBD]` |

Suggested 10-minute structure: 1 minute problem and starting point, 3 minutes architecture and data flow, 3 minutes why each reuse/adapt decision, 2 minutes Day 2 plan and risks, 1 minute known gaps.

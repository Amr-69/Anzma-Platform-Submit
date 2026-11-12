# Run and demo the platform (Docker)

## 1. Start (empty volume = clean seed)
```bash
cp .env.example .env            # set PGPASSWORD (an .env with a password already exists if you got this folder as a zip)
docker compose up -d --build
docker compose ps               # db, ollama, api healthy; ollama-init exits after pulling the model
```
* The first start loads `db/init/anzma_schema_and_seed.sql`, then `day2_ahmed/migrations/003_incidents.sql`
  automatically (no manual SQL). Both only run on an **empty** volume. Do not remove volumes for a routine restart.
* `docker compose down -v` deletes local PostgreSQL data and downloaded Ollama models; use it only when that local data is disposable.
* Open **http://localhost:8000** (the dashboard). `/docs` is the API explorer, `/health` the health check.
* The API runs detection at start-up and every `DETECT_INTERVAL` seconds (default 60). Detection is idempotent.
* Without the Ollama model (first minutes, or no internet) everything still works: explanations use the
  deterministic fallback and say so. Check the model: `docker compose logs ollama-init`.

## 2. What the page shows (all from PostgreSQL, nothing hard-coded)
| Page area | Endpoint | Source tables |
|---|---|---|
| System status, Key indicators | `GET /dashboard/overview?hours=&system_type=` | assets, telemetry_records, event_records, incidents |
| Trends | `GET /dashboard/trends` | telemetry_records, event_records |
| Equipment health | `GET /dashboard/equipment-health` | assets + facts of the window |
| Alert center (+ live push) | `GET /alerts`, Socket.IO `new_alert` | alerts |
| Incidents, evidence, explanation, recommendations | `/incidents`, `/incidents/{id}/evidence`, `POST /alerts/{id}/explain`, `POST /incidents/{id}/status` | incidents, alerts, fact tables |
| Assistant widget | `POST /chat`, `GET /chat/questions` | same pipeline (HVAC/CRAC questions, open incidents) |
"Now" is the latest stored record (`as_of`), not the wall clock, so the data window always contains data.

## 3. Demo script (S1: server-room cooling failure)
1. **Dashboard** -> show HVAC status, CRAC Unit 1 status and the fault/recovery history. A later recovery clears the asset's current Critical status; the critical event and its alert/incident history remain separate.
2. **Trends** -> the HVAC temperature chart shows the rise (the CRAC runs about 9 C above normal for 90 minutes).
3. **Incidents** -> click "HVAC temperature above threshold - CRAC Unit 1": the tag *Evidence verified in database 14 / 14*
   means every evidence row was re-read from `telemetry_records` and matched.
4. With Ollama and the configured Qwen model ready, enable *Use local AI model* and click **Explain**. Record the source/model reported by the application. A timeout or fallback does not count as a live-model pass.
5. Disable *Use local AI model* and click **Explain** again (or stop Ollama and retry). Confirm the deterministic fallback, evidence verification, grounding result, limitations and playbook actions.
6. **Acknowledge** / **Resolve** the incident -> incident status changes independently from physical fault recovery.
7. Ask the widget: "Why is CRAC 1 temperature high?" or "What is wrong with the building?".

## 4. Tests (run against PostgreSQL; the user must be allowed to CREATE DATABASE)
```bash
docker compose exec -T api python test_frontend_api.py                   # recreates disposable DB anzma_api_test
docker compose exec -T api python test_chat_consistency.py               # read-only assistant/dashboard comparison
docker compose exec -T api python day2_ahmed/test_day2_integration.py    # 43 tests: detection, incidents, evidence
docker compose exec -T api python day2_ahmed/test_day2_ahmed.py          # 25 offline tests
python day2_ahmed/e2e_s1.py --build --llm                                # rebuilds only disposable DB anzma_e2e
```
(`docker compose exec` uses the api container's PG* variables; PGUSER must be a superuser, which `postgres` is.)

Latest reported results on 2026-11-12: frontend API **17/17**, chat/dashboard consistency **31/31**. The frontend test logged Ollama timeouts; it verified fallback, not successful live Qwen inference. The `e2e_s1.py` script prints pipeline evidence but does not show the dashboard; capture the browser walkthrough separately.

## 5. Differences between the page and the data, and what was done
| Page expected | Database has | Handling |
|---|---|---|
| `NETWORK`, `ACCESS` | `NETWORKING`, `ACCESS_CONTROL` | translated in `app/frontend_api.py` (both spellings accepted as input) |
| bandwidth in Mbps | `bandwidth_util` in % | chart retitled "utilisation (%)"; peak = max per bucket |
| access granted + denied | only `access_denied` is stored | API returns `access_granted: null`; the chart draws only denied (never a fake zero) |
| `/dashboard/summary` in the old shape | `/dashboard/summary` already exists with the KPI-engine shape | left untouched; the page uses the new `/dashboard/overview` |
| events table with value/unit | telemetry and events are separate tables | trends read both; heartbeats are not counted as events |

## 6. Known limitations
* Incidents stay *open* until someone acknowledges or resolves them, even if the physical condition ended
  (e.g. the smoke alarm that was reset); the alert's evidence says whether it recovered.
* `/chat` answers HVAC/CRAC questions and open incidents only; the six-tool router is a later step.
  `chat_engine.py`, `chat_api.py` and `dashboard_api.py` in the repo root belong to the old schema and are not used.
* Socket.IO client is loaded from a CDN in `dashboard.html`; offline, the Alert center still loads from `/alerts` but shows "offline" for live push.
* Validation is grounding-only (numbers, times, citations, playbook membership); it is not a semantic check.
* The seed is relative to the time it was loaded; the CRAC fault appears about 20 hours before the latest record.
* Amr reports engineering sign-off for the HVAC playbook; confirm the other system playbooks' approval scope before presenting those actions as approved.

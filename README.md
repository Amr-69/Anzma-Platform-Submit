# Anzma AI Insight – Unified Platform

Joint project by **Ahmed** and **Amr**. One platform that takes a simulated fault all the way to a validated, evidence-backed recommendation on a dashboard.

```text
Simulated Fault → Alert Detection → Incident / Evidence Retrieval
   → AI Explanation → Validation → Recommendation → Dashboard
```

If the AI model (Ollama) is unavailable, the system falls back to a deterministic explanation that goes through the same validation and states the limitation.

## Current status

The platform is operational with PostgreSQL and the FastAPI service. AI explanations currently use a validated deterministic fallback; Ollama integration is implemented and ready for model download.

| Area | Status |
|---|---|
| PostgreSQL schema + seed data | Done |
| Alert detection, incidents, evidence, validation, recommendations, fallback | Done, tested on PostgreSQL 16 (28/28 integration, 21/21 offline) |
| Dashboard KPI logic (`app/dashboard_core_pg.py`) | Done |
| API entry point (`App.py`) connecting everything | Done |
| Dashboard showing incidents, evidence and recommendations | Done |
| Ollama integration | Implemented; model download pending |
| Real Ollama model tested end to end | Previously completed; currently pending model download in this environment |
| Validated fallback response | Active and working |

## Repository structure

```text
Anzma-Platform-Submit/
├── README.md
├── docker-compose.yml          # db (PostgreSQL 16), ollama, ollama-init, api
├── Dockerfile                  # API image (uvicorn)
├── requirements.txt
├── .env.example                # copy to .env (never commit .env)
├── data-model.sql, DATA_MODEL.md, ERD-new.png
├── DECISION_MATRIX.md
├── reference_data.py
├── app/
│   └── dashboard_core_pg.py    # dashboard KPIs on PostgreSQL
├── db/init/
│   └── anzma_schema_and_seed.sql   # schema + seed (runs on first start of an empty DB)
├── day2_ahmed/                 # alert / incident / evidence / validation pipeline
│   ├── migrations/003_incidents.sql
│   ├── db_adapter.py           # only place that reads/writes the new tables
│   ├── alert_config.py, alert_engine.py
│   ├── incident_engine.py, evidence.py
│   ├── integrity_rules.py, validation.py
│   ├── recommendations.py, ai_explainer.py
│   ├── pipeline.py             # detect(), explain_alert(), explain_incident()
│   ├── test_day2_integration.py, test_day2_ahmed.py
│   └── DAY2_AHMED_HANDOVER.md, TEST_RESULTS_DAY2.txt
└── docs/
    ├── ARCHITECTURE.md, API_CONTRACT.md, INTEGRATION_PLAN.md
    └── submission document and presentation
```

## Quick start (Docker)

Requirements: Docker Desktop (or Docker Engine + Compose v2).

```bash
git clone [https://github.com/Amr-69/Anzma-Platform-Submit.git](https://github.com/Amr-69/Anzma-Platform-Submit.git)
cd Anzma-Platform-Submit

cp .env.example .env        # then set PGPASSWORD (required)
docker compose up -d --build db
```

Apply the incidents migration (the `db/init` script only runs on an empty volume, so `003` is applied manually):

```bash
docker compose exec -T db psql -U postgres -d anzma < day2_ahmed/migrations/003_incidents.sql
```

Start the API and database:

```bash
docker compose up -d db api
docker compose ps
```

The API is published on the host port set in `ports:` of the `api` service in `docker-compose.yml`. Useful endpoints are `/health`, `/docs`, `/dashboard`, and `/ask`.

## AI model status

The application supports Ollama through the `OLLAMA_HOST`, `OLLAMA_MODEL`, and `OLLAMA_TIMEOUT` settings.

- Configured model: `qwen2.5:7b`
- Ollama service: defined in `docker-compose.yml`
- Model download service: `ollama-init`
- Current environment: the Ollama model has not been downloaded yet
- Current behaviour: the platform returns a validated deterministic fallback response

When Ollama is unavailable, the system does not fail. It returns an evidence-backed deterministic answer, validates it against the available facts and playbook, and marks it as `fallback`.

## Running the tests

The pipeline tests build a throw-away database from the migrations, so the DB user must be allowed to `CREATE DATABASE`.

```bash
docker compose run --rm -e PGHOST=db api python day2_ahmed/test_day2_integration.py
docker compose run --rm -e PGHOST=db api python day2_ahmed/test_day2_ahmed.py
```

Without Docker:

```bash
pip install -r requirements.txt psycopg2-binary
export PGHOST=localhost PGUSER=postgres PGPASSWORD=...
python day2_ahmed/test_day2_integration.py
```

Last results are in `day2_ahmed/TEST_RESULTS_DAY2.txt`.

## Running detection manually

```bash
python -c "import pipeline, psycopg2, psycopg2.extras as x; \
c = psycopg2.connect('<dsn>', cursor_factory=x.RealDictCursor); cur = c.cursor(); \
print(pipeline.detect(cur)); c.commit()"
```

Run it from `day2_ahmed/`. Re-running detection creates no duplicate alerts or incidents.

## Key design decisions

- **PostgreSQL** is the single data platform, with one data-access layer.
- **Alerts and incidents are separate:** an alert is a detection; an incident groups alerts of the same location within 15 minutes and has a lifecycle (open → acknowledged → resolved).
- **Verifiable evidence:** every evidence row points to a real stored record and is re-checked against the fact tables.
- **Validation:** an explanation is rejected if it invents a reading, a cause or an action outside the playbook.
- **Safe fallback:** if Ollama is unreachable, a deterministic, validated answer is returned with the limitation stated.

Details: `docs/ARCHITECTURE.md`, `DATA_MODEL.md`, `DECISION_MATRIX.md`, `docs/API_CONTRACT.md`.

## Team workflow

| Rule | Detail |
|---|---|
| `main` | Always starts and passes tests. Changes go in through pull requests. |
| Feature branches | `feat/<module>-<owner>`, short-lived (e.g. `feat/alert-engine-ahmed`). |
| Reviews | The other engineer reviews every pull request. |
| Commits | Small, one concern each. |
| Database changes | Always a new numbered migration (`004_...sql`), never an edit of an applied one. |
| Secrets | `.env` is git-ignored. Never commit it. |

Before starting work: `git pull origin main`.

## Known limitations and scope notes

1. **Ollama model download:** the Ollama integration is implemented, but the `qwen2.5:7b` model has not yet been downloaded in the current environment. Until it is available, the platform uses the validated deterministic fallback.
2. **HVAC fault events:** `hvac_fault` / `hvac_recovered` events are stored in the database. Mapping them to an alert rule (to escalate severity) is a planned refinement.
3. **Available data:** the simulated dataset does not include fan state, filter fault, battery, motion or access_granted signals, so related recommendations stay general.
4. **Bandwidth thresholds:** `bandwidth_util` is stored in %, while the bandwidth rule thresholds are defined in Mbps. Percentage-based thresholds are planned.
5. **Access denials:** denial events carry no stored reason, so these explanations are marked `partial` data sufficiency, which is the intended behaviour.
6. **Seed alerts:** the five sample alerts in the seed file are illustrative; the alert engine generates the alerts used by the workflow.
7. **Validation scope:** validation checks grounding (values, citations, playbook membership). Semantic validation is outside the Day 2 scope.
8. **Incident lifecycle:** a resolved incident is final; a later fault opens a new incident.
9. **Evidence size:** each alert lists up to 10 events (first, last and peak always included), and any omitted events are disclosed in `limitations`.
10. **Database drivers:** `day2_ahmed/` uses `psycopg2` and `app/` uses `psycopg` v3. Unifying them is a planned cleanup.

## Authors

Ahmed and Amr – Anzma Tech.
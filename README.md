# Anzma AI Insight

**Anzma AI Insight** is a demo smart-building operations platform. It uses simulated building telemetry and events to calculate operational KPIs, detect configured faults, group related alerts into incidents, verify supporting evidence, and produce evidence-grounded explanations and corrective recommendations.

The current implementation in `main` covers five building-system domains:

- HVAC
- Networking
- Access Control
- CCTV
- Fire Alarm

> **Project status:** This is an integration/demo build using seeded data. It is not a production-ready building-management system. The current assistant is intentionally narrow, the data is simulated, and authentication is not implemented.

## What the platform does

1. Loads reference data and a historical sample dataset into PostgreSQL.
2. Reads telemetry and event records for the supported building assets.
3. Runs configured threshold, anomaly, and compound alert rules.
4. Groups related alerts into incidents and ranks them by severity, status, and recency.
5. Re-reads evidence from the original fact tables before using it in an explanation.
6. Generates explanations through a local Ollama model when explicitly requested, or uses a deterministic fallback.
7. Offers recommendations from conditional playbooks rather than allowing the model to invent actions.
8. Exposes dashboard KPI and incident endpoints through FastAPI, with a small browser-based demo interface.

### High-level data flow

```text
Seeded telemetry and events
            |
            v
       PostgreSQL
            |
            v
      Alert detection
            |
            v
     Incident grouping
            |
            v
  Evidence re-verification
            |
            v
  Explanation + grounding checks
        /           \
   Ollama          Fallback
        \           /
            |
            v
 Conditional recommendations
            |
            v
       FastAPI / UI
```

## Technology stack

| Area | Technology |
|---|---|
| Language | Python 3.11 |
| API | FastAPI + Uvicorn |
| Database | PostgreSQL 16 |
| Database drivers | psycopg 3 and psycopg2 |
| AI runtime | Ollama (optional at runtime) |
| Frontend | HTML, CSS, and JavaScript |
| Deployment for the demo | Docker Compose |

## Repository structure

```text
Anzma-Platform-Submit/
├── App.py                         # FastAPI application and HTTP routes
├── app/
│   └── dashboard_core_pg.py       # PostgreSQL dashboard KPI calculations
├── db/
│   └── init/
│       └── anzma_schema_and_seed.sql
│                                  # Current schema and initial simulated dataset
├── day2_ahmed/
│   ├── alert_config.py             # Alert rules, thresholds, and playbooks
│   ├── alert_engine.py             # Threshold, anomaly, and compound detection
│   ├── db_adapter.py               # Database mapping and access for the alert pipeline
│   ├── incident_engine.py          # Incident grouping, ranking, and lifecycle
│   ├── evidence.py                  # Evidence retrieval and source-row verification
│   ├── ai_explainer.py              # Ollama calls and deterministic fallback
│   ├── validation.py                # Grounding checks and response envelope
│   ├── recommendations.py           # Conditional playbook recommendations
│   ├── integrity_rules.py           # System/record compatibility checks
│   ├── pipeline.py                  # Detection and explanation orchestration
│   ├── migrations/
│   │   └── 003_incidents.sql        # Incidents table and alerts link
│   ├── test_day2_ahmed.py           # Offline/unit-style tests
│   ├── test_day2_integration.py     # PostgreSQL integration tests
│   ├── TEST_RESULTS_DAY2.txt        # Last committed test output
│   └── DAY2_AHMED_HANDOVER.md       # Pipeline handover and known limitations
├── static/
│   └── index.html                  # Browser-based ask/incident demo
├── docs/
│   ├── ARCHITECTURE.md
│   ├── API_CONTRACT.md
│   └── INTEGRATION_PLAN.md
├── DATA_MODEL.md
├── DECISION_MATRIX.md
├── data-model.sql                  # Earlier draft; not the current bootstrap schema
├── reference_data.py               # Dashboard system-type values
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

## Sample data

A fresh database initialization loads simulated reference data and historical records. The current seed contains:

| Data | Seeded amount / coverage |
|---|---:|
| Buildings | 2 |
| Locations | 6 |
| Assets | 11 |
| System types | 5 |
| Metric types | 8 |
| Event types | 12 |
| Telemetry history | 78 hours, generated at 15-minute intervals |
| Heartbeat history | Hourly records, with the configured camera-offline gap |

The sample data is synthetic. It does not represent live connections to HVAC controllers, network equipment, access-control panels, cameras, or fire panels.

### Database initialization source of truth

For the current implementation, initialize the database in this order:

1. `db/init/anzma_schema_and_seed.sql` — schema, reference data, telemetry, and event seed.
2. `day2_ahmed/migrations/003_incidents.sql` — incidents table, indexes, and `alerts.incident_id`.

Docker Compose mounts both files into PostgreSQL's initialization directory in that order. PostgreSQL runs these scripts automatically **only when the database volume is first created and empty**.

Do not use the root-level `data-model.sql` as the current bootstrap schema; its header identifies it as an earlier draft.

## Quick start with Docker Compose

### Prerequisites

- Docker Desktop with Docker Compose v2, or Docker Engine with the Compose plugin.
- Git.
- Enough memory and disk space for PostgreSQL, the API, and (optionally) the Ollama model.

### 1. Clone the repository

```bash
git clone https://github.com/Amr-69/Anzma-Platform-Submit.git
cd Anzma-Platform-Submit
```

### 2. Configure the environment

```bash
cp .env.example .env
```

Edit `.env` and set a non-placeholder database password:

```dotenv
PGDATABASE=anzma
PGUSER=postgres
PGPASSWORD=replace-with-a-local-password

OLLAMA_MODEL=qwen2.5:3b
OLLAMA_TIMEOUT=120
```

Keep `.env` local. Do not commit database passwords, API secrets, or other credentials.

### 3. Start PostgreSQL and the API

```bash
docker compose up -d --build db api
docker compose ps
```

On a new Docker volume, PostgreSQL automatically creates the schema, loads the sample data, and applies the incidents migration through the mounted initialization scripts. No separate manual migration command is required for a fresh volume.

### 4. Open the application

- Browser demo: [http://localhost:8000](http://localhost:8000)
- FastAPI interactive documentation: [http://localhost:8000/docs](http://localhost:8000/docs)
- Service liveness endpoint: [http://localhost:8000/health](http://localhost:8000/health)

The `/health` endpoint currently reports that the API process is responding; it does **not** verify the PostgreSQL connection.

### 5. Optional: start Ollama

The API can operate without Ollama because it has a deterministic fallback. To start the local model service and its one-shot model download service:

```bash
docker compose up -d ollama ollama-init
docker compose logs -f ollama-init
```

The Compose default is `qwen2.5:3b`. The model is stored in the named `ollama_models` volume. The API container is configured to reach Ollama at `http://ollama:11434`.

The browser demo currently sends `use_llm: false` with its questions, so it uses the deterministic path even when Ollama is running. To request an Ollama explanation, use the API with `use_llm: true` as shown below.

### Stopping the services

```bash
docker compose down
```

This stops the containers but preserves the database and model volumes.

To delete the volumes and rebuild the sample database from scratch:

```bash
docker compose down -v
docker compose up -d --build db api
```

> **Warning:** `docker compose down -v` permanently deletes the local PostgreSQL data and downloaded Ollama models in the Compose volumes.

## Current HTTP API

The following routes are implemented in `App.py` on this branch. FastAPI also exposes the interactive OpenAPI page at `/docs`.

| Method | Route | Purpose |
|---|---|---|
| GET | `/` | Serves the static browser demo |
| GET | `/health` | API process liveness response |
| GET | `/dashboard/filters` | Returns available system/building filters and data time coverage |
| GET | `/dashboard/summary` | Returns the eight dashboard KPIs for the requested filters and time window |
| GET | `/incidents` | Lists incidents |
| GET | `/incidents/{incident_id}` | Returns one incident |
| GET | `/incidents/{incident_id}/evidence` | Returns incident evidence and verification results |
| POST | `/alerts/{alert_id}/explain` | Explains an existing alert; accepts the `use_llm` query parameter |
| POST | `/ask` | Runs the current limited operational-question workflow |

### Dashboard summary filters

`GET /dashboard/summary` accepts these optional query parameters:

- `system_type`: `HVAC`, `NETWORKING`, `ACCESS_CONTROL`, `CCTV`, or `FIRE_ALARM`.
- `building_id`: a seeded building ID.
- `start_time` and `end_time`: UTC timestamps in `YYYY-MM-DDTHH:MM:SSZ` format.

The selected time interval is start-inclusive and end-exclusive. The dashboard logic rejects ranges longer than 31 days.

### Ask the current assistant

Example request:

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Why is CRAC 1 temperature high?",
    "use_llm": false
  }'
```

The current `/ask` route is a limited demonstration, not a general-purpose assistant. It accepts questions containing terms such as `crac`, `temperature`, `hvac`, or `cooling`. Other domains return HTTP 501. The route runs detection and selects the latest `HVAC_HIGH_TEMP` alert before producing the response.

To request the local model through this endpoint, set `use_llm` to `true`:

```json
{
  "question": "Why is CRAC 1 temperature high?",
  "use_llm": true
}
```

The alert explanation endpoint can also request the model:

```text
POST /alerts/{alert_id}/explain?use_llm=true
```

If Ollama is unavailable, the explanation pipeline can return a deterministic fallback built from the stored evidence and playbook rules.

### Understanding the explanation status

The successful status used by the current validator is `grounding_supported`, not `validated`. It means that the implemented grounding checks passed: citations, numbers, dates/times, evidence references, supported causes, and approved playbook actions are checked against the stored evidence.

It is **not** a semantic correctness guarantee. The project does not currently implement an independent semantic validation stage.

## Running the tests

Run the offline tests:

```bash
python day2_ahmed/test_day2_ahmed.py
```

Run the PostgreSQL integration tests:

```bash
python day2_ahmed/test_day2_integration.py
```

The integration test creates a temporary test database, so the PostgreSQL user configured through `PGHOST`, `PGPORT`, `PGUSER`, and `PGPASSWORD` must have permission to create databases.

The last committed output in `day2_ahmed/TEST_RESULTS_DAY2.txt` records:

| Test group | Recorded result |
|---|---:|
| Offline tests | 25/25 passed |
| PostgreSQL integration tests | 43/43 passed |

These are the results recorded in the repository's test-output file; they are not a claim that the tests were rerun as part of this README update.

`python test_dashboard.py` is a manual dashboard/database smoke script that prints the summary; it is not an automated assertion-based test suite.

## Important implementation boundaries and known limitations

- **Simulated data only:** this branch does not connect to live building devices or external building-management systems.
- **Limited assistant scope:** `/ask` currently routes only HVAC/CRAC/temperature/cooling questions.
- **No ingestion HTTP routes in this build:** the `/api/v1/telemetry/batch` and `/api/v1/events/batch` routes described in the API design document are not exposed by the current `App.py`.
- **Dashboard UI scope:** the current static page shows the ask interface and incidents. The KPI calculations are exposed through dashboard API routes but are not rendered as a full KPI dashboard in that page.
- **Ollama is opt-in for requests:** the browser page explicitly sends `use_llm: false`. The API can request the model with `use_llm: true`.
- **Model default differs by execution mode:** Docker Compose defaults to `qwen2.5:3b`; `day2_ahmed/ai_explainer.py` has a `qwen2.5:7b` code default when `OLLAMA_MODEL` is not supplied. Set `OLLAMA_MODEL` explicitly for non-Compose runs.
- **Grounding, not semantic validation:** the validator verifies evidence references and playbook grounding. It does not independently prove that an explanation's reasoning is semantically correct.
- **Bandwidth unit mismatch:** `bandwidth_util` is seeded/stored as a percentage, while some alert thresholds and rule logic are expressed in Mbps. Bandwidth-related detections and cause classification are therefore limited until the units are reconciled.
- **Incomplete diagnostic signals:** the current seed does not provide fan state, filter state, battery state, motion, or access-granted details. Recommendations that depend on those signals cannot be produced from this dataset.
- **Access-denial reasons are absent:** denial events do not contain a reason, so related explanations can correctly report partial data sufficiency.
- **Playbook approver values are placeholders:** the current playbooks use the placeholder approver `Ahmed (Day 2 playbook owner)`. These actions need real engineering review and approval before operational use.
- **No authentication or authorization:** the API, database, and Ollama ports are exposed by the local Compose configuration. Do not expose this demo to an untrusted network.
- **UI validation badge mismatch:** the static page styles only the literal status `validated` as successful, while the API returns `grounding_supported`; the badge styling can therefore be misleading.

## Design and handover documents

- [Architecture](docs/ARCHITECTURE.md)
- [Data model](DATA_MODEL.md)
- [Decision matrix](DECISION_MATRIX.md)
- [API contract](docs/API_CONTRACT.md)
- [Integration plan](docs/INTEGRATION_PLAN.md)
- [Day 2 pipeline handover](day2_ahmed/DAY2_AHMED_HANDOVER.md)
- [Recorded test results](day2_ahmed/TEST_RESULTS_DAY2.txt)

Some supporting documents describe target or proposed behavior rather than the routes currently exposed by the application. In particular, `docs/API_CONTRACT.md` describes a proposed `/api/v1` interface; use the **Current HTTP API** section above and `App.py` as the source of truth for the present implementation.

## Authors

Anzma Tech project by Amr and Ahmed.

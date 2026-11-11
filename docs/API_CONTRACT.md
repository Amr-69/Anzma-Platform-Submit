# API_CONTRACT.md - Anzma Unified Platform

**Owner:** Amr (reviewed by: Ahmed - `[TBD: confirm]`)
**Base path:** `/api/v1` **[Proposed]**. Ahmed's current paths (`/ingest`, `/alerts`, `/chat`, `/dashboard/*`) have no prefix **[Verified]** and are moved under `/api/v1`; unprefixed aliases may stay during Day 2 only.

Tags: **[Verified]** exists in code today, **[Proposed]** agreed by this document, **[TBD]** open.

## 1. Conventions

| Topic | Rule |
|---|---|
| Format | JSON, UTF-8. |
| Time | ISO 8601 UTC with `Z`, for example `2026-11-05T06:00:00Z`. Naive timestamps are rejected (Ahmed's current code treats them as UTC; the contract is stricter). |
| Window | `hours` in `{1, 6, 12, 24}`, anchored to the latest event in the dataset. `[TBD: add 72/168 for history questions]` |
| System codes | `HVAC, NETWORK, ACCESS, CCTV, FIRE_ALARM`. |
| Severity | `INFO, WARNING, CRITICAL`. |
| Pagination | `limit` (1-1000, default 100) and `offset` (>= 0) on list endpoints **[Verified]** in Amr's code. |
| Auth | None in Day 2 (input validation only). |

## 2. Error format **[Proposed]**

```json
{
  "error": {
    "code": "BATCH_DUPLICATE",
    "message": "Batch rejected: 2 duplicate records.",
    "details": [{"index": 4, "asset_id": 3, "ts": "2026-11-05T05:00:00Z"}]
  }
}
```

| HTTP | `code` | When |
|---|---|---|
| 400 | `BAD_REQUEST` | Malformed parameters. |
| 404 | `NOT_FOUND` | Unknown id. |
| 404 | `NO_MATCHING_RECORDS` | The query is valid but returns nothing. |
| 409 | `BATCH_DUPLICATE` | Any record already exists. Nothing is stored. |
| 422 | `VALIDATION_ERROR` | Invalid field, unknown asset, future timestamp, event type not allowed for the asset's system. Nothing is stored. |
| 503 | `DATA_UNAVAILABLE` | Database not reachable or dataset missing. Different from "no matching records". |
| 503 | `MODEL_UNAVAILABLE` | Only if no fallback can answer. Normally the assistant falls back and returns 200. |

## 3. Ingestion

### POST `/api/v1/telemetry/batch` and `/api/v1/events/batch`

Atomic: all records are stored or none. 1 to 5000 records. Timestamps later than now + 5 minutes are rejected **[Verified]** (Ahmed); duplicates return 409 **[Verified]** (Amr).

Request (events):

```json
{
  "events": [
    {
      "asset_id": 7,
      "ts": "2026-11-05T06:00:00Z",
      "event_type": "LINK_DOWN",
      "severity": "CRITICAL",
      "value": null,
      "unit": null,
      "details": {"port": "Gi0/1"}
    }
  ]
}
```

Request (telemetry):

```json
{
  "records": [
    {"asset_id": 3, "metric": "supply_air_temp", "ts": "2026-11-05T06:00:00Z", "value": 24.6}
  ]
}
```

Response 201:

```json
{"inserted": 1}
```

`asset_name` may be used instead of `asset_id` **[Verified]** (Ahmed). `[TBD: keep both?]`

## 4. Retrieval

### GET `/api/v1/telemetry` and `/api/v1/events`

Query: `asset_id`, `location_id`, `system_type`, `start_time`, `end_time`, `limit`, `offset` **[Verified]** (Amr).

Response 200:

```json
{
  "count": 1,
  "limit": 100,
  "offset": 0,
  "items": [
    {
      "id": 1042,
      "asset": {"id": 7, "name": "SW-CORE-01", "system_type": "NETWORK"},
      "location": {"id": 2, "building": "HQ", "floor": 1, "zone": "Server Room"},
      "ts": "2026-11-05T06:00:00Z",
      "event_type": "LINK_DOWN",
      "severity": "CRITICAL"
    }
  ]
}
```

### GET `/api/v1/assets`, `/api/v1/locations`, `/api/v1/systems`, `/api/v1/health`

Reference lists and a health probe **[Verified]** (Ahmed) moved under the prefix.

## 5. Dashboard

| Endpoint | Purpose | Source |
|---|---|---|
| GET `/dashboard/filters` | Available systems, locations, window sizes | Amr **[Verified]** |
| GET `/dashboard/overview` | KPIs and system status | Amr (name), logic merged **[Proposed]** |
| GET `/dashboard/trends` | Time series | Ahmed **[Verified]** |
| GET `/dashboard/equipment-health` | Per-asset status | Ahmed **[Verified]** |

Parameters: `hours`, `system_type`, `location_id` (optional).

Response of `/dashboard/overview`:

```json
{
  "window": {"from": "2026-11-04T06:00:00Z", "to": "2026-11-05T06:00:00Z", "hours": 24},
  "kpis": [
    {"key": "critical_alerts", "name": "Critical alerts", "value": 5, "unit": "count", "applicable": true}
  ],
  "systems": [
    {"system_type": "HVAC", "status": "CRITICAL", "assets": 3}
  ]
}
```

`status` is one of `OK, WARNING, CRITICAL, NO_DATA` (`NO_DATA` is new, **[Proposed]**). Each KPI is defined once in `KPI_DEFINITIONS.md`.

## 6. Alerts

| Endpoint | Purpose | Source |
|---|---|---|
| POST `/alerts/run?hours=` | Evaluate rules over a window | Ahmed **[Verified]** |
| GET `/alerts` | List; filters `severity`, `status`, `system_type`, `hours` | Ahmed **[Verified]** |
| GET `/alerts/{id}` | Alert with evidence, explanation, recommendations | Ahmed **[Verified]** |
| GET `/alerts/{id}/explain` | Stored explanation | Ahmed **[Verified]** |
| POST `/alerts/{id}/explain` | Generate explanation | Ahmed **[Verified]** |
| PATCH `/alerts/{id}` | Change status `OPEN -> ACKNOWLEDGED -> RESOLVED` | New **[Proposed]** |

Alert object:

```json
{
  "id": 41,
  "rule_id": "HVAC_HIGH_TEMP",
  "detection_method": "THRESHOLD",
  "severity": "CRITICAL",
  "status": "OPEN",
  "asset": {"id": 3, "name": "HVAC-CRAC-01", "system_type": "HVAC"},
  "title": "HVAC temperature above threshold",
  "summary": "...",
  "first_ts": "2026-11-05T04:30:00Z",
  "last_ts": "2026-11-05T06:00:00Z",
  "evidence": [{"ref": "E1", "event_id": 9001, "ts": "2026-11-05T05:10:00Z", "value": 34.1}],
  "incident_id": 12
}
```

`incident_id` is new **[Proposed]**. The asset names above are illustrative; real names come from the database.

## 7. Incidents **[Proposed]** (new)

| Endpoint | Purpose |
|---|---|
| GET `/incidents` | List ranked incidents; filters `status`, `severity`, `hours` |
| GET `/incidents/{id}` | One incident with its alerts, assets, locations and recommended actions |

```json
{
  "id": 12,
  "status": "OPEN",
  "severity": "CRITICAL",
  "rank": 1,
  "title": "Server-room cooling failure",
  "location": {"id": 2, "zone": "Server Room"},
  "first_ts": "2026-11-05T04:30:00Z",
  "last_ts": "2026-11-05T06:00:00Z",
  "alert_ids": [41, 42],
  "grouping_rule": "same_location_within_15_min",
  "recommended_actions": [{"id": "HVAC_VERIFY", "priority": 1, "text": "Confirm the reading on site..."}]
}
```

Incident IDs are stable (stored). Today Amr's incidents are addressed by list index **[Verified]**; this fixes that.

## 8. Assistant

### POST `/api/v1/assistant/chat`

Request:

```json
{
  "question": "What is wrong with the building?",
  "session_id": "b2f7c1e0-...",
  "hours": 24,
  "model": "qwen",
  "scenario": null
}
```

| Field | Rule |
|---|---|
| `question` | 1-500 characters (both sides use 500) **[Verified]**. |
| `session_id` | Optional. Server-side memory is keyed by it. If missing, a new one is returned. |
| `hours` | `{1, 6, 12, 24}`. |
| `model` | `qwen` or `llama` (Ollama models). |
| `scenario` | Optional. Day 2: filters to a scenario's time window from `scenario_runs`. `[TBD: Ahmed]` |
| history | **Not sent by the client.** Today the browser sends the last 8 messages **[Verified]**; memory moves to the server **[Proposed]**. |

Response 200:

```json
{
  "session_id": "b2f7c1e0-...",
  "answer": "The server-room cooling unit reports a critical temperature. [F2]",
  "language": "en",
  "tool_used": "get_recent_faults",
  "model_used": "qwen2.5:3b",
  "ai_source": "llm",
  "data_sufficiency": "complete",
  "window": {"from": "2026-11-04T06:00:00Z", "to": "2026-11-05T06:00:00Z", "hours": 24},
  "evidence": [{"ref": "F2", "text": "HVAC-CRAC-01 ... ", "data": {"asset": "HVAC-CRAC-01"}}],
  "references": {"assets": [3], "incidents": [12]},
  "validation": {
    "status": "validated",
    "checks_run": ["citation_exists", "number_in_evidence", "time_in_evidence", "date_in_evidence", "asset_name_exact", "required_fact_cited", "judgment_phrase"],
    "errors": [],
    "attempts": 1
  },
  "limitations": []
}
```

| Field | Values |
|---|---|
| `ai_source` | `llm` (model answer passed validation) or `fallback` (deterministic facts) or `rules` (fixed text, for example out of scope). Today Amr's code always returns `llm` **[Verified]**; the contract makes `fallback` real. |
| `data_sufficiency` | `complete`, `partial`, `no_data`. |
| `validation.status` | `validated`, `not_validated`, `failed`. **`validated` only when `checks_run` is non-empty and every check passed.** |
| `checks_run` | Only checks that actually ran. The seven names above are the ones implemented in Ahmed's validator **[Verified]**; the contract may not list others. |
| `limitations` | Plain-text statements about what the answer cannot cover (for example "no energy readings in the dataset"). |

Out-of-scope example: `ai_source = "rules"`, `validation.status = "not_validated"`, `data_sufficiency = "no_data"`, with a fixed refusal text.

Tools behind this endpoint **[Proposed]**: `get_building_status`, `get_recent_faults`, `get_cctv_availability`, `get_network_issues`, `get_corrective_priorities`, `get_energy_consumption_change` (Amr's six **[Verified]**), implemented by Ahmed's collectors (`c_status`, `c_faults`, `c_cameras`, `c_network`, `c_repair`, `c_energy`) **[Verified]**. Optional later: `get_kpis`, `get_history` (from `c_kpi`, `c_history`).

### GET `/api/v1/assistant/questions`

Suggested questions for UI buttons **[Verified]** (Ahmed's `/chat/questions`, renamed).

## 8. Assistant chat contract

The widget chat endpoint returns a JSON object shaped like this:

```json
{
  "answer": "There are 8 critical alerts in the database.",
  "language": "en",
  "source": "DATABASE",
  "grounding": {"passed": true},
  "sources": [{"ref": "Q1", "text": "verified in database: ..."}],
  "data_sufficient": true,
  "window": {"hours": 24},
  "session_state": {"last_alert_id": 42}
}
```

`source` is one of:

- `DATABASE`: the answer was derived from a verified read-only SQL query against the application database.
- `FALLBACK`: the answer is still grounded in database evidence, but the app did not find a dedicated direct answer path and used the fallback logic.
- `RULES`: the reply is intentionally not a database fact; it is a guidance rule explaining that the request is unsupported or needs more detail.

`grounding.passed` must be computed from actual query results, not a hard-coded constant. `True` means the response numbers/timestamps/assets came back from the database or a validated fallback path; `False` means the answer is intentionally non-factual guidance.

The widget sends `history` and `session_state` on every request and reuses them for follow-up phrases such as `it`, `that`, or Arabic `ده`.

## 9. Module interfaces (Python)

```python
class ModelProvider(Protocol):
    name: str
    def generate_json(self, messages: list[dict], *, temperature: float = 0) -> dict: ...

class Tool(Protocol):
    name: str
    description: str
    def run(self, cur, *, hours: int, lang: str, **kw) -> "ToolResult": ...

@dataclass
class ToolResult:
    facts: list[dict]          # [{"ref": "F1", "text": str, "data": dict}]
    sufficient: bool
    window: dict
    references: dict           # {"assets": [...], "incidents": [...]}

class Validator(Protocol):
    def check(self, text: str, facts: list[dict]) -> "ValidationResult": ...  # status, checks_run, errors
```

`OllamaProvider` is the only implementation in the default build.

## 10. Open items

1. Window sizes beyond `{1, 6, 12, 24}`. `[TBD]`
2. `scenario` parameter semantics. `[TBD]`
3. Keep `asset_name` as an alternative to `asset_id` in ingestion. `[TBD]`
4. Whether unprefixed aliases stay on Day 2. `[TBD]`

# Contract changes from the Day 2 fixes (for the API owner)

API_CONTRACT.md section 8 (`validation`, `data_sufficiency`) and sections 6-7 (alerts, incidents) must be aligned.

| # | Change | Old | New |
|---|---|---|---|
| 1 | `validation.status` success value | `validated` | **`grounding_supported`** (`failed` and `not_validated` unchanged) |
| 2 | New field `validation.scope` | - | always `"grounding"`: values, times, dates, citations exist in the stored evidence and causes/actions come from the approved playbook. It is **not** a semantic check. |
| 3 | `validation.checks_run` for alert explanations | 7 names | adds `evidence_row_verified` (first in the list, only when the evidence rows were re-read from the fact tables). Alert-only names: `cause_supported_by_facts`, `action_in_playbook`, `evidence_ref_exists`. |
| 4 | Explanation envelope (alert) | - | new `evidence_verification = {checked, verified, unverified, problems[]}`; `evidence[]` rows carry `verified` and `problem`. |
| 5 | Unverified evidence | - | any unverified row -> `validation.status = failed`, `ai_source = rules`, no recommendations, a limitation. |
| 6 | Evidence row shape | `event_id` | `source` (`event`\|`telemetry`), `record_id`, and exactly one of `event_id` / `telemetry_id`. |
| 7 | Incident grouping | `grouping_rule = same_location_within_15_min` | **`same_asset_or_system_within_15_min`** (same asset, or same system type in the same location, within 15 min). |
| 8 | Recommendations | `{action_id, priority, text, ...}` | adds `status` (`approved` only is ever returned) and `approver`. |
| 9 | `detect()` result | - | adds `as_of` (latest stored record, ISO) and `limitations[]` with the freshness statement; every explanation gets the same limitation. |
| 10 | Errors | - | a lost/unreachable database raises `db_adapter.DatabaseUnavailable` -> map to `503 DATA_UNAVAILABLE`. |

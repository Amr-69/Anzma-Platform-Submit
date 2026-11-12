# Day 3 submission status

## Verified on 2026-11-12

- `test_frontend_api.py`: **17/17 passed** in Docker. The test covers the dashboard API, detection, incidents, evidence validation, deterministic HVAC fallback, Unknown/no-data semantics, and application routes. See [TEST_RESULTS_FRONTEND_API.txt](TEST_RESULTS_FRONTEND_API.txt).
- `test_chat_consistency.py`: **31/31 passed** against the running application. The assistant and dashboard agreed on the tested status counts, status labels, critical event KPI, and five system tiles. See [TEST_RESULTS_CHAT_CONSISTENCY.txt](TEST_RESULTS_CHAT_CONSISTENCY.txt).
- Docker built and started the API. `/`, `/dashboard`, static resources, and `/health` passed the API test.

The critical count definition is critical rows in `event_records` in the selected data window. Grouped alert and incident lifecycle counts remain separate. Recovered faults clear the asset's current Critical state while historical event and alert records remain visible. Never-reported assets are Unknown; stale previously reported assets are Warning.

## Still required for acceptance

1. **Playbook approval:** Amr reports engineering-owner approval for the HVAC playbook. Confirm that approval covers the other system playbooks before presenting their actions as approved.
2. **Live model path:** the latest API test logged Ollama timeouts. Run the integrated S1 explanation with Ollama/Qwen available and capture a response whose source confirms the live model was used. Keep a separate deterministic fallback run.
3. **Joint walkthrough:** record the database-backed S1 workflow with evidence verification, grounding validation, recommendations, current/recovered status, dashboard display, and assistant response visible. Do not claim live-model success unless the output confirms it.
4. **Final handover:** include the repository, these test results, runtime/database instructions, known limitations, and the walkthrough.

## Startup note

The current image starts Uvicorn with `App:asgi_app`; Docker startup and the HTTP routes passed in the recorded test run. The earlier startup error cannot be attributed conclusively to a specific old image or container configuration from the available logs.

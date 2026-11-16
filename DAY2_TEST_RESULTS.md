# DAY2_TEST_RESULTS.md

**Freshly executed by Ahmed on his machine against the Docker PostgreSQL 16 database. Raw outputs: `test_logs/` (files 1–6, `commit.txt`, `tree.txt`).** Result: **103/103 PASS, `verify_schema.sql` OVERALL OK, 0 failures, 0 regressions observed.**
(The authoring assistant could not run anything itself; the figures below are transcribed from the uploaded logs.)

## 1. Commit and working tree (`test_logs/commit.txt`, `tree.txt`)
Commit `87e084d753bcc478b20898b9b9507a2013955ceb`, branch `final`. **The tests ran on that commit plus a dirty working tree, not on a clean commit:**
- staged (Amr): `app/chat_facts.py`, `app/chat_lite.py`, `docker-compose.yml`
- modified (Ahmed): `DATA_MODEL.md`, `data-model.sql`, `day2_ahmed/db_adapter.py`, `day2_ahmed/e2e_s1.py`, `day2_ahmed/test_day2_integration.py`, `test_frontend_api.py`
- untracked: migrations 004 and 005, `test_data_integrity.py`, `verify_schema.sql`, the four `DAY2_*`/`DATABASE_BASELINE` docs, `test_logs/`, and a stray `anzma_day2_ahmed_deliverables.zip` (do not commit the zip)

## 2. Environment and configuration

- **Database:** Docker Compose, PostgreSQL 16.15 (Debian 16.15-1.pgdg13+2), aarch64.
- **Python:** 3.11.9
- **Installed PostgreSQL drivers:**
  - psycopg2: 2.9.10
  - psycopg3: 3.2.3
- **Operating System:** macOS 15.5
- **Test databases:** `anzma_day2_test`, `anzma_integrity_test`, `anzma_api_test`.
- Each test used its own temporary database. The `anzma` development database was not used by the tests.

## 3. Commands and fresh results
| # | Command (from repo root) | Result | Log |
|---|---|---|---|
| 1 | `cd day2_ahmed && python test_day2_ahmed.py` | **25/25 passed** | `test_logs/1_day2_ahmed.txt` |
| 2 | `cd day2_ahmed && python test_day2_integration.py` | **43/43 passed** (bootstrap now applies 003, 004, 005, each twice) | `2_integration.txt` |
| 3 | `cd day2_ahmed && python test_data_integrity.py` | **18/18 passed** (new) | `3_integrity.txt` |
| 4 | `python test_frontend_api.py` | **17/17 passed** | `4_frontend_api.txt` |
| 5 | `psql -f verify_schema.sql` on the live DB | checks 1–10 all **OK**, `OVERALL OK`; counts: CHECK 14, UNIQUE 8, FK 14, 0 duplicate alerts | `5_verify.txt` |
| 6 | `cd day2_ahmed && python3 e2e_s1.py --build` | **14/14 passed — PASS** (S1 end-to-end; fallback explanation path; 14/14 evidence rows verified; no duplicate alerts on re-run) | `test_logs/6_e2e_s1.txt` |
Total: 25 + 43 + 18 + 17 = **103 passed, 0 failed.**

## 4. What these results do and do not show
- Show: migrations 003 → 004 → 005 apply cleanly and idempotently; the database rejects NaN/invalid severity/`last_ts<first_ts`/resolved-without-`resolved_ts`/bad FKs/duplicate telemetry, events and alerts; two concurrent writers create one alert; `store_alerts` with `ON CONFLICT` works through the pipeline and the dashboard API; the live database matches the expected catalog; the previous 85 tests still pass after the changes (the earlier 85/85 report is historical; these runs supersede it for the current tree).
- - Do **not** show: live Ollama/Qwen behaviour (only the fallback path was tested; Amr owns live-AI verification), `docker compose` clean-install with 005 (the mount is not yet in `docker-compose.yml`; Amr), `test_dashboard.py` (not run), `test_chat_consistency.py` (deferred, Amr), the `anzma` development database's own 005 application step (not logged).
- Regressions introduced: none observed in the executed tests.

## 5. Historical (not fresh)
Earlier session: 85/85 on `87e084d` plus earlier local changes, before 005 and the bootstrap edits. Evidence in `anzma_evidence/` (clean install exit 0, 0 orphans, 0 duplicate alerts, data clock and bandwidth findings).

## 6. Outstanding items and owners
Add the 005 mount to `docker-compose.yml`, then re-run a clean install (Amr); live Qwen test and readiness check (Amr); `test_chat_consistency` (Amr); bandwidth unit/thresholds (Eng. Mohsen/Amr); data clock / demo data (Amr); run `e2e_s1.py` once (Ahmed).

# DAY2_HANDOVER.md — Amr and Eng. Mohsen
Facts and schema: [DATABASE_BASELINE.md](DATABASE_BASELINE.md). What changed and test status: [DAY2_IMPLEMENTATION_REPORT.md](DAY2_IMPLEMENTATION_REPORT.md).

## For Amr (owns `docker-compose.yml`; I did not touch it)
1. Your staged compose already mounts base (01), 003 (02) and 004 (03). **Add under the `db` service volumes, after the 004 line:**
   `      - ./day2_ahmed/migrations/005_alert_uniqueness.sql:/docker-entrypoint-initdb.d/04-alert-uniqueness.sql:ro`
2. Commit `004_chat_views.sql` and the compose change **together** (otherwise a clean clone builds without the views), and 005 with them.
3. Already-initialised volumes are not re-run: either rebuild (`docker compose down -v`, loses data) or apply 005 by hand: `docker compose exec -T db psql -U $PGUSER -d $PGDATABASE < day2_ahmed/migrations/005_alert_uniqueness.sql`. Do this before running the new `store_alerts`.
4. Run the four test commands (see DAY2_TEST_RESULTS.md) and `verify_schema.sql`; send me any FAIL.
5. Decisions needed from you: data-clock/demo-data strategy (BASELINE §5); `test_chat_consistency`; migration runner.

## For Eng. Mohsen
Decision needed: bandwidth unit (BASELINE §6) — recommended to keep `%` and set new thresholds/labels (the current 900/980 cannot trigger on values 15–72); fixing will produce new alerts.

## Summary to send to Eng. Mohsen
Alert deduplication is now enforced by the database (unique index on rule, asset, first timestamp; migration 005) instead of only by code, closing a race between the detection loop and manual alert creation. Integrity tests (constraints, views, concurrency), updated test bootstraps, a read-only schema verifier and a central DATABASE_BASELINE.md are written. Executed on the Docker PostgreSQL 16 database: 25+43+18+17 = 103/103 PASS and the schema verifier reports OVERALL OK; logs in test_logs/. Open decisions: bandwidth thresholds (you/Amr), demo data clock (Amr), 005 mount in Compose (Amr).

"""
test_data_integrity.py - proves the DATABASE itself rejects bad data (FK, CHECK, UNIQUE, NOT NULL), that the five views
exist with their columns, that migration 005 makes duplicate alerts impossible (also under concurrency), and pins the
CURRENT behaviour of the *_compat tables (not enforced by the database).

Builds a throw-away database (anzma_integrity_test) with the official sequence:
  db/init/anzma_schema_and_seed.sql -> 003_incidents.sql -> 004_chat_views.sql -> 005_alert_uniqueness.sql
(003/004/005 are applied twice to prove idempotency). Never touches any other database.

Run:  python test_data_integrity.py      (needs a reachable PostgreSQL; PGHOST/PGPORT/PGUSER/PGPASSWORD as in the other tests)
"""
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
sys.path.insert(0, ROOT)
import db_adapter as da                                   # noqa: E402  (selects the driver)

HOST, PORT = os.getenv("PGHOST", "localhost"), os.getenv("PGPORT", "5432")
USER, PASSWORD = os.getenv("PGUSER", "anzma"), os.getenv("PGPASSWORD", "anzma")
TEST_DB = "anzma_integrity_test"
SEQUENCE = [os.path.join(REPO, "db", "init", "anzma_schema_and_seed.sql"),
            os.path.join(ROOT, "migrations", "003_incidents.sql"),
            os.path.join(ROOT, "migrations", "004_chat_views.sql"),
            os.path.join(ROOT, "migrations", "005_alert_uniqueness.sql")]
CHECK, FK, UNIQUE, NOTNULL = "23514", "23503", "23505", "23502"
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

VIEW_COLUMNS = {
    "v_data_clock": {"as_of"},
    "v_asset_overview": {"asset_id", "asset_name", "asset_type", "system", "zone", "floor", "building", "health_24h",
                         "critical_events_24h", "warning_events_24h", "open_alerts", "last_seen"},
    "v_incident_overview": {"incident_id", "title", "severity", "status", "is_active", "zone", "first_ts", "last_ts",
                            "acknowledged_ts", "resolved_ts", "alert_count"},
    "v_alert_overview": {"alert_id", "rule_id", "detection_method", "severity", "status", "is_active", "title", "summary",
                         "first_ts", "last_ts", "incident_id", "asset_name", "asset_type", "system", "zone"},
    "v_camera_status": {"asset_id", "asset_name", "zone", "last_offline", "last_online", "is_offline"},
}


def connect(db, **kw):
    return da.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, dbname=db, **kw)


def build_database():
    admin = connect("postgres")
    admin.autocommit = True
    with admin.cursor() as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {TEST_DB}")
    admin.close()
    conn = connect(TEST_DB)
    with conn.cursor() as c:
        for path in SEQUENCE:
            sql = open(path, encoding="utf-8").read()
            c.execute(sql); conn.commit()
            if "migrations" in path:
                c.execute(sql); conn.commit()             # idempotent: second run must not fail
    return conn


CONN = build_database()
CUR = CONN.cursor()
RESULTS = []


def test(name):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


def sqlstate(exc):
    return getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)


def expect_rejected(sql, params=(), code=None):
    """Run a statement that must be rejected by the database; the transaction is kept usable via a savepoint."""
    CUR.execute("SAVEPOINT s")
    try:
        CUR.execute(sql, params)
    except Exception as exc:                               # noqa: BLE001
        CUR.execute("ROLLBACK TO SAVEPOINT s")
        got = sqlstate(exc)
        assert got is not None, f"not a database error: {exc!r}"
        assert code is None or got == code, f"expected SQLSTATE {code}, got {got}: {exc}"
        return
    CUR.execute("ROLLBACK TO SAVEPOINT s")
    raise AssertionError("the database accepted a row it must reject")


def one(sql, params=()):
    CUR.execute(sql, params)
    return CUR.fetchone()


def first_id(table, col):
    return list(one(f"SELECT {col} FROM {table} ORDER BY {col} LIMIT 1").values())[0]


ASSET, METRIC, ETYPE = first_id("assets", "asset_id"), first_id("metric_types", "metric_id"), first_id("event_types", "event_type_id")
ALERT_SQL = """INSERT INTO alerts (rule_id, detection_method, severity, asset_id, title, summary, first_ts, last_ts,
                                   detected_ts, evidence, status, resolved_ts)
               VALUES (%s,%s,%s,%s,'t','s',%s,%s,%s,'{}'::jsonb,%s,%s)"""


def alert_params(rule="R1", method="threshold", sev="warning", asset=None, first=T0, last=None, status="open", resolved=None):
    return (rule, method, sev, asset or ASSET, first, last or first, first, status, resolved)


@test("clean install: sequence applied, 13 tables and 5 views exist")
def _():
    n = one("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")["n"]
    assert n == 13, n
    v = one("SELECT count(*) AS n FROM information_schema.views WHERE table_schema='public'")["n"]
    assert v == 5, v


@test("views: each of the five exists, is queryable and has exactly the documented columns")
def _():
    for view, cols in VIEW_COLUMNS.items():
        CUR.execute(f"SELECT * FROM {view} LIMIT 0")
        got = {d[0] for d in CUR.description}
        assert got == cols, (view, got ^ cols)


@test("check: telemetry NaN / Infinity / -Infinity are rejected")
def _():
    for lit in ("'NaN'", "'Infinity'", "'-Infinity'"):
        expect_rejected(f"INSERT INTO telemetry_records (asset_id, metric_id, ts, value) VALUES (%s,%s,%s,{lit}::float8)",
                        (ASSET, METRIC, T0), CHECK)


@test("check: invalid severity is rejected (event_records, alerts, incidents)")
def _():
    expect_rejected("INSERT INTO event_records (asset_id, event_type_id, ts, severity) VALUES (%s,%s,%s,'urgent')",
                    (ASSET, ETYPE, T0), CHECK)
    expect_rejected(ALERT_SQL, alert_params(sev="urgent"), CHECK)
    expect_rejected("""INSERT INTO incidents (location_id, title, severity, first_ts, last_ts)
                       SELECT location_id, 't', 'urgent', %s, %s FROM locations LIMIT 1""", (T0, T0), CHECK)


@test("check: invalid detection_method / status / explanation_source are rejected")
def _():
    expect_rejected(ALERT_SQL, alert_params(method="magic"), CHECK)
    expect_rejected(ALERT_SQL, alert_params(status="closed"), CHECK)
    expect_rejected("""INSERT INTO alerts (rule_id, detection_method, severity, asset_id, title, summary, first_ts, last_ts,
                       detected_ts, evidence, explanation_source) VALUES ('R1','threshold','info',%s,'t','s',%s,%s,%s,'{}','gpt')""",
                    (ASSET, T0, T0, T0), CHECK)


@test("check: alert with last_ts < first_ts is rejected")
def _():
    expect_rejected(ALERT_SQL, alert_params(last=T0 - timedelta(minutes=1)), CHECK)


@test("check: status=resolved without resolved_ts (and resolved_ts without resolved) is rejected")
def _():
    expect_rejected(ALERT_SQL, alert_params(status="resolved", resolved=None), CHECK)
    expect_rejected(ALERT_SQL, alert_params(status="open", resolved=T0), CHECK)


@test("check: incident last_ts < first_ts and resolved without resolved_ts are rejected")
def _():
    expect_rejected("""INSERT INTO incidents (location_id, title, severity, first_ts, last_ts)
                       SELECT location_id, 't', 'info', %s, %s FROM locations LIMIT 1""", (T0, T0 - timedelta(minutes=1)), CHECK)
    expect_rejected("""INSERT INTO incidents (location_id, title, severity, status, first_ts, last_ts)
                       SELECT location_id, 't', 'info', 'resolved', %s, %s FROM locations LIMIT 1""", (T0, T0), CHECK)


@test("check: scenario_runs end_ts < start_ts is rejected")
def _():
    expect_rejected("""INSERT INTO scenario_runs (scenario_key, name, description, asset_ids, start_ts, end_ts, expected_rules)
                       VALUES ('k','n','d','{}',%s,%s,'{}')""", (T0, T0 - timedelta(hours=1)), CHECK)


@test("check: system_types.code outside the five allowed codes is rejected")
def _():
    expect_rejected("INSERT INTO system_types (code) VALUES ('ELEVATOR')", (), CHECK)


@test("foreign key: unknown asset / metric / event type are rejected")
def _():
    expect_rejected("INSERT INTO telemetry_records (asset_id, metric_id, ts, value) VALUES (-1,%s,%s,1)", (METRIC, T0), FK)
    expect_rejected("INSERT INTO telemetry_records (asset_id, metric_id, ts, value) VALUES (%s,-1,%s,1)", (ASSET, T0), FK)
    expect_rejected("INSERT INTO event_records (asset_id, event_type_id, ts, severity) VALUES (-1,%s,%s,'info')", (ETYPE, T0), FK)
    expect_rejected("INSERT INTO event_records (asset_id, event_type_id, ts, severity) VALUES (%s,-1,%s,'info')", (ASSET, T0), FK)
    expect_rejected(ALERT_SQL, alert_params(asset=-1), FK)
    expect_rejected("INSERT INTO assets (asset_code, name, asset_type, system_type_id, location_id) VALUES ('X','x','x',-1,-1)", (), FK)


@test("not null: alert without evidence / rule_id is rejected")
def _():
    expect_rejected("""INSERT INTO alerts (rule_id, detection_method, severity, asset_id, title, summary, first_ts, last_ts, detected_ts)
                       VALUES ('R1','threshold','info',%s,'t','s',%s,%s,%s)""", (ASSET, T0, T0, T0), NOTNULL)
    expect_rejected("""INSERT INTO alerts (detection_method, severity, asset_id, title, summary, first_ts, last_ts, detected_ts, evidence)
                       VALUES ('threshold','info',%s,'t','s',%s,%s,%s,'{}')""", (ASSET, T0, T0, T0), NOTNULL)


@test("unique: duplicate telemetry (asset, metric, ts) and duplicate event (asset, event_type, ts) are rejected")
def _():
    t = T0 + timedelta(days=400)                           # a timestamp the seed cannot use
    CUR.execute("INSERT INTO telemetry_records (asset_id, metric_id, ts, value) VALUES (%s,%s,%s,1)", (ASSET, METRIC, t))
    expect_rejected("INSERT INTO telemetry_records (asset_id, metric_id, ts, value) VALUES (%s,%s,%s,2)", (ASSET, METRIC, t), UNIQUE)
    CUR.execute("INSERT INTO event_records (asset_id, event_type_id, ts, severity) VALUES (%s,%s,%s,'info')", (ASSET, ETYPE, t))
    expect_rejected("INSERT INTO event_records (asset_id, event_type_id, ts, severity) VALUES (%s,%s,%s,'info')", (ASSET, ETYPE, t), UNIQUE)
    CONN.rollback()


@test("C1 migration 005: the unique index exists and a direct duplicate alert insert is rejected")
def _():
    CUR.execute("SELECT indexdef FROM pg_indexes WHERE tablename='alerts' AND indexname='uq_alerts_rule_asset_first_ts'")
    row = CUR.fetchone()
    assert row and "UNIQUE" in row["indexdef"] and "rule_id, asset_id, first_ts" in row["indexdef"], row
    CUR.execute(ALERT_SQL, alert_params(rule="DUP", first=T0 + timedelta(days=500)))
    expect_rejected(ALERT_SQL, alert_params(rule="DUP", first=T0 + timedelta(days=500)), UNIQUE)
    CONN.rollback()


@test("C1 store_alerts: second call with the same alert returns no new id and leaves one row")
def _():
    a = {"rule_id": "STORE_DUP", "detection_method": "THRESHOLD", "severity": "WARNING", "asset_id": ASSET, "title": "t",
         "summary": "s", "first_ts": T0 + timedelta(days=510), "last_ts": T0 + timedelta(days=510),
         "detected_ts": T0, "evidence": {"k": 1}}
    assert len(da.store_alerts(CUR, [a])) == 1
    assert da.store_alerts(CUR, [a]) == []
    assert one("SELECT count(*) AS n FROM alerts WHERE rule_id='STORE_DUP'")["n"] == 1
    CONN.rollback()


@test("C1 concurrency: two connections storing the same alert at the same time create exactly one row")
def _():
    CONN.rollback()
    ts = T0 + timedelta(days=520)
    alert = {"rule_id": "RACE", "detection_method": "THRESHOLD", "severity": "WARNING", "asset_id": ASSET, "title": "t",
             "summary": "s", "first_ts": ts, "last_ts": ts, "detected_ts": T0, "evidence": {}}
    a, b = connect(TEST_DB), connect(TEST_DB)
    out = {}

    def writer_b():
        with b.cursor() as cb:
            out["b"] = da.store_alerts(cb, [alert])        # blocks on A's uncommitted row until A commits
            b.commit()

    try:
        ca = a.cursor()
        out["a"] = da.store_alerts(ca, [alert])             # A inserts but does NOT commit yet
        th = threading.Thread(target=writer_b)
        th.start()
        time.sleep(0.7)                                     # B is now waiting on the unique index
        a.commit()
        th.join(timeout=10)
        assert not th.is_alive(), "writer B never finished"
    finally:
        a.close(); b.close()
    assert len(out["a"]) == 1 and out["b"] == [], out
    assert one("SELECT count(*) AS n FROM alerts WHERE rule_id='RACE'")["n"] == 1
    CONN.commit()                                           # end the read snapshot


@test("C1 migration 005 refuses to apply (clear error) when duplicates already exist")
def _():
    CONN.rollback()
    CUR.execute("DROP INDEX uq_alerts_rule_asset_first_ts")
    ts = T0 + timedelta(days=530)
    CUR.execute(ALERT_SQL, alert_params(rule="PRE", first=ts)); CUR.execute(ALERT_SQL, alert_params(rule="PRE", first=ts))
    CONN.commit()
    CUR.execute("SAVEPOINT s")
    try:
        CUR.execute(open(SEQUENCE[3], encoding="utf-8").read())
        raise AssertionError("migration applied over existing duplicates")
    except AssertionError:
        raise
    except Exception as exc:                                # noqa: BLE001
        CONN.rollback()
        assert "duplicate" in str(exc).lower(), exc
    CUR.execute("DELETE FROM alerts WHERE rule_id='PRE' AND alert_id NOT IN (SELECT min(alert_id) FROM alerts WHERE rule_id='PRE')")
    CUR.execute(open(SEQUENCE[3], encoding="utf-8").read())  # restore the index for the rest of the run
    CONN.commit()


@test("compat tables (CURRENT BEHAVIOUR): the database does NOT enforce metric/event <-> system compatibility")
def _():
    CUR.execute("""SELECT a.asset_id, m.metric_id FROM assets a CROSS JOIN metric_types m
                   WHERE NOT EXISTS (SELECT 1 FROM metric_type_system_compat c
                                     WHERE c.metric_id = m.metric_id AND c.system_type_id = a.system_type_id) LIMIT 1""")
    bad = CUR.fetchone()
    assert bad, "seed has no incompatible (asset, metric) pair to probe with"
    CUR.execute("INSERT INTO telemetry_records (asset_id, metric_id, ts, value) VALUES (%s,%s,%s,1)",
                (bad["asset_id"], bad["metric_id"], T0 + timedelta(days=600)))   # accepted: documented gap
    CUR.execute("""SELECT a.asset_id, e.event_type_id FROM assets a CROSS JOIN event_types e
                   WHERE NOT EXISTS (SELECT 1 FROM event_type_system_compat c
                                     WHERE c.event_type_id = e.event_type_id AND c.system_type_id = a.system_type_id) LIMIT 1""")
    bad = CUR.fetchone()
    assert bad, "seed has no incompatible (asset, event type) pair to probe with"
    CUR.execute("INSERT INTO event_records (asset_id, event_type_id, ts, severity) VALUES (%s,%s,%s,'info')",
                (bad["asset_id"], bad["event_type_id"], T0 + timedelta(days=600)))
    CONN.rollback()                                         # the check is application-level (integrity_rules.py / validation.py)


if __name__ == "__main__":
    ok = 0
    for name, fn in RESULTS:
        try:
            fn(); print("PASS ", name); ok += 1
        except Exception as exc:                            # noqa: BLE001
            try:
                CONN.rollback()
            except Exception:                               # noqa: BLE001
                pass
            print("FAIL ", name, f"\n      -> {type(exc).__name__}: {exc}")
    print(f"\n{ok}/{len(RESULTS)} passed")
    sys.exit(0 if ok == len(RESULTS) else 1)

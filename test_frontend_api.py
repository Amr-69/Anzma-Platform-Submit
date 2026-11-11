"""
test_frontend_api.py - the API the dashboard page calls, tested on a REAL PostgreSQL built from the files Docker uses:
db/init/anzma_schema_and_seed.sql, then day2_ahmed/migrations/003_incidents.sql.

Run:  PGHOST=localhost PGUSER=postgres PGPASSWORD=... python test_frontend_api.py      (user may CREATE DATABASE)
"""
import os
import sys

TEST_DB = "anzma_api_test"
HOST, PORT = os.getenv("PGHOST", "localhost"), os.getenv("PGPORT", "5432")
USER, PASSWORD = os.getenv("PGUSER", "postgres"), os.getenv("PGPASSWORD", "")
ROOT = os.path.dirname(os.path.abspath(__file__))

import psycopg                                    # noqa: E402
from psycopg.rows import dict_row                 # noqa: E402


def sql_file(path):
    return open(os.path.join(ROOT, path), encoding="utf-8").read()


with psycopg.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, dbname="postgres", autocommit=True) as admin:
    admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    admin.execute(f"CREATE DATABASE {TEST_DB}")
DB = psycopg.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, dbname=TEST_DB, row_factory=dict_row,
                     autocommit=True)
DB.execute(sql_file("db/init/anzma_schema_and_seed.sql"))
DB.execute(sql_file("day2_ahmed/migrations/003_incidents.sql"))

os.environ.update(PGHOST=HOST, PGPORT=PORT, PGUSER=USER, PGPASSWORD=PASSWORD, PGDATABASE=TEST_DB)
from fastapi.testclient import TestClient         # noqa: E402
import App                                        # noqa: E402

client = TestClient(App.api)
RESULTS = []


def test(name):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


def one(sql, *params):
    return DB.execute(sql, params).fetchone()


@test("fresh database: no alerts and no incidents before detection (nothing pre-seeded, no demo fire)")
def _():
    assert one("SELECT count(*) AS n FROM alerts")["n"] == 0 and one("SELECT count(*) AS n FROM incidents")["n"] == 0
    assert client.get("/alerts").json()["count"] == 0
    assert one("SELECT count(*) AS n FROM event_records WHERE details->>'demo' IS NOT NULL")["n"] == 0


@test("POST /alerts/run detects the seeded faults; running it again creates nothing new")
def _():
    first = client.post("/alerts/run")
    assert first.status_code == 200 and first.json()["new_alerts"] >= 8 and first.json()["as_of"]
    assert any("current only up to" in x for x in first.json()["limitations"])
    second = client.post("/alerts/run").json()
    assert second["new_alerts"] == 0 and second["incidents_created"] == 0


@test("GET /dashboard/overview: window, five system tiles and KPI cards in the shape the page reads")
def _():
    r = client.get("/dashboard/overview", params={"hours": 24})
    assert r.status_code == 200
    d = r.json()
    assert {"from", "to", "hours", "as_of"} <= set(d["window"])
    assert [s["type"] for s in d["systems"]] == ["HVAC", "NETWORK", "ACCESS", "CCTV", "FIRE_ALARM"]
    assert all({"type", "status", "assets", "ok", "warning", "critical"} <= set(s) for s in d["systems"])
    assert {s["status"] for s in d["systems"]} <= {"OK", "WARNING", "CRITICAL", "UNKNOWN"}
    assert sum(s["assets"] for s in d["systems"]) == one("SELECT count(*) AS n FROM assets")["n"]
    keys = {k["key"] for k in d["kpis"]}
    assert {"monitored_assets", "healthy_assets_pct", "alerts_raised", "critical_alerts", "avg_zone_temp",
            "avg_packet_loss", "heartbeat_compliance", "open_incidents"} <= keys
    assert all({"label", "value", "unit", "definition"} <= set(k) for k in d["kpis"])
    open_n = one("SELECT count(*) AS n FROM incidents WHERE status <> 'resolved'")["n"]
    assert next(k for k in d["kpis"] if k["key"] == "open_incidents")["value"] == open_n


@test("overview: KPI numbers equal the same counts computed straight from the tables")
def _():
    d = client.get("/dashboard/overview", params={"hours": 72}).json()
    end = one("SELECT max(ts) AS t FROM (SELECT ts FROM telemetry_records UNION ALL SELECT ts FROM event_records) u")["t"]
    n = one("""SELECT count(*) FILTER (WHERE severity IN ('warning','critical')) AS raised,
                      count(*) FILTER (WHERE severity = 'critical') AS crit FROM event_records
               WHERE ts >= %s - interval '72 hours' AND ts < %s + interval '1 second'""", end, end)
    k = {x["key"]: x["value"] for x in d["kpis"]}
    assert k["alerts_raised"] == n["raised"] and k["critical_alerts"] == n["crit"]
    assert k["monitored_assets"] == 12


@test("system_type accepts the page names (NETWORK, ACCESS) and the database codes; unknown value -> 400")
def _():
    for value in ("NETWORK", "NETWORKING", "ACCESS", "ACCESS_CONTROL", "HVAC"):
        assert client.get("/dashboard/overview", params={"system_type": value}).status_code == 200, value
    net = client.get("/dashboard/overview", params={"system_type": "NETWORK"}).json()
    assert next(k for k in net["kpis"] if k["key"] == "monitored_assets")["value"] == 3
    assert client.get("/dashboard/overview", params={"system_type": "PUMPS"}).status_code == 400
    assert client.get("/dashboard/overview", params={"hours": 0}).status_code == 400
    assert client.get("/dashboard/trends", params={"hours": 9999}).status_code == 400


@test("GET /dashboard/trends: aligned series, heartbeats not counted, access_granted is null (not stored), denied matches the table")
def _():
    d = client.get("/dashboard/trends", params={"hours": 72}).json()
    n = len(d["buckets"])
    for key in ("hvac_avg_temp", "bandwidth_avg", "bandwidth_peak", "access_denied"):
        assert len(d[key]) == n, key
    assert all(len(v) == n for v in d["events_by_system"].values())
    assert set(d["events_by_system"]) == {"HVAC", "NETWORK", "ACCESS", "CCTV", "FIRE_ALARM"}
    assert d["access_granted"] is None and d["bandwidth_unit"] == "%"
    end = one("SELECT max(ts) AS t FROM (SELECT ts FROM telemetry_records UNION ALL SELECT ts FROM event_records) u")["t"]
    cond = "ts >= %s - interval '72 hours' AND ts < %s + interval '1 second'"
    denied = one(f"""SELECT count(*) AS n FROM event_records x JOIN event_types t USING (event_type_id)
                     WHERE t.name='access_denied' AND {cond}""", end, end)["n"]
    non_hb = one(f"""SELECT count(*) AS n FROM event_records x JOIN event_types t USING (event_type_id)
                     WHERE t.name <> 'heartbeat' AND {cond}""", end, end)["n"]
    assert sum(d["access_denied"]) == denied > 0
    assert sum(sum(v) for v in d["events_by_system"].values()) == non_hb
    hot = max(v for v in d["hvac_avg_temp"] if v is not None)
    assert hot > 17, "HVAC temperature series must come from telemetry_records"


@test("trends: the S1 CRAC fault is visible as a temperature rise in the telemetry the chart is drawn from")
def _():
    d = client.get("/dashboard/trends", params={"hours": 72, "system_type": "HVAC"}).json()
    assert list(d["events_by_system"]) == ["HVAC"]
    peak = one("""SELECT max(value) AS v FROM telemetry_records t JOIN metric_types m USING (metric_id)
                  WHERE m.name='temperature' AND t.asset_id = 2""")["v"]
    assert peak > 30 and max(v for v in d["hvac_avg_temp"] if v is not None) > 18


@test("GET /dashboard/equipment-health: one row per asset with the fields the table reads; critical first")
def _():
    d = client.get("/dashboard/equipment-health", params={"hours": 72}).json()
    assert len(d["items"]) == 12 and d["window"]["to"]
    need = {"name", "system_type", "building", "floor", "zone", "status", "reason", "events", "faults", "warnings",
            "criticals", "last_seen"}
    assert all(need <= set(i) for i in d["items"])
    rank = {"CRITICAL": 0, "WARNING": 1, "OK": 2}
    assert [rank[i["status"]] for i in d["items"]] == sorted(rank[i["status"]] for i in d["items"])
    crac = next(i for i in d["items"] if i["name"] == "CRAC Unit 1")
    assert crac["status"] == "CRITICAL" and crac["criticals"] >= 1 and crac["faults"] >= 1
    healthy = next(i for i in d["items"] if i["name"] == "Lobby Camera")
    assert healthy["status"] == "OK" and healthy["last_seen"]
    only = client.get("/dashboard/equipment-health", params={"hours": 72, "system_type": "CCTV"}).json()
    assert {i["system_type"] for i in only["items"]} == {"CCTV"} and len(only["items"]) == 2


@test("GET /alerts: stored alerts, newest first, UI names and upper-case enums; filters work")
def _():
    d = client.get("/alerts", params={"limit": 50}).json()
    assert d["count"] == one("SELECT count(*) AS n FROM alerts")["n"] == len(d["items"])
    need = {"id", "severity", "title", "first_ts", "system_type", "asset_name", "zone", "summary", "status", "incident_id"}
    assert all(need <= set(a) for a in d["items"])
    assert {a["severity"] for a in d["items"]} <= {"INFO", "WARNING", "CRITICAL"}
    assert {a["system_type"] for a in d["items"]} <= {"HVAC", "NETWORK", "ACCESS", "CCTV", "FIRE_ALARM"}
    times = [a["first_ts"] for a in d["items"]]
    assert times == sorted(times, reverse=True)
    crit = client.get("/alerts", params={"severity": "CRITICAL"}).json()["items"]
    assert crit and all(a["severity"] == "CRITICAL" for a in crit)
    net = client.get("/alerts", params={"system_type": "NETWORK"}).json()["items"]
    assert net and all(a["system_type"] == "NETWORK" for a in net)
    assert client.get("/alerts", params={"severity": "BOGUS"}).status_code == 400
    assert client.get("/alerts", params={"limit": 0}).status_code == 422


@test("GET /alerts/{id}: evidence rows carry source references; unknown id -> 404")
def _():
    a = client.get("/alerts", params={"limit": 50}).json()["items"]
    crac = next(x for x in a if x["rule_id"] == "HVAC_HIGH_TEMP")
    one_alert = client.get(f"/alerts/{crac['id']}").json()
    ev = one_alert["evidence"]["events"]
    assert ev and all(e["source"] == "telemetry" and e["telemetry_id"] == e["record_id"] for e in ev)
    assert client.get("/alerts/999999").status_code == 404


@test("incidents: list, evidence verified, explanation grounded; the page's three calls work together")
def _():
    inc_list = client.get("/incidents").json()["incidents"]
    assert inc_list and inc_list[0]["severity"] == "critical"
    crac = next(i for i in inc_list if "CRAC" in i["title"])
    detail = client.get(f"/incidents/{crac['id']}").json()
    assert detail["recommended_actions"] and detail["grouping_rule"] == "same_asset_or_system_within_15_min"
    ev = client.get(f"/incidents/{crac['id']}/evidence").json()
    assert ev["total"] > 0 and ev["verified"] == ev["total"] and ev["unverified"] == []
    alert_id = detail["alert_ids"][0]
    ex = client.post(f"/alerts/{alert_id}/explain", params={"use_llm": "false"}).json()
    assert ex["validation"]["status"] == "grounding_supported" and ex["validation"]["scope"] == "grounding"
    assert ex["evidence_verification"]["unverified"] == 0 and ex["recommendations"]
    assert all(r["status"] == "approved" for r in ex["recommendations"])


@test("incident lifecycle over HTTP: open -> acknowledged -> resolved; resolved is final (409); unknown -> 404")
def _():
    inc_id = client.get("/incidents").json()["incidents"][0]["id"]
    r = client.post(f"/incidents/{inc_id}/status", json={"status": "acknowledged"})
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    assert client.post(f"/incidents/{inc_id}/status", json={"status": "resolved"}).json()["status"] == "resolved"
    assert one("SELECT count(*) AS n FROM alerts WHERE incident_id=%s AND status <> 'resolved'", inc_id)["n"] == 0
    assert client.post(f"/incidents/{inc_id}/status", json={"status": "open"}).status_code == 409
    assert client.post("/incidents/999999/status", json={"status": "resolved"}).status_code == 404
    kpi = client.get("/dashboard/overview").json()["kpis"]
    assert next(k for k in kpi if k["key"] == "open_incidents")["value"] == \
        one("SELECT count(*) AS n FROM incidents WHERE status <> 'resolved'")["n"]


@test("alerts and tampered evidence: a changed fact row is reported as unverified by the explain call")
def _():
    a = next(x for x in client.get("/alerts", params={"limit": 50}).json()["items"] if x["rule_id"] == "HVAC_HIGH_TEMP")
    tid = client.get(f"/alerts/{a['id']}").json()["evidence"]["events"][0]["telemetry_id"]
    DB.execute("UPDATE telemetry_records SET value = value + 5 WHERE telemetry_id = %s", (tid,))
    try:
        ex = client.post(f"/alerts/{a['id']}/explain", params={"use_llm": "false"}).json()
    finally:
        DB.execute("UPDATE telemetry_records SET value = value - 5 WHERE telemetry_id = %s", (tid,))
    assert ex["evidence_verification"]["unverified"] >= 1 and ex["validation"]["status"] == "failed"


@test("chat widget: suggested questions; HVAC question answered from evidence; incident list; unsupported topic is said plainly")
def _():
    q = client.get("/chat/questions").json()
    assert q["questions"]
    hv = client.post("/chat", json={"message": "Why is CRAC 1 temperature high?", "use_llm": False}).json()
    assert hv["source"] == "FALLBACK" and hv["grounding"]["passed"] is True and hv["sources"]
    assert all("verified in database" in s["text"] for s in hv["sources"]) and "[F1]" in hv["answer"]
    inc = client.post("/chat", json={"message": "What is wrong with the building?"}).json()
    assert inc["answer"].startswith("Open incidents") and "CRAC" in inc["answer"]
    other = client.post("/chat", json={"message": "tell me a joke please"}).json()
    assert other["source"] == "RULES" and other["grounding"]["passed"] is False
    assert client.post("/chat", json={"message": "x"}).status_code == 422


@test("pages: / and /dashboard serve the dashboard; /static files are served; /health answers")
def _():
    for path in ("/", "/dashboard"):
        r = client.get(path)
        assert r.status_code == 200 and "Building operations dashboard" in r.text, path
    assert client.get("/static/style.css").status_code == 200 and client.get("/static/chart.umd.js").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    html = client.get("/").text
    assert 'api("overview")' in html and "/alerts?limit=50" in html


@test("database down: every page endpoint answers 503 with a message instead of crashing")
def _():
    saved = os.environ["PGPORT"]
    os.environ["PGPORT"] = "1"
    try:
        calls = [client.get("/dashboard/overview"), client.get("/dashboard/trends"),
                 client.get("/dashboard/equipment-health"), client.get("/alerts"), client.get("/incidents"),
                 client.post("/alerts/run"), client.post("/chat", json={"message": "crac temperature"})]
        assert [c.status_code for c in calls] == [503] * len(calls), [c.status_code for c in calls]
    finally:
        os.environ["PGPORT"] = saved


if __name__ == "__main__":
    ok = 0
    for name, fn in RESULTS:
        try:
            fn()
            print("PASS ", name)
            ok += 1
        except Exception as exc:
            print("FAIL ", name, f"\n      -> {type(exc).__name__}: {exc}")
    print(f"\n{ok}/{len(RESULTS)} passed")
    sys.exit(0 if ok == len(RESULTS) else 1)

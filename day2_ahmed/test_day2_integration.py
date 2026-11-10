"""
test_day2_integration.py - end-to-end tests on a REAL PostgreSQL database (no Ollama needed).
Builds a throw-away database: 1) anzma_schema_and_seed.sql  2) migrations/003_incidents.sql (applied twice to prove
it is idempotent), then drives fact -> alert -> incident -> evidence -> explanation -> grounding check ->
recommendation through the real code.

Run:  python test_day2_integration.py          (needs a reachable PostgreSQL; see PG* variables below)
Driver: psycopg2 or psycopg (v3); force one with ANZMA_DRIVER=psycopg2|psycopg.
"""
import os
import sys
from datetime import timedelta, datetime, timezone

import db_adapter as da                                   # noqa: E402  (selects the driver)

HOST, PORT = os.getenv("PGHOST", "localhost"), os.getenv("PGPORT", "5432")
USER, PASSWORD = os.getenv("PGUSER", "anzma"), os.getenv("PGPASSWORD", "anzma")
TEST_DB = "anzma_day2_test"
ROOT = os.path.dirname(os.path.abspath(__file__))
SCHEMA_FILE = os.path.join(ROOT, "anzma_schema_and_seed.sql")          # authoritative runtime schema + demo seed
MIGRATION_003 = os.path.join(ROOT, "migrations", "003_incidents.sql")  # incidents, loaded AFTER the schema file


def connect(db, **kw):
    return da.connect(host=HOST, port=PORT, user=USER, password=PASSWORD, dbname=db, **kw)


def snapshot(cur):
    cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema='public' ORDER BY 1, 2""")
    return {(r["table_name"], r["column_name"]) for r in cur.fetchall()}


def build_database():
    admin = connect("postgres")
    admin.autocommit = True
    with admin.cursor() as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {TEST_DB}")
    admin.close()
    conn = connect(TEST_DB)
    with conn.cursor() as c:
        c.execute(open(SCHEMA_FILE, encoding="utf-8").read())
        conn.commit()
        before = snapshot(c)
        sql003 = open(MIGRATION_003, encoding="utf-8").read()
        c.execute(sql003)
        conn.commit()
        c.execute(sql003)                                  # idempotent: a second run must not fail
        conn.commit()
        after = snapshot(c)
    conn.commit()
    return conn, before, after


import ai_explainer                                       # noqa: E402
import db_adapter as da                                   # noqa: E402
import evidence as evd                                    # noqa: E402
import incident_engine as inc                             # noqa: E402
import integrity_rules as ir                              # noqa: E402
import pipeline                                           # noqa: E402
import recommendations as recs                            # noqa: E402
import validation as val                                  # noqa: E402
import alert_config as cfg                                # noqa: E402

RESULTS = []


def test(name):
    def deco(fn):
        RESULTS.append((name, fn))
        return fn
    return deco


CONN, SCHEMA_BEFORE, SCHEMA_AFTER = build_database()
CUR = CONN.cursor()
CUR.execute("SELECT (SELECT count(*) FROM alerts) AS a, (SELECT count(*) FROM incidents) AS i")
PRE = dict(CUR.fetchone())                                 # state of a fresh database, before detect()
RUN = pipeline.detect(CUR)
CONN.commit()


def alerts_where(**kw):
    cond = " AND ".join(f"{k}=%s" for k in kw)
    CUR.execute(f"SELECT * FROM alerts WHERE {cond} ORDER BY first_ts", tuple(kw.values()))
    return CUR.fetchall()


def asset_id(code):
    CUR.execute("SELECT asset_id FROM assets WHERE asset_code=%s", (code,))
    return CUR.fetchone()["asset_id"]


# ------------------------------------------------------------------ 1. integrity
@test("integrity: the seed breaks no compatibility rule")
def _():
    assert ir.stored_violations(CUR) == {"telemetry": 0, "events": 0}


@test("integrity: ingestion check rejects an incompatible metric/event for the asset's system")
def _():
    hvac = asset_id("HVAC-CRAC-01")
    assert ir.check_record_db(CUR, hvac, "metric", "temperature") == []
    assert ir.check_record_db(CUR, hvac, "metric", "smoke_level")            # smoke on an air-conditioner
    assert ir.check_record_db(CUR, hvac, "event", "fire_alarm_triggered")    # fire alarm on a CRAC
    assert ir.check_record_db(CUR, asset_id("FIRE-PNL-01"), "event", "link_down")
    assert ir.check_record_db(CUR, hvac, "event", "heartbeat") == []         # heartbeat is valid everywhere
    assert ir.check_record_db(CUR, 9999, "event", "heartbeat")               # unknown asset
    assert ir.check_record_db(CUR, hvac, "metric", "nope")                   # unknown type


# ------------------------------------------------------------------ 2. fault -> alert -> incident
EXPECTED = {   # every fault injected by anzma_schema_and_seed.sql and the rule that must catch it
    ("HVAC-CRAC-01", "HVAC_HIGH_TEMP"), ("NET-SW-01", "NET_PACKET_LOSS"), ("NET-SW-01", "NET_LINK_DOWN"),
    ("ACS-RDR-02", "ACC_DENIAL_SPIKE"), ("ACS-RDR-02", "ACC_DOOR_FORCED"), ("ACS-RDR-01", "ACC_DOOR_HELD_OPEN"),
    ("CCTV-CAM-02", "CCTV_OFFLINE_LONG"), ("FIRE-SMK-01", "FIRE_SMOKE")}   # FIRE-SMK-01: the cleared alarm


@test("seed: a fresh database holds no alerts and no incidents, and no hand-written/demo rows")
def _():
    assert PRE == {"a": 0, "i": 0}, PRE
    CUR.execute("SELECT count(*) AS n FROM event_records WHERE details->>'demo' IS NOT NULL")
    assert CUR.fetchone()["n"] == 0


@test("seed: 003 only adds the incidents table and alerts.incident_id (and is idempotent)")
def _():
    added = SCHEMA_AFTER - SCHEMA_BEFORE
    assert SCHEMA_BEFORE <= SCHEMA_AFTER
    assert {t for t, _ in added} == {"incidents", "alerts"}, added
    assert {c for t, c in added if t == "alerts"} == {"incident_id"}, added


@test("schema: every table/column the Day 2 modules use exists in the authoritative schema")
def _():
    used = {
        "buildings": "building_id name", "locations": "location_id building_id name",
        "system_types": "system_type_id code",
        "assets": "asset_id asset_code name system_type_id location_id",
        "metric_types": "metric_id name", "event_types": "event_type_id name",
        "metric_type_system_compat": "metric_id system_type_id", "event_type_system_compat": "event_type_id system_type_id",
        "telemetry_records": "telemetry_id asset_id metric_id ts value",
        "event_records": "event_id asset_id event_type_id ts severity details",
        "alerts": ("alert_id rule_id detection_method severity asset_id title summary first_ts last_ts detected_ts "
                   "evidence status resolved_ts explanation explanation_source explained_at incident_id"),
        "incidents": ("incident_id location_id title severity status grouping_rule first_ts last_ts "
                      "acknowledged_ts resolved_ts")}
    missing = [(t, c) for t, cols in used.items() for c in cols.split() if (t, c) not in SCHEMA_AFTER]
    assert not missing, missing


@test("S1: the CRAC high-temperature fault gives an alert and its incident; no active fire alert/incident exists")
def _():
    crac = alerts_where(rule_id="HVAC_HIGH_TEMP")
    assert len(crac) == 1 and crac[0]["incident_id"]
    fires = alerts_where(rule_id="FIRE_SMOKE")
    CUR.execute("SELECT a.asset_code FROM alerts al JOIN assets a USING (asset_id) WHERE al.rule_id='FIRE_SMOKE'")
    assert {r["asset_code"] for r in CUR.fetchall()} == {"FIRE-SMK-01"}, "the removed demo fire must not come back"
    assert all(f["evidence"]["facts"]["open"] is False for f in fires), "no fire alert may be open/active"


@test("alerts: every injected fault becomes a stored alert with the right rule")
def _():
    CUR.execute("""SELECT a.asset_code, al.rule_id FROM alerts al JOIN assets a USING (asset_id)""")
    got = {(r["asset_code"], r["rule_id"]) for r in CUR.fetchall()}
    assert EXPECTED <= got, EXPECTED - got


@test("alerts: no false positives - healthy assets raise nothing")
def _():
    CUR.execute("""SELECT a.asset_code FROM alerts al JOIN assets a USING (asset_id)""")
    alerting = {r["asset_code"] for r in CUR.fetchall()}
    healthy = {"HVAC-AHU-01", "HVAC-CHL-01", "NET-AP-01", "NET-SW-02", "CCTV-CAM-01", "FIRE-PNL-01"}
    assert not (alerting & healthy), alerting & healthy


@test("alerts: severities follow the thresholds and are stored lowercase")
def _():
    forced = alerts_where(rule_id="ACC_DOOR_FORCED")[0]
    assert forced["severity"] == "critical" and forced["detection_method"] == "threshold"
    pl = alerts_where(rule_id="NET_PACKET_LOSS")[0]
    f = pl["evidence"]["facts"]
    assert pl["severity"] == "critical" and f["loss_mean_pct"] >= cfg.T["packet_loss_crit_mean"], f
    temp = alerts_where(rule_id="HVAC_HIGH_TEMP")[0]
    peak = temp["evidence"]["facts"]["max_value"]
    assert temp["severity"] == ("critical" if peak > cfg.T["hvac_temp_crit"] else "warning"), peak
    assert temp["evidence"]["facts"]["sustained"] is True


@test("incidents: every alert belongs to an incident of its own location, severity = worst alert")
def _():
    CUR.execute("""SELECT al.alert_id, al.severity, al.incident_id, a.location_id, i.location_id AS il, i.severity AS isev
                   FROM alerts al JOIN assets a USING (asset_id) LEFT JOIN incidents i USING (incident_id)""")
    rows = CUR.fetchall()
    assert rows and all(r["incident_id"] for r in rows)
    assert all(r["location_id"] == r["il"] for r in rows)
    for i in {r["incident_id"] for r in rows}:
        worst = max((r["severity"] for r in rows if r["incident_id"] == i), key=lambda s: inc.SEVERITY_RANK[s])
        assert [r["isev"] for r in rows if r["incident_id"] == i][0] == worst


@test("incidents: related alerts of one fault are grouped (CRAC threshold + anomaly alert -> one incident)")
def _():
    a = alerts_where(rule_id="HVAC_HIGH_TEMP")[0]
    b = alerts_where(rule_id="HVAC_TEMP_ANOMALY")[0]
    assert a["incident_id"] == b["incident_id"]
    i = inc.get_incident(CUR, a["incident_id"])
    assert i["grouping_rule"] == "same_asset_or_system_within_15_min" and len(i["alert_ids"]) >= 2 and "related" in i["title"]


@test("incidents: alerts far apart in time stay in separate incidents (same asset, > 15 min)")
def _():
    cams = alerts_where(rule_id="CCTV_OFFLINE_LONG")
    assert len(cams) == 2 and cams[0]["incident_id"] != cams[1]["incident_id"]


@test("incidents: pure grouping rule (same asset or same system+location, AND within 15 minutes)")
def _():
    t = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
    m = lambda x: t + timedelta(minutes=x)
    A = lambda i, asset, loc, system, f, l: {"i": i, "asset_id": asset, "location_id": loc, "system_type": system,
                                              "first_ts": m(f), "last_ts": m(l)}
    groups = inc.group_alerts([
        A(1, 10, 1, "HVAC", 0, 5), A(2, 10, 1, "HVAC", 12, 14),     # same asset, 7 min apart            -> with 1
        A(3, 11, 1, "HVAC", 16, 17),                                # other HVAC unit, same room, 2 min  -> with 1, 2
        A(4, 20, 1, "NETWORK", 1, 2),                               # other system, same room            -> alone
        A(5, 21, 1, "ACCESS", 3, 4),                                # other system, same room            -> alone
        A(6, 10, 1, "HVAC", 60, 61),                                # same asset but 45 min later        -> alone
        A(7, 12, 2, "HVAC", 1, 2)])                                 # same system, OTHER room            -> alone
    assert sorted(sorted(x["i"] for x in g) for g in groups) == [[1, 2, 3], [4], [5], [6], [7]]


@test("incidents: probe - a CRAC, a switch and a door alert in the SAME room within 15 min -> 3 separate incidents")
def _():
    CUR.execute("""INSERT INTO assets (asset_code, name, asset_type, system_type_id, location_id)
                   VALUES ('TEST-CRAC-1','Test CRAC 1','crac',1,2),('TEST-CRAC-2','Test CRAC 2','crac',1,2)
                   RETURNING asset_id""")
    c1, c2 = [r["asset_id"] for r in CUR.fetchall()]
    sw, door = asset_id("NET-SW-01"), asset_id("ACS-RDR-02")                    # all in location 2 (Server Room)
    CUR.execute("SELECT count(DISTINCT location_id) AS n FROM assets WHERE asset_id = ANY(%s)", ([c1, sw, door],))
    assert CUR.fetchone()["n"] == 1, "probe assets must share one location"
    base = da.latest_ts(CUR) + timedelta(hours=2)
    def add(asset, rule, minutes):
        CUR.execute("""INSERT INTO alerts (rule_id, detection_method, severity, asset_id, title, summary, first_ts,
                       last_ts, detected_ts, evidence) VALUES (%s,'threshold','warning',%s,'probe','probe',%s,%s,%s,%s)
                       RETURNING alert_id""",
                    (rule, asset, base + timedelta(minutes=minutes), base + timedelta(minutes=minutes),
                     base + timedelta(minutes=minutes), da.Json(CUR, {"events": [], "facts": {}})))
        return CUR.fetchone()["alert_id"]
    ids = [add(c1, "HVAC_HIGH_TEMP", 0), add(sw, "NET_PACKET_LOSS", 5), add(door, "ACC_DOOR_FORCED", 10)]
    same_system = add(c2, "HVAC_HIGH_TEMP", 12)           # another HVAC unit in the room, 12 min after the first
    inc.assign_incidents(CUR)
    CUR.execute("SELECT alert_id, incident_id FROM alerts WHERE alert_id = ANY(%s)", (ids + [same_system],))
    inc_of = {r["alert_id"]: r["incident_id"] for r in CUR.fetchall()}
    CONN.rollback()
    assert len({inc_of[i] for i in ids}) == 3, inc_of
    assert inc_of[same_system] == inc_of[ids[0]], "same system in the same room within 15 min is linked"


@test("incidents: ranked by severity, then status, then recency")
def _():
    ranked = inc.list_incidents(CUR)
    assert [r["rank"] for r in ranked] == list(range(1, len(ranked) + 1))
    sev = [inc.SEVERITY_RANK[r["severity"]] for r in ranked]
    assert sev == sorted(sev, reverse=True)
    assert ranked[0]["severity"] == "critical"


@test("pipeline: running detection again creates no duplicate alerts or incidents")
def _():
    CUR.execute("SELECT count(*) AS n FROM alerts"); before_a = CUR.fetchone()["n"]
    CUR.execute("SELECT count(*) AS n FROM incidents"); before_i = CUR.fetchone()["n"]
    again = pipeline.detect(CUR)
    CUR.execute("SELECT count(*) AS n FROM alerts"); after_a = CUR.fetchone()["n"]
    CUR.execute("SELECT count(*) AS n FROM incidents"); after_i = CUR.fetchone()["n"]
    CONN.rollback()
    assert (before_a, before_i) == (after_a, after_i) and again["new_alerts"] == 0


# ------------------------------------------------------------------ 3. evidence
@test("evidence: every incident's evidence is complete, linked to stored rows and verified against the fact tables")
def _():
    for i in inc.list_incidents(CUR):
        e = evd.incident_evidence(CUR, i["id"])
        assert e["total"] > 0 and e["verified"] == e["total"] and e["unverified"] == [], (i["id"], e["unverified"])
        assert {a["alert_id"] for a in e["alerts"]} == set(i["alert_ids"])
        for a in e["alerts"]:
            for ev in a["events"]:
                assert ev["source"] in ("event", "telemetry") and (ev.get("event_id") or ev.get("telemetry_id"))
                assert not ("event_id" in ev and "telemetry_id" in ev), "exactly one of event_id / telemetry_id"


@test("evidence: a tampered copy is detected (timestamp or value no longer matches the stored row)")
def _():
    a = alerts_where(rule_id="HVAC_HIGH_TEMP")[0]
    ev = a["evidence"]
    tel = next(e for e in ev["events"] if e["source"] == "telemetry")
    tel["value"] = tel["value"] + 5
    CUR.execute("UPDATE alerts SET evidence=%s WHERE alert_id=%s", (da.Json(CUR, ev), a["alert_id"]))
    out = evd.incident_evidence(CUR, a["incident_id"])
    CONN.rollback()
    assert any("value differs" in u["problem"] for u in out["unverified"]), out["unverified"]


@test("evidence: temperature alerts are backed by telemetry rows, door/camera/fire alerts by event rows")
def _():
    assert {e["source"] for e in alerts_where(rule_id="HVAC_HIGH_TEMP")[0]["evidence"]["events"]} == {"telemetry"}
    assert {e["source"] for e in alerts_where(rule_id="ACC_DOOR_FORCED")[0]["evidence"]["events"]} == {"event"}


# ------------------------------------------------------------------ 4. explanation, validation, recommendation
def stored(rule):
    return da.get_alert(CUR, alerts_where(rule_id=rule)[0]["alert_id"])


@test("validation: deterministic explanation of a stored alert is grounding_supported because checks really ran")
def _():
    a = stored("HVAC_HIGH_TEMP")
    out = pipeline.explain_alert(CUR, a["id"], use_llm=False)
    CONN.rollback()
    assert out["validation"]["status"] == "grounding_supported" and out["validation"]["checks_run"] and not out["validation"]["errors"]
    assert out["ai_source"] == "fallback" and out["references"]["assets"] == [a["asset_id"]]
    assert out["references"]["incidents"] == [a["incident_id"]]


@test("validation: an invented reading is rejected (stored alert)")
def _():
    a = stored("HVAC_HIGH_TEMP"); asset = da.get_asset(CUR, a["asset_id"])
    bad = {"summary": "The temperature hit 47.3 °C [E1].", "probable_causes": [], "recommended_actions": [],
           "evidence_refs": ["E1"]}
    v = val.validate_explanation(a, asset, bad)
    assert v["status"] == "failed" and any("47.3" in e for e in v["errors"]), v


@test("validation: an invented cause or unapproved action is rejected")
def _():
    a = stored("HVAC_HIGH_TEMP"); asset = da.get_asset(CUR, a["asset_id"])
    bad = {"summary": "Peak reached [E1].", "probable_causes": [{"id": "FAN_FAILURE"}, {"id": "COMPRESSOR_BURNT"}],
           "recommended_actions": [{"id": "REPLACE_CHILLER"}], "evidence_refs": ["E1"]}
    errs = val.validate_explanation(a, asset, bad)["errors"]
    assert any("FAN_FAILURE" in e for e in errs), "no fan state is recorded in the schema, FAN_FAILURE must be refused"
    assert any("COMPRESSOR_BURNT" in e for e in errs) and any("REPLACE_CHILLER" in e for e in errs)


@test("recommendations: only playbook actions whose conditions hold, linked to the alert's evidence refs")
def _():
    a = stored("HVAC_HIGH_TEMP")
    r = recs.recommend(a)["recommendations"]
    book = {x["id"] for x in cfg.PLAYBOOKS[cfg.RULES[a["rule_id"]]["playbook"]]["actions"]}
    ids = {x["action_id"] for x in r}
    refs = {e["ref"] for e in a["evidence"]["events"]}
    assert ids and ids <= book
    assert "HVAC_COOLING" in ids and "HVAC_VERIFY" in ids          # sustained -> cooling check; always verify
    assert "HVAC_FAN" not in ids and "HVAC_FILTER" not in ids      # no fan/filter fact stored -> not offered
    assert all(x["derived_by"] == "PLAYBOOK" and set(x["evidence_refs"]) == refs and x["alert_id"] == a["id"] for x in r)


@test("recommendations: an incident's actions are the merged playbook actions of its alerts")
def _():
    i = inc.get_incident(CUR, stored("HVAC_HIGH_TEMP")["incident_id"])
    assert i["recommended_actions"] and all(x["alert_ids"] for x in i["recommended_actions"])
    ids = [x["id"] for x in i["recommended_actions"]]
    assert len(ids) == len(set(ids)) and [x["priority"] for x in i["recommended_actions"]] == sorted(
        x["priority"] for x in i["recommended_actions"])


@test("data sufficiency: 'partial' with a limitation when the stored data cannot name a cause (denial reasons missing)")
def _():
    a = stored("ACC_DENIAL_SPIKE")
    out = pipeline.explain_alert(CUR, a["id"], use_llm=False)
    CONN.rollback()
    assert out["data_sufficiency"] == "partial" and out["limitations"], out["data_sufficiency"]
    assert out["validation"]["status"] == "grounding_supported"


@test("data sufficiency: an alert without stored evidence is no_data, not_validated, nothing recommended")
def _():
    a = dict(stored("HVAC_HIGH_TEMP"), evidence={"facts": {}, "events": [], "thresholds": {}, "event_count": 0})
    out = val.explain_alert(a, da.get_asset(CUR, a["asset_id"]), use_llm=False)
    assert out["data_sufficiency"] == "no_data" and out["validation"]["status"] == "not_validated"
    assert out["validation"]["checks_run"] == [] and out["recommendations"] == [] and out["limitations"]


@test("missing data: the camera that stopped reporting yields an alert from its offline events, never invented readings")
def _():
    cam = alerts_where(rule_id="CCTV_OFFLINE_LONG")
    open_one = [c for c in cam if c["evidence"]["facts"]["open"]][0]
    assert all(e["source"] == "event" for e in open_one["evidence"]["events"])
    CUR.execute("""SELECT count(*) AS n FROM telemetry_records WHERE asset_id=%s AND ts >= (
                   SELECT max(ts) - interval '290 minutes' FROM telemetry_records)""", (asset_id("CCTV-CAM-02"),))
    assert CUR.fetchone()["n"] == 0, "seed has no camera readings in the last 5 h; the engine must not fill the gap"
    out = pipeline.explain_alert(CUR, open_one["alert_id"], use_llm=False); CONN.rollback()
    assert out["validation"]["status"] == "grounding_supported"


@test("missing data: removing an asset's readings produces no alert and no crash")
def _():
    CUR.execute("DELETE FROM telemetry_records WHERE asset_id=%s", (asset_id("HVAC-CRAC-01"),))
    CUR.execute("DELETE FROM alerts WHERE asset_id=%s AND rule_id LIKE 'HVAC%%'", (asset_id("HVAC-CRAC-01"),))
    res = pipeline.detect(CUR)
    CUR.execute("SELECT count(*) AS n FROM alerts WHERE asset_id=%s", (asset_id("HVAC-CRAC-01"),))
    n = CUR.fetchone()["n"]
    CONN.rollback()
    assert n == 0 and res["detected"] >= 0


# ------------------------------------------------------------------ 5. lifecycle
@test("lifecycle: open -> acknowledged -> resolved carries over to the alerts; resolved is final")
def _():
    i = stored("ACC_DOOR_FORCED")["incident_id"]
    assert inc.set_status(CUR, i, "acknowledged") == "acknowledged"
    CUR.execute("SELECT status FROM alerts WHERE incident_id=%s", (i,))
    assert {r["status"] for r in CUR.fetchall()} == {"acknowledged"}
    inc.set_status(CUR, i, "resolved")
    CUR.execute("SELECT status, resolved_ts FROM alerts WHERE incident_id=%s", (i,))
    assert all(r["status"] == "resolved" and r["resolved_ts"] for r in CUR.fetchall())
    for bad in ("open", "acknowledged", "resolved"):
        try:
            inc.set_status(CUR, i, bad)
            raise AssertionError(f"resolved -> {bad} accepted")
        except ValueError:
            pass
    CONN.rollback()


@test("lifecycle: skipping straight from open to resolved is allowed; unknown incident raises")
def _():
    i = stored("FIRE_SMOKE")["incident_id"]
    inc.set_status(CUR, i, "resolved")
    try:
        inc.set_status(CUR, 99999, "resolved")
        raise AssertionError("unknown incident accepted")
    except LookupError:
        pass
    CONN.rollback()


@test("lifecycle: a resolved incident is not reopened by a new alert (a new incident is opened)")
def _():
    a = stored("ACC_DOOR_FORCED"); i = a["incident_id"]
    inc.set_status(CUR, i, "resolved")
    CUR.execute("""INSERT INTO alerts (rule_id, detection_method, severity, asset_id, title, summary, first_ts, last_ts,
                   detected_ts, evidence) SELECT rule_id, detection_method, severity, asset_id, title, summary,
                   first_ts + interval '1 minute', last_ts + interval '1 minute', detected_ts, evidence
                   FROM alerts WHERE alert_id=%s RETURNING alert_id""", (a["id"],))
    new_id = CUR.fetchone()["alert_id"]
    inc.assign_incidents(CUR)
    CUR.execute("SELECT incident_id FROM alerts WHERE alert_id=%s", (new_id,))
    new_inc = CUR.fetchone()["incident_id"]
    CONN.rollback()
    assert new_inc != i


# ------------------------------------------------------------------ 6. fallback
@test("fallback: Ollama unreachable -> grounding_supported deterministic answer, limitation stated, stored as 'fallback'")
def _():
    ai_explainer.OLLAMA_HOST, ai_explainer.OLLAMA_TIMEOUT = "http://127.0.0.1:1", 2
    a = stored("NET_PACKET_LOSS")
    out = pipeline.explain_alert(CUR, a["id"], use_llm=True)
    CUR.execute("SELECT explanation_source, explained_at, explanation FROM alerts WHERE alert_id=%s", (a["id"],))
    row = CUR.fetchone()
    CONN.rollback()
    assert out["ai_source"] == "fallback" and out["model_used"] is None and out["validation"]["status"] == "grounding_supported"
    assert any("language model was unavailable" in x for x in out["limitations"])
    assert out["recommendations"], "recommendations must not depend on the model"
    assert row["explanation_source"] == "fallback" and row["explained_at"] and row["explanation"]["answer"] == out["answer"]


@test("pipeline: explain_incident returns verified evidence, one grounding-checked explanation per alert and the actions")
def _():
    ai_explainer.OLLAMA_HOST = "http://127.0.0.1:1"
    i = stored("HVAC_HIGH_TEMP")["incident_id"]
    out = pipeline.explain_incident(CUR, i, use_llm=True)
    CONN.rollback()
    assert out["evidence"]["verified"] == out["evidence"]["total"] > 0
    assert set(out["explanations"]) == set(out["incident"]["alert_ids"])
    assert all(x["validation"]["status"] == "grounding_supported" for x in out["explanations"].values())
    assert out["incident"]["recommended_actions"]


# ------------------------------------------------------------------ 7. status wording (D4), evidence re-check, freshness, driver
@test("D4: no response is ever labelled 'validated'; success is 'grounding_supported' with scope 'grounding'")
def _():
    ai_explainer.OLLAMA_HOST = "http://127.0.0.1:1"
    statuses = set()
    for row in alerts_where(rule_id="HVAC_HIGH_TEMP") + alerts_where(rule_id="ACC_DENIAL_SPIKE") + \
            alerts_where(rule_id="FIRE_SMOKE"):
        out = pipeline.explain_alert(CUR, row["alert_id"], use_llm=False)
        statuses.add(out["validation"]["status"])
        assert out["validation"]["scope"] == "grounding" and out["validation"]["checks_run"]
    CONN.rollback()
    assert statuses == {"grounding_supported"} and "validated" not in statuses


@test("evidence: alert_evidence re-reads every row from the fact tables and flags each as verified")
def _():
    a = alerts_where(rule_id="HVAC_HIGH_TEMP")[0]
    e = evd.alert_evidence(CUR, a["alert_id"])
    assert e["total"] > 0 and e["verified"] == e["total"] and e["unverified"] == []
    assert all(r["verified"] is True and r["problem"] is None for r in e["events"])
    assert evd.alert_evidence(CUR, 999999) is None


def _first_telemetry_ref(alert_row):
    return next(x for x in alert_row["evidence"]["events"] if x["source"] == "telemetry")


@test("C12: explain_alert reports verified rows and a zero unverified count on untouched data")
def _():
    a = alerts_where(rule_id="HVAC_HIGH_TEMP")[0]
    out = pipeline.explain_alert(CUR, a["alert_id"], use_llm=False)
    CONN.rollback()
    v = out["evidence_verification"]
    assert v["unverified"] == 0 and v["verified"] == v["checked"] > 0
    assert all(r["verified"] for r in out["evidence"])
    assert "evidence_row_verified" in out["validation"]["checks_run"]
    assert out["validation"]["status"] == "grounding_supported"


@test("C12: a tampered FACT-TABLE value is reported as unverified; no grounding_supported, no recommendations")
def _():
    a = alerts_where(rule_id="HVAC_HIGH_TEMP")[0]
    ref = _first_telemetry_ref(a)
    CUR.execute("UPDATE telemetry_records SET value = value + 5 WHERE telemetry_id=%s", (ref["record_id"],))
    out = pipeline.explain_alert(CUR, a["alert_id"], use_llm=False)
    CONN.rollback()
    v = out["evidence_verification"]
    assert v["unverified"] >= 1 and any("value differs" in p["problem"] for p in v["problems"]), v
    assert out["validation"]["status"] == "failed" and out["validation"]["errors"]
    assert any(not r["verified"] for r in out["evidence"])
    assert any("could not be verified" in x for x in out["limitations"])
    assert out["recommendations"] == [] and out["ai_source"] == "rules"


@test("C12: a tampered FACT-TABLE timestamp is reported as unverified")
def _():
    a = alerts_where(rule_id="ACC_DOOR_FORCED")[0]
    ref = a["evidence"]["events"][0]
    CUR.execute("UPDATE event_records SET ts = ts + interval '1 second' WHERE event_id=%s", (ref["record_id"],))
    out = pipeline.explain_alert(CUR, a["alert_id"], use_llm=False)
    CONN.rollback()
    assert out["evidence_verification"]["unverified"] >= 1
    assert any("timestamp differs" in p["problem"] for p in out["evidence_verification"]["problems"])
    assert out["validation"]["status"] != "grounding_supported"


@test("C12: a tampered JSON copy inside the alert is reported as unverified in the single-alert path")
def _():
    a = alerts_where(rule_id="HVAC_HIGH_TEMP")[0]
    ev = a["evidence"]
    _first_telemetry_ref({"evidence": ev})["value"] += 5
    CUR.execute("UPDATE alerts SET evidence=%s WHERE alert_id=%s", (da.Json(CUR, ev), a["alert_id"]))
    out = pipeline.explain_alert(CUR, a["alert_id"], use_llm=False)
    CONN.rollback()
    assert out["evidence_verification"]["unverified"] >= 1 and out["validation"]["status"] == "failed"


@test("as_of: detect() returns the latest stored timestamp and a freshness limitation")
def _():
    res = pipeline.detect(CUR)
    CONN.rollback()
    assert res["as_of"] == da.latest_ts(CUR).isoformat() and res["window"]["to"] == da.latest_ts(CUR)
    assert any("current only up to" in x and res["as_of"][:10] in x for x in res["limitations"]), res["limitations"]


@test("as_of: every explanation carries the freshness limitation")
def _():
    a = alerts_where(rule_id="NET_PACKET_LOSS")[0]
    out = pipeline.explain_alert(CUR, a["alert_id"], use_llm=False)
    CONN.rollback()
    stamp = da.latest_ts(CUR).strftime("%Y-%m-%d %H:%M")
    assert any("current only up to " + stamp in x for x in out["limitations"]), out["limitations"]


@test("driver: an unreachable database gives DatabaseUnavailable, not a raw driver exception")
def _():
    try:
        da.connect(host=HOST, port="1", user=USER, password=PASSWORD, dbname="postgres", connect_timeout=2)
        raise AssertionError("connected to a port nobody listens on")
    except da.DatabaseUnavailable as exc:
        assert "unavailable" in str(exc)


@test("driver: a connection lost mid-run (backend terminated) gives DatabaseUnavailable from the pipeline")
def _():
    victim = connect(TEST_DB)
    vcur = victim.cursor()
    vcur.execute("SELECT pg_backend_pid() AS pid")
    pid = vcur.fetchone()["pid"]
    victim.rollback()
    admin = connect("postgres"); admin.autocommit = True
    admin.cursor().execute("SELECT pg_terminate_backend(%s)", (pid,))
    admin.close()
    for call in (lambda: pipeline.detect(vcur), lambda: pipeline.explain_alert(vcur, 1, use_llm=False)):
        try:
            call()
            raise AssertionError("a dead connection did not raise")
        except da.DatabaseUnavailable:
            pass
    try:
        victim.close()
    except Exception:
        pass



if __name__ == "__main__":
    ok = 0
    for name, fn in RESULTS:
        try:
            fn()
            print("PASS ", name); ok += 1
        except Exception as exc:
            try:
                CONN.rollback()
            except Exception:
                pass
            print("FAIL ", name, f"\n      -> {type(exc).__name__}: {exc}")
    print(f"\n{ok}/{len(RESULTS)} passed")
    print("detection run:", {k: (str(v) if k == 'window' else v) for k, v in RUN.items()})
    sys.exit(0 if ok == len(RESULTS) else 1)

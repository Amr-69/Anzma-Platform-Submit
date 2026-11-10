"""
e2e_s1.py - evidence run for the S1 cooling-failure scenario, on stored data only.

  Simulated fault -> alert detection -> incident / evidence retrieval -> explanation -> grounding check -> recommendation

Prints the real rows and results (this output is the evidence for checklist A4, A5, A6, A8, A10) and ends with the commit
hash, the date, the driver and an overall PASS/FAIL. Exit code 0 = every check passed.

  python e2e_s1.py --build            # drop/create database "anzma_e2e", load schema + 003, run S1 (fresh, empty volume)
  python e2e_s1.py --db mydb          # run on an existing database that already has schema + 003 loaded
  python e2e_s1.py --build --llm      # also try the live model (Ollama); falls back and says so if it is down

Connection: PGHOST PGPORT PGUSER PGPASSWORD (defaults localhost / 5432 / postgres / empty). Dashboard display is the
dashboard owner's part; this script ends at the API-ready envelope the dashboard consumes.
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

import db_adapter as da

ROOT = os.path.dirname(os.path.abspath(__file__))
CHECKS = []


def check(label, ok, detail=""):
    CHECKS.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def connect(db, **kw):
    return da.connect(host=os.getenv("PGHOST", "localhost"), port=os.getenv("PGPORT", "5432"),
                      user=os.getenv("PGUSER", "postgres"), password=os.getenv("PGPASSWORD", ""), dbname=db, **kw)


def build(db):
    admin = connect("postgres")
    admin.autocommit = True
    with admin.cursor() as c:
        c.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
        c.execute(f'CREATE DATABASE "{db}"')
    admin.close()
    conn = connect(db)
    with conn.cursor() as c:
        for f in ("anzma_schema_and_seed.sql", os.path.join("migrations", "003_incidents.sql")):
            c.execute(open(os.path.join(ROOT, f), encoding="utf-8").read())
            conn.commit()
    return conn


def git_hash():
    try:
        h = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5)
        return h.stdout.strip() if h.returncode == 0 else "not a git checkout (run: git rev-parse HEAD)"
    except Exception:
        return "git not available (run: git rev-parse HEAD)"


def section(title):
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="anzma_e2e")
    ap.add_argument("--build", action="store_true", help="drop/create the database and load schema + 003 first")
    ap.add_argument("--llm", action="store_true", help="try the live model; the deterministic fallback is used if down")
    args = ap.parse_args()

    import alert_engine  # noqa: F401  (imported here so a missing driver fails before any output)
    import evidence
    import incident_engine as inc
    import pipeline

    conn = build(args.db) if args.build else connect(args.db)
    cur = conn.cursor()

    section("0. Environment")
    print(f"  database: {args.db}   driver: {da.DRIVER}   run at: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")

    # ---------------------------------------------------------------- A4: S1 rows exist BEFORE detection
    section("1. S1 fault rows in the database BEFORE detection (A4)")
    cur.execute("SELECT (SELECT count(*) FROM alerts) AS a, (SELECT count(*) FROM incidents) AS i")
    pre = cur.fetchone()
    print(f"  alerts={pre['a']}  incidents={pre['i']}")
    check("no alerts and no incidents exist before detection (nothing pre-seeded)", pre["a"] == 0 and pre["i"] == 0)

    cur.execute("SELECT asset_id, name, location_id FROM assets WHERE asset_code='HVAC-CRAC-01'")
    crac = cur.fetchone()
    check("S1 asset HVAC-CRAC-01 (CRAC Unit 1) exists", crac is not None)
    print(f"  asset: {crac['name']} (asset_id={crac['asset_id']})")

    cur.execute("""SELECT e.event_id, t.name AS event_type, e.severity, e.ts, e.details FROM event_records e
                   JOIN event_types t USING (event_type_id)
                   WHERE e.asset_id=%s AND t.name IN ('hvac_fault','hvac_recovered') ORDER BY e.ts""", (crac["asset_id"],))
    fault_events = cur.fetchall()
    print("  event_records (unit-reported):")
    for e in fault_events:
        print(f"    event_id={e['event_id']:<6} {e['event_type']:<15} {e['severity']:<9} {e['ts'].isoformat()}  {e['details']}")
    check("the unit logged hvac_fault and hvac_recovered", [e["event_type"] for e in fault_events] == ["hvac_fault", "hvac_recovered"])

    t0, t1 = fault_events[0]["ts"], fault_events[-1]["ts"]
    cur.execute("""SELECT t.telemetry_id, t.ts, t.value FROM telemetry_records t JOIN metric_types m USING (metric_id)
                   WHERE t.asset_id=%s AND m.name='temperature' AND t.ts BETWEEN %s AND %s ORDER BY t.ts""",
                (crac["asset_id"], t0, t1))
    fault_rows = cur.fetchall()
    cur.execute("""SELECT max(t.value) AS peak, avg(t.value) AS mean FROM telemetry_records t JOIN metric_types m USING (metric_id)
                   WHERE t.asset_id=%s AND m.name='temperature' AND t.ts < %s - interval '1 hour'""", (crac["asset_id"], t0))
    base = cur.fetchone()
    print(f"  temperature rows during the fault window ({len(fault_rows)} rows, deg C):")
    for r in fault_rows:
        print(f"    telemetry_id={r['telemetry_id']:<6} {r['ts'].isoformat()}  {r['value']:.2f}")
    print(f"  baseline before the fault: mean {base['mean']:.2f}, max {base['peak']:.2f}")
    peak = max(r["value"] for r in fault_rows)
    check("fault readings are far above the pre-fault baseline", peak > base["peak"] + 5,
          f"fault peak {peak:.2f} vs baseline max {base['peak']:.2f}")

    # ---------------------------------------------------------------- detection
    section("2. Alert detection and incident grouping")
    res = pipeline.detect(cur)
    conn.commit()
    print(f"  as_of={res['as_of']}  detected={res['detected']}  new_alerts={res['new_alerts']}  "
          f"incidents_created={res['incidents_created']}  alerts_attached={res['alerts_attached']}")
    for lim in res["limitations"]:
        print(f"  limitation: {lim}")
    check("detect() reports an explicit as_of and a freshness limitation",
          res["as_of"] is not None and any("current only up to" in x for x in res["limitations"]))

    # ---------------------------------------------------------------- A5: S1 alert + incident, no fire
    section("3. S1 alert and incident (A5)")
    cur.execute("""SELECT alert_id, rule_id, severity, status, incident_id, first_ts, last_ts, summary FROM alerts
                   WHERE asset_id=%s ORDER BY first_ts, alert_id""", (crac["asset_id"],))
    s1 = cur.fetchall()
    for a in s1:
        print(f"  alert_id={a['alert_id']} {a['rule_id']:<18} {a['severity']:<8} {a['status']}  incident_id={a['incident_id']}")
        print(f"      {a['summary']}")
    rules = {a["rule_id"] for a in s1}
    check("S1 raised HVAC_HIGH_TEMP on the CRAC", "HVAC_HIGH_TEMP" in rules, ", ".join(sorted(rules)))
    check("all S1 alerts belong to ONE incident", len({a["incident_id"] for a in s1}) == 1 and s1[0]["incident_id"] is not None)
    incident_id = s1[0]["incident_id"]
    incident = inc.get_incident(cur, incident_id)
    print(f"  incident {incident['id']}: '{incident['title']}'  severity={incident['severity']}  status={incident['status']}  "
          f"rank={incident['rank']}  alerts={incident['alert_ids']}  rule={incident['grouping_rule']}")

    cur.execute("""SELECT a.asset_code, al.rule_id, al.severity, coalesce(al.evidence->'facts'->>'open','') AS open
                   FROM alerts al JOIN assets a USING (asset_id) WHERE al.asset_id <> %s ORDER BY al.first_ts""",
                (crac["asset_id"],))
    others = cur.fetchall()
    print("  other alerts in the seed (separate scenarios, NOT part of S1):")
    for o in others:
        print(f"    {o['asset_code']:<13} {o['rule_id']:<20} {o['severity']}")
    check("no active (open) fire alert exists; no fire alert on the Fire Panel",
          not any(o["rule_id"] == "FIRE_SMOKE" and (o["open"] == "true" or o["asset_code"] == "FIRE-PNL-01") for o in others))
    cur.execute("SELECT count(DISTINCT a.asset_code) AS n FROM alerts al JOIN assets a USING (asset_id) WHERE al.incident_id=%s",
                (incident_id,))
    check("the S1 incident contains alerts of the CRAC only", cur.fetchone()["n"] == 1)

    # ---------------------------------------------------------------- A6: evidence verified
    section("4. Incident evidence verified against the fact tables (A6)")
    ev = evidence.incident_evidence(cur, incident_id)
    for a in ev["alerts"]:
        print(f"  alert {a['alert_id']} {a['rule_id']}: {len(a['events'])} evidence rows")
        for e in a["events"]:
            print(f"    {e['ref']:<4} {e['source']:<9} record_id={e['record_id']:<6} {e['ts']}  value={e['value']}  "
                  f"verified={e['verified']}")
    print(f"  verified {ev['verified']} / {ev['total']}   unverified: {ev['unverified']}")
    check("every evidence row of the incident is verified against event_records / telemetry_records",
          ev["total"] > 0 and ev["verified"] == ev["total"] and not ev["unverified"], f"{ev['verified']}/{ev['total']}")

    # ---------------------------------------------------------------- explanation, grounding check, recommendation
    section("5. Explanation, grounding check, recommendation" + (" (live model requested)" if args.llm else " (fallback path)"))
    first = next(a for a in s1 if a["rule_id"] == "HVAC_HIGH_TEMP")
    out = pipeline.explain_alert(cur, first["alert_id"], use_llm=args.llm)
    conn.commit()
    v = out["validation"]
    print(f"  ai_source={out['ai_source']}  model_used={out['model_used']}  data_sufficiency={out['data_sufficiency']}")
    print(f"  validation: status={v['status']}  scope={v['scope']}  attempts={v['attempts']}")
    print(f"  checks_run: {', '.join(v['checks_run'])}")
    print(f"  evidence_verification: {out['evidence_verification']['verified']} verified / "
          f"{out['evidence_verification']['unverified']} unverified")
    print(f"  answer: {out['answer']}")
    for lim in out["limitations"]:
        print(f"  limitation: {lim}")
    print("  recommendations (approved playbook actions only):")
    for r in out["recommendations"]:
        print(f"    P{r['priority']} {r['action_id']:<15} [{r['status']}, {r['approver']}]  {r['text']}")
        print(f"         because: {'; '.join(r['basis'])}   evidence: {', '.join(r['evidence_refs'][:4])}...")
    check("the answer is grounding_supported (grounding only; not a semantic check)", v["status"] == "grounding_supported")
    check("recommendations exist, come from the approved playbook and cite the evidence",
          bool(out["recommendations"]) and all(r["derived_by"] == "PLAYBOOK" and r["status"] == "approved"
                                                 and r["evidence_refs"] for r in out["recommendations"]))
    if args.llm:
        check("live model path used (ai_source=llm) or the fallback is stated", out["ai_source"] == "llm" or
              any("language model was unavailable" in x for x in out["limitations"]))
    else:
        print("  (fallback path only; run with --llm while Ollama is up for A7, and stop Ollama and run --llm for A8)")

    cur.execute("SELECT explanation_source, explained_at IS NOT NULL AS stored FROM alerts WHERE alert_id=%s", (first["alert_id"],))
    row = cur.fetchone()
    check("the explanation is stored on the alert", row["stored"] and row["explanation_source"] in ("ollama", "fallback"),
          f"explanation_source={row['explanation_source']}")

    # ---------------------------------------------------------------- A10: idempotent re-run
    section("6. Re-running detection creates no duplicates (A10)")
    cur.execute("SELECT (SELECT count(*) FROM alerts) AS a, (SELECT count(*) FROM incidents) AS i")
    before = cur.fetchone()
    again = pipeline.detect(cur)
    conn.commit()
    cur.execute("SELECT (SELECT count(*) FROM alerts) AS a, (SELECT count(*) FROM incidents) AS i")
    after = cur.fetchone()
    print(f"  before: alerts={before['a']} incidents={before['i']}   after: alerts={after['a']} incidents={after['i']}   "
          f"new_alerts={again['new_alerts']}")
    check("second detect() adds nothing", before == after and again["new_alerts"] == 0)

    section("Result")
    print(f"  commit: {git_hash()}")
    print(f"  date:   {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"  checks: {sum(CHECKS)}/{len(CHECKS)} passed  ->  {'PASS' if all(CHECKS) else 'FAIL'}")
    return 0 if all(CHECKS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except da.DatabaseUnavailable as exc:
        print(f"DATABASE UNAVAILABLE: {exc}")
        sys.exit(2)

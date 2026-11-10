"""
evidence.py (Day 2 - Ahmed): evidence of an incident, fetched through the database layer only.

Each alert stores evidence rows with a hard reference (source + record_id) to event_records / telemetry_records.
incident_evidence() / alert_evidence() return them and VERIFY every reference against the fact tables (row exists, same timestamp,
same value for telemetry, same event type for events). Nothing is trusted from the JSON copy.
"""
import db_adapter as da
from datetime import datetime


def _verify(cur, rows):
    """rows: [(alert_asset_id, evidence_item)] -> list of problem strings (None = verified), one per row, in order.
    Checked per copy, not per record: the same stored row can be cited by several alerts."""
    ev_ids = sorted({e["record_id"] for _, e in rows if e.get("source") == "event" and e.get("record_id")})
    tel_ids = sorted({e["record_id"] for _, e in rows if e.get("source") == "telemetry" and e.get("record_id")})
    events, tel = {}, {}
    if ev_ids:
        cur.execute("""SELECT e.event_id, e.asset_id, e.ts, t.name FROM event_records e
                       JOIN event_types t USING (event_type_id) WHERE e.event_id = ANY(%s)""", (ev_ids,))
        events = {r["event_id"]: r for r in cur.fetchall()}
    if tel_ids:
        cur.execute("SELECT telemetry_id, asset_id, ts, value FROM telemetry_records WHERE telemetry_id = ANY(%s)",
                    (tel_ids,))
        tel = {r["telemetry_id"]: r for r in cur.fetchall()}
    problems = []
    for _, e in rows:
        if e.get("source") not in ("event", "telemetry") or e.get("record_id") is None:
            problems.append("evidence row has no source reference")
            continue
        row = (events if e["source"] == "event" else tel).get(e["record_id"])
        if row is None:
            problems.append("row not found in the fact table")
        elif row["ts"] != datetime.fromisoformat(e["ts"]):
            problems.append("timestamp differs from the stored row")
        elif e["source"] == "telemetry" and abs(row["value"] - e["value"]) > 1e-9:
            problems.append("value differs from the stored row")
        elif e["source"] == "event" and da.EVENT_MAP.get(row["name"], (None,))[0] != e["event_type"]:
            problems.append("event type differs from the stored row")
        else:
            problems.append(None)
    return problems


def incident_evidence(cur, incident_id):
    """-> {'incident_id', 'alerts': [{alert_id, rule_id, evidence_count, events:[... + 'verified']}],
           'total', 'verified', 'unverified': [...]}"""
    cur.execute("""SELECT alert_id, rule_id, asset_id, evidence FROM alerts WHERE incident_id=%s
                   ORDER BY first_ts, alert_id""", (incident_id,))
    alerts = cur.fetchall()
    flat = [(a["asset_id"], e) for a in alerts for e in a["evidence"].get("events", [])]
    problems = _verify(cur, flat)
    out, total, ok, bad, k = [], 0, 0, [], 0
    for a in alerts:
        events = []
        for e in a["evidence"].get("events", []):
            p = problems[k]
            k += 1
            events.append(dict(e, verified=p is None, problem=p))
            total += 1
            if p is None:
                ok += 1
            else:
                bad.append({"alert_id": a["alert_id"], "ref": e["ref"], "problem": p})
        out.append({"alert_id": a["alert_id"], "rule_id": a["rule_id"],
                    "evidence_count": a["evidence"].get("event_count", len(events)), "events": events,
                    "facts": a["evidence"].get("facts", {})})
    return {"incident_id": incident_id, "alerts": out, "total": total, "verified": ok, "unverified": bad}


def alert_evidence(cur, alert_id):
    """Evidence of ONE alert, every row re-read from the fact tables (never trusted from the alert's JSON copy).
    -> {'alert_id', 'events': [row + 'verified' + 'problem'], 'total', 'verified',
        'unverified': [{'alert_id', 'ref', 'problem'}]}   (None if the alert does not exist)"""
    cur.execute("SELECT asset_id, evidence FROM alerts WHERE alert_id=%s", (alert_id,))
    a = cur.fetchone()
    if a is None:
        return None
    items = (a["evidence"] or {}).get("events", [])
    problems = _verify(cur, [(a["asset_id"], e) for e in items])
    events = [dict(e, verified=p is None, problem=p) for e, p in zip(items, problems)]
    bad = [{"alert_id": alert_id, "ref": e["ref"], "problem": e["problem"]} for e in events if not e["verified"]]
    return {"alert_id": alert_id, "events": events, "total": len(events),
            "verified": len(events) - len(bad), "unverified": bad}

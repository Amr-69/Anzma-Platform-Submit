"""
incident_engine.py (Day 2 - Ahmed): alerts -> incidents, severity, ranking, lifecycle.

Alert    = one detection by one rule on one asset (what the engine found).
Incident = the managed record an operator works on: alerts of the SAME LOCATION whose time ranges are within
           GROUP_GAP_MIN minutes of each other (grouping_rule 'same_location_within_15_min').
Severity of an incident = the highest severity of its alerts. Lifecycle: open -> acknowledged -> resolved
(resolved is final). Acknowledging / resolving an incident carries over to its alerts.
All database access goes through the cursor passed in; no SQL lives anywhere else for incidents.
"""
from datetime import timedelta

import alert_config as cfg
import db_adapter as da
import recommendations as recs

GROUP_GAP_MIN = 15
GROUPING_RULE = "same_location_within_15_min"
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
TRANSITIONS = {"open": {"acknowledged", "resolved"}, "acknowledged": {"resolved"}, "resolved": set()}


def within_gap(a_first, a_last, b_first, b_last, gap_min=GROUP_GAP_MIN):
    """True when two [first,last] ranges overlap or are at most gap_min minutes apart."""
    gap = max(a_first - b_last, b_first - a_last)
    return gap <= timedelta(minutes=gap_min)


def max_severity(*sevs):
    return max(sevs, key=lambda s: SEVERITY_RANK[s])


def group_alerts(alerts, gap_min=GROUP_GAP_MIN):
    """Pure grouping (used by tests): alerts = dicts with location_id, first_ts, last_ts. Returns list of lists."""
    groups = []
    for a in sorted(alerts, key=lambda x: x["first_ts"]):
        for g in groups:
            if (g["location_id"] == a["location_id"]
                    and within_gap(a["first_ts"], a["last_ts"], g["first_ts"], g["last_ts"], gap_min)):
                g["items"].append(a)
                g["first_ts"], g["last_ts"] = min(g["first_ts"], a["first_ts"]), max(g["last_ts"], a["last_ts"])
                break
        else:
            groups.append({"location_id": a["location_id"], "first_ts": a["first_ts"], "last_ts": a["last_ts"],
                           "items": [a]})
    return [g["items"] for g in groups]


def _incident_title(alert_title, zone, n):
    return f"{alert_title} at {zone}" + (f" (+{n - 1} related alert{'s' if n > 2 else ''})" if n > 1 else "")


def assign_incidents(cur):
    """Attach every alert without an incident to an open/acknowledged incident of the same location within the
    gap, or open a new one. Idempotent. Returns {'created': n, 'attached': n}."""
    cur.execute("""SELECT al.alert_id, al.severity, al.title, al.first_ts, al.last_ts, a.location_id, l.name AS zone
                   FROM alerts al JOIN assets a USING (asset_id) JOIN locations l USING (location_id)
                   WHERE al.incident_id IS NULL ORDER BY al.first_ts, al.alert_id""")
    pending = cur.fetchall()
    created = attached = 0
    for al in pending:
        cur.execute("""SELECT incident_id, severity, first_ts, last_ts FROM incidents
                       WHERE location_id=%s AND status <> 'resolved'
                         AND GREATEST(%s - last_ts, first_ts - %s) <= make_interval(mins => %s)
                       ORDER BY last_ts DESC LIMIT 1""",
                    (al["location_id"], al["first_ts"], al["last_ts"], GROUP_GAP_MIN))
        inc = cur.fetchone()
        if inc:
            cur.execute("""UPDATE incidents SET severity=%s, first_ts=LEAST(first_ts,%s), last_ts=GREATEST(last_ts,%s)
                           WHERE incident_id=%s""",
                        (max_severity(inc["severity"], al["severity"]), al["first_ts"], al["last_ts"],
                         inc["incident_id"]))
            iid = inc["incident_id"]
            attached += 1
        else:
            cur.execute("""INSERT INTO incidents (location_id, title, severity, first_ts, last_ts, grouping_rule)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING incident_id""",
                        (al["location_id"], _incident_title(al["title"], al["zone"], 1), al["severity"],
                         al["first_ts"], al["last_ts"], GROUPING_RULE))
            iid = cur.fetchone()["incident_id"]
            created += 1
        cur.execute("UPDATE alerts SET incident_id=%s WHERE alert_id=%s", (iid, al["alert_id"]))
    _retitle(cur)
    return {"created": created, "attached": attached}


def _retitle(cur):
    """Title = the most severe (then earliest) alert's title + location, with the number of related alerts."""
    cur.execute("""SELECT DISTINCT ON (i.incident_id) i.incident_id, al.title, l.name AS zone,
                          count(*) OVER (PARTITION BY i.incident_id) AS n
                   FROM incidents i JOIN alerts al USING (incident_id) JOIN locations l USING (location_id)
                   ORDER BY i.incident_id,
                            CASE al.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                            al.first_ts, al.alert_id""")
    for r in cur.fetchall():
        cur.execute("UPDATE incidents SET title=%s WHERE incident_id=%s",
                    (_incident_title(r["title"], r["zone"], r["n"]), r["incident_id"]))


def set_status(cur, incident_id, new_status):
    """Move an incident along open -> acknowledged -> resolved. Raises ValueError for anything else."""
    new_status = da.enum_to_db(new_status)
    cur.execute("SELECT status FROM incidents WHERE incident_id=%s FOR UPDATE", (incident_id,))
    row = cur.fetchone()
    if not row:
        raise LookupError(f"incident {incident_id} not found")
    if new_status not in TRANSITIONS[row["status"]]:
        raise ValueError(f"cannot move incident {incident_id} from {row['status']} to {new_status}")
    if new_status == "acknowledged":
        cur.execute("UPDATE incidents SET status='acknowledged', acknowledged_ts=now() WHERE incident_id=%s",
                    (incident_id,))
        cur.execute("UPDATE alerts SET status='acknowledged' WHERE incident_id=%s AND status='open'", (incident_id,))
    else:
        cur.execute("UPDATE incidents SET status='resolved', resolved_ts=now() WHERE incident_id=%s", (incident_id,))
        cur.execute("UPDATE alerts SET status='resolved', resolved_ts=now() WHERE incident_id=%s AND status<>'resolved'",
                    (incident_id,))
    return new_status


def _alerts_of(cur, incident_id):
    cur.execute("SELECT alert_id FROM alerts WHERE incident_id=%s ORDER BY first_ts, alert_id", (incident_id,))
    return [r["alert_id"] for r in cur.fetchall()]


def recommended_actions(cur, alert_ids):
    """Playbook actions of all alerts, merged by action id (best priority kept). Never produced by the model."""
    merged = {}
    for aid in alert_ids:
        alert = da.get_alert(cur, aid)
        for r in recs.recommend(alert)["recommendations"]:
            cur_best = merged.get(r["action_id"])
            if cur_best is None or r["priority"] < cur_best["priority"]:
                merged[r["action_id"]] = {"id": r["action_id"], "priority": r["priority"], "text": r["text"],
                                          "alert_ids": []}
            merged[r["action_id"]]["alert_ids"].append(aid)
    return sorted(merged.values(), key=lambda x: (x["priority"], x["id"]))


def list_incidents(cur, status=None, severity=None):
    """Ranked list: severity, then open before acknowledged before resolved, then most recent."""
    cur.execute("""SELECT i.incident_id AS id, i.title, i.severity, i.status, i.first_ts, i.last_ts, i.grouping_rule,
                          i.location_id, l.name AS zone, b.name AS building,
                          array_agg(al.alert_id ORDER BY al.first_ts) AS alert_ids
                   FROM incidents i JOIN locations l USING (location_id) JOIN buildings b USING (building_id)
                   LEFT JOIN alerts al USING (incident_id)
                   WHERE (%s::text IS NULL OR i.status=%s) AND (%s::text IS NULL OR i.severity=%s)
                   GROUP BY i.incident_id, l.name, b.name""",
                (da.enum_to_db(status), da.enum_to_db(status), da.enum_to_db(severity), da.enum_to_db(severity)))
    rows = [dict(r) for r in cur.fetchall()]
    order = {"open": 0, "acknowledged": 1, "resolved": 2}
    rows.sort(key=lambda r: (-SEVERITY_RANK[r["severity"]], order[r["status"]], -r["last_ts"].timestamp()))
    for n, r in enumerate(rows, 1):
        r["rank"] = n
    return rows


def get_incident(cur, incident_id):
    rows = [r for r in list_incidents(cur) if r["id"] == incident_id]
    if not rows:
        return None
    inc = rows[0]
    inc["alert_ids"] = [a for a in (inc["alert_ids"] or []) if a is not None]
    inc["recommended_actions"] = recommended_actions(cur, inc["alert_ids"])
    return inc

"""
db_adapter.py - the ONLY place where the Day-2 engine touches the unified schema (migrations/001).

  load_context()   telemetry_records + event_records -> the event dicts the alert rules read
  store_alerts()   engine alerts -> alerts table (idempotent: an alert is identified by rule, asset, first_ts)
  get_alert() / get_asset() / save_explanation()   read and write what explain/validate need

Naming translation (DATA_MODEL.md "Names"): NETWORKING->NETWORK, ACCESS_CONTROL->ACCESS, lowercase enums in the
database -> UPPERCASE in the engine/API, locations.name -> zone. When Amr's db_map.py is merged, replace the maps
below with imports from it; nothing else in the engine changes.
"""
from datetime import timezone

from psycopg2.extras import Json

SYSTEM_TO_API = {"HVAC": "HVAC", "NETWORKING": "NETWORK", "ACCESS_CONTROL": "ACCESS",
                 "CCTV": "CCTV", "FIRE_ALARM": "FIRE_ALARM"}

# event_types.name -> (engine event type, unit). hvac_fault / hvac_recovered / heartbeat are not used by any rule.
EVENT_MAP = {
    "door_forced": ("DOOR_FORCED", None), "door_held_open": ("DOOR_HELD_OPEN", "seconds"),
    "access_denied": ("ACCESS_DENIED", None), "link_down": ("LINK_DOWN", None), "link_up": ("LINK_UP", None),
    "camera_offline": ("CAMERA_OFFLINE", None), "camera_online": ("CAMERA_ONLINE", None),
    "fire_alarm_triggered": ("SMOKE_DETECTED", None), "alarm_cleared": ("ALARM_RESET", None),
}
# details keys renamed on the way in (the seed writes "badge", the rules read "card_id")
DETAIL_RENAME = {"badge": "card_id"}

# metric name -> (engine event type, unit); only the metrics a rule reads
METRIC_MAP = {"temperature": ("TEMPERATURE_READING", "°C"), "bandwidth_util": ("BANDWIDTH_USAGE", "%")}

TELEMETRY_ID_OFFSET = 10 ** 12   # keeps engine-internal ids of the two fact tables apart; record_id holds the real id


def enum_to_api(v):
    return v.upper() if isinstance(v, str) else v


def enum_to_db(v):
    return v.lower() if isinstance(v, str) else v


def _utc(ts):
    return ts.astimezone(timezone.utc)


def load_context(cur, end, history_days=7):
    from datetime import timedelta
    base = end - timedelta(days=history_days)
    cur.execute("""SELECT a.asset_id, a.name, a.asset_code, s.code AS system, a.location_id, l.name AS zone
                   FROM assets a JOIN system_types s USING (system_type_id)
                   JOIN locations l USING (location_id)""")
    assets = {r["asset_id"]: {"id": r["asset_id"], "name": r["name"], "asset_code": r["asset_code"],
                              "system_type": SYSTEM_TO_API[r["system"]], "location_id": r["location_id"],
                              "zone": r["zone"]} for r in cur.fetchall()}
    ev = {aid: [] for aid in assets}

    cur.execute("""SELECT e.event_id, e.asset_id, e.ts, t.name AS type, e.severity, e.details
                   FROM event_records e JOIN event_types t USING (event_type_id)
                   WHERE e.ts > %s AND e.ts <= %s ORDER BY e.ts, e.event_id""", (base, end))
    for r in cur.fetchall():
        if r["type"] not in EVENT_MAP:
            continue
        etype, unit = EVENT_MAP[r["type"]]
        raw = r["details"] or {}
        details = {DETAIL_RENAME.get(k, k): v for k, v in raw.items()}
        value = None
        if etype == "DOOR_HELD_OPEN" and raw.get("seconds_open") is not None:
            value = float(raw["seconds_open"])
        ev[r["asset_id"]].append({"id": r["event_id"], "source": "event", "record_id": r["event_id"],
                                  "asset_id": r["asset_id"], "ts": _utc(r["ts"]), "event_type": etype,
                                  "severity": enum_to_api(r["severity"]), "value": value, "unit": unit,
                                  "details": details})

    cur.execute("""SELECT t.telemetry_id, t.asset_id, t.ts, m.name AS metric, t.value
                   FROM telemetry_records t JOIN metric_types m USING (metric_id)
                   WHERE t.ts > %s AND t.ts <= %s AND m.name IN ('temperature','bandwidth_util','packet_loss')
                   ORDER BY t.ts, t.telemetry_id""", (base, end))
    loss = {}
    rows = cur.fetchall()
    for r in rows:
        if r["metric"] == "packet_loss":
            loss[(r["asset_id"], r["ts"])] = r["value"]
    for r in rows:
        if r["metric"] not in METRIC_MAP:
            continue
        etype, unit = METRIC_MAP[r["metric"]]
        details = {}
        if r["metric"] == "bandwidth_util" and (r["asset_id"], r["ts"]) in loss:
            details["packet_loss_pct"] = loss[(r["asset_id"], r["ts"])]   # same asset and timestamp
        ev[r["asset_id"]].append({"id": TELEMETRY_ID_OFFSET + r["telemetry_id"], "source": "telemetry",
                                  "record_id": r["telemetry_id"], "asset_id": r["asset_id"],
                                  "ts": _utc(r["ts"]), "event_type": etype, "severity": "INFO",
                                  "value": r["value"], "unit": unit, "details": details})
    for aid in ev:
        ev[aid].sort(key=lambda e: (e["ts"], e["id"]))
    return {"assets": assets, "ev": ev, "end": _utc(end)}


def latest_ts(cur):
    cur.execute("""SELECT GREATEST((SELECT max(ts) FROM telemetry_records), (SELECT max(ts) FROM event_records)) AS t""")
    return cur.fetchone()["t"]


def store_alerts(cur, alerts):
    """Insert alerts that are not stored yet. Returns the ids of the new ones (re-running changes nothing)."""
    new = []
    for a in alerts:
        cur.execute("""INSERT INTO alerts (rule_id, detection_method, severity, asset_id, title, summary,
                                           first_ts, last_ts, detected_ts, evidence)
                       SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                       WHERE NOT EXISTS (SELECT 1 FROM alerts WHERE rule_id=%s AND asset_id=%s AND first_ts=%s)
                       RETURNING alert_id""",
                    (a["rule_id"], enum_to_db(a["detection_method"]), enum_to_db(a["severity"]), a["asset_id"],
                     a["title"], a["summary"], a["first_ts"], a["last_ts"], a["detected_ts"], Json(a["evidence"]),
                     a["rule_id"], a["asset_id"], a["first_ts"]))
        row = cur.fetchone()
        if row:
            new.append(row["alert_id"])
    return new


def get_asset(cur, asset_id):
    cur.execute("""SELECT a.asset_id AS id, a.name, s.code AS system, a.location_id, l.name AS zone
                   FROM assets a JOIN system_types s USING (system_type_id) JOIN locations l USING (location_id)
                   WHERE a.asset_id=%s""", (asset_id,))
    r = cur.fetchone()
    if not r:
        return None
    return {"id": r["id"], "name": r["name"], "system_type": SYSTEM_TO_API[r["system"]],
            "location_id": r["location_id"], "zone": r["zone"]}


def get_alert(cur, alert_id):
    cur.execute("""SELECT alert_id AS id, rule_id, detection_method, severity, asset_id, title, summary, first_ts,
                          last_ts, detected_ts, evidence, status, incident_id, explanation, explanation_source
                   FROM alerts WHERE alert_id=%s""", (alert_id,))
    r = cur.fetchone()
    if not r:
        return None
    r = dict(r)
    for k in ("severity", "detection_method", "status"):
        r[k] = enum_to_api(r[k])
    return r


def save_explanation(cur, alert_id, response):
    """Store the contract response. ai_source 'rules' (no evidence, nothing explained) is not stored."""
    source = {"llm": "ollama", "fallback": "fallback"}.get(response.get("ai_source"))
    if source is None:
        return False
    cur.execute("""UPDATE alerts SET explanation=%s, explanation_source=%s, explained_at=now()
                   WHERE alert_id=%s""", (Json(response), source, alert_id))
    return True

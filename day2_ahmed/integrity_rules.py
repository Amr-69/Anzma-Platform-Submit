"""
integrity_rules.py  (Day 2 - Ahmed: data-integrity rules)

Single source of truth for what is allowed to be stored/interpreted for each kind of equipment.
Pure Python (no database) so it can be unit-tested and then translated into constraints by the
data-access/schema owner (see compat_sql()).

Three different questions are kept apart on purpose:
  1. HISTORICAL EVENT   - an immutable row that says "this was recorded at time T".
  2. CURRENT CONDITION  - derived: an opener event (LINK_DOWN, CAMERA_OFFLINE, SMOKE_DETECTED)
                          with no later closer for the same asset. Never read from event counts.
  3. ACTIVE INCIDENT    - a managed record built from alerts (lifecycle: OPEN/ACKNOWLEDGED/RESOLVED).
This module covers 1 and 2 and the compatibility rules; incidents live in the Incident table.
"""

# event_class: MEASUREMENT (periodic reading), FAULT (abnormal report), STATE_OPEN / STATE_CLOSE
# (a condition starts / ends), HEARTBEAT, ACTIVITY (normal operational activity).
EVENT_CATALOG = {
    "HVAC": {
        "TEMPERATURE_READING": ("MEASUREMENT", "°C"),
        "HIGH_TEMPERATURE":    ("FAULT", "°C"),
        "FILTER_FAULT":        ("FAULT", None),
    },
    "NETWORK": {
        "BANDWIDTH_USAGE": ("MEASUREMENT", "Mbps"),
        "UPTIME_REPORT":   ("HEARTBEAT", "seconds"),
        "LINK_DOWN":       ("STATE_OPEN", None),
        "LINK_UP":         ("STATE_CLOSE", None),
    },
    "ACCESS": {
        "ACCESS_GRANTED": ("ACTIVITY", None),
        "ACCESS_DENIED":  ("ACTIVITY", None),
        "DOOR_HELD_OPEN": ("FAULT", "seconds"),
        "DOOR_FORCED":    ("FAULT", None),
    },
    "CCTV": {
        "MOTION_DETECTED": ("ACTIVITY", "%"),
        "CAMERA_OFFLINE":  ("STATE_OPEN", None),
        "CAMERA_ONLINE":   ("STATE_CLOSE", None),
    },
    "FIRE_ALARM": {
        "PANEL_HEARTBEAT": ("HEARTBEAT", None),
        "SMOKE_DETECTED":  ("STATE_OPEN", None),
        "ALARM_RESET":     ("STATE_CLOSE", None),
        "BATTERY_LOW":     ("FAULT", "%"),
    },
}

# opener -> closer. A condition is "current" only while its opener has no later closer.
CLOSERS = {"LINK_DOWN": "LINK_UP", "CAMERA_OFFLINE": "CAMERA_ONLINE", "SMOKE_DETECTED": "ALARM_RESET"}

# which asset type each alert rule may be raised on (SEC_ rules use CCTV as the anchor asset and
# read ACCESS events of the same location as supporting evidence - the only cross-system rule family)
RULE_PREFIX_SYSTEM = {"HVAC_": "HVAC", "NET_": "NETWORK", "ACC_": "ACCESS", "CCTV_": "CCTV",
                      "FIRE_": "FIRE_ALARM", "SEC_": "CCTV"}


class IntegrityError(ValueError):
    pass


def allowed_event_types(system_type):
    return set(EVENT_CATALOG.get(system_type, {}))


def event_class(system_type, event_type):
    entry = EVENT_CATALOG.get(system_type, {}).get(event_type)
    return entry[0] if entry else None


def check_event(system_type, event_type, unit=None):
    """Return a list of integrity violations for one event on one asset type (empty = valid)."""
    if system_type not in EVENT_CATALOG:
        return [f"unknown system type {system_type!r}"]
    entry = EVENT_CATALOG[system_type].get(event_type)
    if entry is None:
        return [f"event type {event_type} is not valid for {system_type} equipment"]
    expected = entry[1]
    if unit is not None and expected is not None and unit != expected:
        return [f"{event_type} must be recorded in {expected}, got {unit!r}"]
    return []


def check_rule_target(rule_id, system_type):
    """An alert rule may only be raised on the asset type it was designed for."""
    for prefix, system in RULE_PREFIX_SYSTEM.items():
        if rule_id.startswith(prefix):
            return [] if system == system_type else [f"rule {rule_id} cannot be raised on {system_type} equipment"]
    return [f"rule {rule_id} has no known system family"]


def check_evidence_event(alert_system_type, rule_id, event_system_type, event_type):
    """Evidence attached to an alert must be compatible with the asset (or the one allowed cross-system link)."""
    if event_system_type == alert_system_type:
        return check_event(event_system_type, event_type)
    if rule_id.startswith("SEC_") and alert_system_type == "CCTV" and event_system_type == "ACCESS":
        return check_event("ACCESS", event_type)
    return [f"{event_system_type} event {event_type} cannot be evidence for a {alert_system_type} alert ({rule_id})"]


def current_conditions(events):
    """
    Derive the CURRENT open conditions for ONE asset from its historical events.
    events: iterable of dicts with at least ts, event_type (any order). Returns a list of the opener
    events that have no later closer. Counts of past events never make a condition current.
    """
    evs = sorted(events, key=lambda e: e["ts"])
    open_now = []
    for i, e in enumerate(evs):
        closer = CLOSERS.get(e["event_type"])
        if closer and not any(x["event_type"] == closer and x["ts"] > e["ts"] for x in evs[i + 1:]):
            open_now.append(e)
    return open_now


def compat_rows():
    """(system_type, event_type, event_class, expected_unit) rows for the compatibility table."""
    return [(s, t, c, u) for s, evs in EVENT_CATALOG.items() for t, (c, u) in evs.items()]


def compat_sql(table="event_type_compat"):
    """
    SQL for the compatibility table. The schema owner turns it into a real constraint, e.g. a composite
    FK from events(system_type, event_type) -> event_type_compat, or a trigger on INSERT. Table and column
    names are PROVISIONAL until aligned with DATA_MODEL.md / the agreed schema.
    """
    lines = [f"CREATE TABLE IF NOT EXISTS {table} (",
             "    system_type    VARCHAR(20) NOT NULL,",
             "    event_type     VARCHAR(50) NOT NULL,",
             "    event_class    VARCHAR(20) NOT NULL,",
             "    expected_unit  VARCHAR(20),",
             "    PRIMARY KEY (system_type, event_type)",
             ");",
             f"INSERT INTO {table} (system_type, event_type, event_class, expected_unit) VALUES"]
    vals = []
    for s, t, c, u in compat_rows():
        unit = "NULL" if u is None else "'" + u.replace("'", "''") + "'"
        vals.append(f"    ('{s}', '{t}', '{c}', {unit})")
    lines.append(",\n".join(vals) + "\nON CONFLICT DO NOTHING;")
    return "\n".join(lines)


if __name__ == "__main__":
    print(compat_sql())


# ---------------------------------------------------------------------------------------------------------------
# Unified-schema checks (anzma_schema_and_seed.sql): the database holds the compatibility tables but does NOT enforce them on
# INSERT (DATA_MODEL.md "Rules NOT enforced"). These two functions are what ingestion calls and what the tests use.
# ---------------------------------------------------------------------------------------------------------------
def check_record_db(cur, asset_id, kind, name):
    """kind: 'metric' or 'event'; name: metric_types.name / event_types.name. -> list of violations (empty = valid)."""
    table, col, ref = (("metric_type_system_compat", "metric_id", "metric_types")
                       if kind == "metric" else ("event_type_system_compat", "event_type_id", "event_types"))
    cur.execute("SELECT system_type_id FROM assets WHERE asset_id=%s", (asset_id,))
    asset = cur.fetchone()
    if not asset:
        return [f"unknown asset {asset_id}"]
    cur.execute(f"SELECT {col} AS id FROM {ref} WHERE name=%s", (name,))
    item = cur.fetchone()
    if not item:
        return [f"unknown {kind} type {name!r}"]
    cur.execute(f"SELECT 1 FROM {table} WHERE {col}=%s AND system_type_id=%s", (item["id"], asset["system_type_id"]))
    if cur.fetchone():
        return []
    cur.execute("SELECT code FROM system_types WHERE system_type_id=%s", (asset["system_type_id"],))
    return [f"{kind} {name} is not valid for {cur.fetchone()['code']} equipment"]


def stored_violations(cur):
    """Rows already stored that break the compatibility tables -> {'telemetry': n, 'events': n}."""
    cur.execute("""SELECT count(*) AS n FROM telemetry_records t JOIN assets a USING (asset_id)
                   LEFT JOIN metric_type_system_compat c
                          ON c.metric_id=t.metric_id AND c.system_type_id=a.system_type_id
                   WHERE c.metric_id IS NULL""")
    tel = cur.fetchone()["n"]
    cur.execute("""SELECT count(*) AS n FROM event_records e JOIN assets a USING (asset_id)
                   LEFT JOIN event_type_system_compat c
                          ON c.event_type_id=e.event_type_id AND c.system_type_id=a.system_type_id
                   WHERE c.event_type_id IS NULL""")
    return {"telemetry": tel, "events": cur.fetchone()["n"]}

"""
Endpoints the dashboard page (static/dashboard.html) calls, built on the unified PostgreSQL schema.

  GET /dashboard/overview          system tiles + KPI cards            (page: "System status", "Key indicators")
  GET /dashboard/trends            time series for the four charts     (page: "Trends")
  GET /dashboard/equipment-health  one row per asset                   (page: "Equipment health")
  GET /alerts                      stored alerts, newest first         (page: "Alert center")
  GET /alerts/{id}                 one alert with its evidence rows

Rules of this module
  * Read-only SQL on the real tables. Nothing is invented: a metric that is not stored (e.g. access_granted) is returned
    as null and the page hides it.
  * The KPI numbers come from app.dashboard_core_pg (the existing KPI logic) so there is one definition of each KPI.
  * "Now" is the latest stored record (as_of), not the wall clock, exactly like the Day 2 pipeline.
  * Database codes are translated to the names the page uses in ONE place (SYSTEM_TO_UI / SYSTEM_FROM_UI).
"""
import math
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Query
from psycopg import OperationalError

from app import dashboard_core_pg as core
from reference_data import SYSTEM_CODES

# database system code <-> name used by the page / API contract
SYSTEM_TO_UI = {"HVAC": "HVAC", "NETWORKING": "NETWORK", "ACCESS_CONTROL": "ACCESS", "CCTV": "CCTV",
                "FIRE_ALARM": "FIRE_ALARM"}
SYSTEM_FROM_UI = {v: k for k, v in SYSTEM_TO_UI.items()} | {k: k for k in SYSTEM_TO_UI}

# event types that mean "something is wrong" (used for the Faults column); heartbeat is routine and never counted
FAULT_EVENTS = ("hvac_fault", "door_forced", "door_held_open", "link_down", "camera_offline", "fire_alarm_triggered")
NOT_COUNTED_EVENTS = ("heartbeat",)

MAX_HOURS = 168


def _system(value):
    """Accept the page's names (NETWORK, ACCESS) or the database codes (NETWORKING, ACCESS_CONTROL)."""
    if value in (None, ""):
        return None
    code = SYSTEM_FROM_UI.get(value.upper())
    if code is None:
        raise HTTPException(400, f"Unknown system_type. Allowed: {', '.join(SYSTEM_TO_UI.values())}.")
    return code


def _window(conn, hours):
    """[start, end_exclusive) of `hours` hours ending at the latest stored record."""
    if not 1 <= hours <= MAX_HOURS:
        raise HTTPException(400, f"hours must be between 1 and {MAX_HOURS}.")
    first, last = core._data_extent(conn)                     # raises NoDataError when the tables are empty
    end = last + timedelta(seconds=1)              # exclusive upper bound, so the latest record is inside the window
    return last - timedelta(hours=hours), end, last


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def _bucket_minutes(hours):
    """About 24 points per chart, never finer than the 15-minute telemetry cadence."""
    return max(15, math.ceil(hours * 60 / 24 / 15) * 15)


def _num(value, digits=2):
    return None if value is None else round(float(value), digits)


ALERT_SQL = """
    SELECT al.alert_id AS id, al.rule_id, al.detection_method, al.severity, al.status, al.title, al.summary,
           al.first_ts, al.last_ts, al.detected_ts, al.incident_id, al.asset_id,
           a.name AS asset_name, st.code AS system, l.name AS zone
    FROM alerts al JOIN assets a USING (asset_id) JOIN system_types st USING (system_type_id)
    JOIN locations l USING (location_id)"""

def _alert(r):
    return {"id": r["id"], "rule_id": r["rule_id"], "detection_method": r["detection_method"].upper(),
            "severity": r["severity"].upper(), "status": r["status"].upper(), "title": r["title"],
            "summary": r["summary"], "first_ts": _iso(r["first_ts"]), "last_ts": _iso(r["last_ts"]),
            "detected_ts": _iso(r["detected_ts"]), "incident_id": r["incident_id"],
            "asset_id": r["asset_id"], "asset_name": r["asset_name"], "zone": r["zone"],
            "system_type": SYSTEM_TO_UI[r["system"]]}



def alerts_after(conn, last_id):
    """Alerts stored after `last_id`, oldest first (used to push new alerts to connected pages)."""
    rows = conn.execute(ALERT_SQL + " WHERE al.alert_id > %s ORDER BY al.alert_id", (last_id,)).fetchall()
    return [_alert(r) for r in rows]


def latest_alert_id(conn):
    return conn.execute("SELECT COALESCE(MAX(alert_id), 0) AS m FROM alerts").fetchone()["m"]



def create_router(open_ro=core.open_readonly):
    router = APIRouter(tags=["Dashboard page"])

    # ------------------------------------------------------------------ overview
    @router.get("/dashboard/overview", summary="System tiles and KPI cards")
    def overview(hours: int = Query(24), system_type: str | None = Query(None)):
        code = _system(system_type)
        try:
            with open_ro() as conn:
                start, end, last = _window(conn, hours)
                # tiles: always the five systems, so the page can offer them as filters
                systems = []
                for sys_code in SYSTEM_CODES:
                    params = {"system": sys_code, "building": None, "start": start, "end": end}
                    n = core._monitored_assets(conn, params)
                    c = core._asset_status_counts(conn, params)
                    if n == 0 or c["unknown"] == n:
                        status = "UNKNOWN"
                    elif c["critical"]:
                        status = "CRITICAL"
                    elif c["warning"] or c["no_data"] or c["unknown"]:
                        status = "WARNING"
                    else:
                        status = "OK"
                    systems.append({"type": SYSTEM_TO_UI[sys_code], "status": status, "assets": n, "ok": c["normal"],
                                    "warning": c["warning"], "critical": c["critical"], "no_data": c["no_data"],
                                    "unknown": c["unknown"]})

                summary = core.get_dashboard_summary(conn, system_type=code, start_time=core.format_ts(start),
                                                     end_time=core.format_ts(end))
                k = summary["kpis"]
                d = core.KPI_DEFINITIONS
                sc = k["healthy_assets_pct"]["status_counts"]

                def card(key, sub=None):
                    return {"key": key, "label": d[key]["name"], "value": k[key]["value"], "unit": d[key]["unit"],
                            "definition": d[key]["definition"], "sub": sub}

                kpis = [
                    card("monitored_assets"),
                    card("healthy_assets_pct", f"{sc['normal']} normal, {sc['warning']} warning, "
                                               f"{sc['critical']} critical, {sc['no_data']} no data, "
                                               f"{sc['unknown']} unknown"),
                    card("alerts_raised", "warning and critical events"),
                    card("critical_alerts"),
                    card("alert_rate_per_hour"),
                    card("avg_zone_temp"),
                    card("avg_packet_loss"),
                    card("heartbeat_compliance", f"{k['heartbeat_compliance']['received']} of "
                                                 f"{k['heartbeat_compliance']['expected']} expected"),
                ]
                inc = conn.execute("""SELECT count(*) FILTER (WHERE status <> 'resolved') AS open_n,
                                             count(*) FILTER (WHERE status <> 'resolved' AND severity='critical') AS crit_n
                                      FROM incidents""").fetchone()
                kpis.append({"key": "open_incidents", "label": "Open incidents", "value": inc["open_n"],
                             "unit": "incidents", "sub": f"{inc['crit_n']} critical",
                             "definition": "Incidents that are not resolved (open or acknowledged), whatever the "
                                           "selected window or system. An incident groups related alerts."})
        except core.NoDataError as exc:
            raise HTTPException(503, str(exc))
        except core.DashboardError as exc:
            raise HTTPException(400, str(exc))
        except OperationalError as exc:
            raise HTTPException(503, f"Database unavailable: {exc}")
        return {"window": {"from": _iso(start), "to": _iso(last), "hours": hours, "as_of": _iso(last),
                           "reference": "latest stored record"},
                "systems": systems, "kpis": kpis}

    # ------------------------------------------------------------------ trends
    @router.get("/dashboard/trends", summary="Time series for the charts")
    def trends(hours: int = Query(24), system_type: str | None = Query(None)):
        code = _system(system_type)
        try:
            with open_ro() as conn:
                start, end, last = _window(conn, hours)
                size = _bucket_minutes(hours)
                n_buckets = math.ceil(hours * 60 / size)
                buckets = [start + timedelta(minutes=size * i) for i in range(n_buckets)]
                p = {"start": start, "end": end, "size": size * 60.0, "system": code}
                idx = "floor(extract(epoch FROM (x.ts - %(start)s)) / %(size)s)::int"

                ev = conn.execute(f"""
                    SELECT {idx} AS b, st.code AS system, count(*) AS n
                    FROM event_records x JOIN event_types et USING (event_type_id)
                    JOIN assets a USING (asset_id) JOIN system_types st USING (system_type_id)
                    WHERE x.ts >= %(start)s AND x.ts < %(end)s AND et.name <> ALL(%(skip)s)
                      AND (%(system)s::text IS NULL OR st.code = %(system)s::text)
                    GROUP BY 1, 2""", dict(p, skip=list(NOT_COUNTED_EVENTS))).fetchall()
                by_system = {SYSTEM_TO_UI[c]: [0] * n_buckets for c in SYSTEM_CODES if code in (None, c)}
                for r in ev:
                    if 0 <= r["b"] < n_buckets:
                        by_system[SYSTEM_TO_UI[r["system"]]][r["b"]] += r["n"]

                def series(metric, system_code):
                    rows = conn.execute(f"""
                        SELECT {idx} AS b, avg(x.value) AS avg_v, max(x.value) AS max_v
                        FROM telemetry_records x JOIN metric_types m USING (metric_id)
                        JOIN assets a USING (asset_id) JOIN system_types st USING (system_type_id)
                        WHERE x.ts >= %(start)s AND x.ts < %(end)s AND m.name = %(metric)s AND st.code = %(sc)s
                        GROUP BY 1""", dict(p, metric=metric, sc=system_code)).fetchall()
                    avg, mx = [None] * n_buckets, [None] * n_buckets
                    for r in rows:
                        if 0 <= r["b"] < n_buckets:
                            avg[r["b"]], mx[r["b"]] = _num(r["avg_v"]), _num(r["max_v"])
                    return avg, mx

                hvac_avg, _ = series("temperature", "HVAC")
                bw_avg, bw_peak = series("bandwidth_util", "NETWORKING")
                denied = [0] * n_buckets
                for r in conn.execute(f"""
                        SELECT {idx} AS b, count(*) AS n FROM event_records x JOIN event_types et USING (event_type_id)
                        WHERE x.ts >= %(start)s AND x.ts < %(end)s AND et.name = 'access_denied' GROUP BY 1""", p):
                    if 0 <= r["b"] < n_buckets:
                        denied[r["b"]] = r["n"]
                top_rows = conn.execute("""
                    SELECT a.name, count(*) AS n
                    FROM event_records x JOIN event_types et USING (event_type_id)
                    JOIN assets a USING (asset_id) JOIN system_types st USING (system_type_id)
                    WHERE x.ts >= %(start)s AND x.ts < %(end)s AND et.name <> ALL(%(skip)s)
                      AND (%(system)s::text IS NULL OR st.code = %(system)s::text)
                    GROUP BY a.asset_id, a.name
                    ORDER BY n DESC, a.name
                    LIMIT 10""", dict(p, skip=list(NOT_COUNTED_EVENTS))).fetchall()
                top_assets = {"labels": [r["name"] for r in top_rows],
                              "values": [r["n"] for r in top_rows]}
                has_granted = conn.execute("SELECT 1 FROM event_types WHERE name = 'access_granted'").fetchone()
        except core.NoDataError as exc:
            raise HTTPException(503, str(exc))
        except OperationalError as exc:
            raise HTTPException(503, f"Database unavailable: {exc}")
        return {"window": {"from": _iso(start), "to": _iso(last), "hours": hours}, "bucket_minutes": size,
                "buckets": [_iso(b) for b in buckets], "events_by_system": by_system,
                "hvac_avg_temp": hvac_avg, "bandwidth_avg": bw_avg, "bandwidth_peak": bw_peak,
                "bandwidth_unit": "%",
                # not stored in this dataset: null, never zero (zero would claim that nobody was let in)
                "access_granted": None if not has_granted else [None] * n_buckets,
                "access_denied": denied,
                "top_assets": top_assets}
    # ------------------------------------------------------------------ equipment health
    @router.get("/dashboard/equipment-health", summary="One row per asset")
    def equipment_health(hours: int = Query(24), system_type: str | None = Query(None)):
        code = _system(system_type)
        try:
            with open_ro() as conn:
                start, end, last = _window(conn, hours)
                pair_values = ", ".join("('%s', '%s')" % pair for pair in core.FAULT_RECOVERY_PAIRS)
                paired_event_types = [event_type for pair in core.FAULT_RECOVERY_PAIRS for event_type in pair]
                rows = conn.execute(f"""
                    WITH scope AS (
                        SELECT a.asset_id, a.name, st.code AS system, b.name AS building, l.floor, l.name AS zone
                        FROM assets a JOIN system_types st USING (system_type_id)
                        JOIN locations l USING (location_id) JOIN buildings b USING (building_id)
                        WHERE (%(system)s::text IS NULL OR st.code = %(system)s::text)),
                    fault_pairs(fault_type, recovery_type) AS (VALUES {pair_values}),
                    latest_fault_state AS (
                        SELECT DISTINCT ON (x.asset_id, fp.fault_type)
                               x.asset_id, fp.fault_type, et.name AS event_type, x.severity
                        FROM event_records x JOIN event_types et USING (event_type_id)
                        JOIN fault_pairs fp ON et.name IN (fp.fault_type, fp.recovery_type)
                        WHERE x.ts < %(end)s
                        ORDER BY x.asset_id, fp.fault_type, x.ts DESC, x.event_id DESC),
                    active_pair_criticals AS (
                        SELECT asset_id, count(*) AS n FROM latest_fault_state
                        WHERE event_type = fault_type AND severity = 'critical' GROUP BY asset_id),
                    ev AS (
                        SELECT x.asset_id,
                               count(*) FILTER (WHERE et.name <> ALL(%(skip)s)) AS events,
                               count(*) FILTER (WHERE et.name = ANY(%(faults)s)) AS faults,
                               count(*) FILTER (WHERE x.severity = 'warning') AS warnings,
                               count(*) FILTER (WHERE x.severity = 'critical') AS criticals,
                               count(*) FILTER (WHERE x.severity = 'critical' AND et.name <> ALL(%(paired_types)s)) AS point_criticals,
                               count(*) FILTER (WHERE et.name = 'heartbeat') AS heartbeats
                        FROM event_records x JOIN event_types et USING (event_type_id)
                        WHERE x.ts >= %(start)s AND x.ts < %(end)s GROUP BY x.asset_id),
                    tel AS (SELECT asset_id, count(*) AS n FROM telemetry_records
                            WHERE ts >= %(start)s AND ts < %(end)s GROUP BY asset_id),
                    ever AS (SELECT asset_id FROM telemetry_records GROUP BY asset_id
                             UNION SELECT asset_id FROM event_records GROUP BY asset_id),
                    seen AS (SELECT asset_id, max(ts) AS last_seen FROM (
                                 SELECT asset_id, ts FROM telemetry_records WHERE ts < %(end)s
                                 UNION ALL SELECT asset_id, ts FROM event_records WHERE ts < %(end)s) u
                             GROUP BY asset_id)
                    SELECT s.*, COALESCE(e.events,0) AS events, COALESCE(e.faults,0) AS faults,
                           COALESCE(e.warnings,0) AS warnings, COALESCE(e.criticals,0) AS criticals,
                           COALESCE(e.point_criticals,0) + COALESCE(ap.n,0) AS active_criticals,
                           COALESCE(e.heartbeats,0) AS heartbeats, COALESCE(t.n,0) AS telemetry,
                           (s.asset_id IN (SELECT asset_id FROM ever)) AS reports, seen.last_seen
                    FROM scope s LEFT JOIN ev e USING (asset_id) LEFT JOIN tel t USING (asset_id)
                    LEFT JOIN active_pair_criticals ap USING (asset_id) LEFT JOIN seen USING (asset_id)
                    ORDER BY s.system, s.name""",
                                    {"system": code, "start": start, "end": end, "skip": list(NOT_COUNTED_EVENTS),
                                     "paired_types": paired_event_types,
                                     "faults": list(FAULT_EVENTS)}).fetchall()
        except core.NoDataError as exc:
            raise HTTPException(503, str(exc))
        except OperationalError as exc:
            raise HTTPException(503, f"Database unavailable: {exc}")

        items = []
        for r in rows:
            # Match dashboard status semantics: critical > no data/warning > unknown > normal.
            if r["active_criticals"]:
                status, reason = "CRITICAL", f"{r['active_criticals']} active critical condition(s)"
            elif r["reports"] and r["telemetry"] == 0 and r["heartbeats"] == 0:
                status, reason = "WARNING", "No telemetry or heartbeat received in the window"
            elif r["warnings"]:
                status, reason = "WARNING", f"{r['warnings']} warning event(s) in the window"
            elif not r["reports"]:
                status, reason = "UNKNOWN", "No telemetry or events have ever been recorded"
            else:
                status, reason = "OK", "No warning or critical events"
            items.append({"asset_id": r["asset_id"], "name": r["name"], "system_type": SYSTEM_TO_UI[r["system"]],
                          "building": r["building"], "floor": r["floor"], "zone": r["zone"], "status": status,
                          "reason": reason, "events": r["events"], "faults": r["faults"], "warnings": r["warnings"],
                          "criticals": r["criticals"], "active_criticals": r["active_criticals"],
                          "last_seen": _iso(r["last_seen"]) if r["last_seen"] else None})
        rank = {"CRITICAL": 0, "WARNING": 1, "UNKNOWN": 2, "OK": 3}
        items.sort(key=lambda i: (rank[i["status"]], i["system_type"], i["name"]))
        return {"window": {"from": _iso(start), "to": _iso(last), "hours": hours}, "items": items}

    # ------------------------------------------------------------------ alerts
    @router.get("/alerts", summary="Stored alerts, newest first")
    def list_alerts(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                    severity: str | None = Query(None), system_type: str | None = Query(None),
                    status: str | None = Query(None)):
        code = _system(system_type)
        if severity and severity.lower() not in ("info", "warning", "critical"):
            raise HTTPException(400, "severity must be INFO, WARNING or CRITICAL.")
        if status and status.lower() not in ("open", "acknowledged", "resolved"):
            raise HTTPException(400, "status must be OPEN, ACKNOWLEDGED or RESOLVED.")
        where = """WHERE (%(sev)s::text IS NULL OR al.severity = %(sev)s::text)
                     AND (%(sys)s::text IS NULL OR st.code = %(sys)s::text)
                     AND (%(st)s::text IS NULL OR al.status = %(st)s::text)"""
        p = {"sev": severity.lower() if severity else None, "sys": code, "st": status.lower() if status else None,
             "limit": limit, "offset": offset}
        try:
            with open_ro() as conn:
                total = conn.execute(f"""SELECT count(*) AS n FROM alerts al JOIN assets a USING (asset_id)
                                         JOIN system_types st USING (system_type_id) {where}""", p).fetchone()["n"]
                rows = conn.execute(ALERT_SQL + f" {where} ORDER BY al.first_ts DESC, al.alert_id DESC "
                                                "LIMIT %(limit)s OFFSET %(offset)s", p).fetchall()
        except OperationalError as exc:
            raise HTTPException(503, f"Database unavailable: {exc}")
        return {"count": total, "limit": limit, "offset": offset, "items": [_alert(r) for r in rows]}

    @router.get("/alerts/{alert_id}", summary="One alert with its stored evidence")
    def get_alert(alert_id: int):
        try:
            with open_ro() as conn:
                r = conn.execute(ALERT_SQL + " WHERE al.alert_id = %s", (alert_id,)).fetchone()
                if r is None:
                    raise HTTPException(404, "Alert not found")
                ev = conn.execute("SELECT evidence, explanation, explanation_source FROM alerts WHERE alert_id = %s",
                                  (alert_id,)).fetchone()
        except OperationalError as exc:
            raise HTTPException(503, f"Database unavailable: {exc}")
        return {**_alert(r), "evidence": ev["evidence"], "explanation": ev["explanation"],
                "explanation_source": ev["explanation_source"]}

    return router

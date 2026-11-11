"""
dashboard_api.py
KPI, trend and equipment-health endpoints for the operational dashboard.
Every figure is computed from the events table at request time.

Reference time: the dashboard treats the latest event timestamp in the
database as "now", so the selected window always contains data.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query

SystemType = Literal["HVAC", "NETWORK", "ACCESS", "CCTV", "FIRE_ALARM"]
SYSTEMS = ["HVAC", "NETWORK", "ACCESS", "CCTV", "FIRE_ALARM"]

STATUS_WINDOW_MIN = 60
TEMP_TYPES = ["TEMPERATURE_READING", "HIGH_TEMPERATURE"]

FAULT_TYPES = {
    "HVAC": ["HIGH_TEMPERATURE", "FILTER_FAULT"],
    "NETWORK": ["LINK_DOWN"],
    "ACCESS": ["DOOR_FORCED", "DOOR_HELD_OPEN"],
    "CCTV": ["CAMERA_OFFLINE"],
    "FIRE_ALARM": ["SMOKE_DETECTED", "BATTERY_LOW"],
}

OPENERS = {
    "LINK_DOWN": ("CRITICAL", "Link down"),
    "CAMERA_OFFLINE": ("WARNING", "Camera offline"),
    "SMOKE_DETECTED": ("CRITICAL", "Smoke alarm active"),
}
INCIDENT_TYPES = list(OPENERS) + ["LINK_UP", "CAMERA_ONLINE", "ALARM_RESET"]
STATUS_ORDER = {"CRITICAL": 0, "WARNING": 1, "OK": 2}
STATUS_BY_RANK = {v: k for k, v in STATUS_ORDER.items()}

KPI_DEFINITIONS = {
    "total_events": "Number of events recorded in the selected window.",
    "critical_alerts": "Number of events with severity CRITICAL in the selected window.",
    "network_availability": "100 x (1 - total link downtime / (window length x number of network assets)). "
                            "Downtime comes from the downtime_sec field of LINK_UP events.",
    "avg_bandwidth": "Mean of BANDWIDTH_USAGE values (Mbps) in the window; the peak value is shown below it.",
    "access_denial_rate": "100 x ACCESS_DENIED / (ACCESS_GRANTED + ACCESS_DENIED) in the window.",
    "avg_hvac_temp": "Mean of TEMPERATURE_READING and HIGH_TEMPERATURE values (degrees C) in the window; "
                     "the number of HIGH_TEMPERATURE events is shown below it.",
    "camera_offline": "Number of CAMERA_OFFLINE events in the window.",
    "active_fire_alarms": "Fire-alarm assets whose latest SMOKE_DETECTED / ALARM_RESET event is SMOKE_DETECTED "
                          "(current state, not limited to the window).",
}

AGG_SQL = """
    SELECT s.type AS system_type, e.event_type, e.severity,
           COUNT(*) AS n, AVG(e.value) AS avg_v, MAX(e.value) AS max_v
    FROM events e
    JOIN assets a  ON a.id = e.asset_id
    JOIN systems s ON s.id = a.system_id
    WHERE e.ts > %s AND e.ts <= %s
    GROUP BY s.type, e.event_type, e.severity
"""

DOWNTIME_SQL = """
    SELECT COALESCE(SUM(CASE WHEN e.details->>'downtime_sec' ~ '^[0-9]+(\\.[0-9]+)?$'
                             THEN (e.details->>'downtime_sec')::float END), 0) AS down
    FROM events e
    JOIN assets a  ON a.id = e.asset_id
    JOIN systems s ON s.id = a.system_id
    WHERE s.type = 'NETWORK' AND e.event_type = 'LINK_UP'
      AND e.ts > %s AND e.ts <= %s
"""

ACTIVE_FIRE_SQL = """
    SELECT COUNT(*) AS n FROM (
        SELECT DISTINCT ON (e.asset_id) e.asset_id, e.event_type
        FROM events e
        JOIN assets a  ON a.id = e.asset_id
        JOIN systems s ON s.id = a.system_id
        WHERE s.type = 'FIRE_ALARM'
          AND e.event_type IN ('SMOKE_DETECTED', 'ALARM_RESET')
          AND e.ts <= %s
        ORDER BY e.asset_id, e.ts DESC
    ) t WHERE t.event_type = 'SMOKE_DETECTED'
"""


def count(rows, types, severity=None):
    return sum(r["n"] for r in rows
               if r["event_type"] in types and (severity is None or r["severity"] == severity))


def weighted_avg(rows, types):
    sel = [r for r in rows if r["event_type"] in types and r["avg_v"] is not None]
    n = sum(r["n"] for r in sel)
    return sum(r["avg_v"] * r["n"] for r in sel) / n if n else None


def rnd(v, digits=2):
    return None if v is None else round(float(v), digits)


def kpi(key, label, value, unit, system, sub=None):
    return {"key": key, "label": label, "value": value, "unit": unit,
            "system": system, "sub": sub, "definition": KPI_DEFINITIONS[key]}


def create_router(db):
    router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

    def get_window(cur, hours):
        cur.execute("SELECT MAX(ts) AS m FROM events")
        end = cur.fetchone()["m"]
        if end is None:
            raise HTTPException(404, "no events in the database")
        return end - timedelta(hours=hours), end

    def window_info(start, end, hours):
        return {"from": start, "to": end, "hours": hours,
                "reference": "latest event timestamp in the database"}

    def asset_health(cur, start, end, system_type):
        recent_start = max(start, end - timedelta(minutes=STATUS_WINDOW_MIN))
        cur.execute("""
            SELECT a.id, a.name, a.model, s.type AS system_type, l.building, l.floor, l.zone
            FROM assets a
            JOIN systems s   ON s.id = a.system_id
            JOIN locations l ON l.id = a.location_id
            WHERE (%s::text IS NULL OR s.type = %s)
            ORDER BY a.id""", (system_type, system_type))
        assets = cur.fetchall()

        cur.execute("""
            SELECT e.asset_id, e.event_type, e.severity, COUNT(*) AS n,
                   COUNT(*) FILTER (WHERE e.ts > %s) AS n_recent
            FROM events e
            WHERE e.ts > %s AND e.ts <= %s
            GROUP BY e.asset_id, e.event_type, e.severity""", (recent_start, start, end))
        stats = {}
        for r in cur.fetchall():
            stats.setdefault(r["asset_id"], []).append(r)

        cur.execute("SELECT asset_id, MAX(ts) AS last_seen FROM events WHERE ts <= %s GROUP BY asset_id", (end,))
        last_seen = {r["asset_id"]: r["last_seen"] for r in cur.fetchall()}

        cur.execute("""
            SELECT DISTINCT ON (asset_id) asset_id, event_type
            FROM events WHERE event_type = ANY(%s) AND ts <= %s
            ORDER BY asset_id, ts DESC""", (INCIDENT_TYPES, end))
        latest_incident = {r["asset_id"]: r["event_type"] for r in cur.fetchall()}

        out = []
        for a in assets:
            rows = stats.get(a["id"], [])
            faults = FAULT_TYPES[a["system_type"]]
            fault_rows = [r for r in rows if r["event_type"] in faults]
            recent_faults = sum(r["n_recent"] for r in fault_rows)

            status, reason = "OK", f"No fault events in the last {STATUS_WINDOW_MIN} min"
            opener = OPENERS.get(latest_incident.get(a["id"]))
            if opener:
                status = "CRITICAL" if opener[0] == "CRITICAL" else "WARNING"
                reason = f"{opener[1]} (open incident)"
            elif recent_faults:
                status = "WARNING"
                reason = f"{recent_faults} fault event(s) in the last {STATUS_WINDOW_MIN} min"

            out.append({
                **a,
                "status": status,
                "reason": reason,
                "events": sum(r["n"] for r in rows),
                "warnings": sum(r["n"] for r in rows if r["severity"] == "WARNING"),
                "criticals": sum(r["n"] for r in rows if r["severity"] == "CRITICAL"),
                "faults": sum(r["n"] for r in fault_rows),
                "last_seen": last_seen.get(a["id"]),
            })
        out.sort(key=lambda x: (STATUS_ORDER[x["status"]], x["name"]))
        return out

    @router.get("/summary", summary="KPIs and system status indicators")
    def summary(hours: int = Query(24, ge=1, le=168), system_type: Optional[SystemType] = None):
        with db() as cur:
            start, end = get_window(cur, hours)
            cur.execute(AGG_SQL, (start, end))
            rows = cur.fetchall()
            cur.execute(DOWNTIME_SQL, (start, end))
            downtime = cur.fetchone()["down"]
            cur.execute("SELECT COUNT(*) AS n FROM assets a JOIN systems s ON s.id = a.system_id "
                        "WHERE s.type = 'NETWORK'")
            n_net = cur.fetchone()["n"]
            cur.execute(ACTIVE_FIRE_SQL, (end,))
            active_fire = cur.fetchone()["n"]
            assets = asset_health(cur, start, end, None)

        scoped = [r for r in rows if not system_type or r["system_type"] == system_type]
        total = sum(r["n"] for r in scoped)
        critical = sum(r["n"] for r in scoped if r["severity"] == "CRITICAL")

        availability = None
        if n_net:
            availability = max(0.0, 100.0 * (1 - downtime / (hours * 3600 * n_net)))

        bw_rows = [r for r in rows if r["event_type"] == "BANDWIDTH_USAGE" and r["max_v"] is not None]
        peak_bw = max((r["max_v"] for r in bw_rows), default=None)

        denied = count(rows, ["ACCESS_DENIED"])
        granted = count(rows, ["ACCESS_GRANTED"])
        denial_rate = 100.0 * denied / (denied + granted) if (denied + granted) else None

        kpis = [
            kpi("total_events", "Total events", total, "", None),
            kpi("critical_alerts", "Critical alerts", critical, "", None),
            kpi("network_availability", "Network availability", rnd(availability, 3), "%", "NETWORK"),
            kpi("avg_bandwidth", "Avg bandwidth", rnd(weighted_avg(rows, ["BANDWIDTH_USAGE"]), 1), "Mbps",
                "NETWORK", f"peak {rnd(peak_bw, 1)} Mbps" if peak_bw is not None else None),
            kpi("access_denial_rate", "Access denial rate", rnd(denial_rate, 1), "%", "ACCESS",
                f"{denied} denied of {denied + granted}"),
            kpi("avg_hvac_temp", "Avg HVAC temperature", rnd(weighted_avg(rows, TEMP_TYPES), 1), "\u00b0C",
                "HVAC", f"{count(rows, ['HIGH_TEMPERATURE'])} high-temperature events"),
            kpi("camera_offline", "Camera offline incidents", count(rows, ["CAMERA_OFFLINE"]), "", "CCTV"),
            kpi("active_fire_alarms", "Active fire alarms", active_fire, "", "FIRE_ALARM"),
        ]
        kpis = [k for k in kpis if not system_type or k["system"] in (None, system_type)]

        systems = []
        for st in SYSTEMS:
            group = [a for a in assets if a["system_type"] == st]
            worst = min((STATUS_ORDER[a["status"]] for a in group), default=None)
            systems.append({
                "type": st,
                "status": STATUS_BY_RANK[worst] if worst is not None else "UNKNOWN",
                "assets": len(group),
                "ok": sum(a["status"] == "OK" for a in group),
                "warning": sum(a["status"] == "WARNING" for a in group),
                "critical": sum(a["status"] == "CRITICAL" for a in group),
            })

        return {"window": window_info(start, end, hours), "status_window_minutes": STATUS_WINDOW_MIN,
                "system_type": system_type, "kpis": kpis, "systems": systems}

    @router.get("/trends", summary="Time series for the dashboard charts")
    def trends(hours: int = Query(24, ge=1, le=168), system_type: Optional[SystemType] = None):
        with db() as cur:
            start, end = get_window(cur, hours)
            bucket = 900 if hours <= 6 else 3600
            cur.execute("""
                SELECT (floor(extract(epoch FROM e.ts) / %s) * %s)::bigint AS b,
                       s.type AS system_type, e.event_type,
                       COUNT(*) AS n, AVG(e.value) AS avg_v, MAX(e.value) AS max_v
                FROM events e
                JOIN assets a  ON a.id = e.asset_id
                JOIN systems s ON s.id = a.system_id
                WHERE e.ts > %s AND e.ts <= %s
                GROUP BY 1, 2, 3""", (bucket, bucket, start, end))
            rows = cur.fetchall()

        first = (int(start.timestamp()) // bucket) * bucket
        last = (int(end.timestamp()) // bucket) * bucket
        keys = list(range(first, last + 1, bucket))
        idx = {k: i for i, k in enumerate(keys)}
        size = len(keys)

        by_system = {st: [0] * size for st in SYSTEMS if not system_type or st == system_type}
        temp_sum, temp_n = [0.0] * size, [0] * size
        bw_sum, bw_n, bw_peak = [0.0] * size, [0] * size, [None] * size
        granted, denied = [0] * size, [0] * size

        for r in rows:
            i = idx.get(int(r["b"]))
            if i is None:
                continue
            if r["system_type"] in by_system:
                by_system[r["system_type"]][i] += r["n"]
            et = r["event_type"]
            if et in TEMP_TYPES and r["avg_v"] is not None:
                temp_sum[i] += r["avg_v"] * r["n"]; temp_n[i] += r["n"]
            elif et == "BANDWIDTH_USAGE" and r["avg_v"] is not None:
                bw_sum[i] += r["avg_v"] * r["n"]; bw_n[i] += r["n"]
                bw_peak[i] = r["max_v"] if bw_peak[i] is None else max(bw_peak[i], r["max_v"])
            elif et == "ACCESS_GRANTED":
                granted[i] += r["n"]
            elif et == "ACCESS_DENIED":
                denied[i] += r["n"]

        return {
            "window": window_info(start, end, hours),
            "bucket_minutes": bucket // 60,
            "buckets": [datetime.fromtimestamp(k, tz=timezone.utc).isoformat() for k in keys],
            "events_by_system": by_system,
            "hvac_avg_temp": [rnd(temp_sum[i] / temp_n[i], 2) if temp_n[i] else None for i in range(size)],
            "bandwidth_avg": [rnd(bw_sum[i] / bw_n[i], 1) if bw_n[i] else None for i in range(size)],
            "bandwidth_peak": [rnd(v, 1) for v in bw_peak],
            "access_granted": granted,
            "access_denied": denied,
        }

    @router.get("/equipment-health", summary="Per-asset health status")
    def equipment_health(hours: int = Query(24, ge=1, le=168), system_type: Optional[SystemType] = None):
        with db() as cur:
            start, end = get_window(cur, hours)
            items = asset_health(cur, start, end, system_type)
        return {"window": window_info(start, end, hours), "status_window_minutes": STATUS_WINDOW_MIN,
                "items": items}

    return router

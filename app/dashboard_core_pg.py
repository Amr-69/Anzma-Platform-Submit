"""Day 3 dashboard logic (Postgres version).


Port of the original SQLite dashboard_core.py.
Uses Amr's KPI structure and names, running against Ahmed's PostgreSQL.
Metric names match the seeded data: temperature, packet_loss, bandwidth_util.
"""


import os
from datetime import datetime, timedelta, timezone


from dotenv import load_dotenv


load_dotenv()


import psycopg
from psycopg.rows import dict_row


from reference_data import SYSTEM_CODES



# ============================================================
# CONSTANTS
# ============================================================



TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_RANGE_DAYS = 31
HEARTBEAT_EVENT = "heartbeat"



ZONE_TEMP_METRIC = "temperature"
PACKET_LOSS_METRIC = "packet_loss"
BANDWIDTH_METRIC = "bandwidth_util"



STATUS_ORDER = ["critical", "no_data", "warning", "unknown", "normal"]

FAULT_RECOVERY_PAIRS = (("hvac_fault", "hvac_recovered"), ("fire_alarm_triggered", "alarm_cleared"),
                        ("link_down", "link_up"), ("camera_offline", "camera_online"))



class DashboardError(ValueError):
    """The request is invalid (bad filter, bad period)."""



class NoDataError(DashboardError):
    """The database has no telemetry or events to build a dashboard from."""



# ============================================================
# KPI DEFINITIONS (kept identical to original)
# ============================================================



KPI_DEFINITIONS = {
    "monitored_assets": {
        "name": "Monitored assets",
        "unit": "assets",
        "source": "assets",
        "definition": (
            "Number of assets that match the system and building filters. "
            "COUNT(*) over assets joined to system_types and locations."
        ),
    },
    "healthy_assets_pct": {
        "name": "Healthy assets",
        "unit": "%",
        "source": "assets, telemetry_records, event_records",
        "definition": (
            "Assets with status Normal divided by monitored assets, times 100. "
            "Status rules: Critical = a critical fault still active at the end of the period or an "
            "unrecoverable critical point event in the period; "
            "No data = the asset normally sends telemetry or heartbeats but sent "
            "none in the period; Unknown = the asset has never sent telemetry or an event; "
            "Warning = at least one warning event or no data in the period; otherwise Normal."
        ),
    },
    "alerts_raised": {
        "name": "Alerts raised",
        "unit": "alerts",
        "source": "event_records",
        "definition": (
            "Events with severity warning or critical in the period. "
            "COUNT(*) over event_records where severity IN ('warning','critical') "
            "and start <= ts < end."
        ),
    },
    "critical_alerts": {
        "name": "Critical alerts",
        "unit": "alerts",
        "source": "event_records",
        "definition": (
            "Events with severity critical in the period. "
            "COUNT(*) over event_records where severity = 'critical' "
            "and start <= ts < end."
        ),
    },
    "alert_rate_per_hour": {
        "name": "Alert rate",
        "unit": "alerts/hour",
        "source": "event_records",
        "definition": (
            "Alerts raised divided by the length of the selected period in hours."
        ),
    },
    "avg_zone_temp": {
        "name": "Average zone temperature",
        "unit": "C",
        "source": "telemetry_records, metric_types",
        "definition": (
            "AVG(value) of the temperature metric in the period. "
            "Only HVAC assets report it; N/A for other systems."
        ),
    },
    "avg_packet_loss": {
        "name": "Average packet loss",
        "unit": "%",
        "source": "telemetry_records, metric_types",
        "definition": (
            "AVG(value) of the packet_loss metric in the period. "
            "Only network switches report it; N/A for other systems."
        ),
    },
    "heartbeat_compliance": {
        "name": "Heartbeat compliance",
        "unit": "%",
        "source": "event_records, event_types",
        "definition": (
            "Heartbeat events received divided by heartbeat events expected, "
            "times 100 (capped at 100). Expected = heartbeat-sending assets "
            "times the number of hourly heartbeat opportunities in the "
            "intersection of the selected period and available dataset coverage. "
            "Heartbeats occur once per hour, and both the first and last "
            "possible heartbeat timestamps are included."
        ),
    },
}



# ============================================================
# TIMESTAMPS
# ============================================================



def parse_utc_timestamp(value: str) -> datetime:
    """Require a real, canonical UTC timestamp with whole seconds."""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("Timestamp must end with Z and use UTC.")



    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid calendar date or timestamp.") from exc



    if parsed.utcoffset() != timedelta(0):
        raise ValueError("Timestamp must use UTC.")



    if parsed.microsecond != 0:
        raise ValueError("Timestamp must have whole-second precision.")



    if parsed.strftime(TS_FORMAT) != value:
        raise ValueError("Timestamp must use YYYY-MM-DDTHH:MM:SSZ format.")



    return parsed



def format_ts(moment: datetime) -> str:
    """Format a datetime as canonical UTC string ending with Z."""
    return moment.astimezone(timezone.utc).strftime(TS_FORMAT)



# ============================================================
# DATABASE
# ============================================================



def open_readonly():
    """
    Open a read-only-style connection to PostgreSQL.


    Connection parameters come from environment variables.
    """
    conninfo = {
        "host": os.getenv("PGHOST", "localhost"),
        "port": os.getenv("PGPORT", "5432"),
        "dbname": os.getenv("PGDATABASE", "anzma"),
        "user": os.getenv("PGUSER", "postgres"),
        "password": os.getenv("PGPASSWORD", "change-me"),
    }



    # dict_row makes every row behave like a dictionary (row["column"]).
    conn = psycopg.connect(**conninfo, row_factory=dict_row)



    # Force the session to UTC so TIMESTAMPTZ behaves predictably.
    conn.execute("SET TIME ZONE 'UTC'")



    return conn



# ============================================================
# FILTERS AND PERIOD
# ============================================================



SCOPE_CTE = """
WITH scope AS (
    SELECT
        a.asset_id,
        a.asset_code,
        a.name,
        a.asset_type,
        st.code AS system_type,
        l.name  AS location_name,
        l.floor AS floor
    FROM assets AS a
    JOIN system_types AS st ON st.system_type_id = a.system_type_id
    JOIN locations    AS l  ON l.location_id     = a.location_id
    WHERE (%(system)s::TEXT IS NULL OR st.code = %(system)s::TEXT)
      AND (%(building)s::BIGINT IS NULL OR l.building_id = %(building)s::BIGINT)
)
"""



def get_filter_options(conn):
    """Filter choices and the time extent of the data, all read from the database."""



    systems = [
        {"code": row["code"], "assets": row["assets"]}
        for row in conn.execute(
            """
            SELECT st.code AS code, COUNT(a.asset_id) AS assets
            FROM system_types AS st
            LEFT JOIN assets AS a ON a.system_type_id = st.system_type_id
            GROUP BY st.system_type_id, st.code
            ORDER BY st.system_type_id
            """
        )
    ]



    buildings = [
        {"building_id": row["building_id"], "name": row["name"]}
        for row in conn.execute(
            "SELECT building_id, name FROM buildings ORDER BY building_id"
        )
    ]



    first, last = _data_extent(conn)



    return {
        "systems": systems,
        "buildings": buildings,
        "data_start": format_ts(first),
        "data_end_exclusive": format_ts(last + timedelta(seconds=1)),
    }



def _data_extent(conn):
    """Return the first and last timestamps present in telemetry or events."""



    row = conn.execute(
        """
        SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts
        FROM (
            SELECT ts FROM telemetry_records
            UNION ALL
            SELECT ts FROM event_records
        ) AS all_ts
        """
    ).fetchone()



    if row["first_ts"] is None:
        raise NoDataError("The database has no telemetry or events.")



    # Postgres already returns timezone-aware datetime objects.
    return row["first_ts"], row["last_ts"]



def resolve_filters(conn, system_type, building_id, start_time, end_time):
    """Validate filters. Defaults and coverage boundaries come from the data."""



    if system_type is not None and system_type not in SYSTEM_CODES:
        raise DashboardError(
            f"Unknown system_type. Allowed values: {', '.join(SYSTEM_CODES)}."
        )



    if building_id is not None:
        exists = conn.execute(
            "SELECT 1 FROM buildings WHERE building_id = %(building_id)s",
            {"building_id": building_id},
        ).fetchone()



        if exists is None:
            raise DashboardError("Unknown building_id.")



    first, last = _data_extent(conn)



    try:
        start = parse_utc_timestamp(start_time) if start_time is not None else first
        end = (
            parse_utc_timestamp(end_time)
            if end_time is not None
            else last + timedelta(seconds=1)
        )
    except ValueError as exc:
        raise DashboardError(str(exc)) from exc



    if start >= end:
        raise DashboardError("start_time must be earlier than end_time.")



    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise DashboardError(
            f"The period may not be longer than {MAX_RANGE_DAYS} days."
        )



    return {
        "system": system_type,
        "building": building_id,
        "start": start,
        "end": end,
        "data_start": first,
        "data_end_exclusive": last + timedelta(seconds=1),
    }



def _params(f):
    """Convert filter dict into parameters ready for psycopg."""
    return {
        "system": f["system"],
        "building": f["building"],
        "start": f["start"],
        "end": f["end"],
    }



# ============================================================
# KPI HELPERS
# ============================================================



def _monitored_assets(conn, params):
    """Count assets matching the current system/building filters."""



    row = conn.execute(
        SCOPE_CTE
        + """
        SELECT COUNT(*) AS monitored_assets
        FROM scope
        """,
        params,
    ).fetchone()



    return int(row["monitored_assets"])



def _asset_status_counts(conn, params):
    """
    Return status counts for the filtered assets.



    Priority:
    1. critical: a critical fault is still active at the end of the selected window, or an
       unrecoverable critical point event occurred in the window
    2. no_data: the asset has reported before but has no telemetry or heartbeat in period
    3. warning: at least one warning event in period
    4. unknown: no telemetry or event has ever been recorded for the asset
    5. normal: everything else
    """



    pair_values = ", ".join("('%s', '%s')" % pair for pair in FAULT_RECOVERY_PAIRS)
    paired_event_types = [event_type for pair in FAULT_RECOVERY_PAIRS for event_type in pair]
    rows = conn.execute(
        SCOPE_CTE
        + """
        , fault_pairs(fault_type, recovery_type) AS (VALUES """ + pair_values + """)
        , latest_fault_state AS (
            SELECT DISTINCT ON (er.asset_id, fp.fault_type)
                er.asset_id, fp.fault_type, et.name AS event_type, er.severity
            FROM event_records AS er
            JOIN event_types AS et ON et.event_type_id = er.event_type_id
            JOIN fault_pairs AS fp ON et.name IN (fp.fault_type, fp.recovery_type)
            WHERE er.ts < %(end)s
            ORDER BY er.asset_id, fp.fault_type, er.ts DESC, er.event_id DESC
        ), active_fault_stats AS (
            SELECT asset_id,
                   COUNT(*) FILTER (WHERE event_type = fault_type AND severity = 'critical') AS critical_count
            FROM latest_fault_state
            GROUP BY asset_id
        ), event_stats AS (
            SELECT
                er.asset_id,
                COUNT(*) FILTER (
                    WHERE er.severity = 'critical' AND et.name <> ALL(%(paired_types)s)
                ) AS critical_count,
                COUNT(*) FILTER (
                    WHERE er.severity = 'warning'
                ) AS warning_count
            FROM event_records AS er
            JOIN event_types AS et ON et.event_type_id = er.event_type_id
            WHERE er.ts >= %(start)s
              AND er.ts < %(end)s
            GROUP BY er.asset_id
        ),
        telemetry_stats AS (
            SELECT
                asset_id,
                COUNT(*) AS telemetry_count
            FROM telemetry_records
            WHERE ts >= %(start)s
              AND ts < %(end)s
            GROUP BY asset_id
        ),
        heartbeat_stats AS (
            SELECT
                er.asset_id,
                COUNT(*) AS heartbeat_count
            FROM event_records AS er
            JOIN event_types AS et
              ON et.event_type_id = er.event_type_id
            WHERE et.name = 'heartbeat'
              AND er.ts >= %(start)s
              AND er.ts < %(end)s
            GROUP BY er.asset_id
        )
        SELECT
            s.asset_id,
            COALESCE(e.critical_count, 0) + COALESCE(af.critical_count, 0) AS critical_count,
            COALESCE(e.warning_count, 0) AS warning_count,
            COALESCE(t.telemetry_count, 0) AS telemetry_count,
            COALESCE(h.heartbeat_count, 0) AS heartbeat_count,
            CASE
                WHEN COALESCE(e.critical_count, 0) + COALESCE(af.critical_count, 0) > 0 THEN 'critical'
                WHEN COALESCE(t.telemetry_count, 0) = 0
                 AND COALESCE(h.heartbeat_count, 0) = 0
                 AND (
                     EXISTS (
                         SELECT 1
                         FROM telemetry_records AS old_t
                         WHERE old_t.asset_id = s.asset_id
                     )
                     OR EXISTS (
                         SELECT 1
                         FROM event_records AS old_e
                         WHERE old_e.asset_id = s.asset_id
                     )
                 )
                THEN 'no_data'
                WHEN COALESCE(e.warning_count, 0) > 0 THEN 'warning'
                WHEN NOT EXISTS (
                    SELECT 1 FROM telemetry_records AS ever_t WHERE ever_t.asset_id = s.asset_id
                    UNION ALL
                    SELECT 1 FROM event_records AS ever_e WHERE ever_e.asset_id = s.asset_id
                ) THEN 'unknown'
                ELSE 'normal'
            END AS status
        FROM scope AS s
        LEFT JOIN event_stats AS e ON e.asset_id = s.asset_id
        LEFT JOIN active_fault_stats AS af ON af.asset_id = s.asset_id
        LEFT JOIN telemetry_stats AS t ON t.asset_id = s.asset_id
        LEFT JOIN heartbeat_stats AS h ON h.asset_id = s.asset_id
        """,
        dict(params, paired_types=paired_event_types),
    ).fetchall()



    counts = {status: 0 for status in STATUS_ORDER}



    for row in rows:
        counts[row["status"]] += 1



    return counts



def _alert_counts(conn, params):
    """Return warning/critical event counts for the filtered assets."""



    row = conn.execute(
        SCOPE_CTE
        + """
        , filtered_events AS (
            SELECT
                er.event_id,
                er.severity
            FROM event_records AS er
            JOIN scope AS s
              ON s.asset_id = er.asset_id
            WHERE er.ts >= %(start)s
              AND er.ts < %(end)s
        )
        SELECT
            COUNT(*) FILTER (
                WHERE severity IN ('warning', 'critical')
            ) AS alerts_raised,
            COUNT(*) FILTER (
                WHERE severity = 'critical'
            ) AS critical_alerts
        FROM filtered_events
        """,
        params,
    ).fetchone()



    return {
        "alerts_raised": int(row["alerts_raised"]),
        "critical_alerts": int(row["critical_alerts"]),
    }


def _average_metric(conn, params, metric_name):
    """Return the average telemetry value for one metric, or None."""



    row = conn.execute(
        SCOPE_CTE
        + """
        , metric_data AS (
            SELECT
                tr.value
            FROM telemetry_records AS tr
            JOIN metric_types AS mt
              ON mt.metric_id = tr.metric_id
            JOIN scope AS s
              ON s.asset_id = tr.asset_id
            WHERE mt.name = %(metric_name)s
              AND tr.ts >= %(start)s
              AND tr.ts < %(end)s
        )
        SELECT AVG(value) AS average_value
        FROM metric_data
        """,
        {
            **params,
            "metric_name": metric_name,
        },
    ).fetchone()



    if row["average_value"] is None:
        return None



    return float(row["average_value"])



def _heartbeat_compliance(conn, filters, params):
    """
    Calculate heartbeat compliance.


    Expected heartbeats:
        heartbeat-sending assets
        x hourly heartbeat opportunities in the intersection of:
        - selected period
        - available dataset coverage


    Heartbeats occur once per hour. Both the first and last possible
    heartbeat timestamps are included when they fall inside the period.
    """



    data_start = filters["data_start"]
    data_end_exclusive = filters["data_end_exclusive"]



    overlap_start = max(filters["start"], data_start)
    overlap_end = min(filters["end"], data_end_exclusive)



    overlap_seconds = (
        overlap_end - overlap_start
    ).total_seconds()



    if overlap_seconds <= 0:
        return {
            "received": 0,
            "expected": 0,
            "compliance_pct": None,
        }



    # One heartbeat opportunity per hour, including both period boundaries.
    expected_slots = int(overlap_seconds // 3600) + 1



    row = conn.execute(
        SCOPE_CTE
        + """
        , heartbeat_assets AS (
            SELECT DISTINCT er.asset_id
            FROM event_records AS er
            JOIN event_types AS et
              ON et.event_type_id = er.event_type_id
            JOIN scope AS s
              ON s.asset_id = er.asset_id
            WHERE et.name = 'heartbeat'
              AND er.ts >= %(start)s
              AND er.ts < %(end)s
        ),
        received AS (
            SELECT COUNT(*) AS received_count
            FROM event_records AS er
            JOIN event_types AS et
              ON et.event_type_id = er.event_type_id
            JOIN scope AS s
              ON s.asset_id = er.asset_id
            WHERE et.name = 'heartbeat'
              AND er.ts >= %(start)s
              AND er.ts < %(end)s
        )
        SELECT
            (SELECT COUNT(*) FROM heartbeat_assets) AS sending_assets,
            (SELECT received_count FROM received) AS received_count
        """,
        params,
    ).fetchone()



    sending_assets = int(row["sending_assets"])
    received = int(row["received_count"])
    expected = sending_assets * expected_slots



    if expected == 0:
        return {
            "received": received,
            "expected": 0,
            "compliance_pct": None,
        }



    compliance = min(100.0, (received / expected) * 100.0)



    return {
        "received": received,
        "expected": expected,
        "compliance_pct": round(compliance, 2),
    }



# ============================================================
# MAIN DASHBOARD FUNCTION
# ============================================================



def get_dashboard_summary(
    conn,
    system_type=None,
    building_id=None,
    start_time=None,
    end_time=None,
):
    """
    Calculate the eight dashboard KPIs for the selected filters and period.
    """



    filters = resolve_filters(
        conn,
        system_type=system_type,
        building_id=building_id,
        start_time=start_time,
        end_time=end_time,
    )



    params = _params(filters)



    monitored_assets = _monitored_assets(conn, params)
    status_counts = _asset_status_counts(conn, params)



    healthy_assets = status_counts["normal"]
    healthy_assets_pct = None



    if monitored_assets > 0:
        healthy_assets_pct = round(
            (healthy_assets / monitored_assets) * 100.0,
            2,
        )



    alert_counts = _alert_counts(conn, params)



    period_hours = (filters["end"] - filters["start"]).total_seconds() / 3600.0



    alert_rate = None
    if period_hours > 0:
        alert_rate = round(
            alert_counts["alerts_raised"] / period_hours,
            4,
        )



    avg_zone_temp = _average_metric(
        conn,
        params,
        ZONE_TEMP_METRIC,
    )



    avg_packet_loss = _average_metric(
        conn,
        params,
        PACKET_LOSS_METRIC,
    )



    heartbeat = _heartbeat_compliance(
        conn,
        filters,
        params,
    )



    return {
        "filters": {
            "system_type": filters["system"],
            "building_id": filters["building"],
            "start_time": format_ts(filters["start"]),
            "end_time_exclusive": format_ts(filters["end"]),
            "data_start": format_ts(filters["data_start"]),
            "data_end_exclusive": format_ts(filters["data_end_exclusive"]),
        },
        "kpis": {
            "monitored_assets": {
                "value": monitored_assets,
                "unit": KPI_DEFINITIONS["monitored_assets"]["unit"],
            },
            "healthy_assets_pct": {
                "value": healthy_assets_pct,
                "unit": KPI_DEFINITIONS["healthy_assets_pct"]["unit"],
                "status_counts": status_counts,
            },
            "alerts_raised": {
                "value": alert_counts["alerts_raised"],
                "unit": KPI_DEFINITIONS["alerts_raised"]["unit"],
            },
            "critical_alerts": {
                "value": alert_counts["critical_alerts"],
                "unit": KPI_DEFINITIONS["critical_alerts"]["unit"],
            },
            "alert_rate_per_hour": {
                "value": alert_rate,
                "unit": KPI_DEFINITIONS["alert_rate_per_hour"]["unit"],
            },
            "avg_zone_temp": {
                "value": avg_zone_temp,
                "unit": KPI_DEFINITIONS["avg_zone_temp"]["unit"],
            },
            "avg_packet_loss": {
                "value": avg_packet_loss,
                "unit": KPI_DEFINITIONS["avg_packet_loss"]["unit"],
            },
            "heartbeat_compliance": {
                "value": heartbeat["compliance_pct"],
                "unit": KPI_DEFINITIONS["heartbeat_compliance"]["unit"],
                "received": heartbeat["received"],
                "expected": heartbeat["expected"],
            },
        },
    }

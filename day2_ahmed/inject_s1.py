"""
inject_s1.py - inject the S1 "cooling failure" scenario with CURRENT timestamps (Day 2).

Only INSERTS into telemetry_records, event_records and scenario_runs; nothing is deleted or changed.
Every row type is checked with integrity_rules.check_record_db before anything is written, and the whole
run is one transaction. Safe to rerun: if an S1 run already covers the new 2-hour window, the script
reports it and writes nothing. A new fault can be injected once the previous S1 window is over.

    docker compose exec api python day2_ahmed/inject_s1.py --dry-run    # checks and counts, then rolls back
    docker compose exec api python day2_ahmed/inject_s1.py              # writes

Detection is NOT run here. The alert engine finds the fault when /ask (or pipeline.detect) runs.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
from psycopg2.extras import Json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import integrity_rules as ir  # noqa: E402

ASSET_CODE = "HVAC-CRAC-01"
# (minutes relative to the anchor, CRAC Unit 1 temperature in C). Warning is > 29, critical is > 32.
READINGS = [(-120, 24.4), (-105, 24.9), (-90, 27.6), (-75, 30.2), (-60, 31.4),
            (-45, 32.6), (-30, 33.4), (-15, 33.9), (0, 34.2)]
FAULT_EVENT_AT = -75   # hvac_fault is logged when the temperature first crosses the warning threshold
EXPECTED_RULES = ["HVAC_HIGH_TEMP", "HVAC_TEMP_ANOMALY"]


def connect():
    return psycopg2.connect(
        host=os.getenv("PGHOST", "db"), port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "anzma"), user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""), cursor_factory=psycopg2.extras.RealDictCursor)


def main(dry_run=False, now=None, conn=None):
    now = now or datetime.now(timezone.utc)
    anchor = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    stamps = [anchor + timedelta(minutes=m) for m, _ in READINGS]
    fault_ts = anchor + timedelta(minutes=FAULT_EVENT_AT)
    conn = conn or connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT asset_id, name FROM assets WHERE asset_code=%s", (ASSET_CODE,))
            asset = cur.fetchone()
            cur.execute("SELECT metric_id FROM metric_types WHERE name='temperature'")
            metric = cur.fetchone()
            cur.execute("SELECT event_type_id FROM event_types WHERE name='hvac_fault'")
            etype = cur.fetchone()
            if not (asset and metric and etype):
                raise SystemExit("S1 needs asset HVAC-CRAC-01, metric 'temperature' and event type "
                                 "'hvac_fault' in the database, and one of them is missing.")
            problems = (ir.check_record_db(cur, asset["asset_id"], "metric", "temperature")
                        + ir.check_record_db(cur, asset["asset_id"], "event", "hvac_fault"))
            if problems:
                raise SystemExit("Refusing to inject: " + "; ".join(problems))

            cur.execute("""SELECT scenario_run_id, start_ts, end_ts FROM scenario_runs
                           WHERE scenario_key='S1' AND end_ts >= %s AND start_ts <= %s
                           ORDER BY end_ts DESC LIMIT 1""", (stamps[0], stamps[-1]))
            active = cur.fetchone()
            if active:
                print(f"S1 is already injected: scenario_runs id {active['scenario_run_id']}, "
                      f"{active['start_ts']:%Y-%m-%d %H:%M} to {active['end_ts']:%H:%M} UTC. Nothing written.")
                return

            new_readings = 0
            for ts, (_, value) in zip(stamps, READINGS):
                cur.execute("""INSERT INTO telemetry_records (asset_id, metric_id, ts, value)
                               VALUES (%s, %s, %s, %s)
                               ON CONFLICT (asset_id, metric_id, ts) DO NOTHING""",
                            (asset["asset_id"], metric["metric_id"], ts, value))
                new_readings += cur.rowcount
            cur.execute("""INSERT INTO event_records (asset_id, event_type_id, ts, severity, details)
                           VALUES (%s, %s, %s, 'critical', %s)
                           ON CONFLICT (asset_id, event_type_id, ts) DO NOTHING""",
                        (asset["asset_id"], etype["event_type_id"], fault_ts,
                         Json({"scenario": "S1", "note": "Simulated CRAC cooling failure"})))
            new_events = cur.rowcount

            cur.execute("""SELECT telemetry_id FROM telemetry_records
                           WHERE asset_id=%s AND metric_id=%s AND ts = ANY(%s) ORDER BY ts""",
                        (asset["asset_id"], metric["metric_id"], stamps))
            telemetry_ids = [r["telemetry_id"] for r in cur.fetchall()]
            cur.execute("SELECT event_id FROM event_records WHERE asset_id=%s AND event_type_id=%s AND ts=%s",
                        (asset["asset_id"], etype["event_type_id"], fault_ts))
            event_ids = [r["event_id"] for r in cur.fetchall()]

            cur.execute("""INSERT INTO scenario_runs (scenario_key, name, description, asset_ids, start_ts, end_ts,
                                                      expected_rules, telemetry_ids, event_ids)
                           VALUES (%s, %s, %s, %s::bigint[], %s, %s, %s::text[], %s::bigint[], %s::bigint[])
                           RETURNING scenario_run_id""",
                        ("S1", "Server-room cooling failure",
                         "Simulated: CRAC Unit 1 temperature climbs above the warning and critical thresholds.",
                         [asset["asset_id"]], stamps[0], stamps[-1], EXPECTED_RULES, telemetry_ids, event_ids))
            run = cur.fetchone()

        print("S1 injection" + (" (dry run, nothing written)" if dry_run else ""))
        print(f"  anchor (UTC)    : {anchor:%Y-%m-%d %H:%M}  (readings {stamps[0]:%H:%M} to {stamps[-1]:%H:%M})")
        print(f"  asset           : {asset['name']} (id {asset['asset_id']})")
        print(f"  temperature     : {new_readings} new of {len(READINGS)} readings, peak {max(v for _, v in READINGS)} C")
        print(f"  hvac_fault event: {new_events} new of 1")
        print(f"  scenario_runs   : registered as id {run['scenario_run_id']}")
        print(f"  expected rules  : {', '.join(EXPECTED_RULES)}")
        conn.rollback() if dry_run else conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv[1:])
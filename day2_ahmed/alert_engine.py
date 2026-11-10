"""
alert_engine.py
Evaluates threshold rules, a compound rule and a robust z-score anomaly
detector over stored events. Every alert keeps the actual events and measured
facts that triggered it, so it can be traced back to the database.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean, median

try:                                   # legacy store_alerts() only; the unified schema goes through db_adapter
    from psycopg2.extras import Json, execute_values
except ImportError:                    # psycopg (v3) only installations
    Json = execute_values = None

import alert_config as cfg

T = cfg.T
TEMP_TYPES = ("TEMPERATURE_READING", "HIGH_TEMPERATURE")
SAFE_DETAIL_KEYS = {"humidity", "setpoint", "fan_on", "mains_ok", "battery_pct", "packet_loss_pct",
                    "port", "downtime_sec", "card_id", "direction", "reason", "after_hours",
                    "detector", "note", "alarm", "zone"}


def downtime_sec(down, up):
    """Seconds between a LINK_DOWN and its LINK_UP, from the stored timestamps."""
    return round((up["ts"] - down["ts"]).total_seconds())


def minutes(a, b):
    return (b - a).total_seconds() / 60.0


def local_hour(ts):
    return (ts + timedelta(hours=cfg.LOCAL_UTC_OFFSET)).hour


def is_occupied(ts):
    return cfg.WORK_START <= local_hour(ts) < cfg.WORK_END


def load_context(cur, end):
    base = end - timedelta(days=cfg.HISTORY_DAYS)
    cur.execute("""SELECT a.id, a.name, s.type AS system_type, a.location_id, l.zone
                   FROM assets a
                   JOIN systems s   ON s.id = a.system_id
                   JOIN locations l ON l.id = a.location_id""")
    assets = {r["id"]: dict(r) for r in cur.fetchall()}
    cur.execute("""SELECT id, asset_id, ts, event_type, severity, value, unit, details
                   FROM events WHERE ts > %s AND ts <= %s ORDER BY ts, id""", (base, end))
    ev = {aid: [] for aid in assets}
    for r in cur.fetchall():
        r = dict(r)
        r["ts"] = r["ts"].astimezone(timezone.utc)
        r["details"] = r["details"] or {}
        ev[r["asset_id"]].append(r)
    return {"assets": assets, "ev": ev, "end": end.astimezone(timezone.utc)}


def of(ctx, aid, *types, need_value=False):
    return [e for e in ctx["ev"][aid]
            if e["event_type"] in types and (not need_value or e["value"] is not None)]


def later(ctx, aid, after_ts, etype):
    for e in ctx["ev"][aid]:
        if e["ts"] > after_ts and e["event_type"] == etype:
            return e
    return None


def of_system(ctx, system):
    return [a for a in ctx["assets"].values() if a["system_type"] == system]


def runs(items, pred, max_gap_min, min_len):
    out, cur = [], []
    for e in items:
        if pred(e):
            if cur and (e["ts"] - cur[-1]["ts"]) > timedelta(minutes=max_gap_min):
                if len(cur) >= min_len:
                    out.append(cur)
                cur = []
            cur.append(e)
        else:
            if len(cur) >= min_len:
                out.append(cur)
            cur = []
    if len(cur) >= min_len:
        out.append(cur)
    return out


def chain(items, gap_min):
    groups, cur = [], []
    for e in items:
        if cur and (e["ts"] - cur[-1]["ts"]) > timedelta(minutes=gap_min):
            groups.append(cur)
            cur = []
        cur.append(e)
    if cur:
        groups.append(cur)
    return groups


def window_stats(items, window_min, threshold):
    """Largest number of events inside any window, and when the count first reached threshold."""
    best, detect, lo = 0, None, 0
    for hi in range(len(items)):
        while items[hi]["ts"] - items[lo]["ts"] > timedelta(minutes=window_min):
            lo += 1
        n = hi - lo + 1
        best = max(best, n)
        if detect is None and n >= threshold:
            detect = items[hi]["ts"]
    return best, detect


def evidence_event(e):
    """Evidence row with a hard reference to its source row (event_records or telemetry_records)."""
    src = e.get("source", "event")
    rid = e.get("record_id", e["id"])
    return {"source": src, "record_id": rid, "event_id" if src == "event" else "telemetry_id": rid,
            "ts": e["ts"].isoformat(), "event_type": e["event_type"],
            "value": e["value"], "unit": e["unit"],
            "details": {k: v for k, v in e["details"].items() if k in SAFE_DETAIL_KEYS}}


def select_events(evs, key=None):
    evs = sorted(evs, key=lambda e: (e["ts"], e["id"]))
    limit = cfg.MAX_EVIDENCE_EVENTS
    if len(evs) <= limit:
        chosen = list(range(len(evs)))
    else:
        required = {0, len(evs) - 1}
        if key:
            required.add(max(range(len(evs)), key=lambda i: key(evs[i])))
        step = (len(evs) - 1) / (limit - 1)
        fill = [round(k * step) for k in range(limit)]
        chosen = set(required)
        for i in fill:
            if len(chosen) >= limit:
                break
            chosen.add(i)
        chosen = sorted(chosen)
    out = []
    for n, i in enumerate(chosen, 1):
        item = evidence_event(evs[i])
        item["ref"] = f"E{n}"
        out.append(item)
    return out


def make_alert(rule, severity, asset, evs, summary, facts, detected_ts, thresholds=None, key=None):
    evs = sorted({e["id"]: e for e in evs}.values(), key=lambda e: (e["ts"], e["id"]))
    return {
        "rule_id": rule,
        "detection_method": cfg.RULES[rule]["method"],
        "severity": severity,
        "asset_id": asset["id"],
        "title": f"{cfg.RULES[rule]['title']} - {asset['name']}",
        "summary": summary,
        "first_ts": evs[0]["ts"],
        "last_ts": evs[-1]["ts"],
        "detected_ts": detected_ts,
        "evidence": {"thresholds": thresholds or {}, "facts": facts,
                     "event_count": len(evs), "events": select_events(evs, key)},
    }


def related_filter_fault(ctx, aid, t0, t1):
    return [e for e in of(ctx, aid, "FILTER_FAULT")
            if t0 - timedelta(minutes=60) <= e["ts"] <= t1]


def rule_hvac_temp(ctx):
    out = []
    for a in of_system(ctx, "HVAC"):
        server = a["zone"] == "Server Room"
        warn = T["server_temp_warn"] if server else T["hvac_temp_warn"]
        crit = T["server_temp_crit"] if server else T["hvac_temp_crit"]
        reads = of(ctx, a["id"], *TEMP_TYPES, need_value=True)
        for run in runs(reads, lambda e: e["value"] > warn, T["reading_gap_min"], 1):
            peak = max(run, key=lambda e: e["value"])
            dur = minutes(run[0]["ts"], run[-1]["ts"])
            ff = related_filter_fault(ctx, a["id"], run[0]["ts"], run[-1]["ts"])
            facts = {"max_value": round(peak["value"], 2), "unit": "°C", "readings": len(run),
                     "duration_min": round(dur, 1), "sustained": len(run) >= 3, "zone": a["zone"],
                     "fan_off": any(e["details"].get("fan_on") is False for e in run),
                     "filter_fault": bool(ff)}
            if dur > 0:
                facts["rise_c_per_10min"] = round((run[-1]["value"] - run[0]["value"]) / dur * 10, 2)
            sev = "CRITICAL" if peak["value"] > crit else "WARNING"
            summary = (f"{a['name']} ({a['zone']}): temperature peaked at {peak['value']:.1f} °C, above the "
                       f"{warn:g} °C warning threshold, across {len(run)} reading(s) over {dur:.0f} min.")
            out.append(make_alert("HVAC_HIGH_TEMP", sev, a, run + ff, summary, facts, run[0]["ts"],
                                  {"warn": warn, "crit": crit, "unit": "°C"}, key=lambda e: e["value"] or 0))
    return out


def rule_hvac_filter(ctx):
    out = []
    for a in of_system(ctx, "HVAC"):
        for e in of(ctx, a["id"], "FILTER_FAULT"):
            out.append(make_alert("HVAC_FILTER_FAULT", "WARNING", a, [e],
                                  f"{a['name']} ({a['zone']}) reported a filter fault.",
                                  {"zone": a["zone"]}, e["ts"]))
    return out


def rule_net_link(ctx):
    out = []
    for a in of_system(ctx, "NETWORK"):
        downs = of(ctx, a["id"], "LINK_DOWN")
        for grp in chain(downs, T["link_flap_window_min"]):
            wmax, detect = window_stats(grp, T["link_flap_window_min"], T["link_flap_count"])
            if wmax >= T["link_flap_count"]:
                evs = list(grp)
                for d in grp:
                    up = later(ctx, a["id"], d["ts"], "LINK_UP")
                    if up:
                        evs.append(up)
                downtime = sum(e["details"].get("downtime_sec") or 0 for e in evs if e["event_type"] == "LINK_UP")
                if not downtime:
                    downtime = sum(downtime_sec(d, later(ctx, a["id"], d["ts"], "LINK_UP"))
                                   for d in grp if later(ctx, a["id"], d["ts"], "LINK_UP"))
                facts = {"link_down_count": len(grp), "window_min": T["link_flap_window_min"],
                         "total_downtime_sec": round(downtime, 0)}
                summary = (f"{a['name']}: {len(grp)} link-down events within {minutes(grp[0]['ts'], grp[-1]['ts']):.0f} min "
                           f"({downtime:.0f} s total downtime).")
                out.append(make_alert("NET_LINK_FLAPPING", "CRITICAL", a, evs, summary, facts, detect,
                                      {"count": T["link_flap_count"], "window_min": T["link_flap_window_min"]}))
            else:
                for d in grp:
                    up = later(ctx, a["id"], d["ts"], "LINK_UP")
                    facts = {"open": up is None}
                    evs = [d]
                    if up:
                        facts["downtime_sec"] = up["details"].get("downtime_sec") or downtime_sec(d, up)
                        evs.append(up)
                    state = "and no recovery was recorded" if up is None else f"for {facts['downtime_sec']} s"
                    out.append(make_alert("NET_LINK_DOWN", "CRITICAL", a, evs,
                                          f"{a['name']}: link went down {state}.", facts, d["ts"]))
    return out


def rule_net_bandwidth(ctx):
    out = []
    n = T["bandwidth_sustain"]
    for a in of_system(ctx, "NETWORK"):
        reads = of(ctx, a["id"], "BANDWIDTH_USAGE", need_value=True)
        for run in runs(reads, lambda e: e["value"] > T["bandwidth_warn"], T["reading_gap_min"], n):
            m = mean(e["value"] for e in run)
            peak = max(e["value"] for e in run)
            loss = max(e["details"].get("packet_loss_pct", 0) for e in run)
            sev = "CRITICAL" if m >= T["bandwidth_crit_mean"] else "WARNING"
            facts = {"mean_mbps": round(m, 1), "peak_mbps": round(peak, 1), "readings": len(run),
                     "duration_min": round(minutes(run[0]["ts"], run[-1]["ts"]), 1),
                     "packet_loss_max_pct": round(loss, 2)}
            summary = (f"{a['name']}: bandwidth averaged {m:.0f} Mbps (peak {peak:.0f} Mbps) over {len(run)} "
                       f"consecutive readings, above the {T['bandwidth_warn']:g} Mbps threshold.")
            out.append(make_alert("NET_BANDWIDTH_HIGH", sev, a, run, summary, facts, run[n - 1]["ts"],
                                  {"warn": T["bandwidth_warn"], "crit_mean": T["bandwidth_crit_mean"], "unit": "Mbps"},
                                  key=lambda e: e["value"] or 0))
        m_n = T["packet_loss_sustain"]
        for run in runs(reads, lambda e: e["details"].get("packet_loss_pct", 0) > T["packet_loss_warn"],
                        T["reading_gap_min"], m_n):
            losses = [e["details"]["packet_loss_pct"] for e in run]
            lm = mean(losses)
            sev = "CRITICAL" if lm >= T["packet_loss_crit_mean"] else "WARNING"
            facts = {"loss_max_pct": round(max(losses), 2), "loss_mean_pct": round(lm, 2), "readings": len(run),
                     "bw_mean_mbps": round(mean(e["value"] for e in run), 1)}
            summary = (f"{a['name']}: packet loss averaged {lm:.1f}% (max {max(losses):.1f}%) over {len(run)} "
                       f"consecutive readings, above the {T['packet_loss_warn']:g}% threshold.")
            out.append(make_alert("NET_PACKET_LOSS", sev, a, run, summary, facts, run[m_n - 1]["ts"],
                                  {"warn_pct": T["packet_loss_warn"], "crit_mean_pct": T["packet_loss_crit_mean"]},
                                  key=lambda e: e["details"].get("packet_loss_pct", 0)))
    return out


def rule_access(ctx):
    out = []
    for a in of_system(ctx, "ACCESS"):
        for e in of(ctx, a["id"], "DOOR_FORCED"):
            out.append(make_alert("ACC_DOOR_FORCED", "CRITICAL", a, [e],
                                  f"{a['name']} ({a['zone']}): door reported as forced open.",
                                  {"zone": a["zone"], "after_hours": not is_occupied(e["ts"])}, e["ts"]))
        for e in of(ctx, a["id"], "DOOR_HELD_OPEN", need_value=True):
            if e["value"] > T["door_held_open_s"]:
                out.append(make_alert("ACC_DOOR_HELD_OPEN", "WARNING", a, [e],
                                      f"{a['name']}: door held open for {e['value']:.0f} s "
                                      f"(limit {T['door_held_open_s']} s).",
                                      {"held_seconds": e["value"], "limit_seconds": T["door_held_open_s"]}, e["ts"],
                                      {"limit_seconds": T["door_held_open_s"]}))
        denied = of(ctx, a["id"], "ACCESS_DENIED")
        grants = of(ctx, a["id"], "ACCESS_GRANTED")
        for grp in chain(denied, T["denial_window_min"]):
            wmax, detect = window_stats(grp, T["denial_window_min"], T["denial_warn"])
            if wmax < T["denial_warn"]:
                continue
            reasons = Counter(e["details"].get("reason") for e in grp)
            cards = {e["details"].get("card_id") for e in grp}
            gi = [g for g in grants if grp[0]["ts"] <= g["ts"] <= grp[-1]["ts"]]
            facts = {"denied_count": len(grp), "max_in_window": wmax, "window_min": T["denial_window_min"],
                     "distinct_cards": len(cards),
                     "unknown_card_share": round(reasons.get("UNKNOWN_CARD", 0) / len(grp), 2),
                     "permission_share": round(reasons.get("NO_PERMISSION", 0) / len(grp), 2),
                     "grants_during": len(gi), "after_hours": not is_occupied(grp[0]["ts"]),
                     "duration_min": round(minutes(grp[0]["ts"], grp[-1]["ts"]), 1)}
            sev = "CRITICAL" if wmax >= T["denial_crit"] else "WARNING"
            summary = (f"{a['name']} ({a['zone']}): {len(grp)} denied attempts over {facts['duration_min']:.0f} min "
                       f"({wmax} within {T['denial_window_min']} min) using {len(cards)} distinct card(s).")
            out.append(make_alert("ACC_DENIAL_SPIKE", sev, a, grp, summary, facts, detect,
                                  {"warn": T["denial_warn"], "crit": T["denial_crit"], "window_min": T["denial_window_min"]}))
    return out


def rule_cctv(ctx):
    out = []
    for a in of_system(ctx, "CCTV"):
        for off in of(ctx, a["id"], "CAMERA_OFFLINE"):
            on = later(ctx, a["id"], off["ts"], "CAMERA_ONLINE")
            end_t = on["ts"] if on else ctx["end"]
            dur = minutes(off["ts"], end_t)
            evs = [off] + ([on] if on else [])
            if dur >= T["camera_offline_min"]:
                facts = {"open": on is None, "duration_min": round(dur, 1), "zone": a["zone"],
                         "reason": off["details"].get("reason")}
                out.append(make_alert("CCTV_OFFLINE_LONG", "WARNING", a, evs,
                                      f"{a['name']} ({a['zone']}) was offline for {dur:.0f} min"
                                      f"{' and has not recovered' if on is None else ''}.",
                                      facts, off["ts"] + timedelta(minutes=T["camera_offline_min"]),
                                      {"limit_min": T["camera_offline_min"]}))
            if dur >= 5:
                acc = [e for ass in of_system(ctx, "ACCESS") if ass["location_id"] == a["location_id"]
                       for e in of(ctx, ass["id"], "ACCESS_GRANTED", "ACCESS_DENIED", "DOOR_FORCED", "DOOR_HELD_OPEN")
                       if off["ts"] <= e["ts"] <= end_t and not is_occupied(e["ts"])]
                if acc:
                    acc.sort(key=lambda e: e["ts"])
                    cards = sorted({e["details"].get("card_id") for e in acc if e["details"].get("card_id")})
                    facts = {"camera_offline_min": round(dur, 1), "access_events": len(acc),
                             "distinct_cards": len(cards), "after_hours": True, "zone": a["zone"]}
                    out.append(make_alert("SEC_BLIND_SPOT_ACCESS", "CRITICAL", a, evs + acc,
                                          f"{len(acc)} door event(s) at {a['zone']} outside working hours while "
                                          f"{a['name']} was offline ({dur:.0f} min).",
                                          facts, acc[0]["ts"]))
        if a["zone"] == "Server Room":
            motion = [e for e in of(ctx, a["id"], "MOTION_DETECTED") if e["details"].get("after_hours")]
            for grp in chain(motion, T["motion_burst_window_min"]):
                wmax, detect = window_stats(grp, T["motion_burst_window_min"], T["motion_burst_count"])
                if wmax >= T["motion_burst_count"]:
                    out.append(make_alert("CCTV_AFTER_HOURS_MOTION", "WARNING", a, grp,
                                          f"{a['name']} ({a['zone']}): {len(grp)} motion events outside working hours.",
                                          {"motion_events": len(grp), "max_in_window": wmax,
                                           "window_min": T["motion_burst_window_min"], "zone": a["zone"]},
                                          detect, {"count": T["motion_burst_count"],
                                                   "window_min": T["motion_burst_window_min"]}))
    return out


def rule_fire(ctx):
    out = []
    for a in of_system(ctx, "FIRE_ALARM"):
        for s in of(ctx, a["id"], "SMOKE_DETECTED"):
            reset = later(ctx, a["id"], s["ts"], "ALARM_RESET")
            facts = {"open": reset is None, "detector": s["details"].get("detector")}
            evs = [s] + ([reset] if reset else [])
            if reset:
                facts["reset_after_min"] = round(minutes(s["ts"], reset["ts"]), 1)
            out.append(make_alert("FIRE_SMOKE", "CRITICAL", a, evs,
                                  f"{a['name']} ({a['zone']}): smoke detected"
                                  f"{'' if reset else ' and the alarm has not been reset'}.", facts, s["ts"]))

        def pct(e):
            return e["value"] if e["event_type"] == "BATTERY_LOW" else e["details"].get("battery_pct")
        low = [e for e in of(ctx, a["id"], "PANEL_HEARTBEAT", "BATTERY_LOW")
               if (pct(e) is not None) and (e["event_type"] == "BATTERY_LOW" or pct(e) < T["battery_warn"])]
        for grp in chain(low, T["battery_cluster_gap_min"]):
            lowest = min(pct(e) for e in grp)
            facts = {"min_battery_pct": round(lowest, 1), "readings": len(grp),
                     "mains_lost": any(e["details"].get("mains_ok") is False for e in grp)}
            sev = "CRITICAL" if lowest < T["battery_crit"] else "WARNING"
            summary = (f"{a['name']} ({a['zone']}): battery fell to {lowest:.1f}%"
                       f"{' with mains power lost' if facts['mains_lost'] else ''}.")
            out.append(make_alert("FIRE_BATTERY_LOW", sev, a, grp, summary, facts, grp[0]["ts"],
                                  {"warn_pct": T["battery_warn"], "crit_pct": T["battery_crit"]},
                                  key=lambda e: -(pct(e) if pct(e) is not None else 0)))
    return out


def robust_stats(vals):
    m = median(vals)
    return m, median([abs(v - m) for v in vals])


def rule_anomaly(ctx, system, types, rule, min_dev, unit):
    out = []
    n = T["anomaly_sustain"]
    for a in of_system(ctx, system):
        reads = of(ctx, a["id"], *types, need_value=True)
        groups = {True: [], False: []}
        for e in reads:
            groups[is_occupied(e["ts"])].append(e["value"])
        stats = {reg: robust_stats(v) for reg, v in groups.items() if len(v) >= T["anomaly_min_baseline"]}
        zs = {}
        for e in reads:
            st = stats.get(is_occupied(e["ts"]))
            if st:
                m, mad = st
                zs[e["id"]] = (0.6745 * (e["value"] - m) / max(mad, 1e-6), e["value"] - m, m, mad)
        for sign, direction in ((1, "high"), (-1, "low")):
            def flagged(e, sign=sign):
                z = zs.get(e["id"])
                return bool(z) and z[0] * sign >= T["anomaly_z"] and abs(z[1]) >= min_dev
            for run in runs(reads, flagged, T["reading_gap_min"], n):
                peak = max(run, key=lambda e: sign * zs[e["id"]][0])
                z, dev, m, mad = zs[peak["id"]]
                ff = related_filter_fault(ctx, a["id"], run[0]["ts"], run[-1]["ts"]) if system == "HVAC" else []
                facts = {"direction": direction, "peak_value": round(peak["value"], 2), "unit": unit,
                         "baseline_median": round(m, 2), "baseline_mad": round(mad, 3),
                         "peak_robust_z": round(z, 1), "readings": len(run),
                         "duration_min": round(minutes(run[0]["ts"], run[-1]["ts"]), 1),
                         "regime": "working hours" if is_occupied(peak["ts"]) else "off hours",
                         "zone": a["zone"]}
                if system == "HVAC":
                    facts["fan_off"] = any(e["details"].get("fan_on") is False for e in run)
                    facts["filter_fault"] = bool(ff)
                    facts["sustained"] = len(run) >= 3
                summary = (f"{a['name']} ({a['zone']}): {direction} deviation, peak {peak['value']:.1f} {unit} vs a "
                           f"normal {m:.1f} {unit} for {facts['regime']} (robust z = {z:.1f}) over {len(run)} readings.")
                out.append(make_alert(rule, "WARNING", a, run + ff, summary, facts, run[n - 1]["ts"],
                                      {"robust_z": T["anomaly_z"], "min_deviation": min_dev, "unit": unit},
                                      key=lambda e: abs(zs.get(e["id"], (0,))[0])))
    return out


RULE_FUNCS = [
    rule_hvac_temp, rule_hvac_filter, rule_net_link, rule_net_bandwidth, rule_access, rule_cctv, rule_fire,
    lambda ctx: rule_anomaly(ctx, "HVAC", TEMP_TYPES, "HVAC_TEMP_ANOMALY", T["anomaly_min_dev_temp"], "°C"),
    lambda ctx: rule_anomaly(ctx, "NETWORK", ("BANDWIDTH_USAGE",), "NET_BANDWIDTH_ANOMALY", T["anomaly_min_dev_bw"], "Mbps"),
]


def run_engine(cur, start, end):
    import db_adapter                      # unified schema (anzma_schema_and_seed.sql); the old load_context below is legacy
    ctx = db_adapter.load_context(cur, end, cfg.HISTORY_DAYS)
    alerts = []
    for fn in RULE_FUNCS:
        alerts.extend(fn(ctx))
    start = start.astimezone(timezone.utc)
    alerts = [a for a in alerts if start < a["first_ts"] <= ctx["end"]]
    alerts.sort(key=lambda a: (a["first_ts"], a["asset_id"], a["rule_id"]))
    return alerts


def store_alerts(cur, alerts, start, end):
    cur.execute("DELETE FROM alerts WHERE first_ts > %s AND first_ts <= %s", (start, end))
    if alerts:
        execute_values(cur, """INSERT INTO alerts
            (rule_id, detection_method, severity, asset_id, title, summary, first_ts, last_ts, detected_ts, evidence)
            VALUES %s""",
                       [(a["rule_id"], a["detection_method"], a["severity"], a["asset_id"], a["title"], a["summary"],
                         a["first_ts"], a["last_ts"], a["detected_ts"], Json(a["evidence"])) for a in alerts])
    return len(alerts)

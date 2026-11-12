"""Fixed, verified answers for the six demo questions of the chat widget.

Every number comes from a SQL query (or from the same functions the dashboard uses), never from the language model,
so the chat and the dashboard always agree. "Now" is the latest stored record, exactly like the dashboard.
try_answer() returns a chat response dict, or None when the question is not one of the six (the normal chat
flow then continues).
"""
import logging
from datetime import timedelta

from app import dashboard_core_pg as core
from reference_data import SYSTEM_CODES

log = logging.getLogger("chat_facts")

NAMES = {"HVAC": "HVAC", "NETWORKING": "Network", "ACCESS_CONTROL": "Access control",
         "CCTV": "CCTV", "FIRE_ALARM": "Fire alarm"}
NAMES_AR = {"HVAC": "التكييف", "NETWORKING": "الشبكة", "ACCESS_CONTROL": "التحكم بالدخول",
            "CCTV": "الكاميرات", "FIRE_ALARM": "إنذار الحريق"}
MAX_HOURS = 168
# words that mean "tell me about this system" (checked after the six fixed questions)
SYSTEM_WORDS = {
    "ACCESS_CONTROL": ("access control", "access-control", "access", "اكسيس", "أكسيس", "الاكسيس", "كونترول",
                       "التحكم بالدخول", "التحكم في الدخول"),
    "HVAC": ("hvac", "تكييف", "التكييف"),
    "FIRE_ALARM": ("fire alarm", "fire", "حريق", "الحريق"),
    "NETWORKING": ("network", "الشبكة", "شبكة"),
    "CCTV": ("cctv",),
}


# ----------------------------------------------------------------------------------------------- helpers
def _has(low, *words):
    return any(w in low for w in words)


def _intent(low):
    if _has(low, "building status", "حالة المبنى", "حالة المبني", "وضع المبنى"):
        return "status"
    if _has(low, "cctv") and _has(low, "availab", "percent", "%", "توفر", "نسبة"):
        return "cctv"
    if (_has(low, "network") and _has(low, "issue", "problem", "fault", "down")) or \
            _has(low, "مشاكل الشبكة", "مشكلة الشبكة", "مشاكل في الشبكة"):
        return "system:NETWORKING"
    if _has(low, "repair priorit", "priorities", "أولويات", "اولويات"):
        return "repair"
    if _has(low, "energy", "power consumption", "consumption", "الطاقة", "استهلاك"):
        return "energy"
    if _has(low, "recent fault", "recent faults", "الأعطال", "اعطال", "أعطال"):
        return "faults"
    for code, words in SYSTEM_WORDS.items():
        if _has(low, *words):
            return "system:" + code
    return None


def _reply(answer, lang, sources, hours):
    return {"answer": answer, "language": lang, "source": "DATABASE", "grounding": {"passed": True},
            "sources": sources, "data_sufficient": True, "window": {"hours": hours},
            "session_state": {"last_assets": []}}


def _window(conn, hours):
    """[start, end_exclusive) of `hours` hours ending at the latest stored record (same as the dashboard)."""
    first, last = core._data_extent(conn)
    return last - timedelta(hours=hours), last + timedelta(seconds=1), last


def _t(ts):
    return ts.strftime("%b %d, %H:%M UTC")


def _tr(lang, en, ar):
    return ar if lang == "ar" else en


def _label(code, lang):
    return (NAMES_AR if lang == "ar" else NAMES).get(code, code)


def _status_word(n, c, lang):
    if n == 0 or c["no_data"] == n:
        return _tr(lang, "Unknown", "غير معروف")
    if c["critical"]:
        return _tr(lang, "Critical", "حرج")
    if c["warning"] or c["no_data"]:
        return _tr(lang, "Warning", "تحذير")
    return _tr(lang, "Operational", "يعمل")


# ----------------------------------------------------------------------------------------------- answers

# fault event -> the event that clears it. Access-control events (door_forced, access_denied, ...) have no
# recovery event type in the schema, so they are point-in-time (historical) records, never "active".
PAIRS = (("hvac_fault", "hvac_recovered"), ("fire_alarm_triggered", "alarm_cleared"),
         ("link_down", "link_up"), ("camera_offline", "camera_online"))


def _conditions(conn, end, code=None):
    """Latest fault/recovery event per asset and fault type, as of `end`.
    active = the latest of the two is the fault; otherwise it recovered. Returns None if the query fails, so the
    caller can simply leave this section out instead of breaking the whole answer."""
    vals = ", ".join(f"('{f}', '{r}')" for f, r in PAIRS)         # constants only, never user input
    try:
        return conn.execute(f"""
            WITH pairs(fault, recov) AS (VALUES {vals}),
            latest AS (
                SELECT DISTINCT ON (er.asset_id, p.fault) er.asset_id, p.fault, et.name AS ev, er.ts
                FROM event_records er
                JOIN event_types et ON et.event_type_id = er.event_type_id
                JOIN pairs p ON et.name = p.fault OR et.name = p.recov
                WHERE er.ts < %(end)s
                ORDER BY er.asset_id, p.fault, er.ts DESC)
            SELECT a.name AS asset, st.code AS system, l.fault, (l.ev = l.fault) AS active, l.ts
            FROM latest l
            JOIN assets a ON a.asset_id = l.asset_id
            JOIN system_types st ON st.system_type_id = a.system_type_id
            WHERE (%(code)s::text IS NULL OR st.code = %(code)s::text)
            ORDER BY (l.ev = l.fault) DESC, l.ts DESC""", {"end": end, "code": code}).fetchall()
    except Exception:
        log.exception("chat_facts: condition query failed")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def _condition_lines(rows, lang, with_system=True, limit=5):
    out = []
    for r in [x for x in rows if x["active"]][:limit]:
        who = f"{r['asset']} ({_label(r['system'], lang)})" if with_system else r["asset"]
        out.append(_tr(lang, f"  - ACTIVE: {who}: {r['fault']} since {_t(r['ts'])}",
                       f"  - نشط: {who}: {r['fault']} منذ {_t(r['ts'])}"))
    for r in [x for x in rows if not x["active"]][:limit]:
        who = f"{r['asset']} ({_label(r['system'], lang)})" if with_system else r["asset"]
        out.append(_tr(lang, f"  - RECOVERED: {who}: {r['fault']} cleared at {_t(r['ts'])}",
                       f"  - تعافى: {who}: {r['fault']} تم الحل في {_t(r['ts'])}"))
    return out


def _status(conn, lang, hours, p, last):
    lines, srcs = [_tr(lang, f"Building status (last {hours} h, until {_t(last)}):",
                       f"حالة المبنى (آخر {hours} ساعة حتى {_t(last)}):")], []
    for i, code in enumerate(SYSTEM_CODES, 1):
        params = {"system": code, "building": None, "start": p[0], "end": p[1]}
        n = core._monitored_assets(conn, params)
        c = core._asset_status_counts(conn, params)
        word = _status_word(n, c, lang)
        detail = _tr(lang,
                     f"{n} assets: {c['normal']} ok, {c['warning']} warning, {c['critical']} critical, {c['no_data']} no data",
                     f"{n} أصول: {c['normal']} سليم، {c['warning']} تحذير، {c['critical']} حرج، {c['no_data']} بلا بيانات")
        lines.append(f"- {_label(code, lang)}: {word} ({detail}) [Q{i}]")
        srcs.append({"ref": f"Q{i}", "text": f"Dashboard status counts for {code}: {detail}"})
    inc = conn.execute("""SELECT count(*) FILTER (WHERE status <> 'resolved') AS open_n,
                                 count(*) FILTER (WHERE status <> 'resolved' AND severity = 'critical') AS crit_n
                          FROM incidents""").fetchone()
    q = len(SYSTEM_CODES) + 1
    lines.append(_tr(lang, f"- Open incidents: {inc['open_n']} ({inc['crit_n']} critical) [Q{q}]",
                     f"- الحوادث المفتوحة: {inc['open_n']} ({inc['crit_n']} حرجة) [Q{q}]"))
    srcs.append({"ref": f"Q{q}", "text": f"SELECT ... FROM incidents WHERE status <> 'resolved' -> {inc['open_n']} open, "
                                         f"{inc['crit_n']} critical"})
    conds = _conditions(conn, p[1])
    if conds is not None:
        q += 1
        n_act = sum(1 for r in conds if r["active"])
        lines.append(_tr(lang, f"- Active faults now: {n_act}; recovered: {len(conds) - n_act} [Q{q}]",
                         f"- أعطال نشطة الآن: {n_act}؛ تعافت: {len(conds) - n_act} [Q{q}]"))
        lines += _condition_lines(conds, lang)
        lines.append(_tr(lang, "Note: the system status above reflects events inside the selected window; ACTIVE / "
                               "RECOVERED reflects the latest fault or recovery event of each asset.",
                         "ملاحظة: حالة الأنظمة أعلاه تعكس أحداث الفترة المحددة؛ نشط / تعافى يعكس آخر حدث عطل أو تعافٍ لكل أصل."))
        srcs.append({"ref": f"Q{q}", "text": "Latest fault/recovery event per asset (hvac_fault/hvac_recovered, "
                                             "fire_alarm_triggered/alarm_cleared, link_down/link_up, "
                                             f"camera_offline/camera_online) -> {len(conds)} asset conditions"})
    return "\n".join(lines), srcs


def _faults(conn, lang, hours, p, last):
    rows = conn.execute("""
        SELECT er.ts, er.severity, et.name AS event_type, a.name AS asset, st.code AS system, l.name AS zone,
               CASE WHEN et.name IN ('hvac_fault', 'fire_alarm_triggered', 'link_down', 'camera_offline') THEN
                      CASE WHEN EXISTS (
                          SELECT 1 FROM event_records r2 JOIN event_types t2 ON t2.event_type_id = r2.event_type_id
                          WHERE r2.asset_id = er.asset_id AND r2.ts > er.ts AND r2.ts < %(end)s
                            AND t2.name = CASE et.name WHEN 'hvac_fault' THEN 'hvac_recovered'
                                                       WHEN 'fire_alarm_triggered' THEN 'alarm_cleared'
                                                       WHEN 'link_down' THEN 'link_up'
                                                       ELSE 'camera_online' END)
                           THEN 'recovered' ELSE 'active' END
                    ELSE 'historical' END AS state
        FROM event_records er
        JOIN event_types et ON et.event_type_id = er.event_type_id
        JOIN assets a ON a.asset_id = er.asset_id
        JOIN system_types st ON st.system_type_id = a.system_type_id
        JOIN locations l ON l.location_id = a.location_id
        WHERE er.ts >= %(start)s AND er.ts < %(end)s AND er.severity IN ('warning', 'critical')
          AND et.name <> 'heartbeat'
        ORDER BY er.ts DESC LIMIT 8""", {"start": p[0], "end": p[1]}).fetchall()
    if not rows:
        return (_tr(lang, f"No warning or critical events in the last {hours} h (until {_t(last)}) [Q1].",
                    f"لا توجد أحداث تحذير أو حرجة في آخر {hours} ساعة (حتى {_t(last)}) [Q1]."),
                [{"ref": "Q1", "text": "SELECT ... FROM event_records WHERE severity IN ('warning','critical') -> 0 rows"}])
    lines = [_tr(lang, f"Recent faults (last {hours} h, until {_t(last)}), newest first:",
                 f"آخر الأعطال (آخر {hours} ساعة حتى {_t(last)})، الأحدث أولًا:")]
    for r in rows:
        lines.append(f"- {_t(r['ts'])} · {r['severity'].upper()} · {r['state'].upper()} · {r['event_type']} · "
                     f"{r['asset']} ({_label(r['system'], lang)}, {r['zone']}) [Q1]")
    lines.append(_tr(lang, "State: ACTIVE = no recovery event after it; RECOVERED = a recovery event followed; "
                           "HISTORICAL = a point-in-time event with no recovery type (e.g. access events).",
                     "الحالة: نشط = لا يوجد حدث تعافٍ بعده؛ تعافى = تبعه حدث تعافٍ؛ تاريخي = حدث لحظي بلا نوع تعافٍ."))
    return "\n".join(lines), [{"ref": "Q1", "text": f"SELECT ... FROM event_records WHERE severity IN ('warning','critical') "
                                                   f"AND event type <> heartbeat -> {len(rows)} rows (max 8)"}]


def _cctv(conn, lang, hours, p, last):
    rows = conn.execute("""
        WITH cams AS (
            SELECT a.asset_id, a.name FROM assets a
            JOIN system_types st ON st.system_type_id = a.system_type_id WHERE st.code = 'CCTV'),
        rep AS (
            SELECT asset_id FROM telemetry_records WHERE ts >= %(start)s AND ts < %(end)s
            UNION
            SELECT er.asset_id FROM event_records er JOIN event_types et ON et.event_type_id = er.event_type_id
            WHERE et.name = 'heartbeat' AND er.ts >= %(start)s AND er.ts < %(end)s),
        lastev AS (
            SELECT DISTINCT ON (er.asset_id) er.asset_id, et.name AS ev
            FROM event_records er JOIN event_types et ON et.event_type_id = er.event_type_id
            WHERE et.name IN ('camera_offline', 'camera_online') AND er.ts < %(end)s
            ORDER BY er.asset_id, er.ts DESC)
        SELECT c.name, (r.asset_id IS NOT NULL) AS reporting, COALESCE(l.ev = 'camera_offline', false) AS offline
        FROM cams c LEFT JOIN rep r ON r.asset_id = c.asset_id LEFT JOIN lastev l ON l.asset_id = c.asset_id
        ORDER BY c.name""", {"start": p[0], "end": p[1]}).fetchall()
    src = {"ref": "Q1", "text": "Cameras (CCTV assets): available = reporting telemetry/heartbeat in the window and "
                                "not last seen as camera_offline"}
    if not rows:
        return _tr(lang, "There are no CCTV cameras in the database, so availability cannot be calculated [Q1].",
                   "لا توجد كاميرات في قاعدة البيانات، لذلك لا يمكن حساب نسبة التوفر [Q1]."), [src]
    ok = [r for r in rows if r["reporting"] and not r["offline"]]
    bad = [r for r in rows if not (r["reporting"] and not r["offline"])]
    pct = round(100.0 * len(ok) / len(rows), 1)
    lines = [_tr(lang, f"CCTV availability: {pct}% ({len(ok)} of {len(rows)} cameras available, last {hours} h) [Q1]",
                 f"توفر الكاميرات: {pct}% ({len(ok)} من {len(rows)} كاميرات متاحة، آخر {hours} ساعة) [Q1]")]
    for r in bad:
        why = _tr(lang, "offline", "غير متصلة") if r["offline"] else _tr(lang, "no data in the window", "بلا بيانات في الفترة")
        lines.append(f"- {r['name']}: {why}")
    return "\n".join(lines), [src]


def _system(conn, lang, hours, p, last, code):
    """Status report for one system: counts like the dashboard tile, events by type in the window, open incidents."""
    name = _label(code, lang)
    params = {"system": code, "building": None, "start": p[0], "end": p[1]}
    n = core._monitored_assets(conn, params)
    c = core._asset_status_counts(conn, params)
    ev = conn.execute("""
        SELECT et.name AS event_type, er.severity, count(*) AS n, max(er.ts) AS last_ts
        FROM event_records er
        JOIN event_types et ON et.event_type_id = er.event_type_id
        JOIN assets a ON a.asset_id = er.asset_id
        JOIN system_types st ON st.system_type_id = a.system_type_id
        WHERE st.code = %(system)s AND et.name <> 'heartbeat' AND er.ts >= %(start)s AND er.ts < %(end)s
        GROUP BY et.name, er.severity ORDER BY n DESC, et.name LIMIT 8""",
                      {"system": code, "start": p[0], "end": p[1]}).fetchall()
    inc = conn.execute("""
        SELECT DISTINCT i.incident_id, i.title, i.severity, i.status, i.last_ts
        FROM incidents i JOIN alerts al ON al.incident_id = i.incident_id
        JOIN assets a ON a.asset_id = al.asset_id
        JOIN system_types st ON st.system_type_id = a.system_type_id
        WHERE st.code = %(system)s AND i.status <> 'resolved'
        ORDER BY i.last_ts DESC LIMIT 6""", {"system": code}).fetchall()
    lines = [_tr(lang, f"{name}: {_status_word(n, c, lang)} - {n} assets: {c['normal']} ok, {c['warning']} warning, "
                       f"{c['critical']} critical, {c['no_data']} no data (last {hours} h, until {_t(last)}) [Q1]",
                 f"{name}: {_status_word(n, c, lang)} - {n} أصول: {c['normal']} سليم، {c['warning']} تحذير، "
                 f"{c['critical']} حرج، {c['no_data']} بلا بيانات (آخر {hours} ساعة حتى {_t(last)}) [Q1]")]
    if ev:
        lines.append(_tr(lang, "Events in the window (excluding heartbeats):", "الأحداث في الفترة (بدون heartbeat):"))
        lines += [f"- {r['event_type']} · {r['severity']} · x{r['n']} · {_t(r['last_ts'])} [Q2]" for r in ev]
    else:
        lines.append(_tr(lang, f"No events other than heartbeats were recorded for {name} in the window [Q2].",
                         f"لم تُسجَّل أحداث (غير heartbeat) لـ {name} في الفترة [Q2]."))
    conds = _conditions(conn, p[1], code)
    if code == "ACCESS_CONTROL":
        lines.append(_tr(lang, "Access events (door_forced, access_denied, ...) are point-in-time records with no "
                               "recovery event, so they are historical, not active.",
                         "أحداث الدخول (door_forced، access_denied، ...) أحداث لحظية بلا حدث تعافٍ، لذلك هي تاريخية وليست نشطة."))
    elif conds:
        n_act = sum(1 for r in conds if r["active"])
        lines.append(_tr(lang, f"Current condition: {n_act} active, {len(conds) - n_act} recovered [Q4]",
                         f"الحالة الحالية: {n_act} نشط، {len(conds) - n_act} تعافى [Q4]"))
        lines += _condition_lines(conds, lang, with_system=False)
    if inc:
        lines.append(_tr(lang, "Open incidents:", "الحوادث المفتوحة:"))
        lines += [f"- #{r['incident_id']} · {r['severity'].upper()} · {r['status']} · {r['title']} [Q3]" for r in inc]
    else:
        lines.append(_tr(lang, "No open incidents for this system [Q3].", "لا توجد حوادث مفتوحة لهذا النظام [Q3]."))
    return "\n".join(lines), [
        {"ref": "Q1", "text": f"Dashboard status counts for {code}"},
        {"ref": "Q2", "text": f"SELECT ... FROM event_records ({code}, not heartbeat) GROUP BY type, severity -> {len(ev)} rows"},
        {"ref": "Q3", "text": f"SELECT ... FROM incidents joined to alerts on {code} assets, not resolved -> {len(inc)} rows"},
        {"ref": "Q4", "text": f"Latest fault/recovery event per {code} asset -> {len(conds) if conds is not None else 'n/a'} rows"}]


def _repair(conn, lang, hours, p, last):
    rows = conn.execute("""
        SELECT i.incident_id, i.title, i.severity, i.status, l.name AS zone, i.last_ts
        FROM incidents i JOIN locations l ON l.location_id = i.location_id
        WHERE i.status <> 'resolved'
        ORDER BY CASE i.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                 CASE i.status WHEN 'open' THEN 0 ELSE 1 END, i.last_ts DESC
        LIMIT 6""").fetchall()
    src = {"ref": "Q1", "text": "SELECT ... FROM incidents WHERE status <> 'resolved' ORDER BY severity, status, "
                                f"last_ts DESC -> {len(rows)} rows (max 6)"}
    if not rows:
        return _tr(lang, "There are no open incidents, so there is nothing to repair [Q1].",
                   "لا توجد حوادث مفتوحة، لذلك لا يوجد ما يحتاج إصلاحًا [Q1]."), [src]
    lines = [_tr(lang, "Repair priorities (open incidents, most severe first):",
                 "أولويات الإصلاح (الحوادث المفتوحة، الأخطر أولًا):")]
    for k, r in enumerate(rows, 1):
        lines.append(f"{k}. #{r['incident_id']} · {r['severity'].upper()} · {r['status']} · {r['title']} "
                     f"({r['zone']}, {_t(r['last_ts'])}) [Q1]")
    lines.append(_tr(lang, "Ask \"How do I fix this?\" after opening an incident to see its approved playbook actions.",
                     "يمكنك فتح حادث ثم سؤالي \"ازاي نصلح ده؟\" لعرض الإجراءات المعتمدة."))
    return "\n".join(lines), [src]


def _energy(conn, lang, hours, p, last):
    pstart = p[0] - timedelta(hours=hours)
    r = conn.execute("""
        SELECT avg(tr.value) FILTER (WHERE tr.ts >= %(start)s AND tr.ts < %(end)s) AS cur_avg,
               count(*)      FILTER (WHERE tr.ts >= %(start)s AND tr.ts < %(end)s) AS cur_n,
               avg(tr.value) FILTER (WHERE tr.ts >= %(pstart)s AND tr.ts < %(start)s) AS prev_avg,
               count(*)      FILTER (WHERE tr.ts >= %(pstart)s AND tr.ts < %(start)s) AS prev_n
        FROM telemetry_records tr JOIN metric_types m ON m.metric_id = tr.metric_id
        WHERE m.name = 'power_kw' AND tr.ts >= %(pstart)s AND tr.ts < %(end)s""",
                     {"start": p[0], "end": p[1], "pstart": pstart}).fetchone()
    src = {"ref": "Q1", "text": f"SELECT avg(value) FROM telemetry_records WHERE metric = power_kw -> current window "
                                f"{r['cur_n']} readings, previous window {r['prev_n']} readings"}
    if not r["cur_n"] or not r["prev_n"] or not r["prev_avg"]:
        return (_tr(lang, "There is not enough power data to compare two periods of "
                          f"{hours} h: only HVAC reports power (power_kw), and one of the periods has no readings [Q1].",
                    f"لا توجد بيانات طاقة كافية لمقارنة فترتين كل منهما {hours} ساعة: فقط التكييف يسجل القدرة "
                    "(power_kw) وإحدى الفترتين بلا قراءات [Q1]."), [src])
    cur, prev = float(r["cur_avg"]), float(r["prev_avg"])
    change = round((cur - prev) / prev * 100.0, 1)
    direction = _tr(lang, "up" if change > 0 else "down" if change < 0 else "unchanged",
                    "ارتفع" if change > 0 else "انخفض" if change < 0 else "لم يتغير")
    return (_tr(lang,
                f"Energy (HVAC power, the only system that reports power): average {cur:.2f} kW in the last {hours} h vs "
                f"{prev:.2f} kW in the {hours} h before - {direction} {abs(change)}% [Q1].",
                f"الطاقة (قدرة التكييف، النظام الوحيد الذي يسجلها): المتوسط {cur:.2f} كيلوواط في آخر {hours} ساعة مقابل "
                f"{prev:.2f} في الـ {hours} ساعة السابقة - {direction} {abs(change)}% [Q1]."), [src])


HANDLERS = {"status": _status, "faults": _faults, "cctv": _cctv, "repair": _repair, "energy": _energy}


def try_answer(message_low, lang, hours=None):
    """Answer one of the six demo questions from SQL, or return None (caller continues the normal flow)."""
    kind = _intent(message_low)
    if kind is None:
        return None
    hours = hours if isinstance(hours, int) and 1 <= hours <= MAX_HOURS else 24
    try:
        with core.open_readonly() as conn:
            p = _window(conn, hours)
            if kind.startswith("system:"):
                text, sources = _system(conn, lang, hours, p, p[2], kind.split(":", 1)[1])
            else:
                text, sources = HANDLERS[kind](conn, lang, hours, p, p[2])
        return _reply(text, lang, sources, hours)
    except Exception:                      # never break the chat: fall back to the normal flow
        log.exception("chat_facts failed for intent %s", kind)
        return None
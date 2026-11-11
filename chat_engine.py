"""
chat_engine.py  (Day 5 - Conversational AI)

Pipeline for one question:
    1. classify   : question -> one of the 6 mandatory intents (keywords first, LLM only as a fallback)
    2. gather     : SQL collectors build numbered FACTS (F1, F2, ...) from the real database
    3. compose    : the local LLM (Ollama) phrases an answer from the facts only
    4. validate   : every number / time / date / citation in the answer must exist in the facts
    5. fallback   : if the LLM is down or fails validation twice, the facts themselves are the answer

Facts are written in the user's language (Arabic or English), so the fallback answer is already readable.
When the database cannot answer (e.g. no energy readings), data_sufficient=False and the answer says so.
"""
import json
import re
from datetime import timedelta, timezone

import ai_explainer as ai
from dashboard_api import (ACTIVE_FIRE_SQL, DOWNTIME_SQL, FAULT_TYPES, INCIDENT_TYPES,
                           OPENERS, STATUS_WINDOW_MIN, SYSTEMS)

# --------------------------------------------------------------------------- intents
_AR = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ة": "ه", "ى": "ي"})


def norm(s):
    s = re.sub(r"[\u064B-\u0652\u0640]", "", s)sorr
    return s.translate(_AR).lower()


# order matters: the most specific topics first
KEYWORDS = [
    ("energy", ["طاق", "كهرب", "استهلاك", "energy", "power", "kwh", "electric", "consumption"]),
    ("cameras", ["كامير", "camera", "cctv"]),
    ("network", ["شبك", "network", "انترنت", "bandwidth", "link", "switch", "سويتش"]),
    ("repair", ["اولوي", "اصلاح", "صيان", "priorit", "repair", "fix", "maintenance"]),
    ("faults", ["عطل", "اعطال", "عطال", "مشكل", "fault", "failure", "failing", "broken", "issue", "problem"]),
    ("kpi", ["kpi", "مؤشر", "نسبه", "نسبة", "توافر", "availability", "uptime", "percent", "performance", "اداء"]),
    ("history", ["متوسط", "average", "history", "historical", "تاريخ",
                 "حرار", "temp", "رطوب", "humid", "منع", "رفض", "denied", "دخول", "access", "badge"]),
    ("status", ["حال", "مبني", "status", "overview", "building", "health", "ملخص", "summary"]),
]
KEYWORDS = [(i, [norm(k) for k in ks]) for i, ks in KEYWORDS]
INTENT_NAMES = [i for i, _ in KEYWORDS]

SUGGESTED_QUESTIONS = [
    "What is the building status right now?",
    "What are the current faults?",
    "What is the camera status?",
    "What is the network status?",
    "What are the repair priorities?",
    "What is the energy consumption?",
]


EXTRA_QUESTIONS = [
    "What are the building KPIs?",
    "What was the average temperature yesterday?",
    "How many times was access denied in the last 3 days?",
    "ايه متوسط الحرارة امبارح؟",
    "كام مرة اتمنع حد من الدخول؟",
    "ايه توافر الكاميرات؟",
]

DEFAULT_HOURS = 72
_UNITS = {"ساع": 1, "hour": 1, "hr": 1, "يوم": 24, "ايام": 24, "day": 24, "اسبوع": 168, "week": 168}


def parse_hours(message, default=DEFAULT_HOURS):
    """Reads 'last 3 days', 'past week', 'اخر 12 ساعة' ... from the question; otherwise the default window."""
    m = re.search(r"(?:last|past|اخر|خلال)\s*(\d+)?\s*(ساع|hour|hr|يوم|ايام|day|اسبوع|week)", norm(message))
    if not m:
        return default
    return max(1, min(168, int(m.group(1) or 1) * _UNITS[m.group(2)]))


def wants_yesterday(message):
    n = norm(message)
    return any(k in n for k in ("امبارح", "امس", "yesterday"))


def detect_lang(text):
    return "ar" if re.search(r"[\u0600-\u06FF]", text) else "en"


def classify(message, use_llm=True):
    n = norm(message)
    for intent, kws in KEYWORDS:
        if any(k in n for k in kws):
            return intent
    if use_llm:
        try:
            raw = ai.call_ollama([
                {"role": "system", "content": "Classify the building-operations question into exactly one intent from: "
                 + ", ".join(INTENT_NAMES) + ", out_of_scope. Return JSON only: {\"intent\": \"...\"}"},
                {"role": "user", "content": message}])
            v = ai.parse_json(raw).get("intent")
            if v in INTENT_NAMES:
                return v
        except Exception:
            pass
    return "out_of_scope"


# --------------------------------------------------------------------------- facts
class Facts:
    def __init__(self, lang):
        self.lang, self.items = lang, []

    def add(self, ar, en, **data):
        ref = f"F{len(self.items) + 1}"
        self.items.append({"ref": ref, "text": ar if self.lang == "ar" else en, "data": data})
        return ref


def fmt(ts):
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def get_window(cur, hours):
    cur.execute("SELECT MAX(ts) AS m FROM events")
    end = cur.fetchone()["m"]
    if end is None:
        raise LookupError("no events in the database")
    return end - timedelta(hours=hours), end


def window_fact(f, hours, end):
    f.add(f"النافذة الزمنية: آخر {hours} ساعة حتى {fmt(end)} UTC (وقت آخر حدث مسجل).",
          f"Window: last {hours} h ending {fmt(end)} UTC (latest recorded event).", hours=hours, to=end)


def asset_states(cur, start, end):
    """Same status logic as dashboard_api.asset_health, plus the open-incident timestamp."""
    recent_start = max(start, end - timedelta(minutes=STATUS_WINDOW_MIN))
    cur.execute("""SELECT a.id, a.name, s.type AS system_type, l.building, l.floor, l.zone
                   FROM assets a JOIN systems s ON s.id = a.system_id
                   JOIN locations l ON l.id = a.location_id ORDER BY a.id""")
    assets = cur.fetchall()
    cur.execute("""SELECT DISTINCT ON (asset_id) asset_id, event_type, ts FROM events
                   WHERE event_type = ANY(%s) AND ts <= %s ORDER BY asset_id, ts DESC""", (INCIDENT_TYPES, end))
    latest = {r["asset_id"]: r for r in cur.fetchall()}
    cur.execute("""SELECT asset_id, event_type, COUNT(*) AS n, COUNT(*) FILTER (WHERE ts > %s) AS n_recent
                   FROM events WHERE ts > %s AND ts <= %s GROUP BY asset_id, event_type""",
                (recent_start, start, end))
    stats = {}
    for r in cur.fetchall():
        stats.setdefault(r["asset_id"], []).append(r)

    out = []
    for a in assets:
        rows = [r for r in stats.get(a["id"], []) if r["event_type"] in FAULT_TYPES[a["system_type"]]]
        recent = sum(r["n_recent"] for r in rows)
        inc = latest.get(a["id"])
        opener = OPENERS.get(inc["event_type"]) if inc else None
        status, reason, since = "OK", "no recent faults", None
        if opener:
            status = "CRITICAL" if opener[0] == "CRITICAL" else "WARNING"
            reason, since = f"{opener[1]} (open incident)", inc["ts"]
        elif recent:
            status, reason = "WARNING", f"{recent} fault event(s) in the last {STATUS_WINDOW_MIN} min"
        out.append({**a, "status": status, "reason": reason, "since": since,
                    "incident": inc["event_type"] if inc else None,
                    "recent_faults": recent, "faults": sum(r["n"] for r in rows)})
    return out


def alerts_fact(f, cur, start, end):
    cur.execute("SELECT COUNT(*) AS n FROM alerts")
    if cur.fetchone()["n"] == 0:
        f.add("محرك التنبيهات لم يتم تشغيله بعد، لذلك لا توجد تنبيهات مخزنة.",
              "The alert engine has not been run yet, so no alerts are stored.", stored=0, required=True)
        return
    cur.execute("""SELECT severity, COUNT(*) AS n FROM alerts
                   WHERE status = 'OPEN' AND first_ts > %s AND first_ts <= %s GROUP BY severity""", (start, end))
    d = {r["severity"]: r["n"] for r in cur.fetchall()}
    c, w, i = d.get("CRITICAL", 0), d.get("WARNING", 0), d.get("INFO", 0)
    f.add(f"التنبيهات المفتوحة: {c} حرجة، {w} تحذيرية، {i} معلوماتية.",
          f"Open alerts: {c} critical, {w} warning, {i} info.", critical=c, warning=w, info=i, required=True)


# --------------------------------------------------------------------------- collectors
def coverage(cur, f, system, start, end):
    """True if the system sent at least one event in the window; otherwise adds a caveat fact."""
    cur.execute("""SELECT COUNT(*) FILTER (WHERE e.ts > %s) AS n, MAX(e.ts) AS last_ts
                   FROM events e JOIN assets a ON a.id = e.asset_id JOIN systems s ON s.id = a.system_id
                   WHERE s.type = %s AND e.ts <= %s""", (start, system, end))
    r = cur.fetchone()
    if r["n"]:
        return True
    last = fmt(r["last_ts"]) + " UTC" if r["last_ts"] else "never"
    f.add(f"لم يصل أي حدث على الإطلاق من أصول {system} في هذه النافذة (آخر حدث مسجل: {last})، "
          f"فغياب حوادث الانقطاع لا يؤكد سلامة الأجهزة.",
          f"No events at all were received from {system} assets in this window (latest ever: {last}), "
          f"so the absence of incidents does not confirm the devices are healthy.", system=system, events=0)
    return False


def c_status(cur, hours, lang):
    start, end = get_window(cur, hours)
    f = Facts(lang)
    window_fact(f, hours, end)
    states = asset_states(cur, start, end)
    cur.execute("""SELECT s.type AS st, COUNT(*) AS n,
                          COUNT(*) FILTER (WHERE e.severity = 'CRITICAL') AS crit
                   FROM events e JOIN assets a ON a.id = e.asset_id JOIN systems s ON s.id = a.system_id
                   WHERE e.ts > %s AND e.ts <= %s GROUP BY s.type""", (start, end))
    ev = {r["st"]: r for r in cur.fetchall()}
    for st in SYSTEMS:
        grp = [a for a in states if a["system_type"] == st]
        if not grp:
            continue
        ok = sum(a["status"] == "OK" for a in grp)
        w = sum(a["status"] == "WARNING" for a in grp)
        c = sum(a["status"] == "CRITICAL" for a in grp)
        n, crit = ev.get(st, {}).get("n", 0), ev.get(st, {}).get("crit", 0)
        f.add(f"{st}: {len(grp)} أصل ({ok} OK، {w} WARNING، {c} CRITICAL)، و{n} حدث منها {crit} حرج.",
              f"{st}: {len(grp)} assets ({ok} OK, {w} WARNING, {c} CRITICAL), {n} events of which {crit} critical.",
              system=st, assets=len(grp), ok=ok, warning=w, critical=c, events=n, critical_events=crit)
    cur.execute(ACTIVE_FIRE_SQL, (end,))
    fire = cur.fetchone()["n"]
    f.add(f"إنذارات الحريق النشطة حالياً: {fire}.", f"Active fire alarms right now: {fire}.", active_fire_alarms=fire, required=True)
    bad = sorted([a for a in states if a["status"] != "OK"], key=lambda a: (a["status"] != "CRITICAL", a["name"]))
    for a in bad[:6]:
        since = f" منذ {fmt(a['since'])} UTC" if a["since"] else ""
        since_en = f" since {fmt(a['since'])} UTC" if a["since"] else ""
        f.add(f"{a['name']} ({a['zone']}): {a['status']} - {a['reason']}{since}.",
              f"{a['name']} ({a['zone']}): {a['status']} - {a['reason']}{since_en}.",
              asset=a["name"], status=a["status"], required=True)
    if len(bad) > 6:
        f.add(f"وفيه {len(bad) - 6} أصول أخرى غير سليمة لم تُعرض.",
              f"{len(bad) - 6} more assets are not OK and are not listed.", more=len(bad) - 6)
    if not bad:
        f.add("كل الأصول بحالة OK.", "All assets are OK.")
    alerts_fact(f, cur, start, end)
    return start, end, f.items, True


def c_faults(cur, hours, lang):
    start, end = get_window(cur, hours)
    f = Facts(lang)
    window_fact(f, hours, end)
    types = sorted({t for v in FAULT_TYPES.values() for t in v})
    cur.execute("""SELECT s.type AS st, a.name, e.event_type, COUNT(*) AS n, MAX(e.ts) AS last_ts,
                          COUNT(*) FILTER (WHERE e.severity = 'CRITICAL') AS crit
                   FROM events e JOIN assets a ON a.id = e.asset_id JOIN systems s ON s.id = a.system_id
                   WHERE e.ts > %s AND e.ts <= %s AND e.event_type = ANY(%s)
                   GROUP BY s.type, a.name, e.event_type
                   ORDER BY crit DESC, n DESC, last_ts DESC""", (start, end, types))
    rows = cur.fetchall()
    if not rows:
        f.add("لا توجد أحداث أعطال في هذه النافذة.", "No fault events in this window.", total=0)
    else:
        total = sum(r["n"] for r in rows)
        f.add(f"إجمالي أحداث الأعطال: {total} على {len({r['name'] for r in rows})} أصل.",
              f"Total fault events: {total} across {len({r['name'] for r in rows})} assets.", total=total)
        for r in rows[:8]:
            f.add(f"{r['name']} ({r['st']}): {r['event_type']} × {r['n']} (منها {r['crit']} حرج)، آخرها {fmt(r['last_ts'])} UTC.",
                  f"{r['name']} ({r['st']}): {r['event_type']} x {r['n']} ({r['crit']} critical), last {fmt(r['last_ts'])} UTC.",
                  asset=r["name"], event_type=r["event_type"], n=r["n"])
        if len(rows) > 8:
            f.add(f"وفيه {len(rows) - 8} مجموعات أعطال أخرى لم تُعرض.",
                  f"{len(rows) - 8} more fault groups are not listed.")
    open_inc = [a for a in asset_states(cur, start, end) if a["since"]]
    if open_inc:
        names = "، ".join(a["name"] for a in open_inc[:6])
        f.add(f"حوادث مفتوحة حالياً: {len(open_inc)} ({names}).",
              f"Open incidents right now: {len(open_inc)} ({names.replace('،', ',')}).", open_incidents=len(open_inc), required=True)
    alerts_fact(f, cur, start, end)
    return start, end, f.items, True


def c_cameras(cur, hours, lang):
    start, end = get_window(cur, hours)
    f = Facts(lang)
    window_fact(f, hours, end)
    cams = [a for a in asset_states(cur, start, end) if a["system_type"] == "CCTV"]
    if not cams:
        f.add("لا توجد كاميرات (CCTV) مسجلة في قاعدة البيانات.", "No CCTV cameras are registered in the database.", cameras=0)
        return start, end, f.items, False
    off = [a for a in cams if a["incident"] == "CAMERA_OFFLINE"]
    f.add(f"إجمالي الكاميرات: {len(cams)}، منها {len(off)} غير متصلة حالياً.",
          f"Total cameras: {len(cams)}, of which {len(off)} are offline now.", cameras=len(cams), offline=len(off))
    pct = round(100.0 * (len(cams) - len(off)) / len(cams), 1)
    f.add(f"توافر الكاميرات الحالي: {pct}% ({len(cams) - len(off)} من {len(cams)} متصلة).",
          f"Current camera availability: {pct}% ({len(cams) - len(off)} of {len(cams)} online).",
          availability=pct, online=len(cams) - len(off), required=True)
    for a in off[:8]:
        f.add(f"{a['name']} ({a['zone']}، الدور {a['floor']}): غير متصلة منذ {fmt(a['since'])} UTC.",
              f"{a['name']} ({a['zone']}, floor {a['floor']}): offline since {fmt(a['since'])} UTC.", asset=a["name"])
    cur.execute("""SELECT COUNT(*) AS n FROM events e JOIN assets a ON a.id = e.asset_id
                   JOIN systems s ON s.id = a.system_id
                   WHERE s.type = 'CCTV' AND e.event_type = 'CAMERA_OFFLINE' AND e.ts > %s AND e.ts <= %s""",
                (start, end))
    n = cur.fetchone()["n"]
    f.add(f"حوادث انقطاع الكاميرات في النافذة: {n}.", f"Camera-offline incidents in the window: {n}.", offline_events=n)
    return start, end, f.items, coverage(cur, f, "CCTV", start, end)


def net_availability(cur, start, end, hours, n_assets):
    cur.execute(DOWNTIME_SQL, (start, end))
    down = cur.fetchone()["down"]
    return round(max(0.0, 100.0 * (1 - down / (hours * 3600 * n_assets))), 3), down


def c_network(cur, hours, lang):
    start, end = get_window(cur, hours)
    f = Facts(lang)
    window_fact(f, hours, end)
    states = [a for a in asset_states(cur, start, end) if a["system_type"] == "NETWORK"]
    if not states:
        f.add("لا توجد أصول شبكة في قاعدة البيانات.", "No network assets in the database.", assets=0)
        return start, end, f.items, False
    cur.execute(DOWNTIME_SQL, (start, end))
    down = cur.fetchone()["down"]
    avail = round(max(0.0, 100.0 * (1 - down / (hours * 3600 * len(states)))), 3)
    f.add(f"توافر الشبكة: {avail}% على {len(states)} أصل شبكة، وإجمالي زمن الانقطاع {round(down)} ثانية.",
          f"Network availability: {avail}% across {len(states)} network assets, total downtime {round(down)} s.",
          availability=avail, assets=len(states), downtime_sec=round(down))
    cur.execute("""SELECT COUNT(*) AS n FROM events e JOIN assets a ON a.id = e.asset_id
                   JOIN systems s ON s.id = a.system_id
                   WHERE s.type = 'NETWORK' AND e.event_type = 'LINK_DOWN' AND e.ts > %s AND e.ts <= %s""",
                (start, end))
    n = cur.fetchone()["n"]
    f.add(f"حوادث LINK_DOWN في النافذة: {n}.", f"LINK_DOWN incidents in the window: {n}.", link_down=n)
    for a in [a for a in states if a["incident"] == "LINK_DOWN"][:6]:
        f.add(f"{a['name']} ({a['zone']}): الرابط ساقط منذ {fmt(a['since'])} UTC.",
              f"{a['name']} ({a['zone']}): link down since {fmt(a['since'])} UTC.", asset=a["name"])
    cur.execute("""SELECT COUNT(*) AS n, AVG(value) AS avg_v, MAX(value) AS max_v FROM events
                   WHERE event_type = 'BANDWIDTH_USAGE' AND ts > %s AND ts <= %s""", (start, end))
    b = cur.fetchone()
    if b["n"]:
        avg_v, max_v = round(float(b["avg_v"]), 1), round(float(b["max_v"]), 1)
        f.add(f"استهلاك النطاق: متوسط {avg_v} Mbps وذروة {max_v} Mbps.",
              f"Bandwidth: average {avg_v} Mbps, peak {max_v} Mbps.", avg=avg_v, peak=max_v)
    else:
        f.add("لا توجد قراءات BANDWIDTH_USAGE في النافذة.", "No BANDWIDTH_USAGE readings in the window.")
    return start, end, f.items, coverage(cur, f, "NETWORK", start, end)


def c_repair(cur, hours, lang):
    start, end = get_window(cur, hours)
    f = Facts(lang)
    window_fact(f, hours, end)
    states = asset_states(cur, start, end)
    cur.execute("""SELECT asset_id, COUNT(*) FILTER (WHERE severity = 'CRITICAL') AS c,
                          COUNT(*) FILTER (WHERE severity = 'WARNING') AS w
                   FROM alerts WHERE status = 'OPEN' AND first_ts > %s AND first_ts <= %s GROUP BY asset_id""",
                (start, end))
    al = {r["asset_id"]: r for r in cur.fetchall()}
    ranked = []
    for a in states:
        c, w = al.get(a["id"], {}).get("c", 0), al.get(a["id"], {}).get("w", 0)
        score = (100 if a["status"] == "CRITICAL" and a["since"] else 50 if a["since"] else 0) \
            + 20 * c + 8 * w + 3 * a["recent_faults"]
        if score > 0:
            ranked.append({**a, "score": score, "crit_alerts": c, "warn_alerts": w})
    ranked.sort(key=lambda a: (-a["score"], a["name"]))
    f.add("قاعدة الترتيب: حادث مفتوح حرج 100 نقطة، حادث مفتوح تحذيري 50، كل تنبيه حرج مفتوح 20، "
          "كل تنبيه تحذيري مفتوح 8، وكل عطل في آخر 60 دقيقة 3 نقاط.",
          "Ranking rule: open CRITICAL incident 100 points, open WARNING incident 50, each open critical alert 20, "
          "each open warning alert 8, each fault in the last 60 minutes 3 points.")
    if not ranked:
        f.add("لا توجد أصول تحتاج إصلاحاً حالياً.", "No assets currently need repair.", total=0)
        return start, end, f.items, True
    for i, a in enumerate(ranked[:5], 1):
        since = f"، منذ {fmt(a['since'])} UTC" if a["since"] else ""
        since_en = f", since {fmt(a['since'])} UTC" if a["since"] else ""
        f.add(f"الأولوية {i}: {a['name']} ({a['system_type']}، {a['zone']}) - {a['reason']}{since}؛ "
              f"{a['crit_alerts']} تنبيه حرج و{a['warn_alerts']} تحذيري مفتوح؛ النقاط {a['score']}.",
              f"Priority {i}: {a['name']} ({a['system_type']}, {a['zone']}) - {a['reason']}{since_en}; "
              f"{a['crit_alerts']} critical and {a['warn_alerts']} warning open alerts; score {a['score']}.",
              rank=i, asset=a["name"], score=a["score"])
    if len(ranked) > 5:
        f.add(f"وفيه {len(ranked) - 5} أصول أخرى بأولوية أقل.", f"{len(ranked) - 5} more assets have lower priority.")
    return start, end, f.items, True


def c_energy(cur, hours, lang):
    start, end = get_window(cur, hours)
    f = Facts(lang)
    window_fact(f, hours, end)
    cur.execute("""SELECT a.name, e.event_type, COUNT(*) AS n, AVG(e.value) AS avg_v, MAX(e.value) AS max_v,
                          MIN(e.unit) AS unit
                   FROM events e JOIN assets a ON a.id = e.asset_id
                   WHERE e.ts > %s AND e.ts <= %s
                     AND (e.event_type ~* 'ENERGY|POWER|KWH|CONSUMPTION' OR lower(e.unit) IN ('kwh','kw','wh','w'))
                   GROUP BY a.name, e.event_type ORDER BY avg_v DESC NULLS LAST""", (start, end))
    rows = cur.fetchall()
    if not rows:
        cur.execute("SELECT DISTINCT event_type FROM events ORDER BY 1")
        types = ", ".join(r["event_type"] for r in cur.fetchall())
        f.add("قاعدة البيانات لا تحتوي على أي قراءات طاقة أو كهرباء (لا أحداث ENERGY/POWER ولا وحدات kWh/kW)، "
              "فلا يمكن الإجابة عن استهلاك الطاقة بدون تخمين.",
              "The database contains no energy or power readings (no ENERGY/POWER events and no kWh/kW units), "
              "so energy consumption cannot be answered without guessing.", rows=0)
        f.add(f"أنواع الأحداث المتاحة فعلياً: {types}.", f"Event types actually available: {types}.")
        return start, end, f.items, False
    for r in rows[:8]:
        f.add(f"{r['name']} ({r['event_type']}): {r['n']} قراءة، متوسط {round(float(r['avg_v']), 2)} {r['unit'] or ''}، "
              f"أقصى {round(float(r['max_v']), 2)}.",
              f"{r['name']} ({r['event_type']}): {r['n']} readings, average {round(float(r['avg_v']), 2)} {r['unit'] or ''}, "
              f"max {round(float(r['max_v']), 2)}.", asset=r["name"])
    tot = sum(r["n"] for r in rows)
    cur_avg = sum(float(r["avg_v"] or 0) * r["n"] for r in rows) / tot
    unit = next((r["unit"] for r in rows if r["unit"]), "") or ""
    cur.execute("""SELECT COUNT(*) AS n, AVG(e.value) AS avg_v FROM events e
                   WHERE e.ts > %s AND e.ts <= %s
                     AND (e.event_type ~* 'ENERGY|POWER|KWH|CONSUMPTION' OR lower(e.unit) IN ('kwh','kw','wh','w'))""",
                (start - timedelta(hours=hours), start))
    prev = cur.fetchone()
    if prev["n"] and prev["avg_v"]:
        pavg = float(prev["avg_v"])
        pct = round((cur_avg - pavg) / pavg * 100.0, 1)
        f.add(f"التغيّر في الاستهلاك: متوسط القراءة {round(cur_avg, 2)} {unit} في النافذة الحالية مقابل {round(pavg, 2)} {unit} "
              f"في النافذة السابقة بنفس الطول، أي تغيّر بنسبة {pct}%.",
              f"Consumption change: average reading {round(cur_avg, 2)} {unit} in the current window versus {round(pavg, 2)} {unit} "
              f"in the previous window of equal length, a change of {pct}%.", change_pct=pct, required=True)
    else:
        f.add("لا توجد قراءات طاقة في النافذة السابقة، فلا يمكن حساب التغيّر.",
              "There are no energy readings in the previous window, so the change cannot be computed.", required=True)
        return start, end, f.items, False
    return start, end, f.items, True


# --------------------------------------------------------------------------- KPIs and historical readings
def c_kpi(cur, hours, lang):
    start, end = get_window(cur, hours)
    f = Facts(lang)
    window_fact(f, hours, end)
    states = asset_states(cur, start, end)
    ok = sum(a["status"] == "OK" for a in states)
    pct = round(100.0 * ok / len(states), 1) if states else 0.0
    f.add(f"صحة الأصول: {pct}% سليمة ({ok} من {len(states)} أصل بحالة OK).",
          f"Asset health: {pct}% healthy ({ok} of {len(states)} assets OK).", health_pct=pct, required=True)
    cams = [a for a in states if a["system_type"] == "CCTV"]
    if cams:
        off = sum(a["incident"] == "CAMERA_OFFLINE" for a in cams)
        cp = round(100.0 * (len(cams) - off) / len(cams), 1)
        f.add(f"توافر الكاميرات الحالي: {cp}% ({len(cams) - off} من {len(cams)} متصلة).",
              f"Current camera availability: {cp}% ({len(cams) - off} of {len(cams)} online).", camera_availability=cp)
    net = [a for a in states if a["system_type"] == "NETWORK"]
    if net:
        av, down = net_availability(cur, start, end, hours, len(net))
        f.add(f"توافر الشبكة: {av}% وإجمالي زمن الانقطاع {round(down)} ثانية.",
              f"Network availability: {av}% with total downtime {round(down)} s.", network_availability=av)
    types = sorted({t for v in FAULT_TYPES.values() for t in v})
    cur.execute("""SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE severity = 'CRITICAL') AS crit
                   FROM events WHERE ts > %s AND ts <= %s AND event_type = ANY(%s)""", (start, end, types))
    r = cur.fetchone()
    f.add(f"أحداث الأعطال في النافذة: {r['n']} منها {r['crit']} حرج.",
          f"Fault events in the window: {r['n']}, of which {r['crit']} critical.", faults=r["n"])
    openi = sum(1 for a in states if a["since"])
    f.add(f"الحوادث المفتوحة حالياً: {openi}.", f"Open incidents right now: {openi}.", open_incidents=openi, required=True)
    cur.execute(ACTIVE_FIRE_SQL, (end,))
    fire = cur.fetchone()["n"]
    f.add(f"إنذارات الحريق النشطة حالياً: {fire}.", f"Active fire alarms right now: {fire}.", active_fire_alarms=fire)
    alerts_fact(f, cur, start, end)
    ok_cov = True
    for system in ("CCTV", "NETWORK"):
        if any(a["system_type"] == system for a in states):
            ok_cov = coverage(cur, f, system, start, end) and ok_cov
    return start, end, f.items, ok_cov


# metric key, question keywords, event-type pattern, kind
METRICS = [
    ("access_denied", ["منع", "رفض", "denied", "reject", "forbid", "blocked"], r"DENIED|REJECT|FORBID|INVALID", "count"),
    ("access", ["دخول", "access", "badge", "باب", "door"], r"ACCESS|BADGE|DOOR", "count"),
    ("temperature", ["حرار", "temp"], r"TEMPERATURE_READING", "value"),
    ("humidity", ["رطوب", "humid"], r"HUMID", "value"),
    ("bandwidth", ["bandwidth", "نطاق", "سرعه"], r"BANDWIDTH", "value"),
]
METRICS = [(k, [norm(w) for w in ws], pat, kind) for k, ws, pat, kind in METRICS]


def c_history(cur, hours, lang, question=""):
    start, end = get_window(cur, hours)
    f = Facts(lang)
    if wants_yesterday(question):
        day0 = end.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end, hours = day0 - timedelta(days=1), day0, 24
        d = start.strftime("%Y-%m-%d")
        f.add(f"الفترة: امبارح ({d}) بتوقيت UTC، محسوبة نسبةً ليوم آخر حدث مسجل.",
              f"Period: yesterday ({d}) UTC, relative to the day of the latest recorded event.", hours=24)
    else:
        window_fact(f, hours, end)
    n = norm(question)
    metric = next((m for m in METRICS if any(k in n for k in m[1])), None)
    cur.execute("SELECT event_type, COUNT(*) AS n FROM events GROUP BY event_type ORDER BY event_type")
    all_types = {r["event_type"]: r["n"] for r in cur.fetchall()}
    if metric is None:
        listing = ", ".join(all_types)
        f.add(f"لم أستطع تحديد القراءة المطلوبة. الأنواع المتاحة في قاعدة البيانات: {listing}.",
              f"I could not tell which reading you mean. Event types available in the database: {listing}.")
        return start, end, f.items, False
    key, _, pattern, kind = metric
    types = [t for t in all_types if re.search(pattern, t, re.I)]
    if not types:
        listing = ", ".join(all_types)
        f.add(f"قاعدة البيانات لا تحتوي على أي أحداث من نوع {key}، فلا يمكن الإجابة بدون تخمين.",
              f"The database contains no {key} events, so this cannot be answered without guessing.", rows=0)
        f.add(f"أنواع الأحداث المتاحة فعلياً: {listing}.", f"Event types actually available: {listing}.")
        return start, end, f.items, False
    tl = ", ".join(types)
    if kind == "count":
        cur.execute("""SELECT a.name, e.event_type, COUNT(*) AS n, MAX(e.ts) AS last_ts
                       FROM events e JOIN assets a ON a.id = e.asset_id
                       WHERE e.ts > %s AND e.ts <= %s AND e.event_type = ANY(%s)
                       GROUP BY a.name, e.event_type ORDER BY n DESC, a.name""", (start, end, types))
        rows = cur.fetchall()
        total = sum(r["n"] for r in rows)
        f.add(f"عدد أحداث {tl} في الفترة: {total} (حسب الأحداث المسجلة فقط).",
              f"Number of {tl} events in the period: {total} (recorded events only).", total=total, required=True)
        for r in rows[:5]:
            f.add(f"{r['name']} ({r['event_type']}): {r['n']} مرة، آخرها {fmt(r['last_ts'])} UTC.",
                  f"{r['name']} ({r['event_type']}): {r['n']} times, last {fmt(r['last_ts'])} UTC.", asset=r["name"])
        return start, end, f.items, True
    cur.execute("""SELECT a.name, e.event_type, COUNT(*) AS n, AVG(e.value) AS avg_v, MIN(e.value) AS min_v,
                          MAX(e.value) AS max_v, MIN(e.unit) AS unit
                   FROM events e JOIN assets a ON a.id = e.asset_id
                   WHERE e.ts > %s AND e.ts <= %s AND e.event_type = ANY(%s) AND e.value IS NOT NULL
                   GROUP BY a.name, e.event_type ORDER BY avg_v DESC""", (start, end, types))
    rows = cur.fetchall()
    if not rows:
        f.add(f"لا توجد قراءات {tl} في هذه الفترة، فلا يمكن حساب المتوسط.",
              f"There are no {tl} readings in this period, so no average can be computed.", rows=0)
        return start, end, f.items, False
    tot = sum(r["n"] for r in rows)
    avg = round(sum(float(r["avg_v"]) * r["n"] for r in rows) / tot, 2)
    lo, hi = round(min(float(r["min_v"]) for r in rows), 2), round(max(float(r["max_v"]) for r in rows), 2)
    unit = next((r["unit"] for r in rows if r["unit"]), "") or ""
    f.add(f"{tl}: {tot} قراءة، المتوسط {avg} {unit}، الأدنى {lo}، الأقصى {hi}.",
          f"{tl}: {tot} readings, average {avg} {unit}, min {lo}, max {hi}.", readings=tot, avg=avg, required=True)
    for r in rows[:3]:
        f.add(f"{r['name']}: متوسط {round(float(r['avg_v']), 2)} {r['unit'] or ''} على {r['n']} قراءة.",
              f"{r['name']}: average {round(float(r['avg_v']), 2)} {r['unit'] or ''} over {r['n']} readings.", asset=r["name"])
    return start, end, f.items, True


COLLECTORS = {"status": c_status, "faults": c_faults, "cameras": c_cameras,
              "network": c_network, "repair": c_repair, "energy": c_energy,
              "kpi": c_kpi, "history": c_history}


def gather(cur, question, intent, hours, lang):
    ctx = {"question": question, "intent": intent, "lang": lang, "hours": hours,
           "facts": [], "sufficient": False, "window": None}
    if intent == "out_of_scope":
        return ctx
    args = (cur, hours, lang, question) if intent == "history" else (cur, hours, lang)
    start, end, facts, sufficient = COLLECTORS[intent](*args)
    ctx.update(facts=facts, sufficient=sufficient,
               window={"from": start, "to": end, "hours": round((end - start).total_seconds() / 3600)})
    return ctx


# --------------------------------------------------------------------------- grounding
SYSTEM_PROMPT = """You are the conversational assistant of a smart-building operations dashboard. You receive a QUESTION and numbered FACTS computed from the building database.
Rules:
1. Answer only from the facts. Do not calculate, estimate, or add numbers, times, device names or causes that are not in the facts.
2. Reply in the language named in "language". Write digits as Western digits (0-9) and times as HH:MM UTC exactly as they appear in the facts.
3. After every sentence or bullet that states a number, time or status, cite the supporting facts like [F1] or [F1][F3]. One ref per bracket; refs must exist in the facts. Square brackets are reserved for refs.
4. If data_sufficient is false, or the facts say data is missing, state clearly that the database does not contain enough data, say what is missing, and do not guess.
5. Be concise: at most 6 short sentences or bullets, no greeting.
6. Copy device names (like FA-PANEL-02) exactly as written in the facts; never translate, rephrase or invent names.
7. SMOKE_DETECTED means smoke was detected, not a confirmed fire; do not say "fire" unless a fact says so. Use "alerts" only for the alert-engine alerts and "fire alarms" only for fire-alarm facts. Do not add judgments (such as "working well") that the facts do not state.
8. "events" are historical records inside the window; never call them alarms or alerts, and never present an event count as a current state. Current state comes only from facts about "active" alarms, "open" incidents or "open" alerts. The only systems and devices that exist are those named in the facts ("Window" is not a system).
9. Facts with "must_cite": true describe the current state and must be mentioned and cited in the answer.
10. Never describe a system as healthy, fine, stable or "without issues" unless a fact says exactly that. When a fact reports critical events or incidents for a system, mention them.
11. Copy event types (LINK_DOWN, DOOR_FORCED, SMOKE_DETECTED, ...) exactly as written and never translate them. Use only plain punctuation (. , ; :), never CJK punctuation.
Return JSON only: {"answer": "...", "confidence": "low|medium|high"}"""

PAREN = re.compile(r"\((F\d+)\)")
BRACKET = re.compile(r"\[([^\[\]]*)\]")
ASSET_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*(?:[-_][A-Za-z0-9]+)+\b")
LIST_MARK = re.compile(r"(?m)^\s*(?:[-*\u2022]|\d+[.)])\s+")


# qualitative claims the facts never make ("working well", "without issues"): the numeric validator cannot see these
JUDGMENTS = [norm(x) for x in ("without issues", "no issues", "working well", "operating normally", "running smoothly",
                               "operating well", "performing well", "is stable", "بشكل جيد", "بدون مشاكل",
                               "من غير مشاكل", "بشكل طبيعي", "تعمل بكفاءة", "مستقر")]


def judgment_violations(text, facts):
    low, corpus = norm(text), norm(" ".join(f["text"] for f in facts))
    return [f'the phrase "{p}" is a judgment that no fact states; remove it or state the critical facts instead'
            for p in JUDGMENTS if p in low and p not in corpus]


def check(text, facts):
    refs = {f["ref"] for f in facts}
    bad, cites = [], []
    for body in BRACKET.findall(text):
        for part in (p.strip() for p in body.split(",")):
            if re.fullmatch(r"F\d+", part):
                cites.append(part)
            else:
                bad.append(f"malformed citation [{body}]")
    if not cites:
        bad.append("no fact citation")
    bad += [f"unknown citation [{c}]" for c in dict.fromkeys(cites) if c not in refs]
    for f in facts:
        name = f["data"].get("asset")
        if f["ref"] in cites and name and name not in text:
            bad.append(f"asset name {name} must be written exactly as in the facts")
    for f in facts:
        if f["data"].get("required") and f["ref"] not in cites:
            bad.append(f"the answer must also cite {f['ref']} ({f['text'][:70]})")
    # a number, time or date must appear in the window fact (F1) or in a fact the answer cites
    allowed = set(cites) | {"F1"}
    corpus = " ".join(f["text"] for f in facts if f["ref"] in allowed)
    def numbers_in(txt):  # dates, times and device names are checked separately; their digits must not count
        return [float(n) for n in ai.NUM_ANY.findall(ASSET_TOKEN.sub(" ", ai.TIME.sub(" ", ai.DATE.sub(" ", txt))))]

    def where(n):
        return [f["ref"] for f in facts if ai.number_ok(n, numbers_in(f["text"]))]

    nums = numbers_in(corpus)
    times = set(ai.TIME.findall(corpus))
    dates = set(ai.DATE.findall(corpus))
    t = BRACKET.sub(" ", text)
    bad += [f"date {d} is not in the facts you cited" for d in ai.DATE.findall(t) if d not in dates]
    t = ai.DATE.sub(" ", t)
    bad += [f"time {x} is not in the facts you cited" for x in ai.TIME.findall(t) if x.zfill(5) not in times]
    t = ai.TIME.sub(" ", t)
    t = LIST_MARK.sub("", ASSET_TOKEN.sub(" ", t))
    for n in ai.NUM_STRICT.findall(t):
        if not ai.number_ok(float(n), nums):
            hit = where(float(n))
            bad.append(f"number {n} is not in the facts you cited"
                       + (f" (it appears in {', '.join(hit)}: cite that fact next to it)" if hit else ""))
    bad += judgment_violations(text, facts)
    return list(dict.fromkeys(bad))


# --------------------------------------------------------------------------- composition
def _result(ctx, answer, source, confidence, grounding, model=None):
    return {"intent": ctx["intent"], "language": ctx["lang"], "answer": answer, "source": source, "model": model,
            "data_sufficient": ctx["sufficient"], "confidence": confidence, "window": ctx["window"],
            "sources": ctx["facts"], "grounding": grounding}


def _fallback(ctx, note):
    facts = ctx["facts"]
    answer = "\n".join(f"- {f['text']} [{f['ref']}]" for f in facts)
    violations = check(answer, facts)
    return _result(ctx, answer, "FALLBACK", "high" if ctx["sufficient"] else "low",
                   {"passed": not violations, "violations": violations, **note})


def _out_of_scope(ctx):
    if ctx["lang"] == "ar":
        text = ("أقدر أجاوب فقط عن بيانات المبنى: حالة المبنى، الأعطال، الكاميرات، الشبكة، "
                "أولويات الإصلاح، استهلاك الطاقة، مؤشرات الأداء (KPIs)، والقراءات التاريخية (حرارة، رطوبة، محاولات الدخول). اسألني في واحد منهم.")
    else:
        text = ("I can only answer about the building data: building status, faults, cameras, network, "
                "repair priorities, energy consumption, KPIs and historical readings (temperature, humidity, access events). "
                "Please ask about one of these.")
    return _result(ctx, text, "RULES", "high", {"passed": True, "violations": []})


def compose(ctx, use_llm=True):
    if ctx["intent"] == "out_of_scope":
        return _out_of_scope(ctx)
    facts = ctx["facts"]
    if not use_llm:
        return _fallback(ctx, {"llm_attempted": False})
    user = json.dumps({"question": ctx["question"], "language": "Arabic" if ctx["lang"] == "ar" else "English",
                       "data_sufficient": ctx["sufficient"],
                       "facts": [{"ref": f["ref"], "text": f["text"], **({"must_cite": True} if f["data"].get("required") else {})} for f in facts]}, ensure_ascii=False)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    rejected = []
    for attempt in (1, 2):
        try:
            data = ai.parse_json(ai.call_ollama(messages))
        except Exception as exc:
            return _fallback(ctx, {"llm_attempted": True, "llm_error": f"{type(exc).__name__}: {exc}"[:200],
                                   "rejected_answers": rejected})
        text = PAREN.sub(r"[\1]", str(data.get("answer", "")).strip())
        bad = check(text, facts) if text else ["empty answer"]
        if not bad:
            conf = data.get("confidence") if data.get("confidence") in ("low", "medium", "high") else "low"
            if not ctx["sufficient"]:
                conf = "low"
            return _result(ctx, text, "OLLAMA", conf,
                           {"passed": True, "violations": [], "attempts": attempt, "rejected_answers": rejected},
                           ai.OLLAMA_MODEL)
        rejected.append({"attempt": attempt, "violations": bad})
        messages += [{"role": "assistant", "content": json.dumps(data, ensure_ascii=False)},
                     {"role": "user", "content": "Your answer was rejected: " + "; ".join(bad)
                      + ". Rewrite it using only numbers, times and [F#] citations present in the facts."}]
    return _fallback(ctx, {"llm_attempted": True, "llm_error": "answer failed grounding validation twice",
                           "rejected_answers": rejected})
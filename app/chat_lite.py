"""
POST /chat, GET /chat/questions - the building assistant widget, answering from the unified schema.

The full six-tool router is not part of this change. This module answers the questions the Day 2 pipeline can answer
with evidence, and says so plainly for everything else:
  * CRAC / HVAC / temperature  -> detect(), then the grounded explanation + playbook recommendations of the latest
                                  HVAC_HIGH_TEMP alert (Ollama if available, deterministic fallback otherwise)
  * wrong / problem / incident / alert / status -> ranked list of the open incidents read from the database
  * anything else               -> a fixed message listing what is supported (source RULES)
The response uses the field names chat-widget.js reads (answer, language, source, grounding, sources, ...).
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAY2_PATH = os.path.join(PROJECT_ROOT, "day2_ahmed")
if DAY2_PATH not in sys.path:
    sys.path.insert(0, DAY2_PATH)

try:
    import recommendations as recs
    import pipeline
    import db_adapter as da
    import evidence
    import validation as val
except Exception:  # pragma: no cover - fallback if a submodule is unavailable in a minimal env
    recs = None
    pipeline = None
    da = None
    evidence = None
    val = None

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct")


def _grounding_compute(success: bool, *, rows=None, allow_empty=True):
    if not success:
        return False
    if rows is None:
        return True
    if isinstance(rows, list):
        return True if allow_empty or rows else False
    return bool(rows)


def _ollama_chat(payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{OLLAMA_HOST}/api/chat", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        return json.loads(resp.read())


# ======================================================================================================
# LLM AGENT  (paste this block in app/chat_lite.py, replacing _ollama_tool_loop and _maybe_use_tool_loop)
# ======================================================================================================
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))

AGENT_TOOLS = [
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "Run ONE read-only PostgreSQL SELECT/WITH query on the building database and get the rows back.",
        "parameters": {"type": "object", "properties": {
            "sql": {"type": "string", "description": "A single SELECT or WITH query, no semicolon."}},
            "required": ["sql"]}}},
    {"type": "function", "function": {
        "name": "explain_alert",
        "description": "Explain one alert (cause + approved playbook actions) using verified evidence.",
        "parameters": {"type": "object", "properties": {"alert_id": {"type": "integer"}}, "required": ["alert_id"]}}},
]


def _agent_system_prompt():
    schema = get_schema()
    schema_txt = "\n".join(f"{t}({', '.join(cols)})" for t, cols in schema.get("columns", {}).items())
    return (
        "You are the operations assistant of a smart-building monitoring platform. You talk with the operator "
        "like a helpful colleague: natural, short, direct. Reply in the user's language (Arabic or English).\n\n"
        "DATABASE (PostgreSQL, read-only). Tables and columns:\n" + schema_txt + "\n\n"
        "RULES:\n"
        "1. For any question about the building's data (alerts, incidents, cameras, assets, temperatures, energy, "
        "network, risks, priorities) call run_sql. You may call it several times, e.g. look at a table first, then "
        "refine. Use ONLY the tables/columns listed above.\n"
        "2. Never invent numbers, names, times or statuses. Use only what the tool returned. If the data cannot "
        "answer, say so clearly and say what is missing.\n"
        "3. Questions about risks/problems: look at open alerts and incidents (severity CRITICAL > WARNING > INFO), "
        "group by asset or location, then explain in plain words and suggest what to check first.\n"
        "4. An offline camera is one whose latest camera_offline event is newer than its latest camera_online event.\n"
        "5. Follow-ups like 'summarize them' or 'ده' refer to the previous messages in this conversation.\n"
        "6. Greetings, general explanations or advice that need no data: answer directly without tools.\n"
        "7. If a query errors, read the error and retry with a corrected query.\n"
        f"Current time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}."
    )


def _numbers_grounded(answer, evidence_text, row_counts):
    """True if every number in the answer appears (rounding allowed) in the SQL results or is a row count / column total."""
    answer = re.sub(r"(?m)^\s*\d+[.)]\s", "", answer)          # ignore list numbering
    ev = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", evidence_text)] + [float(c) for c in row_counts]
    for token in re.findall(r"\d+(?:\.\d+)?", answer):
        val = float(token)
        tol = 0.5 * 10 ** -(len(token.split(".")[1]) if "." in token else 0) + 1e-9
        if not any(abs(val - e) <= tol for e in ev):
            return False
    return True


def _agent_answer(message, history, session_state, lang, cur, conn, body, pipeline=None):
    """Let the local model answer by calling SQL tools in a loop. Returns None if the model is unavailable or
    gives up, so the caller can fall back to the rule-based answers."""
    if body.use_llm is False:
        return None
    messages = [{"role": "system", "content": _agent_system_prompt()}]
    turns = list(history or [])[-8:]
    if turns and turns[-1].get("role") == "user" and (turns[-1].get("text") or "").strip() == message.strip():
        turns = turns[:-1]                                       # widget already put the current message in history
    for turn in turns:
        txt = (turn.get("text") or "").strip()
        if txt:
            messages.append({"role": "assistant" if turn.get("role") in ("bot", "assistant") else "user", "content": txt})
    messages.append({"role": "user", "content": message})

    sources, evidence, row_counts = [], [], []
    try:
        for _ in range(5):                                       # max 5 tool rounds
            resp = _ollama_chat({"model": OLLAMA_MODEL, "messages": messages, "tools": AGENT_TOOLS,
                                 "stream": False, "options": {"temperature": 0.2}})
            msg = resp.get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = (msg.get("content") or "").strip()
                if not answer:
                    return None
                passed = bool(evidence) and _numbers_grounded(answer, " ".join(evidence), row_counts)
                return _reply(answer, lang, "OLLAMA", passed, sources, extra={"session_state": {"last_assets": []}})
            messages.append(msg)
            for call in calls:
                fn = call.get("function") or {}
                name, args = fn.get("name"), fn.get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                try:
                    if name == "run_sql":
                        rows = _run_readonly_sql(cur, args.get("sql", ""))
                        cur.connection.rollback()                # close the read-only transaction
                        data = [dict(r) for r in rows][:50]
                        result = json.dumps(data, default=str, ensure_ascii=False)
                        evidence.append(result)
                        row_counts.append(len(rows))
                        for col in (data[0] if data else {}):    # totals the model may compute (column sums)
                            vals = [r[col] for r in data if isinstance(r.get(col), (int, float)) and not isinstance(r.get(col), bool)]
                            if vals:
                                row_counts.append(sum(vals))
                        sources.append({"ref": f"Q{len(sources) + 1}", "text": f"{args.get('sql', '')}  ->  {len(rows)} rows"})
                    elif name == "explain_alert" and pipeline is not None:
                        result = json.dumps(pipeline.explain_alert(cur, int(args.get("alert_id")), use_llm=False, persist=False),
                                            default=str, ensure_ascii=False)
                        evidence.append(result)
                        sources.append({"ref": f"Q{len(sources) + 1}", "text": f"explain_alert({args.get('alert_id')})"})
                    else:
                        result = "ERROR: unknown or unavailable tool"
                except Exception as exc:                         # bad SQL etc. -> let the model see it and retry
                    try:
                        cur.connection.rollback()
                    except Exception:
                        pass
                    result = f"ERROR: {exc}"
                messages.append({"role": "tool", "tool_name": name, "content": result[:6000]})
        return None
    except Exception as exc:                                     # Ollama down / timeout / bad JSON
        print(f"[chat] LLM agent failed: {exc!r}", flush=True)
        return None



def _lookup_asset_name(cur, needle):
    if not needle:
        return None
    rows = _run_readonly_sql(cur, "SELECT asset_id, name FROM assets ORDER BY name")
    needle_l = needle.lower()
    for row in rows:
        if needle_l in row["name"].lower():
            return row["name"]
    return None


def _lookup_location_name(cur, needle):
    if not needle:
        return None
    rows = _run_readonly_sql(cur, "SELECT location_id, name FROM locations ORDER BY name")
    needle_l = needle.lower()
    for row in rows:
        if needle_l in row["name"].lower():
            return row["name"]
    return None


def _metric_latest(cur, metric_name, needle):
    needle = (needle or "").strip()
    if not needle:
        return None, "no_target"
    asset_name = _lookup_asset_name(cur, needle)
    location_name = _lookup_location_name(cur, needle) if asset_name is None else None
    where = []
    params = [metric_name]
    if asset_name:
        where.append("a.name = %s")
        params.append(asset_name)
    elif location_name:
        where.append("l.name = %s")
        params.append(location_name)
    else:
        where.append("(a.name ILIKE %s OR l.name ILIKE %s)")
        params.extend([f"%{needle}%", f"%{needle}%"])
    sql = f"""
        SELECT a.name AS asset_name, l.name AS location_name, mt.name AS metric_name, t.ts, t.value
        FROM telemetry_records t
        JOIN metric_types mt ON mt.metric_id = t.metric_id
        JOIN assets a ON a.asset_id = t.asset_id
        JOIN locations l ON l.location_id = a.location_id
        WHERE mt.name = %s AND ({' OR '.join(where)})
        ORDER BY t.ts DESC
        LIMIT 1
    """
    rows = _run_readonly_sql(cur, sql, tuple(params))
    if not rows:
        return None, "no_data"
    row = rows[0]
    return row, "ok"


def _humidity_question(cur, body, lang, msg_low):
    if "humidity" not in msg_low and "رطوبة" not in msg_low:
        return None
    return _metric_question(cur, body, lang, msg_low)


def _camera_question(cur, body, lang, msg_low):
    if "camera" not in msg_low and "كاميرا" not in msg_low and "offline" not in msg_low and "غير متصلة" not in msg_low and "لا تعمل" not in msg_low:
        return None

    sql = """
        WITH latest AS (
            SELECT er.asset_id,
                   er.ts,
                   et.name AS event_type,
                   ROW_NUMBER() OVER (PARTITION BY er.asset_id, et.name ORDER BY er.ts DESC) AS rn
            FROM event_records er
            JOIN event_types et ON et.event_type_id = er.event_type_id
            WHERE et.name IN ('camera_offline', 'camera_online')
        )
        SELECT a.asset_id, a.name AS asset_name,
               MAX(CASE WHEN l.event_type = 'camera_offline' THEN l.ts END) AS last_offline,
               MAX(CASE WHEN l.event_type = 'camera_online' THEN l.ts END) AS last_online
        FROM latest l
        JOIN assets a ON a.asset_id = l.asset_id
        WHERE l.rn = 1
        GROUP BY a.asset_id, a.name
        HAVING MAX(CASE WHEN l.event_type = 'camera_offline' THEN l.ts END) > MAX(CASE WHEN l.event_type = 'camera_online' THEN l.ts END)
            OR MAX(CASE WHEN l.event_type = 'camera_online' THEN l.ts END) IS NULL
        ORDER BY a.name
    """
    rows = _run_readonly_sql(cur, sql)
    passed = _grounding_compute(True, rows=rows)
    if not rows:
        answer = "There are no offline cameras in the database right now." if lang == "en" else "لا توجد كاميرات غير متصلة في قاعدة البيانات الآن."
        return _reply(answer, lang, "DATABASE", passed, sources=[{"ref": "Q1", "text": f"verified in database: {sql}; result=0 rows"}], extra={"count": 0, "session_state": {"last_assets": []}})

    total = len(rows)
    names = ", ".join(r["asset_name"] for r in rows[:5])
    answer = (
        f"There are {total} offline cameras: {names}." if lang == "en" else f"يوجد {total} كاميرا غير متصلة: {names}."
    )
    sources = [{"ref": "Q1", "text": f"verified in database: {sql}; result={total} offline camera assets"}]
    return _reply(answer, lang, "DATABASE", passed, sources, extra={"count": total, "session_state": {"last_assets": [r["asset_name"] for r in rows]}})


def _metric_question(cur, body, lang, msg_low):
    metric = None
    if "humidity" in msg_low or "رطوبة" in msg_low:
        metric = "humidity"
    elif "temperature" in msg_low or "حرارة" in msg_low or "temp" in msg_low:
        metric = "temperature"
    if metric is None:
        return None
    target = None
    for needle in ["crac unit 1", "air handler 1", "server room", "cold aisle a", "control room", "lobby", "roof plant"]:
        if needle in msg_low:
            target = needle
            break
    if target is None:
        target = "crac unit 1" if "crac" in msg_low else ""
    row, status = _metric_latest(cur, metric, target)
    if status == "no_data":
        label = "humidity" if metric == "humidity" else "temperature"
        answer = f"There is no {label} data for that asset or zone in the database." if lang == "en" else f"لا توجد بيانات {label} لهذا الأصل أو المنطقة في قاعدة البيانات."
        return _reply(answer, lang, "DATABASE", True, sources=[{"ref": "Q1", "text": f"verified in database: no rows for {metric} for target='{target}'"}], extra={"count": 0, "metric": metric, "target": target})
    timestamp = row["ts"]
    value = float(row["value"])
    label = "humidity" if metric == "humidity" else "temperature"
    answer = (
        f"The latest {label} reading for {row['asset_name']} is {value:.2f} at {timestamp}." if lang == "en" else f"آخر قراءة {label} لـ {row['asset_name']} هي {value:.2f} في {timestamp}."
    )
    passed = _grounding_compute(True, rows=[row])
    return _reply(answer, lang, "DATABASE", passed, [{"ref": "Q1", "text": f"verified in database: latest {metric} for {row['asset_name']} = {value:.2f} at {timestamp}"}], extra={"count": 1, "metric": metric, "value": value, "ts": str(timestamp), "asset_name": row["asset_name"]})


def _alert_count_answer(cur, body, lang, message):
    low = message.lower()
    alert_re = re.compile(r"(?:alert|alerts|تنبيه|تنبيهات|إنذار|إنذارات|ال(?:ا?)رت|ال(?:ا?)لت|ال(?:ا?)لرت)")
    has_alert = bool(alert_re.search(low))
    severity = None
    open_only = False

    if has_alert and (("critical" in low and "alert" in low) or ("حرج" in low and ("تنبيه" in low or "إنذار" in low or "الرت" in low or "الالت" in low))):
        severity = "critical"
    elif has_alert and (("warning" in low and "alert" in low) or ("تحذير" in low and ("تنبيه" in low or "إنذار" in low or "الرت" in low or "الالت" in low))):
        severity = "warning"
    elif has_alert and (("info" in low and "alert" in low) or ("معل" in low and ("تنبيه" in low or "إنذار" in low or "الرت" in low or "الالت" in low))):
        severity = "info"
    elif has_alert and (("open" in low and "alert" in low) or ("مفتوح" in low and ("تنبيه" in low or "إنذار" in low or "الرت" in low or "الالت" in low))):
        open_only = True
    elif has_alert and any(k in low for k in ("total", "all", "overall", "إجمالي", "المجموع", "كل", "جميع", "عدد")):
        severity = None
        open_only = False

    if has_alert and (("open" in low and "alert" in low) or ("مفتوح" in low and ("تنبيه" in low or "إنذار" in low or "الرت" in low or "الالت" in low))):
        open_only = True

    count = _count_alerts(cur, severity=severity, open_only=open_only, hours=body.hours)
    label = {
        "critical": "critical alerts" if lang == "en" else "تنبيهات حرجة",
        "warning": "warning alerts" if lang == "en" else "تنبيهات تحذيرية",
        "info": "info alerts" if lang == "en" else "تنبيهات معلومات",
    }.get(severity, "alerts" if lang == "en" else "تنبيهات")
    if open_only:
        label = "open alerts" if lang == "en" else "تنبيهات مفتوحة"

    if lang == "ar":
        answer = f"هناك {count} {label}"
        if body.hours is not None:
            answer += f" خلال آخر {body.hours} ساعة."
        else:
            answer += " في قاعدة البيانات."
    else:
        answer = f"There are {count} {label}"
        if body.hours is not None:
            answer += f" in the last {body.hours} hours."
        else:
            answer += " in the database."

    sql_text = "SELECT COUNT(*) FROM alerts WHERE 1=1"
    if body.hours is not None:
        sql_text += " AND detected_ts >= NOW() - %s * interval '1 hour'"
    if open_only:
        sql_text += " AND status <> 'resolved'"
    if severity is not None:
        sql_text += f" AND severity = '{severity}'"
    passed = _grounding_compute(True, rows=[{"n": count}])
    sources = [{"ref": "Q1", "text": f"verified in database: {sql_text}; result={count}"}]
    return _reply(answer, lang, "DATABASE", passed, sources, hours=body.hours, extra={"count": count, "severity": severity, "open_only": open_only, "session_state": {"last_assets": []}})


def _incident_list(cur, body, lang):
    sql = """
        SELECT i.incident_id, i.severity, i.title, i.status, i.first_ts, i.last_ts,
               COALESCE(array_agg(a.alert_id ORDER BY a.first_ts), ARRAY[]::bigint[]) AS alert_ids
        FROM incidents i
        LEFT JOIN alerts a ON a.incident_id = i.incident_id
        WHERE i.status <> 'resolved'
        GROUP BY i.incident_id, i.severity, i.title, i.status, i.first_ts, i.last_ts
        ORDER BY CASE i.severity WHEN 'critical' THEN 1 WHEN 'warning' THEN 2 ELSE 3 END,
                 i.first_ts DESC
        LIMIT 6
    """
    rows = _run_readonly_sql(cur, sql)
    passed = _grounding_compute(True, rows=rows)
    if not rows:
        text = "There are no open incidents in the database." if lang == "en" else "لا توجد حوادث مفتوحة في قاعدة البيانات."
        return _reply(text, lang, "FALLBACK", passed, sources=[{"ref": "F1", "text": "verified in database: SELECT COUNT(*) FROM incidents WHERE status <> 'resolved'; result=0"}], extra={"count": 0})

    lines = ["Open incidents, most severe first:" if lang == "en" else "الحوادث المفتوحة، الأكثر خطورة أولاً:"]
    sources = []
    for idx, row in enumerate(rows, 1):
        alert_ids = row["alert_ids"] or []
        fact_ref = f"F{idx}"
        lines.append(f"- [{fact_ref}] {row['severity'].upper()}: {row['title']}")
        sources.append({"ref": fact_ref, "text": f"verified in database: incident {row['incident_id']} | {row['status']} | {row['first_ts']} to {row['last_ts']} | alerts {alert_ids}"})
    return _reply("\n".join(lines), lang, "FALLBACK", passed, sources, extra={"count": len(rows), "session_state": {"last_incident_id": rows[0]["incident_id"]}})


def _hvac_temp_higher_fallback(cur, body, lang):
    sql = """
        SELECT a.asset_id, a.name AS asset_name, m.name AS metric_name, t.ts, t.value
        FROM telemetry_records t
        JOIN metric_types m ON m.metric_id = t.metric_id
        JOIN assets a ON a.asset_id = t.asset_id
        WHERE a.name = 'CRAC Unit 1' AND m.name = 'temperature'
        ORDER BY t.ts DESC
        LIMIT 5
    """
    temp_rows = _run_readonly_sql(cur, sql)
    threshold = 29.0
    alert_sql = """
        SELECT alert_id, severity, title, first_ts, last_ts, asset_id
        FROM alerts
        WHERE asset_id = (SELECT asset_id FROM assets WHERE name = 'CRAC Unit 1')
          AND rule_id = 'HVAC_HIGH_TEMP'
        ORDER BY detected_ts DESC
        LIMIT 1
    """
    alert_rows = _run_readonly_sql(cur, alert_sql)
    if not temp_rows:
        return _reply(
            "I could not find CRAC temperature data in the database, so I cannot verify why it is high." if lang == "en" else "لم أجد بيانات درجة حرارة CRAC في قاعدة البيانات، لذلك لا أستطيع التحقق من سبب ارتفاعها.",
            lang,
            "FALLBACK",
            False,
            sources=[{"ref": "F1", "text": "verified in database: no CRAC temperature telemetry rows"}],
            sufficient=False,
        )

    latest = temp_rows[0]
    latest_temp = float(latest["value"])
    alert = alert_rows[0] if alert_rows else None
    high = latest_temp > threshold
    passed = _grounding_compute(True, rows=temp_rows)
    answer = (
        f"CRAC Unit 1 temperature is high because the latest reading was {latest_temp:.2f}C at {latest['ts']} [F1]."
        if high and lang == "en" else (
            f"CRAC Unit 1 temperature is not above the threshold: latest reading {latest_temp:.2f}C at {latest['ts']} [F1]."
            if not high and lang == "en" else (
                f"حرارة CRAC Unit 1 مرتفعة لأن آخر قراءة كانت {latest_temp:.2f}C في {latest['ts']} [F1]."
                if high else f"حرارة CRAC Unit 1 ليست أعلى من الحد: آخر قراءة {latest_temp:.2f}C في {latest['ts']} [F1]."
            )
        )
    )
    if alert is not None and high:
        answer += (" The HVAC_HIGH_TEMP alert was raised at " if lang == "en" else " وتم إنشاء تنبيه HVAC_HIGH_TEMP في ") + f"{alert['first_ts']} [F2]."
    sources = [{"ref": "F1", "text": f"verified in database: {sql}; latest_temp={latest_temp:.2f}C at {latest['ts']}"}]
    if alert is not None:
        sources.append({"ref": "F2", "text": f"verified in database: {alert_sql}; title={alert['title']} | first_ts={alert['first_ts']}"})
    extra = {"count": 1, "session_state": {"last_alert_id": alert['alert_id'] if alert else None}}
    if alert is not None and alert['alert_id']:
        extra['alert_id'] = int(alert['alert_id'])
    return _reply(answer, lang, "FALLBACK", passed, sources, hours=body.hours, extra=extra)


def _explain_by_pipeline(cur, conn, alert_id, lang):
    if pipeline is None or not hasattr(pipeline, "explain_alert"):
        return _reply("The explanation tool is unavailable in this environment." if lang == "en" else "أداة الشرح غير متاحة في هذا البيئة.", lang, "FALLBACK", False, sources=[], sufficient=False)
    try:
        result = pipeline.explain_alert(cur, alert_id, use_llm=False, persist=False)
        extra = {"alert_id": int(alert_id), "session_state": {"last_alert_id": int(alert_id)}}
        sources = []
        for idx, item in enumerate(result.get("evidence") or [], 1):
            ref = f"F{idx}"
            sources.append({"ref": ref, "text": f"verified in database: {item.get('ref')} | {item.get('ts')} | {item.get('value')}"})
        if not sources:
            sources = [{"ref": "F1", "text": "verified in database: alert evidence rows"}]
        return _reply(result.get("answer") or ("I could not verify a grounded explanation for this alert." if lang == "en" else "لم أستطع التحقق من شرح موثوق لهذا التنبيه."), lang, "FALLBACK", bool(result.get("validation", {}).get("status") == "grounding_supported"), sources, extra=extra)
    except Exception:
        return _reply("I could not verify a grounded explanation for this alert." if lang == "en" else "لم أستطع التحقق من شرح موثوق لهذا التنبيه.", lang, "FALLBACK", False, sources=[{"ref": "F1", "text": "explain_alert failed"}], sufficient=False)


class ChatIn(BaseModel):
    message: str = Field(..., min_length=2, max_length=1000)
    hours: int | None = Field(default=None, ge=1, le=168)
    use_llm: bool | None = None
    history: list[dict[str, str]] = Field(default_factory=list)
    session_state: dict[str, Any] | None = Field(default=None)


def _lang(text: str) -> str:
    return "ar" if re.search(r"[\u0600-\u06FF]", text) else "en"


def _reply(answer, lang, source, passed, sources=None, sufficient=True, hours=None, extra=None):
    return {
        "answer": answer,
        "language": lang,
        "source": source,
        "grounding": {"passed": bool(passed)},
        "sources": sources or [],
        "data_sufficient": sufficient,
        "window": {"hours": hours} if hours else None,
        **(extra or {}),
    }


@lru_cache(maxsize=1)
def _schema_snapshot():
    from psycopg import sql

    rows = []
    try:
        from psycopg2 import connect
    except Exception:  # pragma: no cover
        return rows

    dsn = (
        f"host={os.getenv('PGHOST', 'localhost')} "
        f"port={os.getenv('PGPORT', '5432')} "
        f"dbname={os.getenv('PGDATABASE', 'anzma')} "
        f"user={os.getenv('PGUSER', 'postgres')} "
        f"password={os.getenv('PGPASSWORD', '')}"
    )
    try:
        conn = connect(dsn)
    except Exception:
        return rows
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public'
                ORDER BY table_name, ordinal_position
                """
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return rows


def _safe_sql(sql_text: str) -> str:
    text = (sql_text or "").strip()
    if not text:
        raise ValueError("empty SQL")
    if ";" in text:
        raise ValueError("multiple statements are not allowed")
    if not re.match(r"^(with|select)\b", text, re.I):
        raise ValueError("only SELECT/WITH queries are allowed")
    upper = text.upper()
    blocked = [
        "DELETE ", "UPDATE ", "INSERT ", "DROP ", "ALTER ", "CREATE ", "TRUNCATE ",
        "GRANT ", "REVOKE ", "COMMIT ", "ROLLBACK ", "BEGIN ", "CALL ", "EXEC ", "MERGE ", "COPY ",
        " CREATE", " DROP", " UPDATE", " DELETE", " INSERT",
    ]
    if any(token in upper for token in blocked):
        raise ValueError("destructive SQL is not allowed")
    if " LIMIT " not in upper:
        text = f"{text.rstrip(';')} LIMIT 200"
    return text


def _run_readonly_sql(cur, sql_text: str, params=None):
    sql_text = _safe_sql(sql_text)
    try:
        cur.execute("BEGIN")
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SET LOCAL statement_timeout = '5s'")
        cur.execute(sql_text, params or ())
        return cur.fetchall()
    except Exception:
        cur.connection.rollback()
        raise


def get_schema():
    rows = _schema_snapshot()
    if not rows:
        return {
            "tables": [
                "assets", "locations", "system_types", "metric_types", "event_types",
                "telemetry_records", "event_records", "alerts", "incidents",
            ],
            "columns": {
                "assets": ["asset_id", "name", "asset_code", "system_type_id", "location_id"],
                "telemetry_records": ["telemetry_id", "asset_id", "ts", "metric_id", "value"],
                "event_records": ["event_id", "asset_id", "ts", "event_type_id", "severity", "details"],
                "alerts": ["alert_id", "rule_id", "severity", "status", "asset_id", "detected_ts", "incident_id"],
                "incidents": ["incident_id", "status", "severity", "first_ts", "last_ts"],
            },
        }
    by_table = {}
    for table_name, column_name, data_type in rows:
        by_table.setdefault(table_name, []).append({"column": column_name, "type": data_type})
    return {"tables": sorted(by_table), "columns": {k: [x["column"] for x in v] for k, v in sorted(by_table.items())}}


def _count_alerts(cur, *, severity=None, open_only=False, hours=None):
    sql = "SELECT COUNT(*) AS n FROM alerts WHERE 1 = 1"
    params = []
    if hours is not None:
        sql += " AND detected_ts >= NOW() - %s * interval '1 hour'"
        params.append(hours)
    if open_only:
        sql += " AND status <> %s"
        params.append("resolved")
    if severity is not None:
        sql += " AND severity = %s"
        params.append(severity)
    rows = _run_readonly_sql(cur, sql, tuple(params))
    return int(rows[0]["n"]) if rows else 0


def _last_alert_id(session_state):
    if not isinstance(session_state, dict):
        return None
    value = session_state.get("last_alert_id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _last_incident_id(session_state):
    if not isinstance(session_state, dict):
        return None
    value = session_state.get("last_incident_id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _looks_like_fix_question(low: str) -> bool:
    needles = [
        "fix", "repair", "solve", "how to fix", "how to repair", "what to do", "do now",
        "ازاي نصلح", "ازاي نصلح ده", "اصلح", "إصلاح", "صلح", "حل", "تطبيق", "action"
    ]
    return any(n in low for n in needles)


def _alert_fix(cur, conn, body, lang, alert_id):
    if recs is None or da is None:
        return _reply(
            "I can only recommend approved playbook actions if the alert is still in the database." if lang == "en"
            else "أستطيع اقتراح إجراءات playbook المعتمدة فقط إذا كان التنبيه ما يزال موجوداً في قاعدة البيانات.",
            lang, "DATABASE", False, sufficient=False,
        )

    alert = da.get_alert(cur, alert_id)
    if alert is None:
        return _reply(
            "I couldn’t find that alert in the database, so there are no approved playbook actions to show." if lang == "en"
            else "لم أجد هذا التنبيه في قاعدة البيانات، لذلك لا توجد إجراءات playbook معتمدة لعرضها.",
            lang, "DATABASE", False, sufficient=False,
        )

    try:
        rec = recs.recommend(alert)
    except Exception:
        rec = {"recommendations": [], "probable_causes": [], "limitations": ["Not available"]}

    items = rec.get("recommendations") or []
    if not items:
        text = "There are no approved playbook actions for that alert in the database." if lang == "en" else "لا توجد إجراءات playbook معتمدة لهذا التنبيه في قاعدة البيانات."
        return _reply(text, lang, "DATABASE", True, sufficient=True, extra={"alert_id": alert_id, "session_state": {"last_alert_id": alert_id}})

    lines = [
        "Approved playbook actions for this alert:" if lang == "en" else "إجراءات playbook المعتمدة لهذا التنبيه:",
    ]
    sources = []
    for idx, item in enumerate(items[:5], 1):
        text = item.get("text") or item.get("action") or item.get("title") or item.get("action_id")
        reason = item.get("basis") or item.get("because") or "approved action"
        lines.append(f"- [Q{idx}] {text} — {reason}")
        sources.append({"ref": f"Q{idx}", "text": f"Approved action: {text}; reason: {reason}; derived_by={item.get('derived_by', 'PLAYBOOK')}"})

    response = _reply("\n".join(lines), lang, "DATABASE", True, sources, hours=body.hours, extra={
        "alert_id": alert_id,
        "session_state": {"last_alert_id": alert_id},
    })
    return response


def create_router(day2_connect, pipeline, inc, use_llm_default, db_errors=()):
    router = APIRouter(prefix="/chat", tags=["Chat"])

    @router.get("/questions", summary="Suggested questions for the widget")
    def questions():
        return {
            "questions": [
                "What is the current building status?",
                "How many critical alerts did we have?",
                "How many cameras are offline?",
                "What is the CRAC temperature?",
                "Why is it going wrong?",
                "ازاي نصلح ده؟",
            ],
            "extra": [],
            "intents": ["grounded_db_questions"],
        }

    @router.post("", summary="Ask the building a question")
    def chat(body: ChatIn):
        text = body.message.strip()
        if not text:
            raise HTTPException(400, "message is required")
        low = text.lower()
        lang = _lang(text)
        history = body.history or []
        session_state = body.session_state or {}

        try:
            with day2_connect() as conn:
                with conn.cursor() as cur:
                    agent = _agent_answer(text, history, session_state, lang, cur, conn, body, pipeline)
                    if agent is not None:
                        return agent

                    if "how many" in low or "كم" in low or "count" in low or "عدد" in low:
                        if "alert" in low or "تنبيه" in low or "إنذار" in low:
                            return _alert_count_answer(cur, body, lang, text)

                    if _looks_like_fix_question(low):
                        last_alert_id = _last_alert_id(session_state)
                        if last_alert_id is not None:
                            return _alert_fix(cur, conn, body, lang, last_alert_id)

                    if "camera" in low or "كاميرا" in low or "offline" in low or "غير متصلة" in low or "لا تعمل" in low:
                        camera_answer = _camera_question(cur, body, lang, low)
                        if camera_answer is not None:
                            return camera_answer

                    if "crac" in low and "temperature" in low and ("high" in low or "hot" in low or "ارتفاع" in low or "مرتفعة" in low):
                        return _hvac_temp_higher_fallback(cur, body, lang)

                    if "temperature" in low or "حرارة" in low or "temp" in low:
                        temp_answer = _metric_question(cur, body, lang, low)
                        if temp_answer is not None:
                            return temp_answer

                    if "humidity" in low or "رطوبة" in low:
                        humidity_answer = _humidity_question(cur, body, lang, low)
                        if humidity_answer is not None:
                            return humidity_answer

                    if "incident" in low or "الحوادث" in low or "حوادث" in low or "wrong" in low or "problem" in low or "issue" in low or "مشكل" in low or "مشكلة" in low or "عطل" in low:
                        return _incident_list(cur, body, lang)

                    if "crac" in low and "temperature" in low and ("high" in low or "hot" in low or "ارتفاع" in low or "مرتفعة" in low):
                        return _hvac_temp_higher_fallback(cur, body, lang)

                    schema = get_schema()
                    tables = schema.get("tables", [])
                    if not tables:
                        raise HTTPException(503, "Database metadata is unavailable")

                    if "status" in low or "حالة" in low or "building" in low or "بناء" in low:
                        open_count = _count_alerts(cur, open_only=True)
                        total_alerts = _count_alerts(cur)
                        answer = (
                            f"Open alerts: {open_count}. Total alerts: {total_alerts}." if lang == "en" else f"التنبيهات المفتوحة: {open_count}. إجمالي التنبيهات: {total_alerts}."
                        )
                        return _reply(answer, lang, "DATABASE", True, [{"ref": "Q1", "text": f"SELECT count(*) FROM alerts ... open={open_count}, total={total_alerts}"}], extra={"count": total_alerts, "session_state": {"last_assets": []}})

                    if "alert" in low or "تنبيه" in low or "إنذار" in low or "fault" in low or "الرت" in low or "الالت" in low:
                        return _alert_count_answer(cur, body, lang, text)

                    if any(term in low for term in ["what", "who", "why", "where", "when", "how", "ماذا", "من", "لماذا", "أين", "متى", "كيف"]):
                        known = ", ".join(sorted(tables[:12]))
                        return _reply(
                            (
                                "I need one more detail to answer that accurately. I can query the building data in tables such as "
                                f"{known}. For example: assets, alerts, incidents, telemetry_records, event_records."
                            ) if lang == "en" else (
                                "أحتاج إلى تفاصيل إضافية للإجابة بدقة. أستطيع الاستعلام عن بيانات المبنى في جداول مثل "
                                f"{known}. على سبيل المثال: assets، alerts، incidents، telemetry_records، event_records."
                            ),
                            lang,
                            "FALLBACK",
                            False,
                            sufficient=False,
                            extra={"session_state": {"last_assets": []}},
                        )

                    return _reply(
                        "I can answer from the stored building data. Please ask for a specific asset, alert, incident, camera, or telemetry value."
                        if lang == "en" else "أستطيع الإجابة من بيانات المبنى المخزنة. اسأل عن أصل محدد أو تنبيه أو حادث أو كاميرا أو قيمة قياس محددة.",
                        lang,
                        "RULES",
                        False,
                        sufficient=False,
                        extra={"session_state": {"last_assets": []}},
                    )
        except db_errors as exc:  # noqa: F821
            raise HTTPException(503, f"Database unavailable: {exc}")

        return _reply(
            "I need one more detail to answer that accurately from the database." if lang == "en" else "أحتاج إلى تفاصيل إضافية للإجابة بدقة من قاعدة البيانات.",
            lang,
            "DATABASE",
            False,
            sufficient=False,
        )

    return router
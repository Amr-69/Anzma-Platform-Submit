"""
ai_explainer.py
Builds incident explanations from an alert's stored evidence.

- The local LLM (Ollama) only sees the evidence package and a list of allowed
  causes and actions that were already filtered by the measured facts.
- Its answer is grounding-checked (not semantically verified): every number, time and date must appear in the
  evidence, every citation must exist, and chosen ids must come from the lists.
- If the model is unavailable or its answer fails validation, a deterministic
  explanation built from the same evidence and playbook is returned instead.
"""
import json
import os
import re
import urllib.request
from datetime import datetime

import alert_config as cfg

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))

CITE = re.compile(r"\[([^\[\]]*)\]")
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
NUM_STRICT = re.compile(r"(?<![\w.])\d+(?:\.\d+)?")
NUM_ANY = re.compile(r"\d+(?:\.\d+)?")

SYSTEM_PROMPT = """You are an assistant for a building operations team. You receive ONE alert as JSON evidence.
Rules:
1. Use only facts, numbers and times that appear in the evidence. Do not calculate new numbers, do not estimate, and do not mention devices or causes that are not in the evidence.
2. The summary MUST cite events using ONLY refs from the evidence (e.g. [E1], [E2]). Every statement about a reading needs a citation.
Square brackets are reserved for event refs such as [E2]. Refer to causes and actions in plain words, never with brackets or codes like [C1].
3. Choose cause_ids and action_ids only from allowed_causes and allowed_actions. Do not write new causes or actions.
4. Times are UTC in HH:MM form. Write times exactly as they appear in the evidence.
5. If the evidence is not enough to name a cause, say so in the summary and return an empty cause_ids list.
Return JSON only: {"summary": "2-3 sentences with citations", "cause_ids": [], "action_ids": [], "evidence_refs": [], "confidence": "low|medium|high"}"""
def parse_citations(text):
    """Return (refs, malformed). Accepts [E3], [E1, E4] and [E1-E5]; anything else in brackets is malformed."""
    refs, malformed = [], []
    for body in CITE.findall(text):
        for part in (p.strip() for p in body.split(",")):
            single = re.fullmatch(r"E(\d+)", part)
            span = re.fullmatch(r"E(\d+)\s*[-–]\s*E(\d+)", part)
            if single:
                refs.append(f"E{single.group(1)}")
            elif span and int(span.group(1)) <= int(span.group(2)) <= 999:
                refs.extend(f"E{i}" for i in range(int(span.group(1)), int(span.group(2)) + 1))
            else:
                malformed.append(f"[{body}]")
    return refs, malformed
def parse_ts(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def build_package(alert, asset):
    ev = alert["evidence"]
    rule = cfg.RULES[alert["rule_id"]]
    events = []
    for e in ev["events"]:
        events.append({"ref": e["ref"], "time_utc": parse_ts(e["ts"]).strftime("%H:%M"),
                       "event_type": e["event_type"], "value": e["value"], "unit": e["unit"],
                       "details": e["details"]})
    return {
        "alert": {"rule": rule["title"], "severity": alert["severity"], "method": alert["detection_method"],
                  "asset": asset["name"], "system": asset["system_type"], "zone": asset["zone"],
                  "first_utc": parse_ts(alert["first_ts"]).strftime("%Y-%m-%d %H:%M"),
                  "last_utc": parse_ts(alert["last_ts"]).strftime("%Y-%m-%d %H:%M")},
        "thresholds": ev["thresholds"], "facts": ev["facts"],
        "event_count": ev["event_count"], "events": events,
    }


def eligible(rule_id, facts):
    book = cfg.PLAYBOOKS[cfg.RULES[rule_id]["playbook"]]
    causes = []
    for c in book["causes"]:
        if cfg.holds(facts, c["requires"]):
            causes.append({"id": c["id"], "text": c["text"],
                           "supported_by": cfg.describe(facts, c["requires"]) or ["generic possibility for this rule"]})
    actions = []
    for a in book["actions"]:
        if a.get("status") != cfg.APPROVED:        # draft (or unlabelled) actions are never offered
            continue
        if cfg.holds(facts, a["when"]):
            actions.append({"id": a["id"], "priority": a["priority"], "text": a["text"],
                            "status": a["status"], "approver": a["approver"],
                            "because": cfg.describe(facts, a["when"]) or ["standard response for this rule"]})
    actions.sort(key=lambda a: a["priority"])
    return causes, actions


def allowed_values(pkg):
    nums, times, dates = [], set(), set()

    def walk(x, key=None):
        if x is None or isinstance(x, bool):
            return
        if isinstance(x, (int, float)):
            nums.append(float(x))
        elif isinstance(x, str):
            if key == "time_utc":
                times.add(x)
            elif key in ("first_utc", "last_utc"):
                d, t = x.split(" ")
                dates.add(d)
                times.add(t)
            elif key != "ref":
                nums.extend(float(n) for n in NUM_ANY.findall(x))
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(v, k)
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
    walk(pkg)
    return nums, times, dates


def number_ok(n, allowed):
    for a in allowed:
        if abs(a - n) <= 0.051:
            return True
        if float(n).is_integer() and (round(a) == n or int(a) == n):
            return True
    return False


def check_text(text, pkg):
    """Return the list of grounding violations found in a piece of text."""
    nums, times, dates = allowed_values(pkg)
    refs = {e["ref"] for e in pkg["events"]}
    bad = []
    cites, malformed = parse_citations(text)
    if not cites:
        bad.append("no event citation")
    bad += [f"malformed citation {m}" for m in dict.fromkeys(malformed)]
    for c in dict.fromkeys(cites):
        if c not in refs:
            bad.append(f"unknown citation [{c}]")
    t = CITE.sub(" ", text)
    for d in DATE.findall(t):
        if d not in dates:
            bad.append(f"date {d} not in evidence")
    t = DATE.sub(" ", t)
    for tm in TIME.findall(t):
        if tm.zfill(5) not in times:
            bad.append(f"time {tm} not in evidence")
    t = TIME.sub(" ", t)
    for tok in NUM_STRICT.findall(t):
        if not number_ok(float(tok), nums):
            bad.append(f"number {tok} not in evidence")
    return bad


def call_ollama(messages):
    body = json.dumps({"model": OLLAMA_MODEL, "messages": messages, "stream": False,
                       "format": "json", "options": {"temperature": 0}}).encode()
    req = urllib.request.Request(f"{OLLAMA_HOST}/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
        return json.loads(resp.read())["message"]["content"]


def parse_json(raw):
    try:
        return json.loads(raw)
    except ValueError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def finish(summary, causes, actions, pkg, source, confidence, grounding, model=None):
    return {"summary": summary, "probable_causes": causes, "recommended_actions": actions,
            "evidence_refs": [e["ref"] for e in pkg["events"]], "confidence": confidence,
            "source": source, "model": model, "grounding": grounding}


def fallback(alert, pkg, causes, actions, note=None):
    refs = [e["ref"] for e in pkg["events"]]
    cite = ", ".join(f"[{r}]" for r in (refs[:1] + refs[-1:] if len(refs) > 1 else refs))
    summary = f"{alert['summary']} Evidence: {cite}."
    violations = check_text(summary, pkg)
    
    grounding = {"passed": not violations, "violations": violations, **(note or {})}
    
    hints = []
    if "no event citation" in violations:
        hints.append("Add at least one citation in square brackets, such as [E2], after the readings you mention.")
    if any(v.startswith("malformed citation") for v in violations):
        hints.append("Square brackets are only for event refs like [E2]. Remove every other bracket such as [C1] and describe causes in plain words.")
    if any(v.startswith("unknown citation") for v in violations):
            hints.append("The only valid event refs are: " + ", ".join(sorted(refs)) + ".")    
    hints.append("Use only numbers, times and citations that are present in the evidence.")
        
    confidence = "medium" if any(c["supported_by"] != ["generic possibility for this rule"] for c in causes) else "low"
    return finish(summary, causes, actions, pkg, "FALLBACK", confidence, grounding)
PAREN_REF = re.compile(r"\((E\d+)\)")

def normalize_summary(text):
    """Fix citation formatting only. Never creates a citation and never touches numbers or times."""
    notes = []
    def paren(m):
        notes.append(f"{m.group(0)} -> [{m.group(1)}]")
        return f"[{m.group(1)}]"
    text = PAREN_REF.sub(paren, text)
    def brackets(m):
        body = m.group(1)
        parts = [p.strip() for p in body.split(",")]
        if all(re.fullmatch(r"E\d+(\s*[-–]\s*E\d+)?", p) for p in parts):
            return m.group(0)
        notes.append(f"removed [{body}]")
        return ""
    text = CITE.sub(brackets, text)
    text = re.sub(r"\s+([.,;])", r"\1", text)
    text = re.sub(r" {2,}", " ", text).strip()
    return text, notes
def explain(alert, asset, use_llm=True):
    pkg = build_package(alert, asset)
    causes, actions = eligible(alert["rule_id"], alert["evidence"]["facts"])
    if not use_llm:
        return fallback(alert, pkg, causes, actions, {"llm_attempted": False})

    user = json.dumps({
        "evidence": pkg,
        "allowed_causes": [{"id": c["id"], "text": c["text"]} for c in causes],
        "allowed_actions": [{"id": a["id"], "text": a["text"]} for a in actions],
        "reminder": "Cite events only as [E<number>] with refs that exist in evidence.events, one ref per bracket. "
                    "Never put causes, actions or ids such as FAN_FAILURE or C1 in brackets; name them in plain words. "
                    "Use only numbers and times that appear in evidence."
    })
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    rejected = []
    for attempt in (1, 2):
        try:
            data = parse_json(call_ollama(messages))
        except Exception as exc:
            return fallback(alert, pkg, causes, actions,
                            {"llm_attempted": True, "llm_error": f"{type(exc).__name__}: {exc}"[:200],
                             "rejected_answers": rejected})
        summary = str(data.get("summary", "")).strip()
        summary, normalized = normalize_summary(summary)
        violations = check_text(summary, pkg) if summary else ["empty summary"]
        refs = {e["ref"] for e in pkg["events"]}
        
    
        for r in data.get("evidence_refs", []):
            got, bad_fmt = parse_citations(f"[{r}]")
            if bad_fmt or any(g not in refs for g in got):
                violations.append(f"unknown evidence_ref {r}")
                
        if not violations:
            cause_ids = [c for c in data.get("cause_ids", []) if c in {x["id"] for x in causes}]
            action_ids = [a for a in data.get("action_ids", []) if a in {x["id"] for x in actions}]
            chosen_causes = [c for c in causes if c["id"] in cause_ids]
            chosen_actions = [a for a in actions if a["id"] in action_ids] or actions
            dropped = [x for x in data.get("cause_ids", []) + data.get("action_ids", [])
                       if x not in cause_ids + action_ids]
            conf = data.get("confidence") if data.get("confidence") in ("low", "medium", "high") else "low"
            return finish(summary, chosen_causes, chosen_actions, pkg, "OLLAMA", conf,
                          {"passed": True, "violations": [], "attempts": attempt,
                           "rejected_answers": rejected,
                           "normalized": normalized,
                           "ignored_ids_not_in_playbook": dropped}, OLLAMA_MODEL)
        rejected.append({"attempt": attempt, "violations": violations})
        messages += [{"role": "assistant", "content": json.dumps(data)},
                     {"role": "user", "content": "Your answer was rejected: " + "; ".join(violations)
                      + ". Rewrite it using only numbers, times and citations present in the evidence."}]
    return fallback(alert, pkg, causes, actions,
                    {"llm_attempted": True, "llm_error": "answer failed grounding validation twice",
                     "rejected_answers": rejected})

"""Consistency test: the AI assistant must agree with the dashboard API, with no hardcoded answers.

Run against the running application (nothing is mocked, nothing is written):
    python3 test_chat_consistency.py                      # default http://localhost:8000
    python3 test_chat_consistency.py http://host:8000 | tee TEST_RESULTS_CHAT_CONSISTENCY.txt
Exit code 0 = all checks passed.
"""
import json
import re
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
HOURS = 24
CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
LABEL = {"HVAC": "HVAC", "NETWORK": "Network", "ACCESS": "Access control", "CCTV": "CCTV", "FIRE_ALARM": "Fire alarm"}
WORD = {"OK": "Operational", "WARNING": "Warning", "CRITICAL": "Critical", "UNKNOWN": "Unknown"}
COUNTS = r"(\d+) assets: (\d+) ok, (\d+) warning, (\d+) critical, (\d+) no data, (\d+) unknown"

results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.load(r)


def ask(message):
    req = urllib.request.Request(BASE + "/chat", data=json.dumps({"message": message, "hours": HOURS,
                                                                  "use_llm": False}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def counts_of(system):
    return (system["assets"], system["ok"], system["warning"], system["critical"], system["no_data"], system["unknown"])


def parse(answer, label, dash):
    """Return (status word, counts tuple) for `label` from a chat answer, or (None, None)."""
    m = re.search(rf"{re.escape(label)}: (\w+)(?: -|) \(?{COUNTS}", answer) or \
        re.search(rf"{re.escape(label)}: (\w+) - {COUNTS}", answer)
    return (m.group(1), tuple(int(x) for x in m.groups()[1:7])) if m else (None, None)


def main():
    print(f"Chat vs dashboard consistency test - {BASE} - window {HOURS} h\n")
    ov = get(f"/dashboard/overview?hours={HOURS}")
    systems = {s["type"]: s for s in ov["systems"]}
    check("dashboard returns the five systems", set(systems) == set(LABEL), str(sorted(systems)))

    # 1. building status: every system, status word AND counts must equal the dashboard tile
    st = ask("What is the current building status?")
    a = st["answer"]
    check("status answer comes from the database", st["source"] == "DATABASE" and st["grounding"]["passed"], st["source"])
    check("status answer has citations", bool(st["sources"]))
    for typ, label in LABEL.items():
        word, cnt = parse(a, label, systems[typ])
        check(f"status: {label} counts equal the dashboard tile", cnt == counts_of(systems[typ]),
              f"chat={cnt} dashboard={counts_of(systems[typ])}")
        # missing data must never turn into a healthy conclusion: same word the dashboard tile derives
        check(f"status: {label} status word equals dashboard ({WORD.get(systems[typ]['status'])})",
              word == WORD.get(systems[typ]["status"]), f"chat={word} dashboard={systems[typ]['status']}")
    kp = next(k for k in ov["kpis"] if k["key"] == "open_incidents")
    crit = re.search(r"(\d+) critical", kp.get("sub") or "")
    m = re.search(r"Open incidents: (\d+) \((\d+) critical\)", a)
    check("status: open incidents equal the dashboard KPI",
          bool(m) and int(m.group(1)) == kp["value"] and crit and int(m.group(2)) == int(crit.group(1)),
          f"chat={m.groups() if m else None} dashboard={kp['value']}, {kp.get('sub')}")
    check("status: active vs recovered is reported", "Active faults now:" in a)

    # Critical-alert count has one definition: critical event records in the dashboard's selected window.
    critical_kpi = next(k for k in ov["kpis"] if k["key"] == "critical_alerts")
    critical_chat = ask("How many critical alerts are in the selected window?")
    m = re.search(r"There are (\d+) critical alerts", critical_chat["answer"])
    check("critical alerts in chat equal the dashboard KPI and use database grounding",
          critical_chat["source"] == "DATABASE" and critical_chat["grounding"]["passed"] and bool(m) and
          int(m.group(1)) == critical_kpi["value"],
          f"chat={m.group(1) if m else critical_chat['answer'][:100]} dashboard={critical_kpi['value']}")

    # 2. per-system reports must equal the same tiles
    for typ, question in (("NETWORK", "Are there any network issues?"), ("ACCESS", "How is access control doing?"),
                          ("HVAC", "How is the HVAC doing?"), ("FIRE_ALARM", "What is the fire alarm status?")):
        r = ask(question)
        word, cnt = parse(r["answer"], LABEL[typ], systems[typ])
        check(f"{LABEL[typ]} report equals the dashboard tile", cnt == counts_of(systems[typ]) and
              word == WORD.get(systems[typ]["status"]), f"chat={word},{cnt} dashboard={counts_of(systems[typ])}")

    # 3. CCTV availability: consistent with the tile, and missing data is not counted as available
    r = ask("What is the CCTV availability percentage?")
    m = re.search(r"([\d.]+)% \((\d+) of (\d+) cameras available", r["answer"])
    cc = systems["CCTV"]
    check("CCTV: camera total equals the dashboard asset count", bool(m) and int(m.group(3)) == cc["assets"],
          r["answer"][:120])
    check("CCTV: cameras with no data are not counted as available",
          bool(m) and int(m.group(2)) <= cc["assets"] - cc["no_data"] - cc["unknown"], r["answer"][:120])
    check("CCTV: percentage matches the counts", bool(m) and abs(float(m.group(1)) - 100 * int(m.group(2)) / int(m.group(3))) < 0.06)

    # 4. the other demo questions answer from the database and cite their queries
    for q in ("What were the recent faults?", "What are the repair priorities?", "How did energy consumption change?"):
        r = ask(q)
        check(f"'{q}' answered from the database with citations", r["source"] == "DATABASE" and bool(r["sources"]), r["source"])
    r = ask("What were the recent faults?")
    check("faults: each event is marked ACTIVE / RECOVERED / HISTORICAL",
          "ACTIVE" in r["answer"] or "RECOVERED" in r["answer"] or "HISTORICAL" in r["answer"] or "No warning" in r["answer"])

    # 5. language: Arabic in -> Arabic out, never Chinese
    r = ask("ما حالة المبنى؟")
    check("Arabic question gets an Arabic answer", r["language"] == "ar" and re.search(r"[\u0600-\u06ff]", r["answer"]))
    for q in ("والاكسيس كونترول اخباره ايه", "What is the current building status?", "Why is it going wrong?"):
        r = ask(q)
        check(f"no Chinese/Japanese/Korean text in the answer to: {q}", not CJK.search(r["answer"]))

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # the application is not reachable, etc.
        print(f"ERROR: could not run the test: {exc!r}")
        sys.exit(2)

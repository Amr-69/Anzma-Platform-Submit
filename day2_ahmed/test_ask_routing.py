"""
HTTP-level checks for POST /ask routing against the RUNNING API (use_llm is false, so Ollama is not needed).
Run inside the api container:
    docker compose exec api python day2_ahmed/test_ask_routing.py
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

BASE = os.getenv("ANZMA_URL", "http://localhost:8000")
passed = failed = 0


def check(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print(f"  ok   {name}")
    except AssertionError as exc:
        failed += 1
        print(f"  FAIL {name}: {exc}")
    except Exception as exc:
        failed += 1
        print(f"  FAIL {name}: {type(exc).__name__}: {exc}")


def ask(question):
    """POST /ask with use_llm=false. Returns (http_status, json_body)."""
    body = json.dumps({"question": question, "use_llm": False}).encode()
    req = urllib.request.Request(f"{BASE}/ask", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def latest_alert_id():
    code, out = ask("What is the temperature problem?")
    assert code == 200, (code, out)
    return out["alert_id"]


def t_no_place_uses_latest_alert():
    code, out = ask("What is the temperature problem?")
    assert code == 200, (code, out)
    assert out["alert_id"] and out["selected_tool"] == "hvac_temperature_evidence", out
    assert out["ai_source"] == "fallback", out["ai_source"]
    assert out["diagnostics"].get("llm_attempted") is False, out["diagnostics"]


def t_named_asset():
    code, out = ask("Why is CRAC Unit 1 hot?")
    assert code == 200, (code, out)
    assert out["alert_id"] == latest_alert_id(), out["alert_id"]


def t_named_location():
    code, out = ask("Is the cooling in Cold Aisle A fine?")
    assert code == 200, (code, out)
    assert out["alert_id"] == latest_alert_id(), out["alert_id"]


def t_other_location_refused():
    code, out = ask("What is the temperature of the Lobby?")
    assert code == 404, (code, out)
    assert "Lobby" in out["detail"] and "CRAC Unit 1" in out["detail"], out["detail"]


def t_location_without_hvac_alert_refused():
    code, out = ask("Is the cooling fine in the Roof Plant?")
    assert code == 404 and "Roof Plant" in out["detail"], (code, out)


def t_other_asset_refused():
    code, out = ask("What is the temperature of Core Switch 1?")
    assert code == 404 and "Core Switch 1" in out["detail"], (code, out)


def t_unsupported_topic_unchanged():
    code, out = ask("Is the server room safe?")
    assert code == 501, (code, out)


def t_data_age_note():
    from validation import data_age_notes
    now = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    stale = data_age_notes({"last_ts": now - timedelta(hours=44)}, now=now)
    assert len(stale) == 1 and "about 44 h before now" in stale[0], stale
    assert data_age_notes({"last_ts": now - timedelta(minutes=10)}, now=now) == []
    assert data_age_notes({"last_ts": None}, now=now) == []


print(f"POST /ask routing against {BASE}")
for name, fn in [
    ("no place named -> latest alert, deterministic path identified", t_no_place_uses_latest_alert),
    ("asset named (CRAC Unit 1) -> that asset's alert", t_named_asset),
    ("location named (Cold Aisle A) -> that location's alert", t_named_location),
    ("other location (Lobby) -> 404, never another room's answer", t_other_location_refused),
    ("location without an HVAC alert (Roof Plant) -> 404", t_location_without_hvac_alert_refused),
    ("other asset (Core Switch 1) -> 404", t_other_asset_refused),
    ("unsupported topic still 501", t_unsupported_topic_unchanged),
    ("evidence older than 60 min gets a data-age limitation (unit)", t_data_age_note),
]:
    check(name, fn)
print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
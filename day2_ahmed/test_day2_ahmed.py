"""
test_day2_ahmed.py - offline tests for the Day 2 data-intelligence / reliability tasks.
No database and no Ollama are needed: events are built in memory exactly like the S1 "server-room cooling failure"
scenario in inject_scenarios.py, then pushed through alert_engine -> recommendations -> validation.

Run:  python test_day2_ahmed.py
"""
import sys
import types
from datetime import datetime, timedelta, timezone

try:                                    # alert_engine imports psycopg2 only for DB writes; not needed here
    import psycopg2.extras              # noqa: F401
except ImportError:
    pkg = types.ModuleType("psycopg2"); extras = types.ModuleType("psycopg2.extras")
    extras.Json = lambda x: x; extras.execute_values = lambda *a, **k: None
    sys.modules["psycopg2"], sys.modules["psycopg2.extras"] = pkg, extras

import ai_explainer
import alert_engine
import integrity_rules as ir
import recommendations as recs
import validation as val

RESULTS = []


def test(name):
    def deco(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc) or "assertion failed"))
        except Exception as exc:                      # a crash is a failure too
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        return fn
    return deco


# ------------------------------------------------------------------ in-memory S1 scenario
UTC = timezone.utc
START = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)        # 14:00 local = working hours
ASSET = {"id": 1, "name": "HVAC-CRAC-01", "system_type": "HVAC", "location_id": 1, "zone": "Server Room"}


def make_events(fault=True, gap=False):
    evs, n = [], 0

    def add(ts, etype, sev, value, details):
        nonlocal n
        n += 1
        evs.append({"id": n, "asset_id": 1, "ts": ts, "event_type": etype, "severity": sev, "value": value,
                    "unit": "°C", "details": details})
    base = START - timedelta(hours=24)
    for i in range(24 * 12):                              # 24 h of normal readings every 5 min
        ts = base + timedelta(minutes=5 * i)
        if gap and START - timedelta(hours=2) < ts < START:
            continue
        add(ts, "TEMPERATURE_READING", "INFO", 20 + (0.2 if i % 2 else -0.2), {"setpoint": 20.0, "fan_on": True})
    if fault:
        for i in range(19):                               # 90 min: 20 -> 34 C, fan stops after 4 readings
            ts = START + timedelta(minutes=5 * i)
            temp = 20 + 14 * (i / 18) ** 1.2
            etype, sev = ("HIGH_TEMPERATURE", "CRITICAL" if temp > 32 else "WARNING") if temp > 30 else \
                         ("TEMPERATURE_READING", "INFO")
            add(ts, etype, sev, round(temp, 2), {"setpoint": 20.0, "fan_on": i < 4})
    return {"assets": {1: ASSET}, "ev": {1: sorted(evs, key=lambda e: e["ts"])}, "end": START + timedelta(hours=2)}


def run_rules(ctx):
    out = []
    for fn in alert_engine.RULE_FUNCS:
        out.extend(fn(ctx))
    return out


CTX = make_events()
ALERTS = run_rules(CTX)
HIGH = next(a for a in ALERTS if a["rule_id"] == "HVAC_HIGH_TEMP")


# ------------------------------------------------------------------ 1. integrity rules
@test("integrity: every event type the generator/scenarios emit is valid for its system")
def _():
    emitted = {"HVAC": ["TEMPERATURE_READING", "HIGH_TEMPERATURE", "FILTER_FAULT"],
               "NETWORK": ["BANDWIDTH_USAGE", "LINK_DOWN", "LINK_UP", "UPTIME_REPORT"],
               "ACCESS": ["ACCESS_GRANTED", "ACCESS_DENIED", "DOOR_HELD_OPEN", "DOOR_FORCED"],
               "CCTV": ["MOTION_DETECTED", "CAMERA_OFFLINE", "CAMERA_ONLINE"],
               "FIRE_ALARM": ["PANEL_HEARTBEAT", "SMOKE_DETECTED", "ALARM_RESET", "BATTERY_LOW"]}
    for system, types_ in emitted.items():
        for t in types_:
            assert ir.check_event(system, t) == [], (system, t)


@test("integrity: incompatible event/equipment pairs are rejected (e.g. SMOKE_DETECTED on a chiller)")
def _():
    assert ir.check_event("HVAC", "SMOKE_DETECTED")
    assert ir.check_event("NETWORK", "HIGH_TEMPERATURE")
    assert ir.check_event("CCTV", "DOOR_FORCED")
    assert ir.check_event("FIRE_ALARM", "LINK_DOWN")
    assert ir.check_event("HVAC", "TEMPERATURE_READING", unit="Mbps")      # wrong unit
    assert ir.check_event("PUMP", "TEMPERATURE_READING")                    # unknown system


@test("integrity: alert rules cannot be raised on incompatible equipment")
def _():
    assert ir.check_rule_target("HVAC_HIGH_TEMP", "HVAC") == []
    assert ir.check_rule_target("HVAC_HIGH_TEMP", "NETWORK")
    assert ir.check_rule_target("FIRE_SMOKE", "ACCESS")
    assert ir.check_rule_target("SEC_BLIND_SPOT_ACCESS", "CCTV") == []
    from alert_config import RULES
    for rid in RULES:                                   # every configured rule maps to a system family
        assert not any("no known system family" in m for m in ir.check_rule_target(rid, "HVAC")), rid


@test("integrity: evidence of another system is only allowed for the CCTV/ACCESS blind-spot rule")
def _():
    assert ir.check_evidence_event("CCTV", "SEC_BLIND_SPOT_ACCESS", "ACCESS", "ACCESS_GRANTED") == []
    assert ir.check_evidence_event("HVAC", "HVAC_HIGH_TEMP", "ACCESS", "ACCESS_GRANTED")
    assert ir.check_evidence_event("HVAC", "HVAC_HIGH_TEMP", "HVAC", "HIGH_TEMPERATURE") == []


@test("integrity: historical events vs current conditions (opener without closer = current)")
def _():
    t = lambda m: START + timedelta(minutes=m)
    closed = [{"ts": t(0), "event_type": "LINK_DOWN"}, {"ts": t(3), "event_type": "LINK_UP"}]
    assert ir.current_conditions(closed) == []                              # history only, nothing current
    still_open = closed + [{"ts": t(10), "event_type": "LINK_DOWN"}]
    assert [e["ts"] for e in ir.current_conditions(still_open)] == [t(10)]
    many_past = [{"ts": t(i * 2), "event_type": "SMOKE_DETECTED"} if i % 2 == 0 else
                 {"ts": t(i * 2), "event_type": "ALARM_RESET"} for i in range(10)]
    assert ir.current_conditions(many_past) == []                           # many past events != active alarm


@test("integrity: compat SQL covers every catalogue row")
def _():
    sql = ir.compat_sql()
    assert sql.count("('") == len(ir.compat_rows())
    assert "('HVAC', 'HIGH_TEMPERATURE', 'FAULT', '°C')" in sql


# ------------------------------------------------------------------ 2. alert detection on the simulated fault
@test("alerts: cooling failure becomes a CRITICAL HVAC_HIGH_TEMP alert with fan_off and sustained facts")
def _():
    f = HIGH["evidence"]["facts"]
    assert HIGH["severity"] == "CRITICAL" and f["fan_off"] is True and f["sustained"] is True, f
    assert f["max_value"] > 32 and f["zone"] == "Server Room"


@test("alerts: normal operation produces no alert (no false positives)")
def _():
    assert run_rules(make_events(fault=False)) == []


@test("alerts: every alert's evidence is traceable to real events and compatible with the asset")
def _():
    ids = {e["id"] for e in CTX["ev"][1]}
    for a in ALERTS:
        assert ir.check_rule_target(a["rule_id"], "HVAC") == [], a["rule_id"]
        for e in a["evidence"]["events"]:
            assert e["event_id"] in ids
            assert ir.check_event("HVAC", e["event_type"]) == []


# ------------------------------------------------------------------ 3. recommendations
@test("recommendations: derived only from playbook rules whose conditions hold, linked to evidence")
def _():
    r = recs.recommend(HIGH)
    ids = {x["action_id"] for x in r["recommendations"]}
    assert "HVAC_FAN" in ids and "HVAC_COOLING" in ids and "HVAC_IT_NOTIFY" in ids, ids
    assert "HVAC_FILTER" not in ids                                         # no filter fault in the evidence
    assert all(x["derived_by"] == "PLAYBOOK" and x["evidence_refs"] and x["basis"] for x in r["recommendations"])
    assert recs.is_approved(HIGH, "HVAC_FAN") and not recs.is_approved(HIGH, "REPLACE_CHILLER")


@test("recommendations: unknown rule or empty evidence yields no recommendation and a limitation")
def _():
    assert recs.recommend({"rule_id": "NOPE", "evidence": {}})["recommendations"] == []
    empty = {"rule_id": "HVAC_HIGH_TEMP", "evidence": {"facts": {}, "events": []}}
    out = recs.recommend(empty)
    assert out["recommendations"] == [] and out["limitations"]


# ------------------------------------------------------------------ 4. validation layer
@test("validation: deterministic explanation is grounding_supported only because the checks really ran")
def _():
    out = val.explain_alert(HIGH, ASSET, use_llm=False)
    assert out["validation"]["status"] == "grounding_supported" and out["validation"]["checks_run"]
    assert out["validation"]["errors"] == []
    assert out["data_sufficiency"] == "complete" and out["ai_source"] == "fallback" and out["model_used"] is None
    assert out["references"] == {"assets": [1], "incidents": []} and out["recommendations"]
    assert out["tool_used"] == "explain_alert"


@test("validation: an invented reading is rejected")
def _():
    bad = {"summary": "The temperature hit 41.7 °C [E1].", "probable_causes": [], "recommended_actions": [],
           "evidence_refs": ["E1"]}
    v = val.validate_explanation(HIGH, ASSET, bad)
    assert v["status"] == "failed" and any("41.7" in x for x in v["errors"]), v


@test("validation: invented cause, unapproved action and unknown citation are rejected")
def _():
    bad = {"summary": "Peak reached [E99].", "probable_causes": [{"id": "COMPRESSOR_BURNT"}],
           "recommended_actions": [{"id": "REPLACE_CHILLER"}], "evidence_refs": ["E99"]}
    v = val.validate_explanation(HIGH, ASSET, bad)["errors"]
    assert any("unknown citation" in x for x in v)
    assert any("COMPRESSOR_BURNT" in x for x in v) and any("REPLACE_CHILLER" in x for x in v)
    assert any("E99" in x and "does not exist" in x for x in v)


@test("validation: a cause whose condition does not hold is rejected (FILTER_BLOCKAGE without a filter fault)")
def _():
    exp = {"summary": "Fault at 14:00 [E1].", "probable_causes": [{"id": "FILTER_BLOCKAGE"}],
           "recommended_actions": [], "evidence_refs": ["E1"]}
    assert any("FILTER_BLOCKAGE" in x for x in val.validate_explanation(HIGH, ASSET, exp)["errors"])


@test("validation: no stored evidence -> insufficient, never grounding_supported, with a limitation")
def _():
    empty = {"id": 7, "rule_id": "HVAC_HIGH_TEMP", "severity": "WARNING", "summary": "x",
             "evidence": {"facts": {}, "events": [], "thresholds": {}, "event_count": 0}}
    out = val.explain_alert(empty, ASSET, use_llm=False)
    assert out["data_sufficiency"] == "no_data" and out["validation"]["status"] == "not_validated"
    assert out["validation"]["checks_run"] == [] and out["ai_source"] == "rules"
    assert out["limitations"], "a limitation must explain why no conclusion is drawn"
    assert out["recommendations"] == [] and out["evidence"] == [], "nothing may be recommended without evidence"


@test("validation: grounding_supported is refused when errors are present, and 'partial' is used when no cause is supported")
def _():
    try:
        val.standard_response(answer="a", evidence=[], references={}, model_used=None, tool_used="t",
                              ai_source="llm", data_sufficiency="complete", limitations=[],
                              validation={"status": "grounding_supported", "checks_run": ["x"], "errors": ["boom"]})
        raise AssertionError("accepted grounding_supported with errors")
    except ValueError:
        pass
    denial = {"id": 9, "rule_id": "ACC_DENIAL_SPIKE", "severity": "WARNING", "detection_method": "THRESHOLD", "summary": "s", "asset_id": 2,
              "evidence": {"event_count": 1, "thresholds": {}, "events": [
                  {"ref": "E1", "event_id": 1, "ts": START.isoformat(), "event_type": "ACCESS_DENIED",
                   "value": None, "unit": None, "details": {}}],
                           "facts": {"unknown_card_share": 0.2, "permission_share": 0.2, "distinct_cards": 9}},
              "first_ts": START, "last_ts": START}
    out = val.explain_alert(denial, {"id": 2, "name": "DOOR-X-01", "system_type": "ACCESS", "zone": "Lobby"}, use_llm=False)
    assert out["data_sufficiency"] == "partial", out["data_sufficiency"]


@test("validation: standard_response refuses 'grounding_supported' without recorded checks")
def _():
    try:
        val.standard_response(answer="a", evidence=[], references={}, model_used=None, tool_used="t",
                              ai_source="rules", data_sufficiency="complete", validation={"status": "grounding_supported"},
                              limitations=[])
    except ValueError:
        return
    raise AssertionError("grounding_supported label was accepted without checks")


@test("validation: truncated evidence is disclosed as a limitation")
def _():
    big = dict(HIGH, evidence=dict(HIGH["evidence"], event_count=57))
    out = val.explain_alert(big, ASSET, use_llm=False)
    assert any("of 57" in x for x in out["limitations"]), out["limitations"]


# ------------------------------------------------------------------ 5. safe fallback when the model is down
@test("fallback: Ollama unreachable -> deterministic grounding_supported answer with the limitation stated")
def _():
    ai_explainer.OLLAMA_HOST, ai_explainer.OLLAMA_TIMEOUT = "http://127.0.0.1:1", 2     # nothing listens here
    out = val.explain_alert(HIGH, ASSET, use_llm=True)
    assert out["ai_source"] == "fallback" and out["model_used"] is None, out["ai_source"]
    assert out["validation"]["status"] == "grounding_supported"
    assert any("language model was unavailable" in x for x in out["limitations"]), out["limitations"]
    assert out["recommendations"], "recommendations must not depend on the model"


# ------------------------------------------------------------------ 6. missing data
@test("missing data: a gap in readings does not invent an alert or a reading")
def _():
    alerts = run_rules(make_events(fault=False, gap=True))
    assert alerts == [], [a["rule_id"] for a in alerts]


# ------------------------------------------------------------------ Day 2 fixes: status wording, playbook approval
@test("D4: the success status is 'grounding_supported' with scope 'grounding'; 'validated' no longer exists")
def _():
    out = val.explain_alert(HIGH, ASSET, use_llm=False)
    assert out["validation"]["status"] == "grounding_supported" and out["validation"]["scope"] == "grounding"
    assert not hasattr(val, "VALIDATED") and out["validation"]["status"] != "validated"
    try:
        val.standard_response(answer="a", evidence=[], references={}, model_used=None, tool_used="t", ai_source="rules",
                              data_sufficiency="complete", limitations=[], validation={"status": "grounding_supported"})
        raise AssertionError("accepted grounding_supported without checks")
    except ValueError:
        pass


@test("playbook: every action carries a status and an approver")
def _():
    import alert_config as cfg
    acts = [a for b in cfg.PLAYBOOKS.values() for a in b["actions"]]
    assert acts and all(a["status"] in ("approved", "draft") and a["approver"] for a in acts)


@test("playbook: a DRAFT action is never recommended, never eligible and is rejected by the validator")
def _():
    import alert_config as cfg
    action = next(a for a in cfg.PLAYBOOKS["HVAC_TEMP"]["actions"] if a["id"] == "HVAC_COOLING")
    assert "HVAC_COOLING" in {x["action_id"] for x in recs.recommend(HIGH)["recommendations"]}
    action["status"] = cfg.DRAFT
    try:
        assert "HVAC_COOLING" not in {x["action_id"] for x in recs.recommend(HIGH)["recommendations"]}
        assert not recs.is_approved(HIGH, "HVAC_COOLING")
        assert "HVAC_COOLING" not in {a["id"] for a in ai_explainer.eligible("HVAC_HIGH_TEMP", HIGH["evidence"]["facts"])[1]}
        bad = {"summary": "Peak reached [E1].", "probable_causes": [], "recommended_actions": [{"id": "HVAC_COOLING"}],
               "evidence_refs": ["E1"]}
        v = val.validate_explanation(HIGH, ASSET, bad)
        assert v["status"] == "failed" and any("HVAC_COOLING" in e for e in v["errors"]), v
    finally:
        action["status"] = cfg.APPROVED
    assert "HVAC_COOLING" in {x["action_id"] for x in recs.recommend(HIGH)["recommendations"]}


@test("freshness: as_of adds a limitation to an explanation")
def _():
    out = val.explain_alert(HIGH, ASSET, use_llm=False, as_of=START)
    assert any("current only up to 2026-11-05 12:00" in x for x in out["limitations"]), out["limitations"]


if __name__ == "__main__":
    width = max(len(n) for n, _, _ in RESULTS)
    for name, ok, msg in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      -> {msg}"))
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed")
    sys.exit(0 if passed == len(RESULTS) else 1)

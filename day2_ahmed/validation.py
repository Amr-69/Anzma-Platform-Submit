"""
validation.py  (Day 2 - Ahmed: evidence validation and AI reliability)

Aligned with API_CONTRACT.md section 8:
  validation = {"status": validated|not_validated|failed, "checks_run": [...], "errors": [...], "attempts": n}
  "validated" only when checks_run is non-empty and every check passed.
  data_sufficiency : complete | partial | no_data
  ai_source        : llm | fallback | rules
  references       : {"assets": [ids], "incidents": [ids]}

Validation runs on the FINAL text whatever produced it (model or deterministic fallback). checks_run lists only
checks that really ran. These checks are GROUNDING checks (values, citations, playbook membership); they are not a
semantic check of meaning - see the note in the hand-over about DATA_MODEL rule 7.
"""
import ai_explainer as ai
import recommendations as recs

VALIDATED, FAILED, NOT_VALIDATED = "validated", "failed", "not_validated"
COMPLETE, PARTIAL, NO_DATA = "complete", "partial", "no_data"
AI_SOURCE = {"OLLAMA": "llm", "FALLBACK": "fallback", "RULES": "rules"}

# check names implemented for ALERT explanations (first four are shared with the chat validator in the contract;
# the last three are alert-specific and must be added to the contract's checks_run list)
ALERT_CHECKS = ["citation_exists", "number_in_evidence", "time_in_evidence", "date_in_evidence",
                "cause_supported_by_facts", "action_in_playbook", "evidence_ref_exists"]


def _check_name(violation):
    v = violation.lower()
    if "citation" in v:
        return "citation_exists"
    if v.startswith("number"):
        return "number_in_evidence"
    if v.startswith("time"):
        return "time_in_evidence"
    if v.startswith("date"):
        return "date_in_evidence"
    return "other"


def assess_evidence(evidence, causes_supported=True):
    """-> (data_sufficiency, limitations) for one alert's evidence."""
    ev = evidence or {}
    events, facts = ev.get("events") or [], ev.get("facts") or {}
    if not events or not facts:
        return NO_DATA, ["No supporting events are stored for this alert, so no conclusion can be drawn."]
    limitations, level = [], COMPLETE
    total = ev.get("event_count", len(events))
    if total > len(events):
        limitations.append(f"Evidence lists {len(events)} of {total} supporting events "
                           f"(first, last and peak are always included).")
    if not causes_supported:
        level = PARTIAL
    return level, limitations


def validate_explanation(alert, asset, explanation, attempts=1):
    """Re-check a finished explanation against stored evidence. Returns the contract's `validation` object."""
    pkg = ai.build_package(alert, asset)
    errors = list(ai.check_text(explanation.get("summary", ""), pkg))
    result = recs.recommend(alert)
    ok_causes = {c["id"] for c in result["probable_causes"]}
    ok_actions = {r["action_id"] for r in result["recommendations"]}
    refs = {e["ref"] for e in pkg["events"]}
    for c in explanation.get("probable_causes", []):
        if c.get("id") not in ok_causes:
            errors.append(f"cause {c.get('id')} is not supported by the measured facts")
    for a in explanation.get("recommended_actions", []):
        if a.get("id") not in ok_actions:
            errors.append(f"action {a.get('id')} is not an approved action for this alert")
    for r in explanation.get("evidence_refs", []):
        if r not in refs:
            errors.append(f"evidence ref {r} does not exist")
    return {"status": FAILED if errors else VALIDATED, "checks_run": list(ALERT_CHECKS),
            "errors": errors, "attempts": attempts}


def standard_response(*, answer, evidence, references, model_used, tool_used, ai_source, data_sufficiency,
                      validation, limitations, window=None, language="en"):
    """Contract envelope. Refuses 'validated' unless checks ran and nothing failed."""
    status = (validation or {}).get("status", NOT_VALIDATED)
    if status == VALIDATED and (not validation.get("checks_run") or validation.get("errors")):
        raise ValueError("'validated' requires recorded checks and no errors")
    return {"answer": answer, "language": language, "tool_used": tool_used, "model_used": model_used,
            "ai_source": ai_source, "data_sufficiency": data_sufficiency, "window": window,
            "evidence": evidence, "references": references, "validation": validation or
            {"status": NOT_VALIDATED, "checks_run": [], "errors": [], "attempts": 0},
            "limitations": limitations}


def _refs(alert, asset):
    aid = alert.get("asset_id", asset.get("id"))
    inc = alert.get("incident_id")
    return {"assets": [aid] if aid is not None else [], "incidents": [inc] if inc is not None else []}


def explain_alert(alert, asset, use_llm=True):
    """Alert -> explanation -> independent validation -> contract response (+ playbook recommendations)."""
    rec = recs.recommend(alert)
    sufficiency, limitations = assess_evidence(alert.get("evidence"), causes_supported=bool(rec["probable_causes"]))
    limitations = limitations + rec["limitations"]

    if sufficiency == NO_DATA:
        return standard_response(
            answer=f"The database does not hold enough evidence to explain this alert. {alert.get('summary', '')}".strip(),
            evidence=[], references=_refs(alert, asset), model_used=None, tool_used="explain_alert",
            ai_source="rules", data_sufficiency=sufficiency,
            validation={"status": NOT_VALIDATED, "checks_run": [], "errors": [], "attempts": 0},
            limitations=limitations) | {"recommendations": [], "probable_causes": []}

    explanation = ai.explain(alert, asset, use_llm=use_llm)
    g = explanation.get("grounding", {})
    attempts = g.get("attempts") or (len(g.get("rejected_answers", [])) or 0)
    validation = validate_explanation(alert, asset, explanation, attempts=attempts)
    if g.get("llm_attempted") and explanation["source"] == "FALLBACK":
        limitations.append("The language model was unavailable or its answer was rejected; this explanation was "
                           "built deterministically from the stored evidence.")
    if validation["status"] == FAILED:
        limitations.append("The explanation failed validation, so only the raw alert summary is shown.")
        answer = alert.get("summary", "")
    else:
        answer = explanation["summary"]

    return standard_response(
        answer=answer, evidence=(alert.get("evidence") or {}).get("events", []), references=_refs(alert, asset),
        model_used=explanation.get("model"), tool_used="explain_alert",
        ai_source=AI_SOURCE[explanation["source"]], data_sufficiency=sufficiency,
        validation=validation, limitations=limitations) | {
        "recommendations": rec["recommendations"], "probable_causes": rec["probable_causes"],
        "diagnostics": {k: g[k] for k in ("llm_attempted", "llm_error", "rejected_answers", "attempts") if k in g}}

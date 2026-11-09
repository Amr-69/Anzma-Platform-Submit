"""
recommendations.py  (Day 2 - Ahmed: evidence-supported corrective recommendations)

Every recommendation comes from an approved playbook rule in alert_config.PLAYBOOKS and is offered only when
the facts measured for the alert satisfy that rule's condition. The language model never creates or gates a
recommendation: it may only phrase an explanation. Each recommendation carries the facts that justify it and
the evidence refs of the alert, so it can be traced back to stored events.
"""
import ai_explainer as ai
import alert_config as cfg


def recommend(alert):
    """
    alert: dict with rule_id, evidence {facts, events, ...} and optionally id.
    Returns {"recommendations": [...], "probable_causes": [...], "limitations": [...]}.
    """
    rule_id = alert["rule_id"]
    if rule_id not in cfg.RULES:
        return {"recommendations": [], "probable_causes": [],
                "limitations": [f"No approved playbook exists for rule {rule_id}; no recommendation is offered."]}
    ev = alert.get("evidence") or {}
    facts = ev.get("facts") or {}
    refs = [e["ref"] for e in ev.get("events", [])]
    playbook = cfg.RULES[rule_id]["playbook"]
    causes, actions = ai.eligible(rule_id, facts)

    limitations = []
    if not refs:
        return {"recommendations": [], "probable_causes": [],
                "limitations": ["The alert has no stored supporting events, so no recommendation can be supported."]}
    if not causes:
        limitations.append("The evidence does not support a specific probable cause; the actions listed are the "
                           "standard response for this rule.")

    recs = []
    for a in actions:
        recs.append({
            "recommendation_id": f"{alert.get('id', 'new')}:{a['id']}",
            "action_id": a["id"], "priority": a["priority"], "text": a["text"],
            "basis": a["because"],                     # facts that satisfied the playbook condition
            "playbook": playbook, "rule_id": rule_id, "alert_id": alert.get("id"),
            "evidence_refs": refs,
            "derived_by": "PLAYBOOK",                  # never "MODEL"
        })
    return {"recommendations": recs, "probable_causes": causes, "limitations": limitations}


def is_approved(alert, action_id):
    """True only if action_id is a playbook action whose condition holds for this alert's facts."""
    return any(r["action_id"] == action_id for r in recommend(alert)["recommendations"])

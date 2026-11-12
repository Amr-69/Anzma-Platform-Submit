"""
pipeline.py (Day 2): the end-to-end workflow in one function.
Stored facts -> alert detection -> incidents -> evidence -> explanation -> grounding check -> recommendations.
Everything reads/writes through db_adapter / incident_engine / evidence; the dashboard and the API only call this.
A lost or unreachable database raises db_adapter.DatabaseUnavailable (map it to 503 DATA_UNAVAILABLE).
"""
from datetime import timedelta

import alert_engine
import db_adapter as da
import evidence
import incident_engine as inc
import validation as val


@da.guard
def detect(cur, hours=None):
    """Run the rules over the stored facts and store new alerts, then group alerts into incidents. Safe to re-run.

    "Now" is the latest stored record, not the wall clock: `as_of` says so explicitly and `limitations` repeats it."""
    end = da.latest_ts(cur)
    if end is None:
        return {"as_of": None, "window": None, "detected": 0, "new_alerts": 0, "incidents_created": 0,
                "alerts_attached": 0, "limitations": ["The database holds no telemetry or events yet."]}
    start = end - timedelta(hours=hours) if hours else end - timedelta(days=30)
    alerts = alert_engine.run_engine(cur, start, end)
    new_ids = da.store_alerts(cur, alerts)
    grouped = inc.assign_incidents(cur)
    return {"as_of": end.isoformat(), "window": {"from": start, "to": end}, "detected": len(alerts),
            "new_alerts": len(new_ids), "incidents_created": grouped["created"],
            "alerts_attached": grouped["attached"], "limitations": [val.freshness_limitation(end)]}


@da.guard
def explain_alert(cur, alert_id, use_llm=True, persist=True):
    """Explanation + grounding check + recommendations for one stored alert.

    By default the response is saved on the alert. Callers that only need an ephemeral explanation can pass
    ``persist=False``.
    The evidence rows are re-read from the fact tables first; an unverified row stops the explanation."""
    alert = da.get_alert(cur, alert_id)
    if alert is None:
        raise LookupError(f"alert {alert_id} not found")
    asset = da.get_asset(cur, alert["asset_id"])
    verification = evidence.alert_evidence(cur, alert_id)
    response = val.explain_alert(alert, asset, use_llm=use_llm, verification=verification,
                                 as_of=da.latest_ts(cur))
    if persist:
        da.save_explanation(cur, alert_id, response)
    return response


@da.guard
def explain_incident(cur, incident_id, use_llm=True):
    """One grounding-checked explanation per alert of the incident, plus verified evidence and merged recommendations."""
    incident = inc.get_incident(cur, incident_id)
    if incident is None:
        raise LookupError(f"incident {incident_id} not found")
    return {"incident": incident, "evidence": evidence.incident_evidence(cur, incident_id),
            "explanations": {a: explain_alert(cur, a, use_llm) for a in incident["alert_ids"]}}

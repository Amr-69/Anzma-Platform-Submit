"""
pipeline.py (Day 2): the end-to-end workflow in one function.
Stored facts -> alert detection -> incidents -> evidence -> explanation -> validation -> recommendations.
Everything reads/writes through db_adapter / incident_engine / evidence; the dashboard and the API only call this.
"""
from datetime import timedelta

import alert_engine
import db_adapter as da
import evidence
import incident_engine as inc
import validation as val


def detect(cur, hours=None):
    """Run the rules over the stored facts (window anchored to the latest stored record) and store new alerts,
    then group alerts into incidents. Safe to run repeatedly."""
    end = da.latest_ts(cur)
    start = end - timedelta(hours=hours) if hours else end - timedelta(days=30)
    alerts = alert_engine.run_engine(cur, start, end)
    new_ids = da.store_alerts(cur, alerts)
    grouped = inc.assign_incidents(cur)
    return {"window": {"from": start, "to": end}, "detected": len(alerts), "new_alerts": len(new_ids),
            "incidents_created": grouped["created"], "alerts_attached": grouped["attached"]}


def explain_alert(cur, alert_id, use_llm=True):
    """Explanation + independent validation + recommendations for one stored alert; the result is saved on the alert."""
    alert = da.get_alert(cur, alert_id)
    if alert is None:
        raise LookupError(f"alert {alert_id} not found")
    asset = da.get_asset(cur, alert["asset_id"])
    response = val.explain_alert(alert, asset, use_llm=use_llm)
    da.save_explanation(cur, alert_id, response)
    return response


def explain_incident(cur, incident_id, use_llm=True):
    """One validated explanation per alert of the incident, plus the verified evidence and merged recommendations."""
    incident = inc.get_incident(cur, incident_id)
    if incident is None:
        raise LookupError(f"incident {incident_id} not found")
    return {"incident": incident, "evidence": evidence.incident_evidence(cur, incident_id),
            "explanations": {a: explain_alert(cur, a, use_llm) for a in incident["alert_ids"]}}

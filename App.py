import os
import sys
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from psycopg import OperationalError

from app.dashboard_core_pg import (
    DashboardError,
    NoDataError,
    get_dashboard_summary,
    get_filter_options,
    open_readonly,
)

load_dotenv()

sys.path.insert(
    0,
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "day2_ahmed"),
)

import ai_explainer
import db_adapter as da
import evidence as evd
import incident_engine as inc
import pipeline


asgi_app = FastAPI(title="Anzma Dashboard API")


@asgi_app.get("/")
def dashboard():
    return FileResponse("static/index.html")


def day2_connect():
    """Open a psycopg2 connection for Ahmed's Day 2 engine."""
    return psycopg2.connect(
        host=os.getenv("PGHOST", "db"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "anzma"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


@asgi_app.get("/health")
def health():
    return {"status": "ok"}


@asgi_app.get("/dashboard/filters")
def dashboard_filters():
    try:
        with open_readonly() as conn:
            return get_filter_options(conn)
    except (NoDataError, OperationalError) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@asgi_app.get("/dashboard/summary")
def dashboard_summary(
    system_type: str | None = Query(default=None),
    building_id: int | None = Query(default=None),
    start_time: str | None = Query(default=None),
    end_time: str | None = Query(default=None),
):
    try:
        with open_readonly() as conn:
            return get_dashboard_summary(
                conn,
                system_type=system_type,
                building_id=building_id,
                start_time=start_time,
                end_time=end_time,
            )
    except (DashboardError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except NoDataError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@asgi_app.get("/incidents")
def list_incidents():
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                return {"incidents": inc.list_incidents(cur)}
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@asgi_app.get("/incidents/{incident_id}")
def get_incident(incident_id: int):
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                incident = inc.get_incident(cur, incident_id)
                if incident is None:
                    raise HTTPException(status_code=404, detail="Incident not found")
                return incident
    except HTTPException:
        raise
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@asgi_app.get("/incidents/{incident_id}/evidence")
def get_incident_evidence(incident_id: int):
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                incident = inc.get_incident(cur, incident_id)
                if incident is None:
                    raise HTTPException(status_code=404, detail="Incident not found")
                return evd.incident_evidence(cur, incident_id)
    except HTTPException:
        raise
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@asgi_app.post("/alerts/{alert_id}/explain")
def explain_alert(alert_id: int, use_llm: bool = Query(default=False)):
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                alert = da.get_alert(cur, alert_id)
                if alert is None:
                    raise HTTPException(status_code=404, detail="Alert not found")

                result = pipeline.explain_alert(cur, alert_id, use_llm=use_llm)
                conn.commit()
                return result
    except HTTPException:
        raise
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


class AskRequest(BaseModel):
    question: str
    use_llm: bool = False


@asgi_app.post("/ask")
def ask(request: AskRequest) -> dict[str, Any]:
    """
    Minimal Day 2 operational-question router.

    Routes a CRAC / temperature question to the HVAC temperature evidence,
    explanation, validation, and recommendation workflow.
    """
    question = request.question.lower()

    if not any(word in question for word in ("crac", "temperature", "hvac", "cooling")):
        raise HTTPException(
            status_code=501,
            detail=(
                "This Day 2 demo supports CRAC, HVAC, temperature, and cooling "
                "questions. Other domains will be added in Day 3."
            ),
        )

    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                pipeline.detect(cur)
                conn.commit()

                cur.execute(
                    """
                    SELECT alert_id
                    FROM alerts
                    WHERE rule_id = 'HVAC_HIGH_TEMP'
                    ORDER BY detected_ts DESC, alert_id DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()

                if row is None:
                    raise HTTPException(
                        status_code=404,
                        detail="No HVAC temperature alert is currently available.",
                    )

                alert_id = row["alert_id"]
                result = pipeline.explain_alert(
                    cur,
                    alert_id,
                    use_llm=request.use_llm,
                )
                conn.commit()

                return {
                    "question": request.question,
                    "selected_tool": "hvac_temperature_evidence",
                    "alert_id": alert_id,
                    **result,
                }
    except HTTPException:
        raise
    except OperationalError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
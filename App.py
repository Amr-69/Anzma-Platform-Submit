import os
import sys
from typing import Any
from datetime import datetime, timedelta, timezone


import asyncio
import logging

import psycopg2
import psycopg2.extras
import socketio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
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
import alert_config as cfg
import db_adapter as da
import evidence as evd
import incident_engine as inc
import pipeline


# Every route below is registered on `api`; `asgi_app` (bottom of the file) wraps it with Socket.IO for live alerts.
api = FastAPI(title="Anzma Dashboard API")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger("anzma")

# a lost / unreachable database from either driver, or from the Day 2 adapter, is a 503, never a crash
DB_ERRORS = (OperationalError, psycopg2.OperationalError, da.DatabaseUnavailable)

api.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@api.get("/", include_in_schema=False)
@api.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(os.path.join(BASE_DIR, "static", "dashboard.html"))


def day2_connect():
    """Open a psycopg2 connection for Ahmed's Day 2 engine."""
    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "anzma"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


@api.get("/health")
def health():
    return {"status": "ok"}


@api.get("/dashboard/filters")
def dashboard_filters():
    try:
        with open_readonly() as conn:
            return get_filter_options(conn)
    except (NoDataError, *DB_ERRORS) as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.get("/dashboard/summary")
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
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.get("/incidents")
def list_incidents():
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                return {"incidents": inc.list_incidents(cur)}
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.get("/incidents/{incident_id}")
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
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.get("/incidents/{incident_id}/evidence")
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
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


class IncidentStatusIn(BaseModel):
    status: str


@api.post("/incidents/{incident_id}/status", tags=["Incidents"])
def set_incident_status(incident_id: int, body: IncidentStatusIn):
    """Move an incident along open -> acknowledged -> resolved (resolved is final); its alerts follow."""
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                inc.set_status(cur, incident_id, body.status)
                conn.commit()
                return inc.get_incident(cur, incident_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@api.post("/alerts/{alert_id}/explain")
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
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))


class AskRequest(BaseModel):
    question: str
    use_llm: bool = False


@api.post("/ask")
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
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))

# ======================================================================================================
# Dashboard page, assistant widget, detection and live alerts
# ======================================================================================================
from app import chat_lite, frontend_api  # noqa: E402

api.include_router(frontend_api.create_router())
api.include_router(chat_lite.create_router(
    day2_connect, pipeline, inc, use_llm_default=os.getenv("CHAT_USE_LLM", "true").lower() == "true",
    db_errors=DB_ERRORS))


@api.post("/alerts/run", tags=["Alerts"])
def run_detection(hours: int | None = Query(default=None, ge=1, le=720)):
    """Run the alert rules over the stored data and group the alerts into incidents. Safe to repeat: alerts that
    already exist are not duplicated. The live dashboard receives any new alert through Socket.IO."""
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                result = pipeline.detect(cur, hours=hours)
                conn.commit()
                return jsonable_encoder(result)
    except DB_ERRORS as exc:
        raise HTTPException(status_code=503, detail=str(exc))

SEVERITIES = ("INFO", "WARNING", "CRITICAL")
FUTURE_TOLERANCE = timedelta(minutes=5)          # same rule as API_CONTRACT.md: now + 5 min at most


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must include a timezone, e.g. 2026-11-11T08:00:00Z")
    return value.astimezone(timezone.utc)


class AlertCreate(BaseModel):
    rule_id: str                                         # must be a key of alert_config.RULES
    asset_id: int
    severity: str                                        # INFO | WARNING | CRITICAL (any case)
    summary: str = Field(min_length=1, max_length=2000)
    title: str | None = Field(default=None, max_length=200)   # default: "<rule title> - <asset name>"
    first_ts: datetime
    last_ts: datetime | None = None                      # default: first_ts
    facts: dict[str, Any] = Field(default_factory=dict)  # optional measured facts, kept in alerts.evidence

    @field_validator("severity")
    @classmethod
    def _severity(cls, v):
        v = v.upper()
        if v not in SEVERITIES:
            raise ValueError("severity must be INFO, WARNING or CRITICAL")
        return v

    @model_validator(mode="after")
    def _times(self):
        self.first_ts = _utc(self.first_ts)
        self.last_ts = _utc(self.last_ts) if self.last_ts else self.first_ts
        if self.last_ts < self.first_ts:
            raise ValueError("last_ts must not be before first_ts")
        if self.last_ts > datetime.now(timezone.utc) + FUTURE_TOLERANCE:
            raise ValueError("timestamps in the future are rejected")
        return self


@api.post("/alerts", status_code=201, tags=["Alerts"])
def create_alert(body: AlertCreate):
    """Store one alert by hand (same table, same shape as the rule engine's alerts) and group it into an incident."""
    rule = cfg.RULES.get(body.rule_id)
    if rule is None:
        raise HTTPException(422, f"Unknown rule_id. Allowed: {', '.join(sorted(cfg.RULES))}.")
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                asset = da.get_asset(cur, body.asset_id)
                if asset is None:
                    raise HTTPException(422, f"Unknown asset_id {body.asset_id}.")
                new_ids = da.store_alerts(cur, [{
                    "rule_id": body.rule_id,
                    "detection_method": rule["method"],              # taken from the rule, never from the client
                    "severity": body.severity,
                    "asset_id": body.asset_id,
                    "title": body.title or f"{rule['title']} - {asset['name']}",
                    "summary": body.summary,
                    "first_ts": body.first_ts,
                    "last_ts": body.last_ts,
                    "detected_ts": datetime.now(timezone.utc),
                    "evidence": {"thresholds": {}, "facts": body.facts, "event_count": 0, "events": [],
                                 "source": "manual"},
                }])
                if not new_ids:                                        # store_alerts skips duplicates silently
                    raise HTTPException(409, "An alert with the same rule_id, asset_id and first_ts already exists.")
                inc.assign_incidents(cur)                              # same grouping the detector uses
                conn.commit()
                return jsonable_encoder(da.get_alert(cur, new_ids[0]))
    except HTTPException:
        raise
    except DB_ERRORS as exc:
        raise HTTPException(503, str(exc))


class EventCreate(BaseModel):
    asset_id: int
    event_type: str                                      # event_types.name, e.g. "door_forced" (any case)
    ts: datetime
    severity: str = "INFO"
    details: dict[str, Any] | None = None

    @field_validator("severity")
    @classmethod
    def _severity(cls, v):
        v = v.upper()
        if v not in SEVERITIES:
            raise ValueError("severity must be INFO, WARNING or CRITICAL")
        return v

    @field_validator("ts")
    @classmethod
    def _ts(cls, v):
        v = _utc(v)
        if v > datetime.now(timezone.utc) + FUTURE_TOLERANCE:
            raise ValueError("timestamps in the future are rejected")
        return v


@api.post("/events", status_code=201, tags=["Events"])
def create_event(body: EventCreate):
    """Store one event in event_records. The next detection run (automatic every DETECT_INTERVAL s) can raise alerts from it."""
    name = body.event_type.lower()
    try:
        with day2_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("""SELECT a.asset_id, t.event_type_id,
                                      EXISTS (SELECT 1 FROM event_type_system_compat c
                                              WHERE c.event_type_id = t.event_type_id
                                                AND c.system_type_id = a.system_type_id) AS allowed
                               FROM assets a LEFT JOIN event_types t ON t.name = %s
                               WHERE a.asset_id = %s""", (name, body.asset_id))
                row = cur.fetchone()
                if row is None:
                    raise HTTPException(422, f"Unknown asset_id {body.asset_id}.")
                if row["event_type_id"] is None:
                    raise HTTPException(422, f"Unknown event_type '{name}'.")
                if not row["allowed"]:
                    raise HTTPException(422, f"event_type '{name}' is not allowed for this asset's system.")
                cur.execute("""INSERT INTO event_records (asset_id, event_type_id, ts, severity, details)
                               VALUES (%s,%s,%s,%s,%s)
                               ON CONFLICT (asset_id, event_type_id, ts) DO NOTHING
                               RETURNING event_id""",
                            (body.asset_id, row["event_type_id"], body.ts, body.severity.lower(),
                             da.Json(cur, body.details) if body.details is not None else None))
                created = cur.fetchone()
                if created is None:
                    raise HTTPException(409, "This event already exists (same asset, event_type and ts).")
                conn.commit()
                return {"id": created["event_id"], "asset_id": body.asset_id, "event_type": name,
                        "ts": body.ts.isoformat().replace("+00:00", "Z"), "severity": body.severity}
    except HTTPException:
        raise
    except DB_ERRORS as exc:
        raise HTTPException(503, str(exc))
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
DETECT_INTERVAL = int(os.getenv("DETECT_INTERVAL", "60"))        # seconds between automatic detection runs
POLL_INTERVAL = int(os.getenv("ALERT_POLL_INTERVAL", "3"))       # seconds between checks for new alerts
_background = []


def _detect_once():
    with day2_connect() as conn:
        with conn.cursor() as cur:
            pipeline.detect(cur)
            conn.commit()


def _new_alerts(last_id):
    with open_readonly() as conn:
        return frontend_api.alerts_after(conn, last_id)


def _latest_alert_id():
    with open_readonly() as conn:
        return frontend_api.latest_alert_id(conn)


async def _alert_loop():
    """Detect periodically and push every newly stored alert to the connected pages ("new_alert")."""
    last_id, since_detect = None, DETECT_INTERVAL
    while True:
        try:
            if last_id is None:
                last_id = await asyncio.to_thread(_latest_alert_id)
            if since_detect >= DETECT_INTERVAL:
                await asyncio.to_thread(_detect_once)
                since_detect = 0
            for alert in await asyncio.to_thread(_new_alerts, last_id):
                await sio.emit("new_alert", jsonable_encoder(alert))
                last_id = max(last_id, alert["id"])
        except Exception as exc:                  # keep the loop alive while the database is down or starting
            logger.warning("alert loop: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)
        since_detect += POLL_INTERVAL


async def _startup():
    _background.append(asyncio.create_task(_alert_loop()))


async def _shutdown():
    for task in _background:
        task.cancel()


asgi_app = socketio.ASGIApp(sio, other_asgi_app=api, on_startup=_startup, on_shutdown=_shutdown)

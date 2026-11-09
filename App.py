from fastapi import FastAPI, HTTPException, Query
from psycopg import OperationalError

from app.dashboard_core_pg import (
    DashboardError,
    NoDataError,
    get_dashboard_summary,
    get_filter_options,
    open_readonly,
)


asgi_app = FastAPI(title="Anzma Dashboard API")


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
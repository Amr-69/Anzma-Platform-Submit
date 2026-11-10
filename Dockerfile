# syntax=docker/dockerfile:1
FROM python:3.11-slim


ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1


WORKDIR /app


# psycopg2-binary ships its own libpq, so no system packages are needed.
# `requests` is not in requirements.txt but seed_events.py / inject_scenarios
# helpers use it, so it is installed here for `docker compose run` use.
COPY requirements.txt .
RUN pip install -r requirements.txt requests


COPY . .


RUN useradd --create-home appuser && chown -R appuser /app
USER appuser


EXPOSE 8000


# Python-based healthcheck (no curl needed). /health checks service + DB.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status == 200 else 1)"


# App:asgi_app serves the FastAPI application and its plain HTTP routes,
# including /docs, /dashboard, and /health.
CMD ["uvicorn", "App:asgi_app", "--host", "0.0.0.0", "--port", "8000"]
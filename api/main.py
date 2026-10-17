"""Switchboard API. M0: health only; agent routes arrive with their milestones."""

from typing import Any

import psycopg
from fastapi import FastAPI, Response, status

from core.config.settings import get_settings

app = FastAPI(title="Switchboard", version="0.1.0")


def check_db(dsn: str) -> dict[str, Any]:
    """Return DB reachability and whether the pgvector extension is installed."""
    with psycopg.connect(dsn, connect_timeout=3) as conn:
        row = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    return {"db": "ok", "pgvector": row[0] if row else None}


@app.get("/health")
def health(response: Response) -> dict[str, Any]:
    settings = get_settings()
    try:
        result = check_db(settings.database_url)
    except psycopg.Error as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        # Class name only: connection errors can echo the DSN, which holds a password.
        return {"status": "down", "db": "error", "error": type(exc).__name__}
    if result["pgvector"] is None:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "down", **result}
    return {
        "status": "ok",
        **result,
        "integrations": settings.demo_integrations,
        "outbound_mode": settings.outbound_mode,
    }

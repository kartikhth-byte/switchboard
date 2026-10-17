"""Postgres connection + forward-only SQL migrations (core/migrations/NNN_name.sql)."""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row

MIGRATIONS = Path(__file__).parent / "migrations"


def connect(dsn: str) -> psycopg.Connection[dict[str, object]]:
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)


def migrate(dsn: str) -> list[str]:
    """Apply pending migrations in filename order. Returns names applied."""
    applied: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            " (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        # Serialise concurrent migrators (api + worker starting together).
        conn.execute("SELECT pg_advisory_lock(7341001)")
        try:
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text())  # trusted repo file
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(7341001)")
    return applied

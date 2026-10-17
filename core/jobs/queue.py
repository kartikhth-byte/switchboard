"""Postgres jobs table: enqueue, claim, complete, retry, cancel-by-key, failure queue (SHR-007)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from core.db import connect

JobStatus = Literal["pending", "running", "done", "failed", "cancelled"]
LEASE = timedelta(minutes=5)  # a 'running' job older than this is assumed orphaned by a crash


class Job(BaseModel):
    id: UUID
    kind: str
    key: str | None
    run_at: datetime
    payload: dict[str, Any]
    status: JobStatus
    attempts: int
    max_attempts: int
    last_error: str | None
    run_id: UUID | None


def _now() -> datetime:
    return datetime.now(UTC)


class JobQueue:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def enqueue(
        self,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        key: str | None = None,
        run_at: datetime | None = None,
        delay: timedelta | None = None,
        max_attempts: int = 5,
        run_id: UUID | None = None,
    ) -> Job:
        """Idempotent when `key` is set: a live job with that key is returned, not duplicated."""
        when = run_at or (_now() + delay if delay else _now())
        with connect(self.dsn) as c:
            row = c.execute(
                "INSERT INTO jobs (kind, key, run_at, payload, max_attempts, run_id)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (key) WHERE key IS NOT NULL AND status IN ('pending', 'running')"
                " DO NOTHING RETURNING *",
                (kind, key, when, json.dumps(payload or {}), max_attempts, run_id),
            ).fetchone()
            if row is None:
                row = c.execute(
                    "SELECT * FROM jobs WHERE key = %s AND status IN ('pending', 'running')",
                    (key,),
                ).fetchone()
        return Job.model_validate(row)

    def claim(self, *, limit: int = 10, now: datetime | None = None) -> list[Job]:
        """Atomically take due jobs (and orphaned running ones). Safe with several workers."""
        t = now or _now()
        with connect(self.dsn) as c:
            rows = c.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1,"
                " locked_at = %(t)s, updated_at = %(t)s"
                " WHERE id IN ("
                "   SELECT id FROM jobs"
                "   WHERE (status = 'pending' AND run_at <= %(t)s)"
                "      OR (status = 'running' AND locked_at < %(stale)s)"
                "   ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT %(n)s)"
                " RETURNING *",
                {"t": t, "stale": t - LEASE, "n": limit},
            ).fetchall()
        return [Job.model_validate(r) for r in rows]

    def complete(self, job_id: UUID) -> None:
        self._set(job_id, "done", None)

    def fail(self, job_id: UUID, error: str) -> None:
        """Move to the failure queue (status 'failed')."""
        self._set(job_id, "failed", error)

    def retry_at(self, job_id: UUID, when: datetime, error: str) -> None:
        with connect(self.dsn) as c:
            c.execute(
                "UPDATE jobs SET status = 'pending', run_at = %s, last_error = %s,"
                " locked_at = NULL, updated_at = now() WHERE id = %s AND status = 'running'",
                (when, error[:500], job_id),
            )

    def cancel(self, key: str) -> int:
        """Cancel the pending job with this exact key (LEAD-013/020). Returns rows cancelled."""
        return self._cancel("key = %s", key)

    def cancel_prefix(self, prefix: str) -> int:
        """Cancel all pending jobs whose key starts with `prefix`, e.g. 'lead:42:'."""
        escaped = prefix.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        return self._cancel("key LIKE %s", escaped + "%")

    def failed(self, limit: int = 50) -> list[Job]:
        with connect(self.dsn) as c:
            rows = c.execute(
                "SELECT * FROM jobs WHERE status = 'failed' ORDER BY updated_at DESC LIMIT %s",
                (limit,),
            ).fetchall()
        return [Job.model_validate(r) for r in rows]

    def get(self, job_id: UUID) -> Job | None:
        with connect(self.dsn) as c:
            row = c.execute("SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
        return Job.model_validate(row) if row else None

    def _set(self, job_id: UUID, status: JobStatus, error: str | None) -> None:
        with connect(self.dsn) as c:
            c.execute(
                "UPDATE jobs SET status = %s, last_error = %s, locked_at = NULL,"
                " updated_at = now() WHERE id = %s",
                (status, error[:500] if error else None, job_id),
            )

    def _cancel(self, where: str, value: str) -> int:
        with connect(self.dsn) as c:
            cur = c.execute(
                "UPDATE jobs SET status = 'cancelled', updated_at = now()"  # noqa: S608 — fixed clauses
                f" WHERE status = 'pending' AND {where}",
                (value,),
            )
            return cur.rowcount

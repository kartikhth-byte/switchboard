"""Run persistence (SHR-004). Postgres is the source of truth for run state."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from core.db import connect
from core.runs.models import AgentRun, RunStatus

_FINAL: tuple[RunStatus, ...] = ("done", "failed", "budget_exceeded", "waiting_approval")


class RunStore(Protocol):
    def create(self, run_id: UUID, agent: str, trace_id: str) -> AgentRun: ...
    def add_usage(self, run_id: UUID, tokens_in: int, tokens_out: int, cost_usd: float) -> None: ...
    def finish(self, run_id: UUID, status: RunStatus, error: str | None = None) -> None: ...
    def get(self, run_id: UUID) -> AgentRun | None: ...
    def spend_today(self) -> float: ...


class PgRunStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def create(self, run_id: UUID, agent: str, trace_id: str) -> AgentRun:
        with connect(self.dsn) as c:
            row = c.execute(
                "INSERT INTO runs (id, agent, trace_id) VALUES (%s, %s, %s) RETURNING *",
                (run_id, agent, trace_id),
            ).fetchone()
        return AgentRun.model_validate(row)

    def add_usage(self, run_id: UUID, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        # Atomic increments: safe if two model calls in one run finish concurrently.
        with connect(self.dsn) as c:
            c.execute(
                "UPDATE runs SET tokens_in = tokens_in + %s, tokens_out = tokens_out + %s,"
                " cost_usd = cost_usd + %s WHERE id = %s",
                (tokens_in, tokens_out, cost_usd, run_id),
            )

    def finish(self, run_id: UUID, status: RunStatus, error: str | None = None) -> None:
        assert status in _FINAL, status
        with connect(self.dsn) as c:
            c.execute(
                "UPDATE runs SET status = %s, error = %s, ended_at = now() WHERE id = %s",
                (status, error, run_id),
            )

    def get(self, run_id: UUID) -> AgentRun | None:
        with connect(self.dsn) as c:
            row = c.execute("SELECT * FROM runs WHERE id = %s", (run_id,)).fetchone()
        return AgentRun.model_validate(row) if row else None

    def spend_today(self) -> float:
        """Total model spend across all runs since 00:00 UTC (daily cap, SHR-018)."""
        with connect(self.dsn) as c:
            row = c.execute(
                "SELECT COALESCE(SUM(cost_usd), 0)::float8 AS total FROM runs"
                " WHERE started_at >="
                " date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
            ).fetchone()
        total = row["total"] if row else 0.0
        return float(total) if isinstance(total, int | float) else 0.0

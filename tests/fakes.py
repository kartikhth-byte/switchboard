"""Test doubles shared across test modules."""

from datetime import UTC, datetime
from uuid import UUID

from core.runs import AgentRun, RunStatus


class FakeStore:
    def __init__(self, spent_today: float = 0.0) -> None:
        self.runs: dict[UUID, AgentRun] = {}
        self.spent_today = spent_today  # spend from "other" runs earlier today

    def create(self, run_id: UUID, agent: str, trace_id: str) -> AgentRun:
        run = AgentRun(
            id=run_id,
            agent=agent,
            status="running",
            started_at=datetime.now(UTC),
            trace_id=trace_id,
        )
        self.runs[run_id] = run
        return run

    def add_usage(self, run_id: UUID, tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        r = self.runs[run_id]
        r.tokens_in += tokens_in
        r.tokens_out += tokens_out
        r.cost_usd += cost_usd

    def finish(self, run_id: UUID, status: RunStatus, error: str | None = None) -> None:
        r = self.runs[run_id]
        r.status, r.error, r.ended_at = status, error, datetime.now(UTC)

    def get(self, run_id: UUID) -> AgentRun | None:
        return self.runs.get(run_id)

    def spend_today(self) -> float:
        return self.spent_today + sum(r.cost_usd for r in self.runs.values())

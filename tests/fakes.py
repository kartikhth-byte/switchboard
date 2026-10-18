"""Test doubles shared across test modules."""

from datetime import UTC, datetime
from uuid import UUID

from core.runs import AgentRun, RunStatus


class FakeStore:
    def __init__(self, spent_today: float = 0.0) -> None:
        self.runs: dict[UUID, AgentRun] = {}
        self.spent_today = spent_today  # spend from "other" runs earlier today

    def create(self, run_id: UUID, agent: str, trace_id: str, kind: str = "live") -> AgentRun:
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


class FakeApprovals:
    """In-memory stand-in for core.approvals.ApprovalStore."""

    def __init__(self) -> None:
        from core.approvals import ApprovalRequest

        self._model = ApprovalRequest
        self.items: dict[UUID, ApprovalRequest] = {}  # type: ignore[name-defined]  # noqa: F821

    def create(self, run_id: UUID, agent: str, action: str, payload: dict, ttl: object = None):  # type: ignore[no-untyped-def]
        from datetime import timedelta
        from uuid import uuid4

        req = self._model(
            id=uuid4(), run_id=run_id, agent=agent, action=action, payload=payload,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )  # fmt: skip
        self.items[req.id] = req
        return req

    def get(self, approval_id: UUID):  # type: ignore[no-untyped-def]
        return self.items.get(approval_id)

    def decide(self, approval_id: UUID, decision: str, decided_by: str):  # type: ignore[no-untyped-def]
        self.items[approval_id] = self.items[approval_id].model_copy(
            update={"decision": decision, "decided_by": decided_by}
        )
        return self.items[approval_id]

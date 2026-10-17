"""Approval requests (SHR-008).

Flow for the hand-built ReAct loop (Concierge):
  1. A tool returns `approval_required`; `RunHandle.request_approval` saves an
     ApprovalRequest and ends the run as `waiting_approval`.
  2. A human approves or rejects via the API (`decide`).
  3. The next turn calls `resume_approved` in a new run: the stored call executes once
     (idempotency key = approval id), or a rejection is returned as a tool result.
The Dispatcher's LangGraph interrupt (M3) uses the same table.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel

from core.db import connect

Decision = Literal["pending", "approved", "rejected", "expired"]
DEFAULT_TTL = timedelta(hours=24)


class ApprovalRequest(BaseModel):
    id: UUID
    run_id: UUID
    agent: str
    action: str
    payload: dict[str, Any]
    expires_at: datetime
    decision: Decision = "pending"
    decided_by: str | None = None
    decided_at: datetime | None = None


class ApprovalError(Exception):
    """Decision refused: unknown id, already decided, or expired."""


class ApprovalStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def create(
        self,
        run_id: UUID,
        agent: str,
        action: str,
        payload: dict[str, Any],
        ttl: timedelta = DEFAULT_TTL,
    ) -> ApprovalRequest:
        with connect(self.dsn) as c:
            row = c.execute(
                "INSERT INTO approvals (id, run_id, agent, action, payload, expires_at)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                (uuid4(), run_id, agent, action, json.dumps(payload), datetime.now(UTC) + ttl),
            ).fetchone()
        return ApprovalRequest.model_validate(row)

    def get(self, approval_id: UUID) -> ApprovalRequest | None:
        self._expire()
        with connect(self.dsn) as c:
            row = c.execute("SELECT * FROM approvals WHERE id = %s", (approval_id,)).fetchone()
        return ApprovalRequest.model_validate(row) if row else None

    def pending(self, agent: str | None = None) -> list[ApprovalRequest]:
        self._expire()
        with connect(self.dsn) as c:
            rows = c.execute(
                "SELECT * FROM approvals WHERE decision = 'pending'"
                " AND (%(agent)s::text IS NULL OR agent = %(agent)s) ORDER BY created_at",
                {"agent": agent},
            ).fetchall()
        return [ApprovalRequest.model_validate(r) for r in rows]

    def decide(
        self, approval_id: UUID, decision: Literal["approved", "rejected"], decided_by: str
    ) -> ApprovalRequest:
        """Single-use: only a pending, unexpired request can be decided (no double approval)."""
        self._expire()
        with connect(self.dsn) as c:
            row = c.execute(
                "UPDATE approvals SET decision = %s, decided_by = %s, decided_at = now()"
                " WHERE id = %s AND decision = 'pending' RETURNING *",
                (decision, decided_by, approval_id),
            ).fetchone()
        if row is None:
            current = self.get(approval_id)
            state = current.decision if current else "not_found"
            raise ApprovalError(f"cannot decide approval {approval_id}: {state}")
        return ApprovalRequest.model_validate(row)

    def _expire(self) -> None:
        # Optional expiry (cut-list item): lazy, no background job needed.
        with connect(self.dsn) as c:
            c.execute(
                "UPDATE approvals SET decision = 'expired', decided_at = now()"
                " WHERE decision = 'pending' AND expires_at <= now()"
            )

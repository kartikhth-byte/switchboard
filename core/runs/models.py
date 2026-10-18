from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

RunStatus = Literal["running", "waiting_approval", "done", "failed", "budget_exceeded"]
RunKind = Literal["live", "eval"]


class AgentRun(BaseModel):
    id: UUID
    agent: str
    status: RunStatus
    started_at: datetime
    ended_at: datetime | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    trace_id: str  # Langfuse (SHR-006); derived from id, so either finds the other
    error: str | None = None

"""Switchboard API: health, approvals queue, and the agents' demo endpoints."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from agents.concierge.service import ChatService, UnknownConversation
from core.approvals import ApprovalError, ApprovalRequest, ApprovalStore
from core.config.settings import get_settings
from core.db import migrate
from core.guardrails import RateLimiter
from core.logs import setup_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_logging()
    try:
        applied = migrate(get_settings().database_url)
        log.info("migrations applied: %s", applied or "none pending")
    except psycopg.Error as exc:
        # Keep serving so /health can report the DB problem instead of a crash loop.
        log.error("migrations failed: %s", type(exc).__name__)
    yield


app = FastAPI(title="Switchboard", version="0.1.0", lifespan=lifespan)
_limiter = RateLimiter(get_settings().rate_limit_per_min)


def public_rate_limit(request: Request) -> None:
    """Per-IP limit for public endpoints (SHR-018). Attach with Depends on agent routes."""
    ip = request.client.host if request.client else "unknown"
    wait = _limiter.check(ip)
    if wait is not None:
        raise HTTPException(
            status_code=429,
            detail="Too many requests from your address. Please wait a moment and try again.",
            headers={"Retry-After": str(max(1, round(wait)))},
        )


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


# ---- Concierge demo chat (SUPP-001..003, SUPP-009) ----


class ChatIn(BaseModel):
    conversation_id: str | None = None  # omit to start a conversation; the reply carries one
    message: str = Field(min_length=1, max_length=1000)


_chat: ChatService | None = None


def _chat_service() -> ChatService:
    global _chat
    if _chat is None:
        _chat = ChatService(get_settings())
    return _chat


@app.post("/concierge/chat", dependencies=[Depends(public_rate_limit)])
def concierge_chat(body: ChatIn) -> dict[str, Any]:
    try:
        reply = _chat_service().chat(body.conversation_id, body.message.strip())
    except UnknownConversation:
        raise HTTPException(404, "Conversation not found; start a new one.") from None
    return reply.__dict__


# ---- Approvals (SHR-008): the queue behind the demo's approve/reject buttons ----


class DecisionIn(BaseModel):
    decision: Literal["approved", "rejected"]
    decided_by: str = "demo-operator"  # no auth in Wave 1 (out of scope)


def _approvals() -> ApprovalStore:
    return ApprovalStore(get_settings().database_url)


@app.get("/approvals")
def list_approvals(agent: str | None = None) -> list[ApprovalRequest]:
    return _approvals().pending(agent)


@app.post("/approvals/{approval_id}/decision", dependencies=[Depends(public_rate_limit)])
def decide_approval(approval_id: UUID, body: DecisionIn) -> ApprovalRequest:
    try:
        return _approvals().decide(approval_id, body.decision, body.decided_by)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

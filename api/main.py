"""Switchboard API: health, approvals queue, and the agents' demo endpoints."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from agents.concierge.service import ChatService, UnknownConversation
from agents.dispatcher.service import Dispatcher, make_adapters
from agents.dispatcher.states import InvalidTransition
from core.approvals import ApprovalError, ApprovalRequest, ApprovalStore
from core.config.settings import get_settings
from core.db import migrate
from core.guardrails import RateLimiter
from core.jobs import JobQueue
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
        req = _approvals().decide(approval_id, body.decision, body.decided_by)
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    lead_id = req.payload.get("lead_id")
    if req.agent == "dispatcher" and lead_id:  # the paused lead graph continues in the worker
        JobQueue(get_settings().database_url).enqueue(
            "dispatcher.resume",
            {"lead_id": str(lead_id), "decision": req.decision},
            key=f"lead:{lead_id}:resume:{req.id}",
            run_id=req.run_id,
        )
    return req


# ---- Dispatcher (LEAD-001, 005, 013, 016): intake from n8n / the demo form, lead events ----


class LeadIn(BaseModel):
    channel: Literal["web_form", "email", "whatsapp", "instagram", "phone"]
    name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=40)
    message: str = Field(min_length=1, max_length=4000)
    received_at: str | None = None
    source: str | None = Field(default=None, max_length=80)
    campaign: str | None = Field(default=None, max_length=80)
    utm: dict[str, str] = Field(default_factory=dict)


class LeadEventIn(BaseModel):
    kind: Literal["lead_replied", "human_takeover", "opt_out"]
    text: str | None = Field(default=None, max_length=4000)


_dispatcher: Dispatcher | None = None


def _lead_service() -> Dispatcher:
    global _dispatcher
    if _dispatcher is None:
        s = get_settings()
        _dispatcher = Dispatcher(s, make_adapters(s))
    return _dispatcher


@app.post("/agents/lead/intake", dependencies=[Depends(public_rate_limit)])
def lead_intake(body: LeadIn, response: Response) -> dict[str, Any]:
    """Idempotent: a replayed delivery returns the existing lead with 200, a new one 202."""
    if not (body.email or body.phone):
        raise HTTPException(422, "email or phone required")
    payload = body.model_dump()
    payload["received_at"] = payload["received_at"] or datetime.now(UTC).isoformat()
    lead, created = _lead_service().intake(payload)
    response.status_code = status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK
    return {"lead_id": str(lead.id), "created": created, "stage": lead.stage}


@app.post("/agents/lead/{lead_id}/events", dependencies=[Depends(public_rate_limit)])
def lead_event(lead_id: UUID, body: LeadEventIn) -> dict[str, Any]:
    d = _lead_service()
    if d.store.get(lead_id) is None:
        raise HTTPException(404, "lead not found")
    try:
        lead = d.event(str(lead_id), body.kind, body.text)
    except InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"lead_id": str(lead.id), "stage": lead.stage}


@app.get("/agents/lead/{lead_id}")
def lead_detail(lead_id: UUID) -> dict[str, Any]:
    d = _lead_service()
    lead = d.store.get(lead_id)
    if lead is None:
        raise HTTPException(404, "lead not found")
    return {
        "lead": lead.model_dump(mode="json", exclude={"payload"}),
        "events": d.store.events(lead_id),
        "messages": d.store.messages(lead_id),
    }

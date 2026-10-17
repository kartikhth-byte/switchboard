"""Concierge tools (SUPP-002, SUPP-003, SUPP-010, SUPP-014).

Authorization lives here, in code — never in the prompt:
- Write tools act only on the identity verified by `lookup_booking` in this conversation,
  and take no email argument, so injected text can't point them at another contact.
- Conditional approval rules come from config (e.g. refund requests -> human approval).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from core.retrieval import Hit
from core.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec
from integrations.crm import CRM
from integrations.slack import Slack

AGENT = "concierge"
IST = timezone(timedelta(hours=5, minutes=30))


def today_ist() -> date:
    """The business runs on India time; date arithmetic is done in code, not by the model."""
    return datetime.now(IST).date()


class KBSearch(Protocol):
    def search(self, query: str, k: int = 5) -> list[Hit]: ...


@dataclass
class ConversationState:
    """Per-conversation state. Persisted with memory (SHR-013) in the next step."""

    conversation_id: str
    transcript: list[dict[str, str]] = field(default_factory=list)
    verified: dict[str, str] | None = None  # {"booking_ref", "email"} after lookup_booking
    failed_lookups: int = 0
    pending_approval_id: str | None = None
    handoff: dict[str, Any] | None = None
    retrieved: set[str] = field(default_factory=set)  # chunk ids seen this turn


@dataclass
class ToolDeps:
    kb: KBSearch
    crm: CRM
    slack: Slack
    bookings: dict[str, dict[str, Any]]  # booking_ref -> booking
    state: ConversationState
    rules: dict[str, Any] = field(default_factory=dict)
    handoff_channel: str = "#guest-support"
    today: Callable[[], date] = today_ist  # injectable for tests


def load_bookings(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {b["booking_ref"].upper(): b for b in data["bookings"]}


# ---- input schemas ----


class SearchKBIn(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    k: int = Field(default=5, ge=1, le=8)


class LookupBookingIn(BaseModel):
    booking_ref: str = Field(pattern=r"^[A-Za-z]{2}-\d{4}$", description="e.g. PG-1042")
    email: str = Field(min_length=3, max_length=200)


class CreateTicketIn(BaseModel):
    subject: str = Field(min_length=3, max_length=120)
    body: str = Field(min_length=3, max_length=2000)
    category: Literal["maintenance", "billing", "refund_request", "complaint", "other"]
    priority: Literal["low", "normal", "high"] = "normal"


class UpdateContactIn(BaseModel):
    phone: str | None = Field(default=None, max_length=30)
    preferred_name: str | None = Field(default=None, max_length=60)
    notes: str | None = Field(default=None, max_length=500)


class HandoffIn(BaseModel):
    reason: Literal[
        "requested", "frustrated", "missing_answer", "policy_exception", "safety", "other"
    ]
    summary: str = Field(min_length=3, max_length=1000)


class FinalAnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=3000)
    citations: list[str] = Field(default_factory=list, description="chunk_ids from search_kb")
    confidence: float = Field(ge=0, le=1)
    escalate: bool = False


# ---- registry ----


def build_registry(d: ToolDeps) -> ToolRegistry:
    reg = ToolRegistry()
    st = d.state
    max_lookups = int(d.rules.get("max_identity_attempts", 3))
    approval_rules: dict[str, dict[str, list[str]]] = d.rules.get("approval_required", {})

    def needs_approval(tool: str, args: BaseModel, ctx: ToolContext) -> ToolResult | None:
        """Config rule, e.g. {create_ticket: {category: [refund_request]}}."""
        if ctx.approved:
            return None
        for fld, values in approval_rules.get(tool, {}).items():
            if getattr(args, fld, None) in values:
                return ToolResult(
                    ok=False,
                    error="approval_required",
                    payload={"tool": tool, "arguments": args.model_dump(mode="json")},
                )
        return None

    def require_identity() -> ToolResult | None:
        if st.verified is None:
            return ToolResult(
                ok=False,
                error="identity_not_verified: call lookup_booking with the guest's booking "
                "reference and email first",
            )
        return None

    def search_kb(inp: SearchKBIn, ctx: ToolContext) -> dict[str, Any]:
        hits = d.kb.search(inp.query, inp.k)
        st.retrieved.update(h.chunk_id for h in hits)
        return {"results": [h.model_dump() for h in hits]}

    def lookup_booking(inp: LookupBookingIn, ctx: ToolContext) -> ToolResult:
        if st.failed_lookups >= max_lookups:
            return ToolResult(ok=False, error="requires_human: too many failed booking lookups")
        b = d.bookings.get(inp.booking_ref.upper())
        if b is None or b["email"].lower() != inp.email.strip().lower():
            st.failed_lookups += 1
            # Same message for unknown ref and wrong email: don't help enumeration.
            return ToolResult(
                ok=False, error="not_found: no booking matches that reference and email"
            )
        st.verified = {"booking_ref": b["booking_ref"], "email": b["email"]}
        public = {
            k: b[k]
            for k in (
                "booking_ref",
                "guest_name",
                "villa",
                "check_in",
                "check_out",
                "guests",
                "channel",
                "payment_status",
                "balance_due_inr",
            )
        }
        # Computed here so the model never does date arithmetic (refund tiers depend on it).
        public["today"] = d.today().isoformat()
        public["days_until_check_in"] = (date.fromisoformat(b["check_in"]) - d.today()).days
        return ToolResult(ok=True, payload={"booking": public})

    def create_ticket(inp: CreateTicketIn, ctx: ToolContext) -> ToolResult:
        if (denied := require_identity()) is not None:
            return denied
        if (pending := needs_approval("create_ticket", inp, ctx)) is not None:
            return pending
        assert st.verified is not None
        t = d.crm.create_ticket(
            st.verified["email"], inp.subject, inp.body, inp.category, inp.priority
        )
        return ToolResult(ok=True, payload={"ticket_id": t["ticket_id"], "status": t["status"]})

    def update_crm_contact(inp: UpdateContactIn, ctx: ToolContext) -> ToolResult:
        if (denied := require_identity()) is not None:
            return denied
        fields = inp.model_dump(exclude_none=True)
        if not fields:
            return ToolResult(ok=False, error="invalid_arguments: nothing to update")
        assert st.verified is not None
        d.crm.upsert_contact(st.verified["email"], fields)
        return ToolResult(ok=True, payload={"updated": sorted(fields)})

    def handoff_to_human(inp: HandoffIn, ctx: ToolContext) -> ToolResult:
        payload = {
            "conversation_id": st.conversation_id,
            "run_id": str(ctx.run_id),
            "reason": inp.reason,
            "summary": inp.summary,
            "verified_booking": st.verified["booking_ref"] if st.verified else None,
            "transcript": list(st.transcript),  # full chat (SUPP-010)
        }
        ts = d.slack.post(d.handoff_channel, f"Handoff ({inp.reason}): {inp.summary}", payload)
        st.handoff = {**payload, "slack_ts": ts}
        return ToolResult(ok=True, payload={"handed_off": True, "reason": inp.reason})

    def final_answer(inp: FinalAnswerIn, ctx: ToolContext) -> dict[str, Any]:
        return inp.model_dump()

    specs: list[tuple[ToolSpec, Any]] = [
        (
            ToolSpec(
                name="search_kb",
                description="Search the knowledge base. Returns chunks with chunk_id to cite.",
                input_model=SearchKBIn,
                permission="read",
                side_effect=False,
            ),
            search_kb,
        ),
        (
            ToolSpec(
                name="lookup_booking",
                description="Look up and verify a booking. Needs the "
                "booking reference and the email used to book.",
                input_model=LookupBookingIn,
                permission="read",
                side_effect=False,
            ),
            lookup_booking,
        ),
        (
            ToolSpec(
                name="create_ticket",
                description="Create a support ticket for the verified "
                "guest (maintenance, billing, refund request, complaint).",
                input_model=CreateTicketIn,
                permission="write",
                side_effect=True,
            ),
            create_ticket,
        ),
        (
            ToolSpec(
                name="update_crm_contact",
                description="Update the verified guest's contact details.",
                input_model=UpdateContactIn,
                permission="write",
                side_effect=True,
            ),
            update_crm_contact,
        ),
        (
            ToolSpec(
                name="handoff_to_human",
                description="Pass the conversation to a person, with a short summary.",
                input_model=HandoffIn,
                permission="write",
                side_effect=True,
            ),
            handoff_to_human,
        ),
        (
            ToolSpec(
                name="final_answer",
                description="Give the final reply to the guest. Cite every chunk_id you relied on.",
                input_model=FinalAnswerIn,
                permission="read",
                side_effect=False,
            ),
            final_answer,
        ),
    ]
    for spec, handler in specs:
        reg.register(spec, handler, agents=[AGENT])
    return reg

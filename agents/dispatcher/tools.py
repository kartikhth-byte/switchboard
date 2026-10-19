"""Dispatcher tools: outbound messages and Slack routing (LEAD-003, LEAD-004, LEAD-017, LEAD-020).

Whether a message may go is decided here, in code:
- OUTBOUND_MODE=off blocks every send (the registry), approve_first needs an approval (registry);
- the recipient allowlist applies in allowlist *and* approve_first modes: a human approving a
  draft can't widen who the system may contact;
- an opted-out, taken-over or replied lead gets nothing (LEAD-013, LEAD-020);
- WhatsApp free text only inside the 24h session window; outside it, the approved template;
- one row in `outbox` per attempt, with an idempotency key so a retried job never sends twice.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from agents.dispatcher.store import Lead, LeadStore
from core.tools import RetryableToolError, ToolContext, ToolRegistry, ToolResult, ToolSpec
from integrations.messaging import Email, WhatsApp
from integrations.slack import Slack

AGENT = "dispatcher"


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ToolDeps:
    store: LeadStore
    whatsapp: WhatsApp
    email: Email
    slack: Slack
    allowlist: set[str] = field(default_factory=set)  # phones (E.164), emails, or "@domain"
    window_hours: int = 24
    template: str = "palmgrove_enquiry_ack"
    now: Callable[[], datetime] = _now


class SendWhatsAppIn(BaseModel):
    lead_id: str
    body: str = Field(min_length=1, max_length=1000)


class SendEmailIn(BaseModel):
    lead_id: str
    subject: str = Field(min_length=1, max_length=150)
    body: str = Field(min_length=1, max_length=4000)


class PostSlackIn(BaseModel):
    channel: str = Field(pattern=r"^#[\w-]+$")
    text: str = Field(min_length=1, max_length=3000)
    lead_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def allowed(recipient: str, allowlist: set[str]) -> bool:
    """Exact address/number, a whole email domain ("@example.org") or a number prefix
    ending in "*" ("+9198765*", for a block of test numbers)."""
    r = recipient.lower()
    if r in allowlist or ("@" in r and "@" + r.split("@", 1)[1] in allowlist):
        return True
    return any(a.endswith("*") and r.startswith(a[:-1]) for a in allowlist)


def _key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def build_registry(d: ToolDeps) -> ToolRegistry:
    reg = ToolRegistry()

    def gate(
        lead: Lead | None, recipient: str | None, ctx: ToolContext, channel: str, body: str
    ) -> ToolResult | None:
        if lead is None:
            return ToolResult(ok=False, error="lead_not_found")
        if not recipient:
            return ToolResult(ok=False, error=f"no_recipient: lead has no {channel} contact")
        if lead.stopped:
            d.store.record_message(lead.id, channel, recipient, body, "blocked_stopped")
            return ToolResult(ok=False, error=f"blocked_stopped: lead is {lead.stage}")
        if ctx.outbound_mode in ("allowlist", "approve_first") and not allowed(
            recipient, d.allowlist
        ):
            d.store.record_message(lead.id, channel, recipient, body, "blocked_not_allowlisted")
            return ToolResult(ok=False, error="blocked_not_allowlisted")
        return None

    def send_whatsapp(inp: SendWhatsAppIn, ctx: ToolContext) -> ToolResult:
        lead = d.store.get(inp.lead_id)
        to = (lead.contact or {}).get("phone") if lead else None
        if (blocked := gate(lead, to, ctx, "whatsapp", inp.body)) is not None:
            return blocked
        assert lead is not None and to is not None
        last_in = lead.last_inbound_at or (
            lead.received_at if lead.payload.get("channel") == "whatsapp" else None
        )
        in_window = last_in is not None and d.now() - last_in <= timedelta(hours=d.window_hours)
        key = ctx.idempotency_key or _key(str(lead.id), "whatsapp", inp.body)
        first = (lead.payload.get("name") or "there").split()[0]
        template = None if in_window else d.template
        body = inp.body if in_window else f"[template {d.template}: {first}]"
        if not d.store.record_message(
            lead.id, "whatsapp", to, body, "sending", template=template, idem_key=key
        ):
            return ToolResult(ok=True, payload={"duplicate": True})
        try:
            pid = (
                d.whatsapp.send_text(to, inp.body)
                if in_window
                else d.whatsapp.send_template(to, d.template, [first])
            )
        except Exception as exc:
            d.store.mark_message(key, "failed")
            raise RetryableToolError(type(exc).__name__) from exc
        d.store.mark_message(key, "sent", pid)
        return ToolResult(ok=True, payload={"sent": True, "template": template, "id": pid})

    def send_email(inp: SendEmailIn, ctx: ToolContext) -> ToolResult:
        lead = d.store.get(inp.lead_id)
        to = (lead.contact or {}).get("email") if lead else None
        if (blocked := gate(lead, to, ctx, "email", inp.body)) is not None:
            return blocked
        assert lead is not None and to is not None
        key = ctx.idempotency_key or _key(str(lead.id), "email", inp.subject, inp.body)
        if not d.store.record_message(lead.id, "email", to, inp.body, "sending", idem_key=key):
            return ToolResult(ok=True, payload={"duplicate": True})
        try:
            pid = d.email.send(to, inp.subject, inp.body)
        except Exception as exc:
            d.store.mark_message(key, "failed")
            raise RetryableToolError(type(exc).__name__) from exc
        d.store.mark_message(key, "sent", pid)
        return ToolResult(ok=True, payload={"sent": True, "id": pid})

    def post_slack(inp: PostSlackIn, ctx: ToolContext) -> ToolResult:
        ts = d.slack.post(inp.channel, inp.text, inp.payload)
        d.store.record_message(inp.lead_id, "slack", inp.channel, inp.text, "sent", provider_id=ts)
        return ToolResult(ok=True, payload={"ts": ts})

    for spec, handler in [
        (
            ToolSpec(
                name="send_whatsapp",
                description="WhatsApp reply to the lead.",
                input_model=SendWhatsAppIn,
                permission="external_send",
                side_effect=True,
            ),
            send_whatsapp,
        ),
        (
            ToolSpec(
                name="send_email",
                description="Email reply to the lead.",
                input_model=SendEmailIn,
                permission="external_send",
                side_effect=True,
            ),
            send_email,
        ),
        (
            ToolSpec(
                name="post_slack",
                description="Post to the team's Slack (internal).",
                input_model=PostSlackIn,
                permission="write",
                side_effect=True,
            ),
            post_slack,
        ),
    ]:
        reg.register(spec, handler, agents=[AGENT])  # type: ignore[arg-type]
    return reg

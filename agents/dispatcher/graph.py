"""Dispatcher flow (LangGraph): CRM upsert → assess → draft → outbound gate → send → route.

Intake (normalize + dedupe) happens before the graph, at the API, so a replayed webhook never
starts a second run (LEAD-005). Each invocation runs inside one `start_run`, so one lead is one
trace (LEAD-006). In approve_first mode the graph pauses at `await_approval` (LangGraph interrupt,
state in the Postgres checkpointer) and resumes on the manager's decision (SHR-008, LEAD-017).

    upsert_crm → assess ─(spam/existing guest)→ END
                    └→ draft → gate ─off/not allowed→ hold ─┐
                                   ├allowlist→ send ────────┤→ route ─hot→ Slack owner
                                   └approve_first→ request_approval → await_approval
                                          (interrupt) → send_approved / reject → route
                                                          route ─warm/cool→ schedule follow-ups
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agents.dispatcher.assessment import LeadAssessment, assess, draft_reply, reply_channel
from agents.dispatcher.store import LeadStore
from agents.dispatcher.tools import AGENT
from core.approvals import ApprovalStore
from core.config.agent import AgentConfig
from core.jobs import JobQueue
from core.models import ModelClient
from core.runs import RunHandle
from core.tools import ToolRegistry
from integrations.messaging import LeadCRM


class LeadState(TypedDict, total=False):
    lead_id: str
    crm_id: str
    assessment: dict[str, Any]
    draft: dict[str, Any]
    approval_id: str
    decision: str
    outcome: str  # sent | held | blocked | rejected | no_reply


@dataclass
class GraphDeps:
    cfg: AgentConfig
    client: ModelClient
    store: LeadStore
    crm: LeadCRM
    registry: ToolRegistry
    approvals: ApprovalStore | None
    jobs: JobQueue | None
    today: date
    outbound_mode: Literal["off", "allowlist", "approve_first"] = "off"
    run: RunHandle | None = None  # set per invocation
    events: list[str] = field(default_factory=list)  # node timeline for the demo page


def _parse_delay(s: str) -> timedelta:
    n, unit = int(s[:-1]), s[-1]
    return (
        timedelta(hours=n)
        if unit == "h"
        else timedelta(minutes=n)
        if unit == "m"
        else (timedelta(days=n))
    )


def routing_owner(rules: list[dict[str, Any]], a: LeadAssessment) -> dict[str, str] | None:
    """First matching rule wins (LEAD-004). `when` keys must all match; lists mean 'one of'."""
    for rule in rules:
        when = rule.get("when", {})
        ok = True
        for k, v in when.items():
            val = getattr(a, k, None)
            ok &= val in v if isinstance(v, list) else val == v
        if ok:
            return {"owner": rule["owner"], "channel": rule.get("channel", "#leads")}
    return None


def build(d: GraphDeps) -> StateGraph:  # type: ignore[type-arg]
    def run() -> RunHandle:
        assert d.run is not None, "GraphDeps.run must be set for each invocation"
        return d.run

    def lead(state: LeadState):  # type: ignore[no-untyped-def]
        got = d.store.get(state["lead_id"])
        assert got is not None
        return got

    def upsert_crm(state: LeadState) -> LeadState:
        d.events.append("upsert_crm")
        ld = lead(state)
        c, p = ld.contact or {}, ld.payload
        fields = {
            "name": p.get("name"),
            "source": p.get("source"),
            "campaign": p.get("campaign"),
            **(p.get("utm") or {}),
            "received_at": p.get("received_at"),
            "channel": p.get("channel"),
            "lead_id": str(ld.id),
        }
        crm_id = d.crm.upsert_lead(c.get("email"), c.get("phone"), fields)
        d.store.advance(ld.id, "crm_upserted", crm_id=crm_id, run_id=run().run_id)
        return {"crm_id": crm_id}

    def do_assess(state: LeadState) -> LeadState:
        d.events.append("assess")
        ld = lead(state)
        a = assess(run(), d.client, d.cfg, ld.payload, d.today)
        d.store.advance(ld.id, "assessed", assessment=a.model_dump(mode="json"))
        return {"assessment": a.model_dump(mode="json")}

    def after_assess(state: LeadState) -> str:
        return "end" if state["assessment"]["intent"] in ("spam", "existing_guest") else "draft"

    def no_reply(state: LeadState) -> LeadState:
        d.store.advance(state["lead_id"], "no_reply")
        return {"outcome": "no_reply"}

    def do_draft(state: LeadState) -> LeadState:
        d.events.append("draft")
        ld = lead(state)
        a = LeadAssessment.model_validate(state["assessment"])
        dr = draft_reply(
            run(), d.client, d.cfg, ld.payload, a, reply_channel(ld.payload, ld.contact or {})
        )
        d.store.advance(ld.id, "drafted", draft=dr.model_dump())
        return {"draft": dr.model_dump()}

    def _send_args(state: LeadState) -> tuple[str, dict[str, Any]]:
        dr = state["draft"]
        if dr["channel"] == "whatsapp":
            return "send_whatsapp", {"lead_id": state["lead_id"], "body": dr["body"]}
        return "send_email", {
            "lead_id": state["lead_id"],
            "subject": dr["subject"],
            "body": dr["body"],
        }

    def gate(state: LeadState) -> str:
        if d.outbound_mode == "approve_first" and d.approvals is not None:
            return "request_approval"
        return "send" if d.outbound_mode == "allowlist" else "hold"

    def hold(state: LeadState) -> LeadState:
        d.events.append("hold")
        d.store.advance(state["lead_id"], "held", data={"reason": f"outbound {d.outbound_mode}"})
        return {"outcome": "held"}

    def send(state: LeadState) -> LeadState:
        d.events.append("send")
        tool, args = _send_args(state)
        res = run().tool(d.registry, tool, args, outbound_mode=d.outbound_mode)
        if res.ok:
            d.store.advance(state["lead_id"], "sent", data={"tool": tool})
            return {"outcome": "sent"}
        d.store.advance(state["lead_id"], "held", data={"reason": res.error})
        return {"outcome": "blocked"}

    def request_approval(state: LeadState) -> LeadState:
        d.events.append("request_approval")
        tool, args = _send_args(state)
        res = run().tool(d.registry, tool, args, outbound_mode=d.outbound_mode)
        if res.error != "approval_required":  # blocked before approval (allowlist, stopped)
            d.store.advance(state["lead_id"], "held", data={"reason": res.error})
            return {"outcome": "blocked"}
        assert d.approvals is not None
        req = run().request_approval(res, d.approvals)
        d.store.advance(state["lead_id"], "approval_requested", approval_id=req.id)
        return {"approval_id": str(req.id)}

    def await_approval(state: LeadState) -> LeadState:
        if not state.get("approval_id"):
            return {}
        decision = interrupt({"approval_id": state["approval_id"]})  # pauses the graph
        return {"decision": str(decision)}

    def after_approval(state: LeadState) -> str:
        if not state.get("approval_id"):
            return "route"
        return "send_approved" if state.get("decision") == "approved" else "rejected"

    def send_approved(state: LeadState) -> LeadState:
        d.events.append("send_approved")
        from uuid import UUID

        assert d.approvals is not None
        res = run().resume_approved(d.registry, d.approvals, UUID(state["approval_id"]))
        if res.ok:
            d.store.advance(state["lead_id"], "approved_sent")
            return {"outcome": "sent"}
        d.store.advance(state["lead_id"], "rejected", data={"reason": res.error})
        return {"outcome": "blocked"}

    def rejected(state: LeadState) -> LeadState:
        d.store.advance(state["lead_id"], "rejected")
        return {"outcome": "rejected"}

    def route(state: LeadState) -> LeadState:
        d.events.append("route")
        ld = lead(state)
        if ld.stage not in ("contacted", "held"):
            return {}
        a = LeadAssessment.model_validate(state["assessment"])
        target = routing_owner(d.cfg.rules.get("routing", []), a) if a.tier == 1 else None
        if target:
            p = ld.payload
            thread = [{"from": "lead", "channel": p.get("channel"), "text": p.get("message")}]
            thread += [
                {"from": "palmgrove", "text": m["body"], "status": m["status"]}
                for m in d.store.messages(ld.id)
                if m["channel"] != "slack"
            ]
            text = (
                f"Hot lead for {target['owner']}: {p.get('name')} — {a.intent.replace('_', ' ')}"
                f", {a.guests or '?'} guests, {a.check_in or 'dates TBC'}. {a.rationale}"
            )
            run().tool(
                d.registry,
                "post_slack",
                {
                    "channel": target["channel"],
                    "text": text,
                    "lead_id": str(ld.id),
                    "payload": {
                        "summary": text,
                        "rationale": a.rationale,
                        "tier": a.tier,
                        "thread": thread,
                        "crm_url": d.crm.contact_url(ld.crm_id or ""),
                        "owner": target["owner"],
                    },
                },
            )
            d.store.advance(ld.id, "routed", owner=target["owner"])
        elif ld.stage == "contacted" and d.jobs is not None:
            plan = d.cfg.rules.get("follow_up", {}).get(f"tier_{a.tier}", [])
            for i, delay in enumerate(plan, 1):
                d.jobs.enqueue(
                    "dispatcher.follow_up",
                    {"lead_id": str(ld.id), "n": i},
                    key=f"lead:{ld.id}:followup:{i}",
                    delay=_parse_delay(delay),
                    run_id=run().run_id,
                )
            if plan:
                d.store.advance(ld.id, "follow_up_scheduled", data={"count": len(plan)})
        return {}

    g = StateGraph(LeadState)
    for name, fn in [
        ("upsert_crm", upsert_crm),
        ("assess", do_assess),
        ("no_reply", no_reply),
        ("draft", do_draft),
        ("hold", hold),
        ("send", send),
        ("request_approval", request_approval),
        ("await_approval", await_approval),
        ("send_approved", send_approved),
        ("rejected", rejected),
        ("route", route),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "upsert_crm")
    g.add_edge("upsert_crm", "assess")
    g.add_conditional_edges("assess", after_assess, {"end": "no_reply", "draft": "draft"})
    g.add_edge("no_reply", END)
    g.add_conditional_edges("draft", gate, ["hold", "send", "request_approval"])
    g.add_edge("request_approval", "await_approval")
    g.add_conditional_edges(
        "await_approval", after_approval, ["send_approved", "rejected", "route"]
    )
    for n in ("hold", "send", "send_approved", "rejected"):
        g.add_edge(n, "route")
    g.add_edge("route", END)
    return g


__all__ = ["AGENT", "GraphDeps", "LeadState", "build", "routing_owner"]

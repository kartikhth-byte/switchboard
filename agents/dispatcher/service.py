"""Dispatcher entry points: intake (API/n8n), processing and follow-up jobs (worker), lead events
(replies, takeover, opt-out) and approval resumes. Mocks unless DEMO_INTEGRATIONS=real."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from agents.dispatcher.graph import GraphDeps, build
from agents.dispatcher.normalize import dedupe_key, normalize
from agents.dispatcher.store import Lead, LeadStore
from agents.dispatcher.tools import AGENT, ToolDeps, build_registry
from core.approvals import ApprovalStore
from core.config.agent import AgentConfig, load_agent_config
from core.config.settings import Settings, allowlist
from core.jobs import Job, JobQueue
from core.models import ModelClient
from core.runs import PgRunStore, RunStore, start_run
from core.runs.models import RunKind
from integrations.crm.pg import PgMockCRM
from integrations.messaging import Email, LeadCRM, MockEmail, MockWhatsApp, WhatsApp
from integrations.slack import MockSlack, Slack

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]
IST = timezone(timedelta(hours=5, minutes=30))
EventKind = Literal["lead_replied", "human_takeover", "opt_out"]


def today_ist() -> date:
    return datetime.now(IST).date()


@dataclass
class Adapters:
    crm: LeadCRM
    whatsapp: WhatsApp
    email: Email
    slack: Slack


def mock_adapters(dsn: str) -> Adapters:
    return Adapters(PgMockCRM(dsn), MockWhatsApp(), MockEmail(), MockSlack())


def make_adapters(settings: Settings) -> Adapters:
    """Mocks unless DEMO_INTEGRATIONS=real (real GoHighLevel / WhatsApp / Slack / Mailpit: M3.3)."""
    if settings.demo_integrations == "real":
        raise NotImplementedError("real Dispatcher adapters land in M3.3")
    return mock_adapters(settings.database_url)


@dataclass
class Dispatcher:
    settings: Settings
    adapters: Adapters
    client: ModelClient | None = None  # tests inject a scripted model
    checkpointer: BaseCheckpointSaver[Any] | None = None  # default: Postgres
    run_store: RunStore | None = None
    langfuse: Any = None  # Langfuse client: one trace per lead (LEAD-006)
    run_kind: RunKind = "live"
    today: Callable[[], date] = today_ist
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    cfg: AgentConfig = field(default_factory=lambda: load_agent_config(ROOT / "agents/dispatcher"))
    last_events: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        dsn = self.settings.database_url
        self.store = LeadStore(dsn)
        self.jobs = JobQueue(dsn)
        self.approvals = ApprovalStore(dsn)
        self.runs = self.run_store or PgRunStore(dsn)
        w = self.cfg.rules.get("whatsapp", {})
        self.registry = build_registry(
            ToolDeps(
                store=self.store,
                whatsapp=self.adapters.whatsapp,
                email=self.adapters.email,
                slack=self.adapters.slack,
                allowlist=allowlist(self.settings),
                window_hours=int(w.get("session_window_hours", 24)),
                template=str(w.get("template_first_contact", "palmgrove_enquiry_ack")),
                now=self.now,
            )
        )

    # ---- intake: normalize + dedupe, then hand off to the worker (LEAD-001, 005, 010, 016)

    def intake(self, payload: dict[str, Any], *, enqueue: bool = True) -> tuple[Lead, bool]:
        region = str(self.cfg.rules.get("default_region", "IN"))
        contact = normalize(payload.get("email"), payload.get("phone"), region)
        lead, created = self.store.intake(payload, contact.model_dump(), dedupe_key(contact))
        if created and enqueue:
            self.jobs.enqueue(
                "dispatcher.process",
                {"lead_id": str(lead.id)},
                key=f"lead:{lead.id}:process",
                max_attempts=3,
            )
        return lead, created

    # ---- the graph

    def _graph(self, checkpointer: BaseCheckpointSaver[Any]) -> tuple[Any, GraphDeps]:
        deps = GraphDeps(
            cfg=self.cfg,
            client=self.client or ModelClient(self.cfg.model),
            store=self.store,
            crm=self.adapters.crm,
            registry=self.registry,
            approvals=self.approvals,
            jobs=self.jobs,
            today=self.today(),
            outbound_mode=self.settings.outbound_mode,
        )
        return build(deps).compile(checkpointer=checkpointer), deps

    def _invoke(self, lead_id: str, inp: Any, run_input: Any) -> dict[str, Any]:
        def go(cp: BaseCheckpointSaver[Any]) -> dict[str, Any]:
            graph, deps = self._graph(cp)
            with start_run(
                AGENT,
                self.runs,
                self.langfuse,
                outbound_mode=self.settings.outbound_mode,
                run_input=run_input,
                session_id=f"lead-{lead_id}",
                budget=self.cfg.budget(
                    self.settings.daily_usd_cap if self.run_kind == "live" else None
                ),
                kind=self.run_kind,
            ) as run:
                deps.run = run
                state = graph.invoke(inp, {"configurable": {"thread_id": lead_id}})
                if "__interrupt__" in state:
                    run.set_status("waiting_approval")
            self.last_events = deps.events
            return {
                "state": state,
                "run_id": str(run.run_id),
                "trace_id": run.trace_id,
                "cost_usd": run.cost_usd,
                "status": run.status,
                "events": deps.events,
            }

        if self.checkpointer is not None:
            return go(self.checkpointer)
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(self.settings.database_url) as cp:
            cp.setup()
            return go(cp)

    def process(self, lead_id: str) -> dict[str, Any]:
        lead = self.store.get(lead_id)
        if lead is None or lead.stage != "normalized":
            return {"skipped": True, "stage": lead.stage if lead else None}  # replay of the job
        return self._invoke(lead_id, {"lead_id": lead_id}, {"message": lead.payload.get("message")})

    def resume(self, lead_id: str, decision: str) -> dict[str, Any]:
        """Continue a lead paused for approval (SHR-008): `decision` is approved/rejected."""
        return self._invoke(lead_id, Command(resume=decision), {"decision": decision})

    # ---- events that stop automation (LEAD-013, LEAD-020)

    def is_opt_out(self, text: str | None) -> bool:
        t = (text or "").lower()
        return any(p in t for p in self.cfg.rules.get("opt_out_phrases", []))

    def event(self, lead_id: str, kind: EventKind, text: str | None = None) -> Lead:
        if kind == "lead_replied" and self.is_opt_out(text):
            kind = "opt_out"
        cancelled = self.jobs.cancel_prefix(f"lead:{lead_id}:followup:")
        fields: dict[str, Any] = {}
        if kind == "opt_out":
            fields["opted_out"] = True
        elif kind == "human_takeover":
            fields["human_owner"] = True
        else:
            fields["last_inbound_at"] = self.now()  # a reply opens the WhatsApp 24h window
        return self.store.advance(
            lead_id, kind, data={"text": text, "cancelled_jobs": cancelled}, **fields
        )

    # ---- follow-ups (worker): never after a stop event, even if a cancel raced the job

    def follow_up(self, lead_id: str, n: int) -> str:
        lead = self.store.get(lead_id)
        if lead is None or lead.stopped or lead.stage not in ("contacted", "nurturing"):
            return "skipped"
        a = lead.assessment or {}
        first = (lead.payload.get("name") or "there").split()[0]
        what = a.get("villa") or "a stay with us"
        when = f" for {a['check_in']}" if a.get("check_in") else ""
        body = (
            f"Hi {first}, just checking in on your enquiry about {what}{when}. "
            "Happy to hold dates or answer any questions — just reply here."
        )
        tool = "send_whatsapp" if (lead.draft or {}).get("channel") == "whatsapp" else "send_email"
        args: dict[str, Any] = {"lead_id": lead_id, "body": body}
        if tool == "send_email":
            args["subject"] = "Following up on your Palmgrove Stays enquiry"
        with start_run(
            AGENT,
            self.runs,
            self.langfuse,
            outbound_mode=self.settings.outbound_mode,
            run_input={"follow_up": n},
            session_id=f"lead-{lead_id}",
            kind=self.run_kind,
        ) as run:
            res = run.tool(
                self.registry,
                tool,
                args,
                outbound_mode=self.settings.outbound_mode,
                idempotency_key=f"lead:{lead_id}:followup:{n}",
            )
        if res.ok:
            self.store.advance(lead_id, "follow_up_sent", data={"n": n})
            return "sent"
        return f"not_sent: {res.error}"

    # ---- failures (LEAD-005): alert with the run id, mark the lead

    def on_job_failure(self, job: Job, error: str) -> None:
        lead_id = job.payload.get("lead_id")
        text = (
            f"Dispatcher job failed: {job.kind} lead={lead_id} run={job.run_id or '-'} "
            f"after {job.attempts} attempts: {error[:300]}"
        )
        self.registry.execute(
            "post_slack", {"channel": "#ops-alerts", "text": text, "lead_id": lead_id}, _ctx()
        )
        if lead_id:
            try:
                self.store.advance(lead_id, "failed", data={"error": error[:300]})
            except Exception:  # noqa: BLE001 — terminal stage already; the alert went out
                log.warning("could not mark lead failed")

    def register_jobs(self, worker: Any) -> None:
        worker.register("dispatcher.process", lambda j: self.process(j.payload["lead_id"]))
        worker.register(
            "dispatcher.follow_up",
            lambda j: self.follow_up(j.payload["lead_id"], int(j.payload["n"])),
        )
        worker.register(
            "dispatcher.resume", lambda j: self.resume(j.payload["lead_id"], j.payload["decision"])
        )


def _ctx() -> Any:
    from uuid import uuid4

    from core.tools import ToolContext

    return ToolContext(run_id=uuid4(), agent=AGENT)

"""Runs one EvalCase through the real Dispatcher graph on mocks (CRM, WhatsApp, email, Slack).

The model is real; "today" is frozen at the dataset date; every run gets its own dedupe scope so
re-runs never collide with earlier leads. Outbound runs in allowlist mode against the fixture
domain, so drafts reach the mock adapters and the outbox like a live run would.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver

from agents.dispatcher.service import Dispatcher, mock_adapters
from core.config.settings import Settings
from core.db import connect
from core.evals import AgentOutput, EvalCase
from core.models import ModelClient
from core.runs import RunStore

EVAL_TODAY = date(2026, 10, 17)
ALLOW = "@example.org,+9198765*"


@dataclass
class EvalEnv:
    settings: Settings
    client: ModelClient
    run_store: RunStore | None = None
    langfuse: Any = None
    scope: str = ""

    def __post_init__(self) -> None:
        self.scope = self.scope or f"eval-{uuid4().hex[:8]}"


START = datetime.fromisoformat("2026-10-17T18:00:00+05:30")


class Clock:
    def __init__(self) -> None:
        self.t = START

    def __call__(self) -> datetime:
        return self.t


def dispatcher(env: EvalEnv, clock: Clock | None = None) -> Dispatcher:
    s = env.settings.model_copy(update={"outbound_mode": "allowlist", "outbound_allowlist": ALLOW})
    d = Dispatcher(
        s,
        mock_adapters(s.database_url),
        client=env.client,
        checkpointer=InMemorySaver(),
        run_store=env.run_store,
        langfuse=env.langfuse,
        run_kind="eval",
        today=lambda: EVAL_TODAY,
        now=clock or Clock(),
        dedupe_scope=env.scope,
    )
    return d


def _pending(d: Dispatcher, prefix: str) -> int:
    with connect(d.settings.database_url) as c:
        row = c.execute(
            "SELECT count(*) AS n FROM jobs WHERE status = 'pending' AND key LIKE %s",
            (prefix + "%",),
        ).fetchone()
    return int(row["n"]) if row else 0


def _sent(d: Dispatcher, lead_id: Any) -> int:
    return sum(
        1 for m in d.store.messages(lead_id) if m["status"] == "sent" and m["channel"] != "slack"
    )


def run_target(env: EvalEnv, case: EvalCase) -> AgentOutput:
    assert case.input is not None
    clock = Clock()
    d = dispatcher(env, clock)
    lead, _ = d.intake(dict(case.input["payload"]), enqueue=False)
    out = d.process(str(lead.id))
    data: dict[str, Any] = {"lead_id": str(lead.id), "events": out.get("events", [])}
    got = d.store.get(lead.id)
    assert got is not None
    data["assessment"] = got.assessment
    data["draft"] = got.draft
    data["stage_after_process"] = got.stage

    ev = case.input.get("event")
    if ev:  # LEAD-013 / LEAD-020: an event, then the worst case — every follow-up job fires anyway
        before = _sent(d, lead.id)
        clock.t += timedelta(hours=float(ev.get("after_hours", 1)))
        after_event = d.event(str(lead.id), ev["kind"], ev.get("reply"))
        plan = d.cfg.rules.get("follow_up", {}).get(
            f"tier_{(got.assessment or {}).get('tier')}", []
        )
        follow = [d.follow_up(str(lead.id), n) for n in range(1, len(plan) + 1)]
        data.update(
            event=ev["kind"],
            stage_after_event=after_event.stage,
            sent_before_event=before,
            sent_after_event=_sent(d, lead.id) - before,
            follow_up_results=follow,
            pending_follow_ups=_pending(d, f"lead:{lead.id}:followup:"),
        )
    return AgentOutput(
        answer=(got.draft or {}).get("body"),
        data=data,
        trace_id=str(out.get("trace_id") or ""),
        cost_usd=float(out.get("cost_usd") or 0.0),
    )

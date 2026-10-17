"""Runs one EvalCase against the Concierge and records what the assertions need.

Each case gets a fresh conversation, fresh mock CRM/Slack, and "today" frozen at the
dataset's date, so runs are comparable. Model, KB, run store and approvals are real.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from langfuse import Langfuse

from agents.concierge.loop import Deps, TurnResult, new_state, respond
from agents.concierge.tools import KBSearch, ToolDeps, load_bookings
from core.approvals import ApprovalStore
from core.config.agent import AgentConfig
from core.evals import AgentOutput, EvalCase, SimulatedUser, run_conversation
from core.evals.models import Persona
from core.models import ModelClient
from core.retrieval import Hit
from core.runs import RunStore
from integrations.crm import MockCRM
from integrations.slack import MockSlack

ROOT = Path(__file__).resolve().parents[3]
EVAL_TODAY = date(2026, 10, 17)


class RecordingKB:
    """Wraps the KB so the judge can see exactly what the agent retrieved."""

    def __init__(self, inner: KBSearch) -> None:
        self.inner = inner
        self.hits: dict[str, Hit] = {}

    def search(self, query: str, k: int = 5) -> list[Hit]:
        hits = self.inner.search(query, k)
        self.hits.update({h.chunk_id: h for h in hits})
        return hits


@dataclass
class EvalEnv:
    cfg: AgentConfig
    client: ModelClient
    kb: KBSearch
    run_store: RunStore
    sim_user_client: ModelClient
    approvals: ApprovalStore | None = None
    memory_client: ModelClient | None = None
    langfuse: Langfuse | None = None  # eval runs are traced like any other run (SHR-006/019)
    today: Callable[[], date] = field(default=lambda: EVAL_TODAY)


def _deps(env: EvalEnv, case_id: str) -> tuple[Deps, RecordingKB, MockCRM, MockSlack]:
    kb, crm, slack = RecordingKB(env.kb), MockCRM(), MockSlack()
    tools = ToolDeps(
        kb=kb,
        crm=crm,
        slack=slack,
        bookings=load_bookings(ROOT / "data/fixtures/support/bookings.json"),
        state=new_state(f"eval-{case_id}"),
        rules=env.cfg.rules,
        today=env.today,
    )
    deps = Deps(
        cfg=env.cfg,
        client=env.client,
        tools=tools,
        run_store=env.run_store,
        approvals=env.approvals,
        memory_client=env.memory_client,
        langfuse=env.langfuse,
        daily_cap=None,  # the eval runner's --max-usd caps spend instead
    )
    return deps, kb, crm, slack


def run_target(env: EvalEnv, case: EvalCase) -> AgentOutput:
    deps, kb, crm, slack = _deps(env, case.id)
    turns: list[TurnResult] = []
    sim_cost = 0.0

    if case.persona is not None:
        user = SimulatedUser(Persona.model_validate(case.persona), env.sim_user_client)

        def agent_turn(msg: str) -> str:
            r = respond(deps, msg)
            turns.append(r)
            return r.answer

        _, outcome = run_conversation(agent_turn, user)
        sim_cost = user.cost_usd
    else:
        assert case.input is not None
        messages = case.input.get("turns") or [case.input["message"]]
        for msg in messages:
            turns.append(respond(deps, msg))
        outcome = "scripted"

    last = turns[-1]
    st = deps.tools.state
    return AgentOutput(
        answer=last.answer,
        transcript=list(st.transcript),
        trace_id=last.trace_id,
        cost_usd=sum(t.cost_usd for t in turns) + sim_cost,
        data={
            "status": last.status,
            "citations": last.citations,
            "confidence": last.confidence,
            "escalated": any(t.escalated for t in turns),
            "statuses": [t.status for t in turns],
            "tool_calls": [c for t in turns for c in t.tool_calls],
            "retrieved": {cid: h.model_dump() for cid, h in kb.hits.items()},
            "crm_writes": [list(w) for w in crm.writes],
            "handoffs": [m["payload"] for m in slack.messages],
            "verified": st.verified,
            "assistant_turns": [t.answer for t in turns],
            "outcome": outcome,
            "trace_ids": [t.trace_id for t in turns],
        },
    )

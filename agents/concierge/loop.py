"""Concierge: hand-built ReAct loop, no agent framework (SUPP-015).

One `respond()` call = one guest turn = one run (SHR-004). Each step is one model call
plus its tool calls, each traced as its own observation (SHR-006).

Stop conditions, all explicit:
  final_answer        -> reply (citations validated against this turn's retrieved chunks)
  step limit          -> escalate (reason: step_limit)
  budget exceeded     -> guardrail message (SHR-018)
  requires_human      -> escalate (tool said a person must take over)
  approval_required   -> run ends as waiting_approval; the next turn resumes it (SHR-008)
  same tool error x2  -> escalate (one retry via the model, then a person)
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from uuid import UUID

from langfuse import Langfuse

from agents.concierge.tools import AGENT, ConversationState, ToolDeps, build_registry
from core.approvals import ApprovalStore
from core.config.agent import AgentConfig
from core.config.settings import get_settings
from core.models import Message, ModelClient
from core.runs import RunHandle, RunStore, start_run
from core.tools import ToolRegistry, ToolResult

OBSERVATION_MAX_CHARS = 2_000
HANDOFF_ANSWER = (
    "I've passed this to a member of our team, who will reply here shortly "
    "(8am–10pm IST; overnight messages are answered by 9am)."
)
APPROVAL_ANSWER = (
    "I've sent this to a manager for approval. You'll get an answer here, usually within "
    "one working day."
)


@dataclass
class Deps:
    cfg: AgentConfig
    client: ModelClient
    tools: ToolDeps
    run_store: RunStore
    approvals: ApprovalStore | None = None
    langfuse: Langfuse | None = None


@dataclass
class TurnResult:
    answer: str
    status: str  # done | escalated | waiting_approval | budget_exceeded | failed
    citations: list[str] = field(default_factory=list)
    confidence: float | None = None
    escalated: bool = False
    stop_reason: str = ""
    run_id: str = ""
    trace_id: str = ""
    cost_usd: float = 0.0
    tool_calls: list[str] = field(default_factory=list)


def respond(deps: Deps, user_message: str) -> TurnResult:
    state = deps.tools.state
    state.transcript.append({"role": "user", "content": user_message})
    state.retrieved = set()
    state.handoff = None  # per turn: a new turn may need its own handoff
    settings = get_settings()
    with start_run(
        AGENT,
        deps.run_store,
        deps.langfuse,
        outbound_mode=settings.outbound_mode,
        run_input=user_message,
        session_id=state.conversation_id,
        budget=deps.cfg.budget(settings.daily_usd_cap),
    ) as run:
        result = _loop(run, deps)
    if run.status == "budget_exceeded":
        result = TurnResult(
            answer=run.user_message or "", status="budget_exceeded", stop_reason="budget"
        )
    result.run_id, result.trace_id, result.cost_usd = str(run.run_id), run.trace_id, run.cost_usd
    state.transcript.append({"role": "assistant", "content": result.answer})
    return result


def _messages(deps: Deps) -> list[Message]:
    msgs: list[Message] = [{"role": "system", "content": deps.cfg.prompt("system")}]
    msgs += [{"role": t["role"], "content": t["content"]} for t in deps.tools.state.transcript]
    return msgs


def _observation(call_id: str, result: ToolResult) -> Message:
    body = json.dumps(result.model_dump(), ensure_ascii=False, default=str)
    if len(body) > OBSERVATION_MAX_CHARS:
        body = body[:OBSERVATION_MAX_CHARS] + '…[truncated]"'
    return {"role": "tool", "tool_call_id": call_id, "content": body}


def _escalate(
    run: RunHandle, reg: ToolRegistry, state: ConversationState, reason: str, summary: str
) -> None:
    """Hand off once per turn; the model may already have called handoff_to_human."""
    if state.handoff is None:
        run.tool(reg, "handoff_to_human", {"reason": reason, "summary": summary[:1000]})


def _resume_pending(run: RunHandle, deps: Deps, reg: ToolRegistry) -> str | None:
    """If last turn ended waiting for approval, act on the decision now (SHR-008)."""
    st = deps.tools.state
    if not st.pending_approval_id or deps.approvals is None:
        return None
    res = run.resume_approved(reg, deps.approvals, UUID(st.pending_approval_id))
    if res.error == "approval_pending":
        return "Note: the earlier request is still waiting for a manager's approval."
    st.pending_approval_id = None
    if res.ok:
        return f"Note: a manager approved the earlier request; it was completed: {res.payload}"
    return f"Note: the earlier request was not approved ({res.error}). Tell the guest politely."


def _loop(run: RunHandle, deps: Deps) -> TurnResult:
    state = deps.tools.state
    reg = build_registry(deps.tools)
    schemas = reg.schemas_for(AGENT)
    messages = _messages(deps)
    if (note := _resume_pending(run, deps, reg)) is not None:
        messages.append({"role": "system", "content": note})
    min_conf = float(deps.cfg.rules.get("min_confidence", 0.5))
    errors: Counter[tuple[str, str]] = Counter()
    called: list[str] = []

    for step in range(deps.cfg.limits.max_steps):
        resp = run.model(deps.client, messages, schemas, name=f"step-{step}")
        if not resp.tool_calls:
            # A plain-text reply skips the citation contract: ask once for final_answer.
            messages.append({"role": "assistant", "content": resp.content or ""})
            messages.append(
                {"role": "user", "content": "(system) Reply using the final_answer tool."}
            )
            continue
        messages.append(
            {
                "role": "assistant",
                "content": resp.content or "",
                "tool_calls": [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.raw_arguments or "{}"},
                    }
                    for c in resp.tool_calls
                ],
            }
        )
        for call in resp.tool_calls:
            called.append(call.name)
            result = run.tool(reg, call.name, call.arguments)

            if call.name == "final_answer" and result.ok and result.payload:
                fa = result.payload
                bad = [c for c in fa["citations"] if c not in state.retrieved]
                if bad:
                    result = ToolResult(
                        ok=False,
                        error=f"invalid_citations: {bad} were not returned by search_kb "
                        "in this turn; cite only retrieved chunk_ids",
                    )
                else:
                    escalate = bool(fa["escalate"]) or fa["confidence"] < min_conf
                    if escalate:
                        _escalate(run, reg, state, "missing_answer", fa["answer"])
                    escalated = state.handoff is not None
                    answer = fa["answer"]
                    if escalated and HANDOFF_ANSWER not in answer:
                        answer = f"{answer}\n\n{HANDOFF_ANSWER}"
                    return TurnResult(
                        answer=answer,
                        status="escalated" if escalated else "done",
                        citations=list(fa["citations"]),
                        confidence=float(fa["confidence"]),
                        escalated=escalated,
                        stop_reason="final_answer",
                        tool_calls=called,
                    )

            if result.error == "approval_required" and deps.approvals is not None:
                req = run.request_approval(result, deps.approvals)
                state.pending_approval_id = str(req.id)
                return TurnResult(
                    answer=APPROVAL_ANSWER,
                    status="waiting_approval",
                    stop_reason="approval_required",
                    tool_calls=called,
                )
            if result.error and result.error.startswith("requires_human"):
                _escalate(run, reg, state, "other", result.error)
                return _escalated(called, "requires_human")
            if not result.ok:
                errors[(call.name, (result.error or "")[:60])] += 1
                if errors[(call.name, (result.error or "")[:60])] >= 2:
                    _escalate(run, reg, state, "other", f"repeated tool error in {call.name}")
                    return _escalated(called, "repeated_tool_error")
            if call.name == "handoff_to_human" and result.ok:
                return _escalated(called, "handoff")
            messages.append(_observation(call.id, result))

    _escalate(run, reg, state, "other", "step limit reached without an answer")
    return _escalated(called, "step_limit")


def _escalated(called: list[str], reason: str) -> TurnResult:
    return TurnResult(
        answer=HANDOFF_ANSWER,
        status="escalated",
        escalated=True,
        stop_reason=reason,
        tool_calls=called,
    )


def new_state(conversation_id: str) -> ConversationState:
    return ConversationState(conversation_id=conversation_id)


__all__ = ["Deps", "TurnResult", "new_state", "respond"]

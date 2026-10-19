"""Dispatcher assertions: all code (build spec §6.4). Each returns 1.0 (pass) or 0.0 (fail).

The tier is computed in code from the extracted facts (assessment.tier_for), so tier errors are
extraction errors — dates, guest count or intent read wrongly — not a model "scoring" leads.
"""

from __future__ import annotations

from agents.dispatcher.assessment import LeadAssessment
from core.evals import AgentOutput, EvalCase, assertion


def _a(out: AgentOutput) -> dict:  # type: ignore[type-arg]
    return out.data.get("assessment") or {}


@assertion("assessment_valid")
def assessment_valid(case: EvalCase, out: AgentOutput) -> float:
    try:
        LeadAssessment.model_validate(_a(out))
    except Exception:  # noqa: BLE001
        return 0.0
    return 1.0


@assertion("intent_correct")
def intent_correct(case: EvalCase, out: AgentOutput) -> float:
    return float(_a(out).get("intent") == case.expected["intent"])


@assertion("tier_exact")
def tier_exact(case: EvalCase, out: AgentOutput) -> float:
    return float(_a(out).get("tier") == case.expected["tier"])


@assertion("tier_within_one")
def tier_within_one(case: EvalCase, out: AgentOutput) -> float:
    t = _a(out).get("tier")
    return float(t is not None and abs(int(t) - int(case.expected["tier"])) <= 1)


@assertion("no_messages_after_event")
def no_messages_after_event(case: EvalCase, out: AgentOutput) -> float:
    d = out.data
    stage_ok = d.get("stage_after_event") in {"replied", "with_owner", "opted_out"}
    return float(
        stage_ok
        and d.get("sent_after_event") == case.expected.get("messages_after_event", 0)
        and d.get("pending_follow_ups") == 0
    )

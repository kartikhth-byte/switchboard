"""Trivial suite proving the runner end to end (week-1 checklist item 5). No API keys.

The "agent" is a lookup table; real agents replace `target` with a call into their loop.
"""

from pathlib import Path

from core.evals.models import AgentOutput, EvalCase
from core.evals.runner import EvalSuite

FAQ = {
    "checkout": ("Check-out is at 11am.", {"intent": "faq"}),
    "refund": ("I'll pass this to a person who can help with refunds.", {"intent": "escalate"}),
}


def target(case: EvalCase) -> AgentOutput:
    assert case.input is not None
    answer, data = FAQ.get(
        case.input["topic"], ("Sorry, I don't know that.", {"intent": "unknown"})
    )
    return AgentOutput(answer=answer, data=data, trace_id="", cost_usd=0.0)


def suite() -> EvalSuite:
    return EvalSuite(
        agent="echo",
        cases_dir=Path(__file__).resolve().parents[3] / "data/fixtures/echo/evals",
        target=target,
        agent_provider="none",
    )

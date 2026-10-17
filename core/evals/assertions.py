"""Named assertion functions (code checks first, judges second — spec §8.2).

An assertion takes (case, output) and returns a metric value in [0, 1]. A case passes
when every assertion returns 1.0. Agents register their own in their evals module.
"""

from __future__ import annotations

from collections.abc import Callable

from core.evals.models import AgentOutput, EvalCase

Assertion = Callable[[EvalCase, AgentOutput], float]
_REGISTRY: dict[str, Assertion] = {}


def assertion(name: str) -> Callable[[Assertion], Assertion]:
    def deco(fn: Assertion) -> Assertion:
        if name in _REGISTRY and _REGISTRY[name] is not fn:
            raise ValueError(f"assertion already registered: {name}")
        _REGISTRY[name] = fn
        return fn

    return deco


def get_assertion(name: str) -> Assertion:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown assertion '{name}'; registered: {sorted(_REGISTRY)}") from None


# ---- generic built-ins ----


@assertion("answer_contains")
def answer_contains(case: EvalCase, out: AgentOutput) -> float:
    """expected.answer_contains: list of substrings that must all appear (case-insensitive)."""
    needles = case.expected.get("answer_contains", [])
    text = (out.answer or "").lower()
    return float(all(n.lower() in text for n in needles))


@assertion("answer_excludes")
def answer_excludes(case: EvalCase, out: AgentOutput) -> float:
    banned = case.expected.get("answer_excludes", [])
    text = (out.answer or "").lower()
    return float(not any(b.lower() in text for b in banned))


@assertion("data_equals")
def data_equals(case: EvalCase, out: AgentOutput) -> float:
    """expected.data: every key must match out.data exactly (e.g. intent, should_escalate)."""
    want = case.expected.get("data", {})
    return float(all(out.data.get(k) == v for k, v in want.items()))


@assertion("goal_met")
def goal_met(case: EvalCase, out: AgentOutput) -> float:
    """Simulated-user cases: the conversation driver records whether the persona got its goal."""
    return float(bool(out.data.get("goal_met")))

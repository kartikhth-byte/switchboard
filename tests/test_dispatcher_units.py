"""Dispatcher pieces that need no database."""

from datetime import date

import pytest

from agents.dispatcher import assessment as A
from agents.dispatcher.graph import routing_owner
from core.guardrails.outbound import allowed
from data.generators import lead_evals as gen


@pytest.mark.parametrize("intent", list(gen.MIX))
@pytest.mark.parametrize("days", [None, -1, 0, 30, 90, 91, 200])
@pytest.mark.parametrize("guests", [None, 2])
@pytest.mark.parametrize("fit", [True, False])
def test_agent_rubric_matches_the_labelling_rubric(intent: str, days, guests, fit) -> None:  # type: ignore[no-untyped-def]
    if days is not None and days < 0:
        return  # past dates: the generator never labels them
    assert A.tier_for(intent, days, guests, fit) == gen.tier_for(intent, days, guests, fit)


def test_finalize_computes_tier_in_code() -> None:
    x = A.Extraction(
        intent="new_booking", guests=4, check_in=date(2026, 11, 1), rationale="dates and group"
    )
    a = A.finalize(x, date(2026, 10, 17))
    assert (a.tier, a.tier_name, a.days_ahead) == (1, "hot", 15)
    far = A.finalize(x.model_copy(update={"check_in": date(2027, 3, 1)}), date(2026, 10, 17))
    assert far.tier == 2


def test_allowlist_forms() -> None:
    allow = {"@example.org", "+9198765*", "kt@palmgrove.test"}
    assert allowed("Asha@Example.org", allow) and allowed("+919876543210", allow)
    assert allowed("kt@palmgrove.test", allow)
    assert not allowed("asha@gmail.com", allow) and not allowed("+14155552671", allow)


def test_routing_rules_first_match_wins() -> None:
    rules = [
        {"when": {"tier": 1, "intent": ["group_booking", "event"]}, "owner": "@events"},
        {"when": {"tier": 1}, "owner": "@res"},
    ]
    base = A.finalize(
        A.Extraction(intent="event", guests=8, check_in=date(2026, 11, 1), rationale="party"),
        date(2026, 10, 17),
    )
    assert routing_owner(rules, base) == {"owner": "@events", "channel": "#leads"}
    nb = base.model_copy(update={"intent": "new_booking"})
    assert routing_owner(rules, nb) == {"owner": "@res", "channel": "#leads"}
    assert routing_owner(rules, base.model_copy(update={"tier": 2})) is None

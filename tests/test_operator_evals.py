"""The Operator eval target and assertions, with scripted models (no API keys)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents.operator.evals import assertions as A
from agents.operator.evals.target import EvalEnv, run_target
from agents.operator.salon import load_salon
from core.config.agent import load_agent_config
from core.config.settings import get_settings
from core.db import migrate
from core.evals import EvalCase
from core.models import ModelClient, ModelResponse, ModelSpec
from core.models.client import ToolCall
from core.runs import PgRunStore
from tests.test_operator_session import Script

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


class Caller(ModelClient):
    def __init__(self, lines: list[str]) -> None:
        super().__init__(ModelSpec(provider="openai", name="gpt-4o-mini"))
        self.lines = list(lines)

    def complete(self, messages: Any, tools: Any = None, **_: Any) -> ModelResponse:
        return ModelResponse(content=self.lines.pop(0), model="gpt-4o-mini")


def cases(name: str) -> dict[str, EvalCase]:
    path = ROOT / "data/fixtures/operator/evals" / f"{name}.jsonl"
    return {
        c.id: c for c in (EvalCase.model_validate_json(x) for x in path.read_text().splitlines())
    }


def env(agent: ModelClient, caller: ModelClient) -> EvalEnv:
    s = get_settings()
    migrate(s.database_url)
    return EvalEnv(
        load_agent_config(ROOT / "agents/operator"),
        load_salon(),
        s.database_url,
        agent,
        caller,
        PgRunStore(s.database_url),
    )


def score(case: EvalCase, out: Any) -> dict[str, float]:
    from core.evals import get_assertion

    return {a: get_assertion(a)(case, out) for a in case.assertions}


def test_create_case_scores_from_the_db() -> None:
    c = cases("calls")["call-000"]  # men's haircut with Farhan, Saturday 2027-01-09 at 4pm
    slot = "2027-01-09T16:00:00+05:30"
    agent = Script(
        [
            {"find_slots": {"service_id": "haircut_men", "day": "Saturday", "time": "4pm"}},
            "Farhan has 4pm Saturday. Shall I hold it?",
            {"hold_slot": {"service_id": "haircut_men", "stylist_id": "farhan", "start": slot}},
            "Held. Your name?",
            {"confirm_booking": {"hold_id": "__HOLD__", "name": "Ravi"}},
            "You're booked, Ravi.",
        ]
    )
    caller = Caller(["Yes please.", "Ravi.", "[DONE]"])
    orig = agent.complete

    def patched(messages: Any, tools: Any = None, **kw: Any) -> ModelResponse:
        step = agent.steps[0]
        if isinstance(step, dict) and "confirm_booking" in step:
            tool_msgs = [m for m in messages if m.get("role") == "tool"]
            step["confirm_booking"]["hold_id"] = json.loads(tool_msgs[-1]["content"])["payload"][
                "hold_id"
            ]
        return orig(messages, tools, **kw)

    agent.complete = patched  # type: ignore[method-assign]
    out = run_target(env(agent, caller), c)
    s = score(c, out)
    assert s == {
        "disclosure": 1.0,
        "calendar_state": 1.0,
        "no_false_success": 1.0,
        "transfer_expected": 1.0,
        "no_canary_in_transcript": 1.0,
        "times_grounded": 1.0,
    }, (s, out.data)


def test_false_success_claim_is_caught() -> None:
    c = cases("calls")["call-001"]
    agent = Script(["Great, you're booked for tomorrow at 10!"])  # nothing written to the DB
    out = run_target(env(agent, Caller(["[DONE]"])), c)
    s = score(c, out)
    assert s["calendar_state"] == 0.0 and s["no_false_success"] == 0.0


def test_cancel_case_and_rerun_resets_the_week() -> None:
    c = next(x for x in cases("calls").values() if x.expected.get("action") == "cancel")
    for _ in range(2):  # second run must start from the same seeded state
        agent = Script(
            [{"find_my_bookings": {}}, "Found it. Cancel?", "__CANCEL__", "Done, it's cancelled."]
        )

        def step_cancel(
            messages: Any, tools: Any = None, _a: Script = agent, **kw: Any
        ) -> ModelResponse:
            if _a.steps and _a.steps[0] == "__CANCEL__":
                tool = json.loads([m for m in messages if m.get("role") == "tool"][-1]["content"])
                _a.steps[0] = {
                    "cancel_booking": {"booking_id": tool["payload"]["bookings"][0]["booking_id"]}
                }
            return Script.complete(_a, messages, tools, **kw)

        agent.complete = step_cancel  # type: ignore[method-assign]
        out = run_target(env(agent, Caller(["Yes, cancel it.", "[DONE]"])), c)
        assert score(c, out)["calendar_state"] == 1.0, out.data["bookings"]


def test_faq_facts_and_unknowns() -> None:
    faq = cases("faq")
    q = faq["faq-001"]  # men's haircut price
    out = run_target(
        env(Script(["A men's haircut is 700 rupees and takes half an hour."]), Caller([])), q
    )
    assert score(q, out) == {
        "faq_facts": 1.0,
        "offers_transfer_if_unknown": 1.0,
        "no_booking_made": 1.0,
    }
    unk = next(c for c in faq.values() if c.expected["offer_help"])
    bad = run_target(env(Script(["Yes, we have free wifi."]), Caller([])), unk)
    good = run_target(
        env(Script(["I'm not sure, I can have someone call you back."]), Caller([])), unk
    )
    assert score(unk, bad)["offers_transfer_if_unknown"] == 0.0
    assert score(unk, good)["offers_transfer_if_unknown"] == 1.0


def test_canary_check() -> None:
    from core.evals import AgentOutput

    c = cases("calls")["call-000"]
    leak = AgentOutput(data={"stored_transcript": {"turns": [{"text": "call me on 9845012345"}]}})
    assert A.no_canary_in_transcript(c, leak) == 0.0


def test_a_wrong_latest_claim_fails_times_grounded_even_without_a_booking() -> None:
    """The gap a reader found: a wrong "latest is 6:15" passed if the caller didn't book."""
    c = cases("slots")["slot-005"]  # Friday, men's haircut; the real latest start is 19:00
    agent = Script(["The latest we have on Friday is 6:15 in the evening."])
    out = run_target(env(agent, Script(["[GIVE_UP]"])), c)
    s = score(c, out)
    assert s["times_grounded"] == 0.0 and s["calendar_state"] == 0.0
    assert out.data["ungrounded"][0]["truth"] == ["19:00"]


def test_a_booking_under_another_number_the_caller_gave_still_counts() -> None:
    """Eval call-000: the caller offered 'my contact number', the agent booked under it (the
    prompt allows that), and the eval, looking up by caller ID only, missed the booking."""
    c = cases("calls")["call-000"]  # men's haircut with Farhan, Saturday ~16:00
    hold = {
        "hold_slot": {
            "service_id": "haircut_men",
            "stylist_id": "farhan",
            "start": "2027-01-09T16:00:00+05:30",
        }
    }
    agent = Script([hold, "Held. Your name?", "CONFIRM", "You're booked for 4 on Saturday."])
    real = agent.complete

    def patched(messages, tools=None, **kw):  # type: ignore[no-untyped-def]
        step = real(messages, tools, **kw)
        if step.content == "CONFIRM":  # confirm the hold under the other number
            hid = json.loads([m for m in messages if m.get("role") == "tool"][-1]["content"])
            args = {"hold_id": hid["payload"]["hold_id"], "name": "Ravi", "phone": "+919845012345"}
            return ModelResponse(
                content="",
                model="claude-haiku-4-5",
                tool_calls=[ToolCall(id="c", name="confirm_booking", arguments=args,
                                     raw_arguments=json.dumps(args))],
            )  # fmt: skip
        return step

    agent.complete = patched  # type: ignore[method-assign]
    caller = Caller(["Ravi, and my other number is +91 98450 12345.", "[DONE]", "[DONE]"])
    out = run_target(env(agent, caller), c)
    assert score(c, out)["calendar_state"] == 1.0

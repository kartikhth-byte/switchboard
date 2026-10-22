"""Operator call session with a scripted model: code-decided opening line, transfers, the booking
flow ending in the right DB state, and a redacted transcript (VOICE-001/002/003/017)."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from uuid import uuid4

import pytest

from agents.operator.calendar import Calendar
from agents.operator.salon import load_salon
from agents.operator.session import CallSession
from core.config.agent import load_agent_config
from core.config.settings import get_settings
from core.db import connect, migrate
from core.memory import ConversationStore
from core.models import ModelClient, ModelResponse, ModelSpec
from core.models.client import ToolCall
from core.runs import PgRunStore

pytestmark = pytest.mark.integration
SALON = load_salon()
TZ = SALON.tz
CFG = load_agent_config("agents/operator")


class Script(ModelClient):
    """Replays a list of steps: a dict of tool calls {name: args} or a final string."""

    def __init__(self, steps: list[Any]) -> None:
        super().__init__(ModelSpec(provider="anthropic", name="claude-haiku-4-5"))
        self.steps = list(steps)
        self.seen: list[list[Any]] = []

    def complete(self, messages: Any, tools: Any = None, **_: Any) -> ModelResponse:
        self.seen.append(list(messages))
        step = self.steps.pop(0)
        if isinstance(step, str):
            return ModelResponse(
                content=step, model="claude-haiku-4-5", tokens_in=40, tokens_out=10
            )
        calls = [
            ToolCall(id=f"t{i}", name=n, arguments=a, raw_arguments=json.dumps(a))
            for i, (n, a) in enumerate(step.items())
        ]
        return ModelResponse(
            content="", tool_calls=calls, model="claude-haiku-4-5", tokens_in=40, tokens_out=10
        )


def last_tool_result(script: Script) -> dict[str, Any]:
    tool_msgs = [m for m in script.seen[-1] if m.get("role") == "tool"]
    return json.loads(tool_msgs[-1]["content"])


@pytest.fixture
def make() -> Any:
    dsn = get_settings().database_url
    migrate(dsn)
    with connect(dsn) as c:
        c.execute("TRUNCATE booking_audit, appointments")

    def _make(steps: list[Any], now: datetime, phone: str = "+919800000042") -> CallSession:
        cal = Calendar(dsn, SALON, now=lambda: now)
        return CallSession(
            CFG,
            cal,
            Script(steps),
            PgRunStore(dsn),
            call_id=f"t-{uuid4().hex[:8]}",
            caller_phone=phone,
            transcripts=ConversationStore(dsn, "operator"),
            run_kind="eval",
        )

    return _make


OPEN = datetime(2027, 1, 5, 11, 0, tzinfo=TZ)  # Tuesday 11:00
CLOSED = datetime(2027, 1, 4, 21, 0, tzinfo=TZ)  # Monday night


def test_opening_line_discloses_ai_and_recording_and_after_hours(make: Any) -> None:
    day = make([], OPEN).greet()
    night = make([], CLOSED).greet()
    assert "AI assistant" in day and "recorded" in day and "closed right now" not in day
    assert "closed right now" in night


def test_asking_for_a_person_transfers_without_the_model(make: Any) -> None:
    s = make([], OPEN)
    t = s.respond("Can I speak to a person please?")
    assert t.transferred and s.state.transferred and s.client.seen == []  # type: ignore[attr-defined]


def test_booking_flow_ends_with_a_confirmed_db_row(make: Any) -> None:
    slot = "2027-01-06T10:00:00+05:30"
    s = make(
        [
            {"find_slots": {"service_id": "haircut_men", "day": "tomorrow", "time": "morning"}},
            "Farhan has 10 in the morning or 10:15 tomorrow. Which suits you?",
            {"hold_slot": {"service_id": "haircut_men", "stylist_id": "farhan", "start": slot}},
            "That's a men's haircut with Farhan, Wednesday at 10am. Can I take your name?",
            {"confirm_booking": {"hold_id": "__HOLD__", "name": "Ravi"}},
            "You're booked, Ravi. See you Wednesday at 10.",
        ],
        OPEN,
    )
    s.greet()
    t1 = s.respond("Hi, I need a men's haircut tomorrow morning")
    assert "find_slots" in t1.tools and t1.used_filler
    s.respond("10 is good")
    hold_id = last_tool_result(s.client)["payload"]["hold_id"]  # type: ignore[arg-type]
    s.client.steps[0]["confirm_booking"]["hold_id"] = hold_id  # type: ignore[attr-defined]
    t3 = s.respond("It's Ravi")
    assert "confirm_booking" in t3.tools and "booked" in t3.text
    (row,) = s.cal.bookings_for("+919800000042")
    assert row["service_id"] == "haircut_men" and row["stylist_id"] == "farhan"
    assert row["start"] == datetime.fromisoformat(slot)


def test_reschedule_replaces_the_old_booking(make: Any) -> None:
    s = make([], OPEN)
    old = s.cal.confirm(
        s.cal.hold("leela", "blowdry", datetime(2027, 1, 6, 12, 0, tzinfo=TZ)),
        "Priya",
        "+919800000042",
    )
    s.client.steps = [
        {
            "hold_slot": {
                "service_id": "blowdry",
                "stylist_id": "leela",
                "start": "2027-01-06T15:00:00+05:30",
                "replaces_booking_id": str(old),
            }
        },
        "Held 3pm. Shall I move it?",
        {"confirm_booking": {"hold_id": "__HOLD__", "name": "Priya"}},
        "Done, moved to 3pm.",
    ]
    s.respond("Move my blow-dry tomorrow to 3pm")
    s.client.steps[0]["confirm_booking"]["hold_id"] = last_tool_result(s.client)["payload"][
        "hold_id"
    ]
    s.respond("Yes")
    rows = s.cal.bookings_for("+919800000042")
    assert [r["start"].astimezone(TZ).hour for r in rows] == [15]
    assert s.cal.get(old)["status"] == "cancelled"


def test_someone_elses_booking_cannot_be_cancelled(make: Any) -> None:
    s = make([], OPEN, phone="+919800000099")
    other = s.cal.confirm(
        s.cal.hold("anita", "blowdry", datetime(2027, 1, 6, 12, 0, tzinfo=TZ)),
        "Someone",
        "+919800000001",
    )
    s.client.steps = [{"cancel_booking": {"booking_id": str(other)}}, "I couldn't find that one."]
    s.respond("cancel booking " + str(other))
    assert s.cal.get(other)["status"] == "confirmed"


def test_two_failed_booking_turns_transfer(make: Any) -> None:
    bad = {
        "hold_slot": {
            "service_id": "keratin",
            "stylist_id": "farhan",
            "start": "2027-01-06T10:00:00+05:30",
        }
    }
    s = make([bad, "Sorry, that didn't work.", bad, "Sorry again."], OPEN)
    assert not s.respond("Keratin with Farhan tomorrow at 10").transferred
    assert s.respond("Try again").transferred and s.state.transferred


def test_stored_transcript_is_redacted(make: Any) -> None:
    s = make(["Thanks, noted."], OPEN)
    s.greet()
    s.respond("My other number is +91 98450 12345 and email ravi.k@example.com")
    stored = json.dumps(ConversationStore(get_settings().database_url, "operator").load(s.call_id))
    assert "98450 12345" not in stored and "ravi.k@example.com" not in stored


def test_closed_days_are_reported_by_find_slots(make: Any) -> None:
    s = make([], OPEN)
    assert s.cal.find_slots("haircut_men", date(2027, 1, 11), date(2027, 1, 11)) == []  # Monday


def test_empty_window_returns_hours_and_real_alternatives(make: Any) -> None:
    """Live voice call 2026-10-21: asked for 8am Friday (opens 10), the agent offered 9 without
    checking. The tool now returns the day's hours and the nearest real free times."""
    from uuid import uuid4

    from agents.operator.tools import CallState, build_registry
    from core.tools import ToolContext

    s = make([], OPEN)
    reg = build_registry(s.cal, CallState("x", "+919800000042"))
    out = reg.execute(
        "find_slots",
        {"service_id": "haircut_men", "day": "Friday", "time": "eight in the morning"},
        ToolContext(run_id=uuid4(), agent="operator"),
    ).payload
    assert out["slots"] == [] and out["opening_hours"] == {"2027-01-08": "10:00-20:00"}
    assert out["nearest_free"][0]["start"].startswith("2027-01-08T10:00")
    assert out["understood"].startswith("Friday 08 January (open 10:00-20:00)")
    old_style = reg.execute(
        "find_slots",
        {"service_id": "haircut_men", "date_from": "2027-01-08", "earliest_time": "07:30"},
        ToolContext(run_id=uuid4(), agent="operator"),
    )
    assert not old_style.ok  # unknown parameters are rejected, not silently ignored


def test_sunday_said_on_a_sunday(make: Any) -> None:
    """Article review: 'Sunday' said on a Sunday means today while there's time left to book,
    next Sunday once the day is over; 'this Sunday' and 'today' always mean today."""
    from uuid import uuid4

    from agents.operator.tools import CallState, build_registry
    from core.tools import ToolContext

    def ask(now: datetime, day: str) -> str:
        s = make([], now)
        reg = build_registry(s.cal, CallState("x", "+919800000042"))
        out = reg.execute(
            "find_slots",
            {"service_id": "haircut_men", "day": day},
            ToolContext(run_id=uuid4(), agent="operator"),
        ).payload
        return out["understood"]

    sun_morning = datetime(2027, 1, 10, 9, 0, tzinfo=TZ)  # opens 10:00, closes 17:00
    sun_evening = datetime(2027, 1, 10, 16, 30, tzinfo=TZ)  # past the last bookable start
    assert ask(sun_morning, "Sunday").startswith("Sunday 10 January")
    assert ask(sun_evening, "Sunday").startswith("Sunday 17 January")
    assert ask(sun_evening, "this Sunday").startswith("Sunday 10 January")


def test_unsay_forgets_an_unheard_reply_but_keeps_tool_results() -> None:
    from agents.operator.session import CallSession

    s = CallSession.__new__(CallSession)  # unsay needs only the message lists
    s.messages = [
        {"role": "system", "content": "x"},
        {"role": "user", "content": "Yes, that works."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
        {"role": "assistant", "content": "Held 9:00. Your name?"},
    ]
    s.turns = [{"role": "agent", "text": "Held 9:00. Your name?"}]
    s.unsay()
    assert s.messages[-1]["role"] == "tool"  # the hold is still visible to the model
    assert s.turns[-1]["role"] == "agent_unheard"
    s.unsay()  # nothing spoken to forget: a no-op
    assert s.messages[-1]["role"] == "tool"

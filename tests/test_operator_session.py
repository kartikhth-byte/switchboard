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


def test_caller_resuming_before_any_tool_restarts_from_the_whole_utterance(make: Any) -> None:
    call = make(["(never said)", "Nine o'clock is free. Shall I book it?"], OPEN)
    call.greet()
    stops = iter([False, True])  # the caller resumes while the model is answering
    first = call.respond("Yes, that works.", should_stop=lambda: next(stops))
    assert first.aborted and not first.tools
    second = call.respond("Please book me for nine on Saturday.")
    assert second.text.startswith("Nine o'clock")
    users = [m["content"] for m in call.client.seen[-1] if m["role"] == "user"]
    assert users[-1] == "Yes, that works. Please book me for nine on Saturday."
    assert "(never said)" not in json.dumps(call.messages)


def test_caller_resuming_after_a_tool_keeps_its_result(make: Any) -> None:
    stops = iter([False, False, True])  # top of step 0, after step 0's model call, top of step 1
    call = make(
        [{"find_slots": {"service_id": "haircut_men", "day": "Saturday", "time": "4pm"}}, "Done."],
        OPEN,
    )
    call.greet()
    t = call.respond("Men's haircut Saturday at four", should_stop=lambda: next(stops))
    assert t.aborted and t.tools == ["find_slots"]
    assert call.messages[-1]["role"] == "tool"  # what was looked up stays visible
    assert call.client.steps == ["Done."]  # the second model call never happened


def _jobs(kind: str) -> list[dict[str, Any]]:
    with connect(get_settings().database_url) as c:
        return c.execute(
            "SELECT payload, key FROM jobs WHERE kind = %s ORDER BY created_at", (kind,)
        ).fetchall()


def test_missed_calls_become_one_callback_job_each(make: Any) -> None:
    from agents.operator.notify import Notifier
    from core.jobs import JobQueue

    dsn = get_settings().database_url
    with connect(dsn) as c:
        c.execute("DELETE FROM jobs WHERE kind = 'operator.callback'")

    def session(steps: list[Any]) -> CallSession:
        call = make(steps, OPEN)
        call.notifier = Notifier(call.cal, JobQueue(dsn))
        return call

    silent = session([])
    silent.greet()
    assert silent.end() == "hung up before saying anything"
    assert silent.end() == "hung up before saying anything"  # twice: still one job

    person = session([])
    person.respond("Can I speak to a person please")
    assert "nobody took the call" in (person.end() or "")

    answered = session(["We close at 8 tonight."])
    answered.respond("What time do you close?")
    assert answered.end() is None  # a question answered is not a missed call

    jobs = _jobs("operator.callback")
    assert len(jobs) == 2
    assert jobs[1]["payload"]["last_said"] == "Can I speak to a person please"
    assert jobs[0]["payload"]["phone"] == "+919800000042"


def test_hanging_up_mid_booking_is_a_missed_call(make: Any) -> None:
    call = make(
        [
            {"find_slots": {"service_id": "haircut_men", "day": "Saturday", "time": "4pm"}},
            "Four o'clock is free with Farhan. Shall I hold it?",
            {
                "hold_slot": {
                    "stylist_id": "farhan",
                    "service_id": "haircut_men",
                    "start": "2027-01-09T16:00:00+05:30",
                }
            },
            "Held. What name should I put it under?",
        ],
        OPEN,
    )
    call.respond("Men's haircut Saturday at four")
    call.respond("Yes please")
    assert call.state.holds and not call.state.booked
    assert call.end() == "hung up in the middle of a booking"


def test_find_slots_says_how_many_and_each_days_first_and_last(make: Any) -> None:
    """Reader feedback on the dates post: a sample of slots let the model treat the last one it
    saw as the last one there was. The tool now returns the count and per-day first/last."""
    from uuid import uuid4

    from agents.operator.tools import CallState, build_registry
    from core.tools import ToolContext

    s = make([], OPEN)
    reg = build_registry(s.cal, CallState("x", "+919800000042"))
    out = reg.execute(
        "find_slots",
        {"service_id": "haircut_men", "day": "Saturday"},
        ToolContext(run_id=uuid4(), agent="operator"),
    ).payload
    sat = out["free_by_day"]["2027-01-09"]
    assert len(out["slots"]) == 4 and out["showing"].startswith(f"4 of {sat['count']}")
    assert sat["count"] > 4 and sat["first"] == "09:00"  # Saturdays open at 9
    assert sat["last"] > out["slots"][-1]["start"][11:16]  # the sample's end isn't the day's


def test_slot_summary_can_be_switched_off_for_the_ablation(make: Any, monkeypatch: Any) -> None:
    from uuid import uuid4

    from agents.operator.tools import CallState, build_registry
    from core.tools import ToolContext

    monkeypatch.setenv("OPERATOR_SLOT_SUMMARY", "off")
    s = make([], OPEN)
    out = (
        build_registry(s.cal, CallState("x", "+919800000042"))
        .execute(
            "find_slots",
            {"service_id": "haircut_men", "day": "Saturday"},
            ToolContext(run_id=uuid4(), agent="operator"),
        )
        .payload
    )
    assert len(out["slots"]) == 4 and "showing" not in out and "free_by_day" not in out


HOLD_SAT_4 = {
    "hold_slot": {
        "stylist_id": "farhan",
        "service_id": "haircut_men",
        "start": "2027-01-09T16:00:00+05:30",
    }
}


def test_no_booking_tool_runs_after_the_caller_hangs_up(make: Any) -> None:
    """Review 2026-10-23 #1: a model call returning during hang-up still ran its tools."""
    call = make([HOLD_SAT_4, "Held."], OPEN)

    def model_returns_after_hangup(messages: Any, tools: Any = None, **_: Any) -> ModelResponse:
        call.end()  # the caller hangs up while the model is answering
        return Script.complete(call.client, messages, tools)

    call.client.complete = model_returns_after_hangup  # type: ignore[method-assign]
    t = call.respond("Men's haircut Saturday at four")
    assert t.aborted and not t.tools and not call.state.holds


def test_caller_resuming_mid_batch_skips_the_rest_of_the_tools(make: Any) -> None:
    """Review #4: should_stop was checked around the model call only, so a confirm queued after
    a hold in the same batch still ran."""
    confirm = {"confirm_booking": {"hold_id": "will-not-run", "name": "Ravi"}}
    call = make([{**HOLD_SAT_4, **confirm}, "Done."], OPEN)
    checks = iter([False, False, True])  # top of step, after model, before the 2nd tool
    t = call.respond("Book Saturday at four, I'm Ravi", should_stop=lambda: next(checks, True))
    assert t.aborted and t.tools == ["hold_slot"] and not call.state.booked
    tool_ids = {m["tool_call_id"] for m in call.messages if m["role"] == "tool"}
    asked = {c["id"] for m in call.messages for c in m.get("tool_calls", [])}
    assert tool_ids == asked  # every tool call has a result: the history stays valid
    assert "not run" in call.messages[-1]["content"]


def test_unheard_and_cut_off_replies_are_saved_as_such(make: Any) -> None:
    """Review #5: unsay() changed the transcript in memory only."""
    call = make(["We close at 8.", "Saturdays we open at 9."], OPEN)
    call.respond("When do you close?")
    call.unsay()
    call.respond("And Saturday?")
    call.mark_interrupted()
    stored = ConversationStore(get_settings().database_url, "operator").load(call.call_id)
    roles = [t["role"] for t in stored["turns"]]
    assert roles == ["caller", "agent_unheard", "caller", "agent_interrupted"]

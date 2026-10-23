"""times_grounded: every time the agent says is checked against the calendar before the call.
Phrases are taken from real eval transcripts."""

from __future__ import annotations

from datetime import date

from agents.operator.evals.claims import Truth, check, times_in

FRI = "2027-01-08"


def truth(free: list[str], by: dict[str, list[str]] | None = None, booked: set[str] | None = None):  # type: ignore[no-untyped-def]
    return Truth(
        today=date(2027, 1, 5),
        case_day=date(2027, 1, 8),
        free={FRI: free},
        by_stylist={k: {FRI: v} for k, v in (by or {}).items()},
        hours={FRI: ["10:00", "20:00"]},
        bookings=booked or set(),
        stylists={"anita": "anita", "farhan": "farhan", "leela": "leela"},
    )


EVENING = ["17:30", "17:45", "18:00", "18:15", "18:30", "18:45", "19:00"]


def kinds(text: str, t: Truth) -> list[tuple[str, list[str]]]:
    return [(b["kind"], b["time"]) for b in check([text], t)]


def test_times_are_read_the_way_the_agent_says_them() -> None:
    assert times_in("at 6:15 in the afternoon") == [{"18:15"}]
    assert times_in("How about 3 in the afternoon or 4 in the afternoon?") == [{"15:00"}, {"16:00"}]
    assert times_in("at 12 noon.") == [{"12:00"}] and times_in("at 12 o'clock noon") == [{"12:00"}]
    assert times_in("at 4 o'clock.") == [{"04:00", "16:00"}]  # no am/pm: both readings
    assert times_in("at six pm") == [{"18:00"}]
    none = ["30 minutes and costs 600 rupees", "at least 48 hours before", "the 9th"]
    none += ["we're at 12 Fontainhas Lane", "Friday after 6", "arrive 5 minutes early"]
    assert times_in("Leela can do 4:45, or 5 with Farhan") == [
        {"04:45", "16:45"},
        {"05:00", "17:00"},
    ]
    for text in none:
        assert times_in(text) == [], text


def test_a_wrong_latest_fails_and_the_right_one_passes() -> None:
    t = truth(EVENING)
    assert kinds("The latest we have after work on Friday is 6:15 in the afternoon.", t) == [
        ("late", ["18:15"])
    ]
    assert kinds("The latest we have on Friday is 7 in the evening.", t) == []
    # computed from closing time instead of asking the calendar (call-015)
    t2 = truth(["17:00", "18:15"])
    assert kinds("We close at 8 that day, so the latest would be around 6.", t2)[0][0] == "late"


def test_only_the_time_next_to_earliest_is_the_range_claim() -> None:
    t = truth(["09:00", "09:15", "09:30", "10:00"])
    assert kinds("The earliest he has free is 9 in the morning, or 9:15 or 9:30.", t) == []


def test_offers_must_be_real_and_bookings_and_hours_are_fine() -> None:
    t = truth(EVENING, booked={"16:00"})
    assert kinds("How about 6 in the evening or 6:30?", t) == []
    assert kinds("I can do 5 in the afternoon on Friday.", t) == [("offer", ["17:00"])]
    assert kinds("You're booked for Friday at 4 o'clock.", t) == []  # a booking time
    assert kinds("We open at 10 and close at 8 on Fridays.", t) == []


def test_a_taken_claim_about_a_free_time_fails() -> None:
    t = truth(EVENING)
    assert kinds("Sorry, 6:30 in the evening is taken on Friday.", t) == [("negative", ["18:30"])]
    assert kinds("Sorry, 5 in the afternoon isn't free, but 6 in the evening is.", t) == []


def test_a_named_stylist_is_checked_on_their_own_diary() -> None:
    t = truth(["17:00", "18:00"], by={"anita": ["17:00"], "leela": ["18:00"]})
    assert kinds("Anita has 6 in the evening on Friday.", t) == [("offer", ["18:00"])]
    assert kinds("Leela has 6 in the evening on Friday.", t) == []

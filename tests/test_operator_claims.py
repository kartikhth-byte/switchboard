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
        bookings={f"{FRI} {t} farhan" for t in (booked or set())},
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


def test_cases_from_the_second_review() -> None:
    """Each line is a misjudgement the second review found; all now judged correctly."""
    sat = "2027-01-09"
    t = Truth(
        today=date(2027, 1, 5),
        case_day=date(2027, 1, 8),
        free={FRI: ["10:00", "16:00", "18:15"], sat: ["16:00"]},
        by_stylist={
            "anita": {FRI: ["10:00"]},
            "leela": {FRI: ["16:00"]},
        },
        hours={FRI: ["10:00", "20:00"], sat: ["09:00", "20:00"]},
        bookings={"2027-01-05 17:00 farhan"},
        stylists={"anita": "anita", "farhan": "farhan", "leela": "leela"},
    )
    # a bare or word-number first item in a list is parsed (was silently skipped)
    assert kinds("We can do ten on Friday.", t) == []
    assert kinds("On Friday I have 11, 10:15 or 10:30.", t)[0] == ("offer", ["11:00", "23:00"])
    # two stylists: each time is checked against the nearest one (was the union)
    assert kinds("Anita has 4pm and Leela has 10am on Friday.", t) == [
        ("offer", ["16:00"]),
        ("offer", ["10:00"]),
    ]
    # two days: each time against its own day
    assert kinds("Friday at 10am or Saturday at 4pm.", t) == []
    assert kinds("Friday at 4pm or Saturday at 10am.", t) == [("offer", ["10:00"])]
    # a booking on another day doesn't make a time free today
    assert kinds("I can do Friday at 5pm.", t) == [("offer", ["17:00"])]
    # "sorry" alone isn't a negative claim
    assert kinds("Sorry for the wait, 10am is free on Friday.", t) == []
    # hours skip is per clause: a booking offer at closing time isn't excused
    assert kinds("We close at 8pm and I can book you at 8pm on Friday.", t) == [
        ("offer", ["20:00"])
    ]
    # a range inside a window is about the window
    assert kinds("The first slot after 6 on Friday is 6:15.", t) == []
    # a time's own "in the afternoon" isn't a window; "already booked you" isn't a negative
    t2 = truth(["17:00", "18:15"])
    assert kinds("The latest slot is 6:15 in the afternoon on Friday.", t2) == []
    assert kinds("I've already booked you at 6:15 in the evening on Friday.", t2) == []
    # durations aren't clock times
    assert times_in("Highlights takes 1:30 or so.") == []


def test_cases_from_the_third_review() -> None:
    mon, fri = "2027-01-11", FRI  # Monday is closed (not in hours)
    t = Truth(
        today=date(2027, 1, 5),
        case_day=date(2027, 1, 8),
        free={fri: ["10:00", "16:00", "18:15"]},
        by_stylist={"anita": {fri: ["10:00"]}, "leela": {fri: ["16:00"]}},
        hours={fri: ["10:00", "20:00"]},
        bookings={f"{fri} 17:00 anita"},
        stylists={"anita": "anita", "farhan": "farhan", "leela": "leela"},
        dates=["2027-01-05", fri, mon],
    )
    assert kinds("We have 4pm Monday.", t) == [("offer", ["16:00"])]  # closed: nothing free
    assert kinds("The first slot after 6pm on Friday is 6:15pm.", t) == []  # 6pm is a boundary
    assert kinds("We close at 8pm Friday, with an 8pm slot.", t) == [("offer", ["20:00"])]
    assert kinds("I can do at 4 Friday.", t) == []  # "at 4 Friday" parses
    assert times_in("It takes about 1:30.") == []
    assert kinds("Leela has 5pm on Friday.", t) == [("offer", ["17:00"])]  # that's Anita's
    t.today = date(2027, 1, 8)
    assert kinds("Nothing at 11 this morning.", t) == []  # this morning = today, 11 not free

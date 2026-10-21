"""Operator calendar of record: slots, holds, bookings, reschedule, audit, and VOICE-004's
concurrency test (callers racing for the same slot, 20 runs, 0 double bookings)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier

import pytest

from agents.operator.calendar import BookingError, Calendar, SlotTaken
from agents.operator.salon import load_salon
from core.config.settings import get_settings
from core.db import connect, migrate

pytestmark = pytest.mark.integration
SALON = load_salon()
TZ = SALON.tz
TUE = date(2027, 1, 5)  # Tuesday 10:00-19:00
NOW = datetime(2027, 1, 4, 9, 0, tzinfo=TZ)  # Monday morning, salon closed


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def cal() -> Calendar:
    dsn = get_settings().database_url
    migrate(dsn)
    with connect(dsn) as c:
        c.execute("TRUNCATE booking_audit, appointments")
    return Calendar(dsn, SALON, now=Clock(NOW))


def at(h: int, m: int = 0, day: date = TUE) -> datetime:
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def test_slots_respect_skills_durations_hours_and_closed_days(cal: Calendar) -> None:
    keratin = cal.find_slots("keratin", TUE, TUE, limit=100)
    assert keratin and {s.stylist_id for s in keratin} == {"anita"}  # only Anita has the skill
    assert all(s.end - s.start == timedelta(minutes=180) for s in keratin)
    assert max(s.start for s in keratin) == at(16)  # 16:00 + 3h = closing time
    assert cal.find_slots("haircut_men", NOW.date(), NOW.date()) == []  # closed Monday
    with pytest.raises(BookingError):
        cal.find_slots("keratin", TUE, TUE, stylist_id="farhan")


def test_overlapping_services_of_different_lengths_cannot_both_hold(cal: Calendar) -> None:
    cal.hold("anita", "colour_full", at(10))  # 10:00-12:00
    with pytest.raises(SlotTaken):
        cal.hold("anita", "haircut_women", at(11))  # 11:00-11:45 overlaps, different start
    cal.hold("leela", "haircut_women", at(11))  # another stylist is fine
    cal.hold("anita", "blowdry", at(12))  # touching, not overlapping: [10,12) then [12,12:30)
    starts = {s.start for s in cal.find_slots("blowdry", TUE, TUE, stylist_id="anita", limit=50)}
    assert at(10) not in starts and at(11, 30) not in starts and at(12, 30) in starts


def test_expired_hold_frees_the_slot_and_cannot_be_confirmed(cal: Calendar) -> None:
    clock = cal.now
    h = cal.hold("farhan", "haircut_men", at(15))
    clock.t = NOW + timedelta(seconds=SALON.hold_ttl_seconds + 1)  # type: ignore[attr-defined]
    with pytest.raises(BookingError):
        cal.confirm(h, "Ravi", "+919800000001")
    cal.hold("farhan", "haircut_men", at(15))  # same slot, new caller


def test_confirm_reschedule_cancel_and_audit(cal: Calendar) -> None:
    b = cal.confirm(cal.hold("leela", "kids_cut", at(16), call_id="c1"), "Priya", "+919800000002")
    assert cal.get(b)["status"] == "confirmed"  # type: ignore[index]
    new = cal.confirm(cal.hold("leela", "kids_cut", at(17), call_id="c2", replaces=b),
                      "Priya", "+919800000002", call_id="c2")  # fmt: skip
    assert cal.get(b)["status"] == "cancelled" and cal.get(new)["status"] == "confirmed"  # type: ignore[index]
    assert [r["start"] for r in cal.bookings_for("+919800000002")] == [at(17)]
    cal.cancel(new, call_id="c3")
    with pytest.raises(BookingError):
        cal.cancel(new)
    assert [a["action"] for a in cal.audit(new)] == ["hold", "reschedule", "cancel"]
    assert [a["action"] for a in cal.audit(b)] == ["hold", "confirm", "cancel"]


def test_rules_from_config_are_enforced_on_hold(cal: Calendar) -> None:
    with pytest.raises(BookingError, match="doesn't do"):
        cal.hold("farhan", "keratin", at(10))
    with pytest.raises(BookingError, match="opening hours"):
        cal.hold("anita", "keratin", at(17))  # would end 20:00, closes 19:00
    cal.now.t = at(10, 30)  # type: ignore[attr-defined]  # open now; min lead is 60 minutes
    with pytest.raises(BookingError, match="too soon"):
        cal.hold("farhan", "haircut_men", at(11))
    cal.hold("farhan", "haircut_men", at(11, 30))


@pytest.mark.parametrize("run", range(20))
def test_racing_callers_never_double_book(cal: Calendar, run: int) -> None:
    """VOICE-004: 2-5 callers ask for the same stylist at overlapping times at the same instant."""
    callers = 2 + run % 4
    start = at(10) + timedelta(minutes=15 * (run % 3))
    gate = Barrier(callers)

    def call(i: int) -> str:
        gate.wait()
        try:
            svc = ["haircut_women", "colour_full", "blowdry"][i % 3]
            h = cal.hold("anita", svc, start + timedelta(minutes=15 * (i % 2)), call_id=f"r{i}")
            cal.confirm(h, f"Caller {i}", f"+9198000100{i:02d}")
            return "booked"
        except SlotTaken:
            return "taken"

    with ThreadPoolExecutor(callers) as pool:
        results = list(pool.map(call, range(callers)))
    assert results.count("booked") == 1, results
    with connect(cal.dsn) as c:
        overlaps = c.execute(
            "SELECT count(*) AS n FROM appointments a JOIN appointments b ON a.id < b.id"
            " AND a.stylist_id = b.stylist_id AND a.period && b.period"
            " WHERE a.status IN ('held','confirmed') AND b.status IN ('held','confirmed')"
        ).fetchone()
    assert overlaps and overlaps["n"] == 0

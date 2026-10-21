"""Caller phrases -> dates and windows, resolved in code. 'Today' = Wednesday 21 Oct 2026."""

from __future__ import annotations

from datetime import date, time

import pytest

from agents.operator.when import NotUnderstood, resolve_day, resolve_time

TODAY = date(2026, 10, 21)  # Wednesday


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("today", date(2026, 10, 21)),
        ("tomorrow", date(2026, 10, 22)),
        ("tomorrow morning", date(2026, 10, 22)),
        ("day after tomorrow", date(2026, 10, 23)),
        ("Friday", date(2026, 10, 23)),
        ("this Sunday", date(2026, 10, 25)),  # call-012 got this wrong
        ("Sunday morning", date(2026, 10, 25)),
        ("on Saturday", date(2026, 10, 24)),
        ("sat", date(2026, 10, 24)),
        ("Wednesday", date(2026, 10, 21)),  # same weekday = today
        ("next Wednesday", date(2026, 10, 28)),
        ("next week Friday", date(2026, 10, 30)),
        ("Friday the 23rd", date(2026, 10, 23)),
        ("the 23rd", date(2026, 10, 23)),
        ("the 5th", date(2026, 11, 5)),  # already past this month -> next month
        ("30 October", date(2026, 10, 30)),
        ("October 30", date(2026, 10, 30)),
        ("Nov 2", date(2026, 11, 2)),
        ("2 January", date(2027, 1, 2)),  # year rolls over
        ("30/10", date(2026, 10, 30)),  # day first (India)
        ("in 3 days", date(2026, 10, 24)),
        ("in two days", date(2026, 10, 23)),
    ],
)
def test_days(phrase: str, expected: date) -> None:
    assert resolve_day(phrase, TODAY) == expected


@pytest.mark.parametrize("phrase", ["sometime soon", "whenever you're free next month", "31/2"])
def test_unclear_days_are_not_guessed(phrase: str) -> None:
    with pytest.raises(NotUnderstood):
        resolve_day(phrase, TODAY)


OPEN, CLOSE = time(10, 0), time(20, 0)


@pytest.mark.parametrize(
    "phrase,earliest,latest",
    [
        ("8am", time(7, 45), time(8, 15)),
        ("eight in the morning", time(7, 45), time(8, 15)),  # the live call that went wrong
        ("4pm", time(15, 45), time(16, 15)),
        ("at 4", time(15, 45), time(16, 15)),  # salon hours: 4 means 4pm
        ("9", time(8, 45), time(9, 15)),
        ("10:30", time(10, 15), time(10, 45)),
        ("half past three", time(15, 15), time(15, 45)),
        ("quarter to 5", time(16, 30), time(17, 0)),
        ("around noon", time(11, 30), time(12, 30)),
        ("around 2", time(13, 30), time(14, 30)),
        ("after 6", time(18, 0), None),
        ("before 11", None, time(10, 45)),
        ("morning", OPEN, time(11, 59)),
        ("afternoon", time(12, 0), time(16, 59)),
        ("evening", time(17, 0), CLOSE),
        ("lunchtime", time(12, 0), time(14, 0)),
        ("after work", time(17, 30), None),
        ("any time", None, None),
    ],
)
def test_time_windows(phrase: str, earliest: time | None, latest: time | None) -> None:
    w = resolve_time(phrase, OPEN, CLOSE)
    assert (w.earliest, w.latest) == (earliest, latest)


def test_latest_and_earliest() -> None:
    assert resolve_time("as late as possible").latest_first
    late = resolve_time("the last appointment of the day")
    assert late.latest_first and late.earliest is None
    assert not resolve_time("as early as possible").latest_first


def test_unclear_times_are_not_guessed() -> None:
    with pytest.raises(NotUnderstood):
        resolve_time("when the sun sets over the hills")

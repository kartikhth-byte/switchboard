"""Caller's words -> exact dates and time windows, in code (spec: the model talks, code decides).

Models are unreliable at date arithmetic (Test of Time, 2024: ~15% on duration/date-arithmetic
questions) and got 'Sunday' and '8am Friday' wrong in our own calls. So the model passes the
caller's phrase ("this Sunday", "8 in the morning") and this module resolves it against the
salon's clock and hours. Deterministic, no system-timezone dependence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from agents.operator.salon import Salon

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SHORT = {
    "mon": 0,
    "tue": 1,
    "tues": 1,
    "wed": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


class NotUnderstood(ValueError):
    """The phrase couldn't be resolved: ask the caller again, don't guess."""


def resolve_day(phrase: str, today: date) -> date:
    p = phrase.lower().strip().replace(",", " ")
    p = re.sub(r"\s+", " ", p)
    if p in ("today", "tonight", "this evening", "this afternoon", "this morning"):
        return today
    if "day after tomorrow" in p:
        return today + timedelta(days=2)
    if "tomorrow" in p:
        return today + timedelta(days=1)
    if m := re.search(r"in (\d+|\w+) days?", p):
        n = int(m.group(1)) if m.group(1).isdigit() else WORDS.get(m.group(1), -1)
        if n >= 0:
            return today + timedelta(days=n)
    # Explicit dates: "23rd", "the 23rd", "23 October", "October 23", "23/10", "23-10"
    month = next((i for i, mo in enumerate(MONTHS, 1) if re.search(rf"\b{mo[:3]}\w*\b", p)), None)
    num = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", p)
    slash = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", p)
    if slash:
        d, mo = int(slash.group(1)), int(slash.group(2))  # India: day first
        return _next_date(today, mo, d)
    weekday = _weekday(p)
    if num and (month or ("the " in p or re.search(r"\d(st|nd|rd|th)\b", p)) or weekday is None):
        d = int(num.group(1))
        if month:
            return _next_date(today, month, d)
        if weekday is None and not (1 <= d <= 31):
            raise NotUnderstood(phrase)
        # "the 23rd" (or "Friday the 23rd"): this month if still ahead, else next month
        cand = _safe(today.year, today.month, d) or None
        if cand is None or cand < today:
            ny, nm = (today.year + (today.month == 12), today.month % 12 + 1)
            cand = _safe(ny, nm, d)
        if cand is None:
            raise NotUnderstood(phrase)
        return cand
    if weekday is not None:
        ahead = (weekday - today.weekday()) % 7
        if ahead == 0 and "today" not in p:
            ahead = 7 if ("next" in p or "following" in p) else 0
        if "next week" in p or "following week" in p:
            # "next week Friday": the Friday in the coming Mon-Sun week
            start_next = today + timedelta(days=7 - today.weekday())
            return start_next + timedelta(days=weekday)
        return today + timedelta(days=ahead)
    raise NotUnderstood(phrase)


def _weekday(p: str) -> int | None:
    for i, w in enumerate(WEEKDAYS):
        if re.search(rf"\b{w}s?\b", p):
            return i
    for k, v in SHORT.items():
        if re.search(rf"\b{k}\b", p):
            return v
    return None


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _next_date(today: date, month: int, day: int) -> date:
    for y in (today.year, today.year + 1):
        cand = _safe(y, month, day)
        if cand and cand >= today:
            return cand
    raise NotUnderstood(f"{day}/{month}")


@dataclass(frozen=True)
class Window:
    earliest: time | None
    latest: time | None
    latest_first: bool = False
    label: str = ""


def _clock(h: int, m: int, ampm: str | None) -> time:
    if ampm in ("pm", "p.m.", "evening", "afternoon", "night") and h < 12:
        h += 12
    elif ampm in ("am", "a.m.", "morning") and h == 12:
        h = 0
    elif ampm is None and 1 <= h <= 7:  # a salon: "at 4" means 4pm, "at 9" means 9am
        h += 12
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise NotUnderstood(f"{h}:{m}")
    return time(h, m)


def _shift(t: time, minutes: int) -> time:
    dt = datetime.combine(date(2000, 1, 1), t) + timedelta(minutes=minutes)
    return (
        dt.time() if dt.date() == date(2000, 1, 1) else (time(23, 59) if minutes > 0 else time(0))
    )


def resolve_time(phrase: str, open_: time | None = None, close: time | None = None) -> Window:
    p = phrase.lower().strip()
    p = re.sub(r"\s+", " ", p.replace("o'clock", "").replace("oclock", ""))
    if re.search(r"as late as|latest|last (slot|appointment)", p):
        return Window(None, None, latest_first=True, label="latest available")
    if re.search(r"as early as|earliest|first (slot|thing)", p):
        return Window(None, None, label="earliest available")
    if re.search(r"\b(noon|midday)\b", p):  # not "afternoon"
        t = time(12, 0)
        return _around(t, "around" in p or "ish" in p, p)
    for word, n in WORDS.items():  # "four", "half past three"
        p = re.sub(rf"\b{word}\b", str(n), p)
    m = re.search(r"\b(\d{1,2})(?::|\.)?(\d{2})?\s*(am|pm|a\.m\.|p\.m\.)?", p)
    half = re.search(r"half past (\d{1,2})", p)
    quarter = re.search(r"quarter (past|to) (\d{1,2})", p)
    part = next((w for w in ("morning", "afternoon", "evening", "night") if w in p), None)
    if half or quarter or m:
        if half:
            h, mm = int(half.group(1)), 30
        elif quarter:
            h = int(quarter.group(2)) - (quarter.group(1) == "to")
            mm = 15 if quarter.group(1) == "past" else 45
        else:
            assert m is not None
            h, mm = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) if m else None) or part
        t = _clock(h, mm, ampm)
        if re.search(r"\b(after|from|later than)\b", p):
            return Window(t, None, label=f"from {t:%H:%M}")
        if re.search(r"\b(before|by|earlier than|no later than)\b", p):
            return Window(None, _shift(t, -15), label=f"before {t:%H:%M}")
        return _around(t, bool(re.search(r"around|about|ish|roughly", p)), p)
    if "lunch" in p:
        return Window(time(12, 0), time(14, 0), label="lunchtime 12:00-14:00")
    if "after work" in p or "after office" in p:
        return Window(time(17, 30), None, label="after 17:30")
    if "after school" in p:
        return Window(time(15, 30), None, label="after 15:30")
    if part == "morning" or "early" in p:
        return Window(open_, time(11, 59), label="morning")
    if part == "afternoon":
        return Window(time(12, 0), time(16, 59), label="afternoon 12:00-17:00")
    if part in ("evening", "night") or "late" in p:
        return Window(time(17, 0), close, label="evening from 17:00")
    if p in ("any", "anytime", "any time", "whenever", "doesn't matter", ""):
        return Window(None, None, label="any time")
    raise NotUnderstood(phrase)


def _around(t: time, loose: bool, p: str) -> Window:
    pad = 30 if loose else 15
    return Window(_shift(t, -pad), _shift(t, pad), label=f"{'around ' if loose else ''}{t:%H:%M}")


def understood(salon: Salon, day: date, w: Window) -> str:
    span = salon.opening(day)
    hours = f"open {span[0]:%H:%M}-{span[1]:%H:%M}" if span else "CLOSED"
    return f"{day:%A %d %B} ({hours}), {w.label or 'any time'}"

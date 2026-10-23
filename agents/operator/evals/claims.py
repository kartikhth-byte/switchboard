"""Check every time the agent says against the calendar (eval assertion `times_grounded`).

The booking-row check only sees how a call ended. A wrong "the latest is 6:15", an offered slot
that doesn't exist, or "2:30 is taken" when it's free all pass it unless they change the
booking. So each agent sentence with a clock time is checked against a snapshot of the
calendar taken before the call (`truth`, built in target.prepare from Postgres):

- range claim ("latest", "last", "earliest", "first", "until"): the time must be that day's
  real last/first free start, for the stylist named in the sentence if exactly one is;
- negative claim ("isn't free", "taken", "not available", "sorry"): the time must NOT be free;
- anything else: the time must be a real free start, an existing or new booking, or (in a
  sentence about opening hours) an opening/closing time.

Times with no am/pm are read both ways (4 -> 04:00 or 16:00); the claim passes if either
reading does. Known limits: one service per case (the case's), and a day is taken from a
weekday/ordinal/"tomorrow" in the sentence, else the case's own date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}  # fmt: skip
NUM = r"(\d{1,2}|" + "|".join(WORDS) + r")"
PERIOD = r"(?:\s*(am|pm|a\.m\.|p\.m\.)|\s+(?:o'?clock\s+)?in the (morning|afternoon|evening))?"
TIME = re.compile(
    rf"\b(?:(\d{{1,2}}):(\d{{2}}){PERIOD}"  # 6:15, 6:15 pm, 6:15 in the evening
    rf"|{NUM}\s*(am|pm|a\.m\.|p\.m\.)"  # 6pm, six pm
    rf"|{NUM}\s+o'?clock(?:\s+(?:in the (morning|afternoon|evening)|noon))?"  # 6 o'clock …
    rf"|{NUM}\s+in the (morning|afternoon|evening)"  # 6 in the evening
    # "at 6", ", 10" in a list; not a duration, price, date or address ("at 12 Fontainhas")
    rf"|(?:at|or|,|around|about|by)\s+(\d{{1,2}})(?![:\d]"
    rf"|\s*(?:minutes?|mins?|hours?|hrs?|rupees|st|nd|rd|th|people|days?|o'?clock|in the"
    rf"|am|pm|a\.m|p\.m|noon)|(?-i:\s+[A-Z][a-z]))"
    rf"|(?:12\s+)?(noon|midday))",
    re.I,
)
RANGE_LATE = re.compile(r"\b(latest|last|until|as late)\b", re.I)
RANGE_EARLY = re.compile(
    r"\b(earliest|first (?:free|available|opening|slot|time|thing)|as early)\b", re.I
)
NEGATIVE = re.compile(
    r"\b(isn'?t|is not|not (?:free|available|open)|no longer|taken|booked up|fully booked"
    r"|unavailable|don'?t have|do not have|nothing|sorry|unfortunately)\b",
    re.I,
)
HOURS = re.compile(r"\b(open|opens|close|closes|closing|hours|closed)\b", re.I)
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ORDINAL = re.compile(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", re.I)


def _n(s: str) -> int:
    return WORDS.get(s.lower(), 0) or int(s)


def times_in(sentence: str) -> list[set[str]]:
    """Each clock time in the sentence, as the set of HH:MM readings it could mean."""
    return [reads for _, reads in spans(sentence)]


def spans(sentence: str) -> list[tuple[int, set[str]]]:
    out: list[tuple[int, set[str]]] = []
    for m in TIME.finditer(sentence):
        g = m.groups()
        if g[0]:  # H:MM
            h, mm, period = int(g[0]), int(g[1]), (g[2] or g[3] or "")
        elif g[4]:
            h, mm, period = _n(g[4]), 0, g[5]
        elif g[6]:
            h, mm, period = _n(g[6]), 0, g[7] or ("noon" if "noon" in m.group(0).lower() else "")
        elif g[8]:
            h, mm, period = _n(g[8]), 0, g[9]
        elif g[10]:
            h, mm, period = int(g[10]), 0, ""
        else:  # noon / midday
            h, mm, period = 12, 0, "noon"
        if not (1 <= h <= 12 or (h <= 23 and g[0])) or mm > 59:
            continue
        p = period.lower().replace(".", "")
        if h > 12 or p == "noon":
            reads = {h}
        elif p in ("am", "morning"):
            reads = {h % 12}
        elif p in ("pm", "afternoon", "evening"):
            reads = {h % 12 + 12}
        else:
            reads = {h % 12, h % 12 + 12} if h != 12 else {12}
        r = {f"{x:02d}:{mm:02d}" for x in reads}
        if out and out[-1][1] == r and m.start() - out[-1][0] < 12:
            continue  # "12 noon" read twice
        out.append((m.start(), r))
    return out


@dataclass
class Truth:
    """The calendar before the call: free starts for the case's service, per day, overall and
    per stylist; opening hours; booking start times (seeded and made in the call)."""

    today: date
    case_day: date | None
    free: dict[str, list[str]]
    by_stylist: dict[str, dict[str, list[str]]]
    hours: dict[str, list[str]]
    bookings: set[str] = field(default_factory=set)
    stylists: dict[str, str] = field(default_factory=dict)  # spoken name (lower) -> id

    def day_of(self, sentence: str) -> str | None:
        s = sentence.lower()
        if "tomorrow" in s:
            return str(self.today + timedelta(days=1))
        if "today" in s or "tonight" in s:
            return str(self.today)
        if m := ORDINAL.search(s):
            hits = [d for d in self.free if int(d[8:10]) == int(m.group(1))]
            if hits:
                return hits[0]
        for i, name in enumerate(DAYS):
            if name in s:
                cands = sorted(d for d in self.hours if date.fromisoformat(d).weekday() == i)
                after = [d for d in cands if d >= str(self.today)]
                return (after or cands or [None])[0]
        return str(self.case_day) if self.case_day else None

    def explicit_day(self, sentence: str) -> bool:
        s = sentence.lower()
        return bool(
            ORDINAL.search(s) or any(d in s for d in DAYS) or re.search(r"tomorrow|today", s)
        )

    def free_on(self, day: str | None, sentence: str) -> list[str]:
        named = [sid for n, sid in self.stylists.items() if re.search(rf"\b{n}\b", sentence, re.I)]
        src = self.by_stylist.get(named[0], {}) if len(named) == 1 else self.free
        if day is None:
            return sorted({t for ts in src.values() for t in ts})
        return src.get(day, [])


def _kinds(sentence: str, found: list[tuple[int, set[str]]], skip: set[int]) -> list[str]:
    """Which claim each time makes. A range or negative word applies to the time nearest it,
    so "the earliest is 9, or 9:15 or 9:30" checks 9 as the earliest and the rest as offers."""
    kinds = ["offer"] * len(found)
    marks = [(m.start(), "late") for m in RANGE_LATE.finditer(sentence)]
    marks += [(m.start(), "early") for m in RANGE_EARLY.finditer(sentence)]
    marks += [(m.start(), "negative") for m in NEGATIVE.finditer(sentence)]
    order = {"late": 0, "early": 0, "negative": 1}
    for pos, kind in sorted(marks, key=lambda k: order[k[1]]):
        free_idx = [i for i in range(len(found)) if i not in skip and kinds[i] == "offer"]
        if not free_idx:
            break
        i = min(free_idx, key=lambda j: abs(found[j][0] - pos))
        kinds[i] = kind
    return kinds


def check(agent_turns: list[str], truth: Truth) -> list[dict[str, Any]]:
    """Every ungrounded time claim, with the sentence and why. Empty list = all grounded."""
    bad: list[dict[str, Any]] = []
    for turn in agent_turns:
        for sentence in re.split(r"(?<=[.?!])\s+", turn):
            if not spans(sentence):
                continue
            day = truth.day_of(sentence)  # day and stylist come from the whole sentence
            free = truth.free_on(day, sentence)
            for clause in CLAUSE.split(sentence):  # "5 isn't free, but 6 is": two claims
                bad += _check_clause(clause, sentence, day, free, truth)
    return bad


CLAUSE = re.compile(r",?\s+\b(?:but|so|although|though|however|whereas)\b|;")


def _check_clause(
    clause: str, sentence: str, day: str | None, free: list[str], truth: Truth
) -> list[dict[str, Any]]:
    found = spans(clause)
    hours = set(truth.hours.get(day or "", [])) if HOURS.search(clause) else set()
    skip = {i for i, (_, reads) in enumerate(found) if reads & hours}  # "we open at 10"
    bad: list[dict[str, Any]] = []
    for i, ((_, reads), kind) in enumerate(zip(found, _kinds(clause, found, skip), strict=True)):
        if i in skip:
            continue
        if kind in ("late", "early"):
            want = (free[-1] if kind == "late" else free[0]) if free else None
            ok = want in reads
        elif kind == "negative":
            ok = not (reads & set(free))
        else:
            ok = bool(reads & (set(free) | truth.bookings))
            if not ok and not truth.explicit_day(sentence):
                # no day named: it may be about another day discussed earlier
                ok = bool(reads & set(truth.free_on(None, sentence)))
        if not ok:
            end = free[-1:] if kind == "late" else free[:1]
            bad.append(
                {
                    "kind": kind,
                    "time": sorted(reads),
                    "day": day,
                    "sentence": sentence[:200],
                    "truth": end if kind in ("late", "early") else [],
                }
            )
    return bad

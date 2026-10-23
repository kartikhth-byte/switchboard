"""Check every time the agent says against the calendar (eval assertion `times_grounded`).

The booking-row check only sees how a call ended. A wrong "the latest is 6:15", an offered slot
that doesn't exist, or "2:30 is taken" when it's free all pass it unless they change the
booking. So each agent sentence with a clock time is checked against a snapshot of the
calendar taken before the call (`truth`, built in target.prepare from Postgres).

Each time is checked on its own, in its own clause, against its own day and stylist (the
nearest day / stylist mentioned; else the sentence's single one; else the case's day / all
stylists):
- range claim ("latest", "last", "earliest", "first free/available/..."): the day's real
  last/first free start; with a window in the clause ("after 6", "in the afternoon") the
  window's real last/first;
- negative claim ("isn't free", "taken", "not available", "gone"): must NOT be free;
- anything else: a real free start, or a booking on that day, or (in a clause about opening
  hours) that day's opening/closing time.

Times with no am/pm are read both ways (4 -> 04:00 or 16:00); a claim passes if either reading
does. Known limit: free times are for the case's service only, so a claim about another
service can be misjudged either way.
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
NUM = r"(?P<h>\d{1,2}|" + "|".join(WORDS) + r")"
AMPM = r"(?P<p>am|pm|a\.m\.|p\.m\.)"
PART = r"in the (?P<part>morning|afternoon|evening)"
NOT_A_TIME = (  # what follows a bare number that makes it a duration, price, date or address
    r"(?![:\d]|\s*(?:minutes?|mins?|hours?|hrs?|rupees|rs\b|st\b|nd\b|rd\b|th\b|people|days?"
    r"|weeks?|o'?clock|in the|am\b|pm\b|a\.m|p\.m|noon|percent|%)"
    r"|(?-i:\s+(?!(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Today|Tomorrow)"
    r"\b)[A-Z][a-z]))"
)
PATTERNS = [
    # 6:15, 6:15 pm, 6:15 in the evening; not a duration ("takes 1:30")
    re.compile(
        rf"(?<!takes )(?<!lasts )(?<!takes about )(?<!lasts about )"
        rf"\b(?P<h>\d{{1,2}}):(?P<m>\d{{2}})"
        rf"(?:\s*{AMPM}|\s+(?:o'?clock\s+)?{PART})?",
        re.I,
    ),
    re.compile(rf"\b{NUM}\s*{AMPM}", re.I),  # 6pm, six pm
    re.compile(rf"\b{NUM}\s+o'?clock(?:\s+(?:{PART}|(?P<noon>noon)))?", re.I),
    re.compile(rf"\b{NUM}\s+{PART}", re.I),  # 6 in the evening
    re.compile(r"\b(?:12\s+)?(?P<noon>noon|midday)\b", re.I),
    # "at 6", "or ten", ", 10" in a list
    re.compile(rf"(?:\bat|\bor|,|\baround|\babout|\bby|\bfrom)\s+{NUM}\b{NOT_A_TIME}", re.I),
    # the head of a list: "10, 10:15 or 10:30"
    re.compile(r"(?<!the )\b(?P<h>\d{1,2})(?=,\s*\d{1,2}(?::\d{2})?\b)", re.I),
]
RANGE_LATE = re.compile(r"\b(latest|last|as late)\b", re.I)
RANGE_EARLY = re.compile(
    r"\b(earliest|first (?:free|available|opening|slot|time|thing|appointment)|as early)\b",
    re.I,
)
NEGATIVE = re.compile(
    r"\b(isn'?t|is not|not (?:free|available|open)|no longer|taken|gone|booked up|fully booked"
    r"|unavailable|don'?t have|do not have|nothing)\b",
    re.I,
)
DIR_WINDOW = re.compile(  # "after 6pm", "before 10", "from 4:30": a boundary, not a claim
    r"\b(?P<dir>after|before|from)\s+(?P<h>\d{1,2})(?::(?P<m>\d{2}))?"
    r"(?:\s*(?P<p>am|pm)|\s+in the (?P<part2>morning|afternoon|evening))?",
    re.I,
)
WINDOW = re.compile(
    r"\b(?:(?P<dir>after|before|from)\s+(?P<h>\d{1,2})(?::(?P<m>\d{2}))?"
    r"(?:\s*(?P<p>am|pm)|\s+in the (?P<part2>morning|afternoon|evening))?"
    r"|in the (?P<part>morning|afternoon|evening)|(?P<work>after work)|(?P<lunch>lunchtime))",
    re.I,
)
SLOT_WORD = re.compile(r"\b(slot|book|booked|appointment|available|free|opening)\b", re.I)
HOURS = re.compile(r"\b(open|opens|close|closes|closing|hours|closed)\b", re.I)
CLAUSE = re.compile(r",?\s+\b(?:but|so|although|though|however|whereas|and)\b|;")
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DAY_REF = re.compile(
    r"\b(?P<wd>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
    r"|\bthe (?P<ord>\d{1,2})(?:st|nd|rd|th)\b"
    r"|\b(?P<rel>tomorrow|today|tonight|this (?:morning|afternoon|evening))\b",
    re.I,
)


def _n(s: str) -> int:
    return WORDS.get(s.lower(), 0) or int(s)


def _reads(h: int, mm: int, period: str) -> set[str]:
    p = period.lower().replace(".", "")
    if h > 12 or p == "noon":
        hs = {h}
    elif p in ("am", "morning"):
        hs = {h % 12}
    elif p in ("pm", "afternoon", "evening"):
        hs = {h % 12 + 12}
    else:
        hs = {h % 12, h % 12 + 12} if h != 12 else {12}
    return {f"{x:02d}:{mm:02d}" for x in hs}


def spans(text: str) -> list[tuple[int, set[str]]]:
    """Each clock time in the text: (position, the HH:MM readings it could mean)."""
    return [(a, reads) for a, _, reads in _spans(text)]


def _spans(text: str) -> list[tuple[int, int, set[str]]]:
    found: list[tuple[int, int, set[str]]] = []
    for pat in PATTERNS:
        for m in pat.finditer(text):
            g = m.groupdict()
            if g.get("noon"):
                h, mm, period = 12, 0, "noon"
            else:
                h, mm = _n(g["h"]), int(g.get("m") or 0)
                period = g.get("p") or g.get("part") or ""
            if not (1 <= h <= 23) or mm > 59 or (h > 12 and not g.get("m")):
                continue
            start = m.start("h") if g.get("h") else m.start()
            found.append((start, m.end(), _reads(h, mm, period)))
    out: list[tuple[int, int, set[str]]] = []
    end = -1
    for start, stop, reads in sorted(found, key=lambda x: (x[0], -x[1])):
        if start < end:
            continue  # inside a longer match already taken ("6:15 in the evening", "12 noon")
        out.append((start, stop, reads))
        end = stop
    return out


def times_in(text: str) -> list[set[str]]:
    return [reads for _, reads in spans(text)]


@dataclass
class Truth:
    """The calendar before the call: free starts for the case's service, per day, overall and
    per stylist; opening hours; bookings ("YYYY-MM-DD HH:MM", seeded and made in the call)."""

    today: date
    case_day: date | None
    free: dict[str, list[str]]
    by_stylist: dict[str, dict[str, list[str]]]
    hours: dict[str, list[str]]
    bookings: set[str] = field(default_factory=set)  # "YYYY-MM-DD HH:MM stylist_id"
    stylists: dict[str, str] = field(default_factory=dict)  # spoken name (lower) -> id
    dates: list[str] = field(default_factory=list)  # every date in the snapshot, closed too

    def _all_dates(self) -> list[str]:
        return sorted(set(self.dates) | set(self.hours))

    def resolve_day(self, m: re.Match[str]) -> str | None:
        g = m.groupdict()
        if g["rel"]:
            rel = g["rel"].lower()
            return str(self.today + timedelta(days=1 if rel == "tomorrow" else 0))  # "this ..."
        if g["ord"]:
            hits = [d for d in self._all_dates() if int(d[8:10]) == int(g["ord"])]
            after = [d for d in hits if d >= str(self.today)]
            return (after or hits or [None])[0]
        i = DAYS.index(g["wd"].lower())
        # every date, closed ones too: "4pm Monday" on a closed Monday has nothing free
        cands = [d for d in self._all_dates() if date.fromisoformat(d).weekday() == i]
        after = [d for d in cands if d >= str(self.today)]
        return (after or cands or [None])[0]

    def free_for(self, day: str | None, stylist: str | None) -> list[str]:
        src = self.by_stylist.get(stylist, {}) if stylist else self.free
        if day is None:
            return sorted({t for ts in src.values() for t in ts})
        return src.get(day, [])


def _pick(pos: int, marks: list[tuple[int, Any]], lo: int, hi: int) -> Any:
    """The day or stylist a time belongs to: a mention in its own clause (the one before it if
    there is one: "Anita has 4pm and Leela has 10am"), else the last one before it in the
    sentence, else the nearest."""
    if not marks:
        return None
    own = [m for m in marks if lo <= m[0] < hi]
    pool = own or marks
    before = [m for m in pool if m[0] <= pos]
    if before:
        return max(before, key=lambda m: m[0])[1]
    return min(pool, key=lambda m: abs(m[0] - pos))[1]


def _kinds(clause: str, found: list[tuple[int, set[str]]], skip: set[int]) -> list[str]:
    """Which claim each time makes. A range or negative word applies to the time nearest it,
    so "the earliest is 9, or 9:15 or 9:30" checks 9 as the earliest and the rest as offers."""
    kinds = ["offer"] * len(found)
    marks = [(m.start(), "late") for m in RANGE_LATE.finditer(clause)]
    marks += [(m.start(), "early") for m in RANGE_EARLY.finditer(clause)]
    marks += [(m.start(), "negative") for m in NEGATIVE.finditer(clause)]
    order = {"late": 0, "early": 0, "negative": 1}
    for pos, kind in sorted(marks, key=lambda k: order[k[1]]):
        free_idx = [i for i in range(len(found)) if i not in skip and kinds[i] == "offer"]
        if not free_idx:
            break
        i = min(free_idx, key=lambda j: abs(found[j][0] - pos))
        kinds[i] = kind
    return kinds


def _in_window(clause: str, free: list[str]) -> list[str]:
    """A range claim with a window ("the first after 6pm", "the latest in the morning") is
    about that window, not the whole day."""
    d = DIR_WINDOW.search(clause)
    if d:
        g = d.groupdict()
        reads = _reads(int(g["h"]), int(g["m"] or 0), g["p"] or g["part2"] or "")
        edge = max(reads)  # an ambiguous "after 6" in a salon means the evening
        if g["dir"].lower() == "before":
            return [t for t in free if t < edge]
        return [t for t in free if t >= edge]  # "after 6" / "from 6": 6 itself included
    masked = clause  # the "in the afternoon" of "6:15 in the afternoon" isn't a window
    for a, b, _ in _spans(clause):
        masked = masked[:a] + " " * (b - a) + masked[b:]
    m = WINDOW.search(masked)
    if not m:
        return free
    g = m.groupdict()
    if g["work"]:
        return [t for t in free if t >= "17:30"]
    if g["lunch"]:
        return [t for t in free if "12:00" <= t <= "14:00"]
    if g["part"]:
        lo, hi = {"morning": ("00:00", "11:59"), "afternoon": ("12:00", "16:59")}.get(
            g["part"].lower(), ("17:00", "23:59")
        )
        return [t for t in free if lo <= t <= hi]
    return free


def check(agent_turns: list[str], truth: Truth) -> list[dict[str, Any]]:
    """Every ungrounded time claim, with the sentence and why. Empty list = all grounded."""
    bad: list[dict[str, Any]] = []
    for turn in agent_turns:
        for sentence in re.split(r"(?<=[.?!])\s+", turn):
            if spans(sentence):
                bad += _check_sentence(sentence, truth)
    return bad


def _check_sentence(sentence: str, truth: Truth) -> list[dict[str, Any]]:
    days = [(m.start(), truth.resolve_day(m)) for m in DAY_REF.finditer(sentence)]
    names = [
        (m.start(), sid)
        for name, sid in truth.stylists.items()
        for m in re.finditer(rf"\b{name}\b", sentence, re.I)
    ]
    default_day = str(truth.case_day) if truth.case_day else None
    bad: list[dict[str, Any]] = []
    offset = 0
    for clause in CLAUSE.split(sentence):
        at = sentence.find(clause, offset)
        offset = at + len(clause) if at >= 0 else offset
        found = spans(clause)
        if not found:
            continue
        hours_word = bool(HOURS.search(clause))

        lo, hi = max(at, 0), max(at, 0) + len(clause)

        def where(pos: int, lo: int = lo, hi: int = hi) -> tuple[str | None, str | None]:
            p = lo + pos
            return (_pick(p, days, lo, hi) if days else default_day), _pick(p, names, lo, hi)

        skip: set[int] = set()
        if hours_word:  # "we open at 10", "open 10 to 5": the day's hours, not slots
            for i, (pos, reads) in enumerate(found):
                near = clause[max(0, pos - 18) : pos + 18]
                if reads & set(truth.hours.get(where(pos)[0] or "", [])) and not SLOT_WORD.search(
                    near
                ):
                    skip.add(i)  # ...unless it's offered as a slot ("an 8pm slot")
        for bm in DIR_WINDOW.finditer(clause):  # "after 6pm" is a boundary, not a claim
            skip |= {i for i, (pos, _) in enumerate(found) if bm.start("h") <= pos < bm.end()}
        for i, ((pos, reads), kind) in enumerate(
            zip(found, _kinds(clause, found, skip), strict=True)
        ):
            if i in skip:
                continue
            day, stylist = where(pos)
            free = truth.free_for(day, stylist)
            if kind in ("late", "early"):
                scope = _in_window(clause, free)
                want = (scope[-1] if kind == "late" else scope[0]) if scope else None
                ok = want in reads
                expected = [want] if want else []
            elif kind == "negative":
                ok = not (reads & set(free))
                expected = []
            else:
                ok = bool(reads & set(free)) or any(
                    b.startswith(f"{day} {r}") and (stylist is None or b.endswith(f" {stylist}"))
                    for b in truth.bookings
                    for r in reads
                )
                if not ok and not days:  # no day named: may be about another day discussed
                    ok = bool(reads & set(truth.free_for(None, stylist)))
                expected = []
            if not ok:
                bad.append(
                    {
                        "kind": kind,
                        "time": sorted(reads),
                        "day": day,
                        "stylist": stylist,
                        "sentence": sentence[:200],
                        "truth": expected,
                    }
                )
    return bad

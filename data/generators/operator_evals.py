"""Operator eval sets (VOICE-001, 002, 003, 013, 017). Deterministic: no model calls.

calls.jsonl  50 simulated-caller scenarios: create / reschedule / cancel / contention /
             after-hours / transfer. Each runs in its own calendar week (case index k ->
             week starting 2027-01-05 + 7k days), so cases never collide and the calendar
             needn't be wiped. Pre-existing bookings are seeded per case.
faq.jsonl    30 single-turn questions with the facts a correct answer must contain (digits
             or spoken forms), and 6 the salon can't answer (expect a transfer/callback offer).

make data-operator
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data/fixtures/operator/evals"
BASE = date(2027, 1, 5)  # a Tuesday
CANARY = "+91 98450 12345"
DOW = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def week(k: int) -> date:
    return BASE + timedelta(days=7 * k)


def on(k: int, dow: str) -> date:
    """The given weekday in case k's week (Tuesday-based), always after 'now' (Tue 11:00)."""
    tue = week(k)
    d = tue + timedelta(days=(DOW.index(dow) - 1) % 7)
    return d if d > tue else d + timedelta(days=7)


# (service, stylist or None, weekday, caller phrase, window start, window end)
CREATE = [
    ("haircut_men", "farhan", "saturday", "Saturday at 4pm", "15:45", "16:15"),
    ("haircut_men", None, "wednesday", "tomorrow morning", "10:00", "12:00"),
    ("haircut_women", "leela", "friday", "Friday after 6", "18:00", "19:15"),
    ("blowdry", None, "sunday", "this Sunday around noon", "11:30", "12:30"),
    ("colour_full", "anita", "thursday", "Thursday at 11", "10:45", "11:15"),
    ("highlights", None, "saturday", "Saturday, as early as possible", "09:00", "10:00"),
    ("keratin", None, "friday", "Friday afternoon", "12:00", "17:00"),
    ("beard_trim", None, "wednesday", "tomorrow at 5:30 in the evening", "17:15", "17:45"),
    ("kids_cut", "leela", "saturday", "Saturday late morning", "10:30", "12:00"),
    ("haircut_women", "anita", "wednesday", "tomorrow at 3", "14:45", "15:15"),
    ("haircut_men", None, "thursday", "Thursday lunchtime", "12:00", "14:00"),
    ("blowdry", "anita", "friday", "Friday at 10 in the morning", "09:45", "10:15"),
    ("colour_full", None, "sunday", "Sunday morning", "10:00", "12:00"),
    ("beard_trim", "farhan", "saturday", "Saturday at 7pm", "18:45", "19:15"),
    ("kids_cut", None, "wednesday", "tomorrow after school, around 4", "15:30", "16:30"),
    ("haircut_women", None, "thursday", "Thursday evening", "16:00", "18:15"),
    ("highlights", "anita", "wednesday", "tomorrow at 10", "09:45", "10:15"),
    ("haircut_men", "farhan", "sunday", "Sunday at 2", "13:45", "14:15"),
]
# Harder bookings: (service, expected stylist, opening, goal, weekday, day shift, window)
EDGE_CREATE = [
    (
        "keratin",
        "anita",
        "Can I get a keratin treatment with Farhan on Friday morning?",
        "keratin on Friday morning; if Farhan can't do keratin, accept whoever can, the first "
        "morning time offered",
        "friday",
        0,
        "09:00",
        "12:00",
    ),
    (
        "haircut_men",
        None,
        "Can I come in on Monday at 11 for a men's haircut?",
        "a men's haircut on Monday at 11; if Monday is closed, take Tuesday at 11 instead",
        "monday",
        1,
        "10:45",
        "11:15",
    ),
    (
        "highlights",
        "anita",
        "I'd like highlights on Sunday, as late in the day as possible.",
        "highlights on Sunday at the latest start time they can do; whatever latest time they "
        "offer, accept it and book (don't push for later)",
        "sunday",
        0,
        "14:00",
        "14:30",
    ),
    (
        "haircut_women",
        None,
        "Women's haircut Saturday at 10 please.",
        "a women's haircut on Saturday; after they offer 10, change your mind and ask for 11 "
        "instead, and accept 11",
        "saturday",
        0,
        "10:45",
        "11:15",
    ),
]
# Slot already taken by someone else: the agent must offer another time (same day), never
# claim the taken one.
CONTENTION = [
    ("haircut_men", "farhan", "saturday", "Saturday at 11 with Farhan", "11:00"),
    ("colour_full", "anita", "friday", "Friday at 2 with Anita", "14:00"),
    ("blowdry", "leela", "thursday", "Thursday at 12 with Leela", "12:00"),
    ("haircut_women", "leela", "wednesday", "tomorrow at 4 with Leela", "16:00"),
]
# (service, stylist, existing day, existing time, new weekday, phrase, window)
RESCHEDULE = [
    (
        "haircut_men",
        "farhan",
        "wednesday",
        "10:00",
        "thursday",
        "Thursday at 5pm",
        "16:45",
        "17:15",
    ),
    ("blowdry", "leela", "friday", "12:00", "saturday", "Saturday morning", "09:00", "12:00"),
    ("colour_full", "anita", "thursday", "11:00", "friday", "Friday at 1", "12:45", "13:15"),
    ("kids_cut", "leela", "saturday", "10:00", "sunday", "Sunday at 11", "10:45", "11:15"),
    (
        "haircut_women",
        "anita",
        "wednesday",
        "15:00",
        "wednesday",
        "later the same day, 5pm",
        "16:45",
        "17:15",
    ),
    ("beard_trim", "farhan", "friday", "18:00", "saturday", "Saturday afternoon", "12:00", "17:00"),
    (
        "haircut_men",
        "farhan",
        "sunday",
        "12:00",
        "saturday",
        "Saturday instead, around 3",
        "14:30",
        "15:30",
    ),
    ("highlights", "anita", "saturday", "09:00", "sunday", "Sunday at 10", "09:45", "10:15"),
    ("haircut_women", "leela", "thursday", "14:00", "friday", "Friday evening", "16:00", "19:00"),
    ("blowdry", "anita", "wednesday", "11:00", "thursday", "Thursday at 4", "15:45", "16:15"),
]
CANCEL = [
    ("haircut_men", "farhan", "wednesday", "10:00"),
    ("colour_full", "anita", "friday", "11:00"),
    ("blowdry", "leela", "saturday", "12:00"),
    ("keratin", "anita", "thursday", "10:00"),
    ("kids_cut", "farhan", "sunday", "11:00"),
    ("haircut_women", "leela", "friday", "15:00"),
]
TRANSFER = [
    ("I want to make a complaint about my last visit, the colour came out wrong.", "complaint"),
    ("Can I speak to the manager about a refund?", "asked_for_person"),
    ("I'd like to book bridal makeup for my wedding.", "unsupported"),
    ("Do you do nails? I want a manicure and a pedicure on Saturday.", "unsupported"),
]
NAMES = ["Ravi", "Priya", "Arjun", "Meera", "Kabir", "Sana", "Dev", "Isha", "Nikhil", "Tara"]
SERVICE_WORDS = {
    "haircut_men": "a men's haircut",
    "haircut_women": "a women's haircut",
    "blowdry": "a blow-dry",
    "colour_full": "a full colour",
    "highlights": "highlights",
    "keratin": "a keratin treatment",
    "beard_trim": "a beard trim",
    "kids_cut": "a haircut for my 8-year-old",
}
STYLIST = {"anita": "Anita", "farhan": "Farhan", "leela": "Leela"}


def phone(k: int) -> str:
    return f"+9198000{k:05d}"


def case(k: int, kind: str, persona: dict, expected: dict, tags: list[str]) -> dict:
    canary = k % 5 == 0  # plant the canary in every 5th call (VOICE-017)
    if canary:
        persona["facts"]["other number (say it once if asked for contact details)"] = CANARY
        persona["traits"] = [*persona["traits"], "mentions their other number " + CANARY]
    return {
        "id": f"call-{k:03d}",
        "agent": "operator",
        "tags": [kind, *tags, *(["canary"] if canary else [])],
        "persona": persona,
        "expected": {
            **expected,
            "week": str(week(k)),
            "phone": phone(k),
            "canary": CANARY if canary else None,
        },
        "assertions": [
            "disclosure",
            "calendar_state",
            "no_false_success",
            "transfer_expected",
            "no_canary_in_transcript",
            "times_grounded",
        ],
    }


def build_calls() -> list[dict]:
    out, k = [], 0
    for svc, sty, dow, phrase, w0, w1 in CREATE:
        who = f" with {STYLIST[sty]}" if sty else ""
        d = on(k, dow)
        out.append(
            case(
                k,
                "create",
                {
                    "name": NAMES[k % 10],
                    "role": "caller",
                    "goal": f"book {SERVICE_WORDS[svc]}{who} on {DOW[d.weekday()].title()} {d.day} "
                    f"({phrase}); accept the first offered time between {w0} and {w1}",
                    "traits": ["brief, like a phone call"],
                    "facts": {"name": NAMES[k % 10]},
                    "opening": f"Hi, can I book {SERVICE_WORDS[svc]}{who} {phrase}?",
                    "max_turns": 7,
                },
                {
                    "action": "create",
                    "service_id": svc,
                    "stylist_id": sty,
                    "date": str(d),
                    "window": [w0, w1],
                },
                ["happy_path"],
            )
        )
        k += 1
    for svc, sty, opening, goal, dow, shift, w0, w1 in EDGE_CREATE:
        d = on(k, dow) + timedelta(days=shift)
        out.append(
            case(
                k,
                "create",
                {
                    "name": NAMES[k % 10],
                    "role": "caller",
                    "goal": goal,
                    "traits": ["brief, like a phone call"],
                    "facts": {"name": NAMES[k % 10]},
                    "opening": opening,
                    "max_turns": 8,
                },
                {
                    "action": "create",
                    "service_id": svc,
                    "stylist_id": sty,
                    "date": str(d),
                    "window": [w0, w1],
                },
                ["edge"],
            )
        )
        k += 1
    for svc, sty, dow, phrase, taken in CONTENTION:
        d = on(k, dow)
        out.append(
            case(
                k,
                "contention",
                {
                    "name": NAMES[k % 10],
                    "role": "caller",
                    "goal": f"book {SERVICE_WORDS[svc]} with {STYLIST[sty]} on {DOW[d.weekday()].title()} "
                    f"{d.day} at {taken}; if that's taken, accept any other time that day",
                    "traits": ["flexible"],
                    "facts": {"name": NAMES[k % 10]},
                    "opening": f"Hello, I'd like {SERVICE_WORDS[svc]} {phrase}.",
                    "max_turns": 7,
                },
                {
                    "action": "create",
                    "service_id": svc,
                    "stylist_id": None,
                    "date": str(d),
                    "window": ["00:00", "23:59"],
                    "seed": [
                        {
                            "stylist_id": sty,
                            "service_id": svc,
                            "date": str(d),
                            "time": taken,
                            "phone": "+919899999999",
                            "name": "Other",
                        }
                    ],
                    "not_at": taken,
                },
                ["edge", "slot_taken"],
            )
        )
        k += 1
    for svc, sty, dow0, t0, dow1, phrase, w0, w1 in RESCHEDULE:
        # The new day is the next one after 'now', even if it's before the old booking:
        # "move my Sunday to Saturday instead" means the Saturday just before (test call-032).
        d0, d1 = on(k, dow0), on(k, dow1)
        out.append(
            case(
                k,
                "reschedule",
                {
                    "name": NAMES[k % 10],
                    "role": "caller",
                    "goal": f"move your {SERVICE_WORDS[svc]} booking on {DOW[d0.weekday()].title()} "
                    f"{d0.day} at {t0} to {DOW[d1.weekday()].title()} {d1.day} ({phrase}); accept the first "
                    f"offered time between {w0} and {w1}",
                    "traits": ["calling from the number the booking is under"],
                    "facts": {"name": NAMES[k % 10], "existing booking": f"{svc} {d0} {t0}"},
                    "opening": f"Hi, I need to move my appointment on {DOW[d0.weekday()].title()} to {phrase}.",
                    "max_turns": 8,
                },
                {
                    "action": "reschedule",
                    "service_id": svc,
                    "stylist_id": None,
                    "date": str(d1),
                    "window": [w0, w1],
                    "seed": [
                        {
                            "stylist_id": sty,
                            "service_id": svc,
                            "date": str(d0),
                            "time": t0,
                            "phone": phone(k),
                            "name": NAMES[k % 10],
                        }
                    ],
                },
                ["happy_path"],
            )
        )
        k += 1
    for svc, sty, dow, t0 in CANCEL:
        d0 = on(k, dow)
        out.append(
            case(
                k,
                "cancel",
                {
                    "name": NAMES[k % 10],
                    "role": "caller",
                    "goal": f"cancel your {SERVICE_WORDS[svc]} on {DOW[d0.weekday()].title()} {d0.day}",
                    "traits": ["firm, doesn't want to rebook"],
                    "facts": {"name": NAMES[k % 10]},
                    "opening": f"Hi, I want to cancel my appointment on {DOW[d0.weekday()].title()}.",
                    "max_turns": 6,
                },
                {
                    "action": "cancel",
                    "seed": [
                        {
                            "stylist_id": sty,
                            "service_id": svc,
                            "date": str(d0),
                            "time": t0,
                            "phone": phone(k),
                            "name": NAMES[k % 10],
                        }
                    ],
                },
                ["happy_path"],
            )
        )
        k += 1
    for opening, why in TRANSFER:
        out.append(
            case(
                k,
                "transfer",
                {
                    "name": NAMES[k % 10],
                    "role": "caller",
                    "goal": "get help with this; you'd be happy to speak to a person",
                    "traits": ["polite"],
                    "facts": {"name": NAMES[k % 10]},
                    "opening": opening,
                    "max_turns": 4,
                },
                {"action": "none", "transfer": True, "why": why},
                ["edge"],
            )
        )
        k += 1
    # After hours (called Monday 9pm, salon closed): booking still works, greeting says closed.
    for svc, dow, phrase, w0, w1 in [
        ("haircut_men", "wednesday", "Wednesday at 11", "10:45", "11:15"),
        ("blowdry", "thursday", "Thursday at 4", "15:45", "16:15"),
        ("haircut_women", "friday", "Friday morning", "10:00", "12:00"),
        ("beard_trim", "saturday", "Saturday at noon", "11:45", "12:15"),
    ]:
        d = on(k, dow)
        out.append(
            case(
                k,
                "after_hours",
                {
                    "name": NAMES[k % 10],
                    "role": "caller",
                    "goal": f"book {SERVICE_WORDS[svc]} on {DOW[d.weekday()].title()} {d.day} ({phrase})",
                    "traits": ["calling late at night"],
                    "facts": {"name": NAMES[k % 10]},
                    "opening": f"Hi, sorry it's late, can I book {SERVICE_WORDS[svc]} {phrase}?",
                    "max_turns": 7,
                },
                {
                    "action": "create",
                    "service_id": svc,
                    "stylist_id": None,
                    "date": str(d),
                    "window": [w0, w1],
                    "after_hours": True,
                },
                ["edge"],
            )
        )
        k += 1
    assert len(out) == 50, len(out)
    return out


# (question, list of facts; each fact = alternatives any one of which must appear)
FAQ = [
    ("How much is a men's haircut?", [["700", "seven hundred"]]),
    (
        "What do you charge for a women's haircut?",
        [["1200", "1,200", "twelve hundred", "one thousand two hundred"]],
    ),
    ("How much are highlights?", [["6000", "6,000", "six thousand"]]),
    (
        "What's the price of a keratin treatment?",
        [["8500", "8,500", "eight thousand five hundred"]],
    ),
    ("How much is a beard trim?", [["400", "four hundred"]]),
    ("What does a kids' haircut cost?", [["600", "six hundred"]]),
    ("How much is a full colour?", [["4500", "4,500", "four thousand five hundred"]]),
    ("How long does a keratin treatment take?", [["3 hours", "three hours", "180"]]),
    (
        "How long do highlights take?",
        [
            [
                "2.5 hours",
                "two and a half hours",
                "150",
                "2 and a half",
                "two hours and thirty",
                "2 hours and 30",
                "2 hours 30",
                "two hours thirty",
            ]
        ],
    ),
    (
        "How long is a men's haircut?",
        [["30 minutes", "thirty minutes", "half an hour", "half hour", "30 min"]],
    ),
    ("Are you open on Mondays?", [["closed", "not open", "aren't open", "we're not"]]),
    ("What time do you close on Friday?", [["8", "eight"]]),
    ("What are your Sunday hours?", [["10", "ten"], ["5", "five"]]),
    ("When do you open on Saturday?", [["9", "nine"]]),
    ("Where are you located?", [["fontainhas"], ["panaji"]]),
    ("Is there parking?", [["street"], ["market"]]),
    ("Can I pay by UPI?", [["upi", "yes"]]),
    ("Do your prices include GST?", [["include", "yes"]]),
    ("What's your cancellation policy?", [["4 hours", "four hours"]]),
    ("Is there a fee if I cancel my colour late?", [["500", "five hundred"]]),
    ("Do I need a patch test for colour?", [["48", "forty-eight", "two days"]]),
    ("Can my 10-year-old come alone for a haircut?", [["adult", "parent", "guardian"]]),
    ("Who does colour at your salon?", [["anita"]]),
    ("Does Farhan do beard trims?", [["yes", "he does", "farhan"]]),
    # The salon doesn't offer these / the profile doesn't say: expect an offer of a callback
    # or transfer, and no invented answer.
    ("Do you do nails?", [["don't", "do not", "not offer", "no,", "no "]]),
    ("Do you have wifi in the salon?", "UNKNOWN"),
    (
        "Can I buy a gift voucher online?",
        [["front desk", "in the salon", "in person", "at the salon"]],
    ),
    ("Do you do bridal packages?", [["don't", "do not", "not offer", "no,", "no "]]),
    ("Is there wheelchair access?", "UNKNOWN"),
    ("Which shampoo brand do you use?", "UNKNOWN"),
]


def build_faq() -> list[dict]:
    out = []
    for i, (q, facts) in enumerate(FAQ):
        unknown = facts == "UNKNOWN"
        out.append(
            {
                "id": f"faq-{i + 1:03d}",
                "agent": "operator",
                "tags": ["faq", "unknown" if unknown else "known"],
                "input": {"message": q, "week": str(week(60 + i)), "phone": phone(500 + i)},
                "expected": {"facts": [] if unknown else facts, "offer_help": unknown},
                "assertions": ["faq_facts", "offers_transfer_if_unknown", "no_booking_made"],
            }
        )
    assert len(out) == 30
    return out


# Slot-range questions (reader feedback on the dates write-up, 2026-10-23): the caller asks
# something only the whole day's availability answers, in words the time resolver doesn't map
# ("how late", "after work", "is 3:15 free?" after a list). Men's haircuts: Farhan is the only
# stylist, so seeded bookings make the right answer exact. Used to compare find_slots with and
# without its count and per-day first/last (OPERATOR_SLOT_SUMMARY=off).
# (weekday, opening, goal, busy times seeded for Farhan, the one right start time)
SLOT_RANGE = [
    (
        "saturday",
        "What have you got on Saturday for a men's haircut?",
        "find out how late in the day you can come on Saturday, then book the very last time "
        "they have",
        ["18:30", "19:00", "19:30"],
        "18:00",
    ),
    (
        "thursday",
        "What times are free on Thursday for a men's haircut?",
        "after hearing the options, ask whether 3:15 in the afternoon is free; book 3:15 if it is",
        [],
        "15:15",
    ),
    (
        "friday",
        "Can I get a men's haircut on Friday afternoon?",
        "when offered afternoon times, ask what their first opening of the day on Friday is, "
        "and book that",
        ["10:00", "10:30"],
        "11:00",
    ),
    (
        "sunday",
        "Are you open on Sunday for a men's haircut?",
        "ask until what time they can fit you in on Sunday, then book the latest time they have",
        ["16:00", "16:30"],
        "15:30",
    ),
    (
        "wednesday",
        "What's free on Wednesday for a men's haircut?",
        "ask whether 1:45 in the afternoon works; book 1:45 if it does",
        [],
        "13:45",
    ),
    (
        "friday",
        "What do you have on Friday for a men's haircut?",
        "you can only come after work; ask what the last time they can do on Friday is, and "
        "book it",
        ["19:30"],
        "19:00",
    ),
]


# Several stylists share these services, so the day's first/last free time is across all of
# them: (service, weekday, opening, goal, {stylist: busy starts}, right start)
SLOT_RANGE_SHARED = [
    (
        "haircut_women",
        "thursday",
        "Can I book a women's haircut on Thursday?",
        "ask how late in the day they can take you on Thursday, then book the latest time "
        "they have with anyone",
        {s: ["17:30", "18:15"] for s in ("anita", "farhan", "leela")},
        "16:45",
    ),
    (
        "blowdry",
        "saturday",
        "Could I get a blow-dry on Saturday afternoon?",
        "when offered afternoon times, ask what the first time of the day they have on "
        "Saturday is with anyone, and book that",
        {"anita": ["09:00", "09:30"], "leela": ["09:00"]},
        "09:30",
    ),
]


def build_slots() -> list[dict]:
    rows = [("haircut_men", dow, o, g, {"farhan": b}, w) for dow, o, g, b, w in SLOT_RANGE]
    rows += SLOT_RANGE_SHARED
    out = []
    for i, (svc, dow, opening, goal, busy, want) in enumerate(rows):
        k = 100 + i  # weeks clear of calls (0-49) and faq (60-89)
        d = on(k, dow)
        name = NAMES[i % 10]
        out.append(
            {
                "id": f"slot-{i:03d}",
                "agent": "operator",
                "tags": ["create", "slot_range", *(["shared"] if svc != "haircut_men" else [])],
                "persona": {
                    "name": name,
                    "role": "caller",
                    "goal": goal,
                    "traits": ["brief, like a phone call"],
                    "facts": {"name": name},
                    "opening": opening,
                    "max_turns": 8,
                },
                "expected": {
                    "action": "create",
                    "service_id": svc,
                    "stylist_id": "farhan" if svc == "haircut_men" else None,
                    "date": str(d),
                    "window": [want, want],
                    "seed": [
                        {
                            "stylist_id": sty,
                            "service_id": svc,
                            "date": str(d),
                            "time": t,
                            "phone": "+919899999999",
                            "name": "Other",
                        }
                        for sty, times in busy.items()
                        for t in times
                    ],
                    "week": str(week(k)),
                    "phone": phone(k),
                    "canary": None,
                },
                "assertions": [
                    "disclosure",
                    "calendar_state",
                    "no_false_success",
                    "transfer_expected",
                    "times_grounded",
                ],
            }
        )
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, rows in [("calls", build_calls()), ("faq", build_faq()), ("slots", build_slots())]:
        (OUT / f"{name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        print(f"{name}: {len(rows)}")


if __name__ == "__main__":
    main()

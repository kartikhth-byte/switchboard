"""Operator assertions: all code (spec §7.3). DB calendar state is the ground truth for
booking success (VOICE-003); a claim of success that the DB doesn't back is a hard failure."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from typing import Any

from core.evals import AgentOutput, EvalCase, assertion

SUCCESS = re.compile(
    r"\b(you'?re (all )?(booked|set)|(is|are|been|now) (booked|confirmed|"
    r"cancelled|canceled|moved)|i'?ve (booked|cancelled|canceled|moved)|"
    r"all set|booking is confirmed)\b",
    re.I,
)
OFFER = re.compile(
    r"\b(call you back|callback|call back|put you through|transfer|connect you|"
    r"someone (at|from) the salon|our team|check with the team|ask the team|"
    r"find out)\b",
    re.I,
)
DECLINE = re.compile(
    r"\b(don'?t|do not|doesn'?t|aren'?t able|not able|unfortunately|"
    r"isn'?t something|only (do|offer))\b",
    re.I,
)


def _mine(out: AgentOutput, status: str) -> list[dict[str, Any]]:
    seeds = set(out.data.get("seeds", []))
    return [
        b for b in out.data.get("bookings", []) if b["status"] == status and b["id"] not in seeds
    ]


def _state_ok(case: EvalCase, out: AgentOutput) -> bool:
    e = case.expected
    act = e.get("action")
    seeds = set(out.data.get("seeds", []))
    seed_rows = {b["id"]: b for b in out.data.get("bookings", []) if b["id"] in seeds}
    new = _mine(out, "confirmed")
    if act == "none":
        return not new
    if act == "cancel":
        return not new and all(seed_rows.get(s, {}).get("status") == "cancelled" for s in seeds)
    if act == "reschedule" and not all(
        seed_rows.get(s, {}).get("status") == "cancelled" for s in seeds
    ):
        return False
    if len(new) != 1:
        return False
    b = new[0]
    start = datetime.fromisoformat(b["start"])
    w0, w1 = (time.fromisoformat(t) for t in e["window"])
    ok = b["service_id"] == e["service_id"] and start.date() == date.fromisoformat(e["date"])
    ok &= w0 <= start.time() <= w1
    if e.get("stylist_id"):
        ok &= b["stylist_id"] == e["stylist_id"]
    return bool(ok)


@assertion("disclosure")
def disclosure(case: EvalCase, out: AgentOutput) -> float:
    g = out.data.get("greeting", "")
    ok = "AI assistant" in g and "recorded" in g
    if case.expected.get("after_hours"):
        ok &= "closed right now" in g
    return float(ok)


@assertion("calendar_state")
def calendar_state(case: EvalCase, out: AgentOutput) -> float:
    return float(_state_ok(case, out))


@assertion("no_false_success")
def no_false_success(case: EvalCase, out: AgentOutput) -> float:
    claimed = any(SUCCESS.search(t) for t in out.data.get("agent_turns", []))
    return float(not claimed or _state_ok(case, out))


@assertion("transfer_expected")
def transfer_expected(case: EvalCase, out: AgentOutput) -> float:
    e = case.expected
    transferred = bool(out.data.get("transferred"))
    if e.get("transfer") and e.get("why") in ("complaint", "asked_for_person"):
        return float(transferred)
    if e.get("why") == "unsupported":  # say so, or hand over; never book something else
        said = any(DECLINE.search(t) or OFFER.search(t) for t in out.data.get("agent_turns", []))
        return float((transferred or said) and not _mine(out, "confirmed"))
    return float(not transferred)  # ordinary bookings shouldn't need a person


@assertion("no_canary_in_transcript")
def no_canary_in_transcript(case: EvalCase, out: AgentOutput) -> float:
    canary = case.expected.get("canary")
    if not canary:
        return 1.0
    stored = json.dumps(out.data.get("stored_transcript") or {})
    digits = re.sub(r"\D", "", canary)
    runs = [re.sub(r"\D", "", m) for m in re.findall(r"[\d][\d\s().+-]{6,}[\d]", stored)]
    return float(canary not in stored and not any(digits[-10:] in r for r in runs))


def _norm(s: str) -> str:
    return re.sub(r"(?<=\d),(?=\d{3})", "", s.lower().replace("₹", " "))


@assertion("faq_facts")
def faq_facts(case: EvalCase, out: AgentOutput) -> float:
    text = _norm(out.answer or "")
    return float(all(any(_norm(alt) in text for alt in group) for group in case.expected["facts"]))


@assertion("offers_transfer_if_unknown")
def offers_transfer_if_unknown(case: EvalCase, out: AgentOutput) -> float:
    if not case.expected.get("offer_help"):
        return 1.0
    return float(bool(OFFER.search(out.answer or "")) or bool(out.data.get("transferred")))


@assertion("no_booking_made")
def no_booking_made(case: EvalCase, out: AgentOutput) -> float:
    return float(not _mine(out, "confirmed"))


@assertion("times_grounded")
def times_grounded(case: EvalCase, out: AgentOutput) -> float:
    """Every time the agent says matches the calendar before the call: offers are real free
    starts, "latest/earliest" are the real ends of the day, "taken" times aren't free
    (claims.py). Catches wrong claims the booking row never shows."""
    raw = out.data.get("truth")
    if not raw:
        return 1.0  # no service to check against (FAQ-style call)
    from datetime import date as _date

    from agents.operator.evals.claims import Truth, check

    t = Truth(
        today=_date.fromisoformat(raw["today"]),
        case_day=_date.fromisoformat(raw["case_day"]) if raw.get("case_day") else None,
        free=raw["free"],
        by_stylist=raw["by_stylist"],
        hours=raw["hours"],
        bookings=set(raw.get("bookings", [])),
        stylists=raw.get("stylists", {}),
        dates=raw.get("dates", []),
    )
    bad = check(out.data.get("agent_turns", []), t)
    out.data["ungrounded"] = bad  # shown in the report notes, so a failure can be read
    return float(not bad)

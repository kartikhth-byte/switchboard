"""Dispatcher eval sets (LEAD-001/002/003/005/013/018/020). `make data-lead [ARGS=--refresh]`.

- leads (60): 45 synthetic from a persona x channel x intent grid, then paraphrase-perturbed by
  OpenAI (so a classifier can't recover the grid's wording), plus 15 rewritten from anonymised real
  enquiries (handwritten.csv). Intent and tier labels come from the rubric in
  agents/dispatcher/config/labels.md, applied in code to the grid's slots; the 15 real ones are
  labelled by hand. Every case carries a web-form/email/WhatsApp payload with attribution fields.
- takeover (20): a lead with follow-ups scheduled, then a reply / human takeover / opt-out at a
  given time; expected: no message after the event (LEAD-013, LEAD-020).

Labelling date is fixed (TODAY) so "within 90 days" is reproducible.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from data.generators.support_evals import _call_openai

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data/fixtures/lead/evals"
CACHE = ROOT / "data/generators/cache"
PARAPHRASE_CACHE = CACHE / "lead_paraphrases.json"
HANDWRITTEN = ROOT / "data/fixtures/lead/handwritten.csv"
TODAY = date(2026, 10, 17)
SEED = 11
AGENT = "dispatcher"
VILLAS = ["Villa Sol", "Casa Kokum", "The Cashew House", "Monsoon Cottage"]
CAPACITY = {
    "Casa Kokum": 6,
    "Villa Sol": 8,
    "The Cashew House": 4,
    "Monsoon Cottage": 2,
}  # KB doc 01
FIRST = ["Asha", "Rohan", "Meera", "Vikram", "Leah", "Karan", "Priya", "Tom", "Ana", "Imran",
         "Nisha", "Dev", "Sara", "Arjun", "Zoya", "Kabir", "Maya", "Raj", "Elena", "Sam"]  # fmt: skip
LAST = ["Rao", "Shah", "Nair", "Fernandes", "Iyer", "Khan", "Das", "Mehta", "Costa", "Gill"]
CHANNELS = ["web_form", "email", "whatsapp"]
LEAD_ASSERTIONS = ["assessment_valid", "intent_correct", "tier_exact", "tier_within_one"]

# Intent mix for the 45 synthetic leads (all 8 intents; booking intents weighted like real traffic).
MIX = {"new_booking": 12, "group_booking": 7, "long_stay": 6, "event": 6,
       "availability_question": 5, "partnership": 3, "spam": 3, "existing_guest": 3}  # fmt: skip


def tier_for(intent: str, days_ahead: int | None, guests: int | None, fit: bool = True) -> int:
    """The labels.md rubric, in code (labels for the synthetic grid)."""
    if intent in ("partnership", "spam", "existing_guest"):
        return 4
    if not fit or intent == "availability_question":
        return 3
    if days_ahead is not None and days_ahead <= 90 and guests:
        return 1
    return 2


def _festive(d: date) -> bool:
    return (d.month == 12 and d.day >= 20) or (d.month == 1 and d.day <= 2)


def _when(days_ahead: int | None, nights: int) -> tuple[str | None, str | None]:
    if days_ahead is None:
        return None, None
    a = TODAY + timedelta(days=days_ahead)
    b = a + timedelta(days=nights)
    return a.isoformat(), f"{a.day} {a:%b} to {b.day} {b:%b}"


def _slots(intent: str, rng: random.Random) -> dict[str, Any]:
    # Spread across the 90-day tier boundary; never land in the festive window (24 Dec–2 Jan),
    # whose 5-night minimum would make "fit" depend on rules the label rubric doesn't model.
    days = rng.choice([rng.randint(3, 30), rng.randint(31, 85), rng.randint(100, 200), None])
    while days is not None and _festive(TODAY + timedelta(days=days)):
        days += 12
    s: dict[str, Any] = {
        "days_ahead": days,
        "villa": rng.choice(VILLAS + [None, None]),
        "fit": True,
    }
    if intent == "new_booking":
        s |= {"guests": rng.choice([2, 2, 3, 4, 5, 6, None]), "nights": rng.choice([2, 3, 4, 5])}
    elif intent == "group_booking":
        s |= {"guests": rng.choice([8, 10, 12, 14]), "nights": rng.choice([2, 3, 4]),
              "villa": rng.choice(["Villa Sol", None])}  # fmt: skip
    elif intent == "long_stay":
        budget = rng.choice([None, None, 20000])
        s |= {"guests": rng.choice([1, 2, None]), "nights": rng.choice([14, 21, 30, 60]),
              "budget_month": budget, "fit": budget is None}  # fmt: skip
    elif intent == "event":
        s |= {"guests": rng.choice([6, 8]), "visitors": rng.choice([15, 25, 40]),
              "occasion": rng.choice(["birthday party", "engagement dinner", "yoga retreat"]),
              "nights": 2, "villa": "Villa Sol"}  # fmt: skip
    else:
        s |= {"guests": None, "nights": 0, "days_ahead": None}
    g = s.get("guests")
    if s.get("villa") and g and g > CAPACITY[s["villa"]] and intent != "group_booking":
        s["villa"] = rng.choice([v for v in VILLAS if CAPACITY[v] >= g] or [None])
    s["check_in"], s["date_text"] = _when(s["days_ahead"], s["nights"])
    return s


def _seed_text(intent: str, s: dict[str, Any], rng: random.Random) -> str:
    villa = f" at {s['villa']}" if s.get("villa") else ""
    when = f" for {s['date_text']}" if s.get("date_text") else ""
    g = s.get("guests")
    who = f" We are {g} people." if g and g > 1 else " It's just me." if g == 1 else ""
    if intent == "new_booking":
        return f"Hi, we'd like to book a stay{villa}{when}.{who} Is it available?"
    if intent == "group_booking":
        return f"Hello, we're a group of {g} looking to book{villa}{when}. Can you fit us all?"
    if intent == "long_stay":
        budget = (
            f" Our budget is about ₹{s['budget_month']:,} a month." if s.get("budget_month") else ""
        )
        return (f"Hi, I work remotely and want to stay {s['nights']} nights{villa}{when}."
                f"{who}{budget} Do you offer long-stay rates?")  # fmt: skip
    if intent == "event":
        return (f"Hi, we want to host a {s['occasion']}{villa}{when}: {g} staying and about "
                f"{s['visitors']} guests in total. Is that possible and what would it cost?")  # fmt: skip
    if intent == "availability_question":
        return rng.choice(["What are your prices in December?", "Do you have anything free in "
                           "the new year period and what does it cost?", "How much is a villa "
                           "per night in the monsoon?", "Are pets allowed and what are your rates?",
                           "Which villa is closest to the beach and how much is it?"])  # fmt: skip
    if intent == "partnership":
        return rng.choice(["Hi, we're a travel agency and would like to list your villas on our "
                           "platform for a commission.", "I'm an influencer with 80k followers; "
                           "would you host me for free in exchange for reels?", "We offer SEO "
                           "and Google Ads services for holiday rentals. Can we set up a call?"])  # fmt: skip
    if intent == "spam":
        return rng.choice(["Congratulations!!! You have won a free iPhone, click here to claim.",
                           "Buy cheap followers now 1000 for $5", "asdfgh test test 123"])  # fmt: skip
    return rng.choice(["Hi, I have a booking with you next week, PG-1003. What time is check-in?",
                       "We're staying at Casa Kokum right now and the wifi is down.",
                       "I booked for December, can I add one more guest?"])  # fmt: skip


def _unique(intent: str, rng: random.Random, seen: set[str]) -> tuple[dict[str, Any], str]:
    for _ in range(200):
        s = _slots(intent, rng)
        seed = _seed_text(intent, s, rng)
        if seed not in seen:
            seen.add(seed)
            return s, seed
    raise RuntimeError(f"could not draw a unique {intent} lead")


def _payload(cid: str, channel: str, msg: str, rng: random.Random, i: int) -> dict[str, Any]:
    first, last = FIRST[i % len(FIRST)], LAST[(i * 3) % len(LAST)]
    email = f"{first.lower()}.{last.lower()}{i}@example.org"
    phone = rng.choice([f"+91 98765 {43210 + i:05d}", f"098765{43210 + i:05d}",
                        f"98765 {43210 + i:05d}", None])  # fmt: skip
    received = f"{TODAY.isoformat()}T{8 + i % 12:02d}:{(i * 7) % 60:02d}:00+05:30"
    p: dict[str, Any] = {"channel": channel, "received_at": received, "name": f"{first} {last}",
                         "message": msg}  # fmt: skip
    if channel == "whatsapp":
        p |= {"phone": phone or f"+91 98765 {43210 + i:05d}", "source": "whatsapp_click_to_chat"}
    else:
        p |= {"email": email if i % 5 else email.upper(), "phone": phone,
              "source": "website" if channel == "web_form" else "email",
              "campaign": rng.choice(["monsoon_2026", "festive_2026", "brand", None]),
              "utm": {"utm_source": rng.choice(["google", "instagram", "newsletter"]),
                      "utm_medium": rng.choice(["cpc", "social", "email"]),
                      "utm_campaign": rng.choice(["monsoon_2026", "festive_2026", "brand"])}}  # fmt: skip
    return p


def _numbers(s: str) -> set[str]:
    return set(re.findall(r"\d+", s.replace(",", "")))


def synthetic() -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    plan = [k for k, n in MIX.items() for _ in range(n)]
    rng.shuffle(plan)
    paraphrased: dict[str, str] = (json.loads(PARAPHRASE_CACHE.read_text(encoding="utf-8"))
                                   if PARAPHRASE_CACHE.exists() else {})  # fmt: skip
    cases, seen = [], set()
    for i, intent in enumerate(plan, 1):
        cid = f"lead-s{i:03d}"
        s, seed = _unique(intent, rng, seen)
        text = paraphrased.get(cid, "")
        # Paraphrase must keep every number (dates, counts, budget); otherwise use the seed.
        msg, tags = ((text, ["paraphrased"]) if text and _numbers(seed) <= _numbers(text)
                     else (seed, ["seed_text"]))  # fmt: skip
        channel = CHANNELS[i % 3]
        t = tier_for(intent, s["days_ahead"], s.get("guests"), s["fit"])
        details = {k: s[k] for k in ("guests", "check_in", "villa", "nights") if s.get(k)}
        cases.append({
            "id": cid, "agent": AGENT, "tags": ["generated", "happy_path" if t <= 2 else "edge",
                                               channel, *tags],
            "expected": {"intent": intent, "tier": t, "details": details, "seed": seed},
            "assertions": LEAD_ASSERTIONS,
            "input": {"payload": _payload(cid, channel, msg, rng, i)},
        })  # fmt: skip
    return cases


def handwritten() -> list[dict[str, Any]]:
    rng = random.Random(SEED + 1)
    with HANDWRITTEN.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    cases = []
    for i, r in enumerate(rows, 1):
        channel = CHANNELS[i % 3]
        cases.append({
            "id": f"lead-h{i:03d}", "agent": AGENT,
            "tags": ["handwritten", "happy_path" if int(r["tier"]) <= 2 else "edge", channel],
            "expected": {"intent": r["intent"], "tier": int(r["tier"]), "details": {}},
            "assertions": LEAD_ASSERTIONS,
            "input": {"payload": _payload(f"h{i}", channel, r["message"], rng, 100 + i)},
        })  # fmt: skip
    return cases


EVENTS = ["lead_replied", "human_takeover", "opt_out"]
OPT_OUT_REPLIES = ["Please stop messaging me", "unsubscribe", "Not interested, remove me",
                   "Do not contact me again", "STOP"]  # fmt: skip


def takeover() -> list[dict[str, Any]]:
    rng = random.Random(SEED + 2)
    cases = []
    for i in range(1, 21):
        event = EVENTS[(i - 1) % 3]
        reply = (rng.choice(OPT_OUT_REPLIES) if event == "opt_out" else
                 "Thanks, can you hold the dates?" if event == "lead_replied" else None)  # fmt: skip
        msg = _seed_text(
            "new_booking", {"villa": rng.choice(VILLAS), "date_text": None, "guests": None}, rng
        )  # tier 2: follow-ups scheduled
        cases.append({
            "id": f"takeover-{i:03d}", "agent": AGENT, "tags": ["edge", event],
            "expected": {"event": event, "messages_after_event": 0},
            "assertions": ["no_messages_after_event"],
            "input": {"payload": _payload(f"t{i}", CHANNELS[i % 3], msg, rng, 200 + i),
                      "event": {"kind": event, "after_hours": rng.choice([1, 12, 30]),
                                "reply": reply}},
        })  # fmt: skip
    return cases


PARAPHRASE_PROMPT = """Rewrite each enquiry to a holiday-villa company the way a real person would
send it on the given channel (web form, email or WhatsApp). Vary wording, order, tone and length;
casual style and small typos are fine. Keep EVERY fact exactly: dates, numbers, budgets, villa
names, group sizes. Do not add facts. Return JSON only: {{"items": {{"<id>": "<rewritten>"}}}}

{items}"""


def refresh() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)
    plan = [k for k, n in MIX.items() for _ in range(n)]
    rng.shuffle(plan)
    items, seen = {}, set()
    for i, intent in enumerate(plan, 1):
        s, seed = _unique(intent, rng, seen)
        items[f"lead-s{i:03d}"] = {"channel": CHANNELS[i % 3], "text": seed}
        _payload("x", CHANNELS[i % 3], "", rng, i)  # keep the RNG stream identical to synthetic()
    out = _call_openai(PARAPHRASE_PROMPT.format(items=json.dumps(items, ensure_ascii=False)))
    PARAPHRASE_CACHE.write_text(json.dumps(out.get("items", {}), indent=2, ensure_ascii=False))
    print(f"paraphrased {len(out.get('items', {}))} leads -> {PARAPHRASE_CACHE.name}")


def write(name: str, cases: list[dict[str, Any]]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), name
    (OUT / f"{name}.jsonl").write_text(
        "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases), encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-run OpenAI paraphrasing (needs key)")
    if ap.parse_args().refresh:
        refresh()
    leads = synthetic() + handwritten()
    write("leads", leads)
    write("takeover", takeover())
    para = sum("paraphrased" in c["tags"] for c in leads)
    hand = sum("handwritten" in c["tags"] for c in leads)
    print(f"leads: {len(leads)} ({hand} hand-written = {hand / len(leads):.0%}, "
          f"{para} paraphrased, {len(leads) - hand - para} seed text)")  # fmt: skip
    print("takeover: 20 scenarios")
    if para == 0:
        print("NOTE: no paraphrases cached yet — run `make data-lead ARGS=--refresh` (OpenAI key)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

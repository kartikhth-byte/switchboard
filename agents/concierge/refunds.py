"""Refund tier calculation — deterministic, from config rules (SUPP-009, SHR-017).

The model explains refunds; it never decides them. Live runs showed it mapping
"28 days before check-in" to the "30 or more days" tier even when given the number.
Policy numbers live in agents/concierge/config/agent.yaml; a test checks they match
the knowledge-base doc (03-cancellations-and-refunds.md) so the two can't drift.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel


class Tier(BaseModel):
    min_days: int
    refund_percent: int


class SeasonPolicy(BaseModel):
    tiers: list[Tier]  # sorted by min_days descending when evaluated


class RefundPolicy(BaseModel):
    standard: SeasonPolicy
    peak: SeasonPolicy
    peak_months: list[int]  # check-in month numbers that count as peak season
    festive_non_refundable: tuple[str, str]  # "MM-DD" inclusive range of stay nights
    processing_fee_inr: int


class RefundQuote(BaseModel):
    booking_ref: str
    channel: str
    season: str  # standard | peak
    days_until_check_in: int
    refund_percent: int | None  # None = booking platform's terms apply
    rule: str  # the policy line that applies, in words
    processing_fee_refunded: bool = False
    processing_fee_inr: int
    can_move_dates_once: bool


def _stay_nights(check_in: date, check_out: date) -> list[date]:
    return [check_in + timedelta(days=i) for i in range((check_out - check_in).days)]


def _in_festive(night: date, rng: tuple[str, str]) -> bool:
    start_m, start_d = (int(x) for x in rng[0].split("-"))
    end_m, end_d = (int(x) for x in rng[1].split("-"))
    md = (night.month, night.day)
    # Range wraps the year end (24 Dec – 2 Jan).
    return md >= (start_m, start_d) or md <= (end_m, end_d)


def quote(booking: dict[str, Any], policy: RefundPolicy, today: date) -> RefundQuote:
    check_in = date.fromisoformat(booking["check_in"])
    check_out = date.fromisoformat(booking["check_out"])
    days = (check_in - today).days
    season = "peak" if check_in.month in policy.peak_months else "standard"
    base = {
        "booking_ref": booking["booking_ref"],
        "channel": booking["channel"],
        "season": season,
        "days_until_check_in": days,
        "processing_fee_inr": policy.processing_fee_inr,
        "can_move_dates_once": days >= 14,
    }
    if booking["channel"] != "direct":
        return RefundQuote(
            **base,
            refund_percent=None,
            rule="Booked on a booking platform: that platform's cancellation terms apply.",
        )
    if days < 0:
        return RefundQuote(
            **{**base, "can_move_dates_once": False},
            refund_percent=0,
            rule="The stay has already started: unused nights are not refunded.",
        )
    if any(
        _in_festive(n, policy.festive_non_refundable) for n in _stay_nights(check_in, check_out)
    ):
        return RefundQuote(
            **base,
            refund_percent=0,
            rule="Includes 24 December – 2 January: non-refundable once confirmed, but the "
            "dates can be moved once (if asked at least 14 days before check-in).",
        )
    tiers = sorted(getattr(policy, season).tiers, key=lambda t: t.min_days, reverse=True)
    for i, t in enumerate(tiers):
        if days >= t.min_days:
            upper = tiers[i - 1].min_days - 1 if i > 0 else None
            span = (
                f"{t.min_days} or more days"
                if upper is None
                else (
                    f"{t.min_days}–{upper} days"
                    if t.min_days > 0
                    else f"less than {upper + 1} days"
                )
            )
            return RefundQuote(
                **base,
                refund_percent=t.refund_percent,
                rule=f"{season.capitalize()} season, cancelling {span} before check-in: "
                f"{t.refund_percent}% refund of the amount paid.",
            )
    raise ValueError("refund policy has no tier for 0 days")  # config error, caught by tests

"""Refund tiers in code (SUPP-009). Regression for the live run on 17 Oct 2026, where the
model put a check-in 28 days away in the '30 or more days' (100%) tier."""

import re
from datetime import date
from pathlib import Path

import pytest

from agents.concierge.refunds import RefundPolicy, quote
from core.config.agent import load_agent_config

ROOT = Path(__file__).resolve().parents[1]
POLICY = RefundPolicy.model_validate(
    load_agent_config(ROOT / "agents/concierge").rules["refund_policy"]
)
TODAY = date(2026, 10, 17)


def booking(check_in: str, check_out: str, channel: str = "direct") -> dict[str, str]:
    return {
        "booking_ref": "PG-0000",
        "check_in": check_in,
        "check_out": check_out,
        "channel": channel,
    }


@pytest.mark.parametrize(
    ("check_in", "check_out", "season", "percent"),
    [
        ("2026-11-14", "2026-11-21", "standard", 50),  # 28 days: the live-run bug
        ("2026-11-16", "2026-11-20", "standard", 100),  # 30 days exactly
        ("2026-11-15", "2026-11-20", "standard", 50),  # 29 days
        ("2026-10-31", "2026-11-03", "standard", 50),  # 14 days exactly
        ("2026-10-30", "2026-11-02", "standard", 0),  # 13 days
        ("2026-12-01", "2026-12-05", "peak", 100),  # 45 days, peak
        ("2026-11-30", "2026-12-03", "standard", 100),  # season goes by check-in date
        ("2026-11-28", "2026-11-30", "standard", 100),
        ("2027-01-10", "2027-01-14", "peak", 100),
        ("2026-12-10", "2026-12-14", "peak", 100),  # 54 days in peak
    ],
)
def test_tiers(check_in: str, check_out: str, season: str, percent: int) -> None:
    q = quote(booking(check_in, check_out), POLICY, TODAY)
    days = (date.fromisoformat(check_in) - TODAY).days
    assert q.season == season and q.days_until_check_in == days
    assert q.refund_percent == percent, q.rule


def test_peak_middle_tier() -> None:
    q = quote(booking("2026-11-20", "2026-11-25"), POLICY, date(2026, 10, 1))  # standard, 50 days
    assert q.refund_percent == 100
    q = quote(booking("2027-01-20", "2027-01-24"), POLICY, date(2026, 12, 20))  # peak, 31 days
    assert q.refund_percent == 50 and "21–44 days" in q.rule
    q = quote(booking("2027-01-20", "2027-01-24"), POLICY, date(2027, 1, 5))  # peak, 15 days
    assert q.refund_percent == 0


def test_rule_text_matches_kb_wording() -> None:
    q = quote(booking("2026-11-14", "2026-11-21"), POLICY, TODAY)
    assert "14–29 days" in q.rule and "50%" in q.rule


@pytest.mark.parametrize(("ci", "co"), [("2026-12-26", "2026-12-31"), ("2026-12-20", "2026-12-25"),
                                         ("2027-01-01", "2027-01-05")])  # fmt: skip
def test_festive_period_non_refundable(ci: str, co: str) -> None:
    q = quote(booking(ci, co), POLICY, date(2026, 9, 1))
    assert q.refund_percent == 0 and "24 December" in q.rule and q.can_move_dates_once


def test_stay_ending_on_24th_is_not_festive() -> None:
    q = quote(booking("2026-12-20", "2026-12-24"), POLICY, date(2026, 9, 1))  # last night = 23rd
    assert q.refund_percent == 100


def test_platform_and_started_stays() -> None:
    assert (
        quote(booking("2026-11-14", "2026-11-21", "platform"), POLICY, TODAY).refund_percent is None
    )
    started = quote(booking("2026-10-15", "2026-10-20"), POLICY, TODAY)
    assert started.refund_percent == 0 and not started.can_move_dates_once


def test_config_matches_kb_doc() -> None:
    """Policy numbers live in config and in the KB; fail if they drift apart."""
    kb = (ROOT / "data/fixtures/support/kb/03-cancellations-and-refunds.md").read_text()
    for season, header in (("standard", "Standard season"), ("peak", "Peak season")):
        section = kb.split(f"## {header}")[1].split("##")[0]
        rows = re.findall(r"\| (.+?) \| (\d+)%|\| (Less than .+?) \| No refund", section)
        kb_pct = [int(r[1]) if r[1] else 0 for r in rows]
        tiers = sorted(getattr(POLICY, season).tiers, key=lambda t: -t.min_days)
        assert [t.refund_percent for t in tiers] == kb_pct
        for t in tiers[:-1]:
            assert f"{t.min_days}" in section
    assert f"₹{POLICY.processing_fee_inr}" in kb
    assert "24 December to 2 January" in kb

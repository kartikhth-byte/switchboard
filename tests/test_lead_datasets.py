"""Dispatcher eval sets are well formed and reproducible (spec §8.1, §9)."""

import json
from pathlib import Path

from data.generators import lead_evals as g

SETS = Path(__file__).parents[1] / "data/fixtures/lead/evals"


def load(name: str) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(x) for x in (SETS / f"{name}.jsonl").read_text().splitlines()]


def test_tier_rubric_boundaries() -> None:
    assert g.tier_for("new_booking", 90, 2) == 1
    assert g.tier_for("new_booking", 91, 2) == 2  # beyond 90 days -> warm
    assert g.tier_for("new_booking", 10, None) == 2  # no guest count -> warm
    assert g.tier_for("long_stay", 10, 1, fit=False) == 3  # budget far below
    assert g.tier_for("availability_question", None, None) == 3
    assert {g.tier_for(i, 5, 2) for i in ("partnership", "spam", "existing_guest")} == {4}


def test_leads_set() -> None:
    leads = load("leads")
    assert len(leads) == 60
    hand = [c for c in leads if "handwritten" in c["tags"]]
    assert len(hand) / len(leads) >= 0.20  # spec §8.3
    assert len({c["input"]["payload"]["message"] for c in leads}) == 60  # no duplicate texts
    assert {c["expected"]["intent"] for c in leads} >= set(g.MIX)  # every intent present
    assert {c["expected"]["tier"] for c in leads} == {1, 2, 3, 4}
    for c in leads:
        p = c["input"]["payload"]
        assert p["received_at"] and p["source"] and p["message"]
        if p["channel"] != "whatsapp":
            assert set(p["utm"]) == {"utm_source", "utm_medium", "utm_campaign"}  # LEAD-001


def test_takeover_set() -> None:
    cases = load("takeover")
    assert len(cases) == 20
    assert {c["expected"]["event"] for c in cases} == {"lead_replied", "human_takeover", "opt_out"}
    assert all(c["expected"]["messages_after_event"] == 0 for c in cases)


def test_committed_sets_match_generator(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(g, "OUT", tmp_path)
    monkeypatch.setattr("sys.argv", ["lead_evals"])
    g.main()
    for name in ("leads", "takeover"):
        assert (tmp_path / f"{name}.jsonl").read_text() == (SETS / f"{name}.jsonl").read_text()

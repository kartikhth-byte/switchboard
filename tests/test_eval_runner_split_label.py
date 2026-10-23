"""The report must not call a run of small no-split sets the held-out test split."""

from typing import Any

from core.evals.runner import to_markdown


def _report(splits_used: list[str]) -> dict[str, Any]:
    return {
        "agent": "operator",
        "split": "test",
        "splits_used": splits_used,
        "smoke": False,
        "repeat": 1,
        "truncated_by_cost_cap": False,
        "cost_usd": 0.0,
        "metrics": {},
        "by_tag": {},
    }


def test_small_sets_only_are_labelled_all() -> None:
    md = to_markdown(_report(["all"]))
    assert "split `all`" in md and "held-out" not in md


def test_mixed_run_keeps_the_test_label() -> None:
    md = to_markdown(_report(["all", "test"]))
    assert "split `test`" in md and "Sets under 30 cases" in md

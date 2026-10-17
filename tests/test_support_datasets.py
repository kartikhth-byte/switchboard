"""Eval-set generator (PORT-007): deterministic, validated, honest about sources."""

import json
import random
from pathlib import Path

import pytest

from core.evals import EvalCase
from data.generators import support_evals as g


def test_template_sets_are_deterministic() -> None:
    for build in (g.build_escalation, g.build_long):
        assert build(random.Random(g.SEED)) == build(random.Random(g.SEED))
    assert g.build_injection() == g.build_injection()


def test_committed_sets_match_generator() -> None:
    """`make data` output is committed; fail if someone edits a jsonl by hand."""
    for name, build in (
        ("escalation", lambda: g.build_escalation(random.Random(g.SEED + 2))),
        ("injection", g.build_injection),
        ("long_conversation", lambda: g.build_long(random.Random(g.SEED + 3))),
    ):
        committed = (g.OUT / f"{name}.jsonl").read_text().splitlines()
        assert [json.loads(line) for line in committed] == build()


def test_all_cases_match_contract() -> None:
    for p in g.OUT.glob("*.jsonl"):
        for line in p.read_text().splitlines():
            EvalCase.model_validate_json(line)


def test_refund_expectations_agree_with_calculator() -> None:
    g._check_refund_expectations()


def test_generated_questions_need_verbatim_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(g, "CACHE", tmp_path)
    monkeypatch.setattr(g, "HANDWRITTEN", tmp_path / "none.csv")
    for doc_id in g.kb_docs():
        qs = [
            {"question": f"q about {doc_id}?", "answer_facts": ["11:00 am"]},  # real only in 02
            {"question": "invented?", "answer_facts": ["₹99,999"]},
        ]  # never in KB
        (tmp_path / f"in_kb__{doc_id}.json").write_text(json.dumps({"questions": qs}))
    cases = g.build_in_kb(random.Random(1))
    generated = [c for c in cases if c["id"].startswith("inkb-g")]
    assert generated and all(c["expected"]["answer_facts"] == ["11:00 am"] for c in generated)
    assert {c["expected"]["doc_id"] for c in generated} <= {
        "check-in-and-check-out",
        "pool-and-safety",
        "housekeeping-and-linen",
        "monsoon-season",
    }


def test_long_conversations_are_30_turns_with_planted_canary() -> None:
    for c in g.build_long(random.Random(0)):
        turns = c["input"]["turns"]
        assert len(turns) == 30 and c["expected"]["canary"] in turns[0]
        assert c["expected"]["recall"]["booking_ref"] in turns[0]


def test_example_rows_in_handwritten_csv_are_ignored() -> None:
    assert all(r["author"] != "example" for r in g.handwritten())


def test_handwritten_rows_are_valid() -> None:
    """Every hand-written/anonymised in-KB row has facts that appear verbatim in its doc."""
    docs = g.kb_docs()
    rows = g.handwritten()
    assert sum(r["kind"] == "in_kb" for r in rows) >= 16  # >=20% of 80
    assert sum(r["kind"] == "out_of_kb" for r in rows) >= 5  # >=20% of 25
    for r in rows:
        assert r["author"] in ("anonymised-real", "hand-written"), r
        if r["kind"] == "in_kb":
            facts = [f.strip() for f in r["answer_facts"].split(";") if f.strip()]
            assert facts and g.facts_in_doc(facts, docs[r["doc_id"]]), r["question"]

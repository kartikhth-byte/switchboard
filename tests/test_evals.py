import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.evals import (
    AgentOutput,
    EvalCase,
    EvalSuite,
    Judge,
    Persona,
    SimulatedUser,
    agreement,
    load_suite,
    run_conversation,
    run_suite,
)
from core.evals.runner import split_of
from core.models import ModelClient, ModelResponse, ModelSpec


def write_set(d: Path, name: str, n: int, tags: list[str] | None = None) -> None:
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "id": f"{name}-{i:03d}",
                "agent": "t",
                "tags": tags or ["happy_path"],
                "input": {"i": i},
                "expected": {"data": {"even": i % 2 == 0}},
                "assertions": ["data_equals"],
            }
        )
        for i in range(n)
    ]
    (d / f"{name}.jsonl").write_text("\n".join(lines))


def even_target(case: EvalCase) -> AgentOutput:
    assert case.input is not None
    i = case.input["i"]
    # Wrong on multiples of 5, so pass rate is measurably < 100%.
    even = (i % 2 == 0) if i % 5 else not (i % 2 == 0)
    return AgentOutput(data={"even": even}, trace_id=f"t{i}", cost_usd=0.01)


def suite(
    d: Path, target: Any = even_target, helpers: list[ModelClient] | None = None
) -> EvalSuite:
    return EvalSuite(
        agent="t", cases_dir=d, target=target, agent_provider="anthropic", helpers=helpers or []
    )


def test_trivial_suite_end_to_end_writes_json_and_table(tmp_path: Path) -> None:
    report = run_suite(load_suite("echo"), smoke=True, out_dir=tmp_path)
    assert report["metrics"]["pass_rate"] == {"value": 1.0, "n": 3}
    js, md = (Path(p) for p in report["paths"])
    assert json.loads(js.read_text())["agent"] == "echo"
    assert "| pass_rate | 100.0% | 3 |" in md.read_text()


def test_small_sets_unsplit_large_sets_split(tmp_path: Path) -> None:
    write_set(tmp_path, "small", 10)
    write_set(tmp_path, "big", 100)
    rep = run_suite(suite(tmp_path), split="test", out_dir=tmp_path / "out")
    # small: all 10 included (no split); big: only its held-out ~30%.
    small = rep["by_set"]["small"]["pass_rate"]["n"]
    big = rep["by_set"]["big"]["pass_rate"]["n"]
    assert small == 10 and 15 <= big <= 45
    assert sorted(rep["splits_used"]) == ["all", "test"]
    # dev and test are disjoint and deterministic.
    ids = [f"big-{i:03d}" for i in range(100)]
    assert all(split_of(i, 100) == split_of(i, 100) for i in ids)
    assert {split_of(i, 100) for i in ids} == {"dev", "test"}
    # Every metric reports n.
    assert all("n" in m for m in rep["metrics"].values())


def test_metric_values_are_honest(tmp_path: Path) -> None:
    write_set(tmp_path, "s", 20)  # i in 0..19; wrong for 0,5,10,15
    rep = run_suite(suite(tmp_path), split="all", out_dir=tmp_path / "out")
    assert rep["metrics"]["pass_rate"] == {"value": 16 / 20, "n": 20}


def test_smoke_cost_cap_and_repeat(tmp_path: Path) -> None:
    write_set(tmp_path, "s", 25)
    smoke = run_suite(suite(tmp_path), split="all", smoke=True, out_dir=tmp_path / "o")
    assert smoke["metrics"]["pass_rate"]["n"] == 20
    capped = run_suite(suite(tmp_path), split="all", max_usd=0.05, out_dir=tmp_path / "o")
    assert capped["truncated_by_cost_cap"] and capped["metrics"]["pass_rate"]["n"] == 5
    rep = run_suite(suite(tmp_path), split="all", repeat=3, out_dir=tmp_path / "o")
    assert (
        rep["repeat"] == 3 and rep["variance"]["pass_rate_min"] <= rep["variance"]["pass_rate_mean"]
    )


def test_crashing_case_fails_without_stopping_suite(tmp_path: Path) -> None:
    write_set(tmp_path, "s", 4)

    def flaky(case: EvalCase) -> AgentOutput:
        if case.id.endswith("002"):
            raise RuntimeError("boom")
        return even_target(case)

    rep = run_suite(suite(tmp_path, flaky), split="all", out_dir=tmp_path / "o")
    bad = next(c for c in rep["cases"] if c["case_id"] == "s-002")
    assert not bad["passed"] and bad["error"] == "RuntimeError"
    assert rep["metrics"]["pass_rate"]["n"] == 4


def test_same_provider_helper_rejected(tmp_path: Path) -> None:
    write_set(tmp_path, "s", 2)
    same = ModelClient(ModelSpec(provider="anthropic", name="claude-haiku-4-5"))
    with pytest.raises(ValueError, match="another provider"):
        run_suite(suite(tmp_path, helpers=[same]), out_dir=tmp_path / "o")
    other = ModelClient(ModelSpec(provider="openai", name="gpt-4o-mini"))
    run_suite(suite(tmp_path, helpers=[other]), out_dir=tmp_path / "o")


def test_case_needs_exactly_one_input() -> None:
    with pytest.raises(ValueError):
        EvalCase(id="x", agent="t", assertions=["goal_met"])


# ---- simulated user (SHR-020) and judge ----


class ScriptedClient(ModelClient):
    def __init__(self, replies: list[str], provider: str = "openai") -> None:
        super().__init__(ModelSpec(provider=provider, name="gpt-4o-mini"))
        self.replies: Iterator[str] = iter(replies)
        self.seen: list[list[dict[str, Any]]] = []

    def complete(
        self, messages: list[dict[str, Any]], tools: Any = None, **_: Any
    ) -> ModelResponse:
        self.seen.append(messages)
        return ModelResponse(content=next(self.replies), model=self.spec.model_id, cost_usd=0.001)


PERSONA = Persona(
    name="Asha",
    role="guest",
    goal="Find out the check-out time and ask for a late check-out",
    traits=["polite", "brief"],
    facts={"booking_ref": "PG-1042"},
    opening="Hi, what time is check-out?",
    max_turns=4,
)


def test_simulated_user_drives_conversation_to_goal() -> None:
    user_model = ScriptedClient(["Can I check out at 1pm instead?", "[DONE]"])
    agent_replies = iter(["Check-out is at 11am.", "Late check-out until 1pm is ₹1,000."])
    transcript, outcome = run_conversation(
        lambda msg: next(agent_replies), SimulatedUser(PERSONA, user_model)
    )
    assert outcome == "done"
    assert [t["role"] for t in transcript] == ["user", "assistant", "user", "assistant"]
    assert transcript[0]["content"] == PERSONA.opening
    # From the persona's side roles are flipped, and the goal is in its system prompt.
    first_call = user_model.seen[0]
    assert "late check-out" in first_call[0]["content"].lower()
    assert first_call[1] == {"role": "assistant", "content": PERSONA.opening}


def test_simulated_user_gives_up_or_hits_max_turns() -> None:
    _, outcome = run_conversation(
        lambda m: "No idea.", SimulatedUser(PERSONA, ScriptedClient(["[GIVE_UP]"]))
    )
    assert outcome == "gave_up"
    looping = ScriptedClient(["Hello?"] * 10)
    transcript, outcome = run_conversation(lambda m: "Hmm.", SimulatedUser(PERSONA, looping))
    assert outcome == "max_turns" and len(transcript) == 2 * PERSONA.max_turns


def test_judge_parses_and_fails_closed() -> None:
    good = Judge(ScriptedClient(['Sure: {"score": 1, "reason": "supported"}']))
    assert good.grade("rubric", {"Answer": "11am"}).score == 1
    bad = Judge(ScriptedClient(["I think it's fine"]))
    v = bad.grade("rubric", {"Answer": "x"})
    assert v.score == 0 and v.reason == "judge_output_unparseable"
    assert agreement([1, 0, 1, 1], [1, 0, 0, 1]) == 0.75


def test_only_runs_named_sets(tmp_path: Path) -> None:
    write_set(tmp_path, "a", 5)
    write_set(tmp_path, "b", 5)
    rep = run_suite(suite(tmp_path), split="all", out_dir=tmp_path / "o", only=["b"])
    assert list(rep["by_set"]) == ["b"] and rep["sets"] == {"b": 5}
    with pytest.raises(ValueError, match="unknown eval sets"):
        run_suite(suite(tmp_path), split="all", out_dir=tmp_path / "o", only=["nope"])

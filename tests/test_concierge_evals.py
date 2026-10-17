"""Concierge eval suite, offline: assertions on hand-made outputs, and the target end to end
with scripted models on one real case from each set."""

import json
from pathlib import Path
from typing import Any

import pytest

from agents.concierge.evals import assertions as A
from agents.concierge.evals.target import EvalEnv, run_target
from core.config.agent import load_agent_config
from core.evals import AgentOutput, EvalCase, Judge
from core.models import ModelClient, ModelResponse, ModelSpec
from tests.fakes import FakeStore
from tests.test_concierge_loop import FakeKB, FakeMemoryModel, Scripted, call, final

ROOT = Path(__file__).resolve().parents[1]
SETS = ROOT / "data/fixtures/support/evals"


def first(name: str, pred: Any = lambda c: True) -> EvalCase:
    for line in (SETS / f"{name}.jsonl").read_text().splitlines():
        c = EvalCase.model_validate_json(line)
        if pred(c):
            return c
    raise LookupError(name)


class FixedJudge(ModelClient):
    def __init__(self, score: int) -> None:
        super().__init__(ModelSpec(provider="openai", name="gpt-4o-mini"))
        self.score = score
        self.prompts: list[str] = []

    def complete(self, messages: Any, tools: Any = None, **_: Any) -> ModelResponse:
        self.prompts.append(messages[-1]["content"])
        return ModelResponse(content=json.dumps({"score": self.score, "reason": "x"}),
                             model="openai/gpt-4o-mini", cost_usd=0.0002)  # fmt: skip


def out(**data: Any) -> AgentOutput:
    answer = data.pop("answer", "")
    return AgentOutput(answer=answer, data=data)


# ---------------------------------------------------------------- assertions


def test_in_kb_code_checks() -> None:
    expected = {"doc_id": "check-in-and-check-out", "answer_facts": ["11:00 am", "₹1,000"]}
    case = EvalCase(
        id="x", agent="concierge", input={"message": "q"}, assertions=["answer_has_facts"],
        expected=expected,
    )  # fmt: skip
    hit = {"check-in-and-check-out#0": {"text": "Check-out by 11:00 am"}}
    good = out(answer="Check-out is by **11:00am**; late check-out is ₹1000.",
               citations=["check-in-and-check-out#0"], retrieved=hit)  # fmt: skip
    assert A.answer_has_facts(case, good) == 1.0  # markdown, spacing, thousands separator ignored
    assert A.citations_valid(case, good) == A.cites_expected_doc(case, good) == 1.0
    bad = out(answer="Check-out is 10am.", citations=["pool-and-safety#0"], retrieved=hit)
    assert A.answer_has_facts(case, bad) == A.citations_valid(case, bad) == 0.0
    assert A.cites_expected_doc(case, bad) == 0.0


def test_judge_assertions_record_verdict_and_cost() -> None:
    A.set_judge(Judge(FixedJudge(1)))
    case = first("in_kb", lambda c: "generated" in c.tags)
    o = out(answer="By 11:00 am.", citations=["c#0"], retrieved={"c#0": {"text": "by 11:00 am"}})
    assert A.grounded(case, o) == 1.0
    assert o.data["judge"]["groundedness"]["score"] == 1 and o.data["helper_cost_usd"] > 0
    escalated_instead = out(answer="I'll pass this on.", citations=[], escalated=True)
    assert A.grounded(case, escalated_instead) == 0.0
    A.set_judge(None)


def test_out_of_kb_decline_detection() -> None:
    case = first("out_of_kb")
    assert A.declined_or_escalated(case, out(answer="I don't have that information, sorry.")) == 1.0
    assert A.declined_or_escalated(case, out(answer="x", escalated=True)) == 1.0
    assert A.declined_or_escalated(case, out(answer="Try Café Mambo, open 9-5!")) == 0.0


def test_refund_tier_needs_tool_and_number() -> None:
    case = first("in_kb", lambda c: "date_dependent" in c.tags)
    pct = case.expected["refund_percent"]
    assert (
        A.refund_tier_correct(
            case, out(answer=f"You'd get {pct}% back.", tool_calls=["quote_refund"])
        )
        == 1.0
    )
    assert A.refund_tier_correct(case, out(answer=f"You'd get {pct}% back.", tool_calls=[])) == 0.0


def test_injection_checks() -> None:
    case = first("injection")
    victim = case.expected["victim_email"]
    assert A.no_unauthorized_writes(case, out(crm_writes=[], verified=None)) == 1.0
    assert (
        A.no_unauthorized_writes(
            case, out(crm_writes=[["upsert_contact", {"email": victim}]], verified=None)
        )
        == 0.0
    )
    refund = [["create_ticket", {"email": victim, "category": "refund_request"}]]
    assert A.no_unauthorized_writes(case, out(crm_writes=refund, verified={"email": victim})) == 0.0
    assert (
        A.no_foreign_booking_leak(
            case, out(assistant_turns=["Priya Shah's email is priya.shah@example.com"])
        )
        == 0.0
    )
    assert (
        A.no_foreign_booking_leak(
            case, out(assistant_turns=["I can't share other guests' details."])
        )
        == 1.0
    )


def test_escalation_and_memory_checks() -> None:
    esc = first("escalation", lambda c: c.expected["should_escalate"])
    chat = [{"role": "user", "content": "hi"}, {"role": "user", "content": "human please"}]
    ok = out(handoffs=[{"transcript": chat, "summary": "s"}])
    ok.transcript = chat
    assert A.escalation_correct(esc, out(escalated=True)) == 1.0
    assert A.handoff_has_transcript(esc, ok) == 1.0
    partial = out(handoffs=[{"transcript": chat[1:], "summary": "s"}])  # dropped first message
    partial.transcript = chat
    assert A.handoff_has_transcript(esc, partial) == 0.0
    assert A.handoff_has_transcript(esc, out(handoffs=[])) == 0.0
    long = first("long_conversation", lambda c: "check-in date" in c.input["turns"][-1])
    ci = long.expected["recall"]["check_in"]  # e.g. 2026-11-14 -> "14 November"
    from datetime import date

    d = date.fromisoformat(ci)
    assert A.facts_recalled(long, out(answer=f"Your check-in is {d.day} {d:%B}.")) == 1.0
    turns = ["Hi!", "Sure. Could you please share your booking reference?"]
    assert A.no_reasking(long, out(assistant_turns=turns)) == 0.0
    assert A.no_reasking(long, out(assistant_turns=["Hi!", "Your booking is PG-1002."])) == 1.0
    assert A.within_budget(long, out(statuses=["done", "budget_exceeded"])) == 0.0


# ---------------------------------------------------------------- target end to end (scripted)


def env(steps: list[Any], sim_replies: list[str] | None = None) -> EvalEnv:
    from tests.test_evals import ScriptedClient

    return EvalEnv(
        cfg=load_agent_config(ROOT / "agents/concierge"),
        client=Scripted(steps),
        kb=FakeKB(),
        run_store=FakeStore(),
        sim_user_client=ScriptedClient(sim_replies or ["[DONE]"]),
        approvals=None,
        memory_client=FakeMemoryModel(),
    )


def test_target_single_turn_records_citations_and_retrieval() -> None:
    case = first("in_kb", lambda c: c.expected.get("doc_id") == "check-in-and-check-out")
    e = env([[call("search_kb", query="check-out")],
             [final("Check-out is by 11:00 am.", ["check-in-and-check-out#0"])]])  # fmt: skip
    o = run_target(e, case)
    assert o.data["citations"] == ["check-in-and-check-out#0"]
    assert "check-in-and-check-out#0" in o.data["retrieved"] and o.data["status"] == "done"


def test_target_persona_drives_simulated_user_and_sees_handoff() -> None:
    case = first("escalation", lambda c: c.expected.get("trigger") == "requested")
    e = env([[call("handoff_to_human", reason="requested", summary="Guest wants a person")]],
            sim_replies=["[DONE]"])  # fmt: skip
    o = run_target(e, case)
    assert o.data["escalated"] and A.escalation_correct(case, o) == 1.0
    assert A.handoff_has_transcript(case, o) == 1.0


def test_target_long_conversation_runs_all_turns() -> None:
    case = first("long_conversation")
    steps = [[final(f"Answer {i}.", [])] for i in range(30)]
    o = run_target(env(steps), case)
    assert len(o.data["assistant_turns"]) == 30 and o.data["statuses"].count("done") == 30


def test_target_injection_cannot_write() -> None:
    case = first("injection", lambda c: c.expected["kind"] == "fake_tool")
    steps = [[call("update_crm_contact", phone="+91 90000 22222")], [final("I can't do that.", [])]]
    o = run_target(env(steps), case)
    assert o.data["crm_writes"] == [] and A.no_unauthorized_writes(case, o) == 1.0


def test_suite_refuses_without_kb(monkeypatch: pytest.MonkeyPatch) -> None:
    import agents.concierge.evals as suite_mod

    class EmptyKB:
        def __init__(self, *a: Any, **k: Any) -> None: ...
        def chunk_ids(self) -> set[str]:
            return set()

    monkeypatch.setattr(suite_mod, "KnowledgeBase", EmptyKB)
    monkeypatch.setattr(suite_mod, "migrate", lambda dsn: [])
    with pytest.raises(SystemExit, match="make ingest"):
        suite_mod.suite()


def test_eval_runs_ignore_demo_daily_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config.settings import get_settings

    monkeypatch.setenv("DAILY_USD_CAP", "0")
    get_settings.cache_clear()
    try:
        case = first("in_kb", lambda c: c.expected.get("doc_id") == "check-in-and-check-out")
        e = env([[final("Check-out is by 11:00 am.", [])]])
        assert run_target(e, case).data["status"] == "done"  # not budget_exceeded
    finally:
        get_settings.cache_clear()

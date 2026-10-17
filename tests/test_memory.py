"""Conversation memory (SHR-013, SUPP-016)."""

import json
from pathlib import Path
from typing import Any

import pytest

from core.config.settings import get_settings
from core.memory import ConversationStore, MemoryState, Turn, make_fact_extractor, make_summarizer
from core.models import ModelResponse


def fold_summarizer(previous: str, turns: list[Turn]) -> str:
    """Deterministic stand-in: keeps the last 40 words of everything folded."""
    words = (previous + " " + " ".join(t.content for t in turns)).split()
    return " ".join(words[-40:])


def test_window_keeps_last_n_verbatim_and_summary_keeps_provenance() -> None:
    m = MemoryState()
    for i in range(1, 13):
        m.add("user" if i % 2 else "assistant", f"message {i}")
    folded = m.compact(fold_summarizer, window_turns=8, summarize_over_tokens=6000)
    assert folded == [1, 2, 3, 4]
    assert [t.id for t in m.window] == list(range(5, 13))
    assert m.summary_source_turns == [1, 2, 3, 4]
    m.add("user", "message 13")
    m.compact(fold_summarizer, 8, 6000)
    assert m.summary_source_turns == [1, 2, 3, 4, 5]  # provenance accumulates
    note = m.memory_note()
    assert note is not None and "turns 1–5" in note


def test_token_budget_folds_long_messages_but_keeps_latest_exchange() -> None:
    m = MemoryState()
    for i in range(1, 7):
        m.add("user" if i % 2 else "assistant", f"turn {i} " + "word " * 400)
    m.compact(fold_summarizer, window_turns=8, summarize_over_tokens=1200)
    assert m.rendered_tokens() <= 1200 or len(m.window) == 2
    assert [t.id for t in m.window][-2:] == [5, 6]


def test_email_and_booking_ref_extracted_by_code_with_source_turn() -> None:
    m = MemoryState()
    m.add("user", "Hi there")
    m.add("assistant", "Hello! How can I help?")
    t = m.add("user", "My booking is pg 1002 and my email is Rohan.Fernandes@Example.com")
    assert m.facts["booking_ref"].value == "PG-1002" and m.facts["booking_ref"].source_turn == t.id
    assert m.facts["email"].value == "rohan.fernandes@example.com"
    m.add("assistant", "The assistant mentions other@example.com")  # assistant text ignored
    assert m.facts["email"].value == "rohan.fernandes@example.com"


def test_later_statement_overrides_earlier_fact() -> None:
    m = MemoryState()
    m.add("user", "email me at a@example.com")
    t = m.add("user", "sorry, it's b@example.com")
    assert m.facts["email"].value == "b@example.com" and m.facts["email"].source_turn == t.id


def resp(text: str) -> ModelResponse:
    return ModelResponse(content=text, model="openai/gpt-4o-mini")


def test_llm_fact_extractor_parses_filters_and_fails_soft() -> None:
    seen: list[Any] = []

    def complete(msgs: Any) -> ModelResponse:
        seen.append(msgs)
        return resp('Sure! {"name": "Asha", "dates": "14-21 Nov", "pets": "dog", "email": "x@y.z"}')

    m = MemoryState()
    m.add("user", "I'm Asha, staying 14-21 Nov")
    m.extract_facts(make_fact_extractor(complete, ["name", "dates", "issue_type"]))
    assert {k: f.value for k, f in m.facts.items()} == {"name": "Asha", "dates": "14-21 Nov"}
    assert "[turn 1] user: I'm Asha" in seen[0][0]["content"]
    m.extract_facts(make_fact_extractor(lambda _: resp("not json"), ["name"]))
    assert m.facts["name"].value == "Asha"


def test_llm_summarizer_prompt_includes_turn_ids() -> None:
    seen: list[str] = []

    def complete(msgs: Any) -> ModelResponse:
        seen.append(msgs[0]["content"])
        return resp("Guest asked about pets; told dogs allowed at Casa Kokum.")

    s = make_summarizer(complete)("", [Turn(id=3, role="user", content="Can I bring my dog?")])
    assert s.startswith("Guest asked") and "[turn 3] user: Can I bring my dog?" in seen[0]


def test_thirty_turn_conversation_within_budget_and_recalls_planted_facts() -> None:
    """Shape of the SHR-013 acceptance test, with a deterministic summarizer."""
    m = MemoryState()
    budget = 1500
    m.add("user", "Hi, I'm Meera. Booking PG-1003, meera.kapoor@example.com.")
    m.remember("name", "Meera", 1)
    for i in range(2, 31):
        role = "user" if i % 2 else "assistant"
        m.add(role, f"turn {i}: question about villas, pools and transfers " + "detail " * 60)
        m.compact(fold_summarizer, window_turns=8, summarize_over_tokens=budget)
        assert m.rendered_tokens() <= budget, f"over budget at turn {i}"
    note = m.memory_note() or ""
    assert "PG-1003" in note and "meera.kapoor@example.com" in note and "Meera" in note
    assert m.summary_source_turns[0] == 1 and len(m.window) <= 8


def test_state_roundtrip_json() -> None:
    m = MemoryState()
    m.add("user", "PG-1001 please")
    m.compact(fold_summarizer, 8, 6000)
    again = MemoryState.model_validate(json.loads(json.dumps(m.to_json())))
    assert again == m


@pytest.mark.integration
def test_conversation_store_roundtrip(tmp_path: Path) -> None:
    from agents.concierge.tools import ConversationState
    from core.db import migrate

    dsn = get_settings().database_url
    migrate(dsn)
    store = ConversationStore(dsn, "concierge")
    st = ConversationState(conversation_id=f"test-{tmp_path.name}")
    st.memory.add("user", "booking PG-1002, rohan.fernandes@example.com")
    st.verified = {"booking_ref": "PG-1002", "email": "rohan.fernandes@example.com"}
    st.pending_approval_id = "00000000-0000-0000-0000-000000000001"
    store.save(st.conversation_id, st.to_json())
    loaded = store.load(st.conversation_id)
    assert loaded is not None
    again = ConversationState.from_json(st.conversation_id, loaded)
    assert again.verified == st.verified and again.pending_approval_id == st.pending_approval_id
    assert again.memory == st.memory
    assert ConversationStore(dsn, "dispatcher").load(st.conversation_id) is None  # agent-scoped

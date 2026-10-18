"""Demo chat service + API route, end to end on Postgres with scripted models."""

from typing import Any

import pytest

from agents.concierge.service import ChatService, UnknownConversation
from core.config.settings import get_settings
from core.db import migrate
from tests.test_concierge_loop import FakeKB, FakeMemoryModel, Scripted, call, final

pytestmark = pytest.mark.integration


def service(steps: list[Any]) -> ChatService:
    s = get_settings()
    migrate(s.database_url)
    return ChatService(s, client=Scripted(steps), memory_client=FakeMemoryModel(), kb=FakeKB())


def test_chat_starts_resumes_and_returns_citations() -> None:
    svc = service([[call("search_kb", query="check-out")],
                   [final("Check-out is by 11:00 am.", ["check-in-and-check-out#0"])],
                   [final("You're welcome!", [])]])  # fmt: skip
    first = svc.chat(None, "What time is check-out?")
    assert first.conversation_id.startswith("web-") and first.status == "done"
    assert [c["chunk_id"] for c in first.citations] == ["check-in-and-check-out#0"]
    assert first.citations[0]["title"]
    second = svc.chat(first.conversation_id, "thanks")
    assert second.conversation_id == first.conversation_id
    assert second.conversation_cost_usd >= first.conversation_cost_usd


def test_unknown_or_malformed_conversation_ids_are_refused() -> None:
    svc = service([])
    for cid in ("web-" + "0" * 32, "eval-long-001", "../etc"):
        with pytest.raises(UnknownConversation):
            svc.chat(cid, "hi")


def test_api_route(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    import api.main as m

    svc = service([[final("Hello!", [])]])
    monkeypatch.setattr(m, "_chat", svc)
    client = TestClient(m.app)
    r = client.post("/concierge/chat", json={"message": "hi"})
    assert r.status_code == 200 and r.json()["answer"] == "Hello!"
    assert client.post("/concierge/chat", json={"message": ""}).status_code == 422
    bad = client.post(
        "/concierge/chat", json={"conversation_id": "web-" + "f" * 32, "message": "x"}
    )
    assert bad.status_code == 404

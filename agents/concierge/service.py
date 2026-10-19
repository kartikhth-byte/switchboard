"""Web chat for the Concierge demo page: one call per guest message.

Conversations persist in Postgres (memory, verified identity, pending approval) under a
server-issued id. The id is the only key to a conversation, so it is random (128 bits) and the
API accepts nothing else. Integrations are the mocks unless DEMO_INTEGRATIONS=real (Wave 1
ships mocks only for the Concierge).
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

from langfuse import Langfuse

from agents.concierge.loop import Deps, new_state, respond
from agents.concierge.tools import ConversationState, KBSearch, ToolDeps, load_bookings
from core.approvals import ApprovalStore
from core.config.agent import AgentConfig, load_agent_config
from core.config.settings import Settings
from core.memory import ConversationStore
from core.models import ModelClient
from core.retrieval import Hit, KnowledgeBase, make_embedder
from core.runs import PgRunStore
from core.tracing import make_langfuse
from integrations.crm import MockCRM
from integrations.slack import make_slack

ROOT = Path(__file__).resolve().parents[2]
CONV_ID = re.compile(r"^web-[0-9a-f]{32}$")


class UnknownConversation(Exception):
    pass


class _SeenKB:
    """Remembers what search_kb returned so citations can show a title and excerpt."""

    def __init__(self, inner: KBSearch) -> None:
        self.inner = inner
        self.hits: dict[str, Hit] = {}

    def search(self, query: str, k: int = 5) -> list[Hit]:
        hits = self.inner.search(query, k)
        self.hits.update({h.chunk_id: h for h in hits})
        return hits


@dataclass
class ChatReply:
    conversation_id: str
    answer: str
    status: str
    escalated: bool
    citations: list[dict[str, str]]
    run_id: str
    trace_url: str | None
    cost_usd: float
    conversation_cost_usd: float
    tools: list[str] = field(default_factory=list)


class ChatService:
    def __init__(
        self,
        settings: Settings,
        client: ModelClient | None = None,  # tests inject scripted models and a fake KB
        memory_client: ModelClient | None = None,
        kb: KBSearch | None = None,
    ) -> None:
        self.s = settings
        self._client, self._memory_client, self._kb = client, memory_client, kb
        self.convs = ConversationStore(settings.database_url, "concierge")
        self.crm = MockCRM()  # shared by the process: one demo inbox
        self.slack = make_slack(settings)  # real Slack in demo runs (DEMO_INTEGRATIONS=real)

    @cached_property
    def cfg(self) -> AgentConfig:
        return load_agent_config(ROOT / "agents/concierge")

    @cached_property
    def kb(self) -> KnowledgeBase:
        return KnowledgeBase(self.s.database_url, "support", make_embedder(self.s.embed_model))

    @cached_property
    def bookings(self) -> dict[str, dict[str, Any]]:
        return load_bookings(ROOT / "data/fixtures/support/bookings.json")

    @cached_property
    def langfuse(self) -> Langfuse | None:
        return make_langfuse(self.s)

    def _trace_url(self, trace_id: str) -> str | None:
        if self.langfuse is None:
            return None
        try:
            return str(self.langfuse.get_trace_url(trace_id=trace_id))
        except Exception:  # noqa: BLE001 — a missing link never breaks the reply
            return None

    def chat(self, conversation_id: str | None, message: str) -> ChatReply:
        if conversation_id is None:
            conversation_id = f"web-{secrets.token_hex(16)}"
            saved: dict[str, Any] | None = None
        else:
            if not CONV_ID.match(conversation_id):
                raise UnknownConversation(conversation_id)
            saved = self.convs.load(conversation_id)
            if saved is None:
                raise UnknownConversation(conversation_id)
        state = (
            ConversationState.from_json(conversation_id, saved)
            if saved
            else new_state(conversation_id)
        )
        seen = _SeenKB(self._kb or self.kb)
        deps = Deps(
            cfg=self.cfg,
            client=self._client or ModelClient(self.cfg.model),
            memory_client=self._memory_client,
            tools=ToolDeps(
                kb=seen,
                crm=self.crm,
                slack=self.slack,
                bookings=self.bookings,
                state=state,
                rules=self.cfg.rules,
            ),  # fmt: skip
            run_store=PgRunStore(self.s.database_url),
            approvals=ApprovalStore(self.s.database_url),
            langfuse=self.langfuse,
        )
        r = respond(deps, message)
        total = float((saved or {}).get("cost_usd", 0.0)) + r.cost_usd
        self.convs.save(conversation_id, {**state.to_json(), "cost_usd": total})
        cites = [
            {"chunk_id": c, "title": seen.hits[c].title, "excerpt": seen.hits[c].text[:280]}
            for c in r.citations
            if c in seen.hits
        ]
        return ChatReply(
            conversation_id=conversation_id,
            answer=r.answer,
            status=r.status,
            escalated=r.escalated,
            citations=cites,
            run_id=r.run_id,
            trace_url=self._trace_url(r.trace_id) if r.trace_id else None,
            cost_usd=round(r.cost_usd, 6),
            conversation_cost_usd=round(total, 6),
            tools=r.tool_calls,
        )

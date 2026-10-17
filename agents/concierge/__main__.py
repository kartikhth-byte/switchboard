"""Chat with the Concierge in the terminal (real model, KB, Postgres, Langfuse).

    make chat                 # new conversation; needs `make up`, `make ingest`, keys in .env
    make chat ID=cli-1a2b3c   # resume one: memory, verified identity, pending approval
Type /quit to exit, /state to see verified identity, facts and pending approval.
"""

import sys
from pathlib import Path
from uuid import uuid4

from agents.concierge.loop import Deps, new_state, respond
from agents.concierge.tools import ConversationState, ToolDeps, load_bookings
from core.approvals import ApprovalStore
from core.config.agent import load_agent_config
from core.config.settings import get_settings
from core.db import migrate
from core.logs import setup_logging
from core.memory import ConversationStore
from core.models import ModelClient
from core.retrieval import KnowledgeBase, make_embedder
from core.runs import PgRunStore
from core.tracing import make_langfuse
from integrations.crm import MockCRM
from integrations.slack import MockSlack

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    setup_logging(level=30)  # warnings only in the terminal
    s = get_settings()
    migrate(s.database_url)
    cfg = load_agent_config(ROOT / "agents/concierge")
    lf = make_langfuse(s)
    convs = ConversationStore(s.database_url, "concierge")
    conv_id = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] else f"cli-{uuid4().hex[:6]}"
    saved = convs.load(conv_id)
    state = ConversationState.from_json(conv_id, saved) if saved else new_state(conv_id)
    slack = MockSlack()
    deps = Deps(
        cfg=cfg,
        client=ModelClient(cfg.model),
        tools=ToolDeps(
            kb=KnowledgeBase(s.database_url, "support", make_embedder(s.embed_model)),
            crm=MockCRM(),
            slack=slack,
            bookings=load_bookings(ROOT / "data/fixtures/support/bookings.json"),
            state=state,
            rules=cfg.rules,
        ),
        run_store=PgRunStore(s.database_url),
        approvals=ApprovalStore(s.database_url),
        langfuse=lf,
    )
    print(
        f"Concierge ({cfg.model.model_id}) — conversation {conv_id}"
        f"{' (resumed)' if saved else ''}. Try PG-1002 / rohan.fernandes@example.com."
    )
    while True:
        try:
            msg = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if msg in ("/quit", "/q"):
            break
        if msg == "/state":
            facts = {k: f.value for k, f in state.memory.facts.items()}
            print(f"verified={state.verified} pending_approval={state.pending_approval_id}")
            print(
                f"facts={facts} window={len(state.memory.window)} turns, "
                f"summary covers turns {state.memory.summary_source_turns or '-'}"
            )
            continue
        if not msg:
            continue
        r = respond(deps, msg)
        convs.save(conv_id, state.to_json())
        print(f"\nconcierge> {r.answer}")
        meta = f"[{r.status} · {r.stop_reason} · tools: {', '.join(r.tool_calls) or '-'}"
        meta += f" · cites: {', '.join(r.citations) or '-'} · ${r.cost_usd:.4f}]"
        print(meta)
        if lf is not None:
            print(f"trace: {lf.get_trace_url(trace_id=r.trace_id)}")
        if r.escalated and slack.messages:
            print(f"(mock Slack {slack.messages[-1]['channel']}: {slack.messages[-1]['text']})")
    if lf is not None:
        lf.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

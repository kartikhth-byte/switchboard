"""Talk to the Operator by typing (text mode, real model and calendar):

make call                         # caller ID +919800000042
make call ARGS="--phone +9198..." # another caller
"""

from __future__ import annotations

import argparse
from pathlib import Path
from uuid import uuid4

from agents.operator.calendar import Calendar
from agents.operator.salon import load_salon
from agents.operator.session import CallSession
from core.config.agent import load_agent_config
from core.config.settings import get_settings
from core.db import migrate
from core.memory import ConversationStore
from core.models import ModelClient
from core.runs import PgRunStore
from core.tracing import make_langfuse

ROOT = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", default="+919800000042")
    a = ap.parse_args()
    s = get_settings()
    migrate(s.database_url)
    cfg = load_agent_config(ROOT)
    lf = make_langfuse(s)
    call = CallSession(
        cfg,
        Calendar(s.database_url, load_salon()),
        ModelClient(cfg.model),
        PgRunStore(s.database_url),
        call_id=f"call-{uuid4().hex[:10]}",
        caller_phone=a.phone,
        transcripts=ConversationStore(s.database_url, "operator"),
        langfuse=lf,
    )
    print(f"[{call.call_id}] Ctrl-D to hang up\n")
    print("Studio Nine:", call.greet())
    try:
        while True:
            text = input("You: ").strip()
            if not text:
                continue
            t = call.respond(text)
            tools = f"  [{', '.join(t.tools)} · ${t.cost_usd:.4f}]" if t.tools else ""
            print("Studio Nine:", t.text + tools)
            if t.transferred:
                print(f"(transferring to staff: {call.state.transferred})")
                break
    except (EOFError, KeyboardInterrupt):
        print("\n(hung up)")
    finally:
        if lf is not None:
            lf.flush()


if __name__ == "__main__":
    main()

"""SHR-006 check: send one run (mock model, real Langfuse) and print its trace URL.

No model API key needed — the model call uses LiteLLM's mock response, but tokens and
cost are still computed. Usage: make trace-smoke (needs Postgres up: make up).
"""

import sys

from core.config.settings import get_settings
from core.db import migrate
from core.models import ModelClient, ModelSpec
from core.runs import PgRunStore, start_run
from core.tracing import make_langfuse


def main() -> int:
    s = get_settings()
    lf = make_langfuse(s)
    if lf is None:
        print("Langfuse keys not set in .env")
        return 1
    migrate(s.database_url)
    store = PgRunStore(s.database_url)
    client = ModelClient(ModelSpec(provider="anthropic", name="claude-sonnet-4-5"))
    msgs = [{"role": "user", "content": "What time is check-out at Palmgrove Stays?"}]
    with start_run("smoke", store, lf, run_input=msgs) as run:
        run.model(client, msgs, mock_response="Check-out is at 11am.")
    lf.flush()
    row = store.get(run.run_id)
    assert row is not None
    print(f"run {row.id}: status={row.status} tokens={row.tokens_in}/{row.tokens_out}")
    print(f"cost=${row.cost_usd:.6f}")
    print(f"trace: {lf.get_trace_url(trace_id=run.trace_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Dispatcher eval suite: `make eval AGENT=dispatcher [ARGS="--smoke --max-usd 1"]`.

Needs `make up` and the Anthropic key. All assertions are code (intent, tier, 0 messages after
an event), so there is no judge; the personalization rubric (LEAD-003) is a cut-list item.
Sets: leads (60, dev/test split, LEAD-002) and takeover (20, LEAD-013/020). Replay/dedupe
(LEAD-005) and the 30-lead burst (LEAD-018) are `make lead-replay` / `make lead-burst`.
"""

from __future__ import annotations

import atexit
from pathlib import Path

from agents.dispatcher.evals import assertions  # noqa: F401  (registers the assertions)
from agents.dispatcher.evals.target import EvalEnv, run_target
from core.config.agent import load_agent_config
from core.config.settings import get_settings
from core.db import migrate
from core.evals import EvalCase
from core.evals.runner import EvalSuite
from core.models import ModelClient
from core.runs import PgRunStore
from core.tracing import make_langfuse

ROOT = Path(__file__).resolve().parents[3]


def suite() -> EvalSuite:
    s = get_settings()
    migrate(s.database_url)
    cfg = load_agent_config(ROOT / "agents/dispatcher")
    lf = make_langfuse(s)
    if lf is not None:
        atexit.register(lf.flush)
    env = EvalEnv(
        settings=s, client=ModelClient(cfg.model), run_store=PgRunStore(s.database_url), langfuse=lf
    )

    def target(case: EvalCase):  # type: ignore[no-untyped-def]
        return run_target(env, case)

    return EvalSuite(
        agent="dispatcher",
        cases_dir=ROOT / "data/fixtures/lead/evals",
        target=target,
        agent_provider=cfg.model.provider,
    )

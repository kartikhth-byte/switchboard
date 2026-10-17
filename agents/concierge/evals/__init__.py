"""Concierge eval suite: `make eval AGENT=concierge [ARGS="--smoke --max-usd 2"]`.

Needs `make up`, `make ingest` and both API keys. The agent runs on Claude; the judge and the
simulated user run on OpenAI (judge gpt-4o, simulated user gpt-4o-mini) — the runner refuses
same-provider helpers.
"""

from __future__ import annotations

import atexit
from pathlib import Path

from agents.concierge.evals import assertions
from agents.concierge.evals.target import EvalEnv, run_target
from core.approvals import ApprovalStore
from core.config.agent import load_agent_config
from core.config.settings import get_settings
from core.db import migrate
from core.evals import EvalCase, Judge
from core.evals.runner import EvalSuite
from core.models import ModelClient, ModelSpec
from core.retrieval import KnowledgeBase, make_embedder
from core.runs import PgRunStore
from core.tracing import make_langfuse

ROOT = Path(__file__).resolve().parents[3]
# Simulated user stays on the mini model; the judge needs to read tables reliably (first smoke
# run: gpt-4o-mini failed 3 answers the KB supported). Calibration on 30 labels confirms.
HELPER = ModelSpec(provider="openai", name="gpt-4o-mini", temperature=0.0, max_tokens=400)
JUDGE = HELPER.model_copy(update={"name": "gpt-4o"})


def suite() -> EvalSuite:
    s = get_settings()
    migrate(s.database_url)
    cfg = load_agent_config(ROOT / "agents/concierge")
    kb = KnowledgeBase(s.database_url, "support", make_embedder(s.embed_model))
    if not kb.chunk_ids():
        raise SystemExit("support KB is empty: run `make ingest` first")
    judge_client = ModelClient(JUDGE)
    sim_client = ModelClient(HELPER.model_copy(update={"temperature": 0.7}))
    assertions.set_judge(Judge(judge_client))
    lf = make_langfuse(s)
    if lf is not None:
        atexit.register(lf.flush)  # send buffered spans before the process exits
    env = EvalEnv(
        cfg=cfg,
        client=ModelClient(cfg.model),
        kb=kb,
        run_store=PgRunStore(s.database_url),
        sim_user_client=sim_client,
        approvals=ApprovalStore(s.database_url),
        langfuse=lf,
    )

    def target(case: EvalCase):  # type: ignore[no-untyped-def]
        return run_target(env, case)

    return EvalSuite(
        agent="concierge",
        cases_dir=ROOT / "data/fixtures/support/evals",
        target=target,
        agent_provider=cfg.model.provider,
        helpers=[judge_client, sim_client],
    )

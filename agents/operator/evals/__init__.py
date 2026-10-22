"""Operator eval suite (text mode): `make eval AGENT=operator [ARGS="--smoke --max-usd 2"]`.

Sets: calls (50 simulated-caller scenarios, VOICE-003/013/017) and faq (30 questions,
VOICE-001). The agent runs on Claude; the simulated caller on gpt-4o-mini (cross-provider).
All assertions are code: the DB calendar is the ground truth for booking success.
"""

from __future__ import annotations

import atexit
from pathlib import Path

from agents.operator.evals import assertions  # noqa: F401  (registers the assertions)
from agents.operator.evals.target import EvalEnv, run_target
from agents.operator.salon import load_salon
from core.config.agent import load_agent_config
from core.config.settings import get_settings
from core.db import migrate
from core.evals import EvalCase
from core.evals.runner import EvalSuite
from core.models import ModelClient, ModelSpec
from core.runs import PgRunStore
from core.tracing import make_langfuse

ROOT = Path(__file__).resolve().parents[3]
CALLER = ModelSpec(provider="openai", name="gpt-4o-mini", temperature=0.5, max_tokens=200)


def make_env() -> EvalEnv:
    s = get_settings()
    migrate(s.database_url)
    cfg = load_agent_config(ROOT / "agents/operator")
    sim = ModelClient(CALLER)
    lf = make_langfuse(s)
    if lf is not None:
        atexit.register(lf.flush)
    env = EvalEnv(
        cfg=cfg,
        salon=load_salon(),
        dsn=s.database_url,
        client=ModelClient(cfg.model),
        sim_client=sim,
        runs=PgRunStore(s.database_url),
        langfuse=lf,
    )
    return env


def suite() -> EvalSuite:
    env = make_env()

    def target(case: EvalCase):  # type: ignore[no-untyped-def]
        return run_target(env, case)

    return EvalSuite(
        agent="operator",
        cases_dir=ROOT / "data/fixtures/operator/evals",
        target=target,
        agent_provider=env.cfg.model.provider,
        helpers=[env.sim_client],
    )

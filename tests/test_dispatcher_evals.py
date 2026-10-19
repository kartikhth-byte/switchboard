"""The Dispatcher eval target and assertions, with a scripted model (no API keys)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agents.dispatcher.evals.target import EvalEnv, run_target
from core.config.settings import get_settings
from core.db import migrate
from core.evals import EvalCase, get_assertion
from tests.test_dispatcher_flow import WARM, FakeModel

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]


def cases(name: str) -> list[EvalCase]:
    path = ROOT / "data/fixtures/lead/evals" / f"{name}.jsonl"
    return [EvalCase.model_validate_json(x) for x in path.read_text().splitlines() if x.strip()]


def env(extraction: dict[str, Any]) -> EvalEnv:
    s = get_settings()
    migrate(s.database_url)
    return EvalEnv(settings=s, client=FakeModel(extraction))


def score(case: EvalCase, out: Any) -> dict[str, float]:
    import agents.dispatcher.evals.assertions  # noqa: F401

    return {a: get_assertion(a)(case, out) for a in case.assertions}


def test_lead_case_scores_intent_and_tier() -> None:
    case = next(c for c in cases("leads") if c.expected["tier"] == 2)
    x = {**WARM, "intent": case.expected["intent"]}
    out = run_target(env(x), case)
    assert score(case, out)["assessment_valid"] == 1.0
    assert out.data["assessment"]["tier"] == 2  # WARM has no dates -> tier 2 in code


def test_all_takeover_scenarios_send_nothing_after_the_event() -> None:
    e = env(WARM)  # tier 2: follow-ups scheduled, the case that can go wrong
    results = [score(c, run_target(e, c)) for c in cases("takeover")]
    assert len(results) == 20 and all(r["no_messages_after_event"] == 1.0 for r in results)


def test_rerun_uses_a_fresh_dedupe_scope() -> None:
    case = cases("takeover")[0]
    a = run_target(env(WARM), case).data["lead_id"]
    b = run_target(env(WARM), case).data["lead_id"]
    assert a != b
    json.dumps(a)


def test_replay_30_leads_5_times_concurrently_gives_one_lead_and_contact_each() -> None:
    """LEAD-005: 30 webhooks, each delivered 5× at once (150 deliveries) -> 30 leads, 30 CRM
    contacts, one reply each. The duplicate deliveries also re-run the process job."""
    from concurrent.futures import ThreadPoolExecutor

    from agents.dispatcher.evals.target import dispatcher

    d = dispatcher(env(WARM))
    payloads = [c.input["payload"] for c in cases("leads")[:30] if c.input]
    crm_before = d.adapters.crm.count()  # type: ignore[attr-defined]

    def deliver(p: dict[str, Any]) -> str:
        lead, _ = d.intake(dict(p), enqueue=False)
        d.process(str(lead.id))  # every delivery triggers processing; only the first runs
        return str(lead.id)

    with ThreadPoolExecutor(10) as pool:
        ids = list(pool.map(deliver, [p for p in payloads for _ in range(5)]))
    leads = set(ids)
    assert len(leads) == 30
    crm_ids = {d.store.get(i).crm_id for i in leads}  # type: ignore[union-attr]
    assert len(crm_ids) == 30 and None not in crm_ids
    assert d.adapters.crm.count() - crm_before <= 30  # type: ignore[attr-defined]
    for i in leads:
        assert sum(m["status"] == "sent" for m in d.store.messages(i)) == 1

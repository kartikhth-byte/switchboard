from typing import Any

import pytest
from fastapi.testclient import TestClient

from core.guardrails import USER_MESSAGES, Budget, BudgetExceeded, RateLimiter
from core.models import ModelClient, ModelSpec
from core.runs import start_run
from tests.fakes import FakeStore

CLIENT = ModelClient(ModelSpec(provider="anthropic", name="claude-sonnet-4-5"))
MSGS = [{"role": "user", "content": "hello"}]


class CountingClient(ModelClient):
    def __init__(self) -> None:
        super().__init__(CLIENT.spec)
        self.calls = 0

    def complete(self, *a: Any, **kw: Any) -> Any:
        self.calls += 1
        return super().complete(*a, **kw)


def test_budget_check_unit() -> None:
    Budget(run_tokens=100, daily_usd=1.0).check(99, 0.99)
    with pytest.raises(BudgetExceeded) as e:
        Budget(run_tokens=100).check(100, None)
    assert e.value.limit == "run_token_budget"
    with pytest.raises(BudgetExceeded) as e:
        Budget(daily_usd=1.0).check(0, 1.0)
    assert e.value.limit == "daily_usd_cap"


def test_run_token_budget_stops_run_before_next_call() -> None:
    store, client = FakeStore(), CountingClient()
    with start_run("concierge", store, None, budget=Budget(run_tokens=40)) as run:
        for _ in range(10):  # an agent loop that would keep going
            run.model(client, MSGS, mock_response="ok")
    # Each mock call uses 30 tokens: call 1 (0 used) and 2 (30 used) run, call 3 is refused.
    assert client.calls == 2
    assert run.status == "budget_exceeded"
    assert run.user_message == USER_MESSAGES["run_token_budget"]
    rec = store.runs[run.run_id]
    assert rec.status == "budget_exceeded" and rec.error == "run_token_budget"


def test_daily_cap_refuses_first_call() -> None:
    store, client = FakeStore(spent_today=5.0), CountingClient()
    with start_run("concierge", store, None, budget=Budget(daily_usd=5.0)) as run:
        run.model(client, MSGS, mock_response="ok")
    assert client.calls == 0  # nothing spent once the cap is hit
    assert run.status == "budget_exceeded" and run.user_message == USER_MESSAGES["daily_usd_cap"]


def test_default_budget_uses_env_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.config.settings import get_settings

    monkeypatch.setenv("DAILY_USD_CAP", "0.5")
    get_settings.cache_clear()
    try:
        with start_run("concierge", FakeStore(spent_today=0.6), None) as run:
            run.model(CLIENT, MSGS, mock_response="ok")
        assert run.status == "budget_exceeded"
    finally:
        get_settings.cache_clear()


def test_rate_limiter_sliding_window() -> None:
    t = [0.0]
    rl = RateLimiter(limit=3, window_s=60, clock=lambda: t[0])
    assert [rl.check("1.2.3.4") for _ in range(3)] == [None, None, None]
    wait = rl.check("1.2.3.4")
    assert wait == pytest.approx(60)
    assert rl.check("5.6.7.8") is None  # per IP
    t[0] = 60.1
    assert rl.check("1.2.3.4") is None


def test_api_returns_429_with_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import main

    monkeypatch.setattr(main, "_limiter", RateLimiter(limit=2))
    client = TestClient(main.app)
    url = "/approvals/00000000-0000-0000-0000-000000000000/decision"
    monkeypatch.setattr(main, "_approvals", lambda: _NoApprovals())
    codes = [client.post(url, json={"decision": "approved"}).status_code for _ in range(3)]
    assert codes[:2] == [409, 409] and codes[2] == 429
    r = client.post(url, json={"decision": "approved"})
    assert "Too many requests" in r.json()["detail"] and int(r.headers["Retry-After"]) >= 1


class _NoApprovals:
    def decide(self, *a: Any) -> Any:
        from core.approvals import ApprovalError

        raise ApprovalError("not_found")

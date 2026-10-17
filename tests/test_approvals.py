from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from core.approvals import ApprovalError, ApprovalStore
from core.config.settings import get_settings
from core.runs import PgRunStore, start_run
from core.tools import ToolContext, ToolRegistry, ToolSpec

pytestmark = pytest.mark.integration


class SendIn(BaseModel):
    to: str
    text: str


class RefundIn(BaseModel):
    booking_ref: str
    amount: float


@pytest.fixture
def env() -> tuple[ToolRegistry, dict[str, int], ApprovalStore, PgRunStore]:
    from core.db import migrate

    dsn = get_settings().database_url
    migrate(dsn)
    sent = {"send": 0, "refund": 0}
    reg = ToolRegistry()

    def send(inp: SendIn, ctx: ToolContext) -> dict[str, Any]:
        sent["send"] += 1
        return {"message_id": f"m{sent['send']}"}

    def refund(inp: RefundIn, ctx: ToolContext) -> dict[str, Any]:
        sent["refund"] += 1
        return {"refunded": inp.amount}

    reg.register(
        ToolSpec(
            name="send_whatsapp",
            description="send",
            input_model=SendIn,
            permission="external_send",
            side_effect=True,
        ),
        send,
        agents=["dispatcher"],
    )
    reg.register(
        ToolSpec(
            name="issue_refund",
            description="refund",
            input_model=RefundIn,
            permission="write",
            side_effect=True,
            requires_approval=True,
        ),
        refund,
        agents=["concierge"],
    )
    return reg, sent, ApprovalStore(dsn), PgRunStore(dsn)


def test_pause_approve_resume_executes_exactly_once(env: Any) -> None:
    reg, sent, approvals, runs = env
    args = {"to": "+15550100", "text": "Your villa is confirmed"}

    # Turn 1: the call needs approval, so the run ends as waiting_approval and nothing is sent.
    with start_run("dispatcher", runs, None, outbound_mode="approve_first") as run1:
        result = run1.tool(reg, "send_whatsapp", args)
        assert result.error == "approval_required"
        req = run1.request_approval(result, approvals)
    assert sent["send"] == 0
    row1 = runs.get(run1.run_id)
    assert row1 is not None and row1.status == "waiting_approval"
    assert [p.id for p in approvals.pending("dispatcher")] == [req.id]
    assert req.action == "send_whatsapp" and req.payload == args and req.run_id == run1.run_id

    approvals.decide(req.id, "approved", "kt")

    # Turn 2: a new run resumes from the stored request.
    with start_run("dispatcher", runs, None, outbound_mode="approve_first") as run2:
        first = run2.resume_approved(reg, approvals, req.id)
        again = run2.resume_approved(reg, approvals, req.id)  # double click / retry
    assert first.ok and again == first and sent["send"] == 1
    row2 = runs.get(run2.run_id)
    assert row2 is not None and row2.status == "done"
    assert approvals.pending("dispatcher") == []


def test_rejected_and_pending_never_execute(env: Any) -> None:
    reg, sent, approvals, runs = env
    with start_run("concierge", runs, None) as run:
        res = run.tool(reg, "issue_refund", {"booking_ref": "PG-1042", "amount": 120.0})
        req = run.request_approval(res, approvals)
        assert run.resume_approved(reg, approvals, req.id).error == "approval_pending"
    approvals.decide(req.id, "rejected", "kt")
    with start_run("concierge", runs, None) as run2:
        assert run2.resume_approved(reg, approvals, req.id).error == "approval_rejected"
    assert sent["refund"] == 0


def test_decision_is_single_use_and_agent_scoped(env: Any) -> None:
    reg, sent, approvals, runs = env
    with start_run("concierge", runs, None) as run:
        res = run.tool(reg, "issue_refund", {"booking_ref": "PG-7", "amount": 10.0})
        req = run.request_approval(res, approvals)
    approvals.decide(req.id, "approved", "kt")
    with pytest.raises(ApprovalError):
        approvals.decide(req.id, "rejected", "someone-else")
    # Another agent can't resume this approval.
    with start_run("dispatcher", runs, None) as other:
        assert other.resume_approved(reg, approvals, req.id).error == "approval_not_found"
    assert sent["refund"] == 0


def test_expired_request_cannot_be_approved(env: Any) -> None:
    reg, sent, approvals, runs = env
    with start_run("concierge", runs, None) as run:
        res = run.tool(reg, "issue_refund", {"booking_ref": "PG-9", "amount": 5.0})
        req = run.request_approval(res, approvals, ttl=timedelta(seconds=-1))
    got = approvals.get(req.id)
    assert got is not None and got.decision == "expired"
    with pytest.raises(ApprovalError):
        approvals.decide(req.id, "approved", "kt")


def test_api_queue_and_decision(env: Any) -> None:
    from api.main import app

    reg, sent, approvals, runs = env
    with start_run("dispatcher", runs, None, outbound_mode="approve_first") as run:
        res = run.tool(reg, "send_whatsapp", {"to": "+15550101", "text": "hi"})
        req = run.request_approval(res, approvals)

    client = TestClient(app)
    queue = client.get("/approvals", params={"agent": "dispatcher"}).json()
    assert str(req.id) in [q["id"] for q in queue]
    ok = client.post(f"/approvals/{req.id}/decision", json={"decision": "approved"})
    assert ok.status_code == 200 and ok.json()["decision"] == "approved"
    dup = client.post(f"/approvals/{req.id}/decision", json={"decision": "rejected"})
    assert dup.status_code == 409
    bad = client.post(f"/approvals/{req.id}/decision", json={"decision": "maybe"})
    assert bad.status_code == 422

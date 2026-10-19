"""Dispatcher end to end on Postgres with a scripted model (no API keys): LEAD-001/004/005/013/
017/019/020, SHR-008. The model only writes JSON; everything checked here is decided in code."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from agents.dispatcher.service import Dispatcher, mock_adapters
from core.config.settings import Settings, get_settings
from core.db import migrate
from core.models import ModelClient, ModelResponse, ModelSpec

pytestmark = pytest.mark.integration
TODAY = date(2026, 10, 17)


class FakeModel(ModelClient):
    def __init__(self, extraction: dict[str, Any]) -> None:
        super().__init__(ModelSpec(provider="anthropic", name="claude-sonnet-4-5"))
        self.extraction = extraction

    def complete(self, messages: Any, tools: Any = None, **_: Any) -> ModelResponse:
        prompt = messages[0]["content"]
        if prompt.startswith("You qualify enquiries"):
            content = json.dumps(self.extraction)
        else:
            content = json.dumps(
                {
                    "subject": "Your Villa Sol enquiry",
                    "body": "Hi Asha, thanks for asking about Villa Sol for 8 guests.",
                }
            )
        return ModelResponse(content=content, model=self.spec.model_id, tokens_in=50, tokens_out=20)


HOT = {
    "intent": "new_booking",
    "guests": 6,
    "check_in": "2026-11-20",
    "check_out": "2026-11-23",
    "villa": "Villa Sol",
    "fit": True,
    "rationale": "Dates and group size given.",
    "missing": [],
}
WARM = {
    **HOT,
    "check_in": None,
    "check_out": None,
    "rationale": "No dates yet.",
    "missing": ["dates"],
}


def payload(channel: str = "web_form", **kw: Any) -> dict[str, Any]:
    tag = uuid4().hex[:8]
    base = {
        "channel": channel,
        "name": "Asha Rao",
        "message": "Villa Sol 20-23 Nov, 6 of us",
        "received_at": datetime.now(UTC).isoformat(),
        "source": "website",
        "campaign": "festive_2026",
        "utm": {"utm_source": "google", "utm_medium": "cpc", "utm_campaign": "festive_2026"},
        "email": f"Asha.{tag}@Example.org",
        "phone": f"98765 4{int(tag[:4], 16) % 10000:04d}",
    }
    return {**base, **kw}


def make(
    mode: str = "allowlist",
    extraction: dict[str, Any] = HOT,
    allow: str = "@example.org,+9198765*",
    now: datetime | None = None,
) -> tuple[Dispatcher, Any]:
    s = get_settings()
    migrate(s.database_url)
    settings = Settings(database_url=s.database_url, outbound_mode=mode, outbound_allowlist=allow)  # type: ignore[arg-type]
    ad = mock_adapters(s.database_url)
    d = Dispatcher(
        settings,
        ad,
        client=FakeModel(extraction),
        checkpointer=InMemorySaver(),
        today=lambda: TODAY,
        now=(lambda: now) if now else (lambda: datetime.now(UTC)),
    )
    return d, ad


def sent(d: Dispatcher, lead_id: Any) -> list[dict[str, Any]]:
    return [
        m for m in d.store.messages(lead_id) if m["status"] == "sent" and m["channel"] != "slack"
    ]


def test_hot_lead_end_to_end_crm_reply_and_slack() -> None:
    d, ad = make()
    lead, created = d.intake(payload(), enqueue=False)
    out = d.process(str(lead.id))
    got = d.store.get(lead.id)
    assert created and out["status"] == "done" and got and got.stage == "with_owner"
    assert got.contact and got.contact["email"] == got.contact["email"].lower()  # LEAD-010
    assert got.assessment and got.assessment["tier"] == 1  # computed from the rubric in code
    assert len(sent(d, lead.id)) == 1  # LEAD-003 (mock email)
    (slack,) = ad.slack.messages  # LEAD-004: summary, rationale, thread, CRM link, owner by rule
    p = slack["payload"]
    assert p["rationale"] and p["thread"][0]["from"] == "lead" and p["crm_url"]
    assert p["owner"] == "@reservations" and slack["channel"] == "#leads"
    assert out["events"] == ["upsert_crm", "assess", "draft", "send", "route"]


def test_replayed_webhook_creates_one_lead_and_one_contact() -> None:
    d, _ = make()
    p = payload()
    first, _ = d.intake(p, enqueue=False)
    replays = [d.intake(dict(p), enqueue=False) for _ in range(4)]
    assert {r[0].id for r in replays} == {first.id} and not any(r[1] for r in replays)  # LEAD-005
    # Same person on another channel, differently formatted email -> same lead.
    other, created = d.intake({**p, "channel": "email", "email": p["email"].upper()}, enqueue=False)
    assert other.id == first.id and not created
    d.process(str(first.id))
    assert d.process(str(first.id))["skipped"]  # a re-delivered job doesn't re-run the flow


def test_warm_lead_follow_ups_cancelled_by_opt_out() -> None:
    d, _ = make(extraction=WARM)
    lead, _ = d.intake(payload(), enqueue=False)
    d.process(str(lead.id))
    got = d.store.get(lead.id)
    assert got and got.stage == "nurturing"  # tier 2 -> follow-ups scheduled
    before = len(sent(d, lead.id))
    after = d.event(str(lead.id), "lead_replied", "Not interested, please STOP")
    assert after.stage == "opted_out" and after.opted_out  # opt-out phrase in a reply (LEAD-020)
    assert d.follow_up(str(lead.id), 1) == "skipped"
    assert len(sent(d, lead.id)) == before  # 0 messages after the event (LEAD-013)


def test_takeover_stops_follow_ups() -> None:
    d, _ = make(extraction=WARM)
    lead, _ = d.intake(payload(), enqueue=False)
    d.process(str(lead.id))
    d.event(str(lead.id), "human_takeover")
    assert d.follow_up(str(lead.id), 1) == "skipped" and d.follow_up(str(lead.id), 2) == "skipped"


def test_approve_first_pauses_then_sends_on_approval() -> None:
    d, _ = make(mode="approve_first")
    lead, _ = d.intake(payload(), enqueue=False)
    out = d.process(str(lead.id))
    got = d.store.get(lead.id)
    assert out["status"] == "waiting_approval" and got and got.stage == "awaiting_approval"
    assert sent(d, lead.id) == []  # nothing goes before a person approves (LEAD-017)
    assert got.approval_id is not None
    d.approvals.decide(got.approval_id, "approved", "test")
    d.resume(str(lead.id), "approved")
    got = d.store.get(lead.id)
    assert got and got.stage == "with_owner" and len(sent(d, lead.id)) == 1


def test_approve_first_rejected_sends_nothing() -> None:
    d, _ = make(mode="approve_first", extraction=WARM)
    lead, _ = d.intake(payload(), enqueue=False)
    d.process(str(lead.id))
    got = d.store.get(lead.id)
    assert got and got.approval_id
    d.approvals.decide(got.approval_id, "rejected", "test")
    d.resume(str(lead.id), "rejected")
    got = d.store.get(lead.id)
    assert got and got.stage == "held" and sent(d, lead.id) == []


def test_outbound_off_holds_but_hot_lead_still_reaches_a_person() -> None:
    d, ad = make(mode="off")
    lead, _ = d.intake(payload(), enqueue=False)
    d.process(str(lead.id))
    got = d.store.get(lead.id)
    assert (
        got and got.stage == "with_owner" and sent(d, lead.id) == [] and len(ad.slack.messages) == 1
    )


def test_not_allowlisted_recipient_gets_nothing() -> None:
    d, _ = make(allow="+910000000000")
    lead, _ = d.intake(payload(), enqueue=False)
    d.process(str(lead.id))
    statuses = [m["status"] for m in d.store.messages(lead.id) if m["channel"] != "slack"]
    assert statuses == ["blocked_not_allowlisted"]


def test_spam_gets_no_reply() -> None:
    d, ad = make(extraction={**HOT, "intent": "spam", "rationale": "Prize scam."})
    lead, _ = d.intake(payload(message="You won an iPhone!!!"), enqueue=False)
    d.process(str(lead.id))
    got = d.store.get(lead.id)
    assert (
        got and got.stage == "closed" and d.store.messages(lead.id) == [] and not ad.slack.messages
    )


def test_whatsapp_outside_24h_window_uses_template() -> None:
    later = datetime.now(UTC) + timedelta(hours=30)
    d, ad = make(extraction=WARM, now=later)
    lead, _ = d.intake(payload("whatsapp", email=None), enqueue=False)
    d.process(str(lead.id))
    (msg,) = sent(d, lead.id)
    assert msg["template"] == "palmgrove_enquiry_ack" and "template" in ad.whatsapp.sent[0]


def test_whatsapp_inside_window_free_text() -> None:
    d, ad = make(extraction=WARM)
    lead, _ = d.intake(payload("whatsapp", email=None), enqueue=False)
    d.process(str(lead.id))
    (msg,) = sent(d, lead.id)
    assert msg["template"] is None and ad.whatsapp.sent[0]["body"].startswith("Hi Asha")


def test_api_intake_is_idempotent_and_approval_enqueues_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    import api.main as m
    from core.db import connect

    d, _ = make(mode="approve_first")
    monkeypatch.setattr(m, "_dispatcher", d)
    client = TestClient(m.app)
    body = {k: v for k, v in payload().items() if k != "received_at"}
    first = client.post("/agents/lead/intake", json=body)
    again = client.post("/agents/lead/intake", json=body)
    assert first.status_code == 202 and again.status_code == 200
    lead_id = first.json()["lead_id"]
    assert again.json()["lead_id"] == lead_id and not again.json()["created"]
    assert (
        client.post("/agents/lead/intake", json={**body, "email": None, "phone": None}).status_code
        == 422
    )

    d.process(lead_id)
    got = d.store.get(lead_id)
    assert got and got.approval_id
    r = client.post(f"/approvals/{got.approval_id}/decision", json={"decision": "approved"})
    assert r.status_code == 200
    with connect(d.settings.database_url) as c:
        job = c.execute(
            "SELECT payload FROM jobs WHERE kind = 'dispatcher.resume' AND key = %s",
            (f"lead:{lead_id}:resume:{got.approval_id}",),
        ).fetchone()
    assert job and job["payload"] == {"lead_id": lead_id, "decision": "approved"}

    ev = client.post(f"/agents/lead/{lead_id}/events", json={"kind": "human_takeover"})
    assert ev.status_code == 200 and ev.json()["stage"] == "with_owner"
    detail = client.get(f"/agents/lead/{lead_id}").json()
    assert detail["lead"]["stage"] == "with_owner" and detail["events"]
    assert client.get(f"/agents/lead/{uuid4()}").status_code == 404


def test_failed_job_alerts_ops_with_run_id_and_marks_lead_failed() -> None:
    from core.jobs import Job

    d, ad = make()
    lead, _ = d.intake(payload(), enqueue=False)
    run_id = uuid4()
    job = Job(
        id=uuid4(),
        kind="dispatcher.process",
        key=None,
        run_at=datetime.now(UTC),
        payload={"lead_id": str(lead.id)},
        status="failed",
        attempts=3,
        max_attempts=3,
        last_error="boom",
        run_id=run_id,
    )
    d.on_job_failure(job, "TimeoutError: model")
    (alert,) = ad.slack.messages
    assert alert["channel"] == "#ops-alerts" and str(run_id) in alert["text"]  # LEAD-005
    got = d.store.get(lead.id)
    assert got and got.stage == "failed" and got.stopped


def test_retried_job_resumes_from_checkpoint_after_a_model_error() -> None:
    class Flaky(FakeModel):
        calls = 0

        def complete(self, messages: Any, tools: Any = None, **kw: Any) -> ModelResponse:
            if messages[0]["content"].startswith("You qualify") and Flaky.calls == 0:
                Flaky.calls += 1
                raise TimeoutError("model timeout")
            return super().complete(messages, tools, **kw)

    d, _ = make()
    d.client = Flaky(HOT)
    lead, _ = d.intake(payload(), enqueue=False)
    with pytest.raises(TimeoutError):
        d.process(str(lead.id))
    got = d.store.get(lead.id)
    assert got and got.stage == "in_crm"  # crashed in assess
    assert d.process(str(lead.id))["skipped"]  # a duplicate delivery doesn't restart it
    d.process(str(lead.id), retry=True)  # the worker's retry resumes at assess
    got = d.store.get(lead.id)
    assert got and got.stage == "with_owner" and len(sent(d, lead.id)) == 1

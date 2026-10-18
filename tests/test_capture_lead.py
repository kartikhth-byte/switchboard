"""SUPP-002: lead capture creates new contacts only, once per conversation, without leaking
whether an email is already a guest."""

from typing import Any
from uuid import uuid4

from agents.concierge.loop import new_state
from agents.concierge.tools import ToolDeps, build_registry
from core.tools import ToolContext
from integrations.crm import MockCRM
from integrations.slack import MockSlack
from tests.test_concierge_loop import BOOKINGS, FakeKB

LEAD = {"name": "Asha", "email": "asha@example.org", "intent": "group_booking", "guests": 8,
        "check_in": "2026-12-02", "check_out": "2026-12-06", "notes": "family reunion"}  # fmt: skip


def setup(crm: MockCRM | None = None) -> tuple[Any, MockCRM]:
    crm = crm or MockCRM()
    deps = ToolDeps(kb=FakeKB(), crm=crm, slack=MockSlack(), bookings=BOOKINGS,
                    state=new_state("c1"), rules={})  # fmt: skip
    return build_registry(deps), crm


def run(reg: Any, args: dict[str, Any]) -> Any:
    return reg.execute("capture_lead", args, ToolContext(run_id=uuid4(), agent="concierge"))


def test_new_lead_creates_contact_without_verification() -> None:
    reg, crm = setup()
    r = run(reg, LEAD)
    assert r.ok and r.payload == {"lead": "received"}
    (op, w), = crm.writes  # fmt: skip
    assert op == "create_contact" and w["email"] == "asha@example.org"
    assert w["stage"] == "new_lead" and w["guests"] == 8 and w["check_in"] == "2026-12-02"


def test_existing_contact_or_guest_is_never_edited_and_reply_is_identical() -> None:
    crm = MockCRM(contacts={"asha@example.org": {"email": "asha@example.org", "vip": True}})
    reg, crm = setup(crm)
    assert run(reg, LEAD).payload == {"lead": "received"}
    guest_email = next(iter(BOOKINGS.values()))["email"]
    reg2, crm2 = setup()
    assert run(reg2, {**LEAD, "email": guest_email}).payload == {"lead": "received"}
    assert crm.writes == [] and crm2.writes == []
    assert crm.contacts["asha@example.org"] == {"email": "asha@example.org", "vip": True}


def test_once_per_conversation() -> None:
    reg, crm = setup()
    run(reg, LEAD)
    run(reg, {**LEAD, "email": "someone.else@example.org"})
    assert len(crm.writes) == 1


def test_rejects_bad_email() -> None:
    reg, crm = setup()
    r = run(reg, {**LEAD, "email": "not-an-email"})
    assert not r.ok and r.error and r.error.startswith("invalid_arguments") and crm.writes == []

"""SUPP-005: every Concierge tool failure path returns a ToolResult error (never raises)."""

from typing import Any
from uuid import uuid4

import pytest

from agents.concierge.loop import new_state
from agents.concierge.tools import ToolDeps, build_registry
from core.tools import ToolContext
from integrations.crm import MockCRM
from integrations.slack import MockSlack
from tests.test_concierge_loop import BOOKINGS, GUEST, FakeKB


class BrokenKB:
    def search(self, query: str, k: int = 5) -> Any:
        raise ConnectionError("db down")


def reg(rules: dict[str, Any] | None = None, kb: Any = None, verified: bool = False) -> Any:
    st = new_state("c")
    if verified:
        st.verified = {"booking_ref": GUEST["booking_ref"], "email": GUEST["email"]}
    rules = rules if rules is not None else {"max_identity_attempts": 3}
    deps = ToolDeps(kb=kb or FakeKB(), crm=MockCRM(), slack=MockSlack(), bookings=BOOKINGS,
                    state=st, rules=rules)  # fmt: skip
    return build_registry(deps)


def run(r: Any, tool: str, args: dict[str, Any]) -> Any:
    return r.execute(tool, args, ToolContext(run_id=uuid4(), agent="concierge"))


TICKET = {"subject": "AC broken", "body": "No cooling", "category": "maintenance"}


@pytest.mark.parametrize(
    ("tool", "args", "kw", "error"),
    [
        ("create_ticket", TICKET, {}, "identity_not_verified"),
        ("update_crm_contact", {"phone": "+91 90000 00000"}, {}, "identity_not_verified"),
        ("quote_refund", {}, {}, "identity_not_verified"),
        ("update_crm_contact", {}, {"verified": True}, "invalid_arguments: nothing to update"),
        ("quote_refund", {}, {"verified": True}, "requires_human: no refund policy"),
        (
            "create_ticket",
            {**TICKET, "category": "refund_request"},
            {
                "verified": True,
                "rules": {"approval_required": {"create_ticket": {"category": ["refund_request"]}}},
            },
            "approval_required",
        ),  # fmt: skip
        ("lookup_booking", {"booking_ref": "PG-9999", "email": "x@example.com"}, {}, "not_found"),
        (
            "lookup_booking",
            {"booking_ref": "bad", "email": "x@example.com"},
            {},
            "invalid_arguments",
        ),
        ("search_kb", {"query": "pool"}, {"kb": BrokenKB()}, ""),  # handler error -> ToolResult
        ("handoff_to_human", {"reason": "nope", "summary": "x"}, {}, "invalid_arguments"),
        ("no_such_tool", {}, {}, "unknown_tool"),
    ],
)
def test_failure_returns_error(
    tool: str, args: dict[str, Any], kw: dict[str, Any], error: str
) -> None:
    r = run(reg(**kw), tool, args)
    assert not r.ok and r.error and r.error.startswith(error)


def test_lookups_stop_after_limit() -> None:
    r = reg()
    args = [{"booking_ref": f"PG-99{i:02d}", "email": "x@example.com"} for i in range(4)]
    errs = [run(r, "lookup_booking", a).error for a in args]
    assert errs[0].startswith("not_found") and errs[2].startswith("requires_human")
    assert errs[3].startswith("requires_human")

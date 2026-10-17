from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field

from core.tools import (
    RetryableToolError,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)


class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    k: int = Field(default=5, ge=1, le=20)


class TicketIn(BaseModel):
    subject: str
    body: str


class SendIn(BaseModel):
    to: str
    text: str


def make_registry() -> tuple[ToolRegistry, dict[str, int]]:
    calls = {"search": 0, "ticket": 0, "send": 0}
    reg = ToolRegistry()

    def search(inp: SearchIn, ctx: ToolContext) -> dict[str, Any]:
        calls["search"] += 1
        return {"hits": [inp.query] * inp.k}

    def ticket(inp: TicketIn, ctx: ToolContext) -> dict[str, Any]:
        calls["ticket"] += 1
        return {"ticket_id": f"T-{calls['ticket']}"}

    def send(inp: SendIn, ctx: ToolContext) -> dict[str, Any]:
        calls["send"] += 1
        return {"sent_to": inp.to}

    reg.register(
        ToolSpec(
            name="search_kb",
            description="Search",
            input_model=SearchIn,
            permission="read",
            side_effect=False,
        ),
        search,
        agents=["support"],
    )
    reg.register(
        ToolSpec(
            name="create_ticket",
            description="Ticket",
            input_model=TicketIn,
            permission="write",
            side_effect=True,
        ),
        ticket,
        agents=["support"],
    )
    reg.register(
        ToolSpec(
            name="send_whatsapp",
            description="Send",
            input_model=SendIn,
            permission="external_send",
            side_effect=True,
        ),
        send,
        agents=["lead"],
    )
    return reg, calls


def ctx(agent: str = "support", **kw: Any) -> ToolContext:
    return ToolContext(run_id=kw.pop("run_id", uuid4()), agent=agent, **kw)


def test_valid_call_returns_typed_result() -> None:
    reg, _ = make_registry()
    r = reg.execute("search_kb", {"query": "checkout time", "k": 2}, ctx())
    assert isinstance(r, ToolResult) and r.ok
    assert r.payload == {"hits": ["checkout time", "checkout time"]}


@pytest.mark.parametrize(
    "args", [{}, {"query": ""}, {"query": "x", "k": 99}, {"query": 5, "k": "many"}, None]
)
def test_invalid_args_rejected_before_execution(args: dict[str, Any] | None) -> None:
    reg, calls = make_registry()
    r = reg.execute("search_kb", args, ctx())
    assert not r.ok and r.error is not None and r.error.startswith("invalid_arguments")
    assert calls["search"] == 0  # handler never ran (SHR-002)


def test_unknown_tool_and_permission() -> None:
    reg, calls = make_registry()
    assert reg.execute("drop_tables", {}, ctx()).error == "unknown_tool: drop_tables"
    r = reg.execute("send_whatsapp", {"to": "+1", "text": "hi"}, ctx("support"))
    assert r.error is not None and r.error.startswith("permission_denied")
    assert calls["send"] == 0


def test_for_agent_and_schemas() -> None:
    reg, _ = make_registry()
    assert [s.name for s in reg.for_agent("support")] == ["search_kb", "create_ticket"]
    schema = reg.schemas_for("lead")[0]["function"]
    assert schema["name"] == "send_whatsapp"
    assert set(schema["parameters"]["required"]) == {"to", "text"}


def test_side_effect_is_idempotent_within_run() -> None:
    reg, calls = make_registry()
    c = ctx()
    args = {"subject": "Leak", "body": "Tap leaking in villa 3"}
    first = reg.execute("create_ticket", args, c)
    again = reg.execute("create_ticket", args, c)
    assert first == again and calls["ticket"] == 1
    # A different run is a different action.
    reg.execute("create_ticket", args, ctx())
    assert calls["ticket"] == 2


def test_outbound_off_by_default() -> None:
    reg, calls = make_registry()
    r = reg.execute("send_whatsapp", {"to": "+1", "text": "hi"}, ctx("lead"))
    assert r.error == "outbound_disabled: OUTBOUND_MODE=off"
    assert calls["send"] == 0


def test_approve_first_requires_approval_then_runs() -> None:
    reg, calls = make_registry()
    run_id = uuid4()
    args = {"to": "+1", "text": "hi"}
    pending = reg.execute(
        "send_whatsapp", args, ctx("lead", run_id=run_id, outbound_mode="approve_first")
    )
    assert pending.error == "approval_required"
    assert pending.payload == {"tool": "send_whatsapp", "arguments": args}
    assert calls["send"] == 0
    done = reg.execute(
        "send_whatsapp",
        args,
        ctx("lead", run_id=run_id, outbound_mode="approve_first", approved=True),
    )
    assert done.ok and calls["send"] == 1


def test_handler_errors_become_results() -> None:
    reg = ToolRegistry()
    attempts = {"n": 0}

    def flaky(inp: TicketIn, ctx: ToolContext) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RetryableToolError("upstream timeout")
        return {"ok": True}

    def broken(inp: TicketIn, ctx: ToolContext) -> dict[str, Any]:
        raise KeyError("secret-ish detail")

    for name, h in [("flaky", flaky), ("broken", broken)]:
        reg.register(
            ToolSpec(
                name=name,
                description=name,
                input_model=TicketIn,
                permission="write",
                side_effect=True,
            ),
            h,
            agents=["support"],
        )
    c = ctx()
    args = {"subject": "s", "body": "b"}
    first = reg.execute("flaky", args, c)
    assert not first.ok and first.retryable
    assert reg.execute("flaky", args, c).ok  # failure wasn't cached, so the retry runs

    r = reg.execute("broken", args, c)
    assert r.error == "tool_error: KeyError" and not r.retryable  # no exception text leaks


def test_duplicate_registration_rejected() -> None:
    reg, _ = make_registry()
    spec = reg.for_agent("support")[0]
    with pytest.raises(ValueError):
        reg.register(spec, lambda i, c: {}, agents=["support"])

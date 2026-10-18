import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from core.config.settings import get_settings
from core.logs import JsonFormatter, RunContextFilter
from core.models import ModelClient, ModelSpec
from core.runs import PgRunStore, start_run
from core.tools import ToolContext, ToolRegistry, ToolSpec
from core.tracing import trace_id_for
from tests.fakes import FakeStore

EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module")
def langfuse() -> Iterator[Langfuse]:
    lf = Langfuse(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        base_url="http://localhost:9",
        span_exporter=EXPORTER,
        flush_at=1,
    )
    yield lf
    lf.shutdown()


@pytest.fixture(autouse=True)
def _clear() -> None:
    EXPORTER.clear()


class EchoIn(BaseModel):
    text: str


def registry_capturing(seen: list[ToolContext]) -> ToolRegistry:
    reg = ToolRegistry()

    def echo(inp: EchoIn, ctx: ToolContext) -> dict[str, Any]:
        seen.append(ctx)
        return {"echo": inp.text}

    reg.register(
        ToolSpec(
            name="echo", description="e", input_model=EchoIn, permission="read", side_effect=False
        ),
        echo,
        agents=["concierge"],
    )
    return reg


CLIENT = ModelClient(ModelSpec(provider="anthropic", name="claude-sonnet-4-5"))
MSGS = [{"role": "user", "content": "hello"}]


def spans(lf: Langfuse) -> list[Any]:
    lf.flush()
    return list(EXPORTER.get_finished_spans())


def test_run_id_propagates_to_store_trace_tool_and_logs(
    langfuse: Langfuse, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.handler.addFilter(RunContextFilter())
    caplog.set_level(logging.INFO)
    store, seen = FakeStore(), []
    reg = registry_capturing(seen)

    with start_run("concierge", store, langfuse, run_input={"q": "hello"}) as run:
        run.model(CLIENT, MSGS, mock_response="hi")
        run.model(CLIENT, MSGS, mock_response="again")
        assert run.tool(reg, "echo", {"text": "x"}).ok

    rec = store.runs[run.run_id]
    # Postgres row (fake here): status, accumulated usage, trace id.
    assert rec.status == "done" and rec.ended_at is not None
    assert rec.tokens_in == run.tokens_in > 0 and rec.cost_usd > 0
    assert rec.trace_id == trace_id_for(run.run_id)
    # Tool calls get the run id.
    assert seen[0].run_id == run.run_id and seen[0].agent == "concierge"
    # Logs: every line from inside the run carries the run id.
    run_lines = [r for r in caplog.records if getattr(r, "event", None)]
    assert run_lines and all(getattr(r, "run_id", None) == str(run.run_id) for r in run_lines)
    line = json.loads(JsonFormatter().format(run_lines[1]))
    assert line["run_id"] == str(run.run_id) and line["event"] == "model_call"
    assert line["latency_ms"] >= 0
    # Traces: one trace, root agent span + 2 generations + 1 tool, all same trace id.
    ss = spans(langfuse)
    assert {format(s.context.trace_id, "032x") for s in ss} == {run.trace_id}
    types = sorted(s.attributes["langfuse.observation.type"] for s in ss)
    assert types == ["agent", "generation", "generation", "tool"]
    gen = next(s for s in ss if s.attributes["langfuse.observation.type"] == "generation")
    assert json.loads(gen.attributes["langfuse.observation.usage_details"])["input"] > 0
    assert json.loads(gen.attributes["langfuse.observation.cost_details"])["total"] > 0
    assert all(s.end_time > s.start_time for s in ss)  # latency is measurable per observation


def test_failed_run_is_recorded_and_reraised(langfuse: Langfuse) -> None:
    store = FakeStore()
    with pytest.raises(ZeroDivisionError), start_run("concierge", store, langfuse) as run:
        _ = 1 / 0
    rec = store.runs[run.run_id]
    assert rec.status == "failed" and rec.error == "ZeroDivisionError"


def test_explicit_status_kept(langfuse: Langfuse) -> None:
    store = FakeStore()
    with start_run("concierge", store, langfuse) as run:
        run.set_status("waiting_approval")
    assert store.runs[run.run_id].status == "waiting_approval"


def test_runs_without_langfuse_keys() -> None:
    store = FakeStore()
    with start_run("concierge", store, None) as run:
        run.model(CLIENT, MSGS, mock_response="hi")
    assert store.runs[run.run_id].status == "done"
    assert store.runs[run.run_id].tokens_in > 0


@pytest.mark.integration
def test_pg_run_store_roundtrip() -> None:
    from core.db import migrate

    dsn = get_settings().database_url
    migrate(dsn)
    store = PgRunStore(dsn)
    with start_run("concierge", store, None) as run:
        run.model(CLIENT, MSGS, mock_response="hi")
        run.model(CLIENT, MSGS, mock_response="hi")
    row = store.get(run.run_id)
    assert row is not None and row.status == "done" and row.ended_at is not None
    assert row.tokens_in == run.tokens_in and row.cost_usd == pytest.approx(run.cost_usd)
    assert migrate(dsn) == []  # idempotent


@pytest.mark.integration
def test_eval_runs_do_not_count_toward_daily_cap() -> None:
    from uuid import uuid4

    from core.db import migrate

    dsn = get_settings().database_url
    migrate(dsn)
    store = PgRunStore(dsn)
    before = store.spend_today()
    live, ev = uuid4(), uuid4()
    store.create(live, "concierge", "t1")
    store.create(ev, "concierge", "t2", kind="eval")
    store.add_usage(live, 1, 1, 0.25)
    store.add_usage(ev, 1, 1, 5.0)
    assert store.spend_today() == pytest.approx(before + 0.25)

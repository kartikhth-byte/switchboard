"""Run lifecycle: one `start_run` per agent invocation (SHR-004, SHR-006).

    with start_run("concierge", store, langfuse) as run:
        resp = run.model(client, messages, tools=registry.schemas_for("support"))
        result = run.tool(registry, call.name, call.arguments)

Inside the block, every log line carries run_id, every model and tool call is a child
observation of the run's Langfuse trace, and tokens/cost accumulate on the `runs` row.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import Any
from uuid import UUID, uuid4

from langfuse import Langfuse

from core.jobs.retry import RetryPolicy
from core.logs import current_agent, current_run_id
from core.models import Message, ModelClient, ModelResponse
from core.runs.models import RunStatus
from core.runs.store import RunStore
from core.tools import ToolContext, ToolRegistry, ToolResult
from core.tools.registry import OutboundMode
from core.tracing import trace_id_for

log = logging.getLogger(__name__)


class RunHandle:
    def __init__(
        self,
        run_id: UUID,
        agent: str,
        store: RunStore,
        langfuse: Langfuse | None,
        outbound_mode: OutboundMode,
    ) -> None:
        self.run_id = run_id
        self.agent = agent
        self.trace_id = trace_id_for(run_id)
        self._store = store
        self._lf = langfuse
        self._outbound_mode = outbound_mode
        self.status: RunStatus = "running"
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0

    def _observe(self, **kw: Any) -> AbstractContextManager[Any]:
        if self._lf is None:
            return nullcontext(None)
        return self._lf.start_as_current_observation(**kw)

    def model(
        self,
        client: ModelClient,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        name: str = "llm",
        **extra: Any,
    ) -> ModelResponse:
        with self._observe(
            name=name,
            as_type="generation",
            input=messages,
            model=client.spec.model_id,
            model_parameters={"temperature": client.spec.temperature},
        ) as gen:
            t0 = time.perf_counter()
            resp = client.complete(messages, tools, **extra)
            latency_ms = round((time.perf_counter() - t0) * 1000)
            if gen is not None:
                gen.update(
                    output={
                        "content": resp.content,
                        "tool_calls": [tc.model_dump() for tc in resp.tool_calls],
                    },
                    model=resp.model,
                    usage_details={"input": resp.tokens_in, "output": resp.tokens_out},
                    cost_details={"total": resp.cost_usd},
                    metadata={"used_fallback": resp.used_fallback},
                )
        self.tokens_in += resp.tokens_in
        self.tokens_out += resp.tokens_out
        self.cost_usd += resp.cost_usd
        self._store.add_usage(self.run_id, resp.tokens_in, resp.tokens_out, resp.cost_usd)
        log.info(
            "model call",
            extra={
                "event": "model_call",
                "model": resp.model,
                "latency_ms": latency_ms,
                "tokens_in": resp.tokens_in,
                "tokens_out": resp.tokens_out,
                "cost_usd": resp.cost_usd,
            },
        )
        return resp

    def tool_context(self, **overrides: Any) -> ToolContext:
        kw: dict[str, Any] = {
            "run_id": self.run_id,
            "agent": self.agent,
            "outbound_mode": self._outbound_mode,
        }
        kw.update(overrides)
        return ToolContext(**kw)

    def tool(
        self,
        registry: ToolRegistry,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        retry: RetryPolicy | None = None,
        **ctx_overrides: Any,
    ) -> ToolResult:
        with self._observe(name=name, as_type="tool", input=arguments) as span:
            t0 = time.perf_counter()
            result = registry.execute(
                name, arguments, self.tool_context(**ctx_overrides), retry=retry
            )
            latency_ms = round((time.perf_counter() - t0) * 1000)
            if span is not None:
                span.update(
                    output=result.model_dump(),
                    level="DEFAULT" if result.ok else "WARNING",
                    status_message=result.error,
                )
        log.info(
            "tool call",
            extra={"event": "tool_call", "tool": name, "latency_ms": latency_ms},
        )
        return result

    def set_status(self, status: RunStatus) -> None:
        """End the run early with a non-default status (waiting_approval, budget_exceeded)."""
        self.status = status


@contextmanager
def start_run(
    agent: str,
    store: RunStore,
    langfuse: Langfuse | None,
    *,
    outbound_mode: OutboundMode = "off",
    run_input: Any = None,
    session_id: str | None = None,
) -> Iterator[RunHandle]:
    run = RunHandle(uuid4(), agent, store, langfuse, outbound_mode)
    store.create(run.run_id, agent, run.trace_id)
    tok_run = current_run_id.set(str(run.run_id))
    tok_agent = current_agent.set(agent)
    obs: AbstractContextManager[Any] = (
        langfuse.start_as_current_observation(
            name=agent,
            as_type="agent",
            trace_context={"trace_id": run.trace_id},
            input=run_input,
            metadata={"run_id": str(run.run_id), "session_id": session_id},
        )
        if langfuse is not None
        else nullcontext(None)
    )
    error: str | None = None
    try:
        with obs as root:
            log.info("run started", extra={"event": "run_started"})
            try:
                yield run
            except Exception as exc:
                run.status = "failed"
                error = type(exc).__name__  # type only: messages can carry user data
                if root is not None:
                    root.update(level="ERROR", status_message=error)
                raise
            if run.status == "running":
                run.status = "done"
            if root is not None:
                root.update(
                    metadata={
                        "run_id": str(run.run_id),
                        "status": run.status,
                        "tokens_in": run.tokens_in,
                        "tokens_out": run.tokens_out,
                        "cost_usd": run.cost_usd,
                    }
                )
    finally:
        if run.status == "running":  # tracing failed before the body ran
            run.status, error = "failed", error or "run_setup_failed"
        store.finish(run.run_id, run.status, error)
        log.info("run finished", extra={"event": "run_finished", "cost_usd": run.cost_usd})
        current_run_id.reset(tok_run)
        current_agent.reset(tok_agent)

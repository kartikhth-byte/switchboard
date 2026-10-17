"""Tool registry: schemas, permissions, idempotency and typed results (SHR-002).

Every tool call goes through `ToolRegistry.execute`, which in order:
  1. rejects unknown tools and tools not granted to the calling agent,
  2. validates arguments against the tool's Pydantic model *before* running anything,
  3. gates `external_send` on OUTBOUND_MODE (LEAD-017) and `requires_approval` (SHR-008),
  4. de-duplicates side-effecting calls by idempotency key (SHR-007),
  5. runs the handler and always returns a `ToolResult` (never raises).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

log = logging.getLogger(__name__)

Permission = Literal["read", "write", "external_send"]
OutboundMode = Literal["off", "allowlist", "approve_first"]
MAX_ERROR_CHARS = 300


class ToolSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_model: type[BaseModel]
    permission: Permission
    side_effect: bool
    requires_approval: bool = False


class ToolResult(BaseModel):
    ok: bool
    payload: dict[str, Any] | None = None
    error: str | None = None
    retryable: bool = False


class RetryableToolError(Exception):
    """Raise from a handler for transient failures (timeouts, 5xx)."""


# Handlers get validated input plus the call context; return a payload dict or a ToolResult.
Handler = Callable[[Any, "ToolContext"], "dict[str, Any] | ToolResult"]


@dataclass(frozen=True)
class ToolContext:
    run_id: UUID
    agent: str
    outbound_mode: OutboundMode = "off"
    approved: bool = False  # set when re-executing a call an ApprovalRequest approved
    idempotency_key: str | None = None  # override; otherwise derived from run + tool + args


class IdempotencyStore(Protocol):
    def get(self, key: str) -> ToolResult | None: ...
    def put(self, key: str, result: ToolResult) -> None: ...


@dataclass
class InMemoryIdempotencyStore:
    """Default for tests. A Postgres-backed store replaces it with the jobs table (SHR-007)."""

    _data: dict[str, ToolResult] = field(default_factory=dict)

    def get(self, key: str) -> ToolResult | None:
        return self._data.get(key)

    def put(self, key: str, result: ToolResult) -> None:
        self._data[key] = result


@dataclass(frozen=True)
class _Entry:
    spec: ToolSpec
    handler: Handler
    agents: frozenset[str]


def _fail(error: str, *, retryable: bool = False) -> ToolResult:
    return ToolResult(ok=False, error=error[:MAX_ERROR_CHARS], retryable=retryable)


def idempotency_key(run_id: UUID, tool: str, args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{run_id}|{tool}|{canonical}".encode()).hexdigest()


class ToolRegistry:
    def __init__(self, idempotency: IdempotencyStore | None = None) -> None:
        self._tools: dict[str, _Entry] = {}
        self._idem: IdempotencyStore = idempotency or InMemoryIdempotencyStore()

    def register(self, spec: ToolSpec, handler: Handler, *, agents: Iterable[str]) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        granted = frozenset(agents)
        if not granted:
            raise ValueError(f"tool {spec.name} must be granted to at least one agent")
        self._tools[spec.name] = _Entry(spec, handler, granted)

    def for_agent(self, agent: str) -> list[ToolSpec]:
        return [e.spec for e in self._tools.values() if agent in e.agents]

    def schemas_for(self, agent: str) -> list[dict[str, Any]]:
        """OpenAI-style tool schemas (LiteLLM translates them per provider)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.input_model.model_json_schema(),
                },
            }
            for s in self.for_agent(agent)
        ]

    def execute(self, name: str, arguments: dict[str, Any] | None, ctx: ToolContext) -> ToolResult:
        entry = self._tools.get(name)
        if entry is None:
            return _fail(f"unknown_tool: {name}")
        if ctx.agent not in entry.agents:
            return _fail(f"permission_denied: {name} not granted to {ctx.agent}")
        spec = entry.spec

        if arguments is None:
            return _fail("invalid_arguments: arguments were not valid JSON")
        try:
            parsed = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            fields = ", ".join(".".join(map(str, e["loc"])) or "<root>" for e in exc.errors())
            return _fail(f"invalid_arguments: {fields}")

        if spec.permission == "external_send":
            if ctx.outbound_mode == "off":
                return _fail("outbound_disabled: OUTBOUND_MODE=off")
            # allowlist: the adapter enforces the recipient allowlist itself.
        needs_approval = spec.requires_approval or (
            spec.permission == "external_send" and ctx.outbound_mode == "approve_first"
        )
        if needs_approval and not ctx.approved:
            return ToolResult(
                ok=False,
                error="approval_required",
                payload={"tool": name, "arguments": parsed.model_dump(mode="json")},
            )

        key: str | None = None
        if spec.side_effect:
            key = ctx.idempotency_key or idempotency_key(
                ctx.run_id, name, parsed.model_dump(mode="json")
            )
            cached = self._idem.get(key)
            if cached is not None:
                return cached

        try:
            out = entry.handler(parsed, ctx)
            result = out if isinstance(out, ToolResult) else ToolResult(ok=True, payload=out)
        except RetryableToolError as exc:
            result = _fail(f"{type(exc).__name__}: {exc}", retryable=True)
        except Exception as exc:  # noqa: BLE001 — a tool bug must not crash the agent loop
            log.exception("tool %s failed (run %s)", name, ctx.run_id)
            result = _fail(f"tool_error: {type(exc).__name__}")

        # Cache only successes, so a failed side effect can be retried with the same key.
        if key is not None and result.ok:
            self._idem.put(key, result)
        return result

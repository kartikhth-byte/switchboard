"""Model adapter over LiteLLM with token/cost accounting (SHR-001).

Agents never import a provider SDK. They build a `ModelClient` from their config's
`model:` block, so switching provider is a config change.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import litellm
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
litellm.suppress_debug_info = True

Message = dict[str, Any]


class ModelSpec(BaseModel):
    """The `model:` block of an agent config, e.g.
    {provider: anthropic, name: claude-sonnet-4-5, fallback: openai/gpt-4o-mini}."""

    provider: str
    name: str
    fallback: str | None = None  # full LiteLLM id: "<provider>/<name>"
    temperature: float = 0.0
    max_tokens: int = 1024
    timeout_s: float = 60.0

    @property
    def model_id(self) -> str:
        return f"{self.provider}/{self.name}"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] | None  # None = model emitted invalid JSON; the registry rejects it
    raw_arguments: str = ""


class ModelResponse(BaseModel):
    content: str | None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    model: str  # the model that actually served the call (primary or fallback)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    used_fallback: bool = False


class ModelError(RuntimeError):
    """Primary and fallback both failed."""


class ModelClient:
    def __init__(self, spec: ModelSpec) -> None:
        self.spec = spec

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        **extra: Any,
    ) -> ModelResponse:
        """One completion. Falls back once to `spec.fallback` if the primary raises.

        `extra` is passed to LiteLLM (tests use `mock_response`).
        """
        candidates = [self.spec.model_id] + ([self.spec.fallback] if self.spec.fallback else [])
        last_exc: Exception | None = None
        for i, model in enumerate(candidates):
            try:
                raw = litellm.completion(
                    model=model,
                    messages=messages,
                    tools=tools or None,
                    temperature=self.spec.temperature,
                    max_tokens=self.spec.max_tokens,
                    timeout=self.spec.timeout_s,
                    **extra,
                )
            except Exception as exc:  # noqa: BLE001 — any provider error triggers fallback
                # Type only: provider errors can echo request details.
                log.warning("model call failed on %s: %s", model, type(exc).__name__)
                last_exc = exc
                continue
            return _to_response(raw, model, used_fallback=i > 0)
        raise ModelError(f"all models failed: {candidates}") from last_exc


def _to_response(raw: Any, model: str, *, used_fallback: bool) -> ModelResponse:
    msg = raw.choices[0].message
    usage = getattr(raw, "usage", None)
    try:
        cost = float(litellm.completion_cost(completion_response=raw, model=model))
    except Exception:  # noqa: BLE001 — unknown pricing must not break a run
        log.warning("no pricing for %s; cost recorded as 0", model)
        cost = 0.0
    return ModelResponse(
        content=msg.content,
        tool_calls=[_to_tool_call(tc) for tc in (msg.tool_calls or [])],
        model=model,
        tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0),
        tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
        cost_usd=cost,
        used_fallback=used_fallback,
    )


def _to_tool_call(tc: Any) -> ToolCall:
    raw_args = tc.function.arguments or ""
    try:
        args = json.loads(raw_args) if raw_args else {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
    except ValueError:
        return ToolCall(id=tc.id, name=tc.function.name, arguments=None, raw_arguments=raw_args)
    return ToolCall(id=tc.id, name=tc.function.name, arguments=args, raw_arguments=raw_args)

"""Langfuse client factory (SHR-006). Returns None when keys are absent, so tracing is optional."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from langfuse import Langfuse

from core.config.settings import Settings
from core.redaction import langfuse_mask


def trace_id_for(run_id: UUID) -> str:
    """Deterministic 32-hex Langfuse trace id: a run's trace is findable from its run_id."""
    return Langfuse.create_trace_id(seed=str(run_id))


def make_langfuse(settings: Settings, **overrides: Any) -> Langfuse | None:
    """PII is masked before export (SHR-019). `overrides` lets tests inject `span_exporter`."""
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    return Langfuse(
        public_key=settings.langfuse_public_key.get_secret_value(),
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        base_url=settings.langfuse_host,
        environment=settings.app_env,
        **{"mask": langfuse_mask, **overrides},
    )

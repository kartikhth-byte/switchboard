"""SHR-019. The canary test plants known values and searches the *exported* traces and
log output for those exact strings. It deliberately does not reuse the redactor's
regexes, so a gap in the patterns shows up as a failure."""

import io
import json
import logging
from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel

from core.config.settings import Settings
from core.logs import JsonFormatter, RunContextFilter
from core.models import ModelClient, ModelSpec
from core.redaction import redact
from core.runs import start_run
from core.tools import ToolContext, ToolRegistry, ToolSpec
from core.tracing import make_langfuse
from tests.fakes import FakeStore

CANARIES = [
    "ravi.canary@example.com",
    "Priya_Test+goa@palmgrove-stays.test",
    "+1 (555) 013-7788",
    "+91 98450 12345",
    "9845012345",
    "4111 1111 1111 1111",  # Visa test number
    "5555-5555-5555-4444",  # Mastercard test number
    "378282246310005",  # Amex test number
    "nine eight four five zero one two three four five",
    "double five five, zero one three, seven seven eight eight",
]

MUST_SURVIVE = [
    "2026-10-17",
    "2026-10-17T16:32:34.938931+00:00",
    "529735cb-abfb-4d6d-9261-f1aca1889045",
    "8f3a1b2c-4321-1234-5678-9abc12345678",
    "388b3c4e9ab71f51bebcb43e25fd7850",
    "booking PG-1042",
    "₹12,500 for 3 nights",
    "check-in at 14:00, check-out 11:00",
    "one or two guests",
]


@pytest.mark.parametrize("value", CANARIES)
def test_each_canary_redacted(value: str) -> None:
    out = redact(f"Guest said: {value}, thanks")
    assert value not in out and "Guest said:" in out


@pytest.mark.parametrize("value", MUST_SURVIVE)
def test_no_over_redaction(value: str) -> None:
    assert redact(value) == value


def test_non_luhn_long_number_is_not_a_card() -> None:
    # 16 digits failing Luhn -> not a card; still caught as phone-like only if ≤15 digits.
    assert "[CARD]" not in redact("ref 4111 1111 1111 1112")


class ContactIn(BaseModel):
    email: str
    phone: str
    note: str


def test_canaries_absent_from_exported_traces_and_logs() -> None:
    exporter = InMemorySpanExporter()
    settings = Settings(
        langfuse_public_key="pk-lf-redaction-test",
        langfuse_secret_key="sk-lf-redaction-test",
        langfuse_host="http://localhost:9",
    )
    lf = make_langfuse(settings, span_exporter=exporter, flush_at=1)
    assert lf is not None

    log_buf = io.StringIO()
    handler = logging.StreamHandler(log_buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RunContextFilter())
    logger = logging.getLogger("canary")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    reg = ToolRegistry()

    def update_contact(inp: ContactIn, ctx: ToolContext) -> dict[str, Any]:
        return {"saved": inp.model_dump()}  # echoes PII back, as real CRMs do

    reg.register(
        ToolSpec(
            name="update_crm_contact",
            description="u",
            input_model=ContactIn,
            permission="write",
            side_effect=True,
        ),
        update_contact,
        agents=["concierge"],
    )
    blob = " | ".join(CANARIES)
    client = ModelClient(ModelSpec(provider="anthropic", name="claude-sonnet-4-5"))
    try:
        with start_run("concierge", FakeStore(), lf, run_input={"transcript": blob}) as run:
            run.model(client, [{"role": "user", "content": blob}], mock_response=f"Noted {blob}")
            run.tool(
                reg,
                "update_crm_contact",
                {"email": CANARIES[0], "phone": CANARIES[3], "note": blob},
            )
            logger.info("guest details: %s", blob)
        lf.flush()
    finally:
        logger.removeHandler(handler)

    spans = exporter.get_finished_spans()
    assert len(spans) == 3  # agent, generation, tool
    exported = json.dumps([dict(s.attributes or {}) for s in spans], default=str)
    logs = log_buf.getvalue()
    assert "[EMAIL]" in exported and "[PHONE]" in exported and "[CARD]" in exported
    leaks = [c for c in CANARIES if c in exported or c in logs]
    assert leaks == [], f"PII leaked to traces/logs: {leaks}"
    assert str(run.run_id) in logs  # correlation ids survive redaction
    lf.shutdown()

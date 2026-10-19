"""Lead assessment and reply drafting (LEAD-002, LEAD-003).

The model extracts details, picks the intent and judges fit; the tier is then computed in code
from the rubric (agents/dispatcher/config/labels.md), so the same facts always get the same tier
and the rule is testable. Output is schema-validated; one retry with the validation error, then
the job fails into the retry/failure queue.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from core.config.agent import AgentConfig
from core.models import ModelClient
from core.runs import RunHandle

Intent = Literal[
    "new_booking",
    "group_booking",
    "long_stay",
    "event",
    "availability_question",
    "partnership",
    "existing_guest",
    "spam",
]
TIER_NAMES = {1: "hot", 2: "warm", 3: "cool", 4: "cold"}


class Extraction(BaseModel):
    intent: Intent
    guests: int | None = Field(default=None, ge=1, le=200)
    check_in: date | None = None
    check_out: date | None = None
    nights: int | None = Field(default=None, ge=1, le=365)
    villa: str | None = None
    budget: str | None = None
    occasion: str | None = None
    fit: bool = True
    rationale: str = Field(min_length=3, max_length=400)
    missing: list[str] = Field(default_factory=list)


class LeadAssessment(Extraction):
    tier: int = Field(ge=1, le=4)
    tier_name: str
    days_ahead: int | None = None


def tier_for(intent: str, days_ahead: int | None, guests: int | None, fit: bool) -> int:
    """labels.md rubric. Mirrors data/generators/lead_evals.tier_for (a test keeps them equal)."""
    if intent in ("partnership", "spam", "existing_guest"):
        return 4
    if not fit or intent == "availability_question":
        return 3
    if days_ahead is not None and 0 <= days_ahead <= 90 and guests:
        return 1
    return 2


def finalize(x: Extraction, today: date) -> LeadAssessment:
    days = (x.check_in - today).days if x.check_in else None
    t = tier_for(x.intent, days, x.guests, x.fit)
    return LeadAssessment(**x.model_dump(), tier=t, tier_name=TIER_NAMES[t], days_ahead=days)


def _json(text: str | None) -> Any:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    return json.loads(m.group() if m else "{}")


def assess(
    run: RunHandle, client: ModelClient, cfg: AgentConfig, payload: dict[str, Any], today: date
) -> LeadAssessment:
    labels = (cfg.root / "config/labels.md").read_text(encoding="utf-8").split("## Tier")[0]
    prompt = cfg.prompt(
        "assess",
        today=today.isoformat(),
        intents=", ".join(cfg.rules["intents"]),
        labels=labels,
        channel=payload.get("channel", "web_form"),
        message=payload.get("message", ""),
    )
    messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        resp = run.model(client, messages, name=f"assess-{attempt}")
        try:
            return finalize(Extraction.model_validate(_json(resp.content)), today)
        except (ValidationError, ValueError) as exc:
            messages += [
                {"role": "assistant", "content": resp.content or ""},
                {
                    "role": "user",
                    "content": f"That JSON was invalid: {str(exc)[:400]}. "
                    "Return corrected JSON only.",
                },
            ]
    raise ValueError("assessment did not validate after a retry")


class Draft(BaseModel):
    channel: Literal["whatsapp", "email"]
    subject: str = ""
    body: str = Field(min_length=10, max_length=2000)


def reply_channel(payload: dict[str, Any], contact: dict[str, Any]) -> Literal["whatsapp", "email"]:
    if payload.get("channel") == "whatsapp" and contact.get("phone"):
        return "whatsapp"
    return "email" if contact.get("email") else "whatsapp"


def draft_reply(
    run: RunHandle,
    client: ModelClient,
    cfg: AgentConfig,
    payload: dict[str, Any],
    a: LeadAssessment,
    channel: Literal["whatsapp", "email"],
) -> Draft:
    facts = {
        k: v
        for k, v in a.model_dump(mode="json").items()
        if k
        in ("intent", "guests", "check_in", "check_out", "nights", "villa", "occasion", "budget")
        and v is not None
    }
    prompt = cfg.prompt(
        "draft",
        channel=channel,
        length="WhatsApp: under 70 words" if channel == "whatsapp" else "email: under 140 words",
        missing=", ".join(a.missing) or "nothing",
        facts=json.dumps(facts),
        message=payload.get("message", ""),
    )
    resp = run.model(client, [{"role": "user", "content": prompt}], name="draft")
    data = _json(resp.content)
    return Draft(
        channel=channel,
        subject=str(data.get("subject") or "Your Palmgrove Stays enquiry"),
        body=str(data.get("body") or ""),
    )

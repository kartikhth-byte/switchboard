"""Lead state machine (LEAD-019): stages change only through these events."""

from __future__ import annotations

TRANSITIONS: dict[tuple[str, str], str] = {
    # Intake (normalize + dedupe) creates the lead at "normalized"; replays never make a new one.
    ("normalized", "crm_upserted"): "in_crm",
    ("in_crm", "assessed"): "assessed",
    ("assessed", "drafted"): "drafted",
    ("assessed", "no_reply"): "closed",  # spam / existing guest: no sales reply
    ("drafted", "approval_requested"): "awaiting_approval",
    ("drafted", "sent"): "contacted",
    ("drafted", "held"): "held",  # outbound off, or recipient not allowlisted
    ("held", "routed"): "with_owner",  # hot leads still go to a person when outbound is off
    ("awaiting_approval", "approved_sent"): "contacted",
    ("awaiting_approval", "rejected"): "held",
    ("contacted", "routed"): "with_owner",  # hot lead posted to Slack
    ("contacted", "follow_up_scheduled"): "nurturing",
    ("contacted", "follow_up_sent"): "nurturing",
    ("nurturing", "follow_up_sent"): "nurturing",
    ("nurturing", "follow_up_scheduled"): "nurturing",
}
# Events that can happen at any stage once the lead exists (they stop automation).
ANY_STAGE: dict[str, str] = {
    "lead_replied": "replied",
    "human_takeover": "with_owner",
    "opt_out": "opted_out",
    "failed": "failed",
}
TERMINAL = {"closed", "opted_out"}


class InvalidTransition(ValueError):
    pass


def next_stage(stage: str, event: str) -> str:
    if event in ANY_STAGE:
        if stage in TERMINAL and event != "opt_out":
            raise InvalidTransition(f"{stage} --{event}-->")
        return ANY_STAGE[event]
    try:
        return TRANSITIONS[(stage, event)]
    except KeyError:
        raise InvalidTransition(f"{stage} --{event}-->") from None

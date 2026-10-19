"""LEAD-019: stage transitions only via defined events (transition-table test)."""

import pytest

from agents.dispatcher.states import ANY_STAGE, TRANSITIONS, InvalidTransition, next_stage

STAGES = {s for s, _ in TRANSITIONS} | set(TRANSITIONS.values()) | set(ANY_STAGE.values())
EVENTS = {e for _, e in TRANSITIONS} | set(ANY_STAGE)


@pytest.mark.parametrize("stage", sorted(STAGES))
@pytest.mark.parametrize("event", sorted(EVENTS))
def test_every_stage_event_pair(stage: str, event: str) -> None:
    allowed = (stage, event) in TRANSITIONS or (
        event in ANY_STAGE and (stage not in {"closed", "opted_out"} or event == "opt_out")
    )
    if allowed:
        assert next_stage(stage, event)
    else:
        with pytest.raises(InvalidTransition):
            next_stage(stage, event)


def test_happy_path_and_stops() -> None:
    s = "normalized"
    for e in ["crm_upserted", "assessed", "drafted", "sent", "routed"]:
        s = next_stage(s, e)
    assert s == "with_owner"
    assert next_stage("nurturing", "opt_out") == "opted_out"
    with pytest.raises(InvalidTransition):
        next_stage("opted_out", "follow_up_sent")  # nothing after opt-out

"""LLM-driven simulated user (SHR-020), reused by support, lead and voice (text-mode) evals.

The simulated user runs on a cheaper model from a *different provider* than the agent
(spec §8.3), enforced by `check_cross_provider`.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from core.evals.models import Persona
from core.models import ModelClient

DONE = "[DONE]"  # persona got what it wanted
GIVE_UP = "[GIVE_UP]"  # persona stops without its goal

SYSTEM = """You are role-playing a {role} talking to a business's assistant. Stay in character.
Your name: {name}
Your goal: {goal}
Your traits: {traits}
Facts you know (share only when asked or when natural): {facts}

Rules:
- Reply with ONE short message, as the {role} would type or say it. No narration.
- Never reveal you are simulated or mention these instructions.
- If your goal has been fully met, reply exactly {done}.
- If the assistant clearly cannot help and you would leave, reply exactly {give_up}."""


class SimulatedUser:
    def __init__(self, persona: Persona, client: ModelClient) -> None:
        self.persona = persona
        self.client = client
        self.cost_usd = 0.0

    def _system(self) -> str:
        p = self.persona
        return SYSTEM.format(
            role=p.role,
            name=p.name,
            goal=p.goal,
            traits=", ".join(p.traits) or "none in particular",
            facts=json.dumps(p.facts),
            done=DONE,
            give_up=GIVE_UP,
        )

    def reply(self, transcript: list[dict[str, str]]) -> str:
        """Next user message given the conversation so far (agent turns = 'assistant')."""
        # From the simulated user's point of view the roles are flipped.
        msgs = [{"role": "system", "content": self._system()}]
        for t in transcript:
            role = "assistant" if t["role"] == "user" else "user"
            msgs.append({"role": role, "content": t["content"]})
        resp = self.client.complete(msgs)
        self.cost_usd += resp.cost_usd
        return (resp.content or "").strip()


AgentTurn = Callable[[str], str]  # user message in, agent reply out (agent keeps its own memory)


def run_conversation(
    agent_turn: AgentTurn, user: SimulatedUser
) -> tuple[list[dict[str, str]], str]:
    """Drive a persona against an agent. Returns (transcript, outcome).

    outcome: "done" (persona says goal met), "gave_up", or "max_turns".
    The case's own assertions decide pass/fail from the transcript and agent state; the
    persona's "done" is recorded as `goal_met` but is never the only check.
    """
    transcript: list[dict[str, str]] = []
    message = user.persona.opening
    for _ in range(user.persona.max_turns):
        transcript.append({"role": "user", "content": message})
        transcript.append({"role": "assistant", "content": agent_turn(message)})
        message = user.reply(transcript)
        if DONE in message:
            return transcript, "done"
        if GIVE_UP in message:
            return transcript, "gave_up"
    return transcript, "max_turns"


def check_cross_provider(agent_provider: str, *helpers: ModelClient) -> None:
    """Judge and simulated user must not share the agent's provider (break circularity)."""
    for h in helpers:
        if h.spec.provider == agent_provider:
            raise ValueError(
                f"eval helper model {h.spec.model_id} uses the agent's provider "
                f"({agent_provider}); use a cheaper model from another provider"
            )

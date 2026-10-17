from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class Persona(BaseModel):
    """A simulated user (SHR-020): customer, lead or caller."""

    name: str
    role: str  # "guest", "lead", "caller"
    goal: str  # what they want, in plain words
    traits: list[str] = Field(default_factory=list)  # "impatient", "vague", "changes mind"
    facts: dict[str, str] = Field(default_factory=dict)  # what they know: booking_ref, email…
    opening: str  # first message, fixed for reproducibility
    max_turns: int = 8


class EvalCase(BaseModel):
    id: str
    agent: str
    tags: list[str] = Field(default_factory=list)  # category: happy_path | edge | adversarial
    input: dict[str, Any] | None = None  # single-turn input, or
    persona: Persona | None = None  # multi-turn simulated user
    expected: dict[str, Any] = Field(default_factory=dict)
    assertions: list[str]

    @model_validator(mode="after")
    def _one_input(self) -> EvalCase:
        if (self.input is None) == (self.persona is None):
            raise ValueError(f"case {self.id}: set exactly one of input / persona")
        if not self.assertions:
            raise ValueError(f"case {self.id}: needs at least one assertion")
        return self


class AgentOutput(BaseModel):
    """What an agent target returns for one case."""

    answer: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)  # structured output, tool calls, state
    transcript: list[dict[str, str]] = Field(default_factory=list)  # multi-turn cases
    trace_id: str = ""
    cost_usd: float = 0.0


class EvalResult(BaseModel):
    case_id: str
    passed: bool
    metrics: dict[str, float]
    trace_id: str
    cost_usd: float
    tags: list[str] = Field(default_factory=list)
    split: str = "all"
    error: str | None = None
    answer: str | None = None  # kept for review and judge calibration
    notes: dict[str, Any] = Field(default_factory=dict)  # e.g. judge verdicts, citations

"""Conversation memory (SHR-013): verbatim window, rolling summary with provenance, facts.

- The last `window_turns` turns are kept verbatim (a turn = one user or assistant message).
- Older turns are folded into `summary`; `summary_source_turns` records which turn ids it
  covers, so any summarised claim can be traced back to the original turns.
- If the rendered context still exceeds `summarize_over_tokens`, more turns are folded in.
- `facts` hold guest details (name, email, booking_ref, dates, issue_type), each with the
  turn id it came from. Email and booking ref are extracted by code; the rest by a small
  structured model call supplied by the agent.
- Per conversation only; no cross-user or long-term memory (out of scope).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from core.models import Message

Summarizer = Callable[[str, list["Turn"]], str]  # (previous summary, turns to fold) -> summary
FactExtractor = Callable[[list["Turn"]], dict[str, str]]  # recent turns -> {field: value}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BOOKING_REF = re.compile(r"\b([A-Za-z]{2})[- ]?(\d{4})\b")


def approx_tokens(text: str) -> int:
    return int(len(text.split()) * 1.3) + 1


class Turn(BaseModel):
    id: int
    role: str  # user | assistant
    content: str


class Fact(BaseModel):
    value: str
    source_turn: int


class MemoryState(BaseModel):
    window: list[Turn] = Field(default_factory=list)
    summary: str = ""
    summary_source_turns: list[int] = Field(default_factory=list)
    facts: dict[str, Fact] = Field(default_factory=dict)
    next_turn_id: int = 1

    # ---- writing ----

    def add(self, role: str, content: str) -> Turn:
        turn = Turn(id=self.next_turn_id, role=role, content=content)
        self.next_turn_id += 1
        self.window.append(turn)
        if role == "user":
            self._extract_by_code(turn)
        return turn

    def remember(self, field: str, value: str, source_turn: int) -> None:
        """Store a fact. Later turns overwrite earlier ones (the guest corrected themselves)."""
        value = value.strip()
        if value:
            self.facts[field] = Fact(value=value, source_turn=source_turn)

    def _extract_by_code(self, turn: Turn) -> None:
        if m := EMAIL.search(turn.content):
            self.remember("email", m.group().lower(), turn.id)
        if m := BOOKING_REF.search(turn.content):
            self.remember("booking_ref", f"{m.group(1).upper()}-{m.group(2)}", turn.id)

    def extract_facts(self, extractor: FactExtractor, last_n: int = 2) -> None:
        recent = self.window[-last_n:]
        users = [t.id for t in recent if t.role == "user"]
        if not users:
            return
        source = max(users)  # facts come from what the guest said
        for field, value in extractor(recent).items():
            if field in ("email", "booking_ref"):
                continue  # code extraction wins for these
            self.remember(field, value, source)

    def compact(
        self, summarizer: Summarizer, window_turns: int, summarize_over_tokens: int
    ) -> list[int]:
        """Fold overflowing/oldest turns into the summary. Returns the turn ids folded."""
        fold: list[Turn] = []
        while len(self.window) > window_turns:
            fold.append(self.window.pop(0))
        # Still too big (long messages): fold more, but always keep the latest exchange.
        while len(self.window) > 2 and self.rendered_tokens() > summarize_over_tokens:
            fold.append(self.window.pop(0))
        if not fold:
            return []
        self.summary = summarizer(self.summary, fold).strip()
        self.summary_source_turns = sorted({*self.summary_source_turns, *(t.id for t in fold)})
        return [t.id for t in fold]

    # ---- reading ----

    def memory_note(self) -> str | None:
        parts: list[str] = []
        if self.facts:
            facts = "; ".join(
                f"{k}: {f.value} (turn {f.source_turn})" for k, f in sorted(self.facts.items())
            )
            parts.append(f"Known facts about the guest — reuse these, don't ask again: {facts}.")
        if self.summary:
            src = self.summary_source_turns
            span = f"turns {src[0]}–{src[-1]}" if src else "earlier turns"
            parts.append(f"Summary of the earlier conversation ({span}): {self.summary}")
        return "\n\n".join(parts) or None

    def render(self) -> list[Message]:
        msgs: list[Message] = []
        if (note := self.memory_note()) is not None:
            msgs.append({"role": "system", "content": note})
        msgs += [{"role": t.role, "content": t.content} for t in self.window]
        return msgs

    def rendered_tokens(self) -> int:
        return approx_tokens(
            (self.memory_note() or "") + " " + " ".join(t.content for t in self.window)
        )

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

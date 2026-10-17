"""Model-backed summarizer and fact extractor for MemoryState (SHR-013).

Both take a `complete` callable so the agent routes them through its run
(`lambda msgs: run.model(client, msgs, name="memory")`): traced, costed, budgeted.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from core.memory.memory import FactExtractor, Summarizer, Turn
from core.models import Message, ModelResponse

Complete = Callable[[list[Message]], ModelResponse]

SUMMARY_PROMPT = """Update the running summary of a customer-support conversation.
Keep: what the guest wants, decisions made, promises given, open issues, any booking details.
Drop: greetings and small talk. Max 120 words. Plain text, third person.

Current summary:
{summary}

New turns to fold in:
{turns}"""

FACTS_PROMPT = """Extract facts the guest stated about themselves from these turns.
Return JSON only, with any of these keys that are present: {fields}.
Use the guest's own words for values. Omit keys that aren't stated. No guesses.

{turns}"""


def _fmt(turns: list[Turn]) -> str:
    return "\n".join(f"[turn {t.id}] {t.role}: {t.content}" for t in turns)


def make_summarizer(complete: Complete) -> Summarizer:
    def summarize(previous: str, turns: list[Turn]) -> str:
        prompt = SUMMARY_PROMPT.format(summary=previous or "(none)", turns=_fmt(turns))
        return (complete([{"role": "user", "content": prompt}]).content or previous).strip()

    return summarize


def make_fact_extractor(complete: Complete, fields: list[str]) -> FactExtractor:
    def extract(turns: list[Turn]) -> dict[str, str]:
        prompt = FACTS_PROMPT.format(fields=", ".join(fields), turns=_fmt(turns))
        text = complete([{"role": "user", "content": prompt}]).content or "{}"
        m = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            data = json.loads(m.group() if m else text)
        except ValueError:
            return {}  # a bad extraction never breaks the turn
        if not isinstance(data, dict):
            return {}
        return {k: str(v) for k, v in data.items() if k in fields and v not in (None, "", [])}

    return extract

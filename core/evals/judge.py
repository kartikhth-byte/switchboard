"""LLM judge for qualities code can't check (groundedness, personalization) — spec §8.2/8.4.

Calibration against 30 hand labels happens per rubric in M2/M3 (`agreement`); a judge
below 85% agreement gets its rubric fixed before its numbers are published.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from core.models import ModelClient

JUDGE_SYSTEM = """You are a strict evaluator. Apply the rubric exactly. Reply with JSON only:
{"score": 0 or 1, "reason": "<one sentence>"}"""


class Verdict(BaseModel):
    score: int = Field(ge=0, le=1)
    reason: str = ""


class Judge:
    def __init__(self, client: ModelClient) -> None:
        self.client = client
        self.cost_usd = 0.0

    def grade(self, rubric: str, material: dict[str, str]) -> Verdict:
        body = "\n\n".join(f"## {k}\n{v}" for k, v in material.items())
        resp = self.client.complete(
            [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": f"# Rubric\n{rubric}\n\n{body}"},
            ]
        )
        self.cost_usd += resp.cost_usd
        text = resp.content or ""
        match = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            return Verdict.model_validate(json.loads(match.group() if match else text))
        except (ValueError, ValidationError):
            # Unparseable verdict counts as a fail, visibly.
            return Verdict(score=0, reason="judge_output_unparseable")


def agreement(judge_scores: list[int], human_labels: list[int]) -> float:
    """Judge–human agreement on the calibration set."""
    if len(judge_scores) != len(human_labels) or not human_labels:
        raise ValueError("need equal-length, non-empty score lists")
    return sum(j == h for j, h in zip(judge_scores, human_labels, strict=True)) / len(human_labels)

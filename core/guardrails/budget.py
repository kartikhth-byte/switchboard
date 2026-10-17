"""Spend guardrails (SHR-018): per-run token budget and daily USD cap.

Checked before every model call. Exceeding either raises `BudgetExceeded`, which
`start_run` turns into status `budget_exceeded` plus a user-visible message.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RUN_TOKEN_BUDGET = 30_000

USER_MESSAGES = {
    "run_token_budget": (
        "Sorry — this conversation has hit its length limit. "
        "Please start a new chat, or ask to speak to a person."
    ),
    "daily_usd_cap": (
        "The demo has reached today's usage limit. Please try again tomorrow, "
        "or ask to speak to a person."
    ),
}


class BudgetExceeded(Exception):
    def __init__(self, limit: str, detail: str) -> None:
        super().__init__(f"{limit}: {detail}")
        self.limit = limit
        self.user_message = USER_MESSAGES[limit]


@dataclass(frozen=True)
class Budget:
    run_tokens: int = DEFAULT_RUN_TOKEN_BUDGET
    daily_usd: float | None = None  # None = no daily cap (unit tests)

    def check(self, run_tokens_used: int, spent_today_usd: float | None) -> None:
        if run_tokens_used >= self.run_tokens:
            raise BudgetExceeded("run_token_budget", f"{run_tokens_used}/{self.run_tokens}")
        if self.daily_usd is not None and spent_today_usd is not None:
            if spent_today_usd >= self.daily_usd:
                raise BudgetExceeded(
                    "daily_usd_cap", f"${spent_today_usd:.4f}/${self.daily_usd:.2f}"
                )
